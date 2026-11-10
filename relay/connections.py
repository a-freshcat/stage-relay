"""Reviewable, consent-based configuration of already-installed applications.

Prepares an exact change without writing. Commit checks that the file has not
changed, saves a sibling backup, and atomically installs only our server entry.
No application is stopped, no model is called, and no secret is sent to the UI.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path

from .cleanup import atomic_replace
from .domain import Invalid
from .native_apps import application_target, dsh_runtime

ROOT = Path(__file__).resolve().parent.parent


class Connections:
    def __init__(self, data_dir, port, home=None, yaml_module=None):
        self.data_dir = Path(data_dir).resolve()
        self.port = port
        self.home = Path(home or Path.home()).resolve()
        self.dsh_home = Path(os.environ.get("DSH_HOME", str(self.home / ".dsh"))) if home is None else self.home / ".dsh"
        self.codex_home = Path(os.environ.get("CODEX_HOME", str(self.home / ".codex"))) if home is None else self.home / ".codex"
        self.yaml_module = Path(yaml_module) if yaml_module else self.dsh_home / "profiles/desktop/node_modules/yaml"
        self.plans = {}
        self.owned = {}
        self.journal = self.data_dir / "connection-ownership.json"
        self.reload_owned()

    def reload_owned(self):
        """Take the journal on disk as the record of what this panel wrote.

        A running panel can outlive a change made by another process — a one-shot
        adoption, or a newer panel on the same store. Holding the first reading in
        memory makes a legitimately adopted entry look like a stranger's, and the
        panel then refuses with a conflict message the user cannot resolve. The
        journal is the shared record, so it is re-read whenever ownership is
        consulted, not cached for the life of the process.
        """
        try:
            self.owned = (json.loads(self.journal.read_text(encoding="utf-8"))
                          if self.journal.exists() else {})
        except (OSError, ValueError):
            # A journal that cannot be read is not evidence of ownership; keep
            # what is known rather than treating every entry as ours.
            return self.owned

    def current_entry(self, agent):
        """The bridge this running package ships, for this agent and store."""
        args = ["--agent", agent["id"], "--port", str(self.port), "--data-dir", str(self.data_dir)]
        if getattr(sys, "frozen", False):
            bridge = Path(sys.executable).resolve().parent / "StageRelayBridge.exe"
            if not bridge.is_file():
                raise Invalid("协作桥文件缺失，请保留完整的应用文件夹。")
            return {"command": str(bridge), "args": args}
        return {"command": sys.executable, "args": [str(ROOT / "bridge.py")] + args}

    @staticmethod
    def contract(args):
        """The part of the bridge arguments the user actually approved.

        `--agent`, `--port` and `--data-dir` decide which member, which panel and
        which store: that is the authorization. The executable in front of them
        is packaging, and comparing it would turn every upgrade into a request to
        authorize again — while keeping it pinned would freeze the tool catalogue.
        """
        if not isinstance(args, list):
            return None
        found = {}
        for index, item in enumerate(args[:-1]):
            if item in {"--agent", "--port", "--data-dir"}:
                found[item] = str(args[index + 1])
        return found if len(found) == 3 else None

    def entry(self, agent):
        # The journal records *consent*: this agent, this port, this data store.
        # The executable must follow the package that is running, not the package
        # that was running when the user consented: the bridge binary carries the
        # tool catalogue, so a pinned old bridge keeps advertising old tools and
        # a newly added tool is invisible however often the panel is reloaded.
        return self.current_entry(agent)

    def refresh(self, agent):
        """Re-point an already-authorized entry at this package's bridge.

        Only the path changes; the approval the user gave stays valid, the file
        is backed up first, and a failure leaves the existing configuration
        untouched. Returns None when nothing needed to change.
        """
        owned = self.owned.get(agent["id"], {}).get("entry")
        if not isinstance(owned, dict):
            return None
        current = self.current_entry(agent)
        if self.contract(owned.get("args")) != self.contract(current["args"]):
            return None
        if str(owned.get("command")) == current["command"] and owned.get("args") == current["args"]:
            return None
        plan = self.prepare(agent)
        return self.commit(plan["plan_id"], True)



    def target(self, agent):
        if agent["app"] == "codex":
            return self.codex_home / "config.toml", "toml"
        if agent["app"] == "antigravity":
            modern = self.home / ".gemini/config/mcp_config.json"
            legacy = self.home / ".gemini/antigravity/mcp_config.json"
            return (modern if modern.exists() or not legacy.exists() else legacy), "json"
        if agent["app"] == "dsh":
            target = self.dsh_home / "profiles/desktop/cordis.patch.yml"
            if not target.parent.is_dir():
                raise Invalid("还没检测到 DSH 桌面配置。请先启动一次 DeepSeek Harness，再回来连接。")
            return target, "yaml"
        raise Invalid("暂不支持这个应用。")

    @staticmethod
    def digest(raw):
        return hashlib.sha256(raw).hexdigest()

    def validate_yaml(self, source):
        node = shutil.which("node")
        if not node or not self.yaml_module.is_dir():
            raise Invalid("未找到 DSH 的配置校验组件，请先启动或更新 DSH 后重新检测。")
        script = "const Y=require(process.argv[1]);let s='';process.stdin.setEncoding('utf8');process.stdin.on('data',c=>s+=c);process.stdin.on('end',()=>{const d=Y.parseDocument(s);process.stdout.write(JSON.stringify({ok:!d.errors.length&&Y.isSeq(d.contents)}))});"
        try:
            result = subprocess.run([node, "-e", script, str(self.yaml_module)], input=source.encode("utf-8"), capture_output=True, timeout=8, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            valid = result.returncode == 0 and json.loads(result.stdout)["ok"]
        except (OSError, ValueError, subprocess.TimeoutExpired):
            valid = False
        if not valid:
            raise Invalid("当前 DSH 配置格式无法安全自动修改，未写入任何内容。请更新 DSH 后重试。")

    def build(self, agent, raw, kind, remove=False, adopt=None):
        source = raw.decode("utf-8-sig")
        server = "stage-relay-" + agent["id"]
        entry = self.entry(agent)
        if kind == "json":
            content = json.loads(source) if source.strip() else {}
            if not isinstance(content, dict) or not isinstance(content.get("mcpServers", {}), dict):
                raise Invalid("应用配置格式不兼容，未修改文件。")
            servers = content.setdefault("mcpServers", {})
            current = servers.get(server)
            owned = self.owned.get(agent["id"], {}).get("entry")
            if current is not None and current != owned:
                # An entry this panel did not write. It may be a leftover from an
                # older build, but it is still someone else's configuration: the
                # caller has to say which exact entry it checked. A wildcard or a
                # blanket "yes" would make adoption indistinguishable from
                # silently overwriting whatever is on disk.
                if adopt is None:
                    raise Invalid("检测到同名配置已被其他操作修改，为保留现有配置，请重新核对。")
                if adopt.get("server") != server:
                    raise Invalid("接管确认的条目名与目标不一致，未修改文件。")
                if adopt.get("digest") != self.digest(json.dumps(current, sort_keys=True, ensure_ascii=False).encode("utf-8")):
                    raise Invalid("磁盘上的同名配置与核对时不一致，请重新核对；尚未写入内容。")
            if remove:
                servers.pop(server, None)
            else:
                servers[server] = entry
            return (json.dumps(content, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        begin = f"# BEGIN Stage Relay: {agent['id']}"
        end = f"# END Stage Relay: {agent['id']}"
        pattern = re.compile(r"^" + re.escape(begin) + r"\r?\n.*?^" + re.escape(end) + r"(?:\r?\n|$)", re.M | re.S)
        if begin in source and not pattern.search(source):
            raise Invalid("接入配置标记不完整，未修改文件。")
        cleaned = pattern.sub("", source)
        if kind == "toml":
            content = tomllib.loads(cleaned)
            if server in content.get("mcp_servers", {}):
                raise Invalid("已有同名服务不归此面板管理，未覆盖配置。")
            block = f"[{f'mcp_servers.{server}'}]\ncommand = {json.dumps(entry['command'])}\nargs = {json.dumps(entry['args'], ensure_ascii=False)}\nenabled = true\n"
            updated = cleaned if remove else cleaned.rstrip() + f"\n\n{begin}\n{block}{end}\n"
            tomllib.loads(updated)
        else:
            if re.search(r"^\s*-\s*id:\s*['\"]?" + re.escape(server) + r"['\"]?\s*$", cleaned, re.M):
                raise Invalid("已有同名 DSH 配置，未覆盖。")
            if not remove:
                cleaned = re.sub(r"^\[\]\s*$", "", cleaned, flags=re.M)
            # Cordis supports one independently scoped MCP client entry per server.
            block = f"- id: {server}\n  name: '@deepseek-ai/dsh-mcp-client'\n  config:\n    serverName: {server}\n    transport: stdio\n    command: {json.dumps(entry['command'])}\n    args: {json.dumps(entry['args'], ensure_ascii=False)}\n"
            if not remove:
                runtime=dsh_runtime()
                plugin=ROOT/'integrations/dsh/index.mjs'
                if not plugin.is_file():
                    raise Invalid('DSH 原生连接插件缺失，请使用完整应用文件夹。')
                native_config={'panelUrl':f'http://127.0.0.1:{self.port}','agentId':agent['id'],'credentialsFile':str(self.data_dir/'bridge-credentials.json'),'runtimeRoot':str(runtime)}
                plugin_uri=plugin.as_uri()+'?v='+self.digest(plugin.read_bytes())[:16]
                block+=f"- id: {server}-native\n  name: {json.dumps(plugin_uri)}\n  config: {json.dumps(native_config,ensure_ascii=False)}\n"
                # cordis.patch.yml contains patch operations, not loader entries.
                # A plain id row only changes an existing entry and silently skips
                # new ids. The official root insert operation adds both entries.
                block='- insert:\n'+''.join('  '+line+'\n' for line in block.splitlines())
            updated = cleaned if remove else cleaned.rstrip() + f"\n\n{begin}\n{block}{end}\n"
            has_rows = any(line.strip() and not line.lstrip().startswith("#") for line in updated.splitlines())
            self.validate_yaml(updated if has_rows else updated + "\n[]\n")
        return updated.encode("utf-8")

    def prepare(self, agent, remove=False, adopt=None):
        # Ownership is a shared on-disk record; re-read it before deciding that an
        # entry is somebody else's.
        self.reload_owned()
        target, kind = self.target(agent)
        # Refuse symlink redirection before accepting any configuration write.
        if target.is_symlink() or target.parent.resolve() != target.parent.absolute():
            raise Invalid("配置路径存在重定向，无法安全自动接入。")
        raw = target.read_bytes() if target.exists() else b""
        try:
            updated = self.build(agent, raw, kind, remove, adopt)
        except (ValueError, UnicodeError) as exc:
            if isinstance(exc, Invalid):
                raise
            raise Invalid("现有应用配置无法解析，未修改文件。") from None
        plan_id = secrets.token_urlsafe(24)
        self.plans = {k:v for k,v in self.plans.items() if time.time()-v["created"]<600}
        self.plans[plan_id] = {"agent": dict(agent), "path": target, "original": raw, "updated": updated, "created": time.time(), "remove": remove, "exists": target.exists(), "adopt": adopt}
        return {"plan_id": plan_id, "agent_id": agent["id"], "app": agent["app"], "detected": target.parent.exists(), "path": str(target), "already_configured": agent["id"] in self.owned, "remove": remove, "launch_available": bool(application_target(agent["app"])),
                "permissions": ["读取分配给此成员的任务和议题", "汇报进度、参与群聊、提交交付依据", "检测原生会话及实际模型；向你绑定的会话派发任务和协作消息" if agent['app']=='dsh' else ("启动本机 Codex 原生 harness 后台会话，沿用配置、登录及可用插件；桌面专属连接器需分别核验" if agent['app']=='codex' else "登记协作工具；此应用的原生唤醒尚未支持"), "在应用中登记本地协作工具，并备份现有配置"],
                "notice": "配置后应用需要重新加载工具；收到应用响应前，会显示等待确认。不会替你重启正在工作的应用。"}

    def commit(self, plan_id, consent):
        if consent is not True:
            raise Invalid("请先授权本次接入。")
        plan = self.plans.get(plan_id)
        if not plan or time.time()-plan["created"]>600:
            raise Invalid("接入检查已过期，请重新检测。")
        target = plan["path"]
        current = target.read_bytes() if target.exists() else b""
        if current != plan["original"] or target.exists() != plan["exists"]:
            raise Invalid("应用配置刚刚发生变化，请重新检测；尚未写入内容。")
        if target.is_symlink() or target.parent.resolve() != target.parent.absolute():
            raise Invalid("配置路径发生变化，请重新检测。")
        target.parent.mkdir(parents=True, exist_ok=True)
        backup = None
        if target.exists():
            backup = target.with_name(target.name + f".stage-relay-{time.time_ns()}.bak")
            backup.write_bytes(current)
        temp = target.with_name(target.name + ".stage-relay-" + secrets.token_hex(4) + ".tmp")
        try:
            temp.write_bytes(plan["updated"])
            # Recheck just before replacing; never intentionally overwrite a stale plan.
            if (target.read_bytes() if target.exists() else b"") != current:
                raise Invalid("应用同时修改了配置，接入未完成，请重新检测。")
            # A just-closed reader or an indexer can hold the target for a few
            # milliseconds on Windows; the same retrying rename the cleanup path
            # uses keeps an atomic write atomic without turning it into a failure.
            atomic_replace(temp, target)
        finally:
            if temp.exists():
                temp.unlink()
        agent = plan["agent"]
        # The user's authorization is "登记 ownership 之前必须先验证". So read the
        # file back and prove three things before touching the journal: the JSON
        # still parses, our entry is exactly what we intended to write, and every
        # other server is byte-identical to the snapshot taken before writing.
        problem = self.verify_written(target, plan, agent)
        if problem:
            # Never record success for a write we could not prove. Roll back only
            # while the file is still exactly what this commit wrote: a third
            # party may have edited it since, and that must not be discarded.
            rolled = self.rollback(target, plan["updated"], backup)
            del self.plans[plan_id]
            raise Invalid(f"写入后校验未通过：{problem}。"
                          f"{'已回滚到写入前内容。' if rolled else '目标已被第三方修改，未回滚，备份保留。'}")
        if plan["remove"]:
            self.owned.pop(agent["id"], None)
        else:
            self.owned[agent["id"]] = {"entry": self.entry(agent), "path": str(target)}
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.journal.write_text(json.dumps(self.owned, ensure_ascii=False), encoding="utf-8")
        del self.plans[plan_id]
        return {"agent_id": agent["id"], "configured": not plan["remove"], "path": str(target),
                "backup": str(backup) if backup else None, "verified": True,
                "notice": "已撤销接入配置。" if plan["remove"] else "配置已写入并通过读回校验；应用重新加载后才会响应。"}

    def verify_written(self, target, plan, agent):
        """Returns a problem description, or None when the write is proved good."""
        try:
            written = target.read_bytes()
        except OSError as error:
            return f"无法读回配置：{error}"
        if written != plan["updated"]:
            return "读回内容与计划写入的内容不一致"
        server = "stage-relay-" + agent["id"]
        text = written.decode("utf-8")
        try:
            kind = self.target(agent)[1]
        except Invalid as error:
            return str(error)
        if kind == "toml":
            # Codex keeps toml under mcp_servers; the marker block is the record.
            try:
                content = tomllib.loads(text)
            except (ValueError, UnicodeError):
                return "读回的配置不是合法 TOML"
            present = server in content.get("mcp_servers", {})
            if plan["remove"]:
                if present:
                    return f"撤销后仍存在 {server}"
            elif not present:
                return f"{server} 未写入"
            marker = f"# BEGIN Stage Relay: {agent['id']}"
            if not plan["remove"] and marker not in text:
                return "写入的标记块缺失"
            return None
        if kind != "json":
            # DSH keeps an independently scoped entry; presence is what the
            # panel owns, and re-parsing its YAML here would duplicate the
            # validator that prepare already ran.
            present = re.search(r"^\s*-\s*id:\s*['\"]?" + re.escape(server), text, re.M) is not None
            if plan["remove"] and present:
                return f"撤销后仍存在 {server}"
            if not plan["remove"] and not present:
                return f"{server} 未写入"
            return None
        try:
            after = json.loads(text)
        except (ValueError, UnicodeError):
            return "读回的配置不是合法 JSON"
        servers = after.get("mcpServers")
        if not isinstance(servers, dict):
            return "读回的配置缺少 mcpServers"
        if plan["remove"]:
            if server in servers:
                return f"撤销后仍存在 {server}"
        elif servers.get(server) != self.entry(agent):
            return f"{server} 与预期的桥接条目不一致"
        try:
            before = json.loads((plan["original"] or b"{}").decode("utf-8"))
        except (ValueError, UnicodeError):
            before = {}
        untouched = {k: v for k, v in (before.get("mcpServers") or {}).items() if k != server}
        actual = {k: v for k, v in servers.items() if k != server}
        if actual != untouched:
            changed = sorted(set(actual) ^ set(untouched))
            return f"其他 MCP 条目被改动：{', '.join(changed) or '内容不一致'}"
        return None

    def rollback(self, target, written, backup):
        """Restore only when the target is still what this commit wrote."""
        try:
            if target.read_bytes() != written:
                return False
        except OSError:
            return False
        if not backup or not backup.exists():
            return False
        temp = target.with_name(target.name + ".stage-relay-" + secrets.token_hex(4) + ".tmp")
        try:
            temp.write_bytes(backup.read_bytes())
            atomic_replace(temp, target)
            return True
        except OSError:
            return False
        finally:
            if temp.exists():
                try:
                    temp.unlink()
                except OSError:
                    pass
