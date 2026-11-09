"""Loopback-only panel server and authenticated bridge endpoint."""

from __future__ import annotations

import argparse
import base64
import json
import logging
import mimetypes
import os
import secrets
import socket
import sys
import threading
import webbrowser
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import __version__, rules
from .domain import Invalid, Store, export_markdown
from .cleanup import Cleanup, scratch_for
from .connections import Connections
from .native_apps import open_application
from .runtime import Runtime
from .codex import CodexManager, NativeError

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
USER_ACTIONS = {"project_create", "workspace_set", "demo_create", "demo_reset", "agent_save", "assign", "task_create", "task_edit", "dispatch", "pause", "resolve", "accept", "summary_request", "invite", "issue_create", "message", "project_message", "project_close", "record_archive", "record_restore", "project_forget", "project_restore", "mentions_read", "report_request", "report_fail"}
SETTINGS_KINDS = {
    # Only these app-level leftovers may ever be listed for cleanup, and only
    # after the referenced project is gone. Sessions and credentials are not here.
    "orphan-upload": "未被任何项目消息引用的上传附件",
    "orphan-plan": "已过期或已执行的清理计划文件",
    "scratch": "已删除项目的临时目录",
}

# Actions a member may take through its own bridge. `summary_request` belongs
# here because the progress supervisor is a member, and the store still checks
# the role: user, or the current planner. Without it the MCP tool was
# unreachable even though everything behind it was implemented.
AGENT_ACTIONS = {"connect", "report", "review", "issue_create", "message", "project_message",
                 "summary_submit", "summary_request",
                 # The reporter writes the follow-up report, so these are member
                 # actions by nature; the role check lives in the store.
                 "report_submit", "report_fail"}


class PanelServer(ThreadingHTTPServer):
    daemon_threads = True
    # Windows lets a second listener join an address that the first bound with
    # SO_REUSEADDR, so two panels sharing one port silently split traffic and
    # each answers with its own data directory. Exclusive binding makes the
    # second launch fail loudly instead of impersonating the first.
    allow_reuse_address = False

    def server_bind(self):
        if sys.platform == "win32":
            exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
            if exclusive is not None:
                self.socket.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        super().server_bind()

    def __init__(self, address, store, connection_home=None):
        self.store = store
        self.runtime = Runtime(store)
        self.session_secret = secrets.token_urlsafe(32)
        self.credentials_lock = threading.Lock()
        self.credentials_path = store.path.parent / "bridge-credentials.json"
        self.credentials = {}
        if self.credentials_path.exists():
            self.credentials = json.loads(self.credentials_path.read_text(encoding="utf-8"))
        super().__init__(address, Handler)
        self.connections = Connections(store.path.parent, self.server_port, home=connection_home)
        self.refresh_credentials()
        self.codex=CodexManager(self,enabled=connection_home is None)
        # Before anything can read the catalogue: an authorized member must not
        # keep loading an older bridge just because that path was recorded once.
        self.bridge_refresh = self.repoint_bridges()
        self.register_scratch()
        threading.Thread(target=self.codex.restore,daemon=True,name='RestoreNativeCodex').start()

    def repoint_bridges(self):
        """Make already-authorized members load this package's bridge.

        The bridge process is what advertises the tool list to the application, so
        an entry left pointing at an older package keeps serving an older tool
        catalogue — the user reinstalls, reloads, and still sees `relay_submit_
        summary` only. Each member is re-pointed once per start, with a backup,
        and only when its recorded consent still names this port and store. Any
        failure is reported, never fatal: the panel must start either way.
        """
        moved = []
        for agent in self.store.snapshot()["agents"]:
            if not (agent.get("setup") or {}).get("configured"):
                continue
            try:
                result = self.connections.refresh(agent)
            except (Invalid, OSError, ValueError) as exc:
                moved.append({"agent": agent["id"], "repointed": False, "error": str(exc)})
                continue
            if result:
                moved.append({"agent": agent["id"], "repointed": True,
                              "path": result.get("path"), "backup": result.get("backup")})
        return moved

    def register_scratch(self):
        """Create scratch only where the ownership marker proves it is ours.

        Existing projects without a markered scratch directory are unknown
        leftovers: scratch_for() refuses to adopt them, so nothing is claimed
        just to make the cleanup preview non-empty.
        """
        for project in self.store.snapshot()["projects"]:
            if project.get("demo") or project.get("closed"):
                continue
            try:
                scratch_for(self.store.path.parent, project["id"])
            except OSError:
                # A scratch registry is housekeeping; never block startup on it.
                continue

    def server_close(self):
        if hasattr(self,'codex'):self.codex.close()
        super().server_close()

    def cleanup(self):
        return Cleanup(self.store.path.parent)

    def recycled_projects(self):
        """Projects the user moved to the recycle area, read from their recovery files.

        The workspace is never touched, so this only reports what can be moved
        back into the list and how large each record is.
        """
        folder = self.store.archive_dir
        results = []
        if not folder.is_dir():
            return results
        for path in sorted(folder.glob("project-*.json")):
            try:
                project = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            results.append(dict(
                id=project.get("id"), title=project.get("title"),
                deleted=True, closed=bool(project.get("closed")),
                workspace=project.get("workspace"),
                tasks=len(project.get("tasks", [])), issues=len(project.get("issues", [])),
                chat=len(project.get("chat", [])), size=path.stat().st_size,
                recover=str(path), closed_at=project.get("closed_at"),
            ))
        return results

    def refresh_credentials(self):
        with self.credentials_lock:
            changed = False
            for agent in self.store.snapshot()["agents"]:
                if agent["id"] not in self.credentials:
                    self.credentials[agent["id"]] = secrets.token_urlsafe(36)
                    changed = True
            if changed or not self.credentials_path.exists():
                temp = self.credentials_path.with_suffix(".tmp")
                temp.write_text(json.dumps(self.credentials), encoding="utf-8")
                temp.replace(self.credentials_path)


class Handler(BaseHTTPRequestHandler):
    server: PanelServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        # Never log request bodies, cookies, tokens, or query strings.
        sys.stderr.write(f"[panel] {args[1] if len(args) > 1 else ''}\n")

    def send_bytes(self, body, mime="application/json; charset=utf-8", code=200, headers=None):
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if self.close_connection:
            self.send_header("Connection", "close")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def json(self, value, code=200):
        self.send_bytes(json.dumps(value, ensure_ascii=False).encode("utf-8"), code=code)

    def local_request(self):
        port = self.server.server_port
        allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host") not in allowed_hosts:
            raise PermissionError("仅允许本机访问。")
        origin = self.headers.get("Origin")
        if origin and origin not in {f"http://{h}" for h in allowed_hosts}:
            raise PermissionError("不接受其他网站的请求。")

    def panel_auth(self):
        cookie = SimpleCookie()
        cookie.load(self.headers.get("Cookie", ""))
        value = cookie.get("relay_session")
        if not value or not secrets.compare_digest(value.value, self.server.session_secret):
            raise PermissionError("请先打开本地面板。")

    def agent_auth(self):
        authorization = self.headers.get("Authorization", "")
        if not authorization.startswith("Bearer "):
            raise PermissionError("协作桥认证失败。")
        token = authorization[7:]
        for agent_id, candidate in self.server.credentials.items():
            if secrets.compare_digest(candidate, token):
                agent = next(a for a in self.server.store.snapshot()["agents"] if a["id"] == agent_id)
                if "setup" in agent and not agent["setup"]["configured"]:
                    raise PermissionError("用户已撤销此成员的应用接入。")
                return agent_id
        raise PermissionError("协作桥认证失败。")

    def read_json(self):
        if self.headers.get_content_type() != "application/json":
            raise Invalid("仅接受 JSON 请求。")
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 8 * 1024 * 1024:
            raise Invalid("请求为空或超过大小限制。")
        previous_timeout=self.connection.gettimeout()
        self.connection.settimeout(10)
        try:
            raw=self.rfile.read(length)
        finally:
            self.connection.settimeout(previous_timeout)
        body = json.loads(raw.decode("utf-8"))
        if not isinstance(body, dict):
            raise Invalid("请求必须是对象。")
        return body

    def do_GET(self):
        try:
            self.local_request()
            parsed = urlsplit(self.path)
            path = parsed.path
            if path == "/api/health":
                self.json({
                    "app": "stage-relay",
                    "version": __version__,
                    "pid": os.getpid(),
                    "data_dir": str(self.server.store.path.parent),
                    "bridge_refresh": getattr(self.server, "bridge_refresh", []),
                })
                return
            if path.startswith("/api/"):
                self.panel_auth()
                query = parse_qs(parsed.query)
                if path == "/api/projects/recycled":
                    self.json({"projects": self.server.recycled_projects()})
                    return
                if path == "/api/settings/storage":
                    state = self.server.store.snapshot()
                    cleanup = self.server.cleanup()
                    self.json({"storage": cleanup.storage_rows(state["projects"]),
                               "recycled": self.server.recycled_projects()})
                    return
                if path == "/api/state":
                    state = self.server.store.snapshot()
                    state['runtime'] = self.server.runtime.snapshot()
                    state['codexNative'] = self.server.codex.snapshot()
                    state['storage_path'] = str(self.server.store.path.parent)
                    self.json(state)
                elif path == "/api/settings/cleanup/preview":
                    self.json(self.server.cleanup().settings_preview(self.server.store.snapshot()["projects"]))
                elif path == "/api/config":
                    agent = query.get("agent", [""])[0]
                    if agent not in {a["id"] for a in self.server.store.snapshot()["agents"]}:
                        raise Invalid("成员不存在。")
                    self.server.refresh_credentials()
                    member = next(a for a in self.server.store.snapshot()["agents"] if a["id"] == agent)
                    self.json({"mcpServers": {f"stage-relay-{agent}": self.server.connections.entry(member)}})
                elif path == "/api/cleanup/preview":
                    state = self.server.store.snapshot()
                    project = next((p for p in state["projects"] if p["id"] == query.get("project", [""])[0]), None)
                    if not project:
                        raise Invalid("任务不存在。")
                    self.json(self.server.cleanup().preview(project, state["projects"]))
                elif path == "/api/cleanup/recoveries":
                    project_id = query.get("project", [""])[0]
                    if not any(p["id"] == project_id for p in self.server.store.snapshot()["projects"]):
                        raise Invalid("任务不存在。")
                    self.json(self.server.cleanup().recoveries(project_id))
                elif path == "/api/export":
                    state = self.server.store.snapshot()
                    project = next((p for p in state["projects"] if p["id"] == query.get("project", [""])[0]), None)
                    if not project:
                        raise Invalid("任务不存在。")
                    body = export_markdown(project, state["agents"]).encode("utf-8")
                    self.send_bytes(body, "text/markdown; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="stage-relay-report.md"'})
                else:
                    self.json({"error": "找不到接口。"}, 404)
                return
            if path.startswith("/files/"):
                self.panel_auth()
                file_id = path.removeprefix("/files/")
                if len(file_id) != 24 or any(c not in "0123456789abcdef" for c in file_id):
                    raise Invalid("文件不存在。")
                root = self.server.store.path.parent / "uploads"
                meta = json.loads((root / f"{file_id}.json").read_text(encoding="utf-8"))
                from urllib.parse import quote
                self.send_bytes((root / file_id).read_bytes(), "application/octet-stream", headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(meta['name'])}"})
                return
            relative = "index.html" if path in {"/", "/index.html"} else path.lstrip("/")
            target = (WEB / relative).resolve()
            if not target.is_relative_to(WEB.resolve()) or not target.is_file():
                self.json({"error": "页面不存在。"}, 404)
                return
            headers = {"Set-Cookie": f"relay_session={self.server.session_secret}; HttpOnly; SameSite=Strict; Path=/"} if relative == "index.html" else {}
            mime = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            if mime.startswith("text/") or target.suffix == ".js":
                mime = ("text/javascript" if target.suffix == ".js" else mime) + "; charset=utf-8"
            self.send_bytes(target.read_bytes(), mime, headers=headers)
        except PermissionError as exc:
            self.close_connection = True
            self.json({"error": str(exc)}, 403)
        except (Invalid, ValueError, FileNotFoundError) as exc:
            self.close_connection = True
            self.json({"error": str(exc)}, 400)
        except (ConnectionResetError, BrokenPipeError):
            pass

    def do_POST(self):
        try:
            self.local_request()
            path = urlsplit(self.path).path
            # Consume the bounded body before returning authentication errors.
            # Closing TCP with unread POST bytes can discard the 403 on Windows.
            body = self.read_json()
            if path == "/bridge":
                actor = self.agent_auth()
                action = body.get("action")
                if action == 'runtime_poll':
                    result = self.server.runtime.poll(actor,body.get('args',{}))
                elif action == 'runtime_receipt':
                    result = self.server.runtime.receipt(actor,body.get('args',{}))
                elif action == 'runtime_event':
                    result = self.server.runtime.observe(actor,body.get('args',{}))
                elif action == 'runtime_created':
                    result = self.server.runtime.created(actor,body.get('args',{}))
                elif action == "inbox":
                    # An absent `after` means "continue from my cursor"; an explicit
                    # 0 means "re-read everything", so the two must stay distinct.
                    args = body.get("args", {})
                    result = self.server.store.inbox(actor, args.get("after"),
                                                     full=args.get("full") is True,
                                                     ack=args.get("ack"))
                elif action == "rules":
                    result = {"version": rules.VERSION, "text": rules.full(actor)}
                elif action == "read_project":
                    result = self.server.store.read_project(actor, body.get("args", {}).get("project_id"))
                elif action == "read_task":
                    args = body.get("args", {})
                    result = self.server.store.read_task(actor, args.get("project_id"), args.get("task_id"))
                elif action == "read_issue":
                    args = body.get("args", {})
                    result = self.server.store.read_issue(actor, args.get("project_id"), args.get("issue_id"))
                elif action == "read_message":
                    args = body.get("args", {})
                    result = self.server.store.read_message(actor, args.get("project_id"),
                                                            args.get("message_id"), args.get("issue_id"))
                elif action == "read_list":
                    args = body.get("args", {})
                    result = self.server.store.read_list(actor, args.get("project_id"), args.get("kind"),
                                                         args.get("offset", 0), args.get("limit", 50),
                                                         args.get("issue_id"))
                elif action == "read_stages":
                    args = body.get("args", {})
                    result = self.server.store.read_stages(actor, args.get("project_id"),
                                                           args.get("stage_id"))
                elif action == "read_event":
                    args = body.get("args", {})
                    result = self.server.store.read_event(actor, args.get("project_id"), args.get("seq"))
                elif action in AGENT_ACTIONS:
                    result = self.server.store.apply(action, body.get("args", {}), actor)
                else:
                    raise PermissionError("成员不能操作用户决策或管理设置。")
                self.json({"result": result})
                return
            self.panel_auth()
            if self.headers.get("X-Relay-Client") != "panel":
                raise PermissionError("请使用本地面板提交。")
            if path == '/api/native/codex/connect':
                self.json(self.server.codex.request_connect(body.get('agent_id')))
            elif path == '/api/native/codex/respond':
                self.json(self.server.codex.respond(body.get('agent_id'),body.get('request_key'),body.get('answer',{})))
            elif path == '/api/runtime/model':
                self.json(self.server.runtime.model_command(body.get('project_id'),body.get('agent_id'),body.get('operation'),body.get('selection')))
            elif path == '/api/runtime/create':
                self.json(self.server.runtime.create_session(body.get('project_id'),body.get('agent_id'),replace=body.get('replace') is True))
            elif path == '/api/runtime/bind':
                self.json(self.server.runtime.bind(body.get('project_id'),body.get('agent_id'),body.get('session_id')))
            elif path == "/api/application/open":
                member = next((a for a in self.server.store.snapshot()["agents"] if a["id"] == body.get("agent_id")), None)
                if not member:
                    raise Invalid("成员不存在。")
                self.json(open_application(member["app"],test_home=self.server.connections.home if self.server.connections.home != Path.home().resolve() else None))
            elif path == "/api/connection/prepare":
                agent = next((a for a in self.server.store.snapshot()["agents"] if a["id"] == body.get("agent_id")), None)
                if not agent:
                    raise Invalid("成员不存在。")
                self.json(self.server.connections.prepare(agent, remove=body.get("remove") is True))
            elif path == "/api/connection/commit":
                result = self.server.connections.commit(body.get("plan_id"), body.get("consent"))
                self.server.store.apply("setup_record", result)
                member=next(a for a in self.server.store.snapshot()['agents'] if a['id']==result['agent_id'])
                if member['app']=='codex' and self.server.codex.enabled:
                    if result['configured']:self.server.codex.request_connect(member['id'])
                    else:self.server.codex.disconnect(member['id'])
                if result['configured'] and body.get('project_id') and member['app'] in {'codex','dsh'}:
                    result['native_session']=self.server.runtime.create_session(body['project_id'],member['id'],wait_for_connection=True)
                self.json(result)
            elif path == '/api/native/codex/release':
                # Free the panel's own app-server without touching the app
                # authorization, the thread or any shared process.
                member=next((a for a in self.server.store.snapshot()['agents'] if a['id']==body.get('agent_id')),None)
                if not member or member['app']!='codex':
                    raise Invalid('请选择 Codex 成员。')
                self.json(self.server.codex.release(member['id']))
            elif path == "/api/project/close":
                # Closing is two ordered stages: the durable record stops work,
                # then only registered resources are retired or unbound.
                project_id = body.get("project_id")
                result = self.server.store.apply("project_close", {"project_id": project_id, "retention_days": body.get("retention_days", 7)})
                plan = self.server.runtime.close_project(project_id)
                self.json({"result": result, "plan": plan, "state": self.server.store.snapshot()})
            elif path == "/api/cleanup/preview":
                state = self.server.store.snapshot()
                project = next((p for p in state["projects"] if p["id"] == body.get("project_id")), None)
                if not project:
                    raise Invalid("任务不存在。")
                self.json(self.server.cleanup().preview(project, state["projects"]))
            elif path == "/api/cleanup/execute":
                state = self.server.store.snapshot()
                project = next((p for p in state["projects"] if p["id"] == body.get("project_id")), None)
                if not project:
                    raise Invalid("任务不存在。")
                if body.get("confirm") is not True:
                    raise Invalid("清理需要用户明确确认。")
                # The token binds this run to the exact list the user saw.
                self.json(self.server.cleanup().execute(project, state["projects"], body.get("plan_id")))
            elif path == "/api/cleanup/recoveries":
                project_id = body.get("project_id")
                if not any(p["id"] == project_id for p in self.server.store.snapshot()["projects"]):
                    raise Invalid("任务不存在。")
                self.json(self.server.cleanup().recoveries(project_id))
            elif path == "/api/cleanup/restore":
                if body.get("confirm") is not True:
                    raise Invalid("恢复需要用户明确确认。")
                project_id = body.get("project_id")
                if not any(p["id"] == project_id for p in self.server.store.snapshot()["projects"]):
                    raise Invalid("任务不存在。")
                self.json(self.server.cleanup().restore(project_id, body.get("plan_id")))
            elif path == "/api/settings/cleanup/preview":
                self.json(self.server.cleanup().settings_preview(self.server.store.snapshot()["projects"]))
            elif path == "/api/settings/cleanup/execute":
                state = self.server.store.snapshot()
                if body.get("confirm") is not True:
                    raise Invalid("清理需要用户明确确认。")
                plan_id = body.get("plan_id")
                if not isinstance(plan_id, str) or len(plan_id) != 32 or any(c not in "0123456789abcdef" for c in plan_id):
                    raise Invalid("请先生成清理预览。")
                plan_file = self.server.store.path.parent / "cleanup-plans" / f"{plan_id}.json"
                if not plan_file.is_file():
                    raise Invalid("预览已过期，请重新生成。")
                plan = json.loads(plan_file.read_text(encoding="utf-8"))
                if plan.get("scope") != "settings":
                    raise Invalid("这不是设置清理清单。")
                scope = {"id": "*settings*", "closed": True, "retention_days": 0, "title": "应用占用清理"}
                self.json(self.server.cleanup().execute(scope, state["projects"], plan_id))
            elif path == "/api/action":
                action = body.get("action")
                if action not in USER_ACTIONS:
                    raise Invalid("不支持此操作。")
                args = body.get("args", {})
                # Only these two are hand-written reads/writes; everything else is a
                # normal store action.
                if action == "mentions_read":
                    self.json(self.server.store.mark_user_mentions_read(
                        args.get("project_id"), args.get("issue_id"), args.get("message_ids")))
                    return
                result = self.server.store.apply(action, args)
                if action == "record_archive":
                    self.server.runtime.cancel_archived_record(args.get("project_id"), args.get("kind"), args.get("item_id"), args.get("issue_id"))
                if action in {"project_create", "demo_create"} and not result.get("demo"):
                    # A new project gets its registered scratch directory immediately,
                    # otherwise the folder only appears after a restart.
                    try:
                        scratch_for(self.server.store.path.parent, result["id"])
                    except OSError:
                        pass
                if action == "agent_save":
                    self.server.refresh_credentials()
                if action == 'assign':
                    reviewer=next((a for a in self.server.store.snapshot()['agents'] if a['id']==result['assignments']['planner']),None)
                    if reviewer and reviewer['app']=='codex' and reviewer.get('setup',{}).get('configured') and self.server.codex.enabled:
                        self.server.codex.request_connect(reviewer['id'])
                self.json({"result": result, "state": self.server.store.snapshot()})
            elif path == "/api/upload":
                name = body.get("name")
                if not isinstance(name, str) or not name.strip() or len(name) > 200:
                    raise Invalid("文件名无效。")
                content = base64.b64decode(body.get("data", ""), validate=True)
                if len(content) > 5 * 1024 * 1024:
                    raise Invalid("单个附件不能超过 5 MB。")
                root = self.server.store.path.parent / "uploads"
                root.mkdir(exist_ok=True)
                file_id = secrets.token_hex(12)
                project_id = body.get("project_id")
                project = next((p for p in self.server.store.snapshot()["projects"] if p["id"] == project_id), None)
                meta = {"id": file_id, "name": name, "size": len(content), "project": project_id if project and not project.get("closed") else None}
                (root / file_id).write_bytes(content)
                (root / f"{file_id}.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
                self.json(meta)
            else:
                self.json({"error": "接口不存在。"}, 404)
        except PermissionError as exc:
            self.close_connection = True
            self.json({"error": str(exc)}, 403)
        except (Invalid, NativeError, TimeoutError, ValueError, KeyError, TypeError, AttributeError) as exc:
            self.close_connection = True
            self.json({"error": str(exc)}, 400)
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            self.close_connection = True
        except OSError as exc:
            self.close_connection = True
            logging.getLogger(__name__).warning("Panel operation failed: %s (errno=%s, winerror=%s)",
                                               type(exc).__name__, exc.errno, getattr(exc, "winerror", None))
            self.json({"error": "本地操作暂时失败，原因已记录到面板日志。"}, 500)


def main():
    parser = argparse.ArgumentParser(description="Stage Relay 本地协作面板")
    parser.add_argument("--port", type=int, default=4318)
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--open", action="store_true")
    parser.add_argument("--app-home", type=Path, default=None, help=argparse.SUPPRESS)
    args = parser.parse_args()
    from .storage import default_data_dir, describe
    args.data_dir = (args.data_dir or default_data_dir()).resolve()
    store = Store(args.data_dir / "workspace.sqlite3")
    if not store.snapshot()["projects"]:
        store.apply("demo_create", {})
    try:
        server = PanelServer(("127.0.0.1", args.port), store, connection_home=args.app_home)
    except OSError:
        from urllib.request import urlopen
        try:
            with urlopen(f"http://127.0.0.1:{args.port}/api/health", timeout=2) as response:
                existing = json.load(response)
            if existing.get("app") != "stage-relay":
                raise ValueError("different application")
        except (OSError, ValueError):
            parser.error(f"端口 {args.port} 无法使用，请用 --port 指定其他端口。")
        running_dir = existing.get("data_dir")
        # A second process must never publish a different store on the port the
        # user already has open: the page would then show other (or no) projects.
        if running_dir and Path(running_dir).resolve() != args.data_dir:
            parser.error(
                f"端口 {args.port} 已被另一个 Stage Relay 使用，它加载的数据目录是：\n"
                f"  {running_dir}\n本次请求的是：\n  {args.data_dir}\n"
                "请关闭那个实例，或用 --data-dir 指定同一个目录。"
            )
        print(f"Stage Relay 已在运行：http://127.0.0.1:{args.port}", flush=True)
        print(describe(args.data_dir), flush=True)
        if args.open:
            webbrowser.open(f"http://127.0.0.1:{args.port}")
        return
    url = f"http://127.0.0.1:{server.server_port}"
    print(f"Stage Relay {__version__} | {url}", flush=True)
    print(describe(args.data_dir), flush=True)
    print("Local data is saved. Agent execution requires explicit application connection.", flush=True)
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
