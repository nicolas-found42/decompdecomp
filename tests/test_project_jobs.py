"""The desktop and both helper paths share one queue and the normal judge."""

import threading
import time

import pytest

from decompnet.app import deep_permuter, project_jobs, tools
from decompnet.app.engine import RomNetwork
from decompnet.app.support import Jobs
from decompnet.network import NetworkError

from .test_loading import tiny_game


def test_queued_deep_permuter_uses_latest_source_and_takes_the_initiating_helpers_claim(tmp_path, monkeypatch):
    net = RomNetwork(tmp_path / "test.db", tmp_path)
    proj = tiny_game(net)
    other_project = tiny_game(net, "another")
    net.project_jobs = jobs = project_jobs.ProjectJobs(net, Jobs())
    with net.store.tx() as db:
        db.execute("UPDATE functions SET best_score=15, best_source='initial' WHERE project_id=?", (proj["id"],))
    helper, _ = net.local_agent("helper")
    other, _ = net.local_agent("other")
    net.claim_row(helper, "game", "func_0")
    net._update_cfg("game", lambda cfg: cfg.__setitem__(project_jobs.ENABLED, True))
    monkeypatch.setattr(deep_permuter, "COMPILER_SLOTS", 3)
    release = threading.Event()
    saw_baseline = threading.Event()

    def baseline(search, agent, project, function, fork):
        assert agent.name == "deep-permuter"
        assert function["best_source"] == "updated while queued"
        assert net.store.one("SELECT agent_id FROM claims WHERE function_id=?", function["id"])["agent_id"] == agent.id
        saw_baseline.set()
        return 0

    monkeypatch.setattr(deep_permuter.Search, "baseline", baseline)
    monkeypatch.setattr(deep_permuter.Search, "_permute", lambda *args: None)
    running = jobs.submit("game", "blocking-test", lambda _: release.wait(5))
    try:
        answer = tools.call(net, helper, "game", "deep_permuter", {"function": "func_0", "minutes": 1, "variants": 1})
        jid = answer.split()[3].removesuffix(".")
        assert jobs.status("game", jid)["state"] == "queued"
        assert jobs.deep_search("game", "func_0", 1, 1, agent=helper) == jid
        with pytest.raises(NetworkError, match="already has a queued job"):
            jobs.deep_search("game", "func_1", 1, 1, agent=helper)
        with pytest.raises(NetworkError, match="project owner or the client"):
            jobs.status("game", jid, agent=other, cancel=True)
        with pytest.raises(NetworkError, match="job from this project"):
            jobs.status(other_project["slug"], jid)
        with net.store.tx() as db:
            db.execute("UPDATE functions SET best_source='updated while queued' WHERE project_id=? AND name='func_0'",
                       (proj["id"],))
    finally:
        release.set()
    assert saw_baseline.wait(2)
    for _ in range(200):
        if jobs.status("game", jid)["state"] == "done":
            break
        time.sleep(0.01)
    assert jobs.status("game", jid)["state"] == "done"
    assert jobs.status("game", running)["state"] == "done"
    assert not net.store.one("SELECT * FROM claims WHERE function_id=?", net._function("func_0", "game")["id"])


def test_cancelling_a_queued_job_leaves_the_existing_job_running(tmp_path):
    net = RomNetwork(tmp_path / "test.db", tmp_path)
    tiny_game(net)
    jobs = project_jobs.ProjectJobs(net, Jobs())
    release = threading.Event()
    called = threading.Event()
    current = jobs.submit("game", "blocking-test", lambda _: release.wait(5))
    try:
        queued = jobs.submit("game", "queued-test", lambda _: called.set())
        assert jobs.status("game", queued, cancel=True)["state"] == "cancelled"
        assert jobs.status("game", current)["state"] == "running"
    finally:
        release.set()
    assert not called.is_set()


def test_reopening_keeps_saved_progress_but_drops_jobs(tmp_path):
    db_path = tmp_path / "test.db"
    net = RomNetwork(db_path, tmp_path)
    project = tiny_game(net)
    jobs = project_jobs.ProjectJobs(net, Jobs())
    saved = threading.Event()
    release = threading.Event()

    def search(log):
        with net.store.tx() as db:
            db.execute("UPDATE functions SET best_source='saved',best_score=10 WHERE project_id=?", (project["id"],))
        saved.set()
        release.wait(5)
        log.check_cancel()

    active = jobs.submit("game", "testing-search", search)
    try:
        assert saved.wait(2)
        queued = jobs.submit("game", "queued-search", lambda _: pytest.fail("queued job should not run"))
        jobs.shutdown()
        assert jobs.status("game", queued)["state"] == "cancelled"
    finally:
        release.set()
    for _ in range(200):
        if jobs.status("game", active)["state"] == "cancelled":
            break
        time.sleep(0.01)
    assert jobs.status("game", active)["state"] == "cancelled"
    reopened = RomNetwork(db_path, tmp_path)
    new_jobs = project_jobs.ProjectJobs(reopened, Jobs())
    assert reopened._function("func_0", "game")["best_source"] == "saved"
    with pytest.raises(NetworkError, match="job from this project"):
        new_jobs.status("game", active)
