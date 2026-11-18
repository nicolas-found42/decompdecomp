"""Helper evidence comes from the original executable, even when objdiff represents the operand as a symbol."""

import struct

import pytest

from decompnet import consoles
from decompnet.app import tools
from decompnet.consoles.pc import references as refs
from decompnet.consoles.pc import scratch


def image(x64=False):
    b = bytearray(0x400)
    return scratch.PE(b, 0, len(b), 0, 0, [
        scratch.Section(".text", 0x100, 0x80, 0x100, 0x80, True),
        scratch.Section(".rdata", 0x200, 0x180, 0x200, 0x180, False)], x64=x64)


def test_named_and_ordinal_imports_with_same_names_keep_their_dll_and_use_lookup_before_bound_iat():
    pe = image()
    pe.directories = 0x10
    struct.pack_into("<II", pe.data, 0x18, 0x200, 60)
    for k, dll in enumerate((b"one.dll", b"two.dll")):
        lookup, slots, dll_at = 0x250+k*16, 0x2a0+k*16, 0x280+k*8
        struct.pack_into("<IIIII", pe.data, 0x200+20*k, lookup, 1, 0, dll_at, slots)
        struct.pack_into("<III", pe.data, lookup, 0x300, 0x80000007, 0)
        pe.data[dll_at:dll_at+7] = dll
        struct.pack_into("<II", pe.data, slots, 0x70001234, 0x70003456)
    pe.data[0x302:0x307] = b"Test\0"
    names = scratch._imports(pe)
    assert names[0x2a0] == names[0x2b0] == "Test"
    assert pe.import_identities == {0x2a0: ("one.dll", "Test"), 0x2a4: ("one.dll", 7),
                                    0x2b0: ("two.dll", "Test"), 0x2b4: ("two.dll", 7)}
    # With no lookup table a bound address gives no name, and alignment padding isn't a terminated name.
    struct.pack_into("<I", pe.data, 0x200, 0)
    struct.pack_into("<I", pe.data, 0x260, 0x37a)
    pe.data[0x37c:0x380] = b"Test"
    pe.import_identities.clear()
    scratch._imports(pe)
    assert 0x2a0 not in pe.import_identities and 0x2b0 not in pe.import_identities


@pytest.mark.parametrize("wide", [False, True])
def test_direct_and_thunk_calls_and_jumps_are_distinct_from_loading_a_slot(wide):
    pe = image(wide)
    slot = 0x2a0
    pe.import_identities[slot] = ("audio.dll", 7)
    def disp(at):
        return struct.pack("<i", slot - at if wide else slot)
    pe.data[0x100:0x106] = b"\xff\x15"+disp(0x106)
    pe.data[0x106:0x10b] = b"\xe8"+struct.pack("<i", 0x160-0x10b)
    pe.data[0x10b:0x111] = b"\x8b\x05"+disp(0x111)
    pe.data[0x111:0x117] = b"\xff\x25"+disp(0x117)
    pe.data[0x117:0x119] = b"\xff\xd0"
    pe.data[0x160:0x166] = b"\xff\x25"+disp(0x166)
    e = {"addr": 0x100, "size": 0x19, "relocs": [(o, 0, "external", 0) for o in (2,7,13,19)]}
    out = refs.references(pe, e, [refs.IMPORTS], 20)[refs.IMPORTS]
    assert [s.split(" 0x2a0")[0] for s in out] == [
        "0x100 call via IAT", "0x106 call via thunk 0x160", "0x10b mov IAT memory reference", "0x111 jmp via IAT"]
    assert all(s.endswith("audio.dll!#7") for s in out)


def test_data_refs_need_indexed_operands_and_read_only_the_mapped_section():
    pe = image()
    pe.data[0x200:0x207] = b'quoted\0'
    pe.data[0x220:0x22a] = "wide\0".encode("utf-16le")
    pe.data[0x37c:0x380] = b"tail"
    ptrs = (0x200, 0x201, 0x220, 0x37c, 0x100, 0x200)
    code = b"".join(b"\x68" + struct.pack("<I", p) for p in ptrs)
    pe.data[0x100:0x100+len(code)] = code
    e = {"addr": 0x100, "size": len(code), "relocs": [(1+i*5,6,"data",0) for i in range(5)]}
    lines = refs.references(pe,e,[refs.DATA],20)[refs.DATA]
    assert [line.split(", ")[-1] for line in lines] == ['ascii "quoted"', 'ascii "uoted"', 'utf-16le "wide"', 'bytes 74 61 69 6c']
    assert refs.references(pe,e,[refs.DATA],2)[refs.DATA] == lines[:2]


def test_only_generated_pc_projects_offer_the_reference_tools():
    pc = consoles.get("pc")
    assert set(pc.reference_tools) <= set(tools.tool_names(True, pc))
    for names in (tools.tool_names(True, pc, scratch=False), tools.tool_names(False, pc),
                  tools.tool_names(True, consoles.get("gc")), tools.tool_names(False)):
        assert not set(pc.reference_tools).intersection(names)
