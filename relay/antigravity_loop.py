"""Keep the panel and the Antigravity app talking without a manual call.

`antigravity_worker` knows how to deliver one job. This module is the part that
makes it real for a user: it finds the app, keeps its cascade list advertised to
the Runtime, leases work, delivers it, and records what came back. Without it the
worker is only reachable from a test.

The loop is conservative on purpose, and each rule exists because the obvious
alternative lies:

* a session this panel is still executing is advertised as `running` — otherwise
  the Runtime would lease a second job for it and the first would be orphaned;
* leases are taken once per tick and every one of them is handed to a worker; a
  lease taken and dropped strands the job where nothing can reclaim it;
* a turn that outlives the wait window is *observed* until the app says it is
  over, never counted as finished and never released to a second delivery;
* per-session state lives in per-session structures, so one session's outcome can
  never overwrite another's;
* the model is the session's own; a guess would silently switch what runs.
"""

import threading
import time

from .antigravity import AntigravityServer, cascades, read_csrf_token
from .antigravity_worker import run_job

TICK = 3.0
# A cascade may only be treated as a binding target when the app reports one of
# these; anything else means a turn is in flight.
IDLE = "CASCADE_RUN_STATUS_IDLE"
OBSERVE = 5.0


class AntigravityLoop(threading.Thread):
    def __init__(self, server, port, model=None):
        super().__init__(daemon=True, name="AntigravityLoop")
        self.panel = server
        self.port = port
        self.model = model
        self.stop_flag = threading.Event()
        self.state = {"online": False, "cascades": 0, "delivered": 0, "timeouts": 0,
                      "last_error": None, "last_tick": None, "reason": "尚未启动"}
        self._reported = {}
        self._busy = set()
        self._threads = {}
        self._session_reason = {}
        self._lock = threading.Lock()

    def connect(self):
        """Attach to whatever instance is listening; never start one."""
        return AntigravityServer(self.port, read_csrf_token(self.port, secure=False), secure=False)

    def snapshot(self):
        snapshot = dict(self.state)
        snapshot["busy"] = sorted(self._busy)
        snapshot["sessions"] = dict(self._session_reason)
        return snapshot

    def advertise(self, hosted):
        """Tell the Runtime which of the app's cascades really exist.

        A cascade this panel is executing is reported as `running` even when the
        app's own status still reads idle: the Runtime uses that flag to decide
        whether a second job may be leased, and it must reflect what this worker
        is actually doing.
        """
        sessions = []
        for cascade in hosted:
            if not cascade.get("workspace"):
                continue
            running = cascade["id"] in self._busy or cascade["status"] != IDLE
            sessions.append({"id": cascade["id"],
                             "status": "running" if running else "idle",
                             "workspace": cascade["workspace"], "app": "antigravity"})
        # Always poll, even with nothing to advertise: the empty catalogue is what
        # tells the Runtime this host is alive and has no bindable session.
        return self.panel.runtime.poll("antigravity", {
            "app": "antigravity", "source": "antigravity-language-server",
            "sessions": sessions,
        }) or {}

    def tick(self):
        self.state["last_tick"] = time.time()
        try:
            app = self.connect()
            hosted = cascades(app)
        except Exception as error:  # the app may simply not be running
            self.state.update(online=False, reason=f"原生应用未响应：{error}")
            return
        self.state.update(online=True, cascades=len(hosted), reason="已连接原生应用")

        leased = self.advertise(hosted)
        jobs = leased.get("jobs") or []
        for job in jobs:
            self.start_delivery(job)

    def start_delivery(self, job):
        cascade_id = job.get("session")
        if not cascade_id:
            return
        with self._lock:
            if cascade_id in self._busy:
                # Should be unreachable now that busy sessions advertise running;
                # recorded rather than silently dropped so it can be seen.
                self._session_reason[cascade_id] = "已在执行，未重复派发"
                self.state["last_error"] = "运行时为在途会话再次派发了任务"
                return
            self._busy.add(cascade_id)
            self._session_reason[cascade_id] = "已交付，执行中"
        thread = threading.Thread(target=self._deliver, args=(job,),
                                  daemon=True, name=f"AntigravityDeliver-{cascade_id[:8]}")
        with self._lock:
            self._threads[cascade_id] = thread
        thread.start()

    def guard(self, job):
        """Re-check that this job may still be delivered right now."""
        cascade_id = job.get("session")
        projects = {p["id"]: p for p in self.panel.store.snapshot()["projects"]}
        project = projects.get(job.get("project"))
        if not project or project.get("demo"):
            return "项目不存在或为示例项目"
        if project.get("paused"):
            return "项目已暂停"
        if project.get("closed"):
            return "项目已结束"
        bindings = {b["agent"]: b for b in self.panel.runtime.snapshot()["bindings"]
                    if b["project"] == job.get("project")}
        bound = bindings.get("antigravity")
        if not bound or bound["session"] != cascade_id:
            return "会话已换绑，原租约不再有效"
        if bound.get("generation") is not None and job.get("generation") is not None \
                and bound["generation"] != job["generation"]:
            return "绑定轮次已变更，原租约不再有效"
        return None

    def _deliver(self, job):
        cascade_id = job["session"]
        release = False
        accepted = False
        try:
            app = self.connect()
            reported = self._reported.setdefault(cascade_id, {})
            steps_before = app.steps(cascade_id)
            for index, step in enumerate(steps_before):
                reported.setdefault(index, step.get("status"))
            outcome = run_job(app, self.panel.runtime, "antigravity", cascade_id, job,
                              self.model or self.session_model(cascade_id),
                              reported, guard=self.guard, steps_before=len(steps_before))
            accepted = True
            result = (outcome or {}).get("outcome")
            if result == "settled":
                release = True
                with self._lock:
                    self.state["delivered"] += 1
                    self._session_reason[cascade_id] = "已交付并执行完成"
            else:
                # Not finished yet. Keep observing the app until it says the turn
                # is over; releasing the session now would let a second delivery
                # land inside a live turn.
                with self._lock:
                    self.state["timeouts"] += 1
                    self._session_reason[cascade_id] = "执行较久，仍在观察"
                settled_outcome = self.observe_until_settled(app, cascade_id, job, reported)
                release = settled_outcome
                with self._lock:
                    self._session_reason[cascade_id] = (
                        "观察结束，执行完成" if settled_outcome else "观察中断，未确认结束")
        except Exception as error:
            with self._lock:
                self.state["last_error"] = str(error)
                # Only a send that provably never reached the app leaves nothing
                # running. After acceptance any later failure — a lost reply, a
                # read error — leaves the turn in an unknown state, so the session
                # stays busy and is not handed to a second delivery.
                self._session_reason[cascade_id] = (
                    "派发失败，未自动重发" if accepted
                    else "发送失败，未自动重发")
                if accepted:
                    self.state["unknown"] = self.state.get("unknown", 0) + 1
                else:
                    release = True
        finally:
            if release:
                with self._lock:
                    self._busy.discard(cascade_id)
                    self._threads.pop(cascade_id, None)

    def observe_until_settled(self, app, cascade_id, job, reported):
        """Watch a long turn until the app reports it finished.

        Returns True when the turn ended (releasing the session) and False when
        the app went away or this loop is stopping.
        """
        from .antigravity_worker import observe_steps, settled
        while not self.stop_flag.is_set():
            self.panel.runtime.lease(job["id"])
            time.sleep(OBSERVE)
            try:
                steps = app.steps(cascade_id)
            except Exception as error:
                with self._lock:
                    self.state["last_error"] = f"观察中断：{error}"
                return False
            reported = observe_steps(self.panel.runtime, "antigravity", cascade_id,
                                     steps, reported)
            if settled(steps):
                with self._lock:
                    self.state["delivered"] += 1
                return True
        return False

    def session_model(self, cascade_id):
        """The model this session is already on; never a global first entry.

        Returns None when the app does not state it, so the caller reports
        "needs a choice" instead of silently switching what runs.
        """
        try:
            summaries = (self.connect().call("GetAllCascadeTrajectories", {}) or {})
            summary = (summaries.get("trajectorySummaries") or {}).get(cascade_id) or {}
        except Exception:
            return None
        metadata = summary.get("trajectoryMetadata") or {}
        for key in ("model", "generatorModel", "planModel"):
            if metadata.get(key):
                return metadata[key]
        return None

    def run(self):
        while not self.stop_flag.is_set():
            try:
                self.tick()
            except Exception as error:
                self.state["last_error"] = str(error)
            self.stop_flag.wait(TICK)

    def stop(self):
        self.stop_flag.set()