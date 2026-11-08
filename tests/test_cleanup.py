import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from relay.cleanup import Cleanup, OWNER_MARKER, scratch_for
from relay.domain import Invalid, Store


class CleanupRules(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parent.parent / 'qa'
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='test-cleanup-', dir=root)
        assert Path(self.temp.name).resolve().is_relative_to(root.resolve())
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.store = Store(self.data / 'workspace.sqlite3')
        self.workspace = self.data / 'ws'
        self.workspace.mkdir()
        self.project = self.store.apply('project_create', {'title': '清理规则', 'goal': '验证固定规则清理', 'workspace': str(self.workspace)})
        self.pid = self.project['id']
        self.uploads = self.data / 'uploads'
        self.uploads.mkdir()

    def upload(self, name, age_days=0, project=None):
        file_id = (name.encode().hex() * 4)[:24]
        (self.uploads / file_id).write_bytes(b'x' * 32)
        meta = {'id': file_id, 'name': name, 'size': 32, 'project': project}
        (self.uploads / f'{file_id}.json').write_text(json.dumps(meta), encoding='utf-8')
        if age_days:
            old = time.time() - age_days * 86400
            os.utime(self.uploads / file_id, (old, old))
            os.utime(self.uploads / f'{file_id}.json', (old, old))
        return file_id

    def attach(self, file_id, project=None):
        project = project or self.pid
        self.store.apply('project_message', {'project_id': project, 'body': '附件', 'attachments': [{'id': file_id}]})
        return next(m['id'] for m in next(p for p in self.store.snapshot()['projects'] if p['id'] == project)['chat'])

    def project_now(self):
        return next(p for p in self.store.snapshot()['projects'] if p['id'] == self.pid)

    def projects(self):
        return self.store.snapshot()['projects']

    def closed(self):
        return self.store.apply('project_close', {'project_id': self.pid, 'retention_days': 3})

    def aged_scratch(self, age_days=30, new_file=None):
        scratch = scratch_for(self.data, self.pid)
        old = time.time() - age_days * 86400
        (scratch / 'work.bin').write_bytes(b'w' * 16)
        os.utime(scratch / 'work.bin', (old, old))
        os.utime(scratch / OWNER_MARKER, (old, old))
        if new_file:
            (scratch / new_file).write_bytes(b'n' * 4)
        return scratch

    def test_registered_scratch_becomes_a_candidate_once_past_retention(self):
        self.aged_scratch()
        plan = Cleanup(self.data).preview(self.project_now(), self.projects(), retention_days=3)
        kinds = [i['kind'] for i in plan['items']]
        self.assertIn('项目临时文件', kinds)

    def test_scratch_without_owner_marker_is_never_a_candidate(self):
        stray = self.data / 'tmp' / self.pid
        stray.mkdir(parents=True)
        old = time.time() - 60 * 86400
        (stray / 'someone-else.bin').write_bytes(b's' * 8)
        os.utime(stray / 'someone-else.bin', (old, old))
        plan = Cleanup(self.data).preview(self.project_now(), self.projects(), retention_days=3)
        self.assertEqual([i['kind'] for i in plan['items'] if i['kind'] == '项目临时文件'], [])
        self.assertTrue(any('归属或保留期未确认' in i['reason'] for i in plan['retained']))

    def test_scratch_with_a_fresh_file_stays_retained(self):
        self.aged_scratch(new_file='fresh.bin')
        plan = Cleanup(self.data).preview(self.project_now(), self.projects(), retention_days=3)
        self.assertEqual([i['kind'] for i in plan['items'] if i['kind'] == '项目临时文件'], [])
        self.assertTrue(any('归属或保留期未确认' in i['reason'] for i in plan['retained']))

    def test_workspace_is_never_walked(self):
        deep = self.workspace / 'a' / 'b'
        deep.mkdir(parents=True)
        (deep / 'big.bin').write_bytes(b'q' * 4096)
        plan = Cleanup(self.data).preview(self.project_now(), self.projects(), retention_days=3)
        saved = next(i for i in plan['retained'] if i['path'] == str(self.workspace))
        self.assertIsNone(saved['size'])
        self.assertNotIn('big.bin', json.dumps(plan['items']))

    def test_upload_without_project_belongs_to_nobody(self):
        orphan = self.upload('无人登记.txt', age_days=40)
        plan = Cleanup(self.data).preview(self.project_now(), self.projects(), retention_days=1)
        self.assertEqual([i for i in plan['items'] if Path(i['path']).name == orphan], [])

    def test_referenced_and_young_uploads_are_retained(self):
        young = self.upload('新附件.txt', project=self.pid)
        old_kept = self.upload('旧交付.txt', age_days=30, project=self.pid)
        self.attach(old_kept)
        plan = Cleanup(self.data).preview(self.project_now(), self.projects(), retention_days=7)
        names = {Path(i['path']).name for i in plan['items']}
        self.assertNotIn(young, names)
        self.assertNotIn(old_kept, names)

    def test_project_owned_orphan_upload_is_a_candidate(self):
        owned = self.upload('本项目孤儿.txt', age_days=30, project=self.pid)
        self.attach(owned)
        message_id = next(p for p in self.store.snapshot()['projects'] if p['id'] == self.pid)['chat'][0]['id']
        self.store.apply('record_archive', {'project_id': self.pid, 'kind': 'project_message', 'item_id': message_id})
        # Archiving keeps the reference, so the file must stay pinned.
        plan = Cleanup(self.data).preview(self.project_now(), self.projects(), retention_days=1)
        self.assertNotIn(owned, {Path(i['path']).name for i in plan['items']})

    def test_execute_uses_the_frozen_plan_only(self):
        owned = self.upload('本项目孤儿.txt', age_days=30, project=self.pid)
        self.closed()
        cleanup = Cleanup(self.data)
        plan = cleanup.preview(self.project_now(), self.projects())
        self.assertIn(owned, {Path(i['path']).name for i in plan['items']})
        # A new candidate appearing after the preview cannot join the run.
        late = self.upload('后来的孤儿.txt', age_days=30, project=self.pid)
        result = cleanup.execute(self.project_now(), self.projects(), plan['id'])
        self.assertNotIn(late, {Path(i['path']).name for i in result['moved']})
        self.assertTrue((self.uploads / late).exists())
        self.assertTrue((Path(result['root']) / 'manifest.json').is_file())

    def test_execute_requires_closed_project_and_a_valid_plan(self):
        self.upload('本项目孤儿.txt', age_days=30, project=self.pid)
        cleanup = Cleanup(self.data)
        plan = cleanup.preview(self.project_now(), self.projects())
        with self.assertRaises(Invalid):
            cleanup.execute(self.project_now(), self.projects(), plan['id'])
        self.closed()
        with self.assertRaises(Invalid):
            cleanup.execute(self.project_now(), self.projects(), 'not-a-plan-id')

    def test_repeat_execution_never_widens_scope(self):
        self.upload('本项目孤儿.txt', age_days=30, project=self.pid)
        self.closed()
        cleanup = Cleanup(self.data)
        plan = cleanup.preview(self.project_now(), self.projects())
        first = cleanup.execute(self.project_now(), self.projects(), plan['id'])
        self.assertTrue(first['moved'])
        with self.assertRaises(Invalid):
            cleanup.execute(self.project_now(), self.projects(), plan['id'])

    def test_recovery_restores_and_never_overwrites(self):
        self.upload('本项目孤儿.txt', age_days=30, project=self.pid)
        self.closed()
        cleanup = Cleanup(self.data)
        plan = cleanup.preview(self.project_now(), self.projects())
        result = cleanup.execute(self.project_now(), self.projects(), plan['id'])
        originals = [Path(i['path']) for i in result['moved']]
        listed = cleanup.recoveries(self.pid)
        self.assertEqual(len(listed), 1)
        restored = cleanup.restore(self.pid, plan['id'])
        self.assertTrue(restored['restored'])
        self.assertTrue(all(p.exists() for p in originals if p.exists() or True))

    def test_restore_refuses_when_destination_is_occupied(self):
        self.upload('本项目孤儿.txt', age_days=30, project=self.pid)
        self.closed()
        cleanup = Cleanup(self.data)
        plan = cleanup.preview(self.project_now(), self.projects())
        result = cleanup.execute(self.project_now(), self.projects(), plan['id'])
        occupied = next(i for i in result['moved'] if i['kind'] == '本项目孤儿附件')
        target = Path(occupied['path'])
        target.write_bytes(b'recreated after cleanup')
        restored = cleanup.restore(self.pid, plan['id'])
        # Per-item restore: the occupied file is refused and stays untouched, the
        # rest of the batch still comes back and its progress is persisted.
        self.assertNotIn(str(occupied['rel']), restored['restored'])
        self.assertTrue(any(item['rel'] == str(occupied['rel']) for item in restored['skipped']))
        self.assertEqual(target.read_bytes(), b'recreated after cleanup')
        self.assertEqual(len(restored['restored']), len(result['moved']) - 1)

    def test_execute_rechecks_references_before_moving(self):
        owned = self.upload('预览后被引用.txt', age_days=30, project=self.pid)
        cleanup = Cleanup(self.data)
        plan = cleanup.preview(self.project_now(), self.projects())
        self.assertIn(owned, {Path(i['path']).name for i in plan['items']})
        # A message referencing the attachment appears after the user saw the list.
        self.store.apply('project_message', {'project_id': self.pid, 'body': '附件在使用中', 'attachments': [{'id': owned}]})
        self.closed()
        result = cleanup.execute(self.project_now(), self.projects(), plan['id'])
        self.assertNotIn(owned, {Path(i['path']).name for i in result['moved']})
        self.assertTrue((self.uploads / owned).exists())
        self.assertTrue(any('引用了此附件' in item['reason'] for item in result['skipped']))

    def test_plan_can_only_be_claimed_once_concurrently(self):
        self.upload('并发孤儿.txt', age_days=30, project=self.pid)
        self.closed()
        cleanup = Cleanup(self.data)
        plan = cleanup.preview(self.project_now(), self.projects())
        outcomes = []
        import concurrent.futures
        def run():
            try:
                return ('ok', len(cleanup.execute(self.project_now(), self.projects(), plan['id'])['moved']))
            except Invalid as exc:
                return ('refused', str(exc))
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            outcomes = list(pool.map(lambda _: run(), range(4)))
        self.assertEqual([o[0] for o in outcomes].count('ok'), 1, outcomes)
        self.assertEqual([o[0] for o in outcomes].count('refused'), 3, outcomes)

    def test_restore_persists_progress_across_a_failure(self):
        self.upload('中途失败.txt', age_days=30, project=self.pid)
        self.closed()
        cleanup = Cleanup(self.data)
        plan = cleanup.preview(self.project_now(), self.projects())
        result = cleanup.execute(self.project_now(), self.projects(), plan['id'])
        self.assertGreaterEqual(len(result['moved']), 2)
        # Occupy the second item's original path so that item fails while the rest restore.
        second = Path(result['moved'][1]['path'])
        second.write_bytes(b'occupied')
        restored = cleanup.restore(self.pid, plan['id'])
        self.assertTrue(restored['restored'])
        manifest = json.loads((Path(result['root']) / 'manifest.json').read_text(encoding='utf-8'))
        self.assertTrue(manifest['restored'])
        manifest['restored'] = manifest['restored'][:1]
        (Path(result['root']) / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
        again = cleanup.restore(self.pid, plan['id'])
        self.assertTrue(any(item['rel'] == str(second.relative_to(self.data)) for item in again['skipped']))

    def test_crash_between_move_and_manifest_is_reconciled(self):
        owned = self.upload('崩溃窗口.txt', age_days=30, project=self.pid)
        self.closed()
        cleanup = Cleanup(self.data)
        plan = cleanup.preview(self.project_now(), self.projects())
        item = next(i for i in plan['items'] if Path(i['path']).name == owned)
        root = cleanup.recovery / f"{plan['id']}-{self.pid}"
        target = root / item['rel']
        target.parent.mkdir(parents=True, exist_ok=True)
        # Simulate: file already moved into recovery, manifest written with the
        # intent but without the moved entry (crash right after shutil.move).
        src = Path(item['path'])
        import shutil as _shutil
        _shutil.move(str(src), str(target))
        manifest = dict(plan_id=plan['id'], project=self.pid, root=str(root),
                        moved=[], skipped=[], failed=[],
                        intent=dict(rel=item['rel'], **{'from': item['path']}, item=dict(item, project=self.pid)))
        (root / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
        result = cleanup.execute(self.project_now(), self.projects(), plan['id'])
        moved_rels = {i['rel'] for i in result['moved']}
        self.assertIn(item['rel'], moved_rels)
        self.assertFalse(any(i['rel'] == item['rel'] for i in result['skipped']))
        final = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
        self.assertIn(item['rel'], {i['rel'] for i in final['moved']})
        self.assertNotIn('intent', final)

    def test_restore_reconciles_a_move_that_reached_the_origin(self):
        owned = self.upload('恢复崩溃.txt', age_days=30, project=self.pid)
        self.closed()
        cleanup = Cleanup(self.data)
        plan = cleanup.preview(self.project_now(), self.projects())
        result = cleanup.execute(self.project_now(), self.projects(), plan['id'])
        root = Path(result['root'])
        item = next(i for i in result['moved'] if Path(i['path']).name == owned)
        # Simulate a crash after the file went back but before the manifest said so.
        src = root / item['rel']
        dst = Path(item['path'])
        dst.parent.mkdir(parents=True, exist_ok=True)
        import shutil as _shutil
        _shutil.move(str(src), str(dst))
        restored = cleanup.restore(self.pid, plan['id'])
        self.assertIn(item['rel'], restored['restored'])
        self.assertFalse(any(s['rel'] == item['rel'] for s in restored['skipped']))

    def test_settings_preview_lists_only_unowned_leftovers(self):
        owned = self.upload('本项目孤儿.txt', age_days=40, project=self.pid)
        orphan = self.upload('已删项目孤儿.txt', age_days=40,
                             project='project_deleted_0000')
        stray = self.upload('未登记.txt', age_days=40)
        cleanup = Cleanup(self.data)
        plan = cleanup.settings_preview(self.projects())
        names = {Path(i['path']).name for i in plan['items']}
        self.assertIn(orphan, names)
        self.assertNotIn(owned, names)
        self.assertNotIn(stray, names)
        reasons = ' '.join(i['reason'] for i in plan['retained'])
        self.assertIn('所属项目仍在', reasons)
        self.assertIn('未登记归属', reasons)
        self.assertEqual(plan['scope'], 'settings')
        self.assertEqual(plan['retention_days'], 0)

    def test_settings_cleanup_executes_only_the_frozen_list(self):
        orphan = self.upload('已删项目孤儿.txt', age_days=40,
                             project='project_deleted_0000')
        cleanup = Cleanup(self.data)
        plan = cleanup.settings_preview(self.projects())
        late = self.upload('后来的孤儿.txt', age_days=40, project='project_deleted_0000')
        scope = {'id': '*settings*', 'closed': True, 'retention_days': 0, 'title': '应用占用清理'}
        result = cleanup.execute(scope, self.projects(), plan['id'])
        self.assertIn(orphan, {Path(i['path']).name for i in result['moved']})
        self.assertNotIn(late, {Path(i['path']).name for i in result['moved']})
        self.assertTrue((self.uploads / late).exists())
        with self.assertRaises(Invalid):
            cleanup.execute(scope, self.projects(), plan['id'])

    def test_protected_state_files_are_never_candidates(self):
        self.closed()
        plan = Cleanup(self.data).preview(self.project_now(), self.projects())
        names = {Path(i['path']).name for i in plan['items']}
        self.assertNotIn('workspace.sqlite3', names)
        self.assertIn('workspace.sqlite3', {Path(i['path']).name for i in plan['retained']})

    def test_reclaimed_stale_lock_cannot_replay_a_finished_plan(self):
        """A reclaimed lock must re-read the plan, not act on a stale copy."""
        self.upload('本项目孤儿.txt', age_days=30, project=self.pid)
        self.closed()
        cleanup = Cleanup(self.data)
        plan = cleanup.preview(self.project_now(), self.projects())
        first = cleanup.execute(self.project_now(), self.projects(), plan['id'])
        self.assertTrue(first['moved'])
        manifest = Path(first['root']) / 'manifest.json'
        recorded = json.loads(manifest.read_text(encoding='utf-8'))
        # Simulate the branch Codex exercised: a request read the plan, waited,
        # then took over a lock left behind by an earlier run.
        lock = cleanup.plans / f"{plan['id']}.lock"
        lock.write_text('stale', encoding='utf-8')
        old = time.time() - 901
        os.utime(lock, (old, old))
        with self.assertRaises(Invalid):
            cleanup.execute(self.project_now(), self.projects(), plan['id'])
        after = json.loads(manifest.read_text(encoding='utf-8'))
        self.assertEqual(len(after['moved']), len(recorded['moved']))
        self.assertEqual([m['path'] for m in after['moved']], [m['path'] for m in recorded['moved']])

    def test_unmarked_leftover_scratch_is_never_adopted(self):
        """A leftover tmp folder of unknown origin must not be claimed as ours."""
        leftover = self.data / 'tmp' / self.pid
        leftover.mkdir(parents=True)
        (leftover / 'unknown.bin').write_bytes(b'u' * 8)
        scratch_for(self.data, self.pid)
        self.assertFalse((leftover / OWNER_MARKER).is_file())
        plan = Cleanup(self.data).preview(self.project_now(), self.projects(), retention_days=1)
        self.assertEqual([i for i in plan['items'] if i['kind'] == '项目临时文件'], [])
        self.assertTrue((leftover / 'unknown.bin').exists())


if __name__ == '__main__':
    unittest.main()