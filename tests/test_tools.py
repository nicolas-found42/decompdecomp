"""The tools helpers get are the ones their project's console (or, for a ROM join, its engine) can do (#232): a DS
helper isn't offered joins it can't make, a GameCube one no function ends or C++ classes, and the API helper's
schemas say the same as the MCP tool list."""

import json
from types import SimpleNamespace

import pytest

from decompnet import consoles
from decompnet.agents.api import api_tool_schemas
from decompnet.app import agents, tools
from decompnet.app.engine import RomNetwork

# platform, project mode (a fork), tools that must be there, tools that must not
CASES = [
    ("gc", True, {"claim_function", "file_status", "split_file", "compiler_map"},
     {"join_functions", "report_boundary", "get_class", "guess_struct", "redraft", "search_flags"}),
    ("wii", True, {"claim_function", "file_status", "split_file", "compiler_map"},
     {"join_functions", "report_boundary", "get_class", "guess_struct", "redraft", "search_flags"}),
    ("nds", True, {"claim_function", "file_status", "split_file", "join_functions", "compiler_map"},
     {"report_boundary", "get_class", "guess_struct", "redraft", "search_flags"}),
    ("pc", True, {"join_functions", "report_boundary", "get_class", "guess_struct", "compiler_map", "redraft",
                  "search_flags"}, set()),
    ("n64", False, {"report_boundary", "get_class", "guess_struct", "compiler_map", "redraft", "search_flags"},
     {"join_functions", "read_file", "split_file"} | tools.PS2_ANALYSIS),
    ("ps2", True, {"try_compiler"}, tools.PS2_ANALYSIS | {"compare_compilers"}),
    ("ps2", False, tools.PS2_ANALYSIS | {"report_boundary", "get_class", "guess_struct", "compiler_map", "redraft",
                                       "search_flags", "compare_compilers"},
     {"join_functions", "read_file", "split_file"}),
]


@pytest.mark.parametrize("platform,fork,present,absent", CASES, ids=[c[0] for c in CASES])
def test_the_mcp_tools_and_api_schemas_are_what_the_console_can_do(platform, fork, present, absent):
    console = consoles.get(platform)
    names = tools.tool_names(fork, console)
    assert present <= set(names)
    assert absent.isdisjoint(names)
    # the API helper gets the same list, not a schema for a tool the MCP list leaves out
    assert {t["function"]["name"] for t in api_tool_schemas(fork=fork, names=names)} == set(names)


def test_an_old_prompt_calling_an_unoffered_tool_gets_a_sentence(tmp_path):
    """An older prompt (or a cached tool list) calling join_functions on a GameCube project is told why, not a
    traceback from the console's default."""
    net = RomNetwork(tmp_path / "app.db", tmp_path / "ws")
    root = tmp_path / "ws" / "gc"
    root.mkdir(parents=True)
    (root / "objdiff.json").write_text(json.dumps({"units": []}))
    net.forks._register("gc", "GC", "fork", root,
                        {"kind": "fork", "platform": "gc", "origin": "scratch", "ready": True}, "x")
    agent, _ = net.local_agent("old-prompt")
    text = tools.call(net, agent, "gc", "join_functions",
                      {"function": "a", "with_function": "b", "reason": "a has no epilogue"})
    assert text.startswith("error: join_functions isn't offered in GameCube projects")
    assert "can't join functions" in text and "Traceback" not in text


def test_compare_compilers_ranks_a_saved_candidate_against_its_assigned_profile(tmp_path, monkeypatch):
    net = RomNetwork(tmp_path / "app.db", tmp_path)
    baseline, default, alternate = "ee-gcc-2.9-991111", "ee-gcc-2.96", "ee-gcc-2.9-as296"
    cfg = {"kind": "rom", "platform": "ps2", "compiler": {"toolchain": default, "flags": ["-O2"]},
           "compiler_ranges": [{"start": 0x1000, "end": 0x1020, "toolchain": baseline, "flags": ["-O2", "-G0"]}]}
    candidate = "int timer(void) { return 1; }"
    with net.store.tx() as db:
        pid = db.execute("INSERT INTO projects(slug,name,root,config) VALUES(?,?,?,?)",
                         ("demo", "Demo", str(tmp_path), json.dumps(cfg))).lastrowid
        db.execute("INSERT INTO functions(project_id,name,vram,status,best_source) VALUES(?,?,?,?,?)",
                   (pid, "timer", 0x1000, "open", candidate))
    monkeypatch.setattr(net.progress, "sync", lambda *args, **kwargs: None)
    trials = []

    def judge(proj, fn, compiler, flags, source):
        trials.append((compiler, flags, source))
        if compiler == default:
            return False, None, "Compilation failed."
        return compiler == alternate, SimpleNamespace(score=460, percent=97), ""

    monkeypatch.setattr(net, "_judge_with", judge)
    agent, _ = net.local_agent("compare")
    args = {"function": "timer", "compilers": [default, alternate], "flags": "-O2"}
    report = tools.call(net, agent, "demo", "compare_compilers", args)
    assert trials == [(baseline, ["-O2", "-G0"], candidate), (default, ["-O2"], candidate),
                      (alternate, ["-O2"], candidate)]
    assert report.index("exact linked match") < report.index("score 460") < report.index("no result")
    assert "(assigned)" in report and "Compilation failed." in report

    trials.clear()
    report = tools.call(net, agent, "demo", "compare_compilers", {**args, "compilers": [baseline, baseline], "flags": ""})
    assert "(assigned)" in report and trials == [(baseline, ["-O2", "-G0"], candidate)]
    trials.clear()
    report = tools.call(net, agent, "demo", "compare_compilers", {**args, "compilers": [baseline, default, alternate]})
    assert "one or two" in report and trials == []
    assert net.cfg(net._project("demo")) == cfg
    assert not net.store.all("SELECT * FROM attempts")


def test_ps2_symbol_reports_reuse_scans_until_the_inputs_change(tmp_path, monkeypatch):
    from decompnet.consoles.ps2 import analysis

    program = tmp_path / "program.elf"
    companion = tmp_path / "companion.elf"
    program.write_bytes(b"program")
    companion.write_bytes(b"companion")
    for tool in ("sce-scan", "transfer_symbols.py"):
        (tmp_path / tool).touch()

    cfg = {"rom": {"file": program.name}}
    catalog = [(0x1000, 8, "timer")]
    net = SimpleNamespace(console=lambda _: consoles.get("ps2"), is_rom=lambda _: True, cfg=lambda _: cfg,
                          store=SimpleNamespace(all=lambda *_: catalog))
    proj = {"id": 1, "root": str(tmp_path)}
    monkeypatch.setattr(analysis, "check_elf", lambda _: None)
    monkeypatch.setattr(analysis.analysis_tools, "ready", lambda _: True)
    monkeypatch.setattr(analysis.analysis_tools, "location", lambda _: tmp_path)

    class Text:
        def data(self):
            return program.read_bytes()

        def __getitem__(self, key):
            return 0x1000

    monkeypatch.setattr(analysis, "ELFFile", lambda _: SimpleNamespace(get_section_by_name=lambda _: Text()))
    calls = []

    def run(argv, work):
        if argv[0] == tmp_path / "sce-scan":
            calls.append("sdk")
            return "address\tname\n0x1000\ttimer"
        calls.append("transfer")
        (work / "transfer.tsv").write_text((work / "functions.csv").read_text(), encoding="utf-8")
        return ""

    monkeypatch.setattr(analysis, "run_command", run)

    def scan(tool):
        return analysis.run(net, proj, tool, {"companion": str(companion)}, lambda _: None)

    assert scan("sce-scan") == scan("sce-scan")
    assert scan("transfer") == scan("transfer")
    assert calls == ["sdk", "transfer"]

    catalog[0] = (0x1000, 8, "renamed")
    assert "renamed" in scan("transfer")["text"]
    companion.write_bytes(b"updated companion")
    scan("transfer")
    program.write_bytes(b"updated program")
    scan("sce-scan")
    assert calls == ["sdk", "transfer", "transfer", "transfer", "sdk"]


def test_a_rom_engine_that_can_join_offers_it_to_rom_consoles(monkeypatch):
    """#192 adds ROM joins in the engine, not on Console: once its join_functions handler exists (and FORK_ONLY no
    longer hides the tool), every ROM console gets it. Project mode still asks the console: PC joins, GameCube not."""
    monkeypatch.setattr(tools, "FORK_ONLY", tools.FORK_ONLY - {"join_functions"})
    monkeypatch.setattr(tools.RomNetwork, "join_functions", lambda *args, **kwargs: "", raising=False)
    for platform in ("n64", "ps2", "gba"):
        assert "join_functions" in tools.tool_names(False, consoles.get(platform))
    assert "join_functions" in tools.tool_names(True, consoles.get("pc"))
    assert "join_functions" not in tools.tool_names(True, consoles.get("gc"))


def test_the_rom_prompt_names_joins_only_when_the_engine_has_them(tmp_path, monkeypatch):
    """A ROM prompt follows the same gate as its tool list: no join_functions before #192, and the join guidance
    once the engine has it."""
    net = RomNetwork(tmp_path / "app.db", tmp_path / "ws")
    manager = agents.Manager(net, tmp_path, "http://127.0.0.1:1/mcp")
    session = agents.Session("1", "claude", "claude-1", "rom", "claude-1", {})
    monkeypatch.setattr(net, "overview", lambda slug: {
        "kind": "rom", "title": "Game", "platform": "n64", "compiler": {"toolchain": "gcc-2.7.2-kmc"}})
    assert "join_functions" not in manager._prompt(session)
    monkeypatch.setattr(tools, "FORK_ONLY", tools.FORK_ONLY - {"join_functions"})
    monkeypatch.setattr(tools.RomNetwork, "join_functions", lambda *args, **kwargs: "", raising=False)
    assert "join_functions" in manager._prompt(session)


def test_a_rom_project_without_engine_joins_names_the_engine(tmp_path, monkeypatch):
    """The refusal follows the same gate: ROM joins are the engine's (#192), not a console's, so say so."""
    monkeypatch.setattr(tools, "FORK_ONLY", tools.FORK_ONLY - {"join_functions"})
    net = RomNetwork(tmp_path / "app.db", tmp_path / "ws")
    with net.store.tx() as db:
        db.execute("INSERT INTO projects(slug,name,description,arch,big_endian,root,config) VALUES(?,?,?,?,?,?,?)",
                   ("rom", "ROM", "", "mips", 0, str(tmp_path), json.dumps({"kind": "rom", "platform": "n64"})))
    agent, _ = net.local_agent("old-prompt")
    text = tools.call(net, agent, "rom", "join_functions",
                      {"function": "a", "with_function": "b", "reason": "a has no epilogue"})
    assert text.startswith("error: join_functions isn't offered in Nintendo 64 projects")
    assert "the ROM engine can't join functions" in text and "Traceback" not in text


def test_ps2_api_helper_can_page_analysis_and_only_read_project_companions(tmp_path, monkeypatch):
    from decompnet.consoles.ps2 import analysis

    root = tmp_path / "game"
    inputs = root / "analysis-inputs"
    inputs.mkdir(parents=True)
    (inputs / "CORE.elf").touch()
    outside = tmp_path / "other.elf"
    outside.touch()
    net = RomNetwork(tmp_path / "app.db", tmp_path)
    with net.store.tx() as db:
        pid = db.execute("INSERT INTO projects(slug,name,arch,big_endian,root,config) VALUES(?,?,?,?,?,?)",
                         ("game", "Game", "mips", 0, str(root), json.dumps({"kind": "rom", "platform": "ps2"}))).lastrowid
        db.execute("INSERT INTO functions(project_id,name,vram,size,rom_offset,target,relocs,status) "
                   "VALUES(?,?,?,?,?,?,?,?)", (pid, "timer", 0x1000, 8, 0, bytes(8), "[]", "open"))
    monkeypatch.setattr(net.progress, "sync", lambda *args, **kwargs: None)
    agent, _ = net.local_agent("api-probe")

    def run(net, proj, key, data, log):
        if key == "r5900":
            assert data["address"] == "0x1000"
            return {"summary": "Ghidra", "text": "x" * 5000 + "end"}
        if key == "transfer":
            assert data["companion"] == str(inputs / "CORE.elf")
            text = "name\tdonor_va\tacceptor_vas\ntimer\t0x1000\t0x800\nother\t0x1008\t0x808"
        else:
            text = "address\tname\n1000\tsdkTimer\n1008\tother"
        return {"summary": "candidates", "text": text}

    monkeypatch.setattr(analysis, "run", run)

    def invoke(name, **args):
        return tools.call(net, agent, "game", name, args)

    assert invoke("ps2_decompile", function="timer").endswith("Continue with offset=5000.")
    assert invoke("ps2_decompile", function="timer", offset=5000) == "Ghidra\nend"
    assert invoke("ps2_sdk_symbols", function="timer").endswith("1000\tsdkTimer")
    assert invoke("ps2_symbol_transfer", function="timer").endswith("timer\t0x1000\t0x800")
    for filename in ("../other.elf", str(outside)):
        assert invoke("ps2_symbol_transfer", companion=filename).startswith("error:")
    try:
        (inputs / "link.elf").symlink_to(outside)
    except OSError:
        pytest.skip("creating a symlink is unavailable on this host")
    assert invoke("ps2_symbol_transfer", companion="link.elf").startswith("error:")
