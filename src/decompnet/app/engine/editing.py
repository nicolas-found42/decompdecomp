"""Changing the project: names (rename_symbol), the shared header, notes, splitting a function, function-end
reports, and what the user steers (instructions, title, priorities).
"""

from __future__ import annotations

import hashlib
import json
import re
import time

from ... import arch
from ...network import CLAIM_ORDERS, Agent, NetworkError
from ...rom.symbols import IDENT, Symbol, SymbolTable
from .common import _ADDRESS


class EditMixin:
    def split_function(self, agent: Agent, project: str | None, function: str, at: str, new_name: str = "",
                       *, checked: bool = False) -> str:
        self._rom_only(project, "split_function")
        link = self._link(project)
        if link is not None:
            name = self._function(function, project)["name"]
            text = link.call("/collab/split", agent, function=name, at=at, new_name=new_name)
            if text.startswith("✅"):
                link.sync()
            return text
        fn = self._function(function, project)
        proj = self.store.one("SELECT * FROM projects WHERE id=?", fn["project_id"])
        if not self.is_rom(proj):
            raise NetworkError("split_function is for ROM projects")
        if fn["status"] == "matched":
            raise NetworkError(f"{fn['name']} is matched; it can't be split")
        m = _ADDRESS.match(str(at).strip())
        if not m:
            raise NetworkError("`at` must be an address like 0x80001234")
        addr = int(m.group(1), 16)
        new_name = (new_name or f"func_{addr:08X}").strip()
        syms = self.symbols(proj["id"])
        if not IDENT.match(new_name) or new_name in syms.by_name:
            raise NetworkError(f"`{new_name}` is not a free C identifier")
        console = self.console(proj)
        inside = fn["vram"] <= addr < fn["vram"] + fn["size"]
        if not inside and addr % console.instruction_size(fn) == 0:
            rom, _ = self._rom(proj)
            following = [r["vram"] for r in self.store.all(
                "SELECT vram FROM functions WHERE project_id=? AND vram>?", proj["id"], addr)]
            limit = min(following) if following else fn["vram"] - (fn["rom_offset"] or 0) + len(rom)
            new = console.function_at(rom, addr, limit, new_name)
            if new is not None:
                return self._add_function(proj, fn, new, syms, agent.id, new_name, checked=checked)
        head, tail, head_padding, tail_padding = self._cut(proj, fn, addr, new_name, syms, agent.id, "split",
                                                            f"{fn['name']} / {new_name}", checked=checked)
        if head_padding and tail_padding:
            cont = "Both halves were pure alignment padding and are set aside; there is nothing to continue with."
        elif head_padding:
            cont = (f"The head was pure alignment padding and is set aside; continue with {new_name} "
                    "(get_function shows its assembly).")
        elif tail_padding:
            cont = (f"The tail was pure alignment padding and is set aside; continue with {fn['name']} "
                    "(get_function shows its new assembly).")
        else:
            cont = f"Continue with {fn['name']} (get_function shows its new assembly)."
        return (f"✅ split: {fn['name']} is now {console.function_insns(head)} instructions; {new_name} at "
                f"0x{addr:08X} has {console.function_insns(tail)}. {cont}")

    def _add_function(self, proj, prev, new, syms: SymbolTable, agent_id: int | None, new_name: str,
                      *, checked: bool = False) -> str:
        """Add a function the splitter missed after `prev`; it starts at its own address and mode."""
        console = self.console(proj)
        trial = syms.copy() if checked else syms
        trial.add(Symbol(new_name, new.vram, new.size, "func"))
        target = self._make_target(proj, new, trial)
        meta = console.function_meta(new)
        if checked:
            snapshot = self._check_boundary_matches(proj, trial)
        with self.store.tx() as db:
            if checked:
                self._record_boundary(db, proj, prev, syms, None, snapshot)
                if db.execute("SELECT 1 FROM functions WHERE project_id=? AND COALESCE(unit,'')=COALESCE(?,'') "
                              "AND vram<? AND vram+size>? LIMIT 1",
                              (proj["id"], prev["unit"], new.vram + new.size, new.vram)).fetchone():
                    raise NetworkError("The new function overlaps a function already tracked by this project.")
            db.execute("INSERT INTO functions(project_id,name,size,n_insns,target,relocs,asm,seed,unit,vram,rom_offset,"
                       "status,meta) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (proj["id"], new_name, new.size, console.function_insns(new), target.code,
                        json.dumps(target.to_json()), console.function_asm(new), None, prev["unit"], new.vram, new.rom,
                        console.function_status(new), json.dumps(meta) if meta else None))
            db.execute("INSERT OR REPLACE INTO symbols(project_id,name,address,size,kind) VALUES(?,?,?,?,?)",
                       (proj["id"], new_name, new.vram, new.size, "func"))
            self._event(db, proj["id"], agent_id, "split", f"{prev['name']} / {new_name}")
        self._xrefs.pop(proj["id"], None)
        if checked:
            self._syms.pop(proj["id"], None)
        self._ops.pop(proj["id"], None)
        return f"✅ new function {new_name} at 0x{new.vram:08X}, {new.size} bytes."

    def _cut(self, proj, fn, addr: int, new_name: str, syms: SymbolTable, agent_id: int | None, kind: str, detail: str,
             tail_meta: dict | None = None, *, boundary: dict | None = None, checked: bool = False):
        """Cut a function in two at `addr`: the head keeps its row, the tail becomes `new_name` (status 'asm'
        with `tail_meta` when it isn't code agents should work on). Returns (head, tail, head_padding, tail_padding),
        where each `*_padding` flag says whether that side trimmed down to pure alignment padding and was retired
        ('asm') rather than left open."""
        console = self.console(proj)
        try:
            head, tail = console.split_function(fn, addr, new_name)
        except ValueError as e:
            raise NetworkError(str(e)) from None
        checked = checked or boundary is not None
        trial = syms.copy() if checked else syms
        trial.add(Symbol(new_name, addr, tail.size, "func"))
        old = trial.by_name.get(fn["name"])
        if old is not None:
            old.size = head.size
        th, tt = self._make_target(proj, head, trial), self._make_target(proj, tail, trial)
        if checked:
            if th.code + tt.code != fn["target"] or head.size != addr - fn["vram"]:
                raise NetworkError("The proposed boundary leaves bytes unaccounted for. Check where the extra bytes begin.")
            if console.ends_in_delay_branch(th.code, fn):
                raise NetworkError("The instruction before this boundary still uses the next instruction. Move the boundary.")
            snapshot = self._check_boundary_matches(proj, trial)
        with self.store.tx() as db:
            if checked:
                self._record_boundary(db, proj, fn, syms, boundary, snapshot)
            # The head's extent changed, so its old best attempt no longer describes it (reset seed/best, not just
            # the score). A side trimmed down to pure alignment padding (the processor's nop/filler) is not a
            # function to match — mark it done ('asm'), with an alignment-padding reason for its page, so it leaves
            # the open pool instead of lingering as an open sliver. Both halves are checked: splitting leading
            # padding off a function can leave a padding head, and splitting into trailing padding leaves a padding
            # tail. The console asks the right reader for the function's mode (GBA: ARM vs Thumb).
            head_padding = not checked and bool(head.size) and console.is_padding(th.code, fn)
            # a tail that starts in the head's branch delay slot is not padding: that first word belongs to the
            # head, so don't retire the tail even when its bytes are all the processor's nop.
            tail_padding = ((not checked or (boundary and boundary["tail"] == "padding"))
                            and bool(tail.size) and console.is_padding(tt.code, fn)
                            and not console.ends_in_delay_branch(th.code, fn))
            meta = console.function_meta(tail)
            if tail_meta:
                meta.update(tail_meta)
            if tail_padding and not (tail_meta and tail_meta.get("asm_code")):
                # a caller (resolve_boundary) that already gave a reason keeps it; only unexplained
                # zero fill gets the generic alignment-padding label.
                meta["asm_code"] = "alignment padding (all nops)"
            head_meta = json.loads(fn["meta"] or "{}")
            if head_padding:
                head_meta["asm_code"] = "alignment padding (all nops)"
            db.execute("UPDATE functions SET size=?, n_insns=?, target=?, relocs=?, asm=?, seed=NULL, best_score=NULL, "
                       "best_source=NULL, status=?, meta=? WHERE id=?",
                       (head.size, console.function_insns(head), th.code, json.dumps(th.to_json()),
                        console.function_asm(head), "asm" if head_padding else fn["status"],
                        json.dumps(head_meta) if head_meta else None, fn["id"]))
            if head_padding:
                # the head is retired (set aside as padding); an agent must not keep holding a dead
                # sliver and get it handed back on the next claim (as set_aside_by_you also deletes it).
                db.execute("DELETE FROM claims WHERE function_id=?", (fn["id"],))
            db.execute("INSERT INTO functions(project_id,name,size,n_insns,target,relocs,asm,seed,unit,vram,rom_offset,"
                       "status,meta) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (proj["id"], new_name, tail.size, console.function_insns(tail), tt.code, json.dumps(tt.to_json()),
                        console.function_asm(tail), None, fn["unit"], addr, tail.rom,
                        "asm" if (tail_meta or tail_padding) else console.function_status(tail),
                        json.dumps(meta) if meta else None))
            db.execute("UPDATE symbols SET size=? WHERE project_id=? AND name=?", (head.size, proj["id"], fn["name"]))
            db.execute("INSERT OR REPLACE INTO symbols(project_id,name,address,size,kind) VALUES(?,?,?,?,?)",
                       (proj["id"], new_name, addr, tail.size, "func"))
            self._event(db, proj["id"], agent_id, kind, detail)
        self._xrefs.pop(proj["id"], None)
        if checked:
            self._syms.pop(proj["id"], None)
        self._ops.pop(proj["id"], None)
        return head, tail, head_padding, tail_padding

    def _check_boundary_matches(self, proj, trial):
        """Test the effect of the proposed symbol table without publishing it to other agents."""
        pid = proj["id"]
        matched = self.store.all("SELECT * FROM functions WHERE project_id=? AND status='matched'", pid)
        units = {fn["unit"] for fn in matched}
        headers = {unit: dict(self._context(pid, unit) or {}) for unit in units}
        broken = self.regression_test(proj, None, only=matched, header_for=lambda fn: headers[fn["unit"]].get("header", ""),
                                      scope=lambda: self._using_symbols(pid, trial))
        if broken:
            raise NetworkError(f"The boundary would break the existing match for {broken[0][0]}.")
        return matched, headers

    def _record_boundary(self, db, proj, fn, syms, boundary, snapshot):
        """Recheck the inputs and commit the decision in the same transaction as the cut."""
        pid = proj["id"]
        matched, headers = snapshot
        current = db.execute("SELECT * FROM functions WHERE id=?", (fn["id"],)).fetchone()
        fresh = db.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        fields = ("name", "status", "target", "relocs", "vram", "rom_offset", "size", "meta")
        compile_inputs = ("compiler", "compiler_ranges", "gp", "big_endian", "rom", "segments")
        saved = db.execute("SELECT * FROM functions WHERE project_id=? AND status='matched'", (pid,)).fetchall()
        cfg = json.loads(fresh["config"]) if fresh else {}
        checked = next((row for row in cfg.get("boundaries", []) if boundary and row["id"] == boundary["id"]), None)
        if (not current or not fresh or any(current[f] != fn[f] for f in fields) or fresh["root"] != proj["root"]
                or any(self.cfg(fresh).get(key) != self.cfg(proj).get(key) for key in compile_inputs)
                or self._syms.get(pid) is not syms or {tuple(r) for r in matched} != {tuple(r) for r in saved}
                or any(dict(self._context(pid, unit) or {}).get("version") != header.get("version")
                       for unit, header in headers.items())
                or checked != boundary):
            raise NetworkError("The project changed while checking this boundary. Read the function again and retry.")
        if checked:
            if checked["tail"] != "function" and db.execute(
                    "SELECT 1 FROM functions AS f, json_each(f.relocs) AS ref WHERE f.project_id=? "
                    "AND (f.id!=? OR json_extract(ref.value,'$[0]')<?) "
                    "AND json_extract(ref.value,'$[2]')>=? AND json_extract(ref.value,'$[2]')<? LIMIT 1",
                    (pid, fn["id"], checked["end"] - fn["vram"], checked["end"], fn["vram"] + fn["size"])).fetchone():
                raise NetworkError("Recorded code still references bytes after that boundary. Check what follows the function.")
            checked.update(status="applied", decided_at=time.time())
            for other in cfg["boundaries"]:
                if other["status"] == "open" and other.get("function_id") == fn["id"]:
                    other.update(status="superseded", decided_at=time.time())
            db.execute("UPDATE projects SET config=? WHERE id=?", (json.dumps(cfg), pid))

    def request_boundary(self, agent: Agent, slug: str, function: str, end: str, reason: str, tail: str = "") -> str:
        """Require independent agreement about where the function ends, then check bytes and matches."""
        if link := self._link(slug):
            return link.call("/collab/boundary", agent, function=function, end=end, reason=reason, tail=tail)
        if (fork := self.fork_slug(slug)):
            return self.forks.report_boundary(agent, fork, function, end, reason)
        self._rom_only(slug, "report_boundary")
        fn = self._function(function, slug)
        proj = self._project(slug)
        console = self.console(proj)
        if arch.BOUNDARIES not in console.features:
            raise NetworkError(f"boundary reports aren't supported for {console.name} yet (ROADMAP.md)")
        if fn["status"] == "matched":
            raise NetworkError(f"{fn['name']} is matched: its boundaries are proven")
        m = _ADDRESS.match(str(end).strip())
        if not m:
            raise NetworkError("`end` must be an address like 0x80001234: where the function's last instruction ends")
        addr = int(m.group(1), 16)
        size = console.instruction_size(fn)
        off = addr - fn["vram"]
        if off <= 0 or off >= fn["size"] or off % size:
            word = "word" if size == 4 else "instruction"
            raise NetworkError(f"0x{addr:08X} isn't a {word} inside {fn['name']} "
                               f"(0x{fn['vram']:08X}, {fn['size']} bytes)")
        reason = " ".join(reason.split())[:600]
        if not reason:
            raise NetworkError("say what the bytes after it are (padding, linker leftovers, data) and how you can tell")
        code = fn["target"] or b""
        if len(code) != fn["size"]:
            raise NetworkError("Read the function again. The project has no complete byte range to check.")
        padding = bool(fn["asm"]) and console.is_padding(code[off:], fn)
        if console.ends_in_delay_branch(code[:off], fn):
            raise NetworkError("The instruction before this boundary still uses the next instruction. Move the boundary.")
        if tail not in ("", "padding", "data", "function"):
            raise NetworkError("Choose padding, data, or function for the bytes after the boundary.")
        tail = tail or ("padding" if padding else "data")
        if tail == "padding" and not padding:
            raise NetworkError("These bytes do not pass the padding check. Explain what follows the boundary.")
        if tail != "function" and any(addr <= target < fn["vram"] + fn["size"]
                                      for target in console.branch_targets(code[:off], fn)):
            raise NetworkError("This function still branches past that boundary. Check where the live code ends.")
        digest = hashlib.sha256(code).hexdigest()
        model = self.knowledge.model_of(agent)
        order = "big" if proj["big_endian"] else "little"
        rest = " ".join(f"{int.from_bytes(fn['target'][at:at + size], order):0{2 * size}X}"
                        for at in range(off, min(fn["size"], off + 32), size))

        def change(cfg):
            items = cfg.setdefault("boundaries", [])
            report = next((r for r in items if (r.get("function_id"), r["end"], r.get("tail"), r.get("digest"))
                           == (fn["id"], addr, tail, digest) and r["status"] == "open"), None)
            if report is None:
                report = {"id": max((r["id"] for r in items), default=0) + 1, "function": fn["name"],
                          "function_id": fn["id"], "vram": fn["vram"], "end": addr, "cut": fn["size"] - off,
                          "rest": rest, "reason": reason, "agent": agent.name, "tail": tail, "digest": digest,
                          "t": time.time(), "status": "open", "support": {}}
                items.append(report)
            report["support"][agent.name] = {"model": model, "reason": reason}
            return report

        with self.store.tx() as db:
            cfg = json.loads(db.execute("SELECT config FROM projects WHERE id=?", (proj["id"],)).fetchone()[0])
            report = change(cfg)
            db.execute("UPDATE projects SET config=? WHERE id=?", (json.dumps(cfg), proj["id"]))
        if len({vote["model"] for vote in report["support"].values()}) < 2:
            return f"Reported boundary #{report['id']}. A second model must check the {tail} after it. Meanwhile, release it or continue."
        if not fn["asm"]:
            raise NetworkError("This function is missing its original instructions. Run Check again before changing its boundary.")
        if tail != "function" and not console.ends_with_return(code[:off], fn):
            return (f"Reported boundary #{report['id']}. The app couldn't verify a return before the {tail}. "
                    "The current function stays open.")
        new_name = f"{'func' if tail == 'function' else 'pad'}_{addr:08X}"
        syms = self.symbols(proj["id"])
        if new_name in syms.by_name:
            raise NetworkError(f"{new_name} already exists. Check the functions at the proposed boundary.")
        explanation = f"{fn['name']} ends at 0x{addr:08X}. Kept the remaining bytes as {tail}. {reason}"
        tail_meta = None if tail == "function" else {"asm_code": f"{tail} after {fn['name']}: {reason}"}
        self._cut(proj, fn, addr, new_name, syms, agent.id, "boundary", explanation, tail_meta, boundary=report)
        return f"✅ {explanation} Existing matches still pass."

    def resolve_boundary(self, slug: str, rid: int, apply: bool) -> None:
        """The user's answer to a boundary report: cut the function there (the rest set aside as not code) or dismiss."""
        proj = self._project(slug)
        req = next((r for r in self.cfg(proj).get("boundaries", []) if r["id"] == rid and r["status"] == "open"), None)
        if req is None:
            raise NetworkError(f"no open boundary report {rid}")
        if apply:
            fn = self._function(req["function"], slug)
            if fn["vram"] != req["vram"] or not 0 < req["end"] - fn["vram"] < fn["size"] or fn["status"] == "matched":
                raise NetworkError(f"{fn['name']} changed since it was reported: dismiss this one")
            name = f"pad_{req['end']:08X}"
            self._cut(proj, fn, req["end"], name, self.symbols(proj["id"]), None, "boundary", f"{fn['name']} ends at 0x{req['end']:08X}",
                      {"asm_code": f"not code (set aside from {fn['name']}): {req['reason']}"})
        self._update_cfg(slug, lambda cfg: next(r for r in cfg["boundaries"] if r["id"] == rid).__setitem__(
            "status", "applied" if apply else "dismissed"))

    def rename_symbol(self, agent: Agent, project: str | None, old: str, new: str, message: str = "") -> str:
        if (fork := self.fork_slug(project)):  # the project's own config and files (ForkEngine.rename_symbol)
            return self.forks.rename_symbol(agent, fork, old, new, message)
        proj = self._project(project)
        if not self.is_rom(proj):
            return super().rename_symbol(agent, project, old, new, message)
        link = self._link(project)
        if link is not None:
            text = link.call("/collab/rename", agent, old=old, new=new, message=message)
            if text.startswith("✅"):
                link.sync()
            return text
        lookup = self.console(proj).library_lookup
        if lookup is not None and agent.name != "you" and (proposal := lookup.guard_rename(self, proj, agent, old, new)):
            return proposal
        return self.rename_symbols(agent, project, [(old, new)], message)

    def rename_symbols(self, agent: Agent, project: str | None, pairs: list[tuple[str, str]], message: str = "",
                       *, record=None) -> str:
        """Rename several symbols at once (old -> new): the shared headers, matched sources and best attempts
        change together, after one check that every matched function still matches. One rename is the agents'
        rename_symbol; many at once are the PS2 kernel stubs named at import (consoles/ps2/syscalls.py)."""
        proj = self._project(project)
        pairs = [(old.strip(), new.strip()) for old, new in pairs]
        if not pairs:
            return "nothing to rename"
        syms = self.symbols(proj["id"])
        trial = syms.copy()  # with every overlay twin's name (#117 review)
        addresses = {}
        for old, new in pairs:
            if not (IDENT.match(old) and IDENT.match(new)):
                raise NetworkError("old and new must be C identifiers")
            addr = syms.resolve(old)
            if addr is None:
                raise NetworkError(f"`{old}` is not a symbol of this ROM: use a name from the assembly (func_/D_...) "
                                   f"or one given earlier")
            taken = trial.resolve(new)
            if taken is not None and taken != addr:
                raise NetworkError(f"`{new}` already names 0x{taken:08X}")
            at = self.store.all("SELECT id, name, size FROM functions WHERE project_id=? AND vram=?", proj["id"], addr)
            renamed = [r for r in at if r["name"] == old] or (at if len(at) == 1 else [])
            if any(r["name"] == new for r in at) and not any(r["name"] == new for r in renamed):
                # the other overlay function at this address has it: the names would collide (#117 review)
                raise NetworkError(f"`{new}` already names another function at 0x{addr:08X} (an overlay's)")
            addresses[old] = addr
            if old in trial.by_name:
                trial.rename(old, new)
            else:
                fnrow = at[0] if at else None
                current, _ = syms.name_at(addr)
                base = syms.by_name.get(current) if current != f"D_{addr:08X}" else None
                sym = Symbol(new, addr, fnrow["size"] if fnrow else (base.size if base and base.address == addr else 0),
                             "func" if fnrow else (base.kind if base else "data"))
                if len(at) > 1:  # an overlay twin still called by its address's name: the other twin's name stays
                    trial.alias(sym)
                else:
                    trial.add(sym)
        new_of = dict(pairs)
        word = re.compile(r"\b(" + "|".join(re.escape(old) for old in new_of) + r")\b")
        swap = lambda text: word.sub(lambda m: new_of[m.group(1)], text)
        units = self._unit_contexts(proj["id"])
        current_ctx = {u: self._context(proj["id"], u) for u in ({""} | units)}
        changed = {u: swap(c["header"]) for u, c in current_ctx.items() if word.search(c["header"])}
        matched = self.store.all("SELECT * FROM functions WHERE project_id=? AND status='matched'", proj["id"])
        rewritten = {r["id"]: swap(r["matched_source"]) for r in matched}
        renamed_rows = [dict(r) | {"name": new_of.get(r["name"], r["name"])} for r in matched]
        header_for = lambda fn: changed.get(self._effective_unit(fn, units),
                                            current_ctx[self._effective_unit(fn, units)]["header"])
        broken = self.regression_test(proj, None, sources=rewritten, only=renamed_rows, header_for=header_for,
                                      scope=lambda: self._using_symbols(proj["id"], trial))
        if broken:
            listing = "\n".join(f"- {n}: {why}" for n, why in broken[:10])
            return f"❌ REJECTED: renaming breaks {len(broken)} matched function(s).\n{listing}"
        msg = message.strip() or ", ".join(f"rename {old} -> {new}" for old, new in pairs[:3]) + (" ..." if len(pairs) > 3 else "")
        with self.store.tx() as db:
            fields = ("id", "name", "matched_source", "target", "relocs", "unit", "vram", "rom_offset")
            current = db.execute(f"SELECT {','.join(fields)} FROM functions WHERE project_id=? AND status='matched'",
                                 (proj["id"],)).fetchall()
            fresh = db.execute("SELECT root,config FROM projects WHERE id=?", (proj["id"],)).fetchone()
            cfg = self.cfg(proj)
            updated = self.cfg(fresh) if fresh else {}
            compile_inputs = ("compiler", "compiler_ranges", "gp", "big_endian", "rom", "segments")
            if (fresh is None or fresh["root"] != proj["root"] or self._syms.get(proj["id"]) is not syms
                    or any(cfg.get(k) != updated.get(k) for k in compile_inputs)
                    or {tuple(r[k] for k in fields) for r in matched} != {tuple(r) for r in current}
                    or any(self._latest_version(db, proj["id"], unit) != context["version"]
                           for unit, context in current_ctx.items())):
                raise NetworkError("The project changed during the rename check. Review the current result and try again.")
            for old, new in pairs:
                addr = addresses[old]
                if db.execute("UPDATE symbols SET name=? WHERE project_id=? AND name=?", (new, proj["id"], old)).rowcount == 0:
                    s = trial.by_name[new]
                    db.execute("INSERT OR REPLACE INTO symbols(project_id,name,address,size,kind) VALUES(?,?,?,?,?)",
                               (proj["id"], new, s.address, s.size, s.kind))
                # By name: an address overlays share names one of their functions, never all of them (#107 review).
                # A name only the symbols had (a function whose row kept its address's name) goes by the address.
                if not db.execute("UPDATE functions SET name=? WHERE project_id=? AND name=?",
                                  (new, proj["id"], old)).rowcount:
                    db.execute("UPDATE functions SET name=? WHERE project_id=? AND vram=? AND (SELECT COUNT(*) FROM "
                               "functions WHERE project_id=? AND vram=?)=1", (new, proj["id"], addr, proj["id"], addr))
            for u, header in changed.items():
                version = self._next_version(db, proj["id"])
                db.execute("INSERT INTO context_versions(project_id,version,header,agent_id,message,created_at,unit) "
                           "VALUES(?,?,?,?,?,?,?)", (proj["id"], version, header, agent.id, msg, time.time(), u))
            for fid, src in rewritten.items():
                db.execute("UPDATE functions SET matched_source=?, best_source=? WHERE id=?", (src, src, fid))
            for r in db.execute("SELECT id, best_source FROM functions WHERE project_id=? AND status='open' "
                                "AND best_source IS NOT NULL", (proj["id"],)).fetchall():
                db.execute("UPDATE functions SET best_source=? WHERE id=?", (swap(r["best_source"]), r["id"]))
            if record is not None:
                record(db)
            self._event(db, proj["id"], agent.id, "rename", msg if len(pairs) > 1 else f"{pairs[0][0]} → {pairs[0][1]}")
        self._syms.pop(proj["id"], None)
        if len(pairs) == 1:
            (old, new), = pairs
            return (f"✅ 0x{addresses[old]:08X} is now `{new}` (was `{old}`); {len(rewritten)} matched sources updated, "
                    f"all still match.")
        return f"✅ renamed {len(pairs)} symbols; {len(rewritten)} matched sources updated, all still match."

    def edit_context(self, agent: Agent, project: str | None, old: str, new: str, message: str,
                     function: str | None = None) -> str:
        self._rom_only(project, "edit_context")
        link = self._link(project)
        if link is None:
            return super().edit_context(agent, project, old, new, message, function)
        text = link.call("/collab/context", agent, old=old, new=new, message=message, function=function)
        if text.startswith("✅"):
            link.sync()
        return text

    def add_note(self, agent: Agent, project: str | None, text: str, function: str | None = None) -> str:
        link = self._link(project)
        if link is None:
            return super().add_note(agent, project, text, function)
        name = self._function(function, project)["name"] if function else None
        return link.call("/collab/note", agent, text=text, function=name) or "Noted, and shared with the room."

    def set_instructions(self, slug: str, text: str) -> None:
        """What the user wants agents to know or focus on, given to every agent on this project."""
        self._update_cfg(slug, lambda cfg: cfg.__setitem__("instructions", text.strip()[:4000]))

    def instructions(self, slug: str) -> str:
        return self.cfg(self._project(slug)).get("instructions") or ""

    def set_claim_order(self, slug: str, order: str) -> str:
        """Which functions helpers claim without a name take first, whatever strategy they ask for; "" is the
        agent's own choice (easiest, or finish in project mode)."""
        order = (order or "").strip()
        if order not in ("", *CLAIM_ORDERS):
            raise NetworkError(f"claim order must be left empty (the default) or be one of {', '.join(CLAIM_ORDERS)}")
        self._update_cfg(slug, lambda cfg: cfg.__setitem__("claim_order", order) if order
                         else cfg.pop("claim_order", None))
        return order

    def claim_order(self, slug: str) -> str:
        order = self.cfg(self._project(slug)).get("claim_order") or ""
        return order if order in CLAIM_ORDERS else ""

    def set_title(self, slug: str, title: str) -> str:
        """The name the project shows by, which the user can change: a disc's title is often only its serial
        (GTA San Andreas imports as SLES-52541). The slug, folders and history keep the name they were made with."""
        title = " ".join(title.split())
        if not title:
            raise NetworkError("give the project a name")
        if len(title) > 80:
            raise NetworkError("keep the name under 80 characters")
        proj = self._project(slug)
        with self.store.tx() as db:
            db.execute("UPDATE projects SET name=? WHERE id=?", (title, proj["id"]))
        return title

    def set_priority(self, slug: str, name: str, on: bool = True) -> list[str]:
        """Functions the user wants done next: automatic claims take these first."""
        fn = self._function(name, slug)

        def change(cfg):
            items = [n for n in cfg.get("priority", []) if n != fn["name"]]
            cfg["priority"] = ([fn["name"]] + items) if on else items
        return self._update_cfg(slug, change)["priority"]
