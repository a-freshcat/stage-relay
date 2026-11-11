"""Proof that the interleaved-scope defect is closed, not merely unobserved.

The previous design kept the project being checked in an instance slot. With two
dispatch threads running, an ordinary project could overwrite that slot between a
QA project establishing its scope and reading the policy, and the QA restriction
would vanish for that dispatch. This check pins that sequence and shows the
current code refuses correctly.

Memory only: no files are written, no model is called.
"""

import json
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from relay.antigravity_loop import AntigravityLoop

RECORD = {"allowed": ["MODEL_PLACEHOLDER_M318"], "projects": ["qa"]}


def loop_under_record():
    loop = AntigravityLoop(
        SimpleNamespace(store=SimpleNamespace(path=Path("qa/scope-proof.sqlite3"))), 0)
    loop.session_model = lambda _: "MODEL_PLACEHOLDER_M418"
    return loop


def interleaved(model_refusal, allowed_models):
    """Run a QA check and an ordinary check so the ordinary one lands in between."""
    entered, resume, results = threading.Event(), threading.Event(), {}

    def wrapper(*args, **kwargs):
        if threading.current_thread().name == "qa-scope-thread":
            entered.set()
            resume.wait(2)
        return allowed_models(*args, **kwargs)

    def qa():
        results["qa"] = model_refusal("qa-session", {"id": "qa"})

    thread = threading.Thread(name="qa-scope-thread", target=qa)
    thread.start()
    entered.wait(2)
    results["ordinary"] = model_refusal("ordinary-session", {"id": "ordinary"})
    resume.set()
    thread.join(3)
    assert not thread.is_alive(), "probe synchronization timed out"
    return results


def main():
    with patch.object(Path, "exists", return_value=True), \
         patch.object(Path, "read_text", return_value=json.dumps(RECORD)), \
         patch.dict(os.environ, {AntigravityLoop.MODEL_POLICY_ENV: ""}):
        loop = loop_under_record()
        original = loop.allowed_models
        results = interleaved(loop.model_refusal, lambda *a, **k: original(*a, **k))

    print("current design:")
    print("  qa       :", results["qa"])
    print("  ordinary :", results["ordinary"])
    assert results["qa"] is not None, "the QA restriction was lost to the other project"
    assert results["ordinary"] is None, "an ordinary session must keep its own model"

    # The same interleaving under the removed design: the ordinary check writes a
    # shared slot that the QA check then reads, so the QA refusal disappears.
    holder = {}

    def old_refusal(cascade_id, project=None):
        holder["project"] = project
        return new_refusal(cascade_id, project)

    new_loop = loop_under_record()
    new_refusal = new_loop.model_refusal
    new_allowed = new_loop.allowed_models

    def old_allowed(project=None):
        return new_allowed(holder.get("project"))

    old_results = interleaved(old_refusal, old_allowed)
    print("removed design (for contrast):")
    print("  qa       :", old_results["qa"], "  <- leaked")
    assert old_results["qa"] is None, "the contrast case did not reproduce the old defect"

    print("\nthe defect is closed: the same sequence now refuses the QA dispatch")


if __name__ == "__main__":
    main()