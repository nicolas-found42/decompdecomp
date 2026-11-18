"""Read-only analysis of a PS2 project's original program."""
import csv
import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from elftools.elf.elffile import ELFFile

from ...forks import ghidra
from ...limits import capped, stop_group
from . import analysis_tools

MAX_ELF = 128 << 20


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
    if rom.get('sha1') and hashlib.sha1(program.read_bytes()).hexdigest() != rom['sha1']:
        raise ValueError('The project ELF differs from the imported game.')
    log('Reading ' + program.name)
    with tempfile.TemporaryDirectory(prefix='decompdecomp-ps2-') as tmp:
        work = Path(tmp)
        if key == 'r5900':
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
            with program.open('rb') as f:
                section = ELFFile(f).get_section_by_name('.text')
                if section is None:
                    raise ValueError('This ELF has no .text section to scan.')
                (work / 'code').write_bytes(section.data())
                address = section['sh_addr']
            log('Scanning .text against PS2 SDK signatures')
            result = run_command([analysis_tools.location(key) / 'sce-scan', work / 'code', hex(address)], work)
            return {'summary': f'{len(result.splitlines()) - 1} SDK symbol candidates', 'text': result}
        companion = Path(data['companion']).expanduser() if data.get('companion') else root / 'analysis-inputs/CORE.elf'
        check_elf(companion)
        if companion.samefile(program) or hashlib.sha1(companion.read_bytes()).hexdigest() == rom.get('sha1'):
            raise ValueError('Choose a different executable for the comparison.')
        with (work / 'functions.csv').open('w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(('binary', 'type', 'va', 'size', 'name'))
            for row in net.store.all('SELECT vram,size,name FROM functions WHERE project_id=?', proj['id']):
                writer.writerow((program.name, 'FUNC', hex(row['vram']), row['size'], row['name']))
        log('Comparing function shapes with ' + companion.name)
        report = work / 'transfer.tsv'
        run_command([sys.executable, '-I', analysis_tools.location(key) / 'transfer_symbols.py', '--donor', program,
                     '--donor-symbols', work / 'functions.csv', '--acceptor', companion, '--mask', 'strict',
                     '--out', report], work)
        text = report.read_text(encoding='utf-8')
        return {'summary': f'{len(text.splitlines()) - 1} functions with candidates in {companion.name}', 'text': text}
