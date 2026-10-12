"""Inventory evidence, entitlement gates, TLS trust and routing transactions."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import app as netem
from appliance_inventory import APIError, FortiGateClient, Store, normalize_licenses, observe, readiness, validate


DEFINITION = dict(name="Lab FortiGate", vendor="fortinet", kind="vm", endpoint="https://192.0.2.10",
                  gateway="10.250.10.1", interface="eth1", target="198.18.0.1", firmware="7.6.7",
                  hypervisor="Proxmox", license_mode="FortiFlex", required_licenses="vm")


def observation(vm="vm_valid", ips="licensed"):
    return {"availability": "reachable", "api": "connected", "health": {"state": "healthy", "metrics": {"cpu": 10, "memory": 40}},
            "licenses": normalize_licenses({"vm": {"status": vm, "type": "flex"}, "ips": {"status": ips}}),
            "errors": [], "identity": {"firmware": "v7.6.7"}}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.id = self.store.save(validate(DEFINITION, netem.SDWAN_VENDORS), token="private-token")

    def test_restart_history_revision_and_credential_boundary(self):
        row = self.store.get(self.id)
        self.store.record(row, observation())
        restarted = Store(self.temp.name)
        self.assertEqual(restarted.get(self.id)["observation"]["licenses"][0]["state"], "valid")
        self.assertEqual(len(restarted.history(self.id)), 1)
        self.assertNotIn(b"private-token", restarted.path.read_bytes())
        changed = validate(dict(DEFINITION, endpoint="https://192.0.2.11"), netem.SDWAN_VENDORS)
        restarted.save(changed, self.id)
        self.assertEqual(restarted.token(self.id), "")
        restarted.record(row, observation("vm_expired"))
        self.assertIsNone(restarted.get(self.id)["observation"])
        self.assertEqual(len(restarted.history(self.id)), 1)

    def test_alert_acknowledgement_survives_checks_and_resets_after_recovery(self):
        row = self.store.get(self.id)
        events = self.store.record(row, observation("vm_expired"))
        self.assertEqual(len(events), 1)
        alert = self.store.alerts()[0]
        self.assertEqual(alert["severity"], "critical")
        self.store.acknowledge(alert["key"])
        self.assertEqual(self.store.record(row, observation("vm_expired")), [])
        self.assertIsNotNone(self.store.alerts()[0]["acknowledged_at"])
        self.assertEqual(self.store.record(row, observation())[0]["transition"], "cleared")
        self.store.record(row, observation("vm_expired"))
        self.assertIsNone(self.store.alerts()[0]["acknowledged_at"])

    def test_archive_removes_credential_but_retains_evidence(self):
        self.store.record(self.store.get(self.id), observation())
        self.store.archive(self.id)
        self.assertIsNone(self.store.get(self.id))
        self.assertEqual(self.store.token(self.id), "")
        self.assertEqual(len(self.store.history(self.id)), 1)

    def test_unknown_observation_does_not_claim_expired_license_recovered(self):
        row = self.store.get(self.id)
        self.store.record(row, observation("vm_expired"))
        failed = dict(observation(), api="unreachable", availability="unknown", licenses=[], health={"state": "unknown", "metrics": {}})
        transitions = self.store.record(row, failed)
        self.assertFalse(any(item["transition"] == "cleared" for item in transitions))
        self.assertTrue(any(alert["severity"] == "critical" for alert in self.store.alerts()))
        self.assertEqual(readiness(self.store.get(self.id))["critical"], [])

    def test_required_vm_and_optional_subscription_are_distinct(self):
        row = self.store.get(self.id)
        self.store.record(row, observation(ips="expired"))
        self.assertEqual(readiness(self.store.get(self.id))["critical"], [])
        self.store.record(row, observation("vm_expired"))
        self.assertEqual(readiness(self.store.get(self.id))["critical"], ["vm license expired"])
        self.assertEqual(readiness(self.store.get(self.id), time.time() + 1801)["critical"], [])

    def test_expiry_bands_and_unrecognized_state_never_pass(self):
        now = time.time()
        for days in (29, 13, 6):
            licenses = normalize_licenses({"vm": {"status": "vm_valid", "expires": now + days * 86400}}, now)
            self.assertEqual(licenses[0]["state"], "warning")
        self.assertEqual(normalize_licenses({"vm": {"status": "future-new-state"}})[0]["state"], "unknown")
        self.assertEqual(normalize_licenses({"vm": {"status": "vm_valid", "expires": now - 1}}, now)[0]["state"], "expired")
        self.assertEqual(normalize_licenses({"vm": {"status": "vm_invalid", "expires": float("nan")}})[0]["expires_at"], None)


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.row = validate(DEFINITION, netem.SDWAN_VENDORS)

    def test_auth_failure_is_reachable_with_unknown_health_and_license(self):
        factory = Mock()
        factory.return_value.get.side_effect = APIError("auth_error", "Access denied")
        result = observe(self.row, "token", factory)
        self.assertEqual((result["availability"], result["api"], result["health"]["state"], result["licenses"]),
                         ("reachable", "auth_error", "unknown", []))

    def test_partial_api_data_is_not_a_complete_health_result(self):
        factory = Mock()
        factory.return_value.get.side_effect = [{"results": {}, "version": "v7.6.7"},
                                                APIError("unsupported", "not supported"),
                                                {"results": {"vm": {"status": "vm_expired", "type": "flex"}}}]
        result = observe(self.row, "token", factory)
        self.assertEqual(result["api"], "partial")
        self.assertEqual(result["health"]["state"], "unknown")
        self.assertEqual(result["licenses"][0]["state"], "expired")

    def test_pin_checked_before_token_is_transmitted(self):
        conn = Mock()
        conn.sock.getpeercert.return_value = b"certificate"
        row = dict(self.row, self_signed=True, fingerprint="0" * 64)
        with patch("appliance_inventory.http.client.HTTPSConnection", return_value=conn):
            with self.assertRaises(APIError) as exc:
                FortiGateClient(row, "secret").get("license/status")
        self.assertEqual(exc.exception.category, "tls_error")
        conn.request.assert_not_called()
        conn.close.assert_called_once()

    def test_redirect_is_not_followed_and_body_is_not_exposed(self):
        conn = Mock()
        conn.sock.getpeercert.return_value = b"certificate"
        conn.getresponse.return_value.status = 302
        with patch("appliance_inventory.http.client.HTTPSConnection", return_value=conn):
            with self.assertRaisesRegex(APIError, "HTTP 302"):
                FortiGateClient(self.row, "secret").get("license/status")
        self.assertEqual(conn.request.call_count, 1)
        conn.getresponse.return_value.read.assert_not_called()

    def test_input_cannot_redirect_credentials_or_change_arbitrary_routes(self):
        for changes in ({"endpoint": "http://192.0.2.10"}, {"endpoint": "https://user:pass@example.com"},
                        {"endpoint": "https://example.com/api"}, {"self_signed": "1"},
                        {"target": "8.8.8.8"}, {"interface": "eth1;whoami"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate(dict(DEFINITION, **changes), netem.SDWAN_VENDORS)


class PlatformTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.platform = netem.APPLIANCE_PLATFORM
        mock = patch.object(self.platform, "store", Store(self.temp.name)); mock.start(); self.addCleanup(mock.stop)
        self.id = self.platform.store.save(validate(DEFINITION, netem.SDWAN_VENDORS))
        self.platform.store.state("selection", {"id": self.id, "active": True})
        for name in ("log_event",):
            mock = patch.object(netem, name); mock.start(); self.addCleanup(mock.stop)

    def test_preflight_blocks_expired_fortiflex_and_records_explicit_override(self):
        self.platform.store.record(self.platform.store.get(self.id), observation("vm_expired"))
        with netem.app.test_request_context("/lab/scenario/start", method="POST"), \
             patch.object(self.platform, "check_platform", return_value={}), \
             patch.object(self.platform, "check_appliance", side_effect=lambda row: row):
            with self.assertRaisesRegex(ValueError, "vm license expired"):
                self.platform.preflight({})
            self.platform.preflight({"appliance_override": "1", "appliance_override_reason": "Validate the failure mode"})
        self.assertTrue(any(call.args[0] == "appliance-override" for call in netem.log_event.call_args_list))

    def test_pending_route_cannot_be_overridden(self):
        self.platform.store.state("pending", {"id": "route-job", "appliance_id": self.id})
        with patch.object(self.platform, "check_platform", return_value={}), \
             self.assertRaisesRegex(ValueError, "route verification"):
            self.platform.preflight({"appliance_override": "1", "appliance_override_reason": "Allow this"})

    def test_select_waits_for_failure_and_keeps_previous_selection(self):
        other = self.platform.store.save(validate(dict(DEFINITION, name="Other", gateway="10.250.10.2"), netem.SDWAN_VENDORS))
        self.platform.store.state("selection", {"id": self.id, "active": True})
        with patch.object(self.platform, "check_platform", side_effect=[{}, {"network": {"job": {"id": "j", "state": "failed"}}}]), \
             patch.object(self.platform, "ensure_idle"), \
             patch.object(netem, "traffic_generator_request", side_effect=[{}, {"id": "j"}]), \
             patch("appliance_platform.threading.Event") as event:
            event.return_value.wait.return_value = False
            with self.assertRaisesRegex(ValueError, "Route verification failed"):
                self.platform.select(other)
        self.assertEqual(self.platform.store.state("selection")["id"], self.id)
        self.assertEqual(self.platform.store.state("pending"), {})

    def test_switch_rejected_during_tests_or_workloads(self):
        with patch.object(netem, "scenario_snapshot", return_value={"active": True}), \
             patch.object(netem, "traffic_generator_request") as api:
            with self.assertRaisesRegex(ValueError, "running test"):
                self.platform.ensure_idle()
            api.assert_not_called()
        with patch.object(netem, "scenario_snapshot", return_value={"active": False}), \
             patch.object(netem, "site_plan_snapshot", return_value={"active": False}), \
             patch.object(netem, "traffic_generator_request", return_value={"status": "running"}):
            with self.assertRaisesRegex(ValueError, "running workload"):
                self.platform.ensure_idle()

    def test_selection_changes_only_after_verified_simulator_state(self):
        self.platform.store.state("selection", {})
        route = {key: DEFINITION[key] for key in ("gateway", "target", "interface")}
        network = {"selected": route, "route_health": {"active": True}, "appliances": []}
        with patch.object(self.platform, "configured", return_value=True), \
             patch.object(netem, "traffic_generator_request", side_effect=[{"service": "netem-traffic-simulator"}, network, {"ready": True}, {}, {}]):
            self.platform.check_platform()
        self.assertEqual(self.platform.store.state("selection")["id"], self.id)

    def test_monitor_uses_monotonic_schedule_without_browser(self):
        # Two cycles, 15 minutes apart, each collected by the worker itself.
        stop = Mock()
        stop.is_set.side_effect = [False, False, False, False, True]
        with patch.object(netem, "BACKGROUND_STOP", stop), \
             patch("appliance_platform.time.monotonic", side_effect=[0, 0, 0, 901, 901, 901]), \
             patch.object(self.platform, "check_appliance") as check:
            self.platform.worker()
        self.assertEqual(check.call_count, 2)

    def test_pages_and_read_api_do_not_expose_credentials_and_mutations_need_token(self):
        self.platform.store.save(validate(DEFINITION, netem.SDWAN_VENDORS), self.id, token="never-export-me")
        client = netem.app.test_client()
        for page in ("/appliances", "/platform-health", "/appliances?detail=" + self.id, "/api/v1/appliances"):
            response = client.get(page)
            self.assertEqual(response.status_code, 200, page)
            self.assertNotIn("never-export-me", response.text)
        self.assertEqual(client.post("/appliances/select", data={"id": self.id}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
