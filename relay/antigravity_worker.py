"""Run the relay's own delivery loop against the installed Antigravity app.

The Runtime is application-agnostic: it leases a job for a bound session, and the
application reports what happened. Codex and DSH each have a harness that speaks
that protocol. This is the same harness for Antigravity, with one deliberate
restriction: it can only bind sessions that already exist in the app.

Why not create sessions: a freshly started cascade rejects its first message with
`400 GenerateContentRequest.contents is not specified` on this install, which is
an open defect tracked separately. Reporting "created" for something that cannot
run would be a false capability claim, so creation is refused here.

Three rules this module follows, each because the obvious alternative lies:

1. `delivered` means the app accepted the message. It is written after `send`
   returns, never before, so a rejected send cannot be recorded as handed over.
2. A step that is seen while RUNNING and later DONE is the same step becoming
   finished, not a repeat. Progress is keyed by index *and* status, so the
   completion is reported instead of being swallowed by the earlier sighting.
3. A turn is finished only when the tail says so. Running out of the wait window
   is not completion: the job stays open, the session stays busy, and no new work
   is delivered into a turn that may still be executing.

Nothing here decides what a task means. It reports the app's own steps and lets
`Runtime.observe` record them.
"""

import time

from .antigravity import OutcomeUnknown, activity_of
from .domain import Invalid

# How often to ask the app for new steps while a turn is running.
POLL_SECONDS = 2.0
# How long the tail may stay unchanged before a turn counts as settled. A tool
# that runs for a while produces no new steps, so this stays well above the
# longest observed single-tool pause.
QUIET_SECONDS = 20.0
# Absolute ceiling for one delivery. Hitting it is reported as a timeout, never
# as success.
MAX_TURN_SECONDS = 1800.0

RUNNING = "CORTEX_STEP_STATUS_RUNNING"
DONE = "CORTEX_STEP_STATUS_DONE"


def sessions(host, cascade_ids):
    """Shape the app's cascades the way the relay expects advertised sessions.

    `workspace` is what the binding check compares, so it has to be the real
    directory the cascade was opened on.
    """
    return [{"id": cascade, "status": "idle", "workspace": host, "app": "antigravity"}
            for cascade in cascade_ids]


def observe_steps(runtime, agent_id, cascade, steps, reported):
    """Push every step whose progress is newer than what was already reported.

    `reported` maps a step index to the status last sent. A step that moves from
    RUNNING to DONE is therefore sent twice — once when it starts and once when
    it finishes — which is what lets a caller see a tool complete instead of
    assuming it from the index alone.
    """
    fresh = []
    for index, step in enumerate(steps):
        status = step.get("status")
        if reported.get(index) == status:
            continue
        fresh.append((index, step, status))
    for index, step, status in fresh:
        for event in activity_of([step], seen=set()):
            runtime.observe(agent_id, {
                "session": cascade,
                "seq": index + 1,
                "event": {"type": event["type"], "data": event["data"]},
            })
        reported[index] = status
    return reported


def settled(steps):
    """True only when the newest step says the turn is over.

    Returning False when unsure is deliberate: polling again costs one request,
    while calling a running turn settled stops the relay from reporting what the
    app produced and lets the next delivery land inside a live turn.
    """
    if not steps:
        return False
    tail = steps[-1]
    kind = tail.get("type")
    status = tail.get("status")
    if kind == "CORTEX_STEP_TYPE_PLANNER_RESPONSE":
        return status == DONE
    if kind == "CORTEX_STEP_TYPE_ERROR_MESSAGE":
        return True
    # A tool or a view step in flight, or a user step that has not run: not over.
    return False


def run_job(server, runtime, agent_id, cascade, job, plan_model, reported, guard=None,
            steps_before=None, on_accepted=None):
    """Deliver one leased job to the app and report what it produced.

    `on_accepted` is called the moment the app takes the message. A caller must
    use it to record that the turn may be running, because the polling that
    follows can fail while the app is still working.

    `steps_before` is the step count seen just before sending. Until a step
    appears beyond it, the trajectory still shows the previous turn, and treating
    that tail as this turn's result would report success for work not yet done.

    Returns a dict describing the outcome, so the caller never has to infer
    success from the absence of an exception.
    """
    prompt = job.get("prompt") or ""
    if not prompt:
        # Nothing was sent, so nothing may be marked delivered.
        runtime.receipt(agent_id, {"id": job["id"], "status": "failed",
                                   "detail": "派发内容为空，未发送。"})
        raise Invalid("派发内容为空，未发送。")
    if guard is not None:
        # The lease may have gone stale while this worker was waiting: the user
        # could have rebound, paused or closed the project. Sending then would
        # deliver into a session that no longer answers for this work.
        problem = guard(job)
        if problem:
            runtime.receipt(agent_id, {"id": job["id"], "status": "failed",
                                       "detail": problem})
            raise Invalid(problem)
    try:
        server.send(cascade, prompt, plan_model)
    except OutcomeUnknown as error:
        # The bytes may have reached the app. Saying "failed" would assert
        # something we cannot know, so the job is left uncertain and never
        # retried here.
        runtime.receipt(agent_id, {"id": job["id"], "status": "uncertain",
                                   "detail": f"发送结果未知：{error}"})
        raise
    except Exception as error:
        # A refusal, an invalid argument, or a local precondition: the app did not
        # take the message.
        runtime.receipt(agent_id, {"id": job["id"], "status": "failed",
                                   "detail": f"发送未成功：{error}"})
        raise
    # Accepted by the app; from here the turn may run.
    runtime.receipt(agent_id, {"id": job["id"], "status": "delivered",
                               "detail": "Antigravity 已接受消息"})
    if on_accepted is not None:
        # Tell the caller at the send boundary, not on return. Everything after
        # this point can still raise — a lost reply while reading steps, for one
        # — and a caller that only learns on return would treat that as "nothing
        # was sent" and give the session to a second delivery.
        on_accepted()

    # The tail may still be the previous turn's message. A turn is only this
    # one's once a step appears that the earlier baseline did not have, so the
    # caller counts nothing as "finished" until then.
    # Accept either the step list or its count: the count is all that is needed,
    # and a caller that already has the list should not have to re-count it.
    if steps_before is None:
        step_baseline = None
    elif isinstance(steps_before, int):
        step_baseline = steps_before
    else:
        step_baseline = len(steps_before)
    deadline = time.time() + MAX_TURN_SECONDS
    steps = []
    quiet_since = time.time()
    last_signature = None
    while time.time() < deadline:
        runtime.lease(job["id"])
        time.sleep(POLL_SECONDS)
        try:
            steps = server.steps(cascade)
        except OutcomeUnknown:
            # Accepted already, so a lost reply means this turn's state is
            # unknown, not that it ended.
            return {"outcome": "unknown", "steps": len(steps), "settled": False}
        reported = observe_steps(runtime, agent_id, cascade, steps, reported)
        if step_baseline is not None and len(steps) <= step_baseline:
            # No new step yet: whatever the tail says belongs to the old turn.
            quiet_since = time.time()
            continue
        signature = (len(steps), steps[-1].get("status") if steps else None)
        if signature != last_signature:
            last_signature = signature
            quiet_since = time.time()
            if settled(steps):
                return {"outcome": "settled", "steps": len(steps)}
            continue
        if settled(steps) and time.time() - quiet_since > QUIET_SECONDS:
            return {"outcome": "settled", "steps": len(steps)}
    # The window closed with the turn possibly still running. Say so; a caller
    # that treats this as done would deliver inside a live turn.
    return {"outcome": "timeout", "steps": len(steps), "settled": False}


def adopt_existing(runtime, agent_id, app_sessions):
    """Register the app's live cascades so a project can bind one.

    This is the only supported path today: `create_session` stays refused.
    """
    runtime.poll(agent_id, {"app": "antigravity", "source": "antigravity-language-server",
                            "sessions": app_sessions})
    return app_sessions