"""Homemade EE objects and archives exercise the lookup without any SDK files."""

import json
import struct

import pytest

from decompnet import kits
from decompnet.app import collab, saves, tools
from decompnet.app.engine import RomNetwork
from decompnet.consoles.ps2 import library_lookup, sdk_libraries
from decompnet.network import NetworkError


def object_file(name, code, relocation=4):
    strings = b"\0" + name.encode() + b"\0callee\0"
    symbols = (bytes(16) + struct.pack("<IIIBBH", 1, 0, len(code), 0x12, 0, 1)
               + struct.pack("<IIIBBH", len(name) + 2, 0, 0, 0x10, 0, 0))
    names = b"\0.text\0.strtab\0.symtab\0.rel.text\0.shstrtab\0"
    refs = struct.pack("<II", 4, 2 << 8 | relocation) if relocation is not None else b""
    sections = [(b".text", 1, 6, code, 0, 0, 0), (b".strtab", 3, 0, strings, 0, 0, 0),
                (b".symtab", 2, 0, symbols, 2, 1, 16),
                (b".rel.text", 9, 0, refs, 3, 1, 8),
                (b".shstrtab", 3, 0, names, 0, 0, 0)]
    data, headers = bytearray(52), bytearray(40)
    for label, kind, flags, payload, link, info, stride in sections:
        headers += struct.pack("<10I", names.index(label + b"\0"), kind, flags, 0, len(data), len(payload),
                               link, info, 4, stride)
        data += payload
    data[:52] = (b"\x7fELF\x01\x01\x01" + bytes(9)
                 + struct.pack("<HHIIIIIHHHHHH", 1, 8, 1, 0, 0, len(data), 0x20924001, 52, 0, 0, 40, 6, 5))
    return bytes(data + headers)


def member(name, payload):
    return (f"{name:<16}{0:<12}{0:<6}{0:<6}{'100644':<8}{len(payload):<10}`\n".encode() + payload
            + b"\n" * (len(payload) % 2))


CODE = struct.pack("<8I", 0x27BDFFF0, 0x0C000000, 0x24040001, 0x8FA30000, 0x8FA20004, 0x00621021,
                   0x03E00008, 0x27BD0010)
RELOCATED = CODE[:4] + (0x0C040000).to_bytes(4, "little") + CODE[8:]


def test_index_matches_only_within_the_symbol_and_resolves_known_calls(tmp_path):
    path = tmp_path / "libsample.a"
    path.write_bytes(sdk_libraries.AR_MAGIC + member("sample.o/", object_file("sdkRoutine", CODE)))
    lib = sdk_libraries.index(tmp_path)
    assert (lib.archive_count, lib.function_count, lib.skipped) == (1, 1, 0)
    assert not lib.candidates(RELOCATED[:28])
    assert not lib.candidates(RELOCATED[:8] + b"\2" + RELOCATED[9:])
    assert not lib.candidates(RELOCATED[:4] + b"\0\0\4\x08" + RELOCATED[8:])  # a jump, not a call
    findings = sdk_libraries.compare([lib], RELOCATED, [(4, 4, 0x100000)], {"callee": 0x100000}.get, 0)
    hit, = findings
    assert hit["name"] == "sdkRoutine" and hit["member"] == "sample.o" and hit["archive"] == path.name
    assert hit["extent"] == len(CODE) and hit["resolved_relocations"] == 1 and hit["bulk_evidence"]
    assert all(not isinstance(value, bytes) for value in hit.values())
    other, = sdk_libraries.compare([lib], RELOCATED, [(4, 4, 0x100000)], {"callee": 0x100004}.get, 0)
    assert other["layout_agrees"] and not other["bulk_evidence"]
    missing, = sdk_libraries.compare([lib], RELOCATED, [], {"callee": 0x100000}.get, 0)
    assert not missing["layout_agrees"] and not missing["bulk_evidence"]
    other_sdk = tmp_path / "other-sdk"
    other_sdk.mkdir()
    (other_sdk / path.name).write_bytes(path.read_bytes())
    (other_sdk / "another.a").write_bytes(sdk_libraries.AR_MAGIC + member("data.o/", b"unrelated"))
    copy = sdk_libraries.index(other_sdk)
    assert copy.fingerprint != lib.fingerprint
    common, = sdk_libraries.compare([lib, copy], RELOCATED, [(4, 4, 0x100000)], {"callee": 0x100000}.get, 0)
    assert common["name"] == hit["name"] and common["bulk_evidence"]


def test_an_ambiguous_body_keeps_both_archive_members_for_review(tmp_path):
    data = sdk_libraries.AR_MAGIC + member("//", b"long GNU member name.o/\n")
    data += member("/0", object_file("sdkRoutine", CODE))
    name = b"long BSD member name.o\0"
    data += member(f"#1/{len(name)}", name + object_file("anotherRoutine", CODE))
    (tmp_path / "libsample.a").write_bytes(data)
    lib = sdk_libraries.index(tmp_path)
    hits = sdk_libraries.compare([lib], RELOCATED, [(4, 4, 0x100000)], {"callee": 0x100000}.get, 0)
    assert {h["member"] for h in hits} == {"long GNU member name.o", "long BSD member name.o"}
    assert all(h["resolved_relocations"] == 1 and not h["bulk_evidence"] for h in hits)


def test_unsupported_or_absent_relocations_and_short_stubs_require_individual_review(tmp_path):
    path = tmp_path / "libsample.a"
    path.write_bytes(sdk_libraries.AR_MAGIC + member("short.o/", object_file("stub", CODE[:12])))
    lib = sdk_libraries.index(tmp_path)
    hit, = sdk_libraries.compare([lib], RELOCATED[:12], [(4, 4, 0x100000)], {"callee": 0x100000}.get, 0)
    assert hit["resolved_relocations"] == 1 and not hit["bulk_evidence"]
    path.write_bytes(sdk_libraries.AR_MAGIC + member("sample.o/", object_file("unsupported", CODE, relocation=21)))
    lib = sdk_libraries.index(tmp_path)
    assert not lib.candidates(RELOCATED)
    hit, = sdk_libraries.compare([lib], CODE, [], {}.get, 0)
    assert not hit["layout_agrees"] and not hit["bulk_evidence"]
    path.write_bytes(sdk_libraries.AR_MAGIC + member("no_refs.o/", object_file("no_refs", CODE, relocation=None)))
    hit, = sdk_libraries.compare([sdk_libraries.index(tmp_path)], CODE, [], {}.get, 0)
    assert hit["layout_agrees"] and hit["relocations"] == 0 and not hit["bulk_evidence"]


def test_setup_accepts_the_extracted_ee_folder_and_refuses_thin_archives(tmp_path):
    kit = kits.get("ps2-libraries")
    folder = tmp_path / "MAINDIR" / "ee" / "lib"
    folder.mkdir(parents=True)
    path = folder / "sample.a"
    path.write_bytes(b"!<thin>\n")
    assert kit.locate(tmp_path) is None
    with pytest.raises(ValueError, match="embedded objects"):
        list(sdk_libraries.members(path.read_bytes()))
    path.write_bytes(sdk_libraries.AR_MAGIC + member("sample.o/", object_file("sdkRoutine", CODE)))
    assert kit.locate(tmp_path) == folder.resolve()
    assert kit.locate(folder) == folder.resolve()


def test_lookup_keeps_overlay_twins_distinct_and_owner_decisions_survive_save(tmp_path, monkeypatch):
    from starlette.testclient import TestClient

    libs = tmp_path / "sdk"
    libs.mkdir()
    other_code = CODE[:8] + b"\2" + CODE[9:]
    (libs / "libsample.a").write_bytes(sdk_libraries.AR_MAGIC + member("sample.o/", object_file("sdkRoutine", CODE))
                                     + member("second.o/", object_file("otherRoutine", other_code)))
    monkeypatch.setattr(library_lookup, "_copies", lambda: {"local": libs})
    net = RomNetwork(tmp_path / "app.db", tmp_path)
    cfg = {"kind": "rom", "platform": "ps2", "compiler": {"toolchain": "ee-gcc-2.96", "flags": []},
           "rom": {"file": "game.rom"}, library_lookup.SETTINGS_KEY: ["local"]}
    with net.store.tx() as db:
        pid = db.execute("INSERT INTO projects(slug,name,root,config) VALUES('example','Example',?,?)",
                         (str(tmp_path), json.dumps(cfg))).lastrowid
        db.execute("INSERT INTO context_versions(project_id,version,header) VALUES(?,1,'')", (pid,))
        for name, offset, code in (("primary", 0, CODE), ("overlay", 64, other_code)):
            db.execute("INSERT INTO functions(project_id,name,vram,rom_offset,size,n_insns,target,relocs) "
                       "VALUES(?,?,0x120000,?,32,8,?,'[[4,4,1048576]]')", (pid, name, offset, code))
            db.execute("INSERT INTO symbols(project_id,name,address,size,kind) VALUES(?,?,0x120000,32,'func')", (pid, name))
    project = net.project_row("example")
    helper, _ = net.local_agent("worker")
    owner, _ = net.local_agent("you")
    report = library_lookup.scan(net, project, log=lambda _: None)
    first, second = report["hits"]
    hit, = first["evidence"]
    alternative, = second["evidence"]
    assert hit["name"] == "sdkRoutine" and alternative["name"] == "otherRoutine"
    assert not hit["bulk"] and not alternative["bulk"]
    assert "otherRoutine" not in library_lookup.render(report, first["function_id"])
    assert "sdkRoutine" not in library_lookup.render(report, second["function_id"])
    room = net.rooms["example"] = collab.Room(net, "example")
    token, _ = room.join("guest", "")
    with TestClient(collab.host_app(net, net.rooms), headers={"Authorization": f"Bearer {token}"}) as client:
        result = client.post("/collab/sdk-libraries", json={"function": "overlay"})
        assert result.status_code == 200
        assert "otherRoutine" in result.json()["text"] and "sdkRoutine" not in result.json()["text"]
        proposed = client.post("/collab/sdk-libraries", json={"candidate": alternative["id"]})
        assert "Proposed otherRoutine" in proposed.json()["text"]
        rename = client.post("/collab/rename", json={"old": "primary", "new": "sdkRoutine"})
        assert "Proposed sdkRoutine" in rename.json()["text"]
        assert net._function("primary", "example")["name"] == "primary"
    with pytest.raises(NetworkError, match="one at a time"):
        library_lookup.decide(net, project, owner, [hit["id"], alternative["id"]], True, log=lambda _: None)
    proposed = tools.call(net, helper, "example", "rename_symbol", {"old": "primary", "new": "sdkRoutine"})
    assert "Proposed sdkRoutine" in proposed
    assert net._function("primary", "example")["name"] == "primary"
    library_lookup.decide(net, project, owner, [hit["id"]], True, log=lambda _: None)
    assert net._function("sdkRoutine", "example")["rom_offset"] == 0
    library_lookup.decide(net, project, owner, [alternative["id"]], False, log=lambda _: None)
    project = net.project_row("example")
    decisions = library_lookup.reviews(net, project)
    assert decisions[hit["id"]]["decision"] == "approved"
    assert decisions[alternative["id"]]["decision"] == "dismissed"
    assert "otherRoutine" not in library_lookup.render(report, second["function_id"], decisions)
    rows = saves._function_rows(net, pid, sources=False)
    manifest, _, _ = saves._manifest_from_rows(net, project, net.cfg(project), rows, tmp_path)
    assert manifest["sdk_library_reviews"] == decisions
    assert library_lookup.SETTINGS_KEY not in manifest
    net._update_cfg("example", lambda config: config.pop(library_lookup.REVIEWS_KEY))
    library_lookup.restore_reviews(net, project, manifest["sdk_library_reviews"])
    assert library_lookup.reviews(net, net.project_row("example")) == decisions
    (libs / "unrelated.a").write_bytes(sdk_libraries.AR_MAGIC + member("unrelated.o/", object_file("unrelated", bytes(32))))
    latest = library_lookup.scan(net, net.project_row("example"), log=lambda _: None)
    evidence, = latest["hits"][1]["evidence"]
    assert evidence["sdk"] != alternative["sdk"]
    assert evidence["id"] == alternative["id"]
    assert "otherRoutine" not in library_lookup.render(latest, second["function_id"], decisions)
    archive = libs / "libsample.a"
    archive.write_bytes(archive.read_bytes() + member("marker/", b"changed"))
    latest = library_lookup.scan(net, net.project_row("example"), log=lambda _: None)
    assert "otherRoutine" in library_lookup.render(latest, second["function_id"], decisions)


def test_bulk_review_applies_two_clear_names_together(tmp_path, monkeypatch):
    libs = tmp_path / "sdk"
    libs.mkdir()
    other = CODE[:8] + b"\2" + CODE[9:]
    (libs / "libsample.a").write_bytes(sdk_libraries.AR_MAGIC + member("first.o/", object_file("first", CODE))
                                     + member("second.o/", object_file("second", other)))
    monkeypatch.setattr(library_lookup, "_copies", lambda: {"local": libs})
    net = RomNetwork(tmp_path / "app.db", tmp_path)
    cfg = {"kind": "rom", "platform": "ps2", "compiler": {"toolchain": "ee-gcc-2.96", "flags": []},
           "rom": {"file": "game.rom"}, library_lookup.SETTINGS_KEY: ["local"]}
    with net.store.tx() as db:
        pid = db.execute("INSERT INTO projects(slug,name,root,config) VALUES('example','Example',?,?)",
                         (str(tmp_path), json.dumps(cfg))).lastrowid
        db.execute("INSERT INTO context_versions(project_id,version,header) VALUES(?,1,'')", (pid,))
        db.execute("INSERT INTO symbols(project_id,name,address,size,kind) VALUES(?,'callee',1048576,4,'func')", (pid,))
        for addr, code in ((0x120000, RELOCATED), (0x120020, RELOCATED[:8] + b"\2" + RELOCATED[9:])):
            db.execute("INSERT INTO functions(project_id,name,vram,rom_offset,size,n_insns,target,relocs) "
                       "VALUES(?,?,?,?,32,8,?,'[[4,4,1048576]]')", (pid, f"func_{addr:08X}", addr, addr - 0x120000, code))
    project = net.project_row("example")
    candidates = [item for hit in library_lookup.scan(net, project, log=lambda _: None)["hits"] for item in hit["evidence"]]
    assert len(candidates) == 2 and all(item["bulk"] for item in candidates)
    owner, _ = net.local_agent("you")
    library_lookup.decide(net, project, owner, [item["id"] for item in candidates], True, log=lambda _: None)
    assert [(row["name"], row["vram"]) for row in net.store.all("SELECT name,vram FROM functions ORDER BY vram")] == [
        ("first", 0x120000), ("second", 0x120020)]
