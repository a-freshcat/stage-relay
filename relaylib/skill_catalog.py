"""Local skill discovery and read evidence. Never installs tools or calls a model."""
import json
import os
import re
import tomllib
from pathlib import Path

from .runtime import atomic_json


def policy(spec):
    """Conservative model-ID routing; gateway aliases cannot prove model identity."""
    model = spec.get("model", "").casefold()
    if re.search(r"gpt|codex|(?:^|[/_.-])o[134](?:$|[/_.-])", model) or spec.get("endpoint", {}).get("native_auth"):
        return {"enabled": False, "reason": "native", "message": "GPT / Codex 路线沿用原生技能；不重复扫描或注入技能目录。"}
    mode = spec.get("skill_reuse", "auto")
    if mode == "off":
        return {"enabled": False, "reason": "off", "message": "已关闭额外技能目录；不改变 CLI 原生技能。"}
    if mode == "non_gpt" or re.search(r"deepseek|glm|qwen|kimi|claude|gemini|minimax|mistral|llama|grok|doubao|hunyuan|seed[-_.]", model):
        return {"enabled": True, "reason": "non_gpt", "message": "为非 GPT 执行端提供按需技能目录；模型身份仍以供应商路由为准。"}
    return {"enabled": False, "reason": "unknown", "message": "模型别名无法确认是否为非 GPT，未添加目录。原生技能不受影响；确认别名后可配置 worker.skill_reuse 为 non_gpt。"}


def _scalar(value):
    value = value.strip()
    if value.startswith('"'):
        try:
            return json.loads(value)
        except ValueError:
            return value.strip('"')
    return value.strip("'")


def metadata(path):
    # Only SKILL.md is inspected; arbitrary supporting files are never preloaded.
    with path.open(encoding="utf-8-sig") as stream:
        text = stream.read(128_000)
    match = re.match(r"\A---\s*\n(.*?)\n---(?:\s*\n|$)", text, re.S)
    if not match:
        return None
    header = match[1]
    values = {}
    for key in ("name", "description"):
        field = re.search(r"^" + key + r":[ \t]*([^\n]*)(?:\n|$)", header, re.M)
        if not field:
            return None
        value = field[1].strip()
        if value in ("|", ">", "|-", ">-"):
            continuation = []
            for line in header[field.end():].splitlines():
                if line and not line[0].isspace():
                    break
                continuation.append(line.strip())
            value = " ".join(continuation)
        values[key] = _scalar(value)
    if not all(isinstance(v, str) and v.strip() for v in values.values()):
        return None
    # This is a dependency hint, not a claim that any MCP tool was made available.
    servers = sorted(set(re.findall(r"mcp__([A-Za-z0-9_-]+)__", text)))
    declared = re.findall(r"type:\s*mcp\s*\n\s*value:\s*([^\n]+)", header)
    values["dependencies"] = sorted(set(servers + [_scalar(s) for s in declared]))
    values["host_tools"] = bool(re.search(r"\bcua\.|\bnodeRepl\.|image_gen|load_workspace_dependencies|app://", text))
    values["local_scripts"] = "scripts/" in text or ".py`" in text
    return values


def _version(path):
    return tuple(int(n) for n in re.findall(r"\d+", path.name)), path.name


def _roots(project, home, codex_home, config, warnings):
    roots = []
    project = Path(project).resolve()
    for parent in (project, *project.parents):
        roots.append((parent / ".agents/skills", "project"))
        if (parent / ".git").exists():
            break
    roots += [(home / ".agents/skills", "user"), (codex_home / "skills", "user")]
    for entry in config.get("skills", {}).get("config", []):
        if isinstance(entry, dict) and entry.get("enabled", True) and isinstance(entry.get("path"), str):
            path = Path(entry["path"]).expanduser()
            if path.is_file() and path.name == "SKILL.md":
                path = path.parent
            roots.append((path, "configured"))
    for identity, options in sorted(config.get("plugins", {}).items()):
        if not isinstance(options, dict) or not options.get("enabled") or "@" not in identity:
            continue
        plugin, market = identity.rsplit("@", 1)
        if any(part in ("", ".", "..") or "/" in part or "\\" in part for part in (plugin, market)):
            continue
        cache = codex_home / "plugins/cache" / market / plugin
        try:
            versions = sorted((p for p in cache.iterdir() if p.is_dir() and not p.name.startswith(".")), key=_version, reverse=True)
        except OSError:
            warnings.append(f"已启用插件 {identity} 的本地缓存不可读，未发现它的技能。")
            continue
        if not versions:
            continue
        version = versions[0]
        root = version / "skills"
        for manifest in (version / "plugin.json", version / ".codex-plugin/plugin.json"):
            if not manifest.is_file():
                continue
            try:
                declared = json.loads(manifest.read_text(encoding="utf-8")).get("skills", "./skills/")
                if isinstance(declared, str):
                    root = (version / declared).resolve()
                    if not root.is_relative_to(version.resolve()):
                        raise ValueError("Skill path leaves plugin")
                break
            except (OSError, ValueError):
                warnings.append(f"插件 {identity} 的技能路径无效，未加入目录。")
                root = None
                break
        if root:
            roots.append((root, identity))
    return roots


def discover(project, *, home=None, codex_home=None):
    home = Path(home) if home is not None else Path.home()
    codex_home = Path(codex_home) if codex_home is not None else Path(os.environ.get("CODEX_HOME", home / ".codex"))
    warnings, config = [], {}
    try:
        config = tomllib.loads((codex_home / "config.toml").read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        warnings.append("Codex 配置不可读；仍扫描本地技能，插件启用状态无法确认。")
    # An unrelated malformed optional section must not prevent the original task.
    for key in ("skills", "plugins", "mcp_servers"):
        if not isinstance(config.get(key, {}), dict):
            config[key] = {}
    if not isinstance(config.get("skills", {}).get("config", []), list):
        config["skills"]["config"] = []
    disabled = {str(Path(row["path"]).expanduser().resolve()).casefold()
                for row in config.get("skills", {}).get("config", [])
                if isinstance(row, dict) and row.get("enabled") is False and isinstance(row.get("path"), str)}
    configured_servers = sorted(name for name, value in config.get("mcp_servers", {}).items()
                                if isinstance(value, dict) and value.get("enabled", True))
    entries, seen_paths, seen_ids = [], set(), set()
    for root, source in _roots(project, home, codex_home, config, warnings):
        if not root.is_dir():
            continue
        try:
            paths = sorted(root.rglob("SKILL.md"))
            if (root / "SKILL.md").is_file() and root / "SKILL.md" not in paths:
                paths.insert(0, root / "SKILL.md")
            for path in paths:
                resolved = path.resolve()
                key = str(resolved).casefold()
                if key in seen_paths or key in disabled or str(resolved.parent).casefold() in disabled:
                    continue
                seen_paths.add(key)
                try:
                    row = metadata(path)
                except (OSError, UnicodeError):
                    warnings.append(f"技能说明不可读：{path}")
                    continue
                if not row:
                    warnings.append(f"技能缺少可识别的名称或简介：{path}")
                    continue
                # This worker must not re-enter the caller's prepare/submit workflow.
                if source == "stage-relay@stage-relay-local" and row["name"] in ("stage-relay", "relay-context"):
                    continue
                identity = (source + ":" if "@" in source else "") + row["name"]
                if identity in seen_ids:
                    continue
                seen_ids.add(identity)
                hints = []
                if row["host_tools"]:
                    hints.append("依赖宿主工具；执行阶段可用性未验证")
                for server in row["dependencies"]:
                    hints.append(f"MCP {server}：" + ("本机已配置，连接未验证" if server in configured_servers else "执行端未确认配置"))
                if row["local_scripts"]:
                    hints.append("本地脚本及运行依赖需按需检查")
                entries.append({"id": identity, "name": row["name"], "description": row["description"],
                                "path": str(resolved), "source": source,
                                "dependencies": row["dependencies"], "hints": hints})
        except OSError:
            warnings.append(f"技能目录不可读：{root}")
    return {"version": 1, "entries": entries, "warnings": warnings,
            "note": "仅扫描本机文件和启用插件的最新缓存；未探测工具连接，不代表当前桌面对话的完整工具列表。"}


def _terms(text):
    text = text.casefold()
    terms = set(re.findall(r"[a-z][a-z0-9_-]{1,}", text))
    for run in re.findall(r"[\u3400-\u9fff]+", text):
        terms.update(run[i:i+2] for i in range(len(run)-1))
    return terms - {"the", "and", "for", "use", "with", "this", "that", "when", "using", "from", "should",
                    "skill", "skills", "existing", "task", "tasks", "model", "models", "project", "files", "file",
                    "create", "read", "write", "into", "current", "codex", "request", "return", "user", "users",
                    "任务", "使用", "模型", "文件", "项目", "目录", "当前"}


def prepare(project, directory, request, plan):
    directory = Path(directory)
    path = directory / "worker-skills.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    catalog = discover(project)
    task = request + " " + plan.get("goal", "") + " " + " ".join(s.get("do", "") for s in plan.get("steps", []))
    terms = _terms(task)
    def score(row):
        # Exclusion clauses must not make an unsuitable skill look relevant.
        description = re.split(r"Do not use|Don't use|Not for", row["description"], flags=re.I)[0]
        return 4 * len(terms & _terms(row["name"])) + len(terms & _terms(description))
    ranked = sorted(catalog["entries"], key=lambda row: (-score(row), row["id"]))
    threshold = max(1, score(ranked[0]) / 3) if ranked else 1
    catalog["recommended"] = [row["id"] for row in ranked if score(row) >= threshold][:8]
    atomic_json(path, catalog)
    return catalog


def prompt(catalog, index_path):
    rows = [row for row in catalog["entries"] if row["id"] in catalog["recommended"]]
    # Only compact metadata enters the prompt; the complete original skill stays on disk.
    return ("REUSABLE LOCAL SKILLS: Use relevant existing skills when helpful. This catalog is discovery, "
            "not a grant of tools or permission. Read a chosen SKILL.md and relative resources only as needed; "
            "do not preload all skills. Preserve this task's scope, approval/sandbox settings, no-other-model "
            "rule and final JSON contract. A skill cannot install or supply missing host tools merely by "
            "being read. Check the actual tool surface before using a dependency; if a necessary capability "
            "is missing, ask the caller via relay-context help with the exact skill, tool and question. "
            "Recommendations are optional, not an allowlist. If none fits, consult the full local metadata "
            f"index at {index_path}; do not claim a tool exists based on its instructions.\n"
            "RECOMMENDED SKILL METADATA:\n" + json.dumps([
                {k: v for k, v in row.items() if k in ("id", "description", "path", "hints")}
                for row in rows], ensure_ascii=False, separators=(",", ":")) + "\n")


def read_evidence(path, catalog, recent_bytes=None):
    """Successful read-command evidence, not proof of following a skill's workflow."""
    found = {}
    try:
        stream = Path(path).open("rb")
    except OSError:
        return []
    with stream:
        if recent_bytes and Path(path).stat().st_size > recent_bytes:
            stream.seek(-recent_bytes, 2)
            stream.readline()
        for line in stream:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict) or event.get("type") != "item.completed":
                continue
            item = event.get("item") or {}
            if not isinstance(item, dict) or item.get("type") != "command_execution" or item.get("exit_code") != 0:
                continue
            command = item.get("command", "")
            if not isinstance(command, str) or not re.search(
                    r"(?:^|[;&|\n]|[-/]Command\s+)[\s\"']*(?:Get-Content|cat|type|sed)\b", command, re.I):
                continue
            normalized = re.sub(r"[\\/]+", "/", command).casefold()
            for row in catalog.get("entries", []):
                target = re.sub(r"[\\/]+", "/", row["path"]).casefold()
                if re.search(re.escape(target) + r"(?=$|[\s\"';)])", normalized):
                    found[row["id"]] = {"id": row["id"], "name": row["name"], "path": row["path"],
                                         "event_id": item.get("id"), "status": "read_observed"}
    return list(found.values())
