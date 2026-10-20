"""Local stdio MCP tools. Stdout contains JSON-RPC messages only; no paid startup."""
import argparse
import json
import sys
from pathlib import Path

from relaylib import __version__
from relaylib.contracts import RelayError, fail, validate
from relaylib.plugin import TOOLS, JobStore, execute_job
from relaylib.panel_runtime import ensure_shortcut, launch_hint

PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MAX_MESSAGE = 2_000_000  # Transport safety, not a model context/token budget.


def serve(store, reader, writer):
    initialized, ready = False, False
    tool_map = {t["name"]: t for t in TOOLS}
    while True:
        raw = reader.readline(MAX_MESSAGE + 1)
        if not raw:
            return
        if len(raw) > MAX_MESSAGE:
            # Stop rather than treating a suffix as another request.
            writer.write(b'{"jsonrpc":"2.0","id":null,"error":{"code":-32700,"message":"Message too large"}}\n')
            writer.flush()
            return
        identity = None
        try:
            request = json.loads(raw)
            if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
                raise ValueError()
            method = request["method"]
            if "id" not in request:
                if method == "notifications/initialized" and initialized:
                    ready = True
                continue
            identity = request["id"]
            if type(identity) not in (str, int):
                raise ValueError()
            params = request.get("params", {})
            if not isinstance(params, dict):
                raise ValueError()
            if method == "initialize":
                version = params.get("protocolVersion")
                result = {"protocolVersion": version if version in PROTOCOLS else PROTOCOLS[0],
                          "capabilities": {"tools": {}}, "serverInfo": {"name": "stage-relay", "version": __version__},
                          "instructions": "On explicit plugin invocation, call relay_open_panel and display its URL in the client's browser panel. Opening never starts models. Reuse caller plans when delegating work."}
                initialized = True
            elif method == "ping":
                result = {}
            elif not ready:
                result = None
                response = {"error": {"code": -32600, "message": "Initialize the MCP session first"}}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                name, args = params.get("name"), params.get("arguments", {})
                if name not in tool_map:
                    response = {"error": {"code": -32602, "message": "Unknown tool"}}
                    result = None
                else:
                    try:
                        validate(args, tool_map[name]["inputSchema"], "arguments")
                        if name == "relay_open_panel":
                            value = store.open_panel(**args)
                        elif name == "relay_prepare":
                            value = store.prepare(**args)
                        elif name == "relay_submit":
                            value = store.submit(**args)
                        elif name == "relay_wait":
                            value = store.wait(**args)
                        elif name == "relay_record_review":
                            value = store.record_review(**args)
                        elif name == "relay_correct":
                            value = store.correct_result(**args)
                        elif name == "relay_pause_corrections":
                            value = store.pause_corrections(**args)
                        else:
                            value = store.resume(**args)
                        # One compact text representation; avoid duplicating it in structuredContent.
                        result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False, separators=(",", ":"))}]}
                    except (RelayError, OSError, ValueError):
                        # Never include provider responses, credentials or arbitrary exception strings.
                        exc = sys.exc_info()[1]
                        result = {"content": [{"type": "text", "text": json.dumps({"error": getattr(exc, "code", "local_error")})}], "isError": True}
            else:
                result = None
                response = {"error": {"code": -32601, "message": "Method not found"}}
            if result is not None:
                response = {"result": result}
        except (ValueError, TypeError):
            response = {"error": {"code": -32700, "message": "Invalid JSON-RPC message"}}
        writer.write((json.dumps({"jsonrpc": "2.0", "id": identity, **response}, ensure_ascii=False, separators=(",", ":")) + "\n").encode())
        writer.flush()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-job", help=argparse.SUPPRESS)
    parser.add_argument("--open-panel", action="store_true", help="Print a reusable GUI URL without starting models")
    parser.add_argument("--open-browser", action="store_true", help="With --open-panel, also open the URL in the system browser")
    parser.add_argument("--prepare-plan", help="UTF-8 JSON file with project and plan; save a caller draft without model calls")
    parser.add_argument("--open-task", help="UTF-8 JSON with user_request and optional project; show the new task before planning")
    parser.add_argument("--record-review", help="UTF-8 JSON with job_id, result_revision and the caller-planner's review")
    parser.add_argument("--wait-job", help="Read/wait for a job using this installed version; no model call")
    parser.add_argument("--seconds", type=int, default=50, help="Local wait duration, 0..50 seconds")
    parser.add_argument("--full-result", action="store_true", help="Include complete saved execution and correction evidence")
    parser.add_argument("--config")
    parser.add_argument("--runs-root", default=str(Path(__file__).parent / "runs" / "plugin"))
    parser.add_argument("--projects-root", action="append")
    args = parser.parse_args()
    if args.execute_job:
        execute_job(args.execute_job)
        return
    if not args.config or not args.projects_root:
        parser.error("--config and --projects-root are required; run configure_plugin.py first")
    store = JobStore(args.config, args.runs_root, args.projects_root)
    if args.open_task:
        from relaylib.contracts import load_json
        value = load_json(args.open_task, MAX_MESSAGE)
        validate(value, next(t["inputSchema"] for t in TOOLS if t["name"] == "relay_open_panel"))
        print(json.dumps(store.open_panel(**value), ensure_ascii=False))
        return
    if args.wait_job:
        print(json.dumps(store.wait(args.wait_job, args.seconds, full_result=args.full_result), ensure_ascii=False))
        return
    if args.record_review:
        from relaylib.contracts import load_json
        value = load_json(args.record_review, MAX_MESSAGE)
        validate(value, next(t["inputSchema"] for t in TOOLS if t["name"] == "relay_record_review"))
        print(json.dumps(store.record_review(**value), ensure_ascii=False))
        return
    if args.prepare_plan:
        from relaylib.contracts import load_json
        value = load_json(args.prepare_plan, MAX_MESSAGE)
        validate(value, next(t["inputSchema"] for t in TOOLS if t["name"] == "relay_prepare"))
        print(json.dumps(store.prepare(**value), ensure_ascii=False))
        return
    if args.open_panel:
        ensure_shortcut(store)
        value = store.open_panel(open_browser=args.open_browser)
        value["shortcut_detail"] = launch_hint(store)
        print(json.dumps(value, ensure_ascii=False))
        return
    serve(store, sys.stdin.buffer, sys.stdout.buffer)


if __name__ == "__main__":
    main()
