"""A WAN whose port drops out of its bridge passes nothing while its shaping still looks applied.
NetEm finds it, puts the port back while it manages bridges, and says so."""
import json
import unittest
from unittest.mock import patch

import app as netem

CONFIG = {"restore_bridges_on_startup": True, "wan_links": [
    {"id": "wan1", "name": "WAN1", "bridge": "br-wan1", "inner": "enp6s19", "outer": "enp6s20"},
    {"id": "wan2", "name": "WAN2", "bridge": "br-wan2", "inner": "enp6s21", "outer": "enp6s22"}]}
UP = ["BROADCAST", "MULTICAST", "UP", "LOWER_UP"]


def links(**changes):
    """`ip -j link show` for the lab: both bridges whole unless a change says otherwise."""
    rows = {"br-wan1": {"ifname": "br-wan1", "flags": UP}, "br-wan2": {"ifname": "br-wan2", "flags": UP},
            "enp6s19": {"ifname": "enp6s19", "flags": UP, "master": "br-wan1"},
            "enp6s20": {"ifname": "enp6s20", "flags": UP, "master": "br-wan1"},
            "enp6s21": {"ifname": "enp6s21", "flags": UP, "master": "br-wan2"},
            "enp6s22": {"ifname": "enp6s22", "flags": UP, "master": "br-wan2"}}
    for name, change in changes.items():
        if change is None:
            rows.pop(name)
        else:
            rows[name] = {key: value for key, value in dict(rows[name], **change).items() if value is not None}
    return json.dumps(list(rows.values()))


class Host:
    """Stands in for the VM: answers `ip -j link show` and records every other command."""

    def __init__(self, listing, fixed=None):
        self.listing, self.fixed, self.commands = listing, fixed, []

    def __call__(self, command):
        if command.endswith("-j link show"):
            return 0, self.listing, ""
        self.commands.append(command.replace(netem.IP, "ip"))
        if self.fixed is not None:
            self.listing = self.fixed
        return 0, "", ""


class ProblemTests(unittest.TestCase):
    def test_the_lab_case_a_port_outside_its_bridge(self):
        with patch.object(netem, "run_cmd", Host(links(enp6s21={"master": None}))):
            problems = netem.wan_bridge_problems(CONFIG)
        self.assertEqual([(problem["label"], problem["issues"], problem["fixable"]) for problem in problems],
                         [("WAN2", ["enp6s21 is not in br-wan2"], True)])

    def test_every_kind_of_break_is_named(self):
        cases = [(links(**{"br-wan2": None, "enp6s21": {"master": None}, "enp6s22": {"master": None}}),
                  ["bridge br-wan2 is missing", "enp6s21 is not in br-wan2", "enp6s22 is not in br-wan2"], True),
                 (links(**{"br-wan2": {"flags": ["BROADCAST"]}}), ["bridge br-wan2 is down"], True),
                 (links(enp6s21={"flags": ["BROADCAST", "MULTICAST"]}), ["enp6s21 is down"], True),
                 (links(enp6s21={"flags": ["BROADCAST", "MULTICAST", "UP"]}), ["enp6s21 has no link"], False),
                 (links(enp6s21=None), ["enp6s21 is missing"], False)]
        for listing, issues, fixable in cases:
            with self.subTest(issues=issues), patch.object(netem, "run_cmd", Host(listing)):
                problem = netem.wan_bridge_problems(CONFIG)[0]
                self.assertEqual((problem["issues"], problem["fixable"]), (issues, fixable))
        with patch.object(netem, "run_cmd", Host(links())):
            self.assertEqual(netem.wan_bridge_problems(CONFIG), [])
        with patch.object(netem, "run_cmd", return_value=(1, "", "no ip")):
            self.assertIsNone(netem.wan_bridge_problems(CONFIG))

    def test_repair_puts_the_port_back_without_disturbing_the_other(self):
        host = Host(links(enp6s21={"master": None}))
        with patch.object(netem, "run_cmd", host):
            errors = netem.repair_wan_bridge(netem.wan_bridge_problems(CONFIG)[0])
        self.assertEqual((errors, host.commands), ([], ["ip link set enp6s21 master br-wan2", "ip link set br-wan2 up"]))
        host = Host(links(**{"br-wan2": None, "enp6s21": {"master": None, "flags": ["BROADCAST"]}, "enp6s22": {"master": None}}))
        with patch.object(netem, "run_cmd", host):
            netem.repair_wan_bridge(netem.wan_bridge_problems(CONFIG)[0])
        self.assertEqual(host.commands, ["ip link add name br-wan2 type bridge", "ip link set enp6s21 master br-wan2",
                                         "ip link set enp6s21 up", "ip link set enp6s22 master br-wan2", "ip link set br-wan2 up"])


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        state = {"checked_at": None, "problems": [], "attempts": {}, "reported": {}}
        for name, value in (("BRIDGE_STATE", state), ("load_config", lambda: dict(self.config))):
            mock = patch.object(netem, name, value)
            mock.start()
            self.addCleanup(mock.stop)
        mock = patch.object(netem, "log_event")
        self.log = mock.start()
        self.addCleanup(mock.stop)
        self.config = dict(CONFIG)

    def messages(self):
        return [call.args[1] for call in self.log.call_args_list]

    def test_managed_bridges_are_repaired_once_and_reported(self):
        host = Host(links(enp6s21={"master": None}), fixed=links())
        with patch.object(netem, "run_cmd", host):
            self.assertEqual(netem.check_wan_bridges(), [])
        self.assertEqual(self.messages(), ["WAN2: bridge broken (enp6s21 is not in br-wan2), repairing", "WAN2: bridge repaired"])
        self.assertEqual(netem.wan_bridge_snapshot(), [])

    def test_a_failing_repair_backs_off_and_stays_reported(self):
        host = Host(links(enp6s21={"master": None}))
        with patch.object(netem, "run_cmd", host):
            netem.check_wan_bridges()
            netem.check_wan_bridges()
        self.assertEqual(host.commands, ["ip link set enp6s21 master br-wan2", "ip link set br-wan2 up"])
        self.assertEqual(self.messages(), ["WAN2: bridge broken (enp6s21 is not in br-wan2), repairing"])
        self.assertEqual([problem["link_id"] for problem in netem.wan_bridge_snapshot()], ["wan2"])

    def test_unmanaged_bridges_are_only_reported(self):
        self.config["restore_bridges_on_startup"] = False
        host = Host(links(enp6s21={"master": None}))
        with patch.object(netem, "run_cmd", host):
            netem.check_wan_bridges()
            netem.check_wan_bridges()
            host.listing = links()
            netem.check_wan_bridges()
        self.assertEqual(host.commands, [])
        self.assertEqual(self.messages(), ["WAN2: bridge broken (enp6s21 is not in br-wan2)"])
        self.assertEqual(netem.BRIDGE_STATE["reported"], {})


class ReportTests(unittest.TestCase):
    PROBLEM = {"link_id": "wan2", "label": "WAN2", "issues": ["enp6s21 is not in br-wan2"]}

    def test_diagnosis_and_traffic_path_name_the_broken_bridge(self):
        finding = netem.bridge_finding([self.PROBLEM])
        self.assertEqual((finding["severity"], finding["title"], finding["detail"]),
                         ("bad", "WAN2 passes no traffic: its bridge is broken", "WAN2: enp6s21 is not in br-wan2."))
        self.assertIsNone(netem.bridge_finding([]))
        summary = netem.traffic_path_summary({"available": True, "ready": False, "wan_bridges": [self.PROBLEM],
                                              "message": "The controlled target at 198.18.0.1 did not answer."})
        self.assertEqual(summary, "Traffic path not ready: The controlled target at 198.18.0.1 did not answer. "
                                  "NetEm found WAN2: enp6s21 is not in br-wan2.")

    def test_bridges_are_checked_before_simulated_users_start(self):
        calls = []
        with patch.object(netem, "check_wan_bridges", side_effect=lambda: calls.append("bridges")), \
             patch.object(netem, "traffic_path_readiness", side_effect=lambda max_age=0: calls.append("path") or {"ready": True, "available": True}):
            netem.ensure_traffic_path("Test")
        self.assertEqual(calls, ["bridges", "path"])


if __name__ == "__main__":
    unittest.main()



class SamplerIsolationTests(unittest.TestCase):
    def test_slow_bridge_check_does_not_hold_up_rate_samples(self):
        import threading
        stop, checking, release = threading.Event(), threading.Event(), threading.Event()
        samples = []
        def slow_check():
            checking.set()
            release.wait(2)
        def collect():
            samples.append(True)
            if len(samples) >= 3:
                stop.set()
        with patch.object(netem, 'BACKGROUND_STOP', stop), \
             patch.object(netem, 'BRIDGE_CHECK_SECONDS', .001), \
             patch.object(netem, 'TELEMETRY_SAMPLE_SECONDS', .01), \
             patch.object(netem, 'check_wan_bridges', side_effect=slow_check), \
             patch.object(netem, 'init_telemetry_db'), \
             patch.object(netem, 'discard_future_samples', return_value=0), \
             patch.object(netem, 'collect_telemetry_sample', side_effect=collect), \
             patch.object(netem, 'record_host_sample'):
            bridge = threading.Thread(target=netem.bridge_worker, daemon=True)
            sampler = threading.Thread(target=netem.telemetry_worker, daemon=True)
            bridge.start()
            try:
                self.assertTrue(checking.wait(1))
                sampler.start()
                sampler.join(1)
                self.assertFalse(sampler.is_alive())
                self.assertEqual(len(samples), 3)
                self.assertFalse(release.is_set())
            finally:
                stop.set()
                release.set()
                bridge.join(2)
                if sampler.ident is not None:
                    sampler.join(2)
