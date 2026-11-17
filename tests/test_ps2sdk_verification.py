import hashlib
import json

from decompnet.app import saves
from decompnet.app.engine import RomNetwork
from decompnet.consoles.ps2 import ps2sdk


def test_assembly_receipts_are_separate_from_c_and_reverified_on_restore(tmp_path, monkeypatch):
    net = RomNetwork(tmp_path / "app.db", tmp_path)
    code = bytes.fromhex("010003240c0000000800e00300000000")
    source = {"source": ps2sdk.BASE_URL + ps2sdk.SOURCE, "symbols": ["ResetEE"], "flags": ["-G0", "-DF_ResetEE"]}
    monkeypatch.setattr(ps2sdk, "kernel_routines", lambda: {code: source})
    monkeypatch.setattr(net, "reader", lambda proj, fn: lambda addr, size: code if addr != 48 else b"wrong")
    cfg = {"kind": "rom", "platform": "ps2", "rom": {}, "compiler": {"toolchain": "ee-gcc-2.96", "flags": []}}
    with net.store.tx() as db:
        pid = db.execute("INSERT INTO projects(slug,name,root,config) VALUES('game','Demo',?,?)",
                         (str(tmp_path), json.dumps(cfg))).lastrowid
        db.execute("INSERT INTO context_versions(project_id,version,header) VALUES(?,1,'')", (pid,))
        for index, addr in enumerate((16, 32, 48)):
            db.execute("INSERT INTO functions(project_id,name,size,target,vram,relocs) VALUES(?,?,16,?,?,'[]')",
                       (pid, f"f{index}", code, addr))
    proj = net._project("game", sync=False)
    agent, _ = net.local_agent("worker")
    net.claim_row(agent, "game", "f1")
    assert ps2sdk.verify(net, proj) == {"verified": 1, "claimed": 1}
    assert ps2sdk.verify(net, proj)["verified"] == 0
    rows = saves._function_rows(net, pid, sources=False)
    manifest, _, _ = saves._manifest_from_rows(net, proj, cfg, rows, tmp_path)
    saved = manifest["set_aside"][0]["asm_verification"]
    assert saved == source | {"code_sha256": hashlib.sha256(code).hexdigest()}
    assert net._overview("game", proj)["matched"] == 0
    with net.store.tx() as db:
        db.execute("UPDATE functions SET status='open' WHERE name='f0'")
    assert net._overview("game", proj)["verified_assembly"] == 0
    assert ps2sdk.verify(net, proj, saved={("f0", 16): saved | {"source": "other"}})["verified"] == 0
    saves._apply_context(net, "game", tmp_path, manifest, lambda *_: None)
    assert json.loads(net._function("f0", "game")["asm_verification"]) == saved
    assert net._overview("game", proj)["verified_assembly_bytes"] == len(code)
