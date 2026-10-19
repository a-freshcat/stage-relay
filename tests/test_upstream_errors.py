"""Provider classifications are verified through the real local gateway."""
import json
import threading
import unittest
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
import test_relay as fixtures
from relaylib.gateway import Gateway,Ledger,upstream_failure
from relaylib.diagnostics import describe_failure
from relaylib.contracts import RelayError


class UpstreamErrorTests(unittest.TestCase):
    def test_model_permission_and_ip_restriction_have_actionable_safe_messages(self):
        cases=[('model [m] is not allowed for user type [personal_free_user]','model_access_denied'),
               ("Free models unavailable: abusive traffic from this IP address",'network_access_denied')]
        for message,expected in cases:
            with self.subTest(expected=expected),fixtures.WorkspaceTemp() as temp:
                payload=json.dumps({'error':{'message':message+' SECRET_FIXTURE_ONLY'}}).encode()
                class Handler(BaseHTTPRequestHandler):
                    def log_message(self,*_):pass
                    def do_POST(self):
                        self.rfile.read(int(self.headers['Content-Length']))
                        self.send_response(502);self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload)
                server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
                thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
                try:
                    audit=Path(temp)/'requests.jsonl'
                    with Gateway({'url':f'http://127.0.0.1:{server.server_port}/v1','headers':{}},'m','planner',30,'test',Ledger(Path(temp)/'budget.json'),audit) as gate:
                        req=urllib.request.Request(gate.base_url+'/responses',data=b'{"model":"m"}',headers={'Authorization':'Bearer '+gate.token})
                        with self.assertRaises(urllib.error.HTTPError) as caught:urllib.request.urlopen(req,timeout=5)
                        body=caught.exception.read().decode();caught.exception.close()
                        self.assertEqual(gate.failure,expected)
                        self.assertEqual(gate.upstream_status,502)
                        self.assertNotIn('SECRET_FIXTURE_ONLY',body)
                    self.assertNotIn('SECRET_FIXTURE_ONLY',audit.read_text(encoding='utf-8'))
                    diagnostic=describe_failure(RelayError(expected,''),{'upstream_status':502})
                    self.assertEqual(diagnostic['upstream_status'],502)
                    self.assertTrue(diagnostic['action'])
                finally:server.shutdown();server.server_close()

    def test_unknown_body_is_not_repeated_and_status_fallback_is_stable(self):
        for status,expected in ((401,'authentication_error'),(403,'authentication_error'),(429,'upstream_rate_limit'),(502,'api_error')):
            self.assertEqual(upstream_failure(status,b'<html>SECRET</html>'),expected)

    def test_unsupported_parameter_preserves_upstream_400_without_secret(self):
        with fixtures.WorkspaceTemp() as temp:
            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *_): pass
                def do_POST(self):
                    self.rfile.read(int(self.headers['Content-Length']))
                    payload = b'{"error":{"code":"unsupported_parameter","message":"SECRET"}}'
                    self.send_response(400)
                    self.send_header('Content-Length', str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                with Gateway({'url': f'http://127.0.0.1:{server.server_port}/v1', 'headers': {}},
                             'm', 'worker', 30, 'test', Ledger(Path(temp)/'budget.json'), Path(temp)/'requests.jsonl') as gate:
                    request = urllib.request.Request(gate.base_url+'/responses', data=b'{"model":"m"}',
                                                     headers={'Authorization': 'Bearer '+gate.token})
                    with self.assertRaises(urllib.error.HTTPError) as caught:
                        urllib.request.urlopen(request, timeout=5)
                    self.assertEqual(caught.exception.code, 400)
                    self.assertNotIn('SECRET', caught.exception.read().decode())
                    caught.exception.close()
                    self.assertEqual(gate.failure, 'unsupported_parameter')
            finally:
                server.shutdown()
                server.server_close()
