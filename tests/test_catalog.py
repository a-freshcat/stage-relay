"""Model discovery reads local metadata fixtures only, never generations."""
import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from relaylib.catalog import Catalog, fetch_models
from relaylib.gateway import resolve_endpoint
from relaylib.settings import SettingsError
from panel import Store, make_server


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.seen=[]
        self.status=200
        self.body={"data":[{"id":"deepseek-v4.1-flash-free"},{"id":"vendor/code-model"},{"id":"deepseek-v4.1-flash-free"}]}
        outer=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*_): pass
            def do_GET(self):
                outer.seen.append((self.command,self.path,self.headers.get("Authorization")))
                self.send_response(outer.status)
                self.send_header("Location","https://untrusted.invalid/")
                self.end_headers()
                self.wfile.write(json.dumps(outer.body).encode())
        self.server=ThreadingHTTPServer(("127.0.0.1",0),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.spec={"base_url":f"http://127.0.0.1:{self.server.server_port}/v1/","api_key_env":"RELAY_CATALOG_TEST_KEY"}
        self.env=patch.dict(os.environ,{"RELAY_CATALOG_TEST_KEY":"PRIVATE_FIXTURE_KEY"})
        self.env.start();self.addCleanup(self.env.stop)

    def test_exact_path_key_and_model_ids_no_generation(self):
        models=fetch_models(self.spec)
        self.assertEqual(models,["deepseek-v4.1-flash-free","vendor/code-model"])
        self.assertEqual(self.seen,[("GET","/v1/models","Bearer PRIVATE_FIXTURE_KEY")])

    def test_native_gateway_catalog_uses_explicit_key_without_changing_generation_auth(self):
        spec={"codex_provider":"test","native_auth":True}
        config={"model_providers":{"test":{"base_url":self.spec["base_url"],"experimental_bearer_token":"LOCAL_GATEWAY_KEY","requires_openai_auth":True}}}
        with patch("relaylib.gateway.Path.read_text",return_value=""),patch("relaylib.gateway.tomllib.loads",return_value=config):
            self.assertEqual(len(fetch_models(spec)),2)
            self.assertNotIn("Authorization",resolve_endpoint(spec)["headers"])
        self.assertEqual(self.seen[0][2],"Bearer LOCAL_GATEWAY_KEY")

    def test_cache_expiry_and_explicit_refresh(self):
        catalog=Catalog()
        payload={"endpoint":self.spec}
        self.assertFalse(catalog.discover(payload)["cached"])
        self.assertTrue(catalog.discover(payload)["cached"])
        self.assertEqual(len(self.seen),1)
        self.assertFalse(catalog.discover({**payload,"refresh":True})["cached"])
        self.assertEqual(len(self.seen),2)
        with patch("relaylib.catalog.time.monotonic",return_value=10**15):
            self.assertFalse(catalog.discover(payload)["cached"])
        self.assertEqual(len(self.seen),3)

    def test_missing_auth_fails_without_http_or_secret_echo(self):
        with patch.dict(os.environ,{"RELAY_CATALOG_TEST_KEY":""}):
            with self.assertRaises(SettingsError) as error: fetch_models(self.spec)
        self.assertIn("RELAY_CATALOG_TEST_KEY",str(error.exception))
        self.assertEqual(self.seen,[])

    def test_http_errors_and_redirect_do_not_leak_provider_body(self):
        for status in (401,403,404,429,302):
            self.status=status;self.body={"error":"PRIVATE_FIXTURE_KEY"}
            with self.subTest(status=status),self.assertRaises(SettingsError) as error: fetch_models(self.spec)
            self.assertNotIn("PRIVATE_FIXTURE_KEY",str(error.exception))
        self.assertEqual(len(self.seen),5)

    def test_empty_invalid_and_partial_catalogs(self):
        self.body={"data":[]};self.assertEqual(fetch_models(self.spec),[])
        for data in ({"other":[]},{"data":[{"id":"a"}],"has_more":True},{"data":[{"id":"bad\nID"}]}):
            self.body=data
            with self.assertRaises(SettingsError): fetch_models(self.spec)

    def test_timeout_is_not_reported_as_latency(self):
        with patch("relaylib.catalog.http.client.HTTPConnection") as cls:
            cls.return_value.getresponse.side_effect=TimeoutError()
            with self.assertRaises(SettingsError) as error: fetch_models(self.spec)
        self.assertEqual(error.exception.status,504)

    def test_endpoint_validation_prevents_inline_secrets(self):
        with self.assertRaises(SettingsError): fetch_models({"base_url":"https://user:secret@example.invalid/v1"})
        self.assertEqual(self.seen,[])

    def test_browser_route_requires_same_origin_and_returns_no_key(self):
        panel=make_server(Store())
        threading.Thread(target=panel.serve_forever,daemon=True).start()
        self.addCleanup(panel.server_close);self.addCleanup(panel.shutdown)
        data=json.dumps({"endpoint":self.spec}).encode()
        request=urllib.request.Request(panel.url+"api/models",data=data,headers={"Content-Type":"application/json"})
        with self.assertRaises(urllib.error.HTTPError) as error: urllib.request.urlopen(request)
        self.assertEqual(error.exception.code,403);error.exception.close()
        self.assertEqual(self.seen,[])
        request.add_header("Origin",f"http://127.0.0.1:{panel.server_port}")
        request.add_header("X-Relay-Panel","settings")
        with urllib.request.urlopen(request) as response: result=response.read().decode()
        self.assertIn("vendor/code-model",result)
        self.assertNotIn("PRIVATE_FIXTURE_KEY",result)


if __name__=="__main__": unittest.main()
