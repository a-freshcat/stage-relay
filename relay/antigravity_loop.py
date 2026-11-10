"""Keep the panel and the Antigravity app talking without a manual call.

`antigravity_worker` knows how to deliver one job. This module is the part that
makes it real for a user: it finds the app, keeps its cascade list advertised to
the Runtime, leases work, delivers it, and records what came back. Without it the
worker is only reachable from a test.

The loop is conservative on purpose:

* it advertises first, then leases exactly once per tick — a lease taken and
  dropped would strand a job in `leased` where no later tick can reclaim it;
* a turn that is still running is not delivered twice;
* an app that stops answering is reported as offline instead of being retried
  forever;
* every failure is recorded, never swallowed into a "success" receipt.
"""

import threading
import time

from .antigravity import AntigravityServer, cascades, read_csrf_token
from .antigravity_worker import run_job

TICK = 3.0
# A cascade may only be treated as a binding target when the app reports one of
# these; anything else means a turn is in flight.
IDLE = "CASCADE_RUN_STATUS_IDLE"


class AntigravityLoop(threading.Thread):
    def __init__(self, server, port, model=None):
        super().__init__(daemon=True, name="AntigravityLoop")
        self.panel = server
        self.port = port
        self.model = model
        self.stop_flag = threading.Event()
        self.state = {"online": False, "cascades": 0, "delivered": 0,
                      "last_error": None, "last_tick": None, "reason": "尚未启动"}
        self._reported = {}
        self._busy = set()

    def connect(self):
        """Attach to whatever instance is listening; never start one."""
        return AntigravityServer(self.port, read_csrf_token(self.port, secure=False), secure=False)

    def snapshot(self):
        return dict(self.state)

    def advertise(self, hosted):
        """Tell the Runtime which of the app's cascades really exist.

        Only cascades with a workspace can be bound, so only those are published.
        """
        sessions = [{"id": c["id"],
                     "status": "idle" if c["status"] == IDLE else "running",
                     "workspace": c["workspace"], "app": "antigravity"}
                    for c in hosted if c.get("workspace")]
        if not sessions:
            return []
        return self.panel.runtime.poll("antigravity", {
            "app": "antigravity", "source": "antigravity-language-server",
            "sessions": sessions,
        })

    def tick(self):
        self.state["last_tick"] = time.time()
        try:
            app = self.connect()
            hosted = cascades(app)
        except Exception as error:  # the app may simply not be running
            self.state.update(online=False, reason=f"原生应用未响应：{error}")
            return
        self.state.update(online=True, cascades=len(hosted), reason="已连接原生应用")

        # One poll per tick: it advertises the catalogue and leases whatever is
        # due. Leasing here and dropping it would leave the job unlendable.
        leased = self.advertise(hosted) or {}
        by_session = {j.get("session"): j for j in leased.get("jobs") or []}
        if not by_session:
            return
        for cascade_id, job in by_session.items():
            self.deliver(app, cascade_id, job, hosted)

    def deliver(self, app, cascade_id, job, hosted):
        if cascade_id in self._busy:
            return
        match = next((c for c in hosted if c["id"] == cascade_id), None)
        if not match:
            self.state["reason"] = "绑定的会话不在应用的会话列表中"
            return
        self._busy.add(cascade_id)
        try:
            reported = self._reported.setdefault(cascade_id, set())
            reported.update(range(len(app.steps(cascade_id))))
            run_job(app, self.panel.runtime, "antigravity", cascade_id, job,
                    self.model or self._default_model(app), reported)
            self.state["delivered"] += 1
            self.state["reason"] = "已交付并在执行"
        except Exception as error:
            self.state["last_error"] = str(error)
        finally:
            self._busy.discard(cascade_id)

    def _default_model(self, app):
        """Use the app's own selection; never invent a model globally."""
        try:
            configs = app.models()
        except Exception:
            return None
        return configs[0]["model"] if configs else None

    def run(self):
        while not self.stop_flag.is_set():
            try:
                self.tick()
            except Exception as error:
                self.state["last_error"] = str(error)
            self.stop_flag.wait(TICK)

    def stop(self):
        self.stop_flag.set()