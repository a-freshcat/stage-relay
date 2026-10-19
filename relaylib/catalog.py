"""Read provider model IDs without generation, redirects or browser-side secrets."""
import http.client
import hashlib
import json
import socket
import time
from urllib.parse import urlsplit

from .contracts import RelayError
from .gateway import resolve_endpoint
from .settings import MODEL, SettingsError, endpoint
from .credentials import CredentialError, validate_key

TTL = 300
MAX_BYTES = 4_000_000  # Catalog transport protection only; unrelated to model context.


def fetch_models(spec, api_key=None, trace=None):
    """Read GET /models. `trace` receives the real HTTP status and timing for explicit checks."""
    spec = endpoint(spec)
    started = time.monotonic()
    if trace is not None:
        trace.update(status=None, elapsed_seconds=None)
    try:
        if api_key is not None:
            validate_key(api_key)
            if "base_url" not in spec:
                raise CredentialError("已有供应商使用自己的凭据；直接填写密钥请选择自定义 API。")
        resolved = resolve_endpoint(spec, catalog_request=True, api_key_override=api_key)
    except CredentialError as error:
        raise SettingsError(str(error)) from None
    except (RelayError, OSError, ValueError):
        name = spec.get("api_key_env")
        raise SettingsError((f"无法读取接口凭据，请让面板进程继承环境变量 {name}。" if name else
                             "无法读取接口地址或本机密钥，请在接口设置中重新填写 API Key。"), 422) from None
    parsed = urlsplit(resolved["url"])
    connection = (http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection)(
        parsed.hostname, parsed.port, timeout=15)
    try:
        connection.request("GET", parsed.path.rstrip("/") + "/models",
                           headers={**resolved["headers"], "Accept": "application/json"})
        response = connection.getresponse()
        if trace is not None:
            trace["status"] = response.status
            trace["elapsed_seconds"] = round(time.monotonic() - started, 3)
        if response.status in (401, 403):
            raise SettingsError("模型列表认证失败（HTTP %s），请检查此接口的凭据或目录访问权限。" % response.status, 422)
        if response.status in (404, 405, 501):
            raise SettingsError("此接口未提供 GET /models 模型目录；可改用支持目录的入口，或开启手动填写。", 422)
        if response.status != 200:
            raise SettingsError("获取模型列表失败（HTTP %s）；未发起生成请求。" % response.status, 502)
        raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise SettingsError("接口模型目录超过 4 MB，未截断显示，请检查接口返回。", 502)
        data = json.loads(raw)
        rows = data.get("data", data.get("models")) if isinstance(data, dict) else None
        if not isinstance(rows, list) or data.get("has_more"):
            raise SettingsError("接口未返回完整的模型目录数组，无法作为可选列表。", 502)
        models = []
        for row in rows:
            identity = row.get("id") if isinstance(row, dict) else row
            if not isinstance(identity, str) or not MODEL.fullmatch(identity):
                raise SettingsError("目录含有无法用于当前执行器的模型 ID，请检查供应商返回。", 502)
            models.append(identity)
        return sorted(set(models), key=str.casefold)
    except SettingsError:
        raise
    except (TimeoutError, socket.timeout):
        if trace is not None:
            trace["elapsed_seconds"] = round(time.monotonic() - started, 3)
        raise SettingsError("获取模型列表超时（15 秒）；可点击刷新重试，未发起生成请求。", 504) from None
    except ConnectionRefusedError:
        if trace is not None:
            trace["elapsed_seconds"] = round(time.monotonic() - started, 3)
        raise SettingsError("模型目录连接被拒绝，请确认接口服务已启动且地址、端口正确。", 502) from None
    except ValueError:
        if trace is not None:
            trace["elapsed_seconds"] = round(time.monotonic() - started, 3)
        raise SettingsError("接口的模型目录返回内容不是有效 JSON，请检查 GET /models 支持情况。", 502) from None
    except (OSError, http.client.HTTPException):
        if trace is not None:
            trace["elapsed_seconds"] = round(time.monotonic() - started, 3)
        raise SettingsError("模型目录连接失败，请检查网络、接口服务及证书。", 502) from None
    finally:
        connection.close()


class Catalog:
    def __init__(self):
        self.cache = {}

    def traceable(self, value):
        """Explicit check: same directory request without the 5-minute cache."""
        spec = endpoint(value["endpoint"])
        trace = {}
        try:
            models = fetch_models(spec, trace=trace)
        except Exception as error:
            error.trace = trace
            raise
        return {**trace, "models": models, "count": len(models)}

    def discover(self, value):
        if not isinstance(value, dict) or set(value) - {"endpoint", "refresh", "api_key"} or "endpoint" not in value or type(value.get("refresh", False)) is not bool:
            raise SettingsError("模型目录请求格式不正确。")
        spec = endpoint(value["endpoint"])
        api_key = value.get("api_key")
        if "api_key" in value:
            try:
                validate_key(api_key)
            except CredentialError as error:
                raise SettingsError(str(error)) from None
        key = json.dumps(spec, sort_keys=True) + (hashlib.sha256(api_key.encode()).hexdigest() if api_key is not None else "")
        saved = self.cache.get(key)
        cached = bool(saved and time.monotonic() - saved[0] < TTL and not value.get("refresh"))
        if not cached:
            models = fetch_models(spec, api_key) if api_key is not None else fetch_models(spec)
            saved = (time.monotonic(), models, time.time())
            self.cache[key] = saved
        return {"models": saved[1], "fetched_at": saved[2], "cached": cached,
                "source": "endpoint", "generation_requests": 0,
                "notice": "接口声明的模型目录；未逐个验证生成、工具调用或账户权限。"}
