"""Run the original i386 SDK compiler under an ARM64 macOS process with a private filesystem per job."""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from . import limits
from .linuxvm_worker import MAX_FILE, MAX_TOTAL, unpack_part, work_path

PROFILE = """(version 1)(deny default)(import "system.sb")
(allow sysctl-read)(allow file-read-metadata)
(allow process-exec file-map-executable file-read* (literal (param "BIN")))
(allow file-read* (subpath (param "ROOT")) (literal "/dev/null") (literal "/dev/urandom"))
(allow file-write* (subpath (param "WORK")) (subpath (param "TMP")))
(allow signal (target self))"""


def runtime() -> Path | None:
    path = os.environ.get("DECOMP_ISH_RUNTIME")
    return Path(path).resolve() if path else None


def ready(root: Path) -> bool:
    return (os.access(root / "ish", os.X_OK) and (root / "lib/ld-linux.so.2").is_file()
            and (root / "lib/i386-linux-gnu/libc.so.6").is_file())


def prepare(root: Path, key: str, directories: tuple[Path, ...]) -> None:
    cache = root / "profiles"
    cache.mkdir(exist_ok=True)
    final = cache / key
    if final.is_dir():
        return
    with tempfile.TemporaryDirectory(dir=cache) as tmp:
        tree = Path(tmp) / "root"
        shutil.copytree(root / "lib", tree / "lib")
        for name in ("work", "tmp", "dev", "proc"):
            (tree / name).mkdir()
        for i, folder in enumerate(directories):
            if folder.name == "runtime-i386":  # iSH uses its compatible i386 glibc in /lib
                continue
            dest = tree / "tools" / key / str(i)
            manifest = json.loads((folder / ".decompnet-toolchain.json").read_bytes())
            parts = manifest.get("archive_parts")
            if not parts:
                raise ValueError(f"Reinstall {folder.name} to stage its original compiler archive.")
            dest.mkdir(parents=True)
            shutil.copytree(folder / ".archives", dest / ".archives")
            for part in parts:
                unpack_part(dest, part)
            shutil.rmtree(dest / ".archives")
        try:
            tree.rename(final)
        except OSError:
            if not final.is_dir():
                raise


def run(root: Path, argv: list[str], files: dict, outputs, timeout, check_cancel) -> dict:
    if argv[:2] == ["env", "QEMU_RESERVED_VA=1073741824"]:
        argv = argv[2:]  # only QEMU needs a reserved address space
    match = re.fullmatch(r"/tools/([0-9a-f]{32})/\d+/[\w./+-]+", argv[0]) if argv else None
    if not match or ".." in Path(argv[0]).parts:
        raise ValueError("The iSH compiler must come from a prepared toolchain.")
    if sum(len(os.fsencode(a)) + 1 for a in argv) >= 4096:
        raise ValueError("The compiler command exceeds iSH's argument limit.")
    with tempfile.TemporaryDirectory(prefix="decomp-ish-") as tmp:
        tree = Path(tmp).resolve() / "root"
        subprocess.run(["/bin/cp", "-cR", str(root / "profiles" / match[1]), str(tree)], check=True)
        work = tree / "work"
        for name, data in files.items():
            dest = work_path(work, name)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(base64.b64decode(data, validate=True))
        for name in outputs or []:
            work_path(work, name).parent.mkdir(parents=True, exist_ok=True)
        binary = root / "ish"
        command = ["/usr/bin/sandbox-exec", "-D", f"ROOT={tree}", "-D", f"WORK={work}",
                   "-D", f"TMP={tree / 'tmp'}", "-D", f"BIN={binary}", "-p", PROFILE,
                   str(binary), "-r", str(tree), "-d", "/work", *argv]
        deadline = time.monotonic() + min(timeout, limits.compiler_time_limit())
        with subprocess.Popen(command, env={"PATH": "/usr/bin:/bin"}, cwd=work, start_new_session=True,
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as proc:
            try:
                with limits.CompilerMemoryWatchdog(proc) as watchdog:
                    while True:
                        if check_cancel:
                            check_cancel()
                        try:
                            stdout, stderr = proc.communicate(timeout=0.2)
                            break
                        except subprocess.TimeoutExpired:
                            if time.monotonic() >= deadline:
                                raise subprocess.TimeoutExpired(argv, timeout) from None
                    if watchdog.exceeded:
                        stderr += watchdog.message().encode()
            finally:
                if proc.poll() is None:
                    limits.stop_group(proc)
        results = {}
        total = 0
        names = outputs if outputs is not None else (str(p.relative_to(work)) for p in work.rglob("*"))
        for name in names:
            path = work_path(work, name)
            if path.is_file() and not path.is_symlink():
                size = path.stat().st_size
                total += size
                if size > MAX_FILE or total > MAX_TOTAL:
                    raise ValueError("The compiler output exceeds the transfer limit.")
                results[name] = base64.b64encode(path.read_bytes()).decode()
        return {"returncode": proc.returncode, "stdout": stdout[-16000:].decode(errors="replace"),
                "stderr": stderr[-16000:].decode(errors="replace"), "files": results}
