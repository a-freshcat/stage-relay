"""Registered, frozen cleanup plans with recoverable moves.

Only known project scratch and project-owned orphan uploads are candidates.
A preview freezes exact candidates and file fingerprints in a server-side plan;
execution never widens it by recomputing. Recovery keeps the original path.
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from .domain import Invalid

RULES = [
    "仅本项目登记的临时目录及本项目拥有、跨项目均未引用的过期上传附件进入候选。",
    "预览冻结候选路径、大小和修改时间；执行仅使用已确认清单，不重新扩大范围。",
    "项目工作区、会话库、桥凭据与共享插件只列保留项，不递归扫描工作区。",
    "文件移入可恢复区并记录 manifest；恢复时逐项复核原路径与目标路径。",
]
PROTECTED = {"workspace.sqlite3", "workspace.sqlite3-journal", "workspace.sqlite3-wal", "workspace.sqlite3-shm", "bridge-credentials.json"}
OWNER_MARKER = ".stage-relay-owner.json"


def scratch_for(data_dir, project_id):
    """Create and register a scratch directory for a project the panel created.

    Only call this for a project this panel is creating now. Pre-existing tmp
    folders without a marker are unknown leftovers and must stay untouched, so
    nothing here ever adopts them.
    """
    scratch = Path(data_dir).resolve() / "tmp" / project_id
    marker = scratch / OWNER_MARKER
    if scratch.exists() and not marker.is_file():
        return scratch
    scratch.mkdir(parents=True, exist_ok=True)
    if not marker.is_file():
        marker.write_text(json.dumps({"project": project_id, "created": datetime.now(timezone.utc).isoformat(timespec="seconds")}, ensure_ascii=False), encoding="utf-8")
    return scratch


def _inside(root, path):
    try:
        return Path(path).resolve().is_relative_to(root)
    except (OSError, RuntimeError):
        return False


def _referenced(projects):
    refs = set()
    for project in projects:
        for issue in project.get("issues", []):
            for msg in issue.get("messages", []):
                refs.update(f.get("id") for f in msg.get("attachments", []) if f.get("id"))
        for msg in project.get("chat", []):
            refs.update(f.get("id") for f in msg.get("attachments", []) if f.get("id"))
    return refs


class Cleanup:
    def __init__(self, data_dir):
        self.data = Path(data_dir).resolve()
        self.plans = self.data / "cleanup-plans"
        self.recovery = self.data / "recovery"

    @staticmethod
    def _days(project, retention_days):
        days = retention_days if retention_days is not None else project.get("retention_days") or 7
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 365:
            raise Invalid("保留期请填写 1-365 天。")
        return days

    @staticmethod
    def _item(path, rel, kind, reason):
        stat = path.stat()
        return dict(path=str(path), rel=str(rel), size=stat.st_size, mtime_ns=stat.st_mtime_ns,
                    kind=kind, reason=reason)

    def preview(self, project, projects, retention_days=None):
        if not project:
            raise Invalid("任务不存在。")
        days = self._days(project, retention_days)
        cutoff = time.time() - days * 86400
        refs = _referenced(projects)
        items, retained = [], []
        scratch = self.data / "tmp" / project["id"]
        if scratch.is_dir():
            marker = scratch / ".stage-relay-owner.json"
            try:
                owner = json.loads(marker.read_text(encoding="utf-8")) if marker.is_file() else {}
            except (OSError, ValueError):
                owner = {}
            files = list(scratch.iterdir())
            if owner.get("project") == project["id"] and all(_inside(self.data, f) and f.is_file() for f in files) and files and max(f.stat().st_mtime for f in files) <= cutoff:
                for file in files:
                    items.append(self._item(file, file.relative_to(self.data), "项目临时文件", "登记归属且超过保留期"))
            else:
                retained.append(dict(path=str(scratch), size=None,
                                     reason="归属或保留期未确认，保留；没有递归扫描"))
        uploads = self.data / "uploads"
        if uploads.is_dir():
            for manifest in sorted(uploads.glob("*.json")):
                if manifest.is_symlink() or not _inside(self.data, manifest):
                    continue
                try:
                    meta = json.loads(manifest.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                file_id = manifest.stem
                if meta.get("id") != file_id or len(file_id) != 24 or any(c not in "0123456789abcdef" for c in file_id):
                    continue
                if meta.get("project") != project["id"]:
                    if not meta.get("project"):
                        retained.append(dict(path=str(uploads / file_id), size=None, reason="未登记为本项目附件，保留"))
                    continue
                file = uploads / file_id
                if not file.is_file() or file.is_symlink() or not _inside(self.data, file):
                    continue
                if file_id in refs or max(file.stat().st_mtime, manifest.stat().st_mtime) > cutoff:
                    retained.append(dict(path=str(file), size=file.stat().st_size,
                                         reason="被任一项目引用，属于交付记录" if file_id in refs else "附件仍在保留期内"))
                    continue
                items.append(self._item(file, file.relative_to(self.data), "本项目孤儿附件", "本项目登记且跨项目无引用、超过保留期"))
                items.append(self._item(manifest, manifest.relative_to(self.data), "附件登记", "同批恢复所需登记文件"))
        if project.get("workspace"):
            retained.append(dict(path=project["workspace"], size=None, reason="用户工作区仅展示路径，不扫描"))
        for name in sorted(PROTECTED):
            path = self.data / name
            if path.is_file():
                retained.append(dict(path=str(path), size=path.stat().st_size, reason="本应用状态或凭据，保留"))
        if self.recovery.is_dir():
            retained.append(dict(path=str(self.recovery), size=None, reason="可恢复区，保留"))
        plan = dict(id=secrets.token_hex(16), project=project["id"], title=project["title"],
                    retention_days=days, generated=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    expires=time.time() + 600, rules=RULES, items=items, retained=retained,
                    totals=dict(move_items=len(items), move_bytes=sum(i["size"] for i in items),
                                retained_items=len(retained), retained_bytes=sum(i["size"] or 0 for i in retained)),
                    closed=bool(project.get("closed")))
        self.plans.mkdir(exist_ok=True)
        (self.plans / f"{plan['id']}.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        return plan

    def _still_referenced(self, projects, item):
        """Re-check at execution time: references and ownership may have changed."""
        file_id = Path(item["path"]).stem
        for project in projects or []:
            for issue in project.get("issues", []):
                for message in issue.get("messages", []):
                    if any(f.get("id") == file_id for f in message.get("attachments", [])):
                        return "项目消息在预览后引用了此附件"
            for message in project.get("chat", []):
                if any(f.get("id") == file_id for f in message.get("attachments", [])):
                    return "项目消息在预览后引用了此附件"
        manifest = self.data / "uploads" / f"{file_id}.json"
        if manifest.is_file() and not item.get("settings_scope"):
            try:
                meta = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return "附件登记无法读取"
            if meta.get("project") != item.get("project"):
                return "附件归属已变化"
        return None

    def _claim(self, plan_file):
        """Atomically take ownership of a plan so two requests cannot both run it.

        A lock file created with O_EXCL is the only portable atomic primitive
        here; a plain read-then-write check can let two requests through.
        """
        lock = plan_file.with_suffix(".lock")
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            # A crash can leave a lock behind. Only reclaim it when it is far
            # older than any plausible run, and never while progress is moving.
            try:
                stale = time.time() - lock.stat().st_mtime > 900
            except OSError:
                stale = False
            if not stale:
                return None
            try:
                lock.unlink()
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except OSError:
                return None
        except OSError:
            return None
        try:
            os.write(fd, f"{os.getpid()} {time.time()}".encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        return lock

    def execute(self, project, projects, plan_id):
        if not project.get("closed"):
            raise Invalid("请先结束任务，再执行文件清理。")
        if not isinstance(plan_id, str) or len(plan_id) != 32 or any(c not in "0123456789abcdef" for c in plan_id):
            raise Invalid("请先生成清理预览。")
        plan_file = self.plans / f"{plan_id}.json"
        if not plan_file.is_file():
            raise Invalid("预览已过期、已执行或正在执行，请重新生成。")
        plan = json.loads(plan_file.read_text(encoding="utf-8"))
        if plan["project"] != project["id"] or plan["expires"] < time.time() or plan.get("executed"):
            raise Invalid("预览已过期或已执行，请重新生成。")
        claimed = self._claim(plan_file)
        if claimed is None:
            raise Invalid("另一个清理请求正在执行此清单，请稍后再试。")
        # Re-read only after the claim wins. A request can read the plan, wait,
        # then take over a reclaimed lock; without this second read it would act
        # on a plan that another run already completed.
        plan = json.loads(plan_file.read_text(encoding="utf-8"))
        if plan.get("executed") or plan["expires"] < time.time() or plan["project"] != project["id"]:
            claimed.unlink(missing_ok=True)
            raise Invalid("预览已被执行或已过期，请重新生成。")
        root = self.recovery / f"{plan_id}-{project['id'].replace('*', '')}"
        existing = root / "manifest.json"
        if existing.is_file():
            # Resume the previous run instead of refusing or overwriting: the
            # manifest is the record of what already moved.
            result = json.loads(existing.read_text(encoding="utf-8"))
            if result.get("project") != project["id"] or result.get("plan_id") != plan_id:
                claimed.unlink(missing_ok=True)
                raise Invalid("恢复清单与该清理计划不符，请重新预览。")
            for key in ("moved", "skipped", "failed"):
                result.setdefault(key, [])
        else:
            result = dict(plan_id=plan_id, project=project["id"], root=str(root), moved=[], skipped=[], failed=[])
        root.mkdir(parents=True, exist_ok=True)
        # Progress is written atomically after every item.
        def flush():
            staged = root / "manifest.json.tmp"
            staged.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(staged, root / "manifest.json")

        done = {str(Path(entry["rel"])) for key in ("moved", "skipped", "failed") for entry in result.get(key, [])}
        # A crash can land between the move and the manifest write. The recorded
        # intent is reconciled against the live source/target before continuing,
        # so an already-moved file is never reported missing nor moved twice.
        intent = result.get("intent")
        if intent:
            rel = Path(intent["rel"])
            source = Path(intent["from"])
            target = root / rel
            if not source.exists() and target.is_file():
                result["moved"].append(dict(intent["item"], to=str(target), reconciled=True))
                done.add(str(rel))
            elif source.is_file() and not target.exists():
                pass  # the move never happened; fall through and retry it
            elif source.is_file() and target.exists():
                result["failed"].append(dict(intent["item"], error="源与恢复目标同时存在，需人工确认"))
                done.add(str(rel))
            result.pop("intent", None)
            flush()
        for item in plan["items"]:
            item = dict(item, project=plan["project"])
            if str(Path(item["rel"])) in done:
                continue
            source = Path(item["path"])
            rel = Path(item["rel"])
            if rel.is_absolute() or ".." in rel.parts or not _inside(self.data, source) or source.name in PROTECTED or source.resolve() != (self.data / rel).resolve():
                result["failed"].append(item | {"error": "路径未通过归属校验"})
                flush()
                continue
            blocked = self._still_referenced(projects, item)
            if blocked:
                result["skipped"].append(item | {"reason": blocked})
                flush()
                continue
            if not source.is_file() or source.is_symlink():
                result["skipped"].append(item | {"reason": "候选已不存在或路径状态变化"})
                flush()
                continue
            stat = source.stat()
            if stat.st_size != item["size"] or stat.st_mtime_ns != item["mtime_ns"]:
                result["skipped"].append(item | {"reason": "预览后文件已变化"})
                flush()
                continue
            target = root / rel
            # Write the intent first: if this process dies mid-move, the next run
            # can tell "moved but unrecorded" from "never moved".
            result["intent"] = dict(rel=str(rel), **{"from": str(source)}, item=item)
            flush()
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(target))
            except OSError as exc:
                result["failed"].append(item | {"error": str(exc)})
                result.pop("intent", None)
            else:
                result["moved"].append(item | {"to": str(target)})
                result.pop("intent", None)
            flush()
        plan["executed"] = True
        plan["executed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        # Persist the claim on the plan itself: the lock only guards a running
        # process, while `executed` is what keeps a finished plan from ever
        # running twice, including after the lock goes stale.
        staged = plan_file.with_suffix(".staged")
        staged.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        staged.replace(plan_file)
        claimed.unlink(missing_ok=True)
        flush()
        return result

    @staticmethod
    def _tree(path):
        """Bytes and file count under one directory, without following links."""
        if path.is_symlink() or not path.is_dir():
            return 0, 0
        total = files = 0
        for entry in path.rglob("*"):
            if entry.is_symlink() or not entry.is_file():
                continue
            try:
                total += entry.stat().st_size
            except OSError:
                continue
            files += 1
        return total, files

    def storage_rows(self, projects):
        return self._storage(projects)

    def _storage(self, projects):
        """Per-project footprint this panel itself created.

        Only the panel's own folders are measured: upload attachments it stored,
        the scratch directory it registered, and its recovery area. The project
        workspace is never walked, and unreferenced folders are listed as unknown
        rather than claimed.
        """
        rows = {}
        def row(project_id, title, live):
            return rows.setdefault(project_id, dict(project=project_id, title=title, live=live,
                                                    uploads_bytes=0, uploads_count=0,
                                                    scratch_bytes=0, scratch_files=0,
                                                    recovery_bytes=0, recovery_items=0,
                                                    registered=False))
        for project in projects:
            row(project["id"], project.get("title"), True)
        uploads = self.data / "uploads"
        if uploads.is_dir():
            for manifest in uploads.glob("*.json"):
                file = uploads / manifest.stem
                if file.is_symlink() or not file.is_file():
                    continue
                try:
                    meta = json.loads(manifest.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                owner = meta.get("project")
                if not owner:
                    continue
                target = row(owner, None, any(p["id"] == owner for p in projects))
                size = file.stat().st_size
                target["uploads_bytes"] += size
                target["uploads_count"] += 1
                manifest_size = manifest.stat().st_size
                target["uploads_bytes"] += manifest_size
        tmp = self.data / "tmp"
        if tmp.is_dir():
            for scratch in tmp.iterdir():
                if not scratch.is_dir():
                    continue
                target = row(scratch.name, None, any(p["id"] == scratch.name for p in projects))
                target["registered"] = (scratch / OWNER_MARKER).is_file()
                size, files = self._tree(scratch)
                target["scratch_bytes"] += size
                target["scratch_files"] += files
        if self.recovery.is_dir():
            for folder in self.recovery.iterdir():
                if not folder.is_dir():
                    continue
                manifest = folder / "manifest.json"
                project_id = folder.name.rsplit("-", 1)[-1]
                target = row(project_id, None, any(p["id"] == project_id for p in projects))
                size, files = self._tree(folder)
                target["recovery_bytes"] += size
                target["recovery_items"] += files
        return sorted(rows.values(), key=lambda r: (-(r["uploads_bytes"] + r["scratch_bytes"] + r["recovery_bytes"]), str(r["project"])))

    def settings_preview(self, projects):
        """App-level leftovers, listed only by kind and by reference state.

        This is the "占用清理" view: it never scans the disk, never touches the
        session database or credentials, and only reports objects this panel
        itself created. Execution reuses the frozen-plan path, so the user always
        confirms an exact list before anything moves.
        """
        refs = _referenced(projects)
        live_projects = {p["id"] for p in projects}
        items, retained = [], []
        uploads = self.data / "uploads"
        if uploads.is_dir():
            for manifest in sorted(uploads.glob("*.json")):
                if manifest.is_symlink() or not _inside(self.data, manifest):
                    continue
                try:
                    meta = json.loads(manifest.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                file_id = manifest.stem
                file = uploads / file_id
                if not file.is_file() or file.is_symlink():
                    continue
                owner = meta.get("project")
                if file_id in refs:
                    retained.append(dict(path=str(file), size=file.stat().st_size, reason="被项目消息引用，属于交付记录"))
                elif owner and owner in live_projects:
                    retained.append(dict(path=str(file), size=file.stat().st_size, reason="所属项目仍在，附件交由该项目自行处理"))
                elif not owner:
                    retained.append(dict(path=str(file), size=file.stat().st_size, reason="未登记归属，保留待人工确认"))
                else:
                    items.append(self._item(file, file.relative_to(self.data), "孤儿附件", "所属项目已删除且无任何消息引用") | {"settings_scope": True, "project": owner})
                    items.append(self._item(manifest, manifest.relative_to(self.data), "附件登记", "同批恢复所需登记文件") | {"settings_scope": True, "project": owner})
        plans = self.plans
        if plans.is_dir():
            for plan_file in sorted(plans.glob("*.json")):
                try:
                    plan = json.loads(plan_file.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if plan.get("project") in live_projects and not plan.get("executed"):
                    retained.append(dict(path=str(plan_file), size=plan_file.stat().st_size, reason="有效清理计划，仍可执行"))
                else:
                    items.append(self._item(plan_file, plan_file.relative_to(self.data), "过期清理计划", "所属项目已删除或计划已执行"))
            for lock in sorted(plans.glob("*.lock")):
                if time.time() - lock.stat().st_mtime > 900:
                    items.append(self._item(lock, lock.relative_to(self.data), "残留锁", "超过 15 分钟无人持有的清理锁"))
        tmp = self.data / "tmp"
        if tmp.is_dir():
            for scratch in sorted(tmp.iterdir()):
                if not scratch.is_dir():
                    continue
                marker = scratch / OWNER_MARKER
                if scratch.name in live_projects:
                    retained.append(dict(path=str(scratch), size=None, reason="所属项目仍在，临时目录不在设置清理范围"))
                elif marker.is_file():
                    items.append(self._item(marker, marker.relative_to(self.data), "已删除项目的临时登记", "所属项目已删除"))
                else:
                    retained.append(dict(path=str(scratch), size=None, reason="未登记归属的未知目录，保留"))
        storage = self._storage(projects)
        session_db = self.data / "workspace.sqlite3"
        if session_db.is_file():
            retained.append(dict(path=str(session_db), size=session_db.stat().st_size, reason="会话数据库，只能在项目内确认后处理"))
        for name in sorted(PROTECTED):
            path = self.data / name
            if path.is_file() and str(path) != str(session_db):
                retained.append(dict(path=str(path), size=path.stat().st_size, reason="本应用凭据与状态，永不移动"))
        if self.recovery.is_dir():
            retained.append(dict(path=str(self.recovery), size=None, reason="可恢复区，保留"))
        plan = dict(id=secrets.token_hex(16), project="*settings*", title="应用占用清理",
                    scope="settings", retention_days=0,
                    generated=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    expires=time.time() + 600, rules=RULES, items=items, retained=retained,
                    totals=dict(move_items=len(items), move_bytes=sum(i["size"] for i in items),
                                retained_items=len(retained), retained_bytes=sum(i["size"] or 0 for i in retained)),
                    storage=storage, closed=True)
        self.plans.mkdir(exist_ok=True)
        (self.plans / f"{plan['id']}.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        return plan

    def recoveries(self, project_id):
        if not self.recovery.is_dir():
            return []
        results = []
        for manifest in self.recovery.glob(f"*-{project_id}/manifest.json"):
            if _inside(self.recovery, manifest):
                data = json.loads(manifest.read_text(encoding="utf-8"))
                results.append(dict(plan_id=data["plan_id"], project=project_id,
                                    moved=len(data.get("moved", [])), restored=len(data.get("restored", []))))
        return results

    def restore(self, project_id, plan_id):
        if not isinstance(plan_id, str) or len(plan_id) != 32 or any(c not in "0123456789abcdef" for c in plan_id):
            raise Invalid("恢复清单编号无效。")
        root = self.recovery / f"{plan_id}-{project_id}"
        manifest = root / "manifest.json"
        if not manifest.is_file() or not _inside(self.recovery, manifest):
            raise Invalid("找不到此项目的恢复清单。")
        result = json.loads(manifest.read_text(encoding="utf-8"))
        if result.get("project") != project_id or result.get("plan_id") != plan_id:
            raise Invalid("恢复清单归属不符。")
        restored_ids = set(result.get("restored", []))
        failed = list(result.get("restore_failed", []))
        restored, skipped = [], []
        pending = [item for item in result["moved"] if str(Path(item["rel"])) not in restored_ids]
        # A crash between the file move and the manifest write is reconciled from
        # the live paths: a file already back at its original place counts as
        # restored instead of being reported missing from the recovery area.
        pending_rel = {str(Path(item["rel"])) for item in pending}
        for item in pending:
            rel = Path(item["rel"])
            if (self.data / rel).is_file() and not (root / rel).exists():
                restored.append(str(rel))
        if restored:
            result["restored"] = sorted(restored_ids | set(restored))
            result["restore_failed"] = failed
            staged = root / "manifest.json.tmp"
            staged.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(staged, manifest)
        pending = [item for item in pending if str(Path(item["rel"])) not in set(restored)]
        # A recreated file means the user already worked there: refuse that item,
        # keep restoring the rest, and persist progress after every move so a
        # crash never loses what already came back.
        for item in pending:
            rel = Path(item["rel"])
            source = root / rel
            target = self.data / rel
            problem = None
            if rel.is_absolute() or ".." in rel.parts or not _inside(root, source) or not _inside(self.data, target):
                problem = "路径校验失败"
            elif target.exists():
                problem = "原位置已有同名文件"
            elif not source.is_file():
                problem = "恢复区文件不存在"
            if problem:
                skipped.append(dict(rel=str(rel), reason=problem))
                continue
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(target))
            except OSError as exc:
                failed.append(dict(rel=str(rel), error=str(exc)))
                result["restore_failed"] = failed
                staged = root / "manifest.json.tmp"
                staged.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                os.replace(staged, manifest)
                continue
            restored.append(str(rel))
            result["restored"] = sorted(restored_ids | set(restored))
            result["restore_failed"] = failed
            staged = root / "manifest.json.tmp"
            staged.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(staged, manifest)
        result["restored"] = sorted(restored_ids | set(restored))
        result["restore_failed"] = failed
        staged = root / "manifest.json.tmp"
        staged.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(staged, manifest)
        return dict(project=project_id, plan_id=plan_id, restored=restored, skipped=skipped, failed=failed)
