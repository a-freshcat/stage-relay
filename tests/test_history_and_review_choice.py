"""Batch history and user-selected reviewers, with local fake endpoints only."""
import json
import threading
import unittest
import urllib.error
import urllib.request

import test_relay as fixtures
from panel import Store, make_server
from relaylib.contracts import RelayError
from relaylib.plugin import JobStore
from relaylib.runtime import atomic_json
from relaylib.settings import Settings, SettingsError
from test_review import review


class HistoryAndReviewChoiceTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.RelayIntegrationTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.config = self.f.base / 'config.json'
        atomic_json(self.config, self.f.config)
        self.settings = Settings(self.config)
        atomic_json(self.settings.profiles, {'version': 1, 'profiles': [{
            'id': 'saved', 'name': '已有接口', 'endpoint': self.f.config['worker']['endpoint'],
            'models': ['review-model']}]})
        self.jobs = JobStore(self.config, self.f.base / 'jobs', [self.f.project])
        self.addCleanup(lambda: [p.wait(timeout=20) for p in self.jobs.children])
        self.plan = {'goal': 'Fix value', 'files': [{'path': 'value.txt', 'note': 'Wrong value'}],
                     'steps': ['Write fixed']}

    def start_caller(self):
        atomic_json(self.f.sequence, {'cursor': 0, 'entries': [
            {'response': fixtures.result(), 'write': {'value.txt': 'fixed'}}, {'response': review()}]})
        job = self.jobs.submit(str(self.f.project), 'saved-review', self.plan, review_mode='user_choice')
        return self.jobs.wait(job['job_id'], 20)

    def server(self, store=None):
        server = make_server(store or Store(self.jobs.root), settings=self.settings, jobs=self.jobs)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def post(self, server, route, value, origin=None):
        request = urllib.request.Request(server.url + route, data=json.dumps(value).encode(), headers={
            'Content-Type': 'application/json', 'X-Relay-Panel': 'settings',
            'Origin': origin or server.url.split('/', 3)[0] + '//' + server.url.split('/', 3)[2]})
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.load(response)

    def test_saved_model_reviews_caller_job_without_changing_planner_or_repeating_worker(self):
        row = self.start_caller()
        directory = self.jobs.directory(row['job_id'])
        original_config = (directory / 'config.json').read_bytes()
        original_result = (directory / 'relay/result.json').read_bytes()
        target = self.settings.reviewer('saved', 'review-model')
        self.assertEqual(len(self.f.upstream.requests), 1)  # Choosing is local.
        self.jobs.request_review(row['job_id'], row['review_revision'], target)
        final = self.jobs.wait(row['job_id'], 20)
        self.assertEqual(final['review']['reviewer'], 'review-model', final)
        self.assertEqual(final['review']['reviewer_selection']['provider_id'], 'saved')
        self.assertEqual([r['body']['model'] for r in self.f.upstream.requests], ['worker-model', 'review-model'])
        self.assertEqual(self.f.upstream.requests[-1]['body']['fixture_sandbox'], 'read-only')
        self.assertEqual((directory / 'config.json').read_bytes(), original_config)
        self.assertEqual((directory / 'relay/result.json').read_bytes(), original_result)
        budget = json.loads((directory / 'relay/budget.json').read_text())
        self.assertEqual(budget['roles']['reviewer']['forwarded'], 1)
        self.assertTrue(self.jobs.request_review(row['job_id'], row['review_revision'], target)['reused'])
        self.assertEqual(len(self.f.upstream.requests), 2)

    def test_http_only_accepts_saved_models_and_preserves_origin_protection(self):
        row = self.start_caller()
        server = self.server()
        body = {'job_id': row['job_id'], 'result_revision': row['review_revision'],
                'provider_id': 'saved', 'model': 'not-saved'}
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.post(server, 'api/review', body)
        error.exception.close()
        body['model'] = 'review-model'
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.post(server, 'api/review', body, 'http://invalid.example')
        self.assertEqual(error.exception.code, 403)
        error.exception.close()
        self.assertEqual(len(self.f.upstream.requests), 1)
        self.assertEqual(self.post(server, 'api/review', body)['status'], 'queued')
        self.assertEqual(self.jobs.wait(row['job_id'], 20)['review']['reviewer'], 'review-model')

    def test_current_codex_can_review_an_api_planned_job_locally(self):
        atomic_json(self.f.sequence, {'cursor': 0, 'entries': self.f.good_entries()})
        job = self.jobs.submit_task(str(self.f.project), 'codex-review', 'Fix value', review_mode='user_choice')
        row = self.jobs.wait(job['job_id'], 20)
        saved = self.jobs.record_review(row['job_id'], row['review_revision'], review())
        self.assertEqual(saved['model_calls'], 0)
        self.assertIn('用户指定审核', saved['review']['reviewer'])
        self.assertEqual(len(self.f.upstream.requests), 2)

    def test_saved_selection_reuses_matching_original_planner_review(self):
        atomic_json(self.f.sequence, {'cursor': 0, 'entries': self.f.good_entries() + [{'response': review()}]})
        job = self.jobs.submit_task(str(self.f.project), 'reuse-original', 'Fix value', review_mode='auto')
        row = self.jobs.wait(job['job_id'], 20)
        self.assertEqual(row['review']['reviewer'], 'planner-model', row)
        selected = self.settings.reviewer('saved', 'planner-model')
        self.assertTrue(self.jobs.request_review(row['job_id'], row['review_revision'], selected)['reused'])
        self.assertEqual(len(self.f.upstream.requests), 3)

    def test_selected_review_rejects_changed_project_before_model_call(self):
        row = self.start_caller()
        (self.f.project / 'value.txt').write_text('changed after execution')
        self.jobs.request_review(row['job_id'], row['review_revision'], self.settings.reviewer('saved', 'review-model'))
        final = self.jobs.wait(row['job_id'], 20)
        self.assertEqual(final['failure'], 'project_changed', final)
        self.assertEqual(len(self.f.upstream.requests), 1)

    def test_saved_choice_rejects_unknown_model_and_fingerprints_endpoint(self):
        with self.assertRaises(SettingsError):
            self.settings.reviewer('saved', 'missing')
        first = self.settings.reviewer('saved', 'review-model')
        data = json.loads(self.settings.profiles.read_text(encoding='utf-8'))
        data['profiles'][0]['endpoint'] = {'base_url': self.f.upstream.url + '/another'}
        atomic_json(self.settings.profiles, data)
        second = self.settings.reviewer('saved', 'review-model')
        self.assertNotEqual(first['selection']['fingerprint'], second['selection']['fingerprint'])
        self.assertEqual(self.f.upstream.requests, [])

    def history_store(self):
        root = self.f.base / 'history'
        for name in ('one', 'two', 'running'):
            path = root / name
            path.mkdir(parents=True)
            atomic_json(path / 'state.json', {'status': 'running' if name == 'running' else 'completed'})
        return Store(root)

    def test_batch_history_is_atomic_and_restores_all_selected_records(self):
        store = self.history_store()
        rows = store.runs()
        selected = [row['id'] for row in rows if row['can_delete']]
        active = next(row['id'] for row in rows if not row['can_delete'])
        before = {p: p.read_bytes() for p in store.root.glob('*/state.json')}
        with self.assertRaises(RelayError):
            store.change_history_batch(selected + [active], 'delete')
        self.assertEqual(len(store.runs()), 3)
        store.change_history_batch(selected, 'delete')
        self.assertEqual(store.last_deleted_ids(), selected)
        self.assertEqual(len(store.runs()), 1)
        store.change_history_batch(selected, 'restore')
        self.assertEqual(len(store.runs()), 3)
        self.assertEqual(store.last_deleted_ids(), [])
        self.assertEqual(before, {p: p.read_bytes() for p in store.root.glob('*/state.json')})
        self.assertEqual(self.f.upstream.requests, [])

    def test_http_batch_delete_and_undo(self):
        store = self.history_store()
        server = self.server(store)
        selected = [row['id'] for row in store.runs() if row['can_delete']]
        value = self.post(server, 'api/history', {'ids': selected, 'action': 'delete'})
        self.assertEqual(value['count'], 2)
        with urllib.request.urlopen(server.url + 'api/runs') as response:
            self.assertEqual(json.load(response)['last_deleted_ids'], selected)
        self.post(server, 'api/history', {'ids': selected, 'action': 'restore'})
        self.assertEqual(len(store.runs()), 3)
        self.assertEqual(self.f.upstream.requests, [])


if __name__ == '__main__':
    unittest.main()
