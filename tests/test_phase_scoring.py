"""A phase is scored from its own window: rolling samples from a neighbouring phase do not change it."""
import unittest
from unittest.mock import patch

import app as netem


def record(phase_ends, samples=()):
    return {"phases": ["Baseline", "Severe brownout", "Recovery"],
            "phase_marks": [{"name": "Baseline", "at": 0.0}, {"name": "Severe brownout", "at": 60.0},
                            {"name": "Recovery", "at": 120.0}],
            "samples": list(samples), "phase_ends": phase_ends, "assertions": [], "reactions": [],
            "steering": {}, "links": []}


def window(experience, success, requests, p95=50.0):
    return {"experience": experience, "success": success, "requests": requests, "interactive_p95_ms": p95, "scoring_complete": requests >= 20}


class PhaseWindowTests(unittest.TestCase):
    def test_each_phase_takes_its_own_window_not_a_neighbours_rolling_sample(self):
        samples = [{"t": 130.0, "phase": "Recovery", "experience": 100, "success": 79.4, "interactive_p95_ms": 60}]
        ends = [{"name": "Baseline", "dem": window(100, 100.0, 300)},
                {"name": "Severe brownout", "dem": window(0, 79.4, 240, 180.0)},
                {"name": "Recovery", "dem": window(100, 100.0, 280)}]
        summary = netem.summarize_test(record(ends, samples), "passed", None,
                                     targets={"experience_min": 80, "success_min_pct": 99, "interactive_p95_max_ms": 400})
        by_name = {phase["name"]: phase for phase in summary["phases"]}
        self.assertEqual(by_name["Severe brownout"]["experience_score"], 0)
        self.assertEqual(by_name["Recovery"]["success_pct"], 100.0)
        self.assertEqual(by_name["Recovery"]["worst_success_pct"], 100.0)
        self.assertEqual([phase['experience_verdict'] for phase in summary['phases']], ['pass', 'fail', 'pass'])

    def test_small_phase_shows_measured_utility_but_has_no_target_verdict(self):
        ends = [{"name": "Severe brownout", "dem": window(0, 66.7, 6)}]
        summary = netem.summarize_test(record(ends), "passed", None)
        phase = next(item for item in summary["phases"] if item["name"] == "Severe brownout")
        self.assertEqual(phase["experience_score"], 0)
        self.assertIn("Incomplete phase evidence (6 transactions)", phase["note"])

    def test_without_an_exact_window_rolling_samples_are_not_substituted(self):
        samples = [{"t": 30.0, "phase": "Baseline", "experience": 92, "success": 99.0, "interactive_p95_ms": 55}]
        summary = netem.summarize_test(record([], samples), "passed", None)
        baseline = next(item for item in summary["phases"] if item["name"] == "Baseline")
        self.assertIsNone(baseline["experience_score"])
        self.assertIn("unavailable", baseline["note"])


class PhaseCloseTests(unittest.TestCase):
    def test_closing_a_phase_records_the_window_of_its_duration(self):
        state = {"active": True, "phase_marks": [{"name": "Baseline", "at": 1000.0}], "phase_ends": []}
        with patch.dict(netem.SCENARIO_STATE, {"workload_run_id": "run-test"}), patch.dict(netem.TEST_RECORDER, state, clear=True), \
             patch.object(netem.time, "time", return_value=1060.0), \
             patch.object(netem, "traffic_generator_request", return_value={"experience_score": 100,
                                                                             "availability_pct": 100.0,
                                                                             "requests": 310,
                                                                             "interactive_p95_ms": 52.0}) as dem:
            netem.recorder_close_phase()
            self.assertEqual(netem.TEST_RECORDER["phase_ends"][0]["name"], "Baseline")
            self.assertEqual(netem.TEST_RECORDER["phase_ends"][0]["dem"]["requests"], 310)
        dem.assert_called_once_with("/api/v1/dem/summary?run_id=run-test&since=1000.0&until=1060.0", timeout=7.0)

    def test_nothing_is_closed_when_no_test_is_recording(self):
        with patch.dict(netem.TEST_RECORDER, {"active": False, "phase_marks": [], "phase_ends": []}, clear=True), \
             patch.object(netem, "traffic_generator_request") as dem:
            netem.recorder_close_phase()
        dem.assert_not_called()


def window_with_apps(applications, experience=100, success=100.0, requests=300):
    dem = window(experience, success, requests)
    dem["applications"] = applications
    return dem


class ApplicationGroupTests(unittest.TestCase):
    def applications(self):
        return {"ot_telemetry": {"class": "realtime", "requests": 40, "scoring_complete": True, "success": 100.0, "p95_ms": 52.0, "experience": 98},
                "erp": {"class": "interactive", "requests": 30, "scoring_complete": True, "success": 90.0, "p95_ms": 900.0, "experience": 60},
                "mes": {"class": "interactive", "requests": 60, "scoring_complete": True, "success": 100.0, "p95_ms": 120.0, "experience": 100},
                "plm_cad": {"class": "bulk", "requests": 5, "scoring_complete": False, "success": 100.0, "p95_ms": 8000.0, "experience": 100}}

    def groups(self):
        ends = [{"name": "Baseline", "dem": window_with_apps(self.applications())}]
        summary = netem.summarize_test(record(ends), "passed", None)
        return {group["label"]: group for group in summary["application_groups"]}

    def test_each_application_keeps_its_own_figures_in_the_phase(self):
        ends = [{"name": "Baseline", "dem": window_with_apps(self.applications())}]
        phase = netem.summarize_test(record(ends), "passed", None)["phases"][0]
        self.assertEqual(phase["applications"]["erp"]["experience"], 60)
        self.assertEqual(phase["applications"]["erp"]["p95_ms"], 900.0)

    def test_applications_are_grouped_by_traffic_class(self):
        self.assertEqual(list(self.groups()), ["Voice & video", "Web, collaboration & DNS", "File transfers"])

    def test_a_group_pools_its_scored_applications(self):
        web = self.groups()["Web, collaboration & DNS"]
        self.assertEqual(web["cells"]["Baseline"], "80 · 96.7% · 900 ms (90 tx) [worst app P95]")
        erp = next(app for app in web["apps"] if app["key"] == "erp")
        self.assertEqual(erp["cells"]["Baseline"], "60 · 90.0% · 900 ms (30 tx)")

    def test_small_application_score_is_labelled_limited_evidence(self):
        plm = self.groups()["File transfers"]["apps"][0]
        self.assertEqual(plm["key"], "plm_cad")
        self.assertEqual(plm["cells"]["Baseline"], "100 · 100.0% · 8000 ms (5 tx) [limited evidence]")

    def test_a_phase_without_a_window_has_no_application_figures(self):
        summary = netem.summarize_test(record([]), "passed", None)
        self.assertEqual(summary["application_groups"], [])
        self.assertEqual(summary["phases"][0]["applications"], {})


if __name__ == "__main__":
    unittest.main()
