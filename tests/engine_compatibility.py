"""Actual old/new platform consumers with deterministic simulator envelopes.
Run after combining the companion scoring PR; old checkout is pinned in CI.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def consumer(root, payload):
    return json.loads(subprocess.check_output([sys.executable, str(Path(__file__).resolve()),
        "--consumer", str(root), str(payload)], text=True))


def check():
    sys.path.insert(0, os.environ["SIMULATOR_SOURCE"])
    from trafficgen.dem import summarize_rows
    rows = [dict(application="web_saas", success=True, response_time_ms=100, response_length=100)] * 10000
    rows += [dict(application="erp", success=False, response_time_ms=None, response_length=0)] * 5
    summary = summarize_rows(rows, application_shares={"web_saas": .999, "erp": .001})
    summary["interactive_p95_ms"] = 100
    summary["applications"] = {}
    old_root = ROOT / "_legacy_platform"
    with tempfile.TemporaryDirectory() as directory:
        payload = Path(directory) / "summary.json"
        payload.write_text(json.dumps(summary))
        old = consumer(old_root, payload)
        new = consumer(ROOT, payload)
        assert old["verdicts"]["experience"] == "pass", old
        assert new["verdicts"]["experience"] == "fail", new
        legacy = {"requests": 100, "experience_score": 100, "availability_pct": 100, "interactive_p95_ms": 100}
        payload.write_text(json.dumps(legacy))
        new_with_old = consumer(ROOT, payload)
        assert new_with_old["verdicts"]["experience"] == "unknown", new_with_old
    results = {"old_platform_new_simulator": "renders but cannot enforce critical acceptance; unsupported",
               "new_platform_old_simulator": "unknown; cannot pass missing evidence",
               "new_platform_new_simulator": "critical failure preserved alongside nonzero utility",
               "rollout": "coordinated maintenance window required"}
    (ROOT / "engine-compatibility-results.json").write_text(json.dumps(results, indent=2))
    print("PASS mixed-version consumers and coordinated-upgrade guard expectations")


if __name__ == "__main__":
    if "--consumer" in sys.argv:
        sys.path.insert(0, sys.argv[2])
        import app
        summary = json.loads(Path(sys.argv[3]).read_text())
        print(json.dumps(app.showroom_outcome({"traffic_generator": {"connected": True, "status": {"dem": summary}}}, None)["experience"]))
    else:
        check()
