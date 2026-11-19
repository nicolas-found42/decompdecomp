"""Project-owned PS2 SDK selection and cached evidence for the desktop and helpers."""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from collections import Counter, OrderedDict
from pathlib import Path

from ... import kits
from ...app import workers
from ...network import NetworkError
from . import sdk_libraries

SETTINGS_KEY = "sdk_library_copies"
REVIEWS_KEY = "sdk_library_reviews"
KIT = "ps2-libraries"
MAX_INDEXES = 6
REFRESH_SECONDS = 15
REASON_CHARS = 1000
IDENT = re.compile(r"[A-Za-z_]\w*\Z", re.ASCII)
_indexes = OrderedDict()
_index_lock = threading.Lock()


def _copies() -> dict[str, Path]:
    return {hashlib.sha256(str(folder).encode()).hexdigest()[:24]: folder for folder in kits.folders(KIT)}


def choices() -> list[dict]:
    """Only the owner sees local paths. Project config stores opaque identifiers."""
    return [{"id": key, "path": str(path)} for key, path in _copies().items()]


def select(net, slug: str, selection: list) -> None:
    """Select SDK copies already registered in Setup."""
    available = _copies()
    if not isinstance(selection, list) or any(not isinstance(key, str) or key not in available for key in selection):
        raise NetworkError("Choose SDK library folders registered in Compilers & tools.")
    net._update_cfg(slug, lambda config: config.__setitem__(SETTINGS_KEY, list(dict.fromkeys(selection))))


def start_scan(net, proj) -> str:
    """Use the same low-priority project queue for selection, manual scans, and refreshes."""
    def run(log):
        report = scan(net, net.project_row(proj["slug"]), log)
        return {"archives": report["archives"], "functions": report["functions"], "hits": len(report["hits"])}

    key = json.dumps(net.cfg(proj).get(SETTINGS_KEY) or [])
    jobs = net.project_jobs
    jid = jobs.submit(proj["slug"], "Scan SDK libraries", lambda log: workers.pool().submit(run, log).result(), key=key)
    net.__dict__.setdefault("_sdk_library_next_scan", {})[proj["id"]] = time.monotonic() + REFRESH_SECONDS
    return jid


def maybe_refresh(net, proj) -> None:
    """Check for changed archive files or function boundaries while the project is in use."""
    if (proj["slug"] in net.links or not getattr(net, "project_jobs", None)
            or not net.cfg(proj).get(SETTINGS_KEY)):
        return
    due = net.__dict__.setdefault("_sdk_library_next_scan", {})
    now = time.monotonic()
    if due.get(proj["id"], 0) > now:
        return
    due[proj["id"]] = now + REFRESH_SECONDS
    try:
        start_scan(net, proj)
    except NetworkError:
        pass  # the project's pending slot may be in use; the next check can schedule the refresh


def _index_for(folder: Path, check_cancel):
    def revision():
        files = ((path, path.stat()) for path in sdk_libraries.archives(folder))
        return tuple((path.relative_to(folder), meta.st_ino, meta.st_size, meta.st_mtime_ns, meta.st_ctime_ns)
                     for path, meta in files)

    before = revision()
    with _index_lock:
        if (cached := _indexes.get(folder)) and before == cached[0]:
            _indexes.move_to_end(folder)
            return cached[1]
    index = sdk_libraries.index(folder, check_cancel=check_cancel)
    if revision() != before:
        raise NetworkError("An SDK archive changed during indexing. Run the library scan again.")
    with _index_lock:
        _indexes[folder] = (before, index)
        _indexes.move_to_end(folder)
        while len(_indexes) > MAX_INDEXES:
            _indexes.popitem(last=False)
    return index


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _reports(net) -> dict:
    return net.__dict__.setdefault("_sdk_library_reports", {})


def scan(net, proj, log=print) -> dict:
    """Scan imported function bodies. IDs distinguish overlay twins even at the same address."""
    config = net.cfg(proj)
    selected = config.get(SETTINGS_KEY) or []
    if not selected:
        _reports(net).pop(proj["id"], None)
        return {"selected": [], "missing": 0, "archives": 0, "functions": 0, "hits": []}
    folders = _copies()
    check_cancel = getattr(log, "check_cancel", lambda: None)
    indexes = {}
    missing = 0
    for key in selected:
        check_cancel()
        if key not in folders:
            missing += 1
            continue
        try:
            lib = _index_for(folders[key], check_cancel)
        except OSError:
            missing += 1
            continue
        indexes[lib.fingerprint] = lib
    if not indexes:
        _reports(net).pop(proj["id"], None)
        log("Register the selected SDK library folders in Compilers & tools and scan again.")
        return {"selected": selected, "missing": missing, "archives": 0, "functions": 0, "hits": []}
    log(f"Indexed {sum(lib.archive_count for lib in indexes.values())} local EE archives.")
    rows = net.store.all("SELECT id, name, vram, rom_offset, size, target, relocs FROM functions "
                         "WHERE project_id=? ORDER BY vram,rom_offset,id", proj["id"])
    symbols = net.symbols(proj["id"])
    snapshot = (tuple(selected), tuple(indexes), missing, config.get("gp", 0), symbols, [tuple(row) for row in rows])
    if (cached := _reports(net).get(proj["id"])) and cached[0] == snapshot:
        return cached[1]
    libraries = list(indexes.values())
    names = Counter(row["vram"] for row in rows)
    hits = []
    for row in rows:
        check_cancel()
        code = row["target"]
        if row["vram"] is None or not code or len(code) != row["size"]:
            continue
        relocs = json.loads(row["relocs"] or "[]")
        evidence = sdk_libraries.compare(libraries, code, relocs, symbols.resolve, config.get("gp", 0))
        if not evidence:
            continue
        target = (row["vram"], row["rom_offset"], hashlib.sha256(code).hexdigest())
        # The SDK fingerprint can change because of another archive. Review follows the matching archive member.
        # Per-reference details support a review in this scan. Keep the existing IDs and saved receipts.
        references = [item.pop("references") for item in evidence]
        proofs = [_digest({k: v for k, v in item.items() if k != "sdk"}) for item in evidence]
        scope = _digest([target, relocs, sorted(proofs)])
        for item, proof in zip(evidence, proofs, strict=True):
            item["id"] = _digest([scope, proof])
            item["bulk"] = (item["bulk_evidence"] and names[row["vram"]] == 1
                            and row["name"].startswith(f"func_{row['vram']:08X}")
                            and symbols.resolve(item["name"]) in (None, row["vram"]))
        hits.append({"function_id": row["id"], "function": row["name"], "address": row["vram"],
                     "rom_offset": row["rom_offset"], "target_hash": target[2], "relocs": relocs, "evidence": evidence,
                     "references": {item["id"]: refs for item, refs in zip(evidence, references, strict=True)}})
    occurrences = Counter(name for hit in hits for name in {item["name"] for item in hit["evidence"]})
    for hit in hits:
        for item in hit["evidence"]:
            item["bulk"] = item["bulk"] and occurrences[item["name"]] == 1
    log(f"Compared {len(rows)} imported functions. {len(hits)} have SDK library candidates.")
    report = {"selected": selected, "missing": missing, "archives": sum(lib.archive_count for lib in indexes.values()),
              "functions": len(rows), "hits": hits}
    _reports(net)[proj["id"]] = (snapshot, report, {hit["function_id"]: hit for hit in hits})
    return report


def brief(net, proj, fn) -> list[str]:
    """Reuse scan results for the same imported function while the project settings and body still agree."""
    maybe_refresh(net, proj)
    cached = _reports(net).get(proj["id"])
    if not cached or cached[1]["selected"] != net.cfg(proj).get(SETTINGS_KEY):
        return []
    hit = cached[2].get(fn["id"])
    if not hit or hit["target_hash"] != hashlib.sha256(fn["target"] or b"").hexdigest():
        return []
    dismissed = {key for key, review in reviews(net, proj).items() if review.get("decision") == "dismissed"}
    names = ", ".join(dict.fromkeys(item["name"] for item in hit["evidence"][:5] if item["id"] not in dismissed))
    if not names:
        return []
    return ["", "### SDK library candidates",
            f"{names}. Use ps2_sdk_symbols to inspect the evidence and review_sdk_name to record a decision."]


def reviews(net, proj) -> dict:
    return net.cfg(proj).get(REVIEWS_KEY) or {}


def listing(net, proj) -> dict:
    """Show a scan summary and retained decisions without sending the candidate queue to the desktop."""
    selected = net.cfg(proj).get(SETTINGS_KEY) or []
    cached = _reports(net).get(proj["id"])
    report = cached[1] if cached and cached[1]["selected"] == selected else None
    available = choices()
    active_ids = {choice["id"] for choice in available}
    decisions = reviews(net, proj)
    if report is not None:
        pending = sum(item["id"] not in decisions or decisions[item["id"]]["decision"] == "proposed"
                      for hit in report["hits"] for item in hit["evidence"] if item["name"] != hit["function"])
        report = {key: report[key] for key in ("archives", "functions")} | {"pending": pending}
    saved = save_reviews(net.cfg(proj))
    names = {(row["vram"], row["rom_offset"], row["name"]) for row in net.store.all(
        "SELECT vram,rom_offset,name FROM functions WHERE project_id=?", proj["id"])} if saved else set()
    display = {key: review | {"active": (review["decision"] == "approved"
               and (review["address"], review["rom_offset"], review["evidence"]["name"]) in names)}
               for key, review in saved.items()}
    return {"choices": available, "selected": selected, "report": report, "reviews": display,
            "missing": sum(key not in active_ids for key in selected)}


def save_reviews(config: dict) -> dict:
    return {key: item for key, item in (config.get(REVIEWS_KEY) or {}).items()
            if item.get("decision") in {"approved", "dismissed"}}


def restore_reviews(net, proj, records) -> None:
    """Restore provenance for the saved game bodies without restoring local SDK folder selections."""
    if not isinstance(records, dict) or not records:
        return
    target_hashes = {(row["vram"], row["rom_offset"], hashlib.sha256(row["target"]).hexdigest()) for row in
                     net.store.all("SELECT vram,rom_offset,target FROM functions WHERE project_id=? AND target IS NOT NULL",
                                   proj["id"])}
    allowed = {"name": str, "sdk": str, "archive": str, "archive_hash": str, "member": str, "member_offset": int,
               "extent": int, "relocations": int, "resolved_relocations": int, "layout_agrees": bool,
               "bulk_evidence": bool, "bulk": bool, "id": str}
    accepted = {}
    for key, record in records.items():
        if (not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key) or not isinstance(record, dict)
                or record.get("decision") not in ("approved", "dismissed")):
            continue
        address, offset, digest = (record.get(k) for k in ("address", "rom_offset", "target_hash"))
        if (type(address) is not int or not (offset is None or type(offset) is int) or not isinstance(digest, str)
                or (address, offset, digest) not in target_hashes):
            continue
        evidence = record.get("evidence")
        if (not isinstance(evidence, dict) or evidence.get("id") != key
                or any(type(evidence.get(field)) is not kind for field, kind in allowed.items())):
            continue
        accepted[key] = {"decision": record["decision"], "address": address, "rom_offset": offset, "target_hash": digest,
                         "by": str(record.get("by") or "")[:255], "original_name": str(record.get("original_name") or "")[:255],
                         "evidence": {field: evidence[field] for field in allowed}}
        accepted[key].update(_decision_details(record))
        if isinstance(record.get("history"), list):
            accepted[key]["history"] = [_history_item(previous) for previous in record["history"]
                                        if isinstance(previous, dict)]
    if accepted:
        net._update_cfg(proj["slug"], lambda cfg: cfg.setdefault(REVIEWS_KEY, {}).update(accepted))


def render(report: dict, function_id: int | None, decisions: dict | None = None) -> str:
    """Give clients the evidence for an exact imported function, or list the project's candidates."""
    out = [f"Local SDK archive lookup ({report['archives']} archives, {report['functions']} functions compared)"]
    if report["missing"]:
        out.append(f"{report['missing']} selected SDK folders unavailable. Check Compilers & tools.")
    for hit in report["hits"]:
        if function_id is not None and hit["function_id"] != function_id:
            continue
        visible = [item for item in hit["evidence"]
                   if function_id is not None or (decisions or {}).get(item["id"], {}).get("decision") != "dismissed"]
        if not visible:
            continue
        out.append(f"0x{hit['address']:08X} {hit['function']}")
        for item in visible:
            status = (decisions or {}).get(item["id"], {}).get("decision")
            status = status or ("archive evidence supports applying" if item["bulk"] else "needs corroboration")
            out.append(f"  {item['name']} from {item['archive']}({item['member']}) [SDK {item['sdk'][:12]}]. "
                       f"{item['extent']} bytes, {item['resolved_relocations']}/{item['relocations']} relocations "
                       f"confirmed, {status}. Candidate {item['id']}.")
            if function_id is not None and not item["bulk"]:
                for name, address, known in list(dict.fromkeys(hit["references"][item["id"]]))[:5]:
                    state = "confirmed" if known else "archive name only"
                    out.append(f"    {name} at 0x{address:08X} ({state})")
    if len(out) == 1:
        out.append("No local library candidate.")
    return "\n".join(out)


def _picked(report: dict, ids: list) -> list[tuple[dict, dict]]:
    if not isinstance(ids, list) or not ids or any(not isinstance(key, str) for key in ids):
        raise NetworkError("Choose SDK library candidates from the current scan.")
    requested = set(ids)
    picked = [(hit, item) for hit in report["hits"] for item in hit["evidence"] if item["id"] in requested]
    if len(picked) != len(requested):
        raise NetworkError("SDK library evidence changed. Review the current scan before choosing names.")
    return picked


def _decision_details(record: dict) -> dict:
    return {field: value for field, kind in (("reason", str), ("witness", int), ("decided_at", float))
            if type(value := record.get(field)) is kind}


def _history_item(record: dict) -> dict:
    return {field: str(record.get(field) or "")[:255] for field in ("decision", "by", "original_name")} | _decision_details(record)


def _record(db, proj, picked, decision: str, author: str = "", *, reason="", witness=None) -> None:
    config = json.loads(db.execute("SELECT config FROM projects WHERE id=?", (proj["id"],)).fetchone()["config"])
    saved = config.setdefault(REVIEWS_KEY, {})
    for hit, item in picked:
        previous = saved.get(item["id"], {})
        entry = {"decision": decision, "by": author, "original_name": hit["function"],
                 "address": hit["address"], "rom_offset": hit["rom_offset"],
                 "target_hash": hit["target_hash"], "evidence": item}
        if decision != "proposed":
            entry.update(reason=reason, decided_at=time.time())
            if witness is not None:
                entry["witness"] = witness
            if previous.get("decision") in ("approved", "dismissed"):
                entry["history"] = [*previous.get("history", []), _history_item(previous)]
        saved[item["id"]] = entry
    db.execute("UPDATE projects SET config=? WHERE id=?", (json.dumps(config), proj["id"]))


def propose(net, proj, agent, candidate: str) -> str:
    """Leave a lead other agents can corroborate using the same archive evidence."""
    picked = _picked(scan(net, proj, log=lambda _: None), [candidate])
    hit, item = picked[0]
    previous = reviews(net, proj).get(candidate, {}).get("decision")
    if previous in {"approved", "dismissed"}:
        return f"An agent already {previous} this candidate. Use review_sdk_name to record new evidence."
    with net.store.tx() as db:
        current = json.loads(db.execute("SELECT config FROM projects WHERE id=?", (proj["id"],)).fetchone()["config"])
        if (current.get(REVIEWS_KEY) or {}).get(candidate, {}).get("decision") in {"approved", "dismissed"}:
            return "Another agent already reviewed this candidate."
        _record(db, proj, picked, "proposed", agent.name)
    return f"Proposed {item['name']} for {hit['function']}. Use review_sdk_name when you can support a decision."


def guard_rename(net, proj, agent, old: str, new: str) -> str | None:
    """An SDK candidate needs a review receipt even through the ordinary rename tool."""
    if not net.cfg(proj).get(SETTINGS_KEY):
        return None
    report = scan(net, proj, log=lambda _: None)
    try:
        fid = net._function(old, proj["slug"])["id"]
    except NetworkError:
        addr = net.symbols(proj["id"]).resolve(old)
        rows = net.store.all("SELECT id FROM functions WHERE project_id=? AND vram=?", proj["id"], addr)
        fid = rows[0]["id"] if len(rows) == 1 else None
    candidate = next((item for hit in report["hits"] if hit["function_id"] == fid
                      for item in hit["evidence"] if item["name"] == new), None)
    return propose(net, proj, agent, candidate["id"]) if candidate else None


def _witness(net, proj, report, hit: dict, item: dict, value: str) -> int:
    try:
        address = int(value, 16)
    except (ValueError, TypeError):
        raise NetworkError("Give the hexadecimal start address of a caller, jump target, or referenced symbol.") from None
    if address != hit["address"]:
        supported = [candidate for candidate in hit["evidence"] if candidate["layout_agrees"]
                     and any(dest == address and known for _, dest, known in hit["references"][candidate["id"]])]
        if item in supported and all(candidate["name"] == item["name"] for candidate in supported):
            return address
        # A known caller's archive names its callee even before the game has a name for that address.
        caller = next((c for c in report["hits"] if c["address"] == address), None)
        if caller and all(c["layout_agrees"] and c["name"] == caller["function"] for c in caller["evidence"]):
            rows = net.store.all("SELECT id,name,vram,relocs,target FROM functions WHERE project_id=? AND vram IN (?,?)",
                                 proj["id"], address, hit["address"])
            current = next((row for row in rows if row["id"] == caller["function_id"]), None)
            if (len(rows) == 2 and current and current["name"] == caller["function"]
                    and hashlib.sha256(current["target"] or b"").hexdigest() == caller["target_hash"]
                    and json.loads(current["relocs"] or "[]") == caller["relocs"]
                    and all({name for name, dest, _ in caller["references"][c["id"]] if dest == hit["address"]}
                            == {item["name"]} for c in caller["evidence"])):
                return address
    raise NetworkError("The project has no verified reference at that address that distinguishes this SDK name. "
                       "Check the archive names of a callee or caller, or leave the candidate open.")


def review(net, proj, agent, candidate: str, decision: str, reason: str, witness: str = "") -> str:
    """Let agents act on archive evidence or document corroboration tied to a real reference."""
    if decision not in ("apply", "dismiss"):
        raise NetworkError("Choose apply or dismiss for the SDK candidate.")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > REASON_CHARS:
        raise NetworkError(f"Explain the SDK decision in 1 to {REASON_CHARS} characters.")
    return decide(net, proj, agent, [candidate], decision == "apply", log=lambda _: None,
                  reason=reason.strip(), witness=witness)["message"]


def decide(net, proj, agent, ids: list, approve: bool, log=print, *, reason="", witness="") -> dict:
    """Apply reviewed names through the same match-preserving rename used by the editor."""
    symbols = net.symbols(proj["id"])
    report = scan(net, proj, log=log)
    picked = _picked(report, ids)
    decisions = reviews(net, proj)
    if any(hit["function"] == item["name"] and decisions.get(item["id"], {}).get("decision") == "approved"
           for hit, item in picked):
        if approve:
            return {"message": "This SDK name is already applied."}
        raise NetworkError("Apply a better supported name before dismissing the current SDK name.")
    evidence_address = None
    if reason:
        (hit, item), = picked
        if witness:
            evidence_address = _witness(net, proj, report, hit, item, witness)
        if approve and not item["bulk"] and evidence_address is None:
            raise NetworkError("Give a confirmed caller, jump target, or referenced symbol and explain how it supports this name.")
    if approve and len(picked) > 1 and not all(item["bulk"] for _, item in picked):
        raise NetworkError("Review candidates with ambiguous or incomplete evidence one at a time.")
    if approve and any(not IDENT.fullmatch(item["name"]) for _, item in picked):
        raise NetworkError("An SDK candidate name must be a C identifier before it can name a function.")
    pairs = [(hit["function"], item["name"]) for hit, item in picked if approve and hit["function"] != item["name"]]
    if len({new for _, new in pairs}) != len(pairs):
        raise NetworkError("Two candidates would create the same name. Review them individually.")

    def record(db):
        getattr(log, "check_cancel", lambda: None)()
        config = db.execute("SELECT config FROM projects WHERE id=?", (proj["id"],)).fetchone()
        current = json.loads(config["config"]) if config else {}
        if current.get(SETTINGS_KEY) != report["selected"]:
            raise NetworkError("The selected SDKs changed. Review the current scan before approving names.")
        if net.symbols(proj["id"]) is not symbols or current.get("gp", 0) != net.cfg(proj).get("gp", 0):
            raise NetworkError("The project symbols changed. Review the current SDK scan before choosing names.")
        if evidence_address is not None:
            _witness(net, proj, report, *picked[0], witness)
        for hit, item in picked:
            row = db.execute("SELECT name,vram,rom_offset,target,relocs FROM functions WHERE project_id=? AND id=?",
                             (proj["id"], hit["function_id"])).fetchone()
            expected_name = item["name"] if approve else hit["function"]
            if (not row or row["name"] != expected_name or row["vram"] != hit["address"]
                    or row["rom_offset"] != hit["rom_offset"] or json.loads(row["relocs"] or "[]") != hit["relocs"]
                    or hashlib.sha256(row["target"] or b"").hexdigest() != hit["target_hash"]):
                raise NetworkError("A reviewed function changed. Run the SDK scan again before approving its name.")
        decision = "approved" if approve else "dismissed"
        _record(db, proj, picked, decision, agent.name, reason=reason, witness=evidence_address)
        for hit, item in picked:
            event = f"{decision.title()} {item['name']} at 0x{hit['address']:08X}"
            net._event(db, proj["id"], agent.id, "sdk-review", f"{event}. {reason}" if reason else event)

    if not pairs:
        with net.store.tx() as db:
            record(db)
        return {"message": ("Saved evidence for the existing names." if approve
                            else f"Dismissed {len(picked)} SDK library candidates.")}
    result = net.rename_symbols(agent, proj["slug"], pairs, "Apply SDK library names", record=record)
    if result.startswith("❌"):
        raise NetworkError(result)
    return {"message": result}
