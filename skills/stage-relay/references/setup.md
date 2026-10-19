# Local setup

Requires Python 3.11+, Git, and Codex CLI. The worker can use any model exposed through a compatible Responses gateway, including CCSwitch; the DSH application is not required. Preserve approval settings. Plan execution modifies the chosen repository using the existing CLI runtime.

From the plugin directory, run:

```powershell
python configure_plugin.py --config "<absolute path to config.local.json>" --projects-root "<allowed project parent>"
```

This writes `.mcp.json` with absolute local paths. It does not install or enable the plugin or contact a model. Regenerate it after moving the directory. Import this directory as a local plugin in a compatible client; otherwise register the `stage-relay` entry from `.mcp.json` as a stdio MCP server. Do not register both approaches in the same client.

`relay_open_panel` starts or reuses the local GUI and returns its URL without contacting models. The calling client opens that URL in its browser panel, or passes open_browser=true for the system browser. The GUI supports explicit planner → worker submission and worker-only execution of an existing plan. `relay_submit` always reuses a caller plan and contacts only the worker. Users can paste an API Key into the GUI; Windows Credential Manager stores it for later discovery and execution under that Windows user. JSON configs and job snapshots hold api_key_ref references only. Advanced environment-variable and existing-gateway authentication remain available. Existing PowerShell-only variables are not automatically inherited by already running desktop clients. Replacing or clearing a saved key can require credential reconfiguration for historical jobs referencing the old key.

The plugin inherits execution settings from the selected configuration. It adds no lower request count, output-token cap, repair limit, request/result size limit or upstream timeout. If worker.max_output_tokens is absent, the relay does not inject that field; if the operator explicitly configured it, the existing setting is respected. In caller-plan mode design questions return to the caller; full relay mode uses the configured planner. Normal CLI compaction remains available. Existing operator time, request, logging and I/O limits are operational controls, not promises of cheaper model reasoning.

Configured plugins, MCP services and project rules remain available to the worker. The relay does not disable tool access to reduce metadata. Request sizes include CLI instructions and tool definitions, so the operator's transport limit must allow those overheads.

For stopped v0.2.0 jobs, an explicit relay_resume upgrades the old reduced limits from the original source configuration (or this server's selected operator configuration for older metadata). It backs up the old snapshot and retains the job's model, endpoint, checks, project baseline and accumulated usage. Running jobs and completed historical records are not rewritten. Newly created jobs carry a policy version and keep their configured snapshot during recovery.

Configure independent acceptance checks in `config.local.json` as IDs mapped to argv arrays. Handoffs cannot add commands to this map. Model tools and approvals still follow the configured CLI environment. The compact result includes CLI-reported input, cached-input and output counts when available; missing accounting, auxiliary calls and prices remain unknown.

To view handoffs with the existing panel:

```powershell
python panel.py --runs-root "runs/plugin" --open
```

`relay_submit` accepts an object shaped like:

```json
{"project":"C:/work/example","task_key":"fix-add-v1","plan":{"goal":"Fix add","files":[{"path":"calc.py","note":"add currently subtracts its arguments"}],"steps":["Return a+b from add"],"decisions":["Keep the existing public interface"],"constraints":["Change only calc.py"]}}
```

Supply the facts needed for correctness; the old 12000-byte handoff budget is removed. Structural schema bounds and a 2 MB MCP message transport guard remain, and oversized messages are rejected explicitly rather than silently truncated. Named escalation refers to generated D1/S1 IDs. Successful changed-file previews may omit detail; use relay_wait with full_result=true before making decisions that require it. Blockers and residual issues are returned in full. Inspect the preserved project diff and validation before treating work as accepted. This version is a local stdio integration, not a remotely hosted web plugin.
Windows startup: launch the panel as a normal Windows user process. A launcher inherited from a restricted host execution environment can fail in `codex exec` before any model request (`failed to initialize in-process app-server client`). Use the client's normal application-launch capability or its reviewed normal-process execution path to open the panel. Preserve the model child's sandbox and approval settings; opening the panel starts no models. Existing healthy panels can be reused.
