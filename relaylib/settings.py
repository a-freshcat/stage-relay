"""Local provider presets. Configuration only: no upstream requests or secrets."""
import copy
import hashlib
import json
import re
import os
from pathlib import Path
from urllib.parse import urlsplit

from .contracts import RelayError, load_json
from .runner import read_config
from .runtime import FileLock, atomic_json
from .credentials import WindowsVault, CredentialError, new_reference, valid_reference, validate_key
from .review import target_fingerprint

MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,199}")
ID = re.compile(r"[A-Za-z0-9_-]{1,80}")


class SettingsError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def require(condition, message):
    if not condition:
        raise SettingsError(message)


def endpoint(value):
    require(isinstance(value, dict) and not set(value) - {"codex_provider", "base_url", "api_key_env", "api_key_ref", "native_auth"}, "接口仅接受地址和凭据引用；密钥请填专用密码框。")
    require(type(value.get("native_auth", False)) is bool, "登录选项格式不正确。")
    require(bool(value.get("codex_provider")) != bool(value.get("base_url")), "请选择一种接口来源。")
    if value.get("codex_provider"):
        require(isinstance(value["codex_provider"], str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", value["codex_provider"]), "已有供应商 ID 不正确。")
        require("api_key_env" not in value and "api_key_ref" not in value, "已有供应商使用自己的凭据配置。")
    else:
        url = value.get("base_url")
        require(isinstance(url, str) and len(url) <= 2048 and not any(c.isspace() for c in url), "请填写有效的 API 基础地址。")
        try:
            parsed = urlsplit(url)
            parsed.port
        except ValueError:
            raise SettingsError("API 地址格式不正确。")
        require(parsed.scheme in ("http", "https") and parsed.hostname and not (parsed.username or parsed.password or parsed.query or parsed.fragment), "地址需为 HTTP(S) 基础地址，不能包含凭据、查询参数或片段。")
        local = parsed.hostname in ("localhost", "127.0.0.1", "::1")
        require(parsed.scheme == "https" or local, "远程接口需使用 HTTPS。")
        require(not value.get("native_auth") or local, "已有登录只能透传给本地网关。")
        require(not parsed.path.rstrip("/").endswith(("/responses", "/chat/completions", "/messages")), "请填基础地址（例如 …/v1），不要填具体请求路径；原生 Chat/Messages 接口需经 CCSwitch 转换。")
        key = value.get("api_key_env", "")
        require(isinstance(key, str) and (not key or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,119}", key)), "请填环境变量名（例如 MY_MODEL_KEY），不要填密钥值。")
        require(not (key and value.get("native_auth")), "请选择环境变量凭据或本地登录透传。")
        if "api_key_ref" in value:
            require(valid_reference(value["api_key_ref"]), "本机密钥引用格式不正确。")
            require(not key and not value.get("native_auth"), "请选择一种密钥来源。")
    result = copy.deepcopy(value)
    if "base_url" in result:
        result["base_url"] = result["base_url"].rstrip("/")
    if not result.get("api_key_env"):
        result.pop("api_key_env", None)
    if not result.get("native_auth"):
        result.pop("native_auth", None)
    return result


def validate_profiles(values, allow_empty=False):
    require(isinstance(values, list) and (0 if allow_empty else 1) <= len(values) <= 30, "可保存 1 至 30 个接口。")
    result, ids, endpoints = [], set(), set()
    for profile in values:
        require(isinstance(profile, dict) and set(profile) == {"id", "name", "endpoint", "models"}, "接口字段不正确；不接受密钥值。")
        identity, name = profile["id"], profile["name"]
        require(isinstance(identity, str) and ID.fullmatch(identity) and identity not in ids, "接口 ID 重复或格式不正确。")
        require(isinstance(name, str) and 1 <= len(name.strip()) <= 80, "接口名称需为 1 至 80 个字符。")
        models = profile["models"]
        require(isinstance(models, list) and len(models) <= 200 and all(isinstance(m, str) and MODEL.fullmatch(m) for m in models), "模型 ID 格式不正确。")
        ep = endpoint(profile["endpoint"])
        key = json.dumps(ep, sort_keys=True)
        require(key not in endpoints, "此接口已在列表中，请编辑原接口或为它补充模型。")
        result.append({"id": identity, "name": name.strip(), "endpoint": ep, "models": list(dict.fromkeys(models))})
        ids.add(identity)
        endpoints.add(key)
    return result


class Settings:
    def __init__(self, config_path, profiles_path=None, fallback=None):
        self.config = Path(config_path)
        self.profiles = Path(profiles_path) if profiles_path else self.config.with_name("providers.local.json")
        self.fallback = Path(fallback) if fallback else self.config.with_name("config.example.json")

    def revision(self):
        data = b""
        for path in (self.config, self.profiles):
            data += (path.read_bytes() if path.exists() else b"") + b"\0"
        return hashlib.sha256(data).hexdigest()

    def read(self):
        try:
            config = read_config(self.config if self.config.exists() else self.fallback)
            data = load_json(self.profiles) if self.profiles.exists() else {"version": 1, "profiles": []}
        except RelayError:
            raise SettingsError("本地配置无法读取或格式有误，请检查配置文件。") from None
        require(data.get("version") == 1 and isinstance(data.get("profiles"), list), "本地接口目录格式不正确。")
        profiles = validate_profiles(data["profiles"], allow_empty=True)
        roles = {}
        for role in ("planner", "worker"):
            spec = config[role]
            ep = endpoint(spec["endpoint"])
            profile = next((p for p in profiles if p.get("endpoint") == ep), None)
            if profile is None:
                identity = "import-" + hashlib.sha256(json.dumps(ep, sort_keys=True).encode()).hexdigest()[:12]
                profile = {"id": identity, "name": "现有入口 · " + ep.get("codex_provider", "自定义"), "endpoint": ep, "models": []}
                profiles.append(profile)
            if spec["model"] not in profile.setdefault("models", []):
                profile["models"].append(spec["model"])
            roles[role] = {"provider_id": profile["id"], "model": spec["model"]}
        return config, profiles, roles

    def view(self):
        before = self.revision()
        _, profiles, roles = self.read()
        if self.revision() != before:
            raise SettingsError("配置刚刚变更，请重新读取。", 409)
        credentials = {}
        for profile in profiles:
            ep = profile["endpoint"]
            if ep.get("api_key_ref"):
                try:
                    WindowsVault().get(ep["api_key_ref"], ep["base_url"])
                    credentials[profile["id"]] = "saved"
                except CredentialError:
                    credentials[profile["id"]] = "missing"
            elif ep.get("api_key_env"):
                credentials[profile["id"]] = "environment" if os.environ.get(ep["api_key_env"]) else "environment_missing"
            else:
                credentials[profile["id"]] = "provider" if ep.get("codex_provider") else "none"
        return {"revision": before, "profiles": profiles, "roles": roles, "credential_states": credentials,
                "config_path": str(self.config.resolve()), "protocol": "responses"}

    def select_worker(self, value, catalog):
        require(isinstance(value, dict) and set(value) == {"revision", "provider_id", "model"}, "执行模型选择格式不正确。")
        view = self.view()
        require(value["revision"] == view["revision"], "配置刚刚变更，请刷新后再选择执行模型。")
        profile = next((p for p in view["profiles"] if p["id"] == value["provider_id"]), None)
        require(profile is not None and isinstance(value["model"], str) and MODEL.fullmatch(value["model"]), "请选择接口和模型。")
        if value["model"] not in profile["models"]:
            models = catalog.discover({"endpoint": profile["endpoint"]})["models"]
            require(value["model"] in models, "接口目录未返回该模型，请刷新列表。")
            profile["models"].append(value["model"])
        view["roles"]["worker"] = {"provider_id": profile["id"], "model": value["model"]}
        return self.save({key: view[key] for key in ("revision", "profiles", "roles")})

    def reviewer(self, provider_id, model):
        """Freeze a saved model for this review; no discovery or config mutation."""
        before = self.revision()
        config, profiles, _ = self.read()
        require(before == self.revision(), "配置刚刚变更，请重新选择审核模型。")
        profile = next((p for p in profiles if p["id"] == provider_id), None)
        require(profile is not None and model in profile["models"],
                "审核模型已被移除或尚未保存，请重新选择已有模型。")
        spec = copy.deepcopy(config["planner"])
        spec.update(model=model, endpoint=copy.deepcopy(profile["endpoint"]))
        return {"spec": spec, "selection": {"provider_id": provider_id,
                "provider_name": profile["name"], "model": model, "fingerprint": target_fingerprint(spec)}}

    def save(self, value):
        require(isinstance(value, dict) and {"revision", "profiles", "roles"} <= set(value) and not set(value) - {"revision", "profiles", "roles", "api_keys"}, "配置字段不正确。")
        profiles = validate_profiles(value["profiles"])
        api_keys = value.get("api_keys", {})
        require(isinstance(api_keys, dict) and set(api_keys) <= {p["id"] for p in profiles}, "密钥必须对应本次保存的接口。")
        try:
            for key in api_keys.values():
                validate_key(key)
        except CredentialError as error:
            raise SettingsError(str(error)) from None
        for p in profiles:
            if p["id"] in api_keys:
                require("base_url" in p["endpoint"] and not p["endpoint"].get("api_key_env") and not p["endpoint"].get("native_auth"), "直接填写密钥仅用于自定义 API 地址。")
        ids = {p["id"] for p in profiles}
        roles = value["roles"]
        require(isinstance(roles, dict) and set(roles) == {"planner", "worker"}, "请配置规划和执行两个角色。")
        for role in roles.values():
            require(isinstance(role, dict) and set(role) == {"provider_id", "model"}, "角色字段不正确。")
            require(isinstance(role["provider_id"], str) and role["provider_id"] in ids, "所选接口不存在。")
            require(isinstance(role["model"], str) and MODEL.fullmatch(role["model"]), "请填写准确的模型 ID。")
            profile = next(p for p in profiles if p["id"] == role["provider_id"])
            require(role["model"] in profile["models"], "角色模型已被删除或未保存，请先选择此接口的模型。")
        with FileLock(self.profiles.with_suffix(".settings.lock")):
            if value["revision"] != self.revision():
                raise SettingsError("其他窗口或程序修改了配置。请重新打开设置后再保存。", 409)
            config, old_profiles, _ = self.read()
            known = {p["endpoint"]["api_key_ref"] for p in old_profiles if p["endpoint"].get("api_key_ref")}
            require(all(not p["endpoint"].get("api_key_ref") or p["endpoint"]["api_key_ref"] in known for p in profiles), "本机密钥引用不属于此配置，请重新填写密钥。")
            created = []
            vault = None
            try:
                if api_keys:
                    vault = WindowsVault()
                for p in profiles:
                    if p["id"] in api_keys:
                        reference = new_reference()
                        vault.put(reference, p["endpoint"]["base_url"], api_keys[p["id"]])
                        created.append(reference)
                        if vault.get(reference, p["endpoint"]["base_url"]) != api_keys[p["id"]]:
                            raise CredentialError("密钥写入后无法正确读回，配置未完成保存。")
                        p["endpoint"]["api_key_ref"] = reference
                for role, selection in roles.items():
                    profile = next(p for p in profiles if p["id"] == selection["provider_id"])
                    config[role]["model"] = selection["model"]
                    config[role]["endpoint"] = copy.deepcopy(profile["endpoint"])
                atomic_json(self.profiles, {"version": 1, "profiles": profiles})
                atomic_json(self.config, config)
            except (CredentialError, OSError) as error:
                # Keep credentials referenced by any successfully persisted file.
                persisted = "".join(p.read_text(encoding="utf-8") for p in (self.profiles, self.config) if p.exists())
                for reference in created:
                    if reference not in persisted:
                        vault.delete(reference)
                if isinstance(error, CredentialError):
                    raise SettingsError(str(error)) from None
                raise
            retained = {p["endpoint"]["api_key_ref"] for p in profiles if p["endpoint"].get("api_key_ref")}
            cleanup_warning = False
            for reference in known - retained:
                try:
                    (vault or WindowsVault()).delete(reference)
                except CredentialError:
                    cleanup_warning = True
        result = self.view()
        if cleanup_warning:
            result["credential_warning"] = "配置已保存，但 Windows 未能清理旧密钥；可在凭据管理器的 StageRelay/API 项中移除。"
        return result
