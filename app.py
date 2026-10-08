#!/usr/bin/env python3
import copy
import csv
import io
import json
import math
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    jsonify,
    Response,
    send_file,
)

app = Flask(__name__)
app.secret_key = "techkarma-netem"  

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
VERSION_PATH = BASE_DIR / "version.txt"
RUNTIME_DIR = BASE_DIR / "runtime"
EVENT_LOG_PATH = RUNTIME_DIR / "events.jsonl"
CAPTURE_DIR = RUNTIME_DIR / "captures"

TC = "/usr/sbin/tc"
IP = "/usr/sbin/ip"
GIT = "/usr/bin/git"
UPDATE_BRANCH = "main"

RUNTIME_LOCK = threading.Lock()
ACTIVE_FAULTS = {}
RUNTIME_EFFECTIVE = {}
EVENT_LOG = []
SCENARIO_STOP = threading.Event()
SCENARIO_STATE = {
    "active": False,
    "scenario_id": None,
    "scenario_name": None,
    "link_id": None,
    "started_at": None,
    "step": 0,
    "step_label": None,
}
ORIGINAL_MTUS = {}
CAPTURE_PROCESS = None
CAPTURE_STATE = {
    "active": False,
    "link_id": None,
    "interface": None,
    "started_at": None,
    "duration": None,
    "path": None,
    "error": None,
}


# ---------- Helper: shell ----------

def run_cmd(cmd: str):
    """Run command and return (rc, stdout, stderr)."""
    proc = subprocess.Popen(
        cmd,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    out, err = proc.communicate()
    return proc.returncode, (out or "").strip(), (err or "").strip()


def run_process(args, timeout=45):
    """Run a command without a shell from the application directory."""
    try:
        proc = subprocess.run(
            args,
            cwd=BASE_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", str(exc)


def git_update_status(fetch=False):
    """
    Compare the installed checkout against the stable update channel.

    The application always checks origin/main, regardless of the local branch
    name. This lets older installations that were originally deployed from a
    feature branch continue receiving stable releases after that branch is
    merged or deleted.
    """
    status = {
        "ok": False,
        "error": "",
        "branch": "",
        "target_branch": UPDATE_BRANCH,
        "commit": "",
        "subject": "",
        "remote_url": "",
        "behind": 0,
        "ahead": 0,
        "dirty": False,
        "installed_version": get_app_version(),
        "remote_version": None,
    }

    rc, branch, err = run_process([GIT, "branch", "--show-current"])
    if rc != 0 or not branch:
        status["error"] = err or "Unable to determine the current Git branch."
        return status
    status["branch"] = branch

    rc, remote_url, _ = run_process([GIT, "remote", "get-url", "origin"])
    if rc == 0:
        status["remote_url"] = remote_url

    if fetch:
        rc, _out, err = run_process(
            [GIT, "fetch", "--prune", "origin", UPDATE_BRANCH],
            timeout=90,
        )
        if rc != 0:
            status["error"] = err or "git fetch failed."
            return status

    rc, commit_line, err = run_process([GIT, "log", "-1", "--pretty=%h%x09%s"])
    if rc != 0:
        status["error"] = err or "Unable to read current Git commit."
        return status
    if "\t" in commit_line:
        status["commit"], status["subject"] = commit_line.split("\t", 1)
    else:
        status["commit"] = commit_line

    rc, dirty, _ = run_process([GIT, "status", "--porcelain", "--untracked-files=no"])
    status["dirty"] = rc != 0 or bool(dirty.strip())

    remote_ref = f"origin/{UPDATE_BRANCH}"
    rc, remote_version, _ = run_process([GIT, "show", f"{remote_ref}:version.txt"])
    if rc == 0 and remote_version:
        status["remote_version"] = remote_version.strip()

    rc, _out, _err = run_process([GIT, "rev-parse", "--verify", remote_ref])
    if rc != 0:
        status["error"] = f"Stable update branch {remote_ref} was not found."
        return status

    rc, behind, err = run_process([GIT, "rev-list", "--count", f"HEAD..{remote_ref}"])
    if rc != 0:
        status["error"] = err or "Unable to compare local and stable versions."
        return status

    rc, ahead, err = run_process([GIT, "rev-list", "--count", f"{remote_ref}..HEAD"])
    if rc != 0:
        status["error"] = err or "Unable to compare local and stable versions."
        return status

    status["behind"] = int(behind or 0)
    status["ahead"] = int(ahead or 0)
    status["ok"] = True
    return status


def restart_after_update():
    """Let the HTTP response leave first, then rely on systemd Restart=on-failure."""
    time.sleep(1.5)
    os._exit(75)


# ---------- Config ----------

def get_app_version():
    try:
        return VERSION_PATH.read_text().strip() or "dev"
    except OSError:
        return "dev"


def load_config():
    if CONFIG_PATH.exists():
        try:
            with CONFIG_PATH.open() as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_config(cfg: dict):
    tmp = CONFIG_PATH.with_suffix(".tmp")
    with tmp.open("w") as f:
        json.dump(cfg, f, indent=2)
    tmp.replace(CONFIG_PATH)


DEFAULT_PRESETS = {
    "dia": {
        "name": "DIA",
        "quality_model": "dia",
        "delay_ms": 5.0,
        "jitter_ms": 1.0,
        "loss_pct": 0.0,
        "download_mbit": 1000.0,
        "upload_mbit": 1000.0,
    },
    "dsl": {
        "name": "DSL",
        "quality_model": "dsl",
        "delay_ms": 25.0,
        "jitter_ms": 8.0,
        "loss_pct": 0.1,
        "download_mbit": 100.0,
        "upload_mbit": 20.0,
    },
    "broadband": {
        "name": "Broadband",
        "quality_model": "broadband",
        "delay_ms": 15.0,
        "jitter_ms": 5.0,
        "loss_pct": 0.1,
        "download_mbit": 300.0,
        "upload_mbit": 50.0,
    },
    "4g": {
        "name": "4G",
        "quality_model": "mobile",
        "delay_ms": 45.0,
        "jitter_ms": 20.0,
        "loss_pct": 0.5,
        "download_mbit": 80.0,
        "upload_mbit": 20.0,
    },
    "5g": {
        "name": "5G",
        "quality_model": "mobile",
        "delay_ms": 20.0,
        "jitter_ms": 8.0,
        "loss_pct": 0.2,
        "download_mbit": 300.0,
        "upload_mbit": 50.0,
    },
    "satellite": {
        "name": "Satellite",
        "quality_model": "satellite",
        "delay_ms": 300.0,
        "jitter_ms": 30.0,
        "loss_pct": 0.5,
        "download_mbit": 100.0,
        "upload_mbit": 20.0,
    },
}


QUALITY_MODELS = {
    "dia": "DIA / highly stable",
    "dsl": "DSL / copper access",
    "broadband": "Broadband / shared fixed access",
    "mobile": "Mobile / 4G-5G",
    "satellite": "Satellite",
}

BANDWIDTH_OPTIONS = [
    1, 2, 5, 10, 20, 25, 50, 75, 100, 150, 200, 300, 500,
    1000, 2000, 2500, 5000, 10000,
]

DEFAULT_SCENARIOS = [
    {
        "id": "progressive_brownout",
        "name": "Progressive brownout",
        "description": "Gradually degrades one WAN, holds it in a poor state, then restores it.",
        "steps": [
            {"after": 0, "action": "quality", "value": 100, "label": "Nominal"},
            {"after": 10, "action": "quality", "value": 80, "label": "Minor degradation"},
            {"after": 10, "action": "quality", "value": 60, "label": "Noticeable degradation"},
            {"after": 10, "action": "quality", "value": 40, "label": "Severe brownout"},
            {"after": 20, "action": "quality", "value": 70, "label": "Partial recovery"},
            {"after": 10, "action": "quality", "value": 100, "label": "Recovered"},
        ],
    },
    {
        "id": "sla_failover",
        "name": "SLA failover",
        "description": "Starts healthy, blackholes the WAN while link state remains up, then restores it.",
        "steps": [
            {"after": 0, "action": "quality", "value": 100, "label": "Nominal"},
            {"after": 10, "action": "fault", "value": "blackhole", "label": "Blackhole"},
            {"after": 30, "action": "fault", "value": "normal", "label": "Connectivity restored"},
        ],
    },
    {
        "id": "flaky_underlay",
        "name": "Flaky underlay",
        "description": "Alternates between healthy and one-way failure to exercise SLA hysteresis.",
        "steps": [
            {"after": 0, "action": "quality", "value": 100, "label": "Nominal"},
            {"after": 8, "action": "fault", "value": "downstream_blackhole", "label": "Downstream failure"},
            {"after": 8, "action": "fault", "value": "normal", "label": "Recovered"},
            {"after": 8, "action": "fault", "value": "upstream_blackhole", "label": "Upstream failure"},
            {"after": 8, "action": "fault", "value": "normal", "label": "Recovered"},
        ],
    },
    {
        "id": "availability_stress",
        "name": "Availability stress / DDoS impact",
        "description": "Safely emulates the WAN impact of a saturation event without generating attack traffic.",
        "steps": [
            {"after": 0, "action": "quality", "value": 100, "label": "Nominal"},
            {"after": 8, "action": "quality", "value": 60, "label": "Congestion begins"},
            {"after": 8, "action": "quality", "value": 30, "label": "Heavy saturation impact"},
            {"after": 12, "action": "quality", "value": 10, "label": "Severe availability impact"},
            {"after": 15, "action": "quality", "value": 70, "label": "Attack subsides"},
            {"after": 10, "action": "quality", "value": 100, "label": "Recovered"},
        ],
    },
]


DEFAULT_SLA_PROFILE = {
    "name": "Generic business SLA",
    "latency_ms": 100.0,
    "jitter_ms": 30.0,
    "loss_pct": 2.0,
}


def get_scenarios(cfg: dict):
    """Return built-in scenarios plus validated user-defined scenarios."""
    scenarios = json.loads(json.dumps(DEFAULT_SCENARIOS))
    for item in cfg.get("custom_scenarios", []):
        if isinstance(item, dict) and item.get("id") and item.get("steps"):
            scenarios.append(item)
    return scenarios


def validate_scenario_steps(raw_steps):
    """Validate a compact vendor-neutral scenario definition."""
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ValueError("Scenario must contain at least one step.")
    if len(raw_steps) > 30:
        raise ValueError("A scenario can contain at most 30 steps.")

    validated = []
    for index, step in enumerate(raw_steps, start=1):
        if not isinstance(step, dict):
            raise ValueError(f"Step {index} must be an object.")

        action = str(step.get("action", "")).strip()
        if action not in ("quality", "fault", "mtu"):
            raise ValueError(
                f"Step {index}: action must be quality, fault or mtu."
            )

        try:
            after = max(0, min(3600, int(step.get("after", 0))))
        except (TypeError, ValueError):
            raise ValueError(f"Step {index}: after must be an integer.")

        value = step.get("value")
        if action == "quality":
            try:
                value = max(0, min(100, int(value)))
            except (TypeError, ValueError):
                raise ValueError(f"Step {index}: quality must be 0-100.")
        elif action == "fault":
            if value not in (
                "normal",
                "blackhole",
                "downstream_blackhole",
                "upstream_blackhole",
            ):
                raise ValueError(f"Step {index}: unsupported fault.")
        elif action == "mtu":
            try:
                value = int(value)
            except (TypeError, ValueError):
                raise ValueError(f"Step {index}: MTU must be an integer.")
            if value != 0 and not 576 <= value <= 9000:
                raise ValueError(
                    f"Step {index}: MTU must be 576-9000, or 0 to restore."
                )

        validated.append(
            {
                "after": after,
                "action": action,
                "value": value,
                "label": str(step.get("label") or action)[:80],
            }
        )
    return validated


def get_sla_profile(cfg: dict):
    stored = cfg.get("sla_profile")
    if not isinstance(stored, dict):
        return dict(DEFAULT_SLA_PROFILE)
    return {
        "name": str(stored.get("name") or DEFAULT_SLA_PROFILE["name"])[:80],
        "latency_ms": max(0.0, float(stored.get("latency_ms", 100.0))),
        "jitter_ms": max(0.0, float(stored.get("jitter_ms", 30.0))),
        "loss_pct": min(
            100.0, max(0.0, float(stored.get("loss_pct", 2.0)))
        ),
    }


def evaluate_sla(effective: dict, sla: dict, fault="normal"):
    checks = {
        "latency": float(effective.get("delay_ms", 0.0)) <= sla["latency_ms"],
        "jitter": float(effective.get("jitter_ms", 0.0)) <= sla["jitter_ms"],
        "loss": float(effective.get("loss_pct", 0.0)) <= sla["loss_pct"],
        "data_plane": fault == "normal",
    }
    return {
        "pass": all(checks.values()),
        "checks": checks,
    }


def get_presets(cfg: dict):
    """Return editable presets and migrate older stored presets in place."""
    stored = cfg.get("presets")
    if not stored:
        presets = json.loads(json.dumps(DEFAULT_PRESETS))
        cfg["presets"] = presets
        save_config(cfg)
        return presets

    changed = False
    for preset_id, preset in stored.items():
        default = DEFAULT_PRESETS.get(preset_id, {})
        if "quality_model" not in preset:
            preset["quality_model"] = default.get("quality_model", "broadband")
            changed = True

    if changed:
        save_config(cfg)
    return stored


def quality_status(quality: int):
    quality = max(0, min(100, int(quality)))
    if quality == 0:
        return "Down"
    if quality >= 90:
        return "Excellent"
    if quality >= 75:
        return "Good"
    if quality >= 50:
        return "Fair"
    if quality >= 25:
        return "Poor"
    return "Critical"


def _curve_value(quality: int, points):
    """
    Interpolate staged degradation anchors with exponential easing.

    Different metrics begin degrading at different quality levels. Between
    anchors, deterioration accelerates toward the worse state instead of
    changing linearly.
    """
    q = max(0, min(100, int(quality)))
    points = sorted(points, key=lambda item: item[0], reverse=True)

    if q >= points[0][0]:
        return float(points[0][1])
    if q <= points[-1][0]:
        return float(points[-1][1])

    for (q_high, v_high), (q_low, v_low) in zip(points, points[1:]):
        if q_high >= q >= q_low:
            span = q_high - q_low
            t = 0.0 if span == 0 else (q_high - q) / span

            # Exponential easing reaches the degraded side faster than a
            # straight line while still honoring the configured anchors.
            k = 2.2
            eased = (1.0 - math.exp(-k * t)) / (1.0 - math.exp(-k))
            return float(v_high) + (float(v_low) - float(v_high)) * eased

    return float(points[-1][1])


QUALITY_CURVES = {
    # DIA is clean while healthy, but a 40% link is intentionally very poor:
    # congestion/failure simulation should be obvious, not "800 Mbit/s but bad".
    "dia": {
        "delay_factor": [
            (100, 1.0), (90, 1.0), (75, 1.2), (60, 2.5),
            (50, 4.5), (40, 8.0), (25, 20.0), (10, 60.0),
        ],
        "jitter_factor": [
            (100, 1.0), (90, 1.2), (75, 2.0), (60, 5.0),
            (50, 9.0), (40, 18.0), (25, 45.0), (10, 120.0),
        ],
        "loss_add": [
            (100, 0.0), (75, 0.0), (60, 0.25), (50, 0.8),
            (40, 3.0), (25, 12.0), (10, 35.0),
        ],
        "download_factor": [
            (100, 1.0), (90, 1.0), (75, 0.98), (60, 0.85),
            (50, 0.60), (40, 0.35), (25, 0.12), (10, 0.03),
        ],
        "upload_factor": [
            (100, 1.0), (90, 1.0), (75, 0.98), (60, 0.85),
            (50, 0.60), (40, 0.35), (25, 0.12), (10, 0.03),
        ],
    },

    # DSL usually shows errors/jitter before a full line-rate collapse.
    # Upstream degrades more aggressively because it is typically scarcer.
    "dsl": {
        "delay_factor": [
            (100, 1.0), (90, 1.0), (75, 1.15), (60, 1.6),
            (50, 2.5), (40, 4.0), (25, 10.0), (10, 30.0),
        ],
        "jitter_factor": [
            (100, 1.0), (90, 1.2), (75, 2.0), (60, 3.5),
            (50, 6.0), (40, 10.0), (25, 25.0), (10, 70.0),
        ],
        "loss_add": [
            (100, 0.0), (85, 0.0), (65, 0.15), (50, 1.0),
            (40, 3.0), (25, 12.0), (10, 35.0),
        ],
        "download_factor": [
            (100, 1.0), (90, 1.0), (75, 0.95), (60, 0.80),
            (50, 0.60), (40, 0.40), (25, 0.15), (10, 0.03),
        ],
        "upload_factor": [
            (100, 1.0), (90, 0.98), (75, 0.85), (60, 0.65),
            (50, 0.40), (40, 0.22), (25, 0.08), (10, 0.02),
        ],
    },

    # Shared fixed broadband tends to show queueing/jitter first. Once the
    # connection is genuinely poor, available downstream capacity falls fast.
    "broadband": {
        "delay_factor": [
            (100, 1.0), (90, 1.0), (75, 1.3), (60, 2.0),
            (50, 3.5), (40, 6.0), (25, 15.0), (10, 40.0),
        ],
        "jitter_factor": [
            (100, 1.0), (95, 1.1), (80, 2.0), (60, 4.0),
            (50, 7.0), (40, 12.0), (25, 30.0), (10, 80.0),
        ],
        "loss_add": [
            (100, 0.0), (75, 0.0), (60, 0.4), (50, 1.5),
            (40, 4.0), (25, 15.0), (10, 40.0),
        ],
        "download_factor": [
            (100, 1.0), (90, 1.0), (75, 0.90), (60, 0.72),
            (50, 0.50), (40, 0.28), (25, 0.10), (10, 0.03),
        ],
        "upload_factor": [
            (100, 1.0), (90, 1.0), (75, 0.95), (60, 0.85),
            (50, 0.70), (40, 0.45), (25, 0.20), (10, 0.06),
        ],
    },

    # Cellular performance is volatile. Capacity and jitter are affected early
    # by RF/congestion; packet loss becomes material as quality gets poor.
    "mobile": {
        "delay_factor": [
            (100, 1.0), (95, 1.05), (85, 1.2), (70, 1.7),
            (55, 2.8), (40, 5.0), (25, 12.0), (10, 30.0),
        ],
        "jitter_factor": [
            (100, 1.0), (95, 1.2), (85, 1.8), (70, 3.2),
            (55, 6.0), (40, 12.0), (25, 28.0), (10, 70.0),
        ],
        "loss_add": [
            (100, 0.0), (85, 0.0), (70, 0.2), (55, 0.8),
            (40, 4.0), (25, 15.0), (10, 40.0),
        ],
        "download_factor": [
            (100, 1.0), (95, 0.95), (85, 0.80), (70, 0.60),
            (55, 0.38), (40, 0.20), (25, 0.08), (10, 0.02),
        ],
        "upload_factor": [
            (100, 1.0), (95, 0.92), (85, 0.72), (70, 0.50),
            (55, 0.30), (40, 0.15), (25, 0.06), (10, 0.02),
        ],
    },

    # Satellite starts with high baseline latency. Degradation is represented
    # more by jitter/loss/capacity collapse than by multiplying latency wildly.
    "satellite": {
        "delay_factor": [
            (100, 1.0), (90, 1.02), (75, 1.05), (60, 1.12),
            (50, 1.20), (40, 1.35), (25, 1.70), (10, 2.40),
        ],
        "jitter_factor": [
            (100, 1.0), (95, 1.2), (80, 1.8), (65, 3.0),
            (50, 5.5), (40, 9.0), (25, 22.0), (10, 55.0),
        ],
        "loss_add": [
            (100, 0.0), (80, 0.0), (65, 0.3), (50, 1.5),
            (40, 5.0), (25, 20.0), (10, 50.0),
        ],
        "download_factor": [
            (100, 1.0), (90, 0.98), (75, 0.90), (60, 0.75),
            (50, 0.55), (40, 0.35), (25, 0.12), (10, 0.03),
        ],
        "upload_factor": [
            (100, 1.0), (90, 0.95), (75, 0.82), (60, 0.62),
            (50, 0.42), (40, 0.25), (25, 0.08), (10, 0.02),
        ],
    },
}


def calculate_profile(
    preset: dict,
    quality: int,
    download_mbit=None,
    upload_mbit=None,
):
    """
    Convert a technology preset + relative quality into effective shaping values.

    The degradation curves are access-type specific and staged: different
    metrics begin deteriorating at different quality levels. This intentionally
    avoids reducing every metric together in a linear fashion.
    """
    q = max(0, min(100, int(quality)))
    if q == 0:
        return {
            "delay_ms": max(float(preset.get("delay_ms", 0.0)), 1000.0),
            "jitter_ms": max(float(preset.get("jitter_ms", 0.0)), 200.0),
            "loss_pct": 100.0,
            "download_mbit": 1,
            "upload_mbit": 1,
            "loss_correlation_pct": 0.0,
            "duplicate_pct": 0.0,
            "corrupt_pct": 0.0,
            "reorder_pct": 0.0,
        }

    model = preset.get("quality_model", "broadband")
    curves = QUALITY_CURVES.get(model, QUALITY_CURVES["broadband"])

    base_download = (
        int(round(float(download_mbit)))
        if download_mbit is not None
        else int(round(float(preset.get("download_mbit", 0.0))))
    )
    base_upload = (
        int(round(float(upload_mbit)))
        if upload_mbit is not None
        else int(round(float(preset.get("upload_mbit", 0.0))))
    )

    delay_factor = _curve_value(q, curves["delay_factor"])
    jitter_factor = _curve_value(q, curves["jitter_factor"])
    loss_add = _curve_value(q, curves["loss_add"])
    download_factor = _curve_value(q, curves["download_factor"])
    upload_factor = _curve_value(q, curves["upload_factor"])

    delay = float(preset.get("delay_ms", 0.0)) * delay_factor
    jitter = float(preset.get("jitter_ms", 0.0)) * jitter_factor
    loss = min(100.0, float(preset.get("loss_pct", 0.0)) + loss_add)

    # tc/tbf compatibility: bandwidth is always an integer Mbit/s.
    download = int(round(max(1.0, base_download * download_factor)))
    upload = int(round(max(1.0, base_upload * upload_factor)))

    return {
        "delay_ms": round(delay, 1),
        "jitter_ms": round(jitter, 1),
        "loss_pct": round(loss, 3),
        "download_mbit": download,
        "upload_mbit": upload,
        "loss_correlation_pct": 0.0,
        "duplicate_pct": 0.0,
        "corrupt_pct": 0.0,
        "reorder_pct": 0.0,
    }


def get_effective_profile(link: dict, presets: dict):
    """Return the currently configured effective impairment values without applying them."""
    preset_id = link.get("preset", "broadband")
    preset = presets.get(preset_id) or presets.get("broadband")
    if not preset:
        return {}

    mode = link.get("mode", "quality")
    quality = max(0, min(100, int(link.get("quality", 100))))
    bandwidth_download = link.get("bandwidth_download_mbit")
    bandwidth_upload = link.get("bandwidth_upload_mbit")

    if mode == "custom" and link.get("custom_profile"):
        custom = link["custom_profile"]
        return {
            "delay_ms": max(0.0, float(custom.get("delay_ms", 0.0))),
            "jitter_ms": max(0.0, float(custom.get("jitter_ms", 0.0))),
            "loss_pct": min(100.0, max(0.0, float(custom.get("loss_pct", 0.0)))),
            "download_mbit": int(round(max(0.0, float(custom.get("download_mbit", 0.0))))),
            "upload_mbit": int(round(max(0.0, float(custom.get("upload_mbit", 0.0))))),
            "loss_correlation_pct": min(100.0, max(0.0, float(custom.get("loss_correlation_pct", 0.0)))),
            "duplicate_pct": min(100.0, max(0.0, float(custom.get("duplicate_pct", 0.0)))),
            "corrupt_pct": min(100.0, max(0.0, float(custom.get("corrupt_pct", 0.0)))),
            "reorder_pct": min(100.0, max(0.0, float(custom.get("reorder_pct", 0.0)))),
        }

    return calculate_profile(
        preset,
        quality,
        bandwidth_download,
        bandwidth_upload,
    )


def apply_selected_profile(link: dict, presets: dict):
    """Apply either the quality-derived or custom profile stored on one WAN."""
    inner = link.get("inner")
    outer = link.get("outer")
    if not inner:
        return False, "Missing interface or preset", {}

    effective = get_effective_profile(link, presets)
    if not effective:
        return False, "Missing interface or preset", {}

    ok_down, msg_down = apply_netem(
        inner,
        effective["delay_ms"],
        effective["jitter_ms"],
        effective["loss_pct"],
        effective["download_mbit"],
        effective.get("loss_correlation_pct", 0.0),
        effective.get("duplicate_pct", 0.0),
        effective.get("corrupt_pct", 0.0),
        effective.get("reorder_pct", 0.0),
    )

    ok_up, msg_up = True, "OK"
    if outer:
        ok_up, msg_up = apply_netem(
            outer, 0.0, 0.0, 0.0, effective["upload_mbit"]
        )

    if ok_down and ok_up:
        link_id = link.get("id") or link.get("bridge") or inner
        RUNTIME_EFFECTIVE[link_id] = {
            "effective": dict(effective),
            "quality": int(link.get("quality", 100)),
            "mode": link.get("mode", "quality"),
            "updated_at": time.time(),
        }
        return True, "OK", effective

    details = []
    if not ok_down:
        details.append(f"download/impairment: {msg_down}")
    if not ok_up:
        details.append(f"upload: {msg_up}")
    return False, "; ".join(details), effective

# ---------- NIC discovery ----------

def get_all_nics():
    """
    Returns a list of all non-loopback NIC names,
    e.g. ["ens18", "ens19", "ens20", "br-wan1", "br-wan2"].
    """
    rc, out, err = run_cmd(f"{IP} -o link show")
    if rc != 0:
        return []

    nics = []
    for line in out.splitlines():
        # "2: ens18: <BROADCAST,..."
        parts = line.split(": ", 2)
        if len(parts) < 2:
            continue
        name = parts[1].split("@", 1)[0]
        name = name.strip()
        if name == "lo":
            continue
        nics.append(name)
    return sorted(nics)


def guess_mgmt_interface():
    """
    Guess mgmt interface as the first non-loopback NIC with an IPv4 address.
    """
    rc, out, err = run_cmd(f"{IP} -o addr show")
    if rc != 0:
        return None

    candidates = []
    for line in out.splitlines():
        # "2: ens18    inet 10.240.54.8/24 ..."
        parts = line.split()
        if len(parts) >= 4 and parts[2] == "inet":
            ifname = parts[1].split("@", 1)[0]
            if ifname != "lo":
                candidates.append(ifname)

    return candidates[0] if candidates else None


def get_setup_nics(cfg: dict):
    """
    Return NICs that are eligible for use in WAN 1 / WAN 2:
    - not loopback
    - not mgmt
    - not existing bridges (br-*)
    """
    all_nics = get_all_nics()
    mgmt = cfg.get("mgmt_interface")
    usable = []

    for name in all_nics:
        if name.startswith("br-"):
            continue
        if mgmt and name == mgmt:
            continue
        usable.append(name)

    return usable


# ---------- qdisc parsing / netem ----------

def parse_qdisc_output(raw: str):
    """
    Parse `tc qdisc show dev <if>` into a dict:
    {
      "raw": "...",
      "parsed": {
        "kind": "netem" / "fq_codel" / None,
        "delay_ms": float or None,
        "jitter_ms": float or None,
        "loss_pct": float or None,
        "rate_mbit": float or None,
      }
    }
    """
    info = {
        "raw": raw.strip(),
        "parsed": {
            "kind": None,
            "delay_ms": None,
            "jitter_ms": None,
            "loss_pct": None,
            "rate_mbit": None,
        },
    }

    if not raw.strip():
        return info

    first_line = raw.splitlines()[0]

    # Identify qdisc kind
    m_kind = re.search(r"qdisc\s+(\S+)\s+\d+:", first_line)
    if m_kind:
        kind = m_kind.group(1)
        info["parsed"]["kind"] = kind
    else:
        return info

    if info["parsed"]["kind"] != "netem":
        # we only parse details for netem; others are left with kind only
        return info

    # delay Xms / delay Xms Yms
    m_delay = re.search(r"delay\s+([\d\.]+)ms", first_line)
    if m_delay:
        info["parsed"]["delay_ms"] = float(m_delay.group(1))

    m_delay2 = re.search(r"delay\s+([\d\.]+)ms\s+([\d\.]+)ms", first_line)
    if m_delay2:
        info["parsed"]["jitter_ms"] = float(m_delay2.group(2))

    # loss
    m_loss = re.search(r"loss\s+([\d\.]+)%", first_line)
    if m_loss:
        info["parsed"]["loss_pct"] = float(m_loss.group(1))

    # rate – usually appears in a tbf line
    for line in raw.splitlines():
        m_rate = re.search(r"tbf\s+.*rate\s+([\d\.]+)([KMG])bit", line)
        if m_rate:
            value = float(m_rate.group(1))
            unit = m_rate.group(2).upper()
            if unit == "K":
                value = value / 1000.0
            elif unit == "G":
                value = value * 1000.0
            info["parsed"]["rate_mbit"] = value
            break

    return info


def get_qdisc_state(ifname: str):
    rc, out, err = run_cmd(f"{TC} qdisc show dev {ifname}")
    if rc != 0:
        raw = err or ""
    else:
        raw = out or ""
    return parse_qdisc_output(raw)


def clear_qdisc(ifname: str):
    run_cmd(f"{TC} qdisc del dev {ifname} root")


def apply_netem(
    ifname: str,
    delay_ms: float,
    jitter_ms: float,
    loss_pct: float,
    rate_mbit: float,
    loss_correlation_pct: float = 0.0,
    duplicate_pct: float = 0.0,
    corrupt_pct: float = 0.0,
    reorder_pct: float = 0.0,
):
    """
    Apply netem + optional tbf on interface.

    Advanced impairment controls are intentionally bounded to tc/netem
    primitives and operate only on configured lab interfaces.
    """
    # Always start clean
    clear_qdisc(ifname)

    parts = ["netem"]
    if delay_ms and delay_ms > 0:
        if jitter_ms and jitter_ms > 0:
            parts.append(f"delay {delay_ms:.1f}ms {jitter_ms:.1f}ms")
        else:
            parts.append(f"delay {delay_ms:.1f}ms")

    if loss_pct and loss_pct > 0:
        if loss_correlation_pct and loss_correlation_pct > 0:
            parts.append(
                f"loss {loss_pct:.3f}% {min(100.0, loss_correlation_pct):.3f}%"
            )
        else:
            parts.append(f"loss {loss_pct:.3f}%")

    if duplicate_pct and duplicate_pct > 0:
        parts.append(f"duplicate {min(100.0, duplicate_pct):.3f}%")

    if corrupt_pct and corrupt_pct > 0:
        parts.append(f"corrupt {min(100.0, corrupt_pct):.3f}%")

    if reorder_pct and reorder_pct > 0:
        parts.append(f"reorder {min(100.0, reorder_pct):.3f}%")

    netem_cmd = f"{TC} qdisc add dev {ifname} root handle 1:0 " + " ".join(parts)
    rc, out, err = run_cmd(netem_cmd)
    if rc != 0:
        return False, f"Failed to apply netem: {err or out or 'unknown error'}"

    if rate_mbit and rate_mbit > 0:
        rate_value = int(round(rate_mbit))
        rate_str = f"{rate_value}mbit"
        tbf_cmd = (
            f"{TC} qdisc add dev {ifname} parent 1:1 handle 10: tbf "
            f"rate {rate_str} buffer 3200 limit 32768"
        )
        rc2, out2, err2 = run_cmd(tbf_cmd)
        if rc2 != 0:
            return False, f"Netem OK, but tbf failed: {err2 or out2 or 'unknown error'}"

    return True, "OK"


# ---------- Bridge helpers ----------

def ensure_bridge(br_name: str, inner: str, outer: str):
    """
    Create or update a Linux bridge (using 'ip') and enslave inner/outer.
    """
    # Detach from any previous masters
    for dev in (inner, outer):
        if not dev:
            continue
        run_cmd(f"{IP} link set {dev} down")
        run_cmd(f"{IP} link set {dev} nomaster")

    # Create bridge if missing
    rc, out, err = run_cmd(f"{IP} link show {br_name}")
    if rc != 0:
        run_cmd(f"{IP} link add name {br_name} type bridge")

    # Attach ports
    for dev in (inner, outer):
        if not dev:
            continue
        run_cmd(f"{IP} link set {dev} master {br_name}")
        run_cmd(f"{IP} link set {dev} up")

    # Bring bridge up
    run_cmd(f"{IP} link set {br_name} up")


def delete_bridge(br_name: str):
    """
    Try to delete bridge if it exists.
    """
    rc, out, err = run_cmd(f"{IP} link show {br_name}")
    if rc != 0:
        return  # bridge doesn't exist
    run_cmd(f"{IP} link set {br_name} down")
    run_cmd(f"{IP} link delete {br_name} type bridge")


def restore_runtime_state():
    """
    Restore optional runtime state from config.json.

    Linux bridges and qdiscs are runtime objects and disappear after reboot.
    The GUI controls whether saved bridges and shaping should be recreated
    automatically when the application starts.
    """
    cfg = load_config()
    links = cfg.get("wan_links", [])

    if cfg.get("restore_bridges_on_startup", True):
        for link in links:
            bridge = link.get("bridge")
            inner = link.get("inner")
            outer = link.get("outer")
            if bridge and inner and outer:
                ensure_bridge(bridge, inner, outer)

    if cfg.get("restore_shaping_on_startup", True):
        presets = get_presets(cfg)
        for link in links:
            apply_selected_profile(link, presets)



# ---------- Lab runtime / observability ----------

def load_event_history():
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    if not EVENT_LOG_PATH.exists():
        return
    try:
        lines = EVENT_LOG_PATH.read_text().splitlines()[-500:]
        for line in lines:
            try:
                EVENT_LOG.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        pass
    del EVENT_LOG[:-500]


def log_event(kind: str, message: str, **details):
    event = {
        "timestamp": time.time(),
        "kind": kind,
        "message": message,
        "details": details,
    }
    EVENT_LOG.append(event)
    del EVENT_LOG[:-500]

    try:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        with EVENT_LOG_PATH.open("a") as handle:
            handle.write(json.dumps(event, separators=(",", ":")) + "\n")
    except OSError:
        # History persistence must never break the lab control path.
        pass


load_event_history()


def get_link(cfg: dict, link_id: str):
    return next(
        (
            link for link in cfg.get("wan_links", [])
            if (link.get("id") or link.get("bridge")) == link_id
        ),
        None,
    )


def interface_counters(ifname: str):
    result = {
        "rx_bytes": 0,
        "tx_bytes": 0,
        "rx_packets": 0,
        "tx_packets": 0,
        "rx_dropped": 0,
        "tx_dropped": 0,
        "rx_errors": 0,
        "tx_errors": 0,
    }
    if not ifname:
        return result

    stats_dir = Path("/sys/class/net") / ifname / "statistics"
    for key in result:
        try:
            result[key] = int((stats_dir / key).read_text().strip())
        except (OSError, ValueError):
            result[key] = 0
    return result



def get_interface_mtu(ifname: str):
    if not ifname:
        return None
    try:
        return int((Path("/sys/class/net") / ifname / "mtu").read_text().strip())
    except (OSError, ValueError):
        return None


def apply_mtu_limit(link: dict, mtu: int):
    """
    Apply a transient path-MTU constriction to the bridge and both WAN ports.

    mtu=0 restores the values captured before the first MTU change.
    """
    link_id = link.get("id") or link.get("bridge") or link.get("inner")
    devices = [
        dev for dev in (link.get("inner"), link.get("outer"), link.get("bridge"))
        if dev
    ]
    if not devices:
        return False, "WAN has no interfaces."

    if mtu == 0:
        errors = []
        for dev in devices:
            original = ORIGINAL_MTUS.pop((link_id, dev), None)
            if original is None:
                continue
            rc, out, err = run_cmd(f"{IP} link set dev {dev} mtu {original}")
            if rc != 0:
                errors.append(f"{dev}: {err or out}")
        if errors:
            return False, "; ".join(errors)
        ACTIVE_FAULTS.pop(link_id, None)
        log_event("mtu", f"{link_id}: MTU restored", link_id=link_id)
        return True, "OK"

    mtu = max(576, min(9000, int(mtu)))
    for dev in devices:
        key = (link_id, dev)
        if key not in ORIGINAL_MTUS:
            current = get_interface_mtu(dev)
            if current:
                ORIGINAL_MTUS[key] = current

    errors = []
    # Bridge first, then its member ports.
    ordered = [link.get("bridge"), link.get("inner"), link.get("outer")]
    for dev in [item for item in ordered if item]:
        rc, out, err = run_cmd(f"{IP} link set dev {dev} mtu {mtu}")
        if rc != 0:
            errors.append(f"{dev}: {err or out}")

    if errors:
        return False, "; ".join(errors)

    ACTIVE_FAULTS[link_id] = f"mtu_{mtu}"
    log_event("mtu", f"{link_id}: MTU limited to {mtu}", link_id=link_id, mtu=mtu)
    return True, "OK"


def capture_snapshot():
    with RUNTIME_LOCK:
        state = dict(CAPTURE_STATE)
    path = state.get("path")
    state["download_ready"] = bool(path and Path(path).exists())
    state.pop("process", None)
    return state


def finish_capture(process, duration: int):
    global CAPTURE_PROCESS
    try:
        process.wait(timeout=max(1, duration))
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)

    with RUNTIME_LOCK:
        error = None
        if process.returncode not in (0, -15):
            error = f"tcpdump exited with code {process.returncode}"
        CAPTURE_STATE["active"] = False
        CAPTURE_STATE["error"] = error
        CAPTURE_PROCESS = None
    log_event(
        "capture",
        "Packet capture finished",
        interface=CAPTURE_STATE.get("interface"),
        error=error,
    )


def apply_runtime_fault(link: dict, fault: str, presets: dict):
    """
    Apply a transient fault without changing the persisted WAN profile.

    Blackholes deliberately leave the Linux link/bridge up so an SD-WAN device
    must detect the data-plane failure with its own health checks.
    """
    inner = link.get("inner")
    outer = link.get("outer")
    link_id = link.get("id") or link.get("bridge") or inner
    if not inner:
        return False, "WAN has no inner interface."

    if fault == "normal":
        mtu_ok, mtu_msg = apply_mtu_limit(link, 0)
        ok, msg, _ = apply_selected_profile(link, presets)
        if ok and mtu_ok:
            ACTIVE_FAULTS.pop(link_id, None)
            log_event("fault", f"{link_id} restored to normal", link_id=link_id)
            return True, "OK"
        details = []
        if not mtu_ok:
            details.append(mtu_msg)
        if not ok:
            details.append(msg)
        return False, "; ".join(details)

    # First restore the configured state so one-way faults leave the opposite
    # direction in its normal configured condition.
    ok, msg, _ = apply_selected_profile(link, presets)
    if not ok:
        return False, msg

    if fault in ("blackhole", "downstream_blackhole"):
        ok_down, msg_down = apply_netem(inner, 0.0, 0.0, 100.0, 0.0)
        if not ok_down:
            return False, msg_down

    if fault in ("blackhole", "upstream_blackhole") and outer:
        ok_up, msg_up = apply_netem(outer, 0.0, 0.0, 100.0, 0.0)
        if not ok_up:
            return False, msg_up

    if fault not in ("blackhole", "downstream_blackhole", "upstream_blackhole"):
        return False, "Unknown runtime fault."

    ACTIVE_FAULTS[link_id] = fault
    log_event("fault", f"{link_id}: {fault}", link_id=link_id, fault=fault)
    return True, "OK"


def scenario_snapshot():
    with RUNTIME_LOCK:
        return dict(SCENARIO_STATE)


def run_scenario(link_id: str, scenario: dict):
    cfg = load_config()
    presets = get_presets(cfg)
    link = get_link(cfg, link_id)
    if not link:
        with RUNTIME_LOCK:
            SCENARIO_STATE["active"] = False
        return

    original = copy.deepcopy(link)
    runtime_profile = copy.deepcopy(original)
    try:
        for index, step in enumerate(scenario.get("steps", []), start=1):
            if SCENARIO_STOP.wait(max(0, int(step.get("after", 0)))):
                break

            with RUNTIME_LOCK:
                SCENARIO_STATE["step"] = index
                SCENARIO_STATE["step_label"] = step.get("label") or step.get("action")

            action = step.get("action")
            if action == "quality":
                runtime_profile = copy.deepcopy(original)
                runtime_profile["mode"] = "quality"
                runtime_profile["quality"] = int(step.get("value", 100))
                runtime_profile.pop("custom_profile", None)
                apply_selected_profile(runtime_profile, presets)
                ACTIVE_FAULTS.pop(link_id, None)
                log_event(
                    "scenario",
                    f'{scenario["name"]}: {step.get("label", "quality")}',
                    link_id=link_id,
                    quality=runtime_profile["quality"],
                )
            elif action == "fault":
                apply_runtime_fault(
                    runtime_profile,
                    step.get("value", "normal"),
                    presets,
                )
            elif action == "mtu":
                apply_mtu_limit(runtime_profile, int(step.get("value", 0)))

    finally:
        apply_mtu_limit(original, 0)
        apply_selected_profile(original, presets)
        ACTIVE_FAULTS.pop(link_id, None)
        log_event("scenario", f'{scenario["name"]} finished', link_id=link_id)
        with RUNTIME_LOCK:
            SCENARIO_STATE.update(
                {
                    "active": False,
                    "scenario_id": None,
                    "scenario_name": None,
                    "link_id": None,
                    "started_at": None,
                    "step": 0,
                    "step_label": None,
                }
            )
        SCENARIO_STOP.clear()



def build_link_states(cfg: dict, include_qdisc=False):
    """Build the common vendor-neutral WAN view model used across the UI."""
    presets = get_presets(cfg)
    sla_profile = get_sla_profile(cfg)
    states = []

    for link in cfg.get("wan_links", []):
        inner = link.get("inner")
        if not inner:
            continue

        link_id = link.get("id") or link.get("bridge") or inner
        outer = link.get("outer")
        preset_id = link.get("preset", "broadband")
        if preset_id not in presets:
            preset_id = next(iter(presets), "")
        preset = presets.get(preset_id, {})

        quality = max(0, min(100, int(link.get("quality", 100))))
        mode = link.get("mode", "quality")
        bandwidth_download = link.get("bandwidth_download_mbit")
        bandwidth_upload = link.get("bandwidth_upload_mbit")
        nominal_download = (
            int(bandwidth_download)
            if bandwidth_download is not None
            else int(round(float(preset.get("download_mbit", 0.0))))
        )
        nominal_upload = (
            int(bandwidth_upload)
            if bandwidth_upload is not None
            else int(round(float(preset.get("upload_mbit", 0.0))))
        )

        configured_effective = get_effective_profile(link, presets)
        runtime = RUNTIME_EFFECTIVE.get(link_id) or {}
        effective = runtime.get("effective") or configured_effective
        runtime_quality = runtime.get("quality", quality)
        runtime_mode = runtime.get("mode", mode)
        fault = ACTIVE_FAULTS.get(link_id, "normal")
        sla = evaluate_sla(effective, sla_profile, fault)

        state = {
            "id": link_id,
            "label": link.get("name", "WAN"),
            "name": inner,
            "inner": inner,
            "outer": outer,
            "bridge": link.get("bridge"),
            "preset_id": preset_id,
            "preset_name": preset.get("name", preset_id),
            "quality_model": preset.get("quality_model", "broadband"),
            "quality": quality,
            "runtime_quality": runtime_quality,
            "mode": mode,
            "runtime_mode": runtime_mode,
            "quality_status": (
                "Custom"
                if runtime_mode == "custom"
                else quality_status(runtime_quality)
            ),
            "bandwidth_download_mbit": bandwidth_download,
            "bandwidth_upload_mbit": bandwidth_upload,
            "nominal_download_mbit": nominal_download,
            "nominal_upload_mbit": nominal_upload,
            "configured_effective": configured_effective,
            "effective": effective,
            "fault": fault,
            "sla": sla,
            "mtu": {
                "inner": get_interface_mtu(inner),
                "outer": get_interface_mtu(outer),
                "bridge": get_interface_mtu(link.get("bridge")),
            },
        }

        if include_qdisc:
            state["qdisc"] = get_qdisc_state(inner)
            state["outer_qdisc"] = (
                get_qdisc_state(outer)
                if outer
                else {
                    "raw": "",
                    "parsed": {
                        "kind": None,
                        "delay_ms": None,
                        "jitter_ms": None,
                        "loss_pct": None,
                        "rate_mbit": None,
                    },
                }
            )

        states.append(state)

    return states


def redirect_after(default_endpoint):
    """Redirect form actions to a known UI endpoint without allowing open redirects."""
    requested = request.form.get("return_to")
    allowed = {
        "overview",
        "wan_links",
        "scenarios",
        "traffic_security",
        "analytics",
        "integrations",
        "settings",
        "setup",
        "presets",
        "updates",
    }
    endpoint = requested if requested in allowed else default_endpoint
    return redirect(url_for(endpoint))


# ---------- Nav context ----------

@app.context_processor
def inject_nav():
    cfg = load_config()
    active_fault_labels = [
        f"{link_id}: {fault.replace('_', ' ')}"
        for link_id, fault in ACTIVE_FAULTS.items()
        if fault != "normal"
    ]
    return {
        "nav_groups": [
            {
                "label": "Operate",
                "items": [
                    {"id": "overview", "label": "Overview", "endpoint": "overview", "icon": "overview"},
                    {"id": "wan", "label": "WAN Links", "endpoint": "wan_links", "icon": "wan"},
                    {"id": "scenarios", "label": "Scenarios", "endpoint": "scenarios", "icon": "scenario"},
                    {"id": "traffic", "label": "Traffic & Security", "endpoint": "traffic_security", "icon": "shield"},
                ],
            },
            {
                "label": "Observe",
                "items": [
                    {"id": "analytics", "label": "Analytics", "endpoint": "analytics", "icon": "analytics"},
                    {"id": "integrations", "label": "Integrations", "endpoint": "integrations", "icon": "plug"},
                ],
            },
            {
                "label": "Configure",
                "items": [
                    {"id": "settings", "label": "Settings", "endpoint": "settings", "icon": "settings"},
                ],
            },
        ],
        "config": cfg,
        "app_version": get_app_version(),
        "global_runtime": {
            "scenario": scenario_snapshot(),
            "active_fault_count": len(active_fault_labels),
            "active_fault_labels": active_fault_labels,
            "capture": capture_snapshot(),
        },
    }


# ---------- Routes ----------

@app.route("/")
def overview():
    cfg = load_config()
    if not cfg.get("wan_links"):
        return redirect(url_for("setup"))

    mgmt = cfg.get("mgmt_interface") or guess_mgmt_interface()
    if mgmt and not cfg.get("mgmt_interface"):
        cfg["mgmt_interface"] = mgmt
        save_config(cfg)

    links = build_link_states(cfg)
    healthy = sum(1 for link in links if link["sla"]["pass"])
    return render_template(
        "overview.html",
        page="overview",
        mgmt_interface=mgmt,
        links=links,
        healthy_links=healthy,
        events=list(reversed(EVENT_LOG[-12:])),
        scenario_state=scenario_snapshot(),
        capture_state=capture_snapshot(),
    )


# Backward-compatible endpoint name for older links/bookmarks.
app.add_url_rule("/dashboard", endpoint="index", view_func=overview)


@app.route("/wan")
def wan_links():
    cfg = load_config()
    if not cfg.get("wan_links"):
        return redirect(url_for("setup"))
    return render_template(
        "wan_links.html",
        page="wan",
        links=build_link_states(cfg, include_qdisc=True),
        presets=get_presets(cfg),
        bandwidth_options=BANDWIDTH_OPTIONS,
        quality_curves=QUALITY_CURVES,
    )


@app.route("/lab")
def lab_tools():
    return redirect(url_for("scenarios"))


@app.route("/scenarios")
def scenarios():
    cfg = load_config()
    return render_template(
        "scenarios.html",
        page="scenarios",
        links=build_link_states(cfg),
        scenarios=get_scenarios(cfg),
        custom_scenarios=cfg.get("custom_scenarios", []),
        scenario_state=scenario_snapshot(),
        events=list(reversed([
            event for event in EVENT_LOG
            if event.get("kind") in ("scenario", "scenario-config", "fault", "mtu")
        ][-30:])),
    )


@app.route("/traffic-security")
def traffic_security():
    cfg = load_config()
    return render_template(
        "traffic_security.html",
        page="traffic",
        links=build_link_states(cfg),
        capture_state=capture_snapshot(),
        tcpdump_available=bool(shutil.which("tcpdump")),
        events=list(reversed([
            event for event in EVENT_LOG
            if event.get("kind") in ("capture", "security-test")
        ][-25:])),
    )


@app.route("/analytics")
def analytics():
    cfg = load_config()
    return render_template(
        "analytics.html",
        page="analytics",
        links=build_link_states(cfg),
        sla_profile=get_sla_profile(cfg),
        events=list(reversed(EVENT_LOG[-80:])),
    )


@app.route("/integrations")
def integrations():
    return render_template(
        "integrations.html",
        page="integrations",
    )


@app.route("/settings")
def settings():
    cfg = load_config()
    return render_template(
        "settings.html",
        page="settings",
        mgmt_interface=cfg.get("mgmt_interface") or guess_mgmt_interface(),
        link_count=len(cfg.get("wan_links", [])),
        preset_count=len(get_presets(cfg)),
        update_status=git_update_status(fetch=False),
    )


@app.route("/lab/fault", methods=["POST"])
def lab_fault():
    link_id = request.form.get("link_id") or ""
    fault = request.form.get("fault") or "normal"
    cfg = load_config()
    presets = get_presets(cfg)
    link = get_link(cfg, link_id)

    if not link:
        flash("Unknown WAN link.", "error")
    elif scenario_snapshot().get("active"):
        flash("Stop the active scenario before applying a manual fault.", "error")
    else:
        ok, msg = apply_runtime_fault(link, fault, presets)
        if ok:
            flash(f"Runtime fault for {link_id}: {fault}.", "success")
        else:
            flash("Failed to apply runtime fault: " + msg, "error")
    return redirect_after("scenarios")


@app.route("/lab/scenario/start", methods=["POST"])
def lab_scenario_start():
    link_id = request.form.get("link_id") or ""
    scenario_id = request.form.get("scenario_id") or ""
    cfg = load_config()
    scenario = next(
        (item for item in get_scenarios(cfg) if item["id"] == scenario_id),
        None,
    )

    if not get_link(cfg, link_id):
        flash("Unknown WAN link.", "error")
        return redirect_after("scenarios")
    if not scenario:
        flash("Unknown scenario.", "error")
        return redirect_after("scenarios")

    with RUNTIME_LOCK:
        if SCENARIO_STATE["active"]:
            flash("A scenario is already running.", "error")
            return redirect_after("scenarios")
        SCENARIO_STOP.clear()
        SCENARIO_STATE.update(
            {
                "active": True,
                "scenario_id": scenario_id,
                "scenario_name": scenario["name"],
                "link_id": link_id,
                "started_at": time.time(),
                "step": 0,
                "step_label": "Starting",
            }
        )

    threading.Thread(
        target=run_scenario,
        args=(link_id, scenario),
        daemon=True,
    ).start()
    flash(f'Started scenario "{scenario["name"]}" on {link_id}.', "success")
    return redirect_after("scenarios")


@app.route("/lab/scenario/stop", methods=["POST"])
def lab_scenario_stop():
    if scenario_snapshot().get("active"):
        SCENARIO_STOP.set()
        flash("Scenario stop requested. The configured WAN profile will be restored.", "info")
    return redirect_after("scenarios")


@app.route("/lab/scenario/save", methods=["POST"])
def lab_scenario_save():
    cfg = load_config()
    name = (request.form.get("name") or "").strip()
    description = (request.form.get("description") or "").strip()
    raw_steps = (request.form.get("steps_json") or "").strip()

    if not name:
        flash("Scenario name is required.", "error")
        return redirect_after("scenarios")

    try:
        parsed = json.loads(raw_steps)
        steps = validate_scenario_steps(parsed)
    except (json.JSONDecodeError, ValueError) as exc:
        flash(f"Scenario definition is invalid: {exc}", "error")
        return redirect_after("scenarios")

    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "scenario"
    scenario_id = f"custom_{slug}"
    custom = [
        item for item in cfg.get("custom_scenarios", [])
        if item.get("id") != scenario_id
    ]
    custom.append(
        {
            "id": scenario_id,
            "name": name[:80],
            "description": description[:240],
            "steps": steps,
        }
    )
    cfg["custom_scenarios"] = custom[-20:]
    save_config(cfg)
    log_event("scenario-config", f'Saved custom scenario "{name[:80]}"')
    flash(f'Scenario "{name[:80]}" saved.', "success")
    return redirect_after("scenarios")


@app.route("/lab/scenario/delete", methods=["POST"])
def lab_scenario_delete():
    cfg = load_config()
    scenario_id = request.form.get("scenario_id") or ""
    before = len(cfg.get("custom_scenarios", []))
    cfg["custom_scenarios"] = [
        item for item in cfg.get("custom_scenarios", [])
        if item.get("id") != scenario_id
    ]
    if len(cfg["custom_scenarios"]) != before:
        save_config(cfg)
        log_event("scenario-config", f"Deleted custom scenario {scenario_id}")
        flash("Custom scenario deleted.", "info")
    return redirect_after("scenarios")


@app.route("/lab/mtu", methods=["POST"])
def lab_mtu():
    cfg = load_config()
    link_id = request.form.get("link_id") or ""
    link = get_link(cfg, link_id)
    try:
        mtu = int(request.form.get("mtu", "0"))
    except ValueError:
        mtu = 0

    if not link:
        flash("Unknown WAN link.", "error")
    elif scenario_snapshot().get("active"):
        flash("Stop the active scenario before changing MTU.", "error")
    else:
        ok, msg = apply_mtu_limit(link, mtu)
        if ok:
            flash(
                "Path MTU restored." if mtu == 0 else f"Path MTU limited to {mtu} bytes.",
                "success",
            )
        else:
            flash("Failed to change path MTU: " + msg, "error")
    return redirect_after("scenarios")


@app.route("/lab/sla", methods=["POST"])
def lab_sla():
    cfg = load_config()
    try:
        profile = {
            "name": (request.form.get("name") or "Generic business SLA").strip()[:80],
            "latency_ms": max(0.0, float(request.form.get("latency_ms", "100"))),
            "jitter_ms": max(0.0, float(request.form.get("jitter_ms", "30"))),
            "loss_pct": min(
                100.0,
                max(0.0, float(request.form.get("loss_pct", "2"))),
            ),
        }
    except ValueError:
        flash("SLA thresholds must be numeric.", "error")
        return redirect_after("scenarios")

    cfg["sla_profile"] = profile
    save_config(cfg)
    log_event("sla-config", f'SLA profile updated: {profile["name"]}')
    flash("Generic SLA thresholds saved.", "success")
    return redirect_after("scenarios")


@app.route("/lab/history.json")
def lab_history_json():
    return jsonify({"events": EVENT_LOG[-500:]})


@app.route("/lab/history.csv")
def lab_history_csv():
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["timestamp", "kind", "message", "details"])
    for event in EVENT_LOG[-500:]:
        writer.writerow(
            [
                event.get("timestamp"),
                event.get("kind"),
                event.get("message"),
                json.dumps(event.get("details", {}), separators=(",", ":")),
            ]
        )
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": 'attachment; filename="netem-events.csv"'},
    )


@app.route("/lab/history/clear", methods=["POST"])
def lab_history_clear():
    EVENT_LOG.clear()
    try:
        if EVENT_LOG_PATH.exists():
            EVENT_LOG_PATH.unlink()
    except OSError:
        pass
    flash("Runtime event history cleared.", "info")
    return redirect_after("scenarios")


@app.route("/lab/capture/start", methods=["POST"])
def lab_capture_start():
    global CAPTURE_PROCESS

    tcpdump = shutil.which("tcpdump")
    if not tcpdump:
        flash("tcpdump is not installed on the NetEm VM.", "error")
        return redirect_after("scenarios")

    cfg = load_config()
    link_id = request.form.get("link_id") or ""
    side = request.form.get("side") or "inner"
    link = get_link(cfg, link_id)
    if not link or side not in ("inner", "outer"):
        flash("Invalid capture interface.", "error")
        return redirect_after("scenarios")

    ifname = link.get(side)
    if not ifname:
        flash("Selected WAN side has no interface.", "error")
        return redirect_after("scenarios")

    try:
        duration = max(5, min(120, int(request.form.get("duration", "30"))))
    except ValueError:
        duration = 30

    with RUNTIME_LOCK:
        if CAPTURE_STATE["active"]:
            flash("A packet capture is already running.", "error")
            return redirect_after("scenarios")

        CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
        filename = f"{int(time.time())}-{link_id}-{side}.pcap"
        path = CAPTURE_DIR / filename
        try:
            CAPTURE_PROCESS = subprocess.Popen(
                [
                    tcpdump,
                    "-i", ifname,
                    "-nn",
                    "-s", "256",
                    "-c", "20000",
                    "-w", str(path),
                ],
                cwd=BASE_DIR,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            flash(f"Unable to start tcpdump: {exc}", "error")
            return redirect_after("scenarios")

        CAPTURE_STATE.update(
            {
                "active": True,
                "link_id": link_id,
                "interface": ifname,
                "started_at": time.time(),
                "duration": duration,
                "path": str(path),
                "error": None,
            }
        )

    threading.Thread(
        target=finish_capture,
        args=(CAPTURE_PROCESS, duration),
        daemon=True,
    ).start()
    log_event("capture", f"Packet capture started on {ifname}", duration=duration)
    flash(f"Packet capture started on {ifname} for up to {duration} seconds.", "success")
    return redirect_after("scenarios")


@app.route("/lab/capture/stop", methods=["POST"])
def lab_capture_stop():
    global CAPTURE_PROCESS
    with RUNTIME_LOCK:
        process = CAPTURE_PROCESS
    if process and process.poll() is None:
        process.terminate()
        flash("Packet capture stop requested.", "info")
    return redirect_after("scenarios")


@app.route("/lab/capture/download")
def lab_capture_download():
    state = capture_snapshot()
    path = state.get("path")
    if not path or not Path(path).exists():
        flash("No packet capture is available.", "error")
        return redirect_after("scenarios")
    return send_file(path, as_attachment=True, download_name=Path(path).name)


@app.route("/api/v1/state")
def api_state():
    cfg = load_config()
    links = []
    for link in build_link_states(cfg):
        links.append(
            {
                "id": link["id"],
                "name": link["label"],
                "bridge": link["bridge"],
                "inner": link["inner"],
                "outer": link["outer"],
                "preset": link["preset_id"],
                "preset_name": link["preset_name"],
                "quality": link["runtime_quality"],
                "configured_quality": link["quality"],
                "mode": link["runtime_mode"],
                "fault": link["fault"],
                "nominal_download_mbit": link["nominal_download_mbit"],
                "nominal_upload_mbit": link["nominal_upload_mbit"],
                "mtu": link["mtu"],
                "effective": link["effective"],
                "sla": link["sla"],
            }
        )
    return jsonify(
        {
            "timestamp": time.time(),
            "version": get_app_version(),
            "links": links,
            "scenario": scenario_snapshot(),
            "capture": capture_snapshot(),
            "sla_profile": get_sla_profile(cfg),
            "events": EVENT_LOG[-20:],
        }
    )


@app.route("/api/v1/telemetry")
def api_telemetry():
    cfg = load_config()
    links = []
    for link in cfg.get("wan_links", []):
        link_id = link.get("id") or link.get("bridge")
        links.append(
            {
                "id": link_id,
                "name": link.get("name", "WAN"),
                "inner": {
                    "interface": link.get("inner"),
                    "counters": interface_counters(link.get("inner")),
                },
                "outer": {
                    "interface": link.get("outer"),
                    "counters": interface_counters(link.get("outer")),
                },
                "fault": ACTIVE_FAULTS.get(link_id, "normal"),
            }
        )
    return jsonify({"timestamp": time.time(), "links": links})


@app.route("/api/v1/events")
def api_events():
    try:
        limit = max(1, min(500, int(request.args.get("limit", "100"))))
    except ValueError:
        limit = 100
    return jsonify({"timestamp": time.time(), "events": EVENT_LOG[-limit:]})


@app.route("/metrics")
def prometheus_metrics():
    cfg = load_config()
    lines = [
        "# HELP netem_link_quality Configured NetEm quality percentage.",
        "# TYPE netem_link_quality gauge",
        "# HELP netem_interface_rx_bytes Interface received bytes.",
        "# TYPE netem_interface_rx_bytes counter",
        "# HELP netem_interface_tx_bytes Interface transmitted bytes.",
        "# TYPE netem_interface_tx_bytes counter",
        "# HELP netem_runtime_fault Runtime fault state (1 when a fault is active).",
        "# TYPE netem_runtime_fault gauge",
    ]
    for link in cfg.get("wan_links", []):
        link_id = link.get("id") or link.get("bridge") or "unknown"
        quality = int(link.get("quality", 100))
        lines.append(f'netem_link_quality{{link="{link_id}"}} {quality}')
        lines.append(
            f'netem_runtime_fault{{link="{link_id}"}} '
            + ("0" if ACTIVE_FAULTS.get(link_id, "normal") == "normal" else "1")
        )
        for direction in ("inner", "outer"):
            ifname = link.get(direction)
            counters = interface_counters(ifname)
            lines.append(
                f'netem_interface_rx_bytes{{link="{link_id}",side="{direction}",interface="{ifname}"}} '
                f'{counters["rx_bytes"]}'
            )
            lines.append(
                f'netem_interface_tx_bytes{{link="{link_id}",side="{direction}",interface="{ifname}"}} '
                f'{counters["tx_bytes"]}'
            )
    return Response("\n".join(lines) + "\n", mimetype="text/plain; version=0.0.4")


@app.route("/lab/security/eicar.txt")
def security_eicar():
    """
    Serve the standard harmless EICAR anti-malware test string.

    This is intentionally a detection test artifact, not malware.
    """
    payload = (
        "X5O!P%@AP[4\\PZX54(P^)7CC)7}$"
        "EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
    )
    log_event("security-test", "EICAR test artifact requested")
    return Response(
        payload + "\n",
        mimetype="text/plain",
        headers={"Content-Disposition": 'attachment; filename="eicar.com.txt"'},
    )


@app.route("/lab/security/beacon", methods=["GET", "POST"])
def security_beacon():
    """
    Benign callback sink for C2-like beacon visibility testing.
    It accepts no commands and returns no executable content.
    """
    log_event(
        "security-test",
        "Benign beacon received",
        method=request.method,
        user_agent=(request.headers.get("User-Agent") or "")[:120],
    )
    return ("", 204)


@app.route("/updates", methods=["GET", "POST"])
def updates():
    restarting = False

    if request.method == "POST":
        action = request.form.get("action") or "check"
        status = git_update_status(fetch=True)

        if not status["ok"]:
            flash(status["error"], "error")
        elif action == "update":
            if status["dirty"]:
                flash(
                    "Update blocked because tracked application files have local changes.",
                    "error",
                )
            elif status["ahead"] > 0 and status["behind"] > 0:
                flash(
                    "Update blocked because the local and remote branches have diverged.",
                    "error",
                )
            elif status["behind"] == 0:
                flash("The application is already up to date.", "info")
            else:
                remote_ref = f'origin/{status["target_branch"]}'
                rc, out, err = run_process(
                    [GIT, "merge", "--ff-only", remote_ref],
                    timeout=90,
                )
                if rc == 0:
                    status = git_update_status(fetch=False)
                    restarting = True
                    flash(
                        "Update installed. The application is restarting.",
                        "success",
                    )
                    threading.Thread(
                        target=restart_after_update,
                        daemon=True,
                    ).start()
                else:
                    flash(err or out or "Git fast-forward update failed.", "error")
    else:
        status = git_update_status(fetch=False)

    return render_template(
        "updates.html",
        page="updates",
        update_status=status,
        restarting=restarting,
    )


@app.route("/setup", methods=["GET", "POST"])
def setup():
    cfg = load_config()

    # Set mgmt if not yet configured
    mgmt = cfg.get("mgmt_interface") or guess_mgmt_interface()
    if mgmt and not cfg.get("mgmt_interface"):
        cfg["mgmt_interface"] = mgmt
        save_config(cfg)

    if request.method == "POST":
        # Les input fra wizard – inkl. alias
        wan1_inner = request.form.get("wan1_inner") or ""
        wan1_outer = request.form.get("wan1_outer") or ""
        wan2_inner = request.form.get("wan2_inner") or ""
        wan2_outer = request.form.get("wan2_outer") or ""

        wan1_name = (request.form.get("wan1_name") or "").strip()
        wan2_name = (request.form.get("wan2_name") or "").strip()

        restore_bridges_on_startup = request.form.get("restore_bridges_on_startup") == "on"
        restore_shaping_on_startup = request.form.get("restore_shaping_on_startup") == "on"

        # Preserve the selected access preset and quality while interface
        # mappings or aliases are edited.
        old_links = cfg.get("wan_links", [])
        old_by_id = {}
        for link in old_links:
            link_id = link.get("id") or (
                "wan1" if link.get("bridge") == "br-wan1" else
                "wan2" if link.get("bridge") == "br-wan2" else ""
            )
            if link_id:
                old_by_id[link_id] = link

            for dev in (link.get("inner"), link.get("outer")):
                if dev:
                    clear_qdisc(dev)
            br = link.get("bridge")
            if br:
                delete_bridge(br)

        wan_links = []

        # WAN 1
        if wan1_inner and wan1_outer:
            previous = old_by_id.get("wan1", {})
            ensure_bridge("br-wan1", wan1_inner, wan1_outer)
            wan_links.append(
                {
                    "id": "wan1",
                    "name": wan1_name or "WAN 1",
                    "bridge": "br-wan1",
                    "inner": wan1_inner,
                    "outer": wan1_outer,
                    "preset": previous.get("preset", "broadband"),
                    "quality": int(previous.get("quality", 100)),
                    "mode": previous.get("mode", "quality"),
                    "custom_profile": previous.get("custom_profile"),
                    "bandwidth_download_mbit": previous.get("bandwidth_download_mbit"),
                    "bandwidth_upload_mbit": previous.get("bandwidth_upload_mbit"),
                }
            )

        # WAN 2
        if wan2_inner and wan2_outer:
            previous = old_by_id.get("wan2", {})
            ensure_bridge("br-wan2", wan2_inner, wan2_outer)
            wan_links.append(
                {
                    "id": "wan2",
                    "name": wan2_name or "WAN 2",
                    "bridge": "br-wan2",
                    "inner": wan2_inner,
                    "outer": wan2_outer,
                    "preset": previous.get("preset", "broadband"),
                    "quality": int(previous.get("quality", 100)),
                    "mode": previous.get("mode", "quality"),
                    "custom_profile": previous.get("custom_profile"),
                    "bandwidth_download_mbit": previous.get("bandwidth_download_mbit"),
                    "bandwidth_upload_mbit": previous.get("bandwidth_upload_mbit"),
                }
            )

        cfg["wan_links"] = wan_links
        cfg["restore_bridges_on_startup"] = restore_bridges_on_startup
        cfg["restore_shaping_on_startup"] = restore_shaping_on_startup
        save_config(cfg)

        if restore_shaping_on_startup:
            presets_cfg = get_presets(cfg)
            for link in wan_links:
                apply_selected_profile(link, presets_cfg)

        if wan_links:
            flash("WAN links saved and bridges created.", "success")
            return redirect_after("wan_links")
        else:
            flash("No WAN links configured – please select at least one inner/outer pair.", "info")
            return redirect(url_for("setup"))

    # GET
    setup_nics = get_setup_nics(cfg)
    links_by_id = {}
    for link in cfg.get("wan_links", []):
        link_id = link.get("id") or (
            "wan1" if link.get("bridge") == "br-wan1" else
            "wan2" if link.get("bridge") == "br-wan2" else ""
        )
        if link_id:
            links_by_id[link_id] = link

    return render_template(
        "setup.html",
        page="setup",
        all_nics=setup_nics,
        config=cfg,
        wan1=links_by_id.get("wan1", {}),
        wan2=links_by_id.get("wan2", {}),
    )


@app.route("/reset-config", methods=["POST"])
def reset_config():
    cfg = load_config()
    # Clear qdiscs and delete bridges
    for link in cfg.get("wan_links", []):
        for dev in (link.get("inner"), link.get("outer")):
            if dev:
                clear_qdisc(dev)
        br = link.get("bridge")
        if br:
            delete_bridge(br)

    if CONFIG_PATH.exists():
        CONFIG_PATH.unlink()

    flash("Configuration reset. Bridges removed and qdiscs cleared.", "info")
    return redirect(url_for("setup"))


@app.route("/configure", methods=["POST"])
def configure():
    """
    Apply a persisted preset in either quality-driven or custom override mode.
    """
    link_id = request.form.get("link_id") or ""
    preset_id = request.form.get("preset_id") or ""
    mode = request.form.get("mode") or "quality"
    if mode not in ("quality", "custom"):
        mode = "quality"

    try:
        quality = int(request.form.get("quality", "100"))
    except ValueError:
        quality = 100
    quality = max(0, min(100, quality))

    cfg = load_config()
    presets = get_presets(cfg)
    preset = presets.get(preset_id)
    if not preset:
        flash("Unknown preset.", "error")
        return redirect(url_for("index"))

    link = next(
        (
            item
            for item in cfg.get("wan_links", [])
            if (item.get("id") or item.get("bridge")) == link_id
        ),
        None,
    )
    if not link:
        flash("Unknown WAN link.", "error")
        return redirect(url_for("index"))

    link["preset"] = preset_id
    link["quality"] = quality

    def selected_bandwidth(field):
        value = (request.form.get(field) or "").strip()
        if not value:
            return None
        try:
            parsed = int(round(float(value)))
        except ValueError:
            return None
        return max(1, parsed)

    bandwidth_download = selected_bandwidth("bandwidth_download_mbit")
    bandwidth_upload = selected_bandwidth("bandwidth_upload_mbit")

    if bandwidth_download is None:
        link.pop("bandwidth_download_mbit", None)
    else:
        link["bandwidth_download_mbit"] = bandwidth_download

    if bandwidth_upload is None:
        link.pop("bandwidth_upload_mbit", None)
    else:
        link["bandwidth_upload_mbit"] = bandwidth_upload

    link["mode"] = mode

    if mode == "custom":
        baseline = calculate_profile(
            preset,
            quality,
            bandwidth_download,
            bandwidth_upload,
        )

        def custom_float(field, default):
            value = request.form.get(field)
            if value is None or value == "":
                return float(default)
            try:
                return max(0.0, float(value))
            except ValueError:
                return float(default)

        link["custom_profile"] = {
            "delay_ms": custom_float("custom_delay_ms", baseline["delay_ms"]),
            "jitter_ms": custom_float("custom_jitter_ms", baseline["jitter_ms"]),
            "loss_pct": min(
                100.0,
                custom_float("custom_loss_pct", baseline["loss_pct"]),
            ),
            "download_mbit": int(round(custom_float(
                "custom_download_mbit", baseline["download_mbit"]
            ))),
            "upload_mbit": int(round(custom_float(
                "custom_upload_mbit", baseline["upload_mbit"]
            ))),
            "loss_correlation_pct": min(
                100.0, custom_float("custom_loss_correlation_pct", 0.0)
            ),
            "duplicate_pct": min(
                100.0, custom_float("custom_duplicate_pct", 0.0)
            ),
            "corrupt_pct": min(
                100.0, custom_float("custom_corrupt_pct", 0.0)
            ),
            "reorder_pct": min(
                100.0, custom_float("custom_reorder_pct", 0.0)
            ),
        }
    else:
        # Returning to the quality slider deliberately discards manual overrides.
        link.pop("custom_profile", None)

    ok, msg, _effective = apply_selected_profile(link, presets)

    if ok:
        save_config(cfg)
        if mode == "custom":
            flash(
                f'{link.get("name", "WAN")} set to {preset.get("name", preset_id)} '
                "with custom impairment values.",
                "success",
            )
        else:
            flash(
                f'{link.get("name", "WAN")} set to {preset.get("name", preset_id)} '
                f'at {quality}% ({quality_status(quality)}).',
                "success",
            )
    else:
        flash("Failed to apply WAN profile: " + msg, "error")

    return redirect(url_for("index"))

@app.route("/presets", methods=["GET", "POST"])
def presets():
    cfg = load_config()
    current = get_presets(cfg)

    if request.method == "POST":
        updated = {}
        for preset_id, existing in current.items():
            def field_float(field, default):
                value = request.form.get(f"{preset_id}_{field}")
                if value is None or value == "":
                    return float(default)
                try:
                    return max(0.0, float(value))
                except ValueError:
                    return float(default)

            updated[preset_id] = {
                "name": (
                    request.form.get(f"{preset_id}_name")
                    or existing.get("name")
                    or preset_id
                ).strip(),
                "quality_model": (
                    request.form.get(f"{preset_id}_quality_model")
                    if request.form.get(f"{preset_id}_quality_model") in QUALITY_MODELS
                    else existing.get("quality_model", "broadband")
                ),
                "delay_ms": field_float("delay_ms", existing.get("delay_ms", 0.0)),
                "jitter_ms": field_float(
                    "jitter_ms", existing.get("jitter_ms", 0.0)
                ),
                "loss_pct": min(
                    100.0,
                    field_float("loss_pct", existing.get("loss_pct", 0.0)),
                ),
                "download_mbit": int(round(field_float(
                    "download_mbit", existing.get("download_mbit", 0.0)
                ))),
                "upload_mbit": int(round(field_float(
                    "upload_mbit", existing.get("upload_mbit", 0.0)
                ))),
            }

        cfg["presets"] = updated
        save_config(cfg)

        # Keep live links consistent with their displayed preset values.
        for link in cfg.get("wan_links", []):
            apply_selected_profile(link, updated)

        flash("Presets saved and active WAN profiles refreshed.", "success")
        return redirect(url_for("presets"))

    return render_template(
        "presets.html",
        page="presets",
        presets=current,
        quality_models=QUALITY_MODELS,
    )


@app.route("/presets/reset", methods=["POST"])
def reset_presets():
    cfg = load_config()
    cfg["presets"] = json.loads(json.dumps(DEFAULT_PRESETS))
    save_config(cfg)

    for link in cfg.get("wan_links", []):
        apply_selected_profile(link, cfg["presets"])

    flash("Preset defaults restored and active WAN profiles refreshed.", "info")
    return redirect(url_for("presets"))


@app.route("/clear", methods=["POST"])
def clear():
    ifname = request.form.get("itf") or request.args.get("itf")
    outer_ifname = request.form.get("outer_itf") or request.args.get("outer_itf")
    if not ifname:
        flash("Missing interface name.", "error")
        return redirect(url_for("index"))

    clear_qdisc(ifname)
    if outer_ifname:
        clear_qdisc(outer_ifname)

    cfg = load_config()
    shaping = cfg.get("shaping_profiles", {})
    if ifname in shaping:
        shaping.pop(ifname, None)
        cfg["shaping_profiles"] = shaping
        save_config(cfg)

    flash("Cleared WAN shaping.", "info")
    return redirect(url_for("index"))


if __name__ == "__main__":
    restore_runtime_state()
    app.run(host="0.0.0.0", port=8081, debug=False)