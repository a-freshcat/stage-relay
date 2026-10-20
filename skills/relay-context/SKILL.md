---
name: relay-context
description: Save an execution checkpoint for a fresh Stage Relay context, or pause and ask the calling Codex for specific help. Use when context is growing, attempts repeat without progress, or the worker lacks a capability needed for the task.
---

Use only inside a Stage Relay worker that advertises `relay_control` support. Otherwise report that this handoff requires Stage Relay; do not claim to compact an unrelated conversation.

Keep the original task, plan, constraints, files and test evidence. Carry forward still-relevant caller advice and unresolved questions. Summarize observable working state, not hidden reasoning. Reference long logs by path instead of repeating them. Never delete history, reset files, lower model quality, truncate requirements or clear cumulative usage.

When a fresh context would help, finish the current tool operation and return one JSON object instead of the normal result:

```json
{"relay_control":"compact","summary":"Current state and key decisions","completed":["Completed work and relevant file paths"],"next_steps":["Concrete remaining work"],"evidence":["Checks actually run and their results, or log paths"],"question":""}
```

Stage Relay validates and saves the checkpoint, keeps all artifacts and accounting, then starts the same execution model in a fresh CLI process with the original task/plan plus this summary. This resets that worker's conversation history; it is not a native `/compact` call and does not reduce already-spent tokens. Do not do it after every step. If no work remains, return the actual task result instead.

When stuck, unsure about a consequential decision, or missing a required browser capability, use `"relay_control":"help"` and write a specific `question` in the same shape. Include what you tried, the relevant evidence and what the caller must decide or verify. Stop there. Do not keep exploring while awaiting advice or contact another model yourself. The current Codex receives the question through `relay_wait`; an idle chat requires the user to hand it back from the panel. After the versioned answer arrives, the same worker resumes in a fresh context.

Use the task's language. A missing optional visual check alone does not require help: deliver with a concise, concrete limitation. In this Windows worker sandbox, do not launch shell Edge/Chrome to work around missing host browser access; that route has produced access-denied browser crashes. Request caller inspection when it is actually required.
