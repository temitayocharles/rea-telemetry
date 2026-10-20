"""Auth and TLS: the lock has to hold, and it must not get in the way when off."""
import base64
import json
import os
import shutil
import ssl
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

os.environ.setdefault("CRAFT_DB", os.path.join(tempfile.mkdtemp(), "auth.db"))

from craft import auth as authmod  # noqa: E402
from craft import db, seed  # noqa: E402
from craft import server as server_mod  # noqa: E402


def get(url, headers=None, context=None):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=20, context=context) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def post(url, body):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


class AuthUnitTests(unittest.TestCase):
    def test_disabled_by_default(self):
        gate = authmod.Auth()
        self.assertFalse(gate.enabled)
        self.assertEqual(gate.method, "none")
        gate.check({}, "/api/stats")  # must not raise

    def test_token_matches_only_the_token(self):
        gate = authmod.Auth(token="hunter2")
        self.assertEqual(gate.method, "token")
        gate.check({"Authorization": "Bearer hunter2"}, "/api/stats")
        gate.check({}, "/api/stats", {"token": ["hunter2"]})
        with self.assertRaises(authmod.AuthError):
            gate.check({"Authorization": "Bearer hunter3"}, "/api/stats")
        with self.assertRaises(authmod.AuthError):
            gate.check({}, "/api/stats")

    def test_basic_auth(self):
        gate = authmod.Auth(user="ops", password="s3cret")
        self.assertEqual(gate.method, "basic")
        header = base64.b64encode(b"ops:s3cret").decode()
        gate.check({"Authorization": f"Basic {header}"}, "/api/stats")
        bad = base64.b64encode(b"ops:nope").decode()
        with self.assertRaises(authmod.AuthError):
            gate.check({"Authorization": f"Basic {bad}"}, "/api/stats")
        with self.assertRaises(authmod.AuthError):
            gate.check({"Authorization": "Basic not-base64!"}, "/api/stats")

    def test_basic_needs_a_password(self):
        gate = authmod.Auth(user="ops")
        self.assertFalse(gate.enabled)

    def test_public_paths_stay_open(self):
        gate = authmod.Auth(token="t")
        for path in ("/api/health", "/api/status/health", "/api/auth/status", "/api/auth/login"):
            gate.check({}, path)

    def test_throttle_counts_failures_per_client(self):
        gate = authmod.Auth(token="t")
        for _ in range(authmod.FAILED_ATTEMPT_LIMIT):
            gate.note_failure("10.0.0.1")
        self.assertTrue(gate.throttle("10.0.0.1"))
        self.assertFalse(gate.throttle("10.0.0.2"))
        gate.note_success("10.0.0.1")
        self.assertFalse(gate.throttle("10.0.0.1"))

    def test_status_reports_the_presented_credential(self):
        gate = authmod.Auth(token="t")
        self.assertEqual(gate.status(), {"enabled": True, "method": "token", "ok": False})
        self.assertTrue(gate.status({"Authorization": "Bearer t"})["ok"])
        self.assertFalse(gate.status({"Authorization": "Bearer nope"})["ok"])
        self.assertFalse(authmod.Auth().status()["enabled"])

    def test_from_env(self):
        os.environ["CRAFT_AUTH_TOKEN"] = "envtoken"
        try:
            gate = authmod.Auth.from_env()
            self.assertEqual(gate.method, "token")
            self.assertTrue(gate.verify_token("envtoken"))
        finally:
            del os.environ["CRAFT_AUTH_TOKEN"]

    def test_tls_context_is_optional(self):
        self.assertIsNone(authmod.tls_context("", "", ""))
        self.assertEqual(authmod.scheme_for(None), "http")
        with self.assertRaises(ValueError):
            authmod.tls_context("/tmp/only-cert.pem", "", "")
        with self.assertRaises(FileNotFoundError):
            authmod.tls_context("/tmp/nope-cert.pem", "/tmp/nope-key.pem", "")


class AuthHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.reset()
        seed.seed("http://127.0.0.1:1", force=True)
        cls.token = "s3cret-token"
        cls._saved = server_mod.AUTH
        server_mod.AUTH = authmod.Auth(token=cls.token)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server_mod.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        server_mod.AUTH = cls._saved

    def test_everything_else_needs_a_credential(self):
        code, headers, _ = get(f"{self.base}/api/stats")
        self.assertEqual(code, 401)
        self.assertIn("Bearer", headers.get("WWW-Authenticate", ""))
        self.assertEqual(get(f"{self.base}/api/overview")[0], 401)
        self.assertEqual(get(f"{self.base}/metrics")[0], 401)

    def test_the_page_itself_loads_so_the_login_form_can_render(self):
        self.assertEqual(get(f"{self.base}/")[0], 200)

    def test_public_paths(self):
        self.assertEqual(get(f"{self.base}/api/health")[0], 200)
        self.assertEqual(get(f"{self.base}/api/status/health")[0], 200)
        status, _, body = get(f"{self.base}/api/auth/status")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"enabled": True, "method": "token", "ok": False})

    def test_bearer_and_query_token(self):
        self.assertEqual(get(f"{self.base}/api/stats", {"Authorization": f"Bearer {self.token}"})[0], 200)
        self.assertEqual(get(f"{self.base}/api/stats?token={self.token}")[0], 200)
        self.assertEqual(get(f"{self.base}/api/stats?token=wrong")[0], 401)

    def test_login_hands_back_a_replayable_credential(self):
        code, payload = post(f"{self.base}/api/auth/login", {"token": self.token})
        self.assertEqual((code, payload["ok"], payload["method"]), (200, True, "token"))
        self.assertEqual(payload["credential"], f"Bearer {self.token}")
        self.assertEqual(get(f"{self.base}/api/stats", {"Authorization": payload["credential"]})[0], 200)
        self.assertEqual(post(f"{self.base}/api/auth/login", {"token": "nope"})[0], 401)

    def test_existing_credential_is_confirmed_by_status(self):
        _, _, body = get(f"{self.base}/api/auth/status", {"Authorization": f"Bearer {self.token}"})
        self.assertTrue(json.loads(body)["ok"])

    def test_post_routes_are_protected_too(self):
        req = urllib.request.Request(
            f"{self.base}/api/write", data=b'{"metric":"x"}', method="POST",
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=20)
        self.assertEqual(ctx.exception.code, 401)


class ThrottleHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._saved = server_mod.AUTH
        server_mod.AUTH = authmod.Auth(token="good")
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server_mod.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        server_mod.AUTH = cls._saved

    def setUp(self):
        # every test starts from a clean failure counter for 127.0.0.1
        server_mod.AUTH = authmod.Auth(token="good")

    def test_repeated_failures_get_locked_out(self):
        limit = authmod.FAILED_ATTEMPT_LIMIT
        codes = [get(f"{self.base}/api/stats", {"Authorization": "Bearer bad"})[0]
                 for _ in range(limit + 2)]
        # the attempt that crosses the limit is already locked out
        self.assertEqual(codes[: limit - 1], [401] * (limit - 1))
        self.assertEqual(codes[limit - 1:], [429] * 3)

    def test_login_endpoint_counts_failures(self):
        limit = authmod.FAILED_ATTEMPT_LIMIT
        codes = [post(f"{self.base}/api/auth/login", {"token": "wrong"})[0] for _ in range(limit)]
        self.assertEqual(codes[: limit - 1], [401] * (limit - 1))
        self.assertEqual(codes[-1], 429)

    def test_lockout_does_not_block_a_valid_credential(self):
        self.assertEqual(get(f"{self.base}/api/stats", {"Authorization": "Bearer good"})[0], 200)


@unittest.skipUnless(shutil.which("openssl"), "openssl binary is required to mint a test certificate")
class TLSHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        cls.cert = os.path.join(cls.dir, "cert.pem")
        cls.key = os.path.join(cls.dir, "key.pem")
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
             "-keyout", cls.key, "-out", cls.cert, "-subj", "/CN=localhost",
             "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"],
            check=True, capture_output=True,
        )
        cls._saved = server_mod.AUTH
        server_mod.AUTH = authmod.Auth(token="tls-token")
        cls.context = authmod.tls_context(cls.cert, cls.key)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server_mod.Handler)
        cls.httpd.socket = cls.context.wrap_socket(cls.httpd.socket, server_side=True)
        cls.port = cls.httpd.server_address[1]
        cls.base = f"https://localhost:{cls.port}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        server_mod.AUTH = cls._saved

    def client(self):
        ctx = ssl.create_default_context(cafile=self.cert)
        return ctx

    def test_scheme_is_https(self):
        self.assertEqual(authmod.scheme_for(self.context), "https")

    def test_requests_are_encrypted_and_still_authenticated(self):
        code, _, body = get(f"{self.base}/api/health", context=self.client())
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["status"], "ok")
        self.assertEqual(get(f"{self.base}/api/stats", context=self.client())[0], 401)
        self.assertEqual(
            get(f"{self.base}/api/stats", {"Authorization": "Bearer tls-token"}, context=self.client())[0], 200
        )


if __name__ == "__main__":
    unittest.main()
