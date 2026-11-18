"""The same bounded project jobs for desktop, managed helpers, and manual MCP clients."""

from __future__ import annotations

from ..network import NetworkError
from . import deep_permuter, workers

ENABLED = "agent_background_jobs"
ACTIVE = frozenset({"queued", "running"})


class ProjectJobs:
    def __init__(self, net, jobs):
        self.net, self.jobs = net, jobs
        self.closing = False

    def _active(self) -> list[dict]:
        return [job for job in self.jobs.jobs.values() if job.get("project_job") and job["state"] in ACTIVE]

    def _start_ready(self) -> None:
        """Respect the project and shared worker limits before starting any time budget."""
        if self.closing:
            return
        active = self._active()
        running = [job for job in active if job["state"] == "running"]
        for job in active:
            if job["state"] != "queued" or any(r["slug"] == job["slug"] for r in running):
                continue
            if len(running) >= deep_permuter.max_searches():
                break
            if running and (job["high_performance"] or any(r["high_performance"] for r in running)):
                break
            self.jobs.launch_queued(job["id"])
            running.append(job)

    def _finished(self) -> None:
        with self.jobs._lock:
            self._start_ready()

    def submit(self, slug: str, kind: str, fn, *, key: str = "", function: str = "", agent=None,
               high_performance: bool = False) -> str:
        """Join an identical active job or queue one request behind the project's running job."""
        if slug in self.net.links:
            raise NetworkError("Start this job on the project host.")
        if agent is not None and not self.net.cfg(self.net.project_row(slug)).get(ENABLED):
            raise NetworkError("Ask the project owner to enable background jobs in the desktop settings.")

        def work(log):
            if agent is not None and not self.net.cfg(self.net.project_row(slug)).get(ENABLED):
                raise NetworkError("The owner turned off background jobs while this job was queued.")
            return fn(log)

        with self.jobs._lock:
            if self.closing:
                raise NetworkError("The app is closing. Start this job after reopening the project.")
            active = [job for job in self._active() if job["slug"] == slug]
            for job in active:
                if (job["kind"], job["job_key"]) == (kind, key):
                    return job["id"]
            if any(job["state"] == "queued" for job in active):
                raise NetworkError("This project already has a queued job. Wait or cancel it before starting another.")
            jid = self.jobs.start(kind, work, defer=True, finished=self._finished, cancelled="The job was cancelled.",
                                  metadata={"project_job": True, "slug": slug, "function": function, "job_key": key,
                                            "initiator": agent.id if agent else None, "high_performance": high_performance})
            self._start_ready()
        return jid

    def shutdown(self) -> None:
        """Keep saved progress while ending jobs before the app flushes project files."""
        with self.jobs._lock:
            self.closing = True
            active = [job["id"] for job in self._active()]
        for jid in active:
            self.jobs.cancel(jid)

    def deep_search(self, slug: str, name: str, minutes: int, variants: int, agent=None) -> str:
        """Look up the function by row ID again when its queued turn starts."""
        deep_permuter.validate_budget(minutes, variants)
        proj = self.net.project_row(slug)
        function = self.net._function(name, slug)
        fid = function["id"]
        high_performance = deep_permuter.trial_width(self.net.fork_slug(slug)) == deep_permuter.HIGH_PERFORMANCE_WORKERS

        def work(log):
            latest = self.net.store.one("SELECT name FROM functions WHERE id=? AND project_id=?", fid, proj["id"])
            if not latest:
                raise NetworkError("The selected function changed while the search was queued. Choose it again.")
            if agent is None:
                return deep_permuter.run(self.net, slug, latest["name"], minutes, variants, log)
            return deep_permuter.run(self.net, slug, latest["name"], minutes, variants, log, handoff=agent)

        return self.submit(slug, "deep-permuter", work, key=str(fid), function=function["name"],
                           agent=agent, high_performance=high_performance)

    def verify_assembly(self, slug: str, agent=None) -> str:
        def current():
            proj = self.net.project_row(slug)
            verify = self.net.console(proj).verify_assembly
            if not verify or not self.net.is_rom(proj) or slug in self.net.rooms or slug in self.net.links:
                raise NetworkError("Choose a standalone ROM project with an SDK assembly verifier.")
            return proj, verify

        def work(log):
            proj, verify = current()
            return workers.pool().submit(verify, self.net, proj, log).result()

        current()
        return self.submit(slug, "Verify SDK assembly", work, agent=agent)

    def status(self, slug: str, jid: str, agent=None, *, cancel: bool = False) -> dict:
        if type(cancel) is not bool or not isinstance(jid, str):
            raise NetworkError("Give a job ID and say whether to cancel it.")
        with self.jobs._lock:
            job = self.jobs.jobs.get(jid)
            if not job or not job.get("project_job") or job["slug"] != slug:
                raise NetworkError("Choose a job from this project.")
            if cancel and agent is not None and job["initiator"] != agent.id:
                raise NetworkError("Only the project owner or the client that started this job can cancel it.")
        if cancel and not self.jobs.cancel(jid):
            raise NetworkError("That job already finished.")
        with self.jobs._lock:
            return {key: job[key] for key in ("id", "kind", "state", "function", "progress", "result", "error")}
