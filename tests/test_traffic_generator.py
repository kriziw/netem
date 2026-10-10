"""Regression tests for the simulator API client and scenario control path."""
import hashlib
import json
import ssl
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import app as netem


class SimulatorIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.certs = tempfile.TemporaryDirectory()
        cls.cert = Path(cls.certs.name) / "tls.crt"
        cls.key = Path(cls.certs.name) / "tls.key"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=localhost", "-keyout", str(cls.key), "-out", str(cls.cert)], check=True, capture_output=True)
        cls.fingerprint = hashlib.sha256(ssl.PEM_cert_to_DER_cert(cls.cert.read_text())).hexdigest()

    @classmethod
    def tearDownClass(cls):
        cls.certs.cleanup()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.paths = patch.multiple(netem, CONFIG_PATH=root / "config.json", RUNTIME_DIR=root, SECRETS_PATH=root / "secrets.json", EVENT_LOG_PATH=root / "events.jsonl", SESSIONS_PATH=root / "sessions.json")
        self.paths.start()
        netem.app.config["TESTING"] = True
        netem.SCENARIO_STOP.clear()
        self.client = netem.app.test_client()
        self.requests = []
        self.status_code = 200
        self.payload = {"status": "idle", "run": None, "users": 0, "dem": {name: None for name in ("experience_score", "availability_pct", "p50_ms", "p95_ms", "requests_per_second", "failures_per_second")}}
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                owner.requests.append((self.path, self.headers.get("Authorization")))
                self.send_response(owner.status_code)
                self.send_header("Location", "https://elsewhere.example/api/v1/status")
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(owner.payload).encode())

            def log_message(self, *_args):
                pass

        class QuietServer(ThreadingHTTPServer):
            def handle_error(self, request, client_address):
                pass  # A rejected TLS fingerprint closes before the HTTP request.

        self.server = QuietServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.cert, self.key)
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.config = {"wan_links": [{"id": "wan1", "inner": "lo", "outer": "lo", "preset": "dia"}], "traffic_generator": {"host": "127.0.0.1", "port": self.server.server_port, "allow_self_signed": True}}
        netem.save_config(self.config)
        netem.save_secrets({"traffic_generator_api_key": "test-key"})

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=2)
        self.paths.stop()
        self.directory.cleanup()

    def test_self_signed_certificate_is_pinned_before_authenticated_request(self):
        result = netem.traffic_generator_request("/api/v1/status")
        self.assertEqual(result["status"], "idle")
        self.assertEqual(netem.load_config()["traffic_generator"]["tls_sha256"], self.fingerprint)
        self.assertEqual(self.requests, [("/api/v1/status", "Bearer test-key")])
        self.config["traffic_generator"]["tls_sha256"] = "0" * 64
        netem.save_config(self.config)
        with self.assertRaisesRegex(RuntimeError, "fingerprint changed"):
            netem.traffic_generator_request("/api/v1/status")
        self.assertEqual(len(self.requests), 1)

    def test_redirects_are_rejected_and_secret_errors_redacted(self):
        self.status_code = 302
        self.payload = {"error": "test-key"}
        with self.assertRaises(RuntimeError) as error:
            netem.traffic_generator_request("/api/v1/status")
        self.assertNotIn("test-key", str(error.exception))
        self.assertEqual(len(self.requests), 1)

    def test_malformed_status_is_reported_disconnected(self):
        for payload in ([], {}, {"status": "idle", "dem": None}):
            self.payload = payload
            snapshot = netem.traffic_generator_snapshot()
            self.assertFalse(snapshot["connected"])
            self.assertTrue(snapshot["error"])

    def test_dem_score_reads_experience_contract(self):
        self.payload = {"endpoint_experience": {"score": 93, "rating": "Excellent"}, "active_users": 10}
        passed, observed, _detail = netem.evaluate_scenario_condition({"type": "dem", "field": "experience_score", "op": ">=", "value": 90}, "wan1")
        self.assertTrue(passed)
        self.assertEqual(observed, 93)

    def test_scenario_executes_workload_start_adjust_stop(self):
        steps = netem.validate_scenario_steps([{"after": 0, "action": "traffic_generator", "value": {"operation": operation, "users": 2}} for operation in ("start", "adjust", "stop")])
        with patch.object(netem, "traffic_generator_request", return_value={"users": 2, "run": {"run_id": "run-test"}}) as api, patch.object(netem, "apply_selected_profile", return_value=(True, "OK", {})), patch.object(netem, "apply_mtu_limit", return_value=(True, "OK")), patch.object(netem, "log_event"),              patch.object(netem, "ensure_traffic_path") as path:
            netem.run_scenario("wan1", {"id": "test", "name": "test", "steps": steps})
        self.assertEqual([call.args[0] for call in api.call_args_list], ["/api/v1/workloads/start", "/api/v1/workloads/adjust", "/api/v1/workloads/stop"])
        path.assert_called_once_with("test")
        self.assertEqual(netem.SCENARIO_STATE["result"], "passed")

    def test_scenario_owned_workload_is_stopped_after_failure(self):
        steps = [{"after": 0, "action": "traffic_generator", "value": {"operation": "start"}}, {"after": 0, "action": "assert", "condition": {"type": "dem"}}]
        responses = [{"status": "running", "run": {"run_id": "owned"}}, {"status": "running", "run": {"run_id": "owned"}}, {"status": "stopped"}]
        with patch.object(netem, "traffic_generator_request", side_effect=responses) as api, patch.object(netem, "wait_for_scenario_condition", return_value=(False, None, "no data", 0)), patch.object(netem, "apply_selected_profile", return_value=(True, "OK", {})), patch.object(netem, "apply_mtu_limit", return_value=(True, "OK")), patch.object(netem, "log_event"),              patch.object(netem, "ensure_traffic_path"):
            netem.run_scenario("wan1", {"id": "test", "name": "test", "steps": steps})
        self.assertEqual(api.call_args_list[-1].args[0], "/api/v1/workloads/stop")
        self.assertEqual(netem.SCENARIO_STATE["result"], "failed")

    def test_scenario_connection_error_fails_and_restores_wan(self):
        steps = [{"after": 0, "action": "traffic_generator", "value": {"operation": "start"}}]
        with patch.object(netem, "traffic_generator_request", side_effect=RuntimeError("offline")), patch.object(netem, "apply_selected_profile", return_value=(True, "OK", {})) as restore, patch.object(netem, "apply_mtu_limit", return_value=(True, "OK")), patch.object(netem, "log_event"):
            netem.run_scenario("wan1", {"id": "test", "name": "test", "steps": steps})
        self.assertEqual(netem.SCENARIO_STATE["result"], "failed")
        restore.assert_called_once()

    def test_forms_require_csrf_and_invalid_urls_do_not_change_configuration(self):
        self.assertEqual(self.client.post("/traffic-generator/stop").status_code, 400)
        self.client.get("/integrations")
        before = netem.load_config()
        with self.client.session_transaction() as state:
            token = state["integration_csrf"]
        response = self.client.post("/integrations/traffic-generator/save", data={"manual_host": "https://127.0.0.1:bad", "integration_csrf": token})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(netem.load_config(), before)
        response = self.client.post("/integrations/traffic-generator/save", data={"manual_host": f"https://127.0.0.1:{self.server.server_port}", "port": "8443", "api_key": "test-key", "allow_self_signed": "on", "integration_csrf": token})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(netem.load_config()["traffic_generator"]["port"], self.server.server_port)

    def test_stop_is_attempted_even_when_status_read_fails(self):
        with self.client.session_transaction() as state:
            state["integration_csrf"] = "token"
        with patch.object(netem, "traffic_generator_request", side_effect=[RuntimeError("status unavailable"), {"status": "stopped"}]) as api:
            response = self.client.post("/traffic-generator/stop", data={"integration_csrf": "token"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(api.call_args_list[-1].args[0], "/api/v1/workloads/stop")

    def test_nonfinite_scenarios_are_rejected(self):
        for value in ({"operation": "start", "spawn_rate": "nan"}, {"operation": "start", "applications": {"dns": None}}, {"operation": "start", "personas": {"developer": 0}}):
            with self.assertRaises(ValueError):
                netem.validate_scenario_steps([{"action": "traffic_generator", "value": value}])
        with self.assertRaises(ValueError):
            netem.validate_condition({"type": "dem", "value": 1, "window": None}, 1)


if __name__ == "__main__":
    unittest.main()
