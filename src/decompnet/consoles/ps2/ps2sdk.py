"""Assemble independently published PS2SDK kernel routines, preserving source provenance."""
import hashlib
import json
import re
import tempfile
import time
from collections import defaultdict
from pathlib import Path
from urllib.request import urlopen

from ... import toolchains
from ...compiler import run_in
from ...elfobj import extract_function
from ...network import NetworkError
from ...project import compiler_config

REVISION = "cf9557d3c931544c6940c0f5e1ad24fe7d2ac596"
SOURCE = "ee/kernel/src/kernel.S"
HEADER = "ee/kernel/include/syscallnr.h"
SETUP = "ee/kernel/src/setup_syscalls.S"
BASE_URL = f"https://github.com/ps2dev/ps2sdk/blob/{REVISION}/"
DIGESTS = {
    SETUP: "4b9cd6b4bf62ff2f451c9f5b1074acfabb22cacd73e06a5c9fc980715543755d",
    SOURCE: "469af1cadc5e1a5adab92716ebed19dd4f390f68fbf072d8fcb8df49726f6630",
    HEADER: "573f0c3276eaae064f0da04990126133b434259b3b235e53ed8f013694300edc",
}


def _source(relative: str) -> bytes:
    installed = toolchains.data_dir() / "sources" / "ps2sdk" / REVISION / relative
    if installed.is_file():
        data = installed.read_bytes()
    else:
        url = f"https://raw.githubusercontent.com/ps2dev/ps2sdk/{REVISION}/{relative}"
        with urlopen(url, timeout=20) as response:
            data = response.read(512 * 1024)
    if hashlib.sha256(data).hexdigest() != DIGESTS[relative]:
        raise ValueError(f"PS2SDK source checksum differs for {relative}")
    return data


def _assemble(directory: Path, toolchain: str, executable: str, args: list[str]) -> bytes:
    cfg = compiler_config(directory, {"compiler": {"toolchain": toolchain, "cmd": ["{tc}/" + executable]}})
    result = run_in(cfg, directory, [*cfg.cmd, *args], 60, outputs=["kernel.o"])
    if result.returncode:
        raise ValueError(f"PS2SDK assembly failed: {result.stderr[-1000:]}")
    return (directory / "kernel.o").read_bytes()


def _functions(obj: bytes, names: list[str]) -> dict[bytes, list[str]]:
    wrappers = defaultdict(list)
    for name in names:
        fn = extract_function(obj, name)
        if fn and not fn.big_endian and fn.code and not fn.relocs:
            wrappers[fn.code].append(name)
    if not wrappers:
        raise ValueError("The assembled object contains no complete kernel functions")
    return dict(wrappers)


def kernel_routines() -> dict[bytes, dict]:
    receipts = {}
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        source = _source(SOURCE)
        (directory / "kernel.S").write_bytes(source)
        (directory / "syscallnr.h").write_bytes(_source(HEADER))
        names = sorted(set(re.findall(r"^SYSCALL(?:_SPECIAL)?\((\w+)[,)]", source.decode(), re.M)))
        names += ["_InvalidDCache", "_SyncDCache"]
        (directory / "defines.h").write_text("".join(f"#define F_{name} 1\n" for name in names))
        flags = ["-c", "-x", "assembler-with-cpp", "-I.", "-G0", "-include", "defines.h"]
        obj = _assemble(directory, "ee-gcc-2.96", "bin/ee-gcc", [*flags, "kernel.S", "-o", "kernel.o"])
        for code, symbols in _functions(obj, names).items():
            receipts[code] = {"source": BASE_URL + SOURCE, "symbols": symbols, "toolchain": "ee-gcc-2.96",
                              "flags": ["-G0", *["-DF_" + symbol for symbol in symbols]], "inputs_sha256":
                              {path: DIGESTS[path] for path in (SOURCE, HEADER)}}
        setup = _source(SETUP)
        (directory / "setup_syscalls.S").write_bytes(setup)
        (directory / "mode.s").write_bytes(b".set noreorder\n")
        flags = ["-EL", "-march=r5900", "-mabi=eabi", "-G0"]
        obj = _assemble(directory, "binutils-mips", "mips-ps2-decompals-as",
                        [*flags, "mode.s", "setup_syscalls.S", "-o", "kernel.o"])
        names = re.findall(r"^\.ent\s+(\w+)", setup.decode(), re.M)
        for code, symbols in _functions(obj, names).items():
            receipts.setdefault(code, {"source": BASE_URL + SETUP, "symbols": symbols, "toolchain": "binutils-mips",
                                       "flags": flags, "mode": ".set noreorder", "inputs_sha256": {SETUP: DIGESTS[SETUP]}})
    return receipts


def verify(net, proj, log=print, *, saved=None):
    """Record independently assembled, relocation-free whole functions from the owner's ROM."""
    slug = proj["slug"]
    if not net.is_rom(proj) or slug in net.rooms or slug in net.links:
        raise NetworkError("Assembly verification needs a standalone ROM project.")
    log("Assembling the pinned PS2SDK kernel sources")
    routines = kernel_routines()
    recorded = held = 0
    with net.store.tx() as db:
        net._expire_claims(db)
        rows = db.execute(
            "SELECT f.*, c.agent_id FROM functions f LEFT JOIN claims c ON c.function_id=f.id "
            "WHERE f.project_id=? AND f.status IN ('open','asm') AND f.size=length(f.target) "
            "AND f.asm_verification IS NULL", (proj["id"],))
        for fn in rows.fetchall():
            key = (fn["name"], fn["vram"])
            if saved is not None and key not in saved:
                continue
            code = fn["target"]
            receipt = routines.get(code)
            if not receipt or json.loads(fn["relocs"] or "[]"):
                continue
            if fn["agent_id"] is not None:
                held += 1
                continue
            if net.reader(proj, fn)(fn["vram"], len(code)) != code:
                continue
            receipt = receipt | {"code_sha256": hashlib.sha256(code).hexdigest()}
            if saved is not None:
                original = saved[key]
                comparison = receipt
                if original.get("flags") == ["-G0", "upstream F_ definitions"] and receipt["source"] == BASE_URL + SOURCE:
                    comparison = receipt | {"flags": original["flags"]}
                if original != comparison:
                    continue
                receipt = original
            db.execute("UPDATE functions SET status='asm', asm_verification=? WHERE id=?",
                       (json.dumps(receipt), fn["id"]))
            if saved is None:
                db.execute("INSERT INTO notes(project_id,function_id,text,created_at) VALUES(?,?,?,?)",
                           (proj["id"], fn["id"], f"Verified assembly: {', '.join(receipt['symbols'])}. "
                            f"All {len(code)} bytes match {receipt['source']}", time.time()))
            recorded += 1
        if recorded:
            net._event(db, proj["id"], None, "analysis", f"Verified {recorded} PS2SDK assembly functions")
    return {"verified": recorded, "claimed": held}
