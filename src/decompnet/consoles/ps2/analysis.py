"""Read-only analysis of a PS2 project's original program."""
import csv
import hashlib
import os
import subprocess
import sys
import tempfile
import threading
from functools import lru_cache
from pathlib import Path

from elftools.common.exceptions import ELFError
from elftools.elf.elffile import ELFFile

from ...forks import ghidra
from ...limits import capped, stop_group
from ...network import NetworkError
from . import analysis_tools

MAX_ELF = 128 << 20
SYMBOL_REPORTS_CACHED = 4


def check_elf(path):
    if not path.is_file() or path.stat().st_size > MAX_ELF:
        raise ValueError('Choose a PS2 ELF file under 128 MiB.')
    with path.open('rb') as f:
        if f.read(6) != b'\x7fELF\x01\x01':
            raise ValueError('Choose a 32-bit little-endian PS2 ELF file.')
        f.seek(0)
        if ELFFile(f).header.e_machine != 'EM_MIPS':
            raise ValueError('The selected ELF uses a different processor.')


def run_command(argv, directory, env=None):
    with subprocess.Popen(capped([str(arg) for arg in argv], stop_all=False), cwd=directory, env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=os.name != 'nt') as proc:
        try:
            out, err = proc.communicate(timeout=180)
        except subprocess.TimeoutExpired:
            stop_group(proc)
            raise ValueError('The analysis tool exceeded its three-minute time limit.') from None
    if proc.returncode:
        raise ValueError((err or out)[-1600:].decode(errors='replace') or 'The analysis tool failed.')
    return out.decode(errors='replace')


def _tool_revision(path):
    stat = path.stat()
    return stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


@lru_cache(maxsize=SYMBOL_REPORTS_CACHED)
def _sdk_symbols(program, program_digest, scanner, scanner_revision):
    # A function filter is applied after this whole-ELF scan. All helpers can reuse the same report.
    with tempfile.TemporaryDirectory(prefix='decompdecomp-ps2-sdk-') as tmp:
        work = Path(tmp)
        with program.open('rb') as f:
            section = ELFFile(f).get_section_by_name('.text')
            if section is None:
                raise ValueError('This ELF has no .text section to scan.')
            (work / 'code').write_bytes(section.data())
            address = section['sh_addr']
        text = run_command([scanner, work / 'code', hex(address)], work)
        _check_scan_inputs((program, program_digest), tool=(scanner, scanner_revision))
        return text


@lru_cache(maxsize=SYMBOL_REPORTS_CACHED)
def _transfer_symbols(program, program_digest, companion, companion_digest, catalog, script, script_revision):
    # Capture symbol addresses, sizes and names together so edits cannot cache a report under the old catalog.
    with tempfile.TemporaryDirectory(prefix='decompdecomp-ps2-transfer-') as tmp:
        work = Path(tmp)
        with (work / 'functions.csv').open('w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(('binary', 'type', 'va', 'size', 'name'))
            for address, size, name in catalog:
                writer.writerow((program.name, 'FUNC', hex(address), size, name))
        report = work / 'transfer.tsv'
        run_command([sys.executable, '-I', script, '--donor', program,
                     '--donor-symbols', work / 'functions.csv', '--acceptor', companion, '--mask', 'strict',
                     '--out', report], work)
        _check_scan_inputs((program, program_digest), (companion, companion_digest), tool=(script, script_revision))
        return report.read_text(encoding='utf-8')


def _check_scan_inputs(*images, tool):
    if (any(hashlib.sha1(path.read_bytes()).hexdigest() != checksum for path, checksum in images)
            or _tool_revision(tool[0]) != tool[1]):
        raise ValueError('A PS2 scan input changed during analysis. Run it again.')


def run(net, proj, key, data, log):
    if key not in analysis_tools.TOOLS or net.console(proj).id != 'ps2' or not net.is_rom(proj):
        raise ValueError('Choose a PS2 ROM project and a PS2 analysis tool.')
    if not analysis_tools.ready(key):
        raise ValueError('Install this PS2 analysis tool in Compilers & tools.')
    root = Path(proj['root']).resolve()
    rom = net.cfg(proj)['rom']
    program = (root / (rom.get('program') or rom['file'])).resolve()
    if not program.is_relative_to(root):
        raise ValueError('The project ELF must be inside the project folder.')
    check_elf(program)
    program_digest = hashlib.sha1(program.read_bytes()).hexdigest()
    if rom.get('sha1') and program_digest != rom['sha1']:
        raise ValueError('The project ELF differs from the imported game.')
    log('Reading ' + program.name)
    if key == 'r5900':
        with tempfile.TemporaryDirectory(prefix='decompdecomp-ps2-') as tmp:
            work = Path(tmp)
            try:
                address = int(str(data.get('address', '')), 16)
            except ValueError:
                raise ValueError('Enter the hexadecimal start address of a project function.') from None
            if not 0 <= address <= 0xffffffff or not net.store.one(
                    'SELECT id FROM functions WHERE project_id=? AND vram=?', proj['id'], address):
                raise ValueError('Choose the start address of a function in this project.')
            report = work / 'draft.c'
            log(f'Decompiling 0x{address:08X} with the Emotion Engine processor')
            out = run_command([ghidra._headless(), work, 'ps2', '-import', program, '-processor', 'r5900:LE:32:default',
                               '-max-cpu', '2', '-scriptPath', analysis_tools.ROOT, '-postScript', 'R5900Draft.java',
                               hex(address), report, '-deleteProject'], work, ghidra._env())
            if not report.is_file():
                raise ValueError('Ghidra could not produce a draft. ' + out[-700:])
            return {'summary': f'R5900 pseudocode for 0x{address:08X}', 'text': report.read_text(encoding='utf-8')}
    if key == 'sce-scan':
        scanner = analysis_tools.location(key) / 'sce-scan'
        log('Reading the PS2 SDK signature scan')
        text = _sdk_symbols(program, program_digest, scanner, _tool_revision(scanner))
        return {'summary': f'{len(text.splitlines()) - 1} SDK symbol candidates', 'text': text}
    companion = Path(data['companion']).expanduser() if data.get('companion') else root / 'analysis-inputs/CORE.elf'
    check_elf(companion)
    companion_digest = hashlib.sha1(companion.read_bytes()).hexdigest()
    if companion.samefile(program) or companion_digest == program_digest:
        raise ValueError('Choose a different executable for the comparison.')
    catalog = tuple(tuple(row) for row in net.store.all(
        'SELECT vram,size,name FROM functions WHERE project_id=? ORDER BY vram,name', proj['id']))
    script = analysis_tools.location(key) / 'transfer_symbols.py'
    log('Reading function shape candidates in ' + companion.name)
    text = _transfer_symbols(program, program_digest, companion, companion_digest, catalog, script, _tool_revision(script))
    return {'summary': f'{len(text.splitlines()) - 1} functions with candidates in {companion.name}', 'text': text}


# Shared by MCP and in-process API helpers. Starting many Ghidra JVMs can exhaust a Mac.
_agent_slots = threading.BoundedSemaphore(2)


def for_agent(net, slug, key, function="", offset=0, companion="CORE.elf"):
    """Show a project's decompiler or symbol evidence in pages small enough for a helper's context."""
    if type(offset) is not int or offset < 0:
        raise NetworkError("Use a nonnegative character offset.")
    proj = net.project_row(slug)
    address = net._function(function, slug)["vram"] if function else None
    data = {"address": hex(address)} if address is not None else {}
    if key == "r5900" and address is None:
        raise NetworkError("Specify a project function name or its start address.")
    if key == "transfer":
        # Only the owner's desktop picker may open files outside the project.
        root = Path(proj["root"]).resolve()
        folder = root / "analysis-inputs"
        if (not isinstance(companion, str) or not companion or Path(companion).name != companion
                or not companion.lower().endswith(".elf") or "\\" in companion):
            raise NetworkError("Choose an ELF filename in this project's analysis-inputs folder.")
        path = (folder / companion).resolve()
        if not path.is_relative_to(folder) or not path.is_file():
            raise NetworkError("The companion ELF must exist inside this project's analysis-inputs folder.")
        data["companion"] = str(path)
    if not _agent_slots.acquire(blocking=False):
        raise NetworkError("Two PS2 analyses are running. Retry this analysis after they finish.")
    try:
        result = run(net, proj, key, data, lambda line: None)
    except (OSError, ValueError, ELFError) as exc:
        raise NetworkError(str(exc)) from exc
    finally:
        _agent_slots.release()
    text = result["text"]
    if address is not None and key != "r5900":
        lines = text.splitlines()
        column = 0 if key == "sce-scan" else 1
        hits = [line for line in lines[1:] if int(line.split("\t")[column], 16) == address]
        if not hits:
            return f"No candidate starts at 0x{address:08X}."
        text = "\n".join([lines[0], *hits])
    end = offset + 5000
    more = f"\nContinue with offset={end}." if end < len(text) else ""
    return f"{result['summary']}\n{text[offset:end]}{more}"
