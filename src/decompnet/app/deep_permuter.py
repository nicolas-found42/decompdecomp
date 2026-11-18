"""A user-started, bounded search of one saved C candidate.

The ordinary permuter handles near misses in short background turns. This search
lets the user spend a larger explicit budget on one function, while keeping the
same compiler, byte comparison, and best-candidate recording path.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, wait

from ..compiler import COMPILER_SLOTS, cancellable
from ..network import NetworkError
from ..project import compiler_for
from . import permuter_local, workers
from .permute import Permuter, State

# Its own worker, not the automatic permuter's "permuter" (network.MACHINE_AGENTS): sharing that name, a claim the
# automatic permuter took between this search's turns looked like the search's own, and ended it (#244).
AGENT = "deep-permuter"
HIGH_PERFORMANCE_WORKERS = 10
_trial_pools: dict[int, ThreadPoolExecutor] = {}
_trial_lock = threading.Lock()


def high_performance_available() -> bool:
    return COMPILER_SLOTS >= HIGH_PERFORMANCE_WORKERS + 2


def trial_width(fork: str | None) -> int:
    if fork:
        return 1
    if high_performance_available():
        return HIGH_PERFORMANCE_WORKERS
    return max(1, min(6, COMPILER_SLOTS - 2))


def trial_pool(width: int) -> ThreadPoolExecutor:
    """ROM searches share a trial pool and still acquire the app-wide compiler slots."""
    with _trial_lock:
        if width not in _trial_pools:
            _trial_pools[width] = ThreadPoolExecutor(width, thread_name_prefix="deep-trial",
                                                     initializer=workers._lower_priority)
        return _trial_pools[width]


def max_searches() -> int:
    """How many deep searches the app runs at once. Each fills one of the shared background workers (workers.pool)
    for its whole length, so one worker is always left for the automatic permuter and the drafts (a fixed 2 took
    every worker of a machine with 2). With a single worker there's nothing to leave, and one search may still run."""
    return max(1, min(2, workers.WORKERS - 1))


class SearchLimit(Exception):
    pass


class TrialStopped(Exception):
    pass


def validate_budget(minutes: int, max_variants: int) -> None:
    if isinstance(minutes, bool) or not isinstance(minutes, int) or not 1 <= minutes <= 120:
        raise NetworkError("Choose a time limit from 1 to 120 minutes.")
    if isinstance(max_variants, bool) or not isinstance(max_variants, int) or not 1 <= max_variants <= 1_000_000:
        raise NetworkError("Choose a variant limit from 1 to 1,000,000.")


def validate(net, slug: str, name: str, minutes: int, max_variants: int, *, handoff=None):
    validate_budget(minutes, max_variants)
    proj = net.project_row(slug)
    fn = net._function(name, slug)
    if slug in net.links:
        raise NetworkError("Run deep permuter on the room host's project.")
    if fn["status"] != "open" or not fn["best_source"] or fn["best_score"] is None:
        raise NetworkError("Choose an unmatched function with saved, scored C first.")
    claim = net.store.one("SELECT agent_id FROM claims WHERE function_id=? AND expires_at>?", fn["id"], time.time())
    if claim and (handoff is None or handoff.id != claim["agent_id"]):
        raise NetworkError("This function is working; wait for its claim to finish.")
    return proj, fn


class Search(Permuter):
    def __init__(self, net, minutes: int, max_variants: int, log, *, fork: str | None = None):
        super().__init__(net)
        self.trial_workers = trial_width(fork)
        self.deadline = time.monotonic() + minutes * 60
        self.limit = max_variants
        self.log = log
        self.tested = 0
        self.turns = 0
        self.seed = time.time_ns()
        self.started = time.monotonic()
        self.starting_score = self.best_score = None
        self.history = []
        self.skipped = self.visited = 0
        self.recent = OrderedDict()
        self.baseline_scope = None
        self.measuring_baseline = False
        self.held = False

    def report(self):
        getattr(self.log, "progress", lambda values: None)({
            "tested": self.tested, "skipped": self.skipped, "variant_limit": self.limit,
            "starting_score": self.starting_score, "best_score": self.best_score,
            "elapsed": round(time.monotonic() - self.started),
            "seconds_left": max(0, round(self.deadline - time.monotonic())),
            "history": list(self.history),
        })

    def _hold(self, agent, fn):
        # A manual search must not exhaust the automatic permuter's turn allowance.
        if not super()._hold(agent, fn, count_turn=False) or not self._renew(agent, fn):
            return False
        self.held = True
        return True

    def take_handoff(self, helper, agent, fn):
        """Transfer the helper's claim at the moment the search starts."""
        if helper is not None:
            now = time.time()
            with self.net.store.tx() as db:
                changed = db.execute("UPDATE claims SET agent_id=?, claimed_at=?, expires_at=? "
                                     "WHERE function_id=? AND agent_id=? AND expires_at>?",
                                     (agent.id, now, now + max(0, self.deadline - time.monotonic()) + 120,
                                      fn["id"], helper.id, now)).rowcount
            if changed:
                self.held = True
                return True
        return self._hold(agent, fn)

    def _let_go(self, agent, fn):
        self.held = False
        return super()._let_go(agent, fn)

    def _renew(self, agent, fn) -> bool:
        """The hold counted to the search's deadline; False when the claim isn't this search's any more.

        Permuter._permute ends a turn when this is False, so it must say True when the renewal worked: returning
        None made every project-mode turn end right after trial_run, untried (#244)."""
        now = time.time()
        expires = now + max(0, self.deadline - time.monotonic()) + 120
        with self.net.store.tx() as db:
            return db.execute("UPDATE claims SET expires_at=? WHERE function_id=? AND agent_id=? AND expires_at>?",
                              (expires, fn["id"], agent.id, now)).rowcount > 0

    def _random_seed(self, fn):
        self.turns += 1
        return f"{fn['id']}-deep-{self.seed}-{self.turns}"

    def _turn_deadline(self):
        return min(super()._turn_deadline(), self.deadline)

    def input_scope(self, proj, fn, fork):
        # Imported builds can change headers/build dependencies outside this database.
        # They always go through their build judge; do not reuse their old trials here.
        if fork:
            return None
        fresh_proj = self.net.project_row(proj["slug"])
        fresh_fn = self.net._function(fn["name"], proj["slug"])
        ctx = self.net._context(proj["id"], fresh_fn["unit"])
        if ctx is None:
            raise NetworkError("This project has no compiler header yet. Finish its setup before searching.")
        # Publishing this search's own improvement advances the save counter. It is
        # bookkeeping, not a compiler input: including it stopped the search after a win.
        config = json.loads(fresh_proj["config"])
        compiler = compiler_for(config, fresh_fn["vram"]) if config.get("compiler") else {}
        inputs = (json.dumps({"compiler": compiler, **{key: config.get(key) for key in
                              ("gp", "big_endian", "platform", "segments", "rom")}}, sort_keys=True),
                  fresh_proj["root"], id(self.net.symbols(proj["id"])), ctx["header"], ctx["version"],
                  tuple(fresh_fn[k] for k in ("id", "target", "relocs", "vram", "unit", "n_insns")))
        return hashlib.sha256(repr(inputs).encode()).digest()

    def baseline(self, agent, proj, fn, fork):
        self.baseline_scope = self.input_scope(proj, fn, fork)
        self.measuring_baseline = True
        try:
            score = self._score(agent, proj, fn, fn["best_source"], float("inf"), fork)
        finally:
            self.measuring_baseline = False
        if score is None:
            raise NetworkError("The saved C no longer compiles or cannot be judged. Fix it in Edit before searching.")
        if self.input_scope(proj, fn, fork) != self.baseline_scope:
            raise NetworkError("Compiler, header or target changed while checking the baseline. Run the search again.")
        # A header/compiler change can make the saved scalar score obsolete. Rebase
        # only this unchanged source; a concurrent improvement must never be overwritten.
        with self.net.store.tx() as db:
            current = self.net._function(fn["name"], proj["slug"])
            if current["status"] == "open" and current["best_score"] != score:
                if current["best_source"] != fn["best_source"] or current["best_score"] != fn["best_score"]:
                    raise NetworkError("The saved candidate changed while checking its baseline. Run the search again.")
                db.execute("UPDATE functions SET best_score=? WHERE id=?", (score, fn["id"]))
        self.starting_score = self.best_score = score
        self.history = [{"tested": 0, "score": score}]
        self.report()
        return score

    def _check_deadline(self):
        self.log.check_cancel()
        if time.monotonic() >= self.deadline:
            raise SearchLimit("Time limit reached")

    def _generate(self, gen, job):
        return gen.batch(job, check_cancel=self._check_deadline)

    def _check_scope(self, proj, fn, scope):
        if self.input_scope(proj, fn, None) != scope:
            raise SearchLimit("Compiler, header or target changed; run the search again")

    def _publish_trial(self, agent, proj, fn, ctx, scope, source, diff):
        progress = self.net.progress
        progress.sync(proj["slug"], force=True)
        with progress._project_lock(proj["slug"]), self.net.store.tx():
            self._check_deadline()
            self._check_scope(proj, fn, scope)
            current = self.net._function(fn["name"], proj["slug"])
            if current["status"] != "open" or not self._renew(agent, fn):
                raise SearchLimit("The function is no longer held by this search")
            if current["best_score"] is None or diff.score < current["best_score"]:
                self.net._record(current, agent, diff.score, source, ctx["version"], matched=diff.bytes_match,
                                 via="permuter")
            else:
                return False
        progress.sync(proj["slug"], force=True)
        return True

    def _trials(self, agent, proj, fn, batch, best, fork, deadline, st):
        if fork or len(batch) <= 1 or self.trial_workers == 1:
            yield from super()._trials(agent, proj, fn, batch, best, fork, deadline, st)
            return

        slug = proj["slug"]
        with self.net.progress.judging(slug):
            scope = self.baseline_scope or self.input_scope(proj, fn, None)
            self._check_scope(proj, fn, scope)
            ctx = self.net._context(proj["id"], fn["unit"])
            for offset in range(0, len(batch), self.trial_workers):
                if time.monotonic() >= deadline or st.stop.is_set():
                    break
                self._check_deadline()
                self._check_scope(proj, fn, scope)
                if not self._renew(agent, fn):
                    raise SearchLimit("The function is no longer held by this search")
                yield from self._trial_wave(agent, proj, fn, ctx, scope,
                                            batch[offset:offset + self.trial_workers], best)

    def _trial_wave(self, agent, proj, fn, ctx, scope, candidates, best):
        stop = threading.Event()
        futures = {}
        pending = []

        def check_cancel():
            if stop.is_set():
                raise TrialStopped
            self._check_deadline()

        def judge(source):
            with cancellable(check_cancel):
                check_cancel()
                return self._judge_rom(proj, fn, ctx["header"], source)

        try:
            for source in candidates:
                self._check_deadline()
                self._visit()
                key = (scope, hashlib.sha256(source.encode()).digest())
                if key in futures:
                    self.skipped += 1
                    # The serial search sees a duplicate's cached score too; this can affect the next random seed.
                    pending.append((source, key, None, futures[key]))
                    continue
                if key in self.recent:
                    previous = self.recent[key]
                    if previous is None or previous >= best():
                        self.skipped += 1
                        pending.append((source, key, previous, None))
                        continue
                if self.tested >= self.limit:
                    break
                future = futures[key] = trial_pool(self.trial_workers).submit(judge, source)
                pending.append((source, key, None, future))
                self.tested += 1
            self.report()
            for source, key, score, future in pending:
                self._check_deadline()
                if future is not None:
                    try:
                        diff = future.result()
                    except NetworkError as error:
                        yield source, None, error
                        continue
                    self._check_deadline()
                    score = None if diff is None else (0 if diff.bytes_match else max(1, diff.score))
                    if score is not None and score < best():
                        if not self._publish_trial(agent, proj, fn, ctx, scope, source, diff):
                            raise SearchLimit("The saved candidate changed; run the search again")
                        if diff.bytes_match:
                            stop.set()
                    self._remember(key, score)
                    self._note_score(best(), score)
                yield source, score, None
            if self.tested >= self.limit:
                raise SearchLimit("Variant limit reached")
        finally:
            stop.set()
            for future in futures.values():
                future.cancel()
            wait(futures.values())

    def _visit(self):
        self.visited += 1
        if self.visited > max(100, self.limit * 20):
            raise SearchLimit("No new candidates available")

    def _remember(self, key, score):
        self.recent[key] = score
        self.recent.move_to_end(key)
        if len(self.recent) > 1024:
            self.recent.popitem(last=False)

    def _score(self, agent, proj, fn, source, best, fork):
        self._check_deadline()
        self._visit()
        scope = self.input_scope(proj, fn, fork)
        if self.baseline_scope is not None and scope != self.baseline_scope:
            raise SearchLimit("Compiler, header or target changed; run the search again")
        key = (scope, hashlib.sha256(source.encode()).digest())
        if not self.measuring_baseline and scope is not None and key in self.recent:
            previous = self.recent[key]
            if previous is None or previous >= best:
                self.skipped += 1
                self.report()
                return previous
        if self.tested >= self.limit:
            raise SearchLimit("Variant limit reached")
        self.tested += 1
        if self.held and not self._renew(agent, fn):  # (another worker may have it by now: record nothing for it)
            raise SearchLimit("The function is no longer held by this search")
        with cancellable(self._check_deadline):
            score = super()._score(agent, proj, fn, source, best, fork)
        if self.input_scope(proj, fn, fork) != scope:
            raise SearchLimit("Compiler, header or target changed; run the search again")
        if scope is not None:
            self._remember(key, score)
        if not self.measuring_baseline:
            self._note_score(best, score)
        return score

    def _note_score(self, best, score):
        if self.starting_score is None:
            self.starting_score = self.best_score = best
            self.history = [{"tested": 0, "score": best}]
        if score is not None and score < self.best_score:
            self.best_score = score
            self.history.append({"tested": self.tested, "score": score})
            if len(self.history) > 200:
                self.history = self.history[:1] + self.history[2:-1:2] + self.history[-1:]
        self.report()
        if self.tested == 1 or self.tested % 25 == 0:
            self.log(f"Tested {self.tested} candidates; best score {min(best, score) if score is not None else best}.")


def run(net, slug: str, name: str, minutes: int, max_variants: int, log, *, handoff=None):
    """Use the same limited, lower-priority worker pool as automatic permutation."""
    return workers.pool().submit(_run, net, slug, name, minutes, max_variants, log, handoff=handoff).result()


def _run(net, slug: str, name: str, minutes: int, max_variants: int, log, *, handoff=None):
    proj, fn = validate(net, slug, name, minutes, max_variants, handoff=handoff)
    fork = net.fork_slug(slug)
    search = Search(net, minutes, max_variants, log, fork=fork)
    search.report()
    state = State()
    agent, _ = net.local_agent(AGENT)
    reason = "Search ended"
    log("Deep permuter is testing this function with the project's compiler and byte comparison.")
    if search.trial_workers > 1:
        log(f"Judging batches with up to {search.trial_workers} compiler workers.")
    try:
        if not search.take_handoff(handoff, agent, fn):
            raise NetworkError("This function was claimed meanwhile. Try again after it is released.")
        try:
            log("Checking the saved C against the current compiler, header and target.")
            best = search.baseline(agent, proj, fn, fork)
            if best > 0:
                current = net._function(name, slug)
                for family, candidate in permuter_local.variants(current["best_source"], name):
                    log.check_cancel()
                    score = search._score(agent, proj, current, candidate, best, fork)
                    if score is not None and score < best:
                        best = score
                        log(f"Local rewrite {family} improved the saved candidate.")
                    if score == 0:
                        break
        finally:
            search._let_go(agent, fn)
        while time.monotonic() < search.deadline and search.tested < search.limit:
            log.check_cancel()
            current = net._function(name, slug)
            if current["status"] == "matched":
                reason = "Matched"
                break
            before = state.variants
            search._permute(agent, proj, current, state)
            if state.variants == before:
                # A turn that couldn't hold the function: another worker has it between this search's turns (the
                # automatic permuter, an agent). Wait for it rather than end the search as if nothing was left. This
                # search's own claim is let go at the end of each turn, so a claim still there is someone else's.
                if net.store.one("SELECT 1 FROM claims WHERE function_id=? AND expires_at>?",
                                 current["id"], time.time()):
                    log("Another worker holds this function; waiting for its claim to end.")
                    time.sleep(min(0.25, max(0, search.deadline - time.monotonic())))
                    continue
                reason = "No further variants available"
                break
        else:
            reason = "Variant limit reached" if search.tested >= search.limit else "Time limit reached"
    except SearchLimit as error:
        reason = str(error)
    finally:
        search.report()
    current = net._function(name, slug)
    if current["status"] == "matched":
        reason = "Matched"
    log(f"{reason}: {search.tested} candidates tested; verified improvements were saved immediately.")
    return {"function": name, "reason": reason, "tested": search.tested,
            "matched": current["status"] == "matched", "best_score": current["best_score"]}
