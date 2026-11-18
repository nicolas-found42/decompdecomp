"""Install optional PS2 analysis tools next to the app's pinned toolchains."""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
import threading
import zipfile
from pathlib import Path
from urllib.request import urlopen

from ... import toolchains
from ...forks import ghidra

EXT = 'ghidra-emotionengine-reloaded'
GHIDRA_URL = ('https://github.com/chaoticgd/ghidra-emotionengine-reloaded/releases/download/v2.1.39/'
              'ghidra_12.1.4_PUBLIC_20261029_ghidra-emotionengine-reloaded.zip')
RAW = 'https://raw.githubusercontent.com/'
KIT = RAW + 'LinuxJessi/decomp-compat-kit/ea19d81e470676732f799ef7d9bf7b9470f7c9c9/'
RECOMP = RAW + 'ran-j/PS2Recomp/e5636981a1c9de368f6b71e819c3dc188dfedbf4/'
TOOLS = {
    'r5900': ('Ghidra Emotion Engine', 'Decompile a PS2 function with R5900, MMI and VU0 macro instruction support.'),
    'sce-scan': ('PS2Recomp SDK symbol scanner', 'Find possible SDK names by comparing executable code with library signatures.'),
    'transfer': ('PS2 symbol transfer', 'Find corresponding functions in a second PS2 ELF.'),
}
SCANNER_FILES = {
    'ps2xAnalyzer/src/sce_symbol_scanner.cpp': '01396c14abfb7ba3311a2f97c385b230916ee6cfb01e682c8a39e68b90058e59',
    'ps2xAnalyzer/include/ps2recomp/sce_symbol_scanner.h': '1e4141966227b200d336a48488d911ea686d89813f6e72a3eb122dd4cebb2364',
    'ps2xAnalyzer/include/ps2recomp/sce_symbol_database_data.h': '0fdd1168a815c8af2529cae9076ae813a1304c5c3a41e48d88d38e21e7b5833d',
    'ps2xRecomp/include/ps2recomp/types.h': '2f42f723235ed09b6040fcb248e501903f53d679b174f782e5b94475932dcaad',
    'LICENSE': '831e92808eb553250066261a1387a9323cbf300d4559a41f2c71fd3712bdb44a',
}
ROOT = Path(__file__).parent
_install_lock = threading.Lock()


def location(key):
    if key == 'r5900':
        return toolchains.path('ghidra') / 'Ghidra/Extensions' / EXT
    return toolchains.root() / {'sce-scan': 'sce-scanner-e563698', 'transfer': 'compat-kit-ea19d81'}[key]


def ready(key):
    dest = location(key)
    if key == 'r5900':
        return ghidra.available() and all((dest / p).is_file() for p in
            ('data/languages/emotionengine.ldefs', 'lib/ghidra-emotionengine-reloaded.jar'))
    return (dest / 'transfer_symbols.py').is_file() if key == 'transfer' else os.access(dest / 'sce-scan', os.X_OK)


def catalog():
    return [dict(id=key, name=name, use=description, platform='PS2', supported=True,
                 analysis=True, installed=ready(key)) for key, (name, description) in TOOLS.items()]


def fetch(url, sha, path):
    with urlopen(url, timeout=60) as response:
        data = response.read(32 * 1024 * 1024 + 1)
    if hashlib.sha256(data).hexdigest() != sha:
        raise ValueError('The download checksum differs for ' + path.name)
    path.parent.mkdir(exist_ok=True, parents=True)
    path.write_bytes(data)


def install(key, log):
    if key not in TOOLS:
        raise ValueError('Unknown PS2 tool.')
    with _install_lock:
        if ready(key):
            return
        if key == 'r5900' and not ghidra.available():
            raise ValueError('Install the pinned Ghidra and a Java 21+ JDK in Compilers & tools first.')
        dest = location(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            raise ValueError(f'An incomplete installation exists at {dest}. Move it aside before reinstalling.')
        log('Downloading and verifying ' + TOOLS[key][0])
        with tempfile.TemporaryDirectory(dir=dest.parent) as tmp:
            work = Path(tmp)
            output = work / 'output'
            output.mkdir()
            if key == 'r5900':
                fetch(GHIDRA_URL, '088369c4d7de43cc95dbf254936dfed45eae5eddfcaf8b928189344bcc6a4dac', work / 'ee.zip')
                with zipfile.ZipFile(work / 'ee.zip') as archive:
                    archive.extractall(output)
                output = output / EXT
            elif key == 'transfer':
                fetch(KIT + 'tools/transfer_symbols.py', 'de9c783080ccb6b08f080b1917d5ae9ba755e8d1bc31f389e3534e392323fdf8', output / 'transfer_symbols.py')
                fetch(KIT + 'LICENSE', 'beecfa56ed5e30402222e538babb495f231ca915119c0ca5c37f7c3408f3ede9', output / 'LICENSE')
            else:
                compiler = shutil.which('c++')
                if not compiler:
                    raise ValueError('Install the system C++ compiler to build the native SDK scanner.')
                for path, sha in SCANNER_FILES.items():
                    fetch(RECOMP + path, sha, work / path)
                fetch(RAW + 'nlohmann/json/v3.11.3/single_include/nlohmann/json.hpp',
                      '9bea4c8066ef4a1c206b2be5a36302f8926f7fdc6087af5d20b417d0cf103ea6', work / 'nlohmann/json.hpp')
                log('Building the native SDK scanner')
                run = subprocess.run([compiler, '-std=c++17', '-O2', '-I', str(work), '-I', str(work / 'ps2xAnalyzer/include'),
                                      '-I', str(work / 'ps2xRecomp/include'), str(ROOT / 'sce_scan.cpp'),
                                      str(work / 'ps2xAnalyzer/src/sce_symbol_scanner.cpp'), '-o', str(output / 'sce-scan')],
                                     capture_output=True, text=True, timeout=180)
                if run.returncode:
                    raise ValueError('Building the SDK scanner failed: ' + run.stderr[-1200:])
                shutil.copyfile(work / 'LICENSE', output / 'LICENSE')
            output.rename(dest)
