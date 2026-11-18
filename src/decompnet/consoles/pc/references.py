"""Show what a function references in the original PE using the operand relocations found at import."""

from __future__ import annotations

import json

from capstone import x86

from . import scratch

IMPORTS = "pe_imports"
DATA = "pe_data_refs"
PREVIEW_BYTES = 160


def preview(raw: bytes) -> str:
    """Describe file bytes conservatively. Quoted text is data from the executable."""
    for width, encoding in ((1, "ascii"), (2, "utf-16le")):
        end = next((i for i in range(0, len(raw) - width + 1, width) if raw[i:i + width] == bytes(width)), None)
        if end is None or end < 4 * width:
            continue
        if width == 2 and not any(raw[i] == 0 for i in range(1, end, 2)):
            continue  # Arbitrary pairs of binary bytes often decode to printable Unicode.
        try:
            text = raw[:end].decode(encoding)
        except UnicodeDecodeError:
            continue
        if all(c.isprintable() or c in "\r\n\t" for c in text):
            return f"{encoding} {json.dumps(text, ensure_ascii=True)}"
    return "bytes " + raw[:12].hex(" ") if raw else "no file-backed bytes"


def references(pe, entry: dict, kinds, limit: int) -> dict[str, list[str]]:
    """Use only relocated operands in decoded instructions. Embedded tables, immediates and dynamic dispatch
    without a resolvable static address do not establish references."""
    rows = {k: [] for k in kinds}
    relocs = {r[0] for r in entry["relocs"] if r[2]}
    if not relocs:
        return rows
    start = entry["addr"]
    code = pe.mapped_bytes(start, min(entry["size"], max(relocs) + 16))
    for ins in scratch.reader(pe).disasm(code, start):
        if all(len(v) >= limit for v in rows.values()):
            break
        off = ins.address - start
        branch = ins.id in (x86.X86_INS_CALL, x86.X86_INS_JMP)
        for operand in ins.operands:
            if operand.type == x86.X86_OP_IMM and ins.imm_size and off + ins.imm_offset in relocs:
                addr = operand.imm
                mode = "address"
            elif operand.type == x86.X86_OP_MEM and ins.disp_size and off + ins.disp_offset in relocs:
                mem = operand.mem
                if mem.index or mem.segment or mem.base not in (0, x86.X86_REG_RIP):
                    continue
                addr = mem.disp + (ins.address + ins.size if mem.base else 0)
                mode = "address" if ins.id == x86.X86_INS_LEA else "memory"
            else:
                continue
            if not pe.x64:
                addr &= 0xffffffff
            sec = pe.section_at(addr)
            if not sec:
                continue
            if branch and operand.type == x86.X86_OP_IMM and not sec.code:
                continue
            thunk = branch and operand.type == x86.X86_OP_IMM and sec.code
            slot = scratch.import_thunk_slot(pe, addr) if thunk else addr
            ident = pe.import_identities.get(slot)
            at = f"{ins.address:#x} {ins.mnemonic}"
            if ident:
                if IMPORTS in rows and len(rows[IMPORTS]) < limit:
                    dll, target = ident
                    label = f"#{target}" if isinstance(target, int) else target
                    via = f"via thunk {addr:#x}" if thunk else "via IAT" if branch else f"IAT {mode} reference"
                    rows[IMPORTS].append(f"{at} {via} {slot:#x} -> {dll}!{label}")
            elif not sec.code and addr not in pe.imports and DATA in rows and len(rows[DATA]) < limit:
                raw = pe.mapped_bytes(addr, PREVIEW_BYTES)
                rows[DATA].append(f"{at} {mode} {addr:#x} ({sec.name}), {preview(raw)}")
    return rows
