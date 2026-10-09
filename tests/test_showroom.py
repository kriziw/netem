"""The customer listener shares live data without exposing operator capabilities."""
import os
import unittest
from unittest.mock import patch
from urllib.request import urlopen

import app as netem
from showroom import create_showroom_app


STATE = {
    "id": "wan1", "label": "Customer WAN", "preset_name": "DIA",
    "runtime_quality": 90, "fault": "normal", "sla": {"pass": True},
    "effective": {"delay_ms": 10, "jitter_ms": 2, "loss_pct": 0,
                  "download_mbit": 100, "upload_mbit": 20},
    "appliance_addresses": ["192.0.2.1"], "inner": "private-interface",
}


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        for name, value in (
            ("load_config", {"integration_token": "secret-token"}),
            ("build_link_states", [STATE]),
            ("scenario_snapshot", {"active": True, "scenario_name": "Brownout",
                                   "step": 1, "step_count": 2, "step_label": "Degrade",
                                   "error": "sensitive diagnostic"}),
            ("session_snapshot", {"active": True, "name": "Showcase", "id": "internal"}),
        ):
            mock = patch.object(netem, name, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)
        mock = patch.object(netem.time, "time", return_value=100)
        mock.start()
        self.addCleanup(mock.stop)

    def snapshot(self, sample):
        with patch.object(netem, "latest_telemetry_sample", return_value=sample):
            return netem.showroom_snapshot()

    def test_live_and_idle_rates_and_allowlisted_data(self):
        result = self.snapshot({"timestamp": 99, "rate_valid": 1,
                                "down_mbps": 0, "up_mbps": 1.5})
        link = result["links"][0]
        self.assertTrue(link["traffic_available"])
        self.assertEqual((link["down_mbps"], link["up_mbps"]), (0, 1.5))
        self.assertEqual(result["scenario"]["scenario_name"], "Brownout")
        self.assertEqual(result["session"], {"active": True, "name": "Showcase"})
        for secret in ("secret-token", "private-interface", "192.0.2.1", "sensitive diagnostic", "internal"):
            self.assertNotIn(secret, str(result))

    def test_missing_invalid_stale_and_future_measurements_are_unknown(self):
        for sample in (None, {"timestamp": 99, "rate_valid": 0},
                       {"timestamp": 93, "rate_valid": 1, "down_mbps": 5, "up_mbps": 2},
                       {"timestamp": 101, "rate_valid": 1, "down_mbps": 5, "up_mbps": 2},
                       {"timestamp": 99, "rate_valid": 1, "down_mbps": None, "up_mbps": 2},
                       {"timestamp": 99, "rate_valid": 1, "down_mbps": float("nan"), "up_mbps": 2}):
            with self.subTest(sample=sample):
                link = self.snapshot(sample)["links"][0]
                self.assertFalse(link["traffic_available"])
                self.assertIsNone(link["down_mbps"])
                self.assertIsNone(link["up_mbps"])


class ListenerTests(unittest.TestCase):
    def setUp(self):
        self.state = {"links": [], "timestamp": 1}
        self.viewer = create_showroom_app(lambda: self.state)
        self.client = self.viewer.test_client()

    def test_surface_is_only_presentation_routes(self):
        allowed = {"/", "/api/snapshot", "/assets/showroom.css", "/assets/showroom.js"}
        self.assertEqual({rule.rule for rule in self.viewer.url_map.iter_rules()}, allowed)
        for rule in netem.app.url_map.iter_rules():
            path = rule.rule.replace("<", "").replace(">", "")
            if path not in allowed:
                with self.subTest(path=path):
                    self.assertEqual(self.client.get(path).status_code, 404)
        for path in allowed:
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertIn("connect-src 'self'", response.headers["Content-Security-Policy"])
            response.close()
            with self.client.head(path) as response:
                self.assertEqual(response.status_code, 200)
            for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE"):
                self.assertEqual(self.client.open(path, method=method).status_code, 405)
        self.assertEqual(self.client.get("/assets/../app.py").status_code, 404)
        self.assertEqual(self.client.get("/static/actions.js").status_code, 404)

    def test_same_provider_reflects_operator_changes_without_cookies(self):
        self.assertEqual(self.client.get("/api/snapshot").json["timestamp"], 1)
        self.state["timestamp"] = 2
        response = self.client.get("/api/snapshot")
        self.assertEqual(response.json["timestamp"], 2)
        self.assertNotIn("Set-Cookie", response.headers)
        html = self.client.get("/").text
        self.assertNotIn("<form", html)
        self.assertNotIn("actions.js", html)

    def test_startup_uses_same_process_and_closes_real_listener(self):
        from werkzeug.serving import make_server
        servers = []

        def bind(host, port, application, threaded):
            self.assertEqual((host, port, threaded), ("127.0.0.1", 8082, True))
            server = make_server(host, 0, self.viewer, threaded=threaded)
            servers.append(server)
            return server

        def operator(**kwargs):
            self.assertEqual(kwargs, {"host": "0.0.0.0", "port": 8081, "debug": False})
            address = f"http://127.0.0.1:{servers[0].server_port}/api/snapshot"
            with urlopen(address, timeout=3) as response:
                self.assertIn(b'"timestamp":1', response.read())
            self.state["timestamp"] = 2
            with urlopen(address, timeout=3) as response:
                self.assertIn(b'"timestamp":2', response.read())

        with patch.dict(os.environ, {"NETEM_SHOWROOM_HOST": "127.0.0.1", "NETEM_SHOWROOM_PORT": "8082"}), \
             patch("werkzeug.serving.make_server", side_effect=bind), \
             patch.object(netem, "restore_runtime_state") as restore, \
             patch.object(netem, "start_background_workers") as workers, \
             patch.object(netem.app, "run", side_effect=operator):
            netem.run_servers()
        restore.assert_called_once()
        workers.assert_called_once()
        self.assertEqual(servers[0].socket.fileno(), -1)

    def test_disabled_and_invalid_ports(self):
        with patch.object(netem, "restore_runtime_state"), \
             patch.object(netem, "start_background_workers"), \
             patch.object(netem.app, "run") as run, \
             patch("werkzeug.serving.make_server") as bind:
            with patch.dict(os.environ, {"NETEM_SHOWROOM_PORT": "0"}):
                netem.run_servers()
                bind.assert_not_called()
                run.assert_called_once()
            for port in ("8081", "-1", "65536", "invalid"):
                with patch.dict(os.environ, {"NETEM_SHOWROOM_PORT": port}):
                    with self.assertRaises(ValueError):
                        netem.run_servers()


if __name__ == "__main__":
    unittest.main()
