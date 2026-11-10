"""Run the relay's own delivery loop against the installed Antigravity app.

The Runtime is application-agnostic: it leases a job for a bound session, and the
application reports what happened. Codex and DSH each have a harness that speaks
that protocol. This is the same harness for Antigravity, with one deliberate
restriction: it can only bind sessions that already exist in the app.

Why not create sessions: a freshly started cascade rejects its first message with
`400 GenerateContentRequest.contents is not specified` on this install, which is
an open defect tracked separately. Reporting "created" for something that cannot
run would be a false capability claim, so creation is refused here and the panel
is expected to say so.

Nothing in this module decides what a task means. It reports the app's own steps
and lets `Runtime.observe` record them.
"""

import time

from .antigravity import AntigravityServer, activity_of
from .domain import Invalid

# How often to ask the app for new steps while a turn is running, and how long a
# turn may stay silent before the relay stops waiting on it. The app is slow
# (a tool call takes seconds), so the idle window is generous.
POLL_SECONDS = 2.0
IDLE_WINDOW = 180.0


def sessions(host, cascade_ids):
    """Shape the app's cascades the way the relay expects advertised sessions.

    `workspace` is what the binding check compares, so it has to be the real
    directory the cascade was opened on.
    """
    return [{"id": cascade, "status": "idle", "workspace": host, "app": "antigravity"}
            for cascade in cascade_ids]


def observe_steps(runtime, agent_id, cascade, steps, reported):
    """Push only the steps the relay has not seen yet, in order."""
    fresh = activity_of(steps, seen=reported)
    for event in fresh:
        runtime.observe(agent_id, {
            "session": cascade,
            "seq": event["index"] + 1,
            "event": {"type": event["type"], "data": event["data"]},
        })
        reported.add(event["index"])
    return reported


def run_job(server, runtime, agent_id, cascade, job, plan_model, reported):
    """Deliver one leased job to the app and report what it produced.

    Returns the final step list so the caller can keep its view current.
    """
    prompt = job.get("prompt") or ""
    if not prompt:
        raise Invalid("派发内容为空，未发送。")
    runtime.receipt(agent_id, {"id": job["id"], "status": "delivered",
                               "detail": "已交给 Antigravity 语言服务器"})
    server.send(cascade, prompt, plan_model)
    deadline = time.time() + IDLE_WINDOW
    last_count = -1
    quiet_since = time.time()
    steps = []
    while time.time() < deadline:
        time.sleep(POLL_SECONDS)
        steps = server.steps(cascade)
        reported = observe_steps(runtime, agent_id, cascade, steps, reported)
        if len(steps) != last_count:
            last_count = len(steps)
            quiet_since = time.time()
            continue
        # The app stopped producing steps. Treat a settled tail as done; a
        # pending tool keeps the loop alive until the idle window runs out.
        if steps and steps[-1].get("type") == "CORTEX_STEP_TYPE_PLANNER_RESPONSE" \
                and quiet_since and time.time() - quiet_since > 8:
            break
    return steps


def adopt_existing(runtime, agent_id, app_sessions):
    """Register the app's live cascades so a project can bind one.

    This is the only supported path today: `create_session` stays refused.
    """
    runtime.poll(agent_id, {"app": "antigravity", "source": "antigravity-language-server",
                            "sessions": app_sessions})
    return app_sessions