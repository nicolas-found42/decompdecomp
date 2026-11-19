"""Identify imported EE functions using relocatable objects in an owner's SDK archives."""

from __future__ import annotations

import hashlib
import re
from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from struct import error as StructError

from elftools.common.exceptions import ELFError
from elftools.elf.elffile import ELFFile
from elftools.elf.relocation import RelocationSection

from ...elfobj import MASKS_BY_ARCH, Reloc, mask_relocs
from ...rom.match import R_MIPS_26, R_MIPS_32, R_MIPS_GPREL16, R_MIPS_HI16, R_MIPS_LO16, encode

AR_MAGIC = b"!<arch>\n"
AR_HEADER_SIZE = 60
MAX_ARCHIVE_SIZE = 256 << 20
MAX_FUNCTION_SIZE = 256 << 10
MIN_REVIEW_WORDS = 8
EXECUTABLE = 4  # SHF_EXECINSTR
SUPPORTED = frozenset({R_MIPS_32, R_MIPS_26, R_MIPS_HI16, R_MIPS_LO16, R_MIPS_GPREL16})
SYMBOL = re.compile(r"[A-Za-z_.$][A-Za-z0-9_.$]{0,254}\Z")


def archives(folder: Path) -> list[Path]:
    """Only regular archives under the selected SDK folder. Thin ar archives can refer to other folders."""
    return sorted(path for path in folder.rglob("*.a") if not path.is_symlink() and path.is_file())


def members(data: bytes):
    """Yield a member's name, archive offset and bytes for GNU and BSD ar files."""
    if not data.startswith(AR_MAGIC):
        raise ValueError("Choose an ordinary ar archive with embedded objects.")
    entries = b""
    at = len(AR_MAGIC)
    while at < len(data):
        header = data[at:at + AR_HEADER_SIZE]
        if header[-2:] != b"`\n" or len(header) != AR_HEADER_SIZE:
            raise ValueError("Invalid ar member header.")
        size = int(header[48:58])
        start = at + AR_HEADER_SIZE
        end = start + size
        if size < 0 or end + (size & 1) > len(data):
            raise ValueError("Invalid ar member size.")
        name = header[:16].rstrip(b" ")
        offset = at
        at = end + (size & 1)
        if name == b"//":
            entries = data[start:end]
            continue
        if name in (b"/", b"/SYM64/"):
            continue
        if name.startswith(b"#1/"):
            length = int(name[3:])
            if not 0 <= length <= size:
                raise ValueError("Invalid BSD ar member name.")
            name = data[start:start + length].rstrip(b"\0")
            start += length
        elif name.startswith(b"/"):
            index = int(name[1:])
            if not 0 <= index < len(entries):
                raise ValueError("Invalid GNU ar member name.")
            name = entries[index:].split(b"\n", 1)[0].rstrip(b"/")
        else:
            name = name.rstrip(b"/")
        if name.startswith(b"__.SYMDEF") or data[start:start + 6] != b"\x7fELF\x01\x01":
            continue
        # Member names are display labels. An archive member never becomes a filesystem path.
        label = "".join(c if c.isprintable() else "?" for c in name.decode("utf-8", errors="replace"))
        yield label[:255], offset, data[start:end]


@dataclass(frozen=True)
class LibraryFunction:
    name: str
    archive: str
    archive_hash: str
    member: str
    member_offset: int
    refs: tuple[Reloc, ...]
    unsupported: bool


def _functions(data: bytes, archive: str, archive_hash: str, member: str, member_offset: int):
    elf = ELFFile(BytesIO(data))
    if elf["e_machine"] != "EM_MIPS" or elf["e_type"] != "ET_REL":
        return
    symbols = elf.get_section_by_name(".symtab")
    if symbols is None:
        return
    code_sections = {i: sec.data() for i, sec in enumerate(elf.iter_sections())
                     if sec["sh_type"] == "SHT_PROGBITS" and sec["sh_flags"] & EXECUTABLE}
    relocations = defaultdict(list)
    for section in elf.iter_sections():
        if not isinstance(section, RelocationSection) or section["sh_info"] not in code_sections:
            continue
        table = elf.get_section(section["sh_link"])
        for entry in section.iter_relocations():
            if rtype := entry["r_info_type"]:
                symbol = table.get_symbol(entry["r_info_sym"])
                relocations[section["sh_info"]].append((entry["r_offset"], rtype, symbol.name,
                                                       entry["r_addend"] if entry.is_RELA() else None))
    for records in relocations.values():
        records.sort(key=lambda item: item[0])
    for symbol in symbols.iter_symbols():
        index, size, start = symbol["st_shndx"], symbol["st_size"], symbol["st_value"]
        if (symbol["st_info"]["type"] != "STT_FUNC" or index not in code_sections or not SYMBOL.fullmatch(symbol.name)
                or not 0 < size <= MAX_FUNCTION_SIZE or size % 4 or start % 4):
            continue
        code = code_sections[index][start:start + size]
        if len(code) != size:
            continue
        records = relocations.get(index, [])
        refs = []
        unsupported = False
        first = bisect_left(records, start, key=lambda item: item[0])
        last = bisect_left(records, start + size, lo=first, key=lambda item: item[0])
        for absolute, rtype, name, addend in records[first:last]:
            at = absolute - start
            if rtype not in SUPPORTED or at % 4 or at + 4 > size:
                unsupported = True
                continue
            mask = MASKS_BY_ARCH.get(("mips", rtype), (0, 4, 0xFFFFFFFF))[2]
            encoded = int.from_bytes(code[at:at + 4], "little") & mask if addend is None else encode(rtype, addend)
            refs.append(Reloc(at, rtype, name if SYMBOL.fullmatch(name) else "", encoded))
        body = LibraryFunction(symbol.name, archive, archive_hash, member, member_offset, tuple(refs), unsupported)
        yield code, body


@dataclass
class LibraryIndex:
    fingerprint: str
    by_size: dict
    archive_count: int
    function_count: int
    skipped: int

    def candidates(self, code: bytes) -> list[LibraryFunction]:
        """Compare complete bodies, clearing only the fields listed in each SDK relocation layout."""
        return [candidate for layout, signatures in self.by_size.get(len(code), {}).items()
                for candidate in signatures.get(mask_relocs(code, layout, "mips", False), ())]


def compare(indexes: list[LibraryIndex], code: bytes, relocs: list, resolve, gp: int) -> list[dict]:
    """Return evidence for name review, with no library code or SDK paths in the result."""
    target_refs = {(offset, kind): addr for offset, kind, addr in relocs}
    candidates = {fn: lib.fingerprint for lib in indexes for fn in lib.candidates(code)}
    out = []
    for fn, fingerprint in candidates.items():
        supported = {(ref.offset, ref.type) for ref in fn.refs}
        layout_ok = not fn.unsupported and supported == target_refs.keys()
        references = []
        for ref in fn.refs:
            actual = target_refs.get((ref.offset, ref.type))
            expected = resolve(ref.symbol) if ref.symbol else None
            # With a REL addend in the instruction, this simple comparison cannot distinguish base and addend.
            if actual is None or not ref.symbol or ref.addend:
                continue
            mask = MASKS_BY_ARCH.get(("mips", ref.type), (0, 4, 0xFFFFFFFF))[2]
            linked = actual - gp if ref.type == R_MIPS_GPREL16 else actual
            field = int.from_bytes(code[ref.offset:ref.offset + 4], "little") & mask
            if field == encode(ref.type, linked) & mask:
                references.append((ref.symbol, actual, expected == actual))
        resolved = sum(known for _, _, known in references)
        out.append({"name": fn.name, "sdk": fingerprint, "archive": fn.archive, "archive_hash": fn.archive_hash,
                    "member": fn.member, "member_offset": fn.member_offset, "extent": len(code),
                    "relocations": len(fn.refs), "resolved_relocations": resolved, "layout_agrees": layout_ok,
                    "references": references,
                    "bulk_evidence": len(candidates) == 1 and len(code) >= MIN_REVIEW_WORDS * 4
                    and layout_ok and 0 < resolved == len(fn.refs)})
    return out


def index(folder: Path, check_cancel=lambda: None) -> LibraryIndex:
    """Index named functions in the SDK without running vendor tools or retaining member files."""
    root = folder.resolve()
    digest = hashlib.sha256()
    signatures = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    archive_count = function_count = skipped = 0
    for archive in archives(root):
        check_cancel()
        if archive.stat().st_size > MAX_ARCHIVE_SIZE:
            skipped += 1
            continue
        path = archive.relative_to(root).as_posix()
        data = archive.read_bytes()
        archive_hash = hashlib.sha256(data).hexdigest()
        digest.update(path.encode("utf-8", errors="surrogateescape") + b"\0" + bytes.fromhex(archive_hash))
        try:
            for member, offset, payload in members(data):
                check_cancel()
                try:
                    for code, body in _functions(payload, path, archive_hash, member, offset):
                        layout = tuple(Reloc(r.offset, r.type, "", 0) for r in body.refs)
                        masked = mask_relocs(code, layout, "mips", False)
                        signatures[len(code)][layout][masked].append(body)
                        function_count += 1
                except (ELFError, IndexError, KeyError, StructError, ValueError):
                    skipped += 1  # some SDKs ship other object formats beside their EE ELF objects
            archive_count += 1
        except ValueError:
            skipped += 1  # some SDKs have linker scripts with .a names, or contain damaged archives
    return LibraryIndex(digest.hexdigest(), signatures, archive_count, function_count, skipped)
