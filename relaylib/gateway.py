"""Short-lived loopback Responses proxy with a durable pre-forward request gate.

Counts this relay's forwarded HTTP requests, not invisible upstream retries,
approval services, or provider-side billing. It never estimates a hard USD cap.
"""

import hmac
import http.client
import json
import os
import secrets
import socket
import threading
import time
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .contracts import fail, load_json
from .runtime import atomic_json
from .credentials import WindowsVault, CredentialError


def resolve_endpoint(spec, catalog_request=False, api_key_override=None):
    """Read only the selected provider; never log credentials or run auth commands."""
    native_auth = spec.get("native_auth", False)
    catalog_models = None
    if "codex_provider" in spec:
        codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        config = tomllib.loads((codex_home / "config.toml").read_text(encoding="utf-8-sig"))
        provider = config.get("model_providers", {}).get(spec["codex_provider"])
        if not provider:
            fail("endpoint_config", "Named provider is missing from Codex config")
        if provider.get("auth") or (provider.get("requires_openai_auth") and not native_auth):
            fail("endpoint_config", "V1 requires an API-key/local-gateway provider, not OAuth/command auth")
        if provider.get("wire_api", "responses") != "responses":
            fail("endpoint_config", "The selected endpoint must speak Responses")
        url = provider.get("base_url", "")
        headers = dict(provider.get("http_headers", {}))
        for key, env_name in provider.get("env_http_headers", {}).items():
            if env_name not in os.environ:
                fail("endpoint_config", "A provider header environment variable is missing")
            headers[key] = os.environ[env_name]
        token = provider.get("experimental_bearer_token")
        if provider.get("env_key"):
            token = os.environ.get(provider["env_key"])
            if not token:
                fail("endpoint_config", "Provider API-key environment variable is missing")
        if provider.get("query_params"):
            fail("endpoint_config", "Provider query parameters require an explicit adapter")
        catalog = config.get("model_catalog_json")
        if catalog:
            path = Path(catalog)
            if not path.is_absolute():
                path = codex_home / path
            if path.exists():
                data = load_json(path, 5_000_000)
                models = data if isinstance(data, list) else data.get("models", [])
                if not isinstance(models, list):
                    fail("endpoint_config", "Model catalog must contain a models array")
                catalog_models = [m.get("slug", m.get("id")) for m in models if isinstance(m, dict)]
    else:
        url = spec.get("base_url", "")
        token = api_key_override if api_key_override is not None else os.environ.get(spec.get("api_key_env", ""))
        if api_key_override is None and spec.get("api_key_ref"):
            try:
                token = WindowsVault().get(spec["api_key_ref"], url)
            except CredentialError as error:
                fail("endpoint_config", str(error))
        if api_key_override is None and spec.get("api_key_env") and not token:
            fail("endpoint_config", "Configured API-key environment variable is missing")
        headers = {}
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        fail("endpoint_config", "Use a plain HTTP(S) Responses base URL without credentials/query")
    if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
        fail("endpoint_config", "Remote endpoints must use HTTPS")
    if native_auth and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
        fail("endpoint_config", "Native-login passthrough is only supported for the user's loopback gateway")
    if token and (not native_auth or catalog_request):
        headers["Authorization"] = "Bearer " + token
    if native_auth and not catalog_request:
        headers = {key: value for key, value in headers.items() if key.lower() != "authorization"}
    if any("\r" in str(v) or "\n" in str(v) for v in headers.values()):
        fail("endpoint_config", "Invalid provider header")
    return {"url": url.rstrip("/"), "headers": headers, "native_auth": native_auth,
            "catalog_models": catalog_models}


def healthcheck(endpoint):
    parsed = urlsplit(endpoint["url"])
    try:
        with socket.create_connection((parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)), timeout=2):
            pass
    except OSError:
        fail("endpoint_unavailable", "Model endpoint is unreachable; start CC Switch routing or fix the endpoint")


class Ledger:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.value = load_json(path) if self.path.exists() else {
            "roles": {}, "hard_usd_cap": None, "cost_usd": None,
            "auxiliary_request_coverage": "unknown", "accounting": "forwarded_http_requests_only"}

    def reserve(self, role, model, limit, phase):
        with self.lock:
            row = self.value["roles"].setdefault(role, {"forwarded": 0, "denied": 0})
            if row["forwarded"] >= limit:
                row["denied"] += 1
                atomic_json(self.path, self.value)
                return False
            row["forwarded"] += 1
            row["last_model"] = model
            row["last_phase"] = phase
            # Persist before any upstream request, including failed network attempts.
            atomic_json(self.path, self.value)
            return True


def upstream_failure(status, body):
    """Classify explicit provider errors without persisting any provider text."""
    try:
        data = json.loads(body)
        error = data.get("error", {}) if isinstance(data, dict) else {}
        message = error.get("message", "") if isinstance(error, dict) else ""
        code = error.get("code", error.get("type")) if isinstance(error, dict) else None
        message = message.casefold() if isinstance(message, str) else ""
    except (ValueError, UnicodeError):
        message = ""
        code = None
    if status == 400 and code == "unsupported_parameter":
        return "unsupported_parameter"
    if ("not allowed for user type" in message or "账号类型暂不支持" in message or
            "model access denied" in message):
        return "model_access_denied"
    if ("abusive traffic from this ip" in message or "ip address is blocked" in message or
            "not available in your region" in message):
        return "network_access_denied"
    if status in (401, 403):
        return "authentication_error"
    if status == 429:
        return "upstream_rate_limit"
    return "api_error"


class Gateway:
    def __init__(self, endpoint, model, role, limit, phase, ledger, audit_path,
                 max_body=2_000_000, timeout=60, max_output_tokens=None):
        self.endpoint, self.model, self.role = endpoint, model, role
        self.limit, self.phase, self.ledger = limit, phase, ledger
        self.audit_path = Path(audit_path)
        self.max_body, self.timeout = max_body, timeout
        self.max_output_tokens = max_output_tokens
        self.token = secrets.token_urlsafe(32)
        self.cancel = threading.Event()
        self.failure = None
        self.upstream_status = None
        self.audit_lock = threading.Lock()
        self.connections, self.connection_lock = set(), threading.Lock()
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):
                pass

            def reply(self, status, code):
                # Drain a small rejected body before closing, so Windows does
                # not discard the error response with a TCP reset.
                if not getattr(self, "request_body_read", False):
                    length = self.headers.get("Content-Length", "")
                    if length.isascii() and length.isdigit() and len(length) <= 6:
                        self.connection.settimeout(0.25)
                        try:
                            self.rfile.read1(min(int(length), 32_000))
                        except OSError:
                            pass
                payload = json.dumps({"error": {"type": "relay_gate", "message": code}}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(payload)
                self.close_connection = True

            def do_GET(self):
                self.reply(405, "Only Responses POST requests are supported")

            def do_POST(self):
                native = gateway.endpoint.get("native_auth", False)
                path = self.path
                if native:
                    prefix = "/" + gateway.token
                    if not path.startswith(prefix + "/"):
                        return self.reply(401, "Invalid local relay path token")
                    path = path[len(prefix):]
                elif not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + gateway.token):
                    return self.reply(401, "Invalid local relay token")
                if path not in ("/v1/responses", "/v1/responses/compact"):
                    return self.reply(404, "Unsupported endpoint")
                if self.headers.get("Transfer-Encoding") or self.headers.get("Content-Encoding", "identity") != "identity":
                    gateway.failure = "protocol_error"
                    gateway.cancel.set()
                    return self.reply(400, "Compressed/chunked request bodies are unsupported")
                try:
                    length = int(self.headers.get("Content-Length", "-1"))
                    if length > gateway.max_body:
                        gateway.failure = "input_limit"
                        gateway.cancel.set()
                        gateway.audit({"event": "request_denied", "reason": "input_limit",
                                       "body_bytes": length, "limit_bytes": gateway.max_body,
                                       "role": gateway.role, "phase": gateway.phase})
                        return self.reply(413, "Request exceeds configured byte limit; no model request was forwarded")
                    if length < 0:
                        raise ValueError()
                    self.connection.settimeout(10)
                    raw = self.rfile.read(length)
                    self.request_body_read = True
                    if len(raw) != length:
                        raise ValueError()
                    body = json.loads(raw)
                    if not isinstance(body, dict) or body.get("model") != gateway.model:
                        gateway.failure = "model_mismatch"
                        gateway.cancel.set()
                        return self.reply(400, "Request model differs from the configured stage model")
                except (ValueError, OSError):
                    gateway.failure = "protocol_error"
                    gateway.cancel.set()
                    return self.reply(400, "Invalid or oversized request body")
                if gateway.max_output_tokens is not None and path == "/v1/responses":
                    requested = body.get("max_output_tokens", gateway.max_output_tokens)
                    if type(requested) is not int or requested <= 0:
                        gateway.failure = "protocol_error"
                        gateway.cancel.set()
                        return self.reply(400, "Invalid output token limit")
                    body["max_output_tokens"] = min(requested, gateway.max_output_tokens)
                    raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                if gateway.cancel.is_set():
                    return self.reply(429, "Stage is stopped")
                try:
                    if not gateway.ledger.reserve(gateway.role, gateway.model, gateway.limit, gateway.phase):
                        gateway.failure = "request_limit"
                        gateway.cancel.set()
                        return self.reply(429, "Stage role request limit reached")
                    request_id = secrets.token_hex(12)
                    # Only a UUID-like identifier is public. Never log arbitrary routing headers.
                    from .subagents import public_agent_id
                    agent_id = public_agent_id(self.headers)
                    gateway.audit({"event": "request_forwarded", "role": gateway.role,
                                   "request_id": request_id, "agent_id": agent_id,
                                   "phase": gateway.phase, "model": gateway.model,
                                   "body_bytes": length, "time": time.time()})
                except OSError:
                    gateway.failure = "ledger_io_error"
                    gateway.cancel.set()
                    return self.reply(503, "Could not persist the request reservation; no request was forwarded")
                upstream = urlsplit(gateway.endpoint["url"])
                cls = http.client.HTTPSConnection if upstream.scheme == "https" else http.client.HTTPConnection
                conn = cls(upstream.hostname, upstream.port, timeout=gateway.timeout)
                with gateway.connection_lock:
                    gateway.connections.add(conn)
                started = False
                try:
                    path = upstream.path.rstrip("/") + path[len("/v1"):]
                    headers = {"Content-Type": "application/json", "Accept": "text/event-stream",
                               "Accept-Encoding": "identity", **gateway.endpoint["headers"]}
                    if native and self.headers.get("Authorization"):
                        headers["Authorization"] = self.headers["Authorization"]
                    # Keep harmless Codex routing metadata; never forward local credentials.
                    for key in ("OpenAI-Beta", "session_id", "conversation_id", "x-codex-turn-metadata",
                                "ChatGPT-Account-ID", "OpenAI-Organization", "OpenAI-Project", "originator", "User-Agent"):
                        if self.headers.get(key):
                            headers[key] = self.headers[key]
                    conn.request("POST", path, raw, headers)
                    response = conn.getresponse()
                    gateway.upstream_status = response.status
                    gateway.audit({"event": "upstream_status", "role": gateway.role,
                                   "phase": gateway.phase, "status": response.status})
                    if response.status >= 300:
                        gateway.failure = upstream_failure(response.status, response.read(64_000))
                        # Return only our generic error; upstream bodies can contain secrets.
                        gateway.audit({"event": "upstream_failure", "role": gateway.role,
                                       "phase": gateway.phase, "code": gateway.failure, "status": response.status})
                        self.reply(response.status, gateway.failure)
                        gateway.cancel.set()
                        return
                    self.send_response(response.status)
                    self.send_header("Content-Type", response.getheader("Content-Type", "application/json"))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    started = True
                    from .subagents import UsageCollector
                    telemetry = UsageCollector(response.getheader("Content-Type", "application/json"))
                    while not gateway.cancel.is_set():
                        chunk = response.read1(8192)
                        if not chunk:
                            break
                        telemetry.feed(chunk)
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    gateway.audit({"event": "response_usage", "role": gateway.role, "phase": gateway.phase,
                                   "request_id": request_id, "agent_id": agent_id,
                                   "usage": telemetry.finish() if not gateway.cancel.is_set() else None})
                    self.close_connection = True
                except (OSError, http.client.HTTPException):
                    gateway.failure = "transport_error"
                    gateway.cancel.set()
                    if not started:
                        try:
                            self.reply(502, "Upstream connection failed")
                        except OSError:
                            pass
                finally:
                    conn.close()
                    with gateway.connection_lock:
                        gateway.connections.discard(conn)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def audit(self, value):
        with self.audit_lock:
            with self.audit_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(value, ensure_ascii=False) + "\n")
                stream.flush()

    def __enter__(self):
        self.thread.start()
        return self

    @property
    def base_url(self):
        prefix = "/" + self.token if self.endpoint.get("native_auth") else ""
        return f"http://127.0.0.1:{self.server.server_port}{prefix}/v1"

    def __exit__(self, *_):
        self.cancel.set()
        with self.connection_lock:
            for conn in list(self.connections):
                conn.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
