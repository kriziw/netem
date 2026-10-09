#!/usr/bin/env python3
import copy
import csv
import io
import ipaddress
import json
import math
import os
import re
import shutil
import socket
import sqlite3
import ssl
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit
from urllib import error as urllib_error
from urllib import request as urllib_request

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
    abort,
)

app = Flask(__name__)
app.secret_key = "techkarma-netem"  

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
VERSION_PATH = BASE_DIR / "version.txt"
RUNTIME_DIR = BASE_DIR / "runtime"
EVENT_LOG_PATH = RUNTIME_DIR / "events.jsonl"
SESSIONS_PATH = RUNTIME_DIR / "sessions.json"
TELEMETRY_DB_PATH = RUNTIME_DIR / "telemetry.db"
SECRETS_PATH = RUNTIME_DIR / "secrets.json"
CAPTURE_DIR = RUNTIME_DIR / "captures"

TRAFFIC_GENERATOR_DISCOVERY_PORT = 47890
TRAFFIC_GENERATOR_DISCOVERY_MAGIC = "NETEM_TRAFFIC_SIMULATOR_DISCOVERY_V1"

TC = "/usr/sbin/tc"
IP = "/usr/sbin/ip"
GIT = "/usr/bin/git"
PING = shutil.which("ping") or "/usr/bin/ping"
UPDATE_BRANCH = "main"

TELEMETRY_SAMPLE_SECONDS = 2.0
TELEMETRY_RETENTION_HOURS = 168
MAX_PROBES = 20

RUNTIME_LOCK = threading.Lock()
ACTIVE_FAULTS = {}
RUNTIME_EFFECTIVE = {}
EVENT_LOG = []
LAB_SESSIONS = []
ACTIVE_SESSION = {
    "active": False,
    "id": None,
    "name": None,
    "started_at": None,
}
SCENARIO_STOP = threading.Event()
SCENARIO_STATE = {
    "active": False,
    "scenario_id": None,
    "scenario_name": None,
    "link_id": None,
    "started_at": None,
    "step": 0,
    "step_count": 0,
    "step_label": None,
    "step_action": None,
    "condition": None,
    "result": None,
    "error": None,
}
ORIGINAL_MTUS = {}
CAPTURE_PROCESS = None
BACKGROUND_STOP = threading.Event()
TELEMETRY_THREAD = None
PROBE_THREAD = None
TELEMETRY_PREVIOUS = {}
PROBE_RUNTIME = {}
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


def load_secrets():
    if not SECRETS_PATH.exists():
        return {}
    try:
        raw = json.loads(SECRETS_PATH.read_text())
        return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_secrets(secrets_data):
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SECRETS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(secrets_data, indent=2))
    os.chmod(tmp, 0o600)
    tmp.replace(SECRETS_PATH)
    try:
        os.chmod(SECRETS_PATH, 0o600)
    except OSError:
        pass


def traffic_generator_config(cfg=None):
    cfg = cfg or load_config()
    raw = cfg.get("traffic_generator")
    return raw if isinstance(raw, dict) else {}


def traffic_generator_api_key():
    return str(load_secrets().get("traffic_generator_api_key") or "").strip()


def traffic_generator_base_url(cfg=None):
    integration = traffic_generator_config(cfg)
    host = str(integration.get("host") or "").strip()
    if not host:
        return None
    port = int(integration.get("port") or 8443)
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"https://{host}:{port}"


def traffic_generator_request(path, method="GET", payload=None, timeout=3.0):
    cfg = load_config()
    base_url = traffic_generator_base_url(cfg)
    key = traffic_generator_api_key()
    if not base_url:
        raise RuntimeError("Traffic Simulator is not configured.")
    if not key:
        raise RuntimeError("Traffic Simulator API key is not configured.")

    body = None
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {key}",
        "User-Agent": f"NetEm-WAN-Lab/{get_app_version()}",
    }
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    integration = traffic_generator_config(cfg)
    allow_self_signed = bool(integration.get("allow_self_signed", True))
    context = (
        ssl._create_unverified_context()
        if allow_self_signed
        else ssl.create_default_context()
    )
    req = urllib_request.Request(
        base_url + path,
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urllib_request.urlopen(req, timeout=timeout, context=context) as response:
            raw = response.read(2 * 1024 * 1024)
            return json.loads(raw.decode("utf-8")) if raw else {}
    except urllib_error.HTTPError as exc:
        try:
            detail = exc.read(8192).decode("utf-8", "replace")
        except Exception:
            detail = str(exc)
        raise RuntimeError(f"Traffic Simulator returned HTTP {exc.code}: {detail[:300]}")
    except (urllib_error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Traffic Simulator connection failed: {exc}")


def traffic_generator_snapshot(include_catalog=False):
    cfg = load_config()
    integration = traffic_generator_config(cfg)
    configured = bool(integration.get("host")) and bool(traffic_generator_api_key())
    result = {
        "configured": configured,
        "connected": False,
        "integration": {
            "host": integration.get("host"),
            "port": int(integration.get("port") or 8443),
            "allow_self_signed": bool(integration.get("allow_self_signed", True)),
            "instance_name": integration.get("instance_name"),
            "version": integration.get("version"),
            "tls_sha256": integration.get("tls_sha256"),
        },
        "status": None,
        "catalog": None,
        "error": None,
    }
    if not configured:
        return result
    try:
        result["status"] = traffic_generator_request("/api/v1/status", timeout=2.0)
        result["connected"] = True
        if include_catalog:
            result["catalog"] = traffic_generator_request(
                "/api/v1/catalog", timeout=2.0
            )
    except RuntimeError as exc:
        result["error"] = str(exc)
    return result


def _management_broadcast_addresses(cfg=None):
    cfg = cfg or load_config()
    addresses = {"255.255.255.255"}
    interface = cfg.get("mgmt_interface") or guess_mgmt_interface()
    if not interface:
        return sorted(addresses)
    try:
        proc = subprocess.run(
            [IP, "-j", "-4", "addr", "show", "dev", interface],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        if proc.returncode != 0:
            return sorted(addresses)
        data = json.loads(proc.stdout or "[]")
        for device in data:
            for addr in device.get("addr_info", []):
                if addr.get("family") != "inet":
                    continue
                local = addr.get("local")
                prefix = addr.get("prefixlen")
                if local and prefix is not None:
                    network = ipaddress.ip_network(f"{local}/{prefix}", strict=False)
                    addresses.add(str(network.broadcast_address))
    except (OSError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired):
        pass
    return sorted(addresses)


def discover_traffic_generators(timeout=1.25):
    nonce = str(time.time_ns())
    request_payload = json.dumps(
        {
            "protocol": TRAFFIC_GENERATOR_DISCOVERY_MAGIC,
            "nonce": nonce,
        }
    ).encode("utf-8")
    found = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(0.2)
        sock.bind(("", 0))
        for address in _management_broadcast_addresses():
            try:
                sock.sendto(
                    request_payload,
                    (address, TRAFFIC_GENERATOR_DISCOVERY_PORT),
                )
            except OSError:
                continue

        deadline = time.time() + max(0.25, min(3.0, float(timeout)))
        while time.time() < deadline:
            try:
                data, peer = sock.recvfrom(8192)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                payload = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if payload.get("service") != "netem-traffic-simulator":
                continue
            if payload.get("protocol") != TRAFFIC_GENERATOR_DISCOVERY_MAGIC:
                continue
            if payload.get("nonce") != nonce:
                continue
            payload["detected_address"] = peer[0]
            key = f'{peer[0]}:{payload.get("api_port", 8443)}'
            found[key] = payload
    finally:
        sock.close()
    return list(found.values())


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
            {
                "after": 0,
                "action": "assert",
                "condition": {"type": "sla", "state": "fail"},
                "timeout": 5,
                "label": "Expected SLA detects failure"
            },
            {"after": 30, "action": "fault", "value": "normal", "label": "Connectivity restored"},
            {
                "after": 0,
                "action": "assert",
                "condition": {"type": "sla", "state": "pass"},
                "timeout": 5,
                "label": "Expected SLA recovers"
            },
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


def validate_condition(raw, step_index):
    if not isinstance(raw, dict):
        raise ValueError(f"Step {step_index}: condition must be an object.")

    condition_type = str(raw.get("type") or "").strip().lower()
    if condition_type not in ("sla", "probe", "traffic", "dem"):
        raise ValueError(
            f"Step {step_index}: condition type must be sla, probe, traffic or dem."
        )

    condition = {"type": condition_type}
    link_id = str(raw.get("link_id") or "").strip()
    if link_id:
        condition["link_id"] = link_id

    if condition_type == "sla":
        state = str(raw.get("state") or "").strip().lower()
        if state not in ("pass", "fail"):
            raise ValueError(
                f"Step {step_index}: SLA condition state must be pass or fail."
            )
        condition["state"] = state

    elif condition_type == "probe":
        probe_id = str(raw.get("probe_id") or "").strip()
        if not probe_id:
            raise ValueError(f"Step {step_index}: probe_id is required.")
        field = str(raw.get("field") or "success").strip().lower()
        if field not in ("success", "latency_ms"):
            raise ValueError(
                f"Step {step_index}: probe field must be success or latency_ms."
            )
        op = str(raw.get("op") or "==").strip()
        if op not in ("==", "!=", "<", "<=", ">", ">="):
            raise ValueError(f"Step {step_index}: unsupported comparison operator.")
        value = raw.get("value")
        if field == "success":
            if isinstance(value, str):
                value = value.strip().lower() in ("1", "true", "yes", "pass", "up")
            else:
                value = bool(value)
            if op not in ("==", "!="):
                raise ValueError(
                    f"Step {step_index}: success supports only == or !=."
                )
        else:
            try:
                value = float(value)
            except (TypeError, ValueError):
                raise ValueError(
                    f"Step {step_index}: probe latency comparison needs a number."
                )
        condition.update(
            {
                "probe_id": probe_id,
                "field": field,
                "op": op,
                "value": value,
            }
        )

    elif condition_type == "traffic":
        field = str(raw.get("field") or "down_mbps").strip().lower()
        if field not in ("down_mbps", "up_mbps", "down_pps", "up_pps"):
            raise ValueError(
                f"Step {step_index}: traffic field must be down_mbps, up_mbps, "
                "down_pps or up_pps."
            )
        op = str(raw.get("op") or ">=").strip()
        if op not in ("==", "!=", "<", "<=", ">", ">="):
            raise ValueError(f"Step {step_index}: unsupported comparison operator.")
        try:
            value = float(raw.get("value"))
        except (TypeError, ValueError):
            raise ValueError(
                f"Step {step_index}: traffic comparison needs a numeric value."
            )
        condition.update({"field": field, "op": op, "value": value})

    elif condition_type == "dem":
        field = str(raw.get("field") or "experience_score").strip().lower()
        if field not in (
            "experience_score",
            "availability_pct",
            "p50_ms",
            "p95_ms",
            "requests_per_second",
            "failures_per_second",
            "active_users",
        ):
            raise ValueError(
                f"Step {step_index}: unsupported DEM field."
            )
        op = str(raw.get("op") or ">=").strip()
        if op not in ("==", "!=", "<", "<=", ">", ">="):
            raise ValueError(f"Step {step_index}: unsupported comparison operator.")
        try:
            value = float(raw.get("value"))
        except (TypeError, ValueError):
            raise ValueError(
                f"Step {step_index}: DEM comparison needs a numeric value."
            )
        condition.update(
            {
                "field": field,
                "op": op,
                "value": value,
                "window": max(10, min(3600, int(raw.get("window", 60)))),
            }
        )

    return condition


def validate_scenario_steps(raw_steps):
    """Validate a vendor-neutral timed + conditional scenario definition."""
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ValueError("Scenario must contain at least one step.")
    if len(raw_steps) > 30:
        raise ValueError("A scenario can contain at most 30 steps.")

    validated = []
    for index, step in enumerate(raw_steps, start=1):
        if not isinstance(step, dict):
            raise ValueError(f"Step {index} must be an object.")

        action = str(step.get("action", "")).strip()
        if action not in ("quality", "fault", "mtu", "wait", "assert"):
            raise ValueError(
                f"Step {index}: action must be quality, fault, mtu, wait or assert."
            )

        try:
            after = max(0, min(3600, int(step.get("after", 0))))
        except (TypeError, ValueError):
            raise ValueError(f"Step {index}: after must be an integer.")

        validated_step = {
            "after": after,
            "action": action,
            "label": str(step.get("label") or action)[:80],
        }

        if action == "quality":
            try:
                value = max(0, min(100, int(step.get("value"))))
            except (TypeError, ValueError):
                raise ValueError(f"Step {index}: quality must be 0-100.")
            validated_step["value"] = value

        elif action == "fault":
            value = step.get("value")
            if value not in (
                "normal",
                "blackhole",
                "downstream_blackhole",
                "upstream_blackhole",
            ):
                raise ValueError(f"Step {index}: unsupported fault.")
            validated_step["value"] = value

        elif action == "mtu":
            try:
                value = int(step.get("value"))
            except (TypeError, ValueError):
                raise ValueError(f"Step {index}: MTU must be an integer.")
            if value != 0 and not 576 <= value <= 9000:
                raise ValueError(
                    f"Step {index}: MTU must be 576-9000, or 0 to restore."
                )
            validated_step["value"] = value

        elif action in ("wait", "assert"):
            validated_step["condition"] = validate_condition(
                step.get("condition"), index
            )
            try:
                timeout_s = max(1, min(600, int(step.get("timeout", 30))))
                poll_s = max(0.25, min(5.0, float(step.get("poll", 0.5))))
            except (TypeError, ValueError):
                raise ValueError(
                    f"Step {index}: timeout/poll must be numeric values."
                )
            on_fail = str(step.get("on_fail") or "stop").strip().lower()
            if on_fail not in ("stop", "continue"):
                raise ValueError(
                    f"Step {index}: on_fail must be stop or continue."
                )
            validated_step.update(
                {
                    "timeout": timeout_s,
                    "poll": poll_s,
                    "on_fail": on_fail,
                }
            )

        validated.append(validated_step)
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
    active_session_id = ACTIVE_SESSION.get("id") if ACTIVE_SESSION.get("active") else None
    if active_session_id and "session_id" not in details:
        details["session_id"] = active_session_id

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


def load_session_history():
    if not SESSIONS_PATH.exists():
        return
    changed = False
    try:
        raw = json.loads(SESSIONS_PATH.read_text())
        if isinstance(raw, list):
            for item in raw[-100:]:
                # A process restart cannot safely resume an in-memory lab
                # session. Mark any previously-active record as interrupted.
                if item.get("status") == "active" and not item.get("ended_at"):
                    item["status"] = "interrupted"
                    item["ended_at"] = time.time()
                    changed = True
                LAB_SESSIONS.append(item)
            if changed:
                save_session_history()
    except (OSError, json.JSONDecodeError):
        pass


def save_session_history():
    try:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        tmp = SESSIONS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(LAB_SESSIONS[-100:], indent=2))
        tmp.replace(SESSIONS_PATH)
    except OSError:
        pass


def session_snapshot():
    with RUNTIME_LOCK:
        return dict(ACTIVE_SESSION)


def session_event_count(session_id: str):
    return sum(
        1 for event in EVENT_LOG
        if event.get("details", {}).get("session_id") == session_id
    )


def session_rows():
    rows = []
    for item in reversed(LAB_SESSIONS[-100:]):
        row = dict(item)
        row["event_count"] = session_event_count(row.get("id"))
        rows.append(row)
    return rows


load_session_history()


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


def interface_runtime_status(ifname: str):
    if not ifname:
        return {
            "available": False,
            "operstate": "unknown",
            "carrier": None,
        }

    base = Path("/sys/class/net") / ifname
    available = base.exists()
    operstate = "unknown"
    carrier = None

    if available:
        try:
            operstate = (base / "operstate").read_text().strip() or "unknown"
        except OSError:
            pass
        try:
            carrier = (base / "carrier").read_text().strip() == "1"
        except OSError:
            carrier = None

    return {
        "available": available,
        "operstate": operstate,
        "carrier": carrier,
    }




# ---------- Persistent telemetry / active measurement ----------

def telemetry_connect():
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(TELEMETRY_DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_telemetry_db():
    with telemetry_connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS telemetry_samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                link_id TEXT NOT NULL,
                down_mbps REAL NOT NULL,
                up_mbps REAL NOT NULL,
                down_pps REAL NOT NULL,
                up_pps REAL NOT NULL,
                delay_ms REAL NOT NULL,
                jitter_ms REAL NOT NULL,
                loss_pct REAL NOT NULL,
                quality REAL NOT NULL,
                sla_pass INTEGER NOT NULL,
                fault TEXT NOT NULL,
                session_id TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_telemetry_link_time
                ON telemetry_samples(link_id, timestamp);
            CREATE INDEX IF NOT EXISTS idx_telemetry_session
                ON telemetry_samples(session_id, timestamp);

            CREATE TABLE IF NOT EXISTS probe_samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                probe_id TEXT NOT NULL,
                link_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                target TEXT NOT NULL,
                success INTEGER NOT NULL,
                latency_ms REAL,
                status TEXT,
                detail TEXT,
                session_id TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_probe_probe_time
                ON probe_samples(probe_id, timestamp);
            CREATE INDEX IF NOT EXISTS idx_probe_link_time
                ON probe_samples(link_id, timestamp);
            CREATE INDEX IF NOT EXISTS idx_probe_session
                ON probe_samples(session_id, timestamp);
            """
        )


def active_session_id():
    with RUNTIME_LOCK:
        return ACTIVE_SESSION.get("id") if ACTIVE_SESSION.get("active") else None


def collect_telemetry_sample():
    cfg = load_config()
    states = {
        item["id"]: item
        for item in build_link_states(cfg)
    }
    now = time.time()
    rows = []

    for link in cfg.get("wan_links", []):
        link_id = link.get("id") or link.get("bridge")
        state = states.get(link_id)
        if not state:
            continue

        inner = interface_counters(link.get("inner"))
        outer = interface_counters(link.get("outer"))
        current = {
            "timestamp": now,
            "down_bytes": inner["tx_bytes"],
            "up_bytes": outer["tx_bytes"],
            "down_packets": inner["tx_packets"],
            "up_packets": outer["tx_packets"],
        }
        previous = TELEMETRY_PREVIOUS.get(link_id)
        down_mbps = up_mbps = down_pps = up_pps = 0.0
        if previous:
            dt = max(0.001, now - previous["timestamp"])
            down_mbps = max(0, current["down_bytes"] - previous["down_bytes"]) * 8 / dt / 1_000_000
            up_mbps = max(0, current["up_bytes"] - previous["up_bytes"]) * 8 / dt / 1_000_000
            down_pps = max(0, current["down_packets"] - previous["down_packets"]) / dt
            up_pps = max(0, current["up_packets"] - previous["up_packets"]) / dt

        TELEMETRY_PREVIOUS[link_id] = current
        effective = state.get("effective", {})
        rows.append(
            (
                now,
                link_id,
                down_mbps,
                up_mbps,
                down_pps,
                up_pps,
                float(effective.get("delay_ms", 0.0)),
                float(effective.get("jitter_ms", 0.0)),
                float(effective.get("loss_pct", 0.0)),
                float(state.get("runtime_quality", 100)),
                1 if state.get("sla", {}).get("pass") else 0,
                state.get("fault", "normal"),
                active_session_id(),
            )
        )

    if rows:
        with telemetry_connect() as conn:
            conn.executemany(
                """
                INSERT INTO telemetry_samples (
                    timestamp, link_id, down_mbps, up_mbps, down_pps, up_pps,
                    delay_ms, jitter_ms, loss_pct, quality, sla_pass, fault, session_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
    return rows


def prune_telemetry_history():
    cutoff = time.time() - TELEMETRY_RETENTION_HOURS * 3600
    with telemetry_connect() as conn:
        conn.execute("DELETE FROM telemetry_samples WHERE timestamp < ?", (cutoff,))
        conn.execute("DELETE FROM probe_samples WHERE timestamp < ?", (cutoff,))


def telemetry_worker():
    init_telemetry_db()
    next_prune = time.time() + 300
    while not BACKGROUND_STOP.is_set():
        started = time.time()
        try:
            collect_telemetry_sample()
            if started >= next_prune:
                prune_telemetry_history()
                next_prune = started + 300
        except Exception as exc:
            # Telemetry persistence must never stop the control plane.
            log_event("telemetry", "Persistent telemetry sample failed", error=str(exc)[:240])
        elapsed = time.time() - started
        BACKGROUND_STOP.wait(max(0.2, TELEMETRY_SAMPLE_SECONDS - elapsed))


def query_telemetry_history(link_id: str, since: float, max_points=1200):
    init_telemetry_db()
    now = time.time()
    span = max(1.0, now - since)
    bucket_seconds = max(TELEMETRY_SAMPLE_SECONDS, span / max(50, min(5000, max_points)))
    with telemetry_connect() as conn:
        rows = conn.execute(
            """
            SELECT
                AVG(timestamp) AS timestamp,
                AVG(down_mbps) AS down_mbps,
                AVG(up_mbps) AS up_mbps,
                AVG(down_pps) AS down_pps,
                AVG(up_pps) AS up_pps,
                AVG(delay_ms) AS delay_ms,
                AVG(jitter_ms) AS jitter_ms,
                AVG(loss_pct) AS loss_pct,
                AVG(quality) AS quality,
                MIN(sla_pass) AS sla_pass
            FROM telemetry_samples
            WHERE link_id = ? AND timestamp >= ?
            GROUP BY CAST((timestamp - ?) / ? AS INTEGER)
            ORDER BY timestamp ASC
            """,
            (link_id, since, since, bucket_seconds),
        ).fetchall()
    return [dict(row) for row in rows]


def latest_telemetry_sample(link_id: str):
    init_telemetry_db()
    with telemetry_connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM telemetry_samples
            WHERE link_id = ?
            ORDER BY timestamp DESC LIMIT 1
            """,
            (link_id,),
        ).fetchone()
    return dict(row) if row else None


def query_probe_history(probe_id=None, link_id=None, since=None, limit=1000):
    init_telemetry_db()
    clauses = []
    values = []
    if probe_id:
        clauses.append("probe_id = ?")
        values.append(probe_id)
    if link_id:
        clauses.append("link_id = ?")
        values.append(link_id)
    if since is not None:
        clauses.append("timestamp >= ?")
        values.append(float(since))
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    values.append(max(1, min(5000, int(limit))))
    with telemetry_connect() as conn:
        rows = conn.execute(
            f"""
            SELECT timestamp, probe_id, link_id, kind, target, success,
                   latency_ms, status, detail, session_id
            FROM probe_samples
            {where}
            ORDER BY timestamp DESC LIMIT ?
            """,
            values,
        ).fetchall()
    return [dict(row) for row in reversed(rows)]


def latest_probe_sample(probe_id: str):
    with RUNTIME_LOCK:
        runtime = PROBE_RUNTIME.get(probe_id)
        if runtime:
            return dict(runtime)
    rows = query_probe_history(probe_id=probe_id, limit=1)
    return rows[-1] if rows else None


def get_probes(cfg: dict):
    probes = cfg.get("probes", [])
    return probes if isinstance(probes, list) else []


def resolve_probe_interface(probe: dict, cfg: dict):
    side = probe.get("source_side", "auto")
    if side == "auto":
        return None
    link = get_link(cfg, probe.get("link_id", ""))
    if not link:
        return None
    if side == "inner":
        return link.get("inner")
    if side == "outer":
        return link.get("outer")
    return None


def validate_probe_definition(raw: dict, cfg: dict, existing_id=None):
    link_id = str(raw.get("link_id") or "").strip()
    if not get_link(cfg, link_id):
        raise ValueError("Probe must reference a configured WAN.")

    kind = str(raw.get("kind") or "icmp").strip().lower()
    if kind not in ("icmp", "tcp", "http", "dns"):
        raise ValueError("Probe type must be ICMP, TCP, HTTP or DNS.")

    target = str(raw.get("target") or "").strip()
    if not target or len(target) > 512:
        raise ValueError("Probe target is required and must be at most 512 characters.")

    source_side = str(raw.get("source_side") or "auto").strip().lower()
    if source_side not in ("auto", "inner", "outer"):
        raise ValueError("Probe source must be automatic, inner or outer.")

    try:
        interval_s = max(2, min(3600, int(raw.get("interval_s", 5))))
    except (TypeError, ValueError):
        raise ValueError("Probe interval must be an integer from 2 to 3600 seconds.")

    try:
        timeout_s = max(0.2, min(10.0, float(raw.get("timeout_s", 2.0))))
    except (TypeError, ValueError):
        raise ValueError("Probe timeout must be between 0.2 and 10 seconds.")

    port = None
    if kind == "tcp":
        try:
            port = int(raw.get("port", 443))
        except (TypeError, ValueError):
            raise ValueError("TCP probe port must be an integer.")
        if not 1 <= port <= 65535:
            raise ValueError("TCP probe port must be 1-65535.")

    if kind == "http":
        parsed = urlsplit(target)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("HTTP probe target must be an http:// or https:// URL.")

    resolver = str(raw.get("resolver") or "1.1.1.1").strip()
    if kind == "dns" and (not resolver or len(resolver) > 255):
        raise ValueError("DNS resolver is required.")

    name = str(raw.get("name") or f"{link_id} {kind.upper()}").strip()[:80]
    probe_id = existing_id or str(raw.get("id") or "").strip()
    if not probe_id:
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or kind
        probe_id = f"probe-{link_id}-{slug}-{time.time_ns() % 1000000}"

    return {
        "id": probe_id[:120],
        "name": name or f"{link_id} {kind.upper()}",
        "link_id": link_id,
        "kind": kind,
        "target": target,
        "port": port,
        "resolver": resolver if kind == "dns" else None,
        "source_side": source_side,
        "interval_s": interval_s,
        "timeout_s": timeout_s,
        "enabled": bool(raw.get("enabled", True)),
    }


def bind_socket_to_interface(sock: socket.socket, ifname: str | None):
    if not ifname:
        return
    option = getattr(socket, "SO_BINDTODEVICE", 25)
    sock.setsockopt(socket.SOL_SOCKET, option, ifname.encode() + b"\0")


def open_bound_tcp(host: str, port: int, timeout_s: float, ifname=None):
    last_error = None
    for family, socktype, proto, _canonname, sockaddr in socket.getaddrinfo(
        host, port, type=socket.SOCK_STREAM
    ):
        sock = socket.socket(family, socktype, proto)
        try:
            sock.settimeout(timeout_s)
            bind_socket_to_interface(sock, ifname)
            sock.connect(sockaddr)
            return sock
        except OSError as exc:
            last_error = exc
            sock.close()
    raise OSError(str(last_error or "Unable to connect"))


def dns_query_packet(hostname: str, transaction_id: int):
    labels = hostname.rstrip(".").split(".")
    if not labels or any(not label or len(label.encode()) > 63 for label in labels):
        raise ValueError("Invalid DNS hostname.")
    qname = b"".join(bytes([len(label.encode())]) + label.encode() for label in labels) + b"\x00"
    header = transaction_id.to_bytes(2, "big") + b"\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00"
    return header + qname + b"\x00\x01\x00\x01"


def execute_probe(probe: dict, cfg: dict):
    kind = probe["kind"]
    timeout_s = float(probe.get("timeout_s", 2.0))
    ifname = resolve_probe_interface(probe, cfg)
    target = probe["target"]
    started = time.perf_counter()
    status = None
    detail = ""

    try:
        if kind == "icmp":
            cmd = [PING, "-n", "-c", "1", "-W", str(max(1, math.ceil(timeout_s)))]
            if ifname:
                cmd += ["-I", ifname]
            cmd.append(target)
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout_s + 1.5,
            )
            if proc.returncode != 0:
                raise OSError((proc.stderr or proc.stdout or "ICMP probe failed").strip())
            match = re.search(r"time[=<]([0-9.]+)\s*ms", proc.stdout or "")
            latency_ms = float(match.group(1)) if match else (time.perf_counter() - started) * 1000
            status = "reply"
            detail = "ICMP echo reply"

        elif kind == "tcp":
            sock = open_bound_tcp(
                target,
                int(probe.get("port") or 443),
                timeout_s,
                ifname,
            )
            latency_ms = (time.perf_counter() - started) * 1000
            sock.close()
            status = "connected"
            detail = f'TCP/{int(probe.get("port") or 443)} connected'

        elif kind == "http":
            parsed = urlsplit(target)
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            sock = open_bound_tcp(parsed.hostname, port, timeout_s, ifname)
            if parsed.scheme == "https":
                context = ssl.create_default_context()
                sock = context.wrap_socket(sock, server_hostname=parsed.hostname)
                sock.settimeout(timeout_s)
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            request_bytes = (
                f"HEAD {path} HTTP/1.1\r\n"
                f"Host: {parsed.hostname}\r\n"
                "User-Agent: NetEm-WAN-Lab-Probe/1\r\n"
                "Connection: close\r\n\r\n"
            ).encode()
            sock.sendall(request_bytes)
            first_line = b""
            while b"\r\n" not in first_line and len(first_line) < 4096:
                chunk = sock.recv(512)
                if not chunk:
                    break
                first_line += chunk
            sock.close()
            latency_ms = (time.perf_counter() - started) * 1000
            line = first_line.split(b"\r\n", 1)[0].decode("latin-1", "replace")
            match = re.match(r"HTTP/\d(?:\.\d)?\s+(\d{3})", line)
            if not match:
                raise OSError("HTTP response did not contain a valid status line.")
            code = int(match.group(1))
            if code >= 500:
                raise OSError(f"HTTP {code}")
            status = str(code)
            detail = line[:180]

        elif kind == "dns":
            resolver = probe.get("resolver") or "1.1.1.1"
            addr = socket.getaddrinfo(resolver, 53, type=socket.SOCK_DGRAM)[0]
            family, socktype, proto, _canonname, sockaddr = addr
            sock = socket.socket(family, socktype, proto)
            sock.settimeout(timeout_s)
            bind_socket_to_interface(sock, ifname)
            transaction_id = int(time.time_ns() & 0xFFFF)
            packet = dns_query_packet(target, transaction_id)
            sock.sendto(packet, sockaddr)
            response, _ = sock.recvfrom(4096)
            sock.close()
            latency_ms = (time.perf_counter() - started) * 1000
            if len(response) < 12 or int.from_bytes(response[:2], "big") != transaction_id:
                raise OSError("DNS response did not match the query.")
            rcode = response[3] & 0x0F
            if rcode != 0:
                raise OSError(f"DNS response code {rcode}")
            answers = int.from_bytes(response[6:8], "big")
            status = f"{answers} answer" + ("" if answers == 1 else "s")
            detail = f"DNS via {resolver}"

        else:
            raise ValueError("Unsupported probe type.")

        return {
            "timestamp": time.time(),
            "probe_id": probe["id"],
            "link_id": probe["link_id"],
            "kind": kind,
            "target": target,
            "success": True,
            "latency_ms": round(latency_ms, 3),
            "status": status,
            "detail": detail,
            "source_interface": ifname,
        }
    except Exception as exc:
        return {
            "timestamp": time.time(),
            "probe_id": probe["id"],
            "link_id": probe["link_id"],
            "kind": kind,
            "target": target,
            "success": False,
            "latency_ms": None,
            "status": "failed",
            "detail": str(exc)[:240],
            "source_interface": ifname,
        }


def record_probe_result(result: dict):
    session_id = active_session_id()
    with telemetry_connect() as conn:
        conn.execute(
            """
            INSERT INTO probe_samples (
                timestamp, probe_id, link_id, kind, target, success,
                latency_ms, status, detail, session_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result["timestamp"],
                result["probe_id"],
                result["link_id"],
                result["kind"],
                result["target"],
                1 if result["success"] else 0,
                result.get("latency_ms"),
                result.get("status"),
                result.get("detail"),
                session_id,
            ),
        )

    with RUNTIME_LOCK:
        previous = PROBE_RUNTIME.get(result["probe_id"])
        runtime = dict(result)
        runtime["session_id"] = session_id
        PROBE_RUNTIME[result["probe_id"]] = runtime

    if previous is not None and bool(previous.get("success")) != bool(result.get("success")):
        state = "recovered" if result.get("success") else "failed"
        log_event(
            "probe",
            f'{result["probe_id"]}: probe {state}',
            probe_id=result["probe_id"],
            link_id=result["link_id"],
            success=result["success"],
            latency_ms=result.get("latency_ms"),
        )


def run_probe_and_record(probe: dict, cfg: dict):
    result = execute_probe(probe, cfg)
    record_probe_result(result)
    return result


def probe_snapshot(cfg: dict):
    rows = []
    with RUNTIME_LOCK:
        runtime = {key: dict(value) for key, value in PROBE_RUNTIME.items()}
    for probe in get_probes(cfg):
        row = dict(probe)
        row["latest"] = runtime.get(probe.get("id")) or latest_probe_sample(probe.get("id"))
        rows.append(row)
    return rows


def probe_worker():
    init_telemetry_db()
    next_due = {}
    while not BACKGROUND_STOP.is_set():
        cfg = load_config()
        probes = [item for item in get_probes(cfg) if item.get("enabled", True)]
        active_ids = {item.get("id") for item in probes}
        next_due = {key: value for key, value in next_due.items() if key in active_ids}
        now = time.time()

        for probe in probes:
            probe_id = probe.get("id")
            if not probe_id or now < next_due.get(probe_id, 0):
                continue
            next_due[probe_id] = now + max(2, int(probe.get("interval_s", 5)))
            try:
                run_probe_and_record(probe, cfg)
            except Exception as exc:
                log_event(
                    "probe",
                    f"{probe_id}: probe execution error",
                    error=str(exc)[:240],
                )
        BACKGROUND_STOP.wait(0.5)


def start_background_workers():
    global TELEMETRY_THREAD, PROBE_THREAD
    init_telemetry_db()
    BACKGROUND_STOP.clear()
    if TELEMETRY_THREAD is None or not TELEMETRY_THREAD.is_alive():
        TELEMETRY_THREAD = threading.Thread(
            target=telemetry_worker,
            name="netem-telemetry",
            daemon=True,
        )
        TELEMETRY_THREAD.start()
    if PROBE_THREAD is None or not PROBE_THREAD.is_alive():
        PROBE_THREAD = threading.Thread(
            target=probe_worker,
            name="netem-probes",
            daemon=True,
        )
        PROBE_THREAD.start()



def read_session_events(session_id: str):
    events = []
    if EVENT_LOG_PATH.exists():
        try:
            for line in EVENT_LOG_PATH.read_text().splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("details", {}).get("session_id") == session_id:
                    events.append(event)
            return events
        except OSError:
            pass
    return [
        event for event in EVENT_LOG
        if event.get("details", {}).get("session_id") == session_id
    ]


def percentile(values, pct):
    clean = sorted(float(value) for value in values if value is not None)
    if not clean:
        return None
    position = (len(clean) - 1) * float(pct)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return clean[lower]
    fraction = position - lower
    return clean[lower] + (clean[upper] - clean[lower]) * fraction


def build_session_report(session_id: str, end_time=None):
    session = next(
        (item for item in LAB_SESSIONS if item.get("id") == session_id),
        None,
    )
    if not session:
        return None

    started_at = float(session.get("started_at") or 0)
    ended_at = float(end_time or session.get("ended_at") or time.time())
    events = read_session_events(session_id)
    assertions = []
    tests = []

    for event in events:
        details = event.get("details", {})
        if event.get("kind") == "assertion":
            assertions.append(
                {
                    "timestamp": event.get("timestamp"),
                    "message": event.get("message"),
                    "label": details.get("label"),
                    "passed": bool(details.get("passed")),
                    "condition": details.get("condition"),
                    "observed": details.get("observed"),
                    "detail": details.get("detail"),
                    "elapsed_s": details.get("elapsed_s"),
                }
            )
        if (
            event.get("kind") == "scenario"
            and details.get("result") is not None
        ):
            tests.append(
                {
                    "timestamp": event.get("timestamp"),
                    "scenario_id": details.get("scenario_id"),
                    "message": event.get("message"),
                    "result": details.get("result"),
                    "duration_s": details.get("duration_s"),
                    "error": details.get("error"),
                }
            )

    telemetry = {}
    probes = {}
    init_telemetry_db()
    with telemetry_connect() as conn:
        telemetry_rows = conn.execute(
            """
            SELECT
                link_id,
                COUNT(*) AS samples,
                AVG(down_mbps) AS avg_down_mbps,
                MAX(down_mbps) AS max_down_mbps,
                AVG(up_mbps) AS avg_up_mbps,
                MAX(up_mbps) AS max_up_mbps,
                AVG(delay_ms) AS avg_injected_delay_ms,
                MAX(delay_ms) AS max_injected_delay_ms,
                AVG(jitter_ms) AS avg_injected_jitter_ms,
                MAX(jitter_ms) AS max_injected_jitter_ms,
                MAX(loss_pct) AS max_injected_loss_pct,
                MIN(quality) AS min_quality,
                SUM(CASE WHEN sla_pass = 0 THEN 1 ELSE 0 END) AS sla_fail_samples
            FROM telemetry_samples
            WHERE session_id = ? AND timestamp BETWEEN ? AND ?
            GROUP BY link_id
            """,
            (session_id, started_at, ended_at),
        ).fetchall()
        for row in telemetry_rows:
            telemetry[row["link_id"]] = dict(row)

        probe_rows = conn.execute(
            """
            SELECT
                probe_id,
                link_id,
                kind,
                target,
                COUNT(*) AS samples,
                SUM(success) AS success_samples,
                AVG(CASE WHEN success = 1 THEN latency_ms END) AS avg_latency_ms,
                MAX(CASE WHEN success = 1 THEN latency_ms END) AS max_latency_ms
            FROM probe_samples
            WHERE session_id = ? AND timestamp BETWEEN ? AND ?
            GROUP BY probe_id, link_id, kind, target
            """,
            (session_id, started_at, ended_at),
        ).fetchall()
        for row in probe_rows:
            item = dict(row)
            latency_rows = conn.execute(
                """
                SELECT latency_ms
                FROM probe_samples
                WHERE session_id = ? AND probe_id = ? AND success = 1
                  AND timestamp BETWEEN ? AND ?
                ORDER BY latency_ms
                """,
                (session_id, row["probe_id"], started_at, ended_at),
            ).fetchall()
            values = [value["latency_ms"] for value in latency_rows]
            item["p95_latency_ms"] = percentile(values, 0.95)
            item["success_rate_pct"] = (
                100.0 * float(item["success_samples"] or 0) / item["samples"]
                if item["samples"]
                else None
            )
            probes[row["probe_id"]] = item

    failed_assertions = [item for item in assertions if not item["passed"]]
    failed_tests = [item for item in tests if item.get("result") == "failed"]
    if failed_assertions or failed_tests:
        result = "failed"
    elif assertions:
        result = "passed"
    else:
        result = "unscored"

    event_counts = {}
    for event in events:
        kind = event.get("kind") or "event"
        event_counts[kind] = event_counts.get(kind, 0) + 1

    return {
        "session": {
            "id": session_id,
            "name": session.get("name"),
            "started_at": started_at,
            "ended_at": ended_at,
            "duration_s": round(max(0, ended_at - started_at), 3),
            "status": session.get("status"),
        },
        "generated_at": time.time(),
        "result": result,
        "assertions": assertions,
        "tests": tests,
        "telemetry": telemetry,
        "probes": probes,
        "event_counts": event_counts,
        "event_count": len(events),
        "events": events[-250:],
    }


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


def compare_condition_value(actual, operator, expected):
    if actual is None:
        return False
    if operator == "==":
        return actual == expected
    if operator == "!=":
        return actual != expected
    try:
        actual_n = float(actual)
        expected_n = float(expected)
    except (TypeError, ValueError):
        return False
    if operator == "<":
        return actual_n < expected_n
    if operator == "<=":
        return actual_n <= expected_n
    if operator == ">":
        return actual_n > expected_n
    if operator == ">=":
        return actual_n >= expected_n
    return False


def condition_summary(condition: dict):
    kind = condition.get("type")
    if kind == "sla":
        return f'expected SLA = {condition.get("state", "pass").upper()}'
    if kind == "probe":
        return (
            f'{condition.get("probe_id")} {condition.get("field")} '
            f'{condition.get("op")} {condition.get("value")}'
        )
    if kind == "traffic":
        return (
            f'{condition.get("field")} {condition.get("op")} '
            f'{condition.get("value")}'
        )
    if kind == "dem":
        return (
            f'DEM {condition.get("field")} {condition.get("op")} '
            f'{condition.get("value")}'
        )
    return "condition"


def evaluate_scenario_condition(condition: dict, default_link_id: str):
    cfg = load_config()
    kind = condition.get("type")
    link_id = condition.get("link_id") or default_link_id

    if kind == "sla":
        state = next(
            (item for item in build_link_states(cfg) if item.get("id") == link_id),
            None,
        )
        if not state:
            return False, None, f"Unknown WAN {link_id}"
        actual = bool(state.get("sla", {}).get("pass"))
        expected = condition.get("state") == "pass"
        return actual == expected, actual, "PASS" if actual else "FAIL"

    if kind == "probe":
        probe_id = condition.get("probe_id")
        sample = latest_probe_sample(probe_id)
        if not sample:
            return False, None, "No probe sample yet"

        probe_cfg = next(
            (item for item in get_probes(cfg) if item.get("id") == probe_id),
            None,
        )
        max_age = max(
            15.0,
            float((probe_cfg or {}).get("interval_s", 5)) * 3,
        )
        age = time.time() - float(sample.get("timestamp", 0))
        if age > max_age:
            return False, None, f"Probe sample is stale ({age:.1f}s old)"

        field = condition.get("field", "success")
        actual = (
            bool(sample.get("success"))
            if field == "success"
            else sample.get("latency_ms")
        )
        passed = compare_condition_value(
            actual,
            condition.get("op", "=="),
            condition.get("value"),
        )
        return passed, actual, sample.get("detail") or sample.get("status")

    if kind == "traffic":
        sample = latest_telemetry_sample(link_id)
        if not sample:
            return False, None, "No telemetry sample yet"
        field = condition.get("field", "down_mbps")
        actual = sample.get(field)
        passed = compare_condition_value(
            actual,
            condition.get("op", ">="),
            condition.get("value"),
        )
        return passed, actual, f"{field}={actual}"

    if kind == "dem":
        try:
            window = int(condition.get("window", 60))
            payload = traffic_generator_request(
                f"/api/v1/dem/experience?window={window}",
                timeout=2.5,
            )
        except (RuntimeError, ValueError) as exc:
            return False, None, str(exc)

        field = condition.get("field", "experience_score")
        experience = payload.get("endpoint_experience") or {}
        actual = (
            payload.get("active_users")
            if field == "active_users"
            else experience.get(field)
        )
        passed = compare_condition_value(
            actual,
            condition.get("op", ">="),
            condition.get("value"),
        )
        detail = (
            f'{field}={actual} · '
            f'rating={experience.get("rating", "unknown")}'
        )
        return passed, actual, detail

    return False, None, "Unsupported condition"


def wait_for_scenario_condition(
    condition: dict,
    default_link_id: str,
    timeout_s: float,
    poll_s: float,
):
    started = time.time()
    deadline = started + max(1.0, float(timeout_s))
    last_observed = None
    last_detail = None

    with RUNTIME_LOCK:
        SCENARIO_STATE["condition"] = {
            "description": condition_summary(condition),
            "started_at": started,
            "timeout": timeout_s,
            "observed": None,
        }

    while time.time() <= deadline:
        if SCENARIO_STOP.is_set():
            return False, last_observed, "stopped", time.time() - started

        passed, observed, detail = evaluate_scenario_condition(
            condition, default_link_id
        )
        last_observed = observed
        last_detail = detail
        with RUNTIME_LOCK:
            if isinstance(SCENARIO_STATE.get("condition"), dict):
                SCENARIO_STATE["condition"]["observed"] = observed
                SCENARIO_STATE["condition"]["detail"] = detail

        if passed:
            return True, observed, detail, time.time() - started
        SCENARIO_STOP.wait(max(0.25, min(5.0, float(poll_s))))

    passed, observed, detail = evaluate_scenario_condition(
        condition, default_link_id
    )
    return passed, observed, detail or last_detail, time.time() - started


def run_scenario(link_id: str, scenario: dict):
    cfg = load_config()
    presets = get_presets(cfg)
    link = get_link(cfg, link_id)
    if not link:
        with RUNTIME_LOCK:
            SCENARIO_STATE.update(
                {"active": False, "result": "failed", "error": "Unknown WAN"}
            )
        return

    original = copy.deepcopy(link)
    runtime_profile = copy.deepcopy(original)
    scenario_result = "passed"
    scenario_error = None
    started_at = time.time()

    log_event(
        "scenario",
        f'{scenario["name"]} started',
        scenario_id=scenario.get("id"),
        link_id=link_id,
        stage_count=len(scenario.get("steps", [])),
    )

    try:
        for index, step in enumerate(scenario.get("steps", []), start=1):
            if SCENARIO_STOP.wait(max(0, int(step.get("after", 0)))):
                scenario_result = "stopped"
                break

            action = step.get("action")
            label = step.get("label") or action
            with RUNTIME_LOCK:
                SCENARIO_STATE.update(
                    {
                        "step": index,
                        "step_label": label,
                        "step_action": action,
                        "condition": None,
                    }
                )

            if action == "quality":
                runtime_profile = copy.deepcopy(original)
                runtime_profile["mode"] = "quality"
                runtime_profile["quality"] = int(step.get("value", 100))
                runtime_profile.pop("custom_profile", None)
                ok, msg, _effective = apply_selected_profile(
                    runtime_profile, presets
                )
                if not ok:
                    scenario_result = "failed"
                    scenario_error = msg
                    break
                ACTIVE_FAULTS.pop(link_id, None)
                log_event(
                    "scenario",
                    f'{scenario["name"]}: {label}',
                    scenario_id=scenario.get("id"),
                    link_id=link_id,
                    quality=runtime_profile["quality"],
                )

            elif action == "fault":
                ok, msg = apply_runtime_fault(
                    runtime_profile,
                    step.get("value", "normal"),
                    presets,
                )
                if not ok:
                    scenario_result = "failed"
                    scenario_error = msg
                    break

            elif action == "mtu":
                ok, msg = apply_mtu_limit(
                    runtime_profile, int(step.get("value", 0))
                )
                if not ok:
                    scenario_result = "failed"
                    scenario_error = msg
                    break

            elif action in ("wait", "assert"):
                condition = step.get("condition") or {}
                passed, observed, detail, elapsed = wait_for_scenario_condition(
                    condition,
                    link_id,
                    step.get("timeout", 30),
                    step.get("poll", 0.5),
                )
                if SCENARIO_STOP.is_set():
                    scenario_result = "stopped"
                    break

                details = {
                    "scenario_id": scenario.get("id"),
                    "link_id": link_id,
                    "label": label,
                    "passed": bool(passed),
                    "condition": condition,
                    "observed": observed,
                    "detail": detail,
                    "elapsed_s": round(elapsed, 3),
                }

                if action == "assert":
                    log_event(
                        "assertion",
                        f'{scenario["name"]}: {label} — '
                        + ("PASS" if passed else "FAIL"),
                        **details,
                    )
                else:
                    log_event(
                        "condition",
                        f'{scenario["name"]}: {label} — '
                        + ("satisfied" if passed else "timeout"),
                        **details,
                    )

                if not passed:
                    scenario_result = "failed"
                    scenario_error = (
                        f'{label}: condition not satisfied within '
                        f'{step.get("timeout", 30)}s'
                    )
                    if step.get("on_fail", "stop") == "stop":
                        break

        if SCENARIO_STOP.is_set() and scenario_result == "passed":
            scenario_result = "stopped"

    except Exception as exc:
        scenario_result = "failed"
        scenario_error = str(exc)[:240]

    finally:
        apply_mtu_limit(original, 0)
        apply_selected_profile(original, presets)
        ACTIVE_FAULTS.pop(link_id, None)
        duration_s = round(time.time() - started_at, 3)
        log_event(
            "scenario",
            f'{scenario["name"]} finished — {scenario_result.upper()}',
            scenario_id=scenario.get("id"),
            link_id=link_id,
            result=scenario_result,
            error=scenario_error,
            duration_s=duration_s,
        )
        with RUNTIME_LOCK:
            SCENARIO_STATE.update(
                {
                    "active": False,
                    "scenario_id": None,
                    "scenario_name": None,
                    "link_id": None,
                    "started_at": None,
                    "step": 0,
                    "step_count": 0,
                    "step_label": None,
                    "step_action": None,
                    "condition": None,
                    "result": scenario_result,
                    "error": scenario_error,
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
        "documentation",
        "tests",
        "sessions",
    }
    endpoint = requested if requested in allowed else default_endpoint
    return redirect(url_for(endpoint))


DOC_HELP_BY_ENDPOINT = {
    "overview": "overview",
    "index": "overview",
    "tests": "tests",
    "scenarios": "tests",
    "lab_tools": "tests",
    "traffic_security": "tests",
    "analytics": "analytics-sla",
    "sessions": "sessions",
    "integrations": "integrations-api",
    "settings": "topology-profiles",
    "setup": "topology-profiles",
    "presets": "topology-profiles",
    "updates": "updates-releases",
}


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
                "label": "Lab",
                "items": [
                    {"id": "overview", "label": "Command Center", "endpoint": "overview", "icon": "overview"},
                    {"id": "tests", "label": "Tests", "endpoint": "tests", "icon": "scenario"},
                    {"id": "analytics", "label": "Analytics", "endpoint": "analytics", "icon": "analytics"},
                    {"id": "sessions", "label": "Sessions", "endpoint": "sessions", "icon": "sessions"},
                ],
            },
            {
                "label": "System",
                "items": [
                    {"id": "settings", "label": "Settings", "endpoint": "settings", "icon": "settings"},
                ],
            },
        ],
        "config": cfg,
        "app_version": get_app_version(),
        "help_doc_slug": DOC_HELP_BY_ENDPOINT.get(request.endpoint),
        "global_links": build_link_states(cfg) if cfg.get("wan_links") else [],
        "global_runtime": {
            "scenario": scenario_snapshot(),
            "session": session_snapshot(),
            "active_fault_count": len(active_fault_labels),
            "active_fault_labels": active_fault_labels,
            "capture": capture_snapshot(),
        },
    }


DOCS_PAGES = [
    {
        "slug": "getting-started",
        "title": "Getting started",
        "category": "Start here",
        "summary": "Understand the lab workflow and run your first controlled WAN impairment.",
        "template": "docs/articles/getting_started.html",
        "keywords": "first run workflow lab test basic restore topology",
    },
    {
        "slug": "architecture",
        "title": "Architecture & traffic direction",
        "category": "Start here",
        "summary": "How the transparent bridges, inner/outer interfaces and directional shaping model work.",
        "template": "docs/articles/architecture.html",
        "keywords": "bridge inner outer download upload tc netem tbf linux datapath",
    },
    {
        "slug": "overview",
        "title": "Command Center",
        "category": "Operate",
        "summary": "Operate the lab from one live view with clickable WAN controls, flow state, sparklines and quick actions.",
        "template": "docs/articles/overview.html",
        "keywords": "command center dashboard live topology throughput events health status quick actions",
    },
    {
        "slug": "tests",
        "title": "Tests",
        "category": "Operate",
        "summary": "Run guided brownout, failover, unstable-link, security and packet-capture workflows.",
        "template": "docs/articles/tests.html",
        "keywords": "tests scenarios brownout failover flaky link eicar beacon capture guided workflow",
    },
    {
        "slug": "sessions",
        "title": "Lab sessions",
        "category": "Operate",
        "summary": "Group tests and runtime events into one named validation run.",
        "template": "docs/articles/sessions.html",
        "keywords": "session lab run validation evidence event group result history",
    },
    {
        "slug": "wan-links",
        "title": "WAN Links",
        "category": "Operate",
        "summary": "Profiles, quality models, manual impairments, faults, MTU tests and diagnostics.",
        "template": "docs/articles/wan_links.html",
        "keywords": "quality latency jitter loss bandwidth duplicate corrupt reorder blackhole mtu qdisc",
    },
    {
        "slug": "scenarios",
        "title": "Scenarios",
        "category": "Operate",
        "summary": "Run built-in tests and create reusable quality, fault and MTU sequences.",
        "template": "docs/articles/scenarios.html",
        "keywords": "brownout failover flaky availability custom json steps automation",
    },
    {
        "slug": "traffic-security",
        "title": "Traffic & Security",
        "category": "Operate",
        "summary": "Use EICAR, benign beacons, availability stress and bounded packet capture safely.",
        "template": "docs/articles/traffic_security.html",
        "keywords": "eicar beacon tcpdump pcap capture security ddos safe",
    },
    {
        "slug": "analytics-sla",
        "title": "Analytics & SLA",
        "category": "Observe",
        "summary": "Interpret measured traffic, injected impairment, chart scales and expected SLA state.",
        "template": "docs/articles/analytics_sla.html",
        "keywords": "analytics charts throughput pps latency jitter loss quality sla measured injected",
    },
    {
        "slug": "integrations-api",
        "title": "Integrations & API",
        "category": "Observe",
        "summary": "REST endpoints, Prometheus metrics and the vendor-neutral adapter model.",
        "template": "docs/articles/integrations_api.html",
        "keywords": "api json prometheus metrics state telemetry events vendor adapter fortinet cisco palo alto",
    },
    {
        "slug": "topology-profiles",
        "title": "Topology & access profiles",
        "category": "Configure",
        "summary": "Map interfaces, persist bridges and customize DIA, broadband, mobile and satellite baselines.",
        "template": "docs/articles/topology_profiles.html",
        "keywords": "setup interface bridge management presets dia dsl broadband 4g 5g satellite startup",
    },
    {
        "slug": "updates-releases",
        "title": "Updates & releases",
        "category": "Configure",
        "summary": "Stable origin/main updates, fast-forward safety, versioning and Release Please.",
        "template": "docs/articles/updates_releases.html",
        "keywords": "git update release version main release please permissions techkarma",
    },
    {
        "slug": "troubleshooting",
        "title": "Troubleshooting",
        "category": "Reference",
        "summary": "Diagnose live telemetry, shaping, permissions, packet capture and service problems.",
        "template": "docs/articles/troubleshooting.html",
        "keywords": "debug troubleshoot telemetry zero throughput permission qdisc tbf tcpdump cap_net_raw service",
    },
    {
        "slug": "roadmap",
        "title": "Product roadmap",
        "category": "Reference",
        "summary": "Prioritized next capabilities for measurement, evidence, vendor correlation, orchestration and scale.",
        "template": "docs/articles/roadmap.html",
        "keywords": "roadmap future active probes sqlite reports adapter assertions conditional traffic generator scale",
    },
    {
        "slug": "reference",
        "title": "Reference & limits",
        "category": "Reference",
        "summary": "Default values, validation limits, terminology, storage locations and current boundaries.",
        "template": "docs/articles/reference.html",
        "keywords": "limits defaults paths glossary event history config runtime max minimum",
    },
]

DOCS_BY_SLUG = {item["slug"]: item for item in DOCS_PAGES}


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
        session_state=session_snapshot(),
        presets=get_presets(cfg),
        quality_curves=QUALITY_CURVES,
        traffic_generator=traffic_generator_snapshot(),
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
    return redirect(url_for("tests"))


@app.route("/tests")
def tests():
    cfg = load_config()
    scenario_id = request.args.get("scenario")
    traffic_generator = traffic_generator_snapshot(include_catalog=True)
    return render_template(
        "tests.html",
        page="tests",
        links=build_link_states(cfg),
        scenarios=get_scenarios(cfg),
        custom_scenarios=cfg.get("custom_scenarios", []),
        scenario_state=scenario_snapshot(),
        capture_state=capture_snapshot(),
        tcpdump_available=bool(shutil.which("tcpdump")),
        selected_scenario=scenario_id,
        traffic_generator=traffic_generator,
        events=list(reversed([
            event for event in EVENT_LOG
            if event.get("kind") in (
                "scenario", "scenario-config", "fault", "mtu",
                "capture", "security-test", "traffic-generator"
            )
        ][-40:])),
    )


@app.route("/sessions")
def sessions():
    active = session_snapshot()
    active_events = []
    if active.get("active"):
        active_events = list(reversed([
            event for event in EVENT_LOG
            if event.get("details", {}).get("session_id") == active.get("id")
        ][-30:]))
    return render_template(
        "sessions.html",
        page="sessions",
        active_session=active,
        active_events=active_events,
        sessions=session_rows(),
    )


@app.route("/sessions/start", methods=["POST"])
def session_start():
    name = (request.form.get("name") or "Lab session").strip()[:100]
    with RUNTIME_LOCK:
        if ACTIVE_SESSION["active"]:
            flash("A lab session is already active.", "error")
            return redirect_after("sessions")

        session_id = f"session-{time.time_ns()}"
        ACTIVE_SESSION.update(
            {
                "active": True,
                "id": session_id,
                "name": name or "Lab session",
                "started_at": time.time(),
            }
        )
        LAB_SESSIONS.append(
            {
                "id": session_id,
                "name": ACTIVE_SESSION["name"],
                "started_at": ACTIVE_SESSION["started_at"],
                "ended_at": None,
                "status": "active",
            }
        )
        del LAB_SESSIONS[:-100]
        save_session_history()

    log_event("session", f'Lab session started: {ACTIVE_SESSION["name"]}')
    flash(f'Lab session "{ACTIVE_SESSION["name"]}" started.', "success")
    return redirect_after("sessions")


@app.route("/sessions/stop", methods=["POST"])
def session_stop():
    with RUNTIME_LOCK:
        if not ACTIVE_SESSION["active"]:
            flash("No lab session is active.", "info")
            return redirect_after("sessions")
        session_id = ACTIVE_SESSION["id"]
        session_name = ACTIVE_SESSION["name"]

    log_event("session", f"Lab session completed: {session_name}")

    ended_at = time.time()
    with RUNTIME_LOCK:
        for item in reversed(LAB_SESSIONS):
            if item.get("id") == session_id:
                item["ended_at"] = ended_at
                item["status"] = "completed"
                break

    report = build_session_report(session_id, end_time=ended_at)

    with RUNTIME_LOCK:
        for item in reversed(LAB_SESSIONS):
            if item.get("id") == session_id:
                item["report"] = report
                break
        ACTIVE_SESSION.update(
            {
                "active": False,
                "id": None,
                "name": None,
                "started_at": None,
            }
        )
        save_session_history()

    flash(
        f'Lab session "{session_name}" completed'
        + (
            f' · result {report["result"].upper()}.'
            if report
            else "."
        ),
        "success",
    )
    return redirect_after("sessions")


@app.route("/sessions/<session_id>/report")
def session_report(session_id):
    session = next(
        (item for item in LAB_SESSIONS if item.get("id") == session_id),
        None,
    )
    if not session:
        abort(404)
    report = session.get("report") or build_session_report(session_id)
    if not report:
        abort(404)
    return render_template(
        "session_report.html",
        page="sessions",
        report=report,
    )


@app.route("/sessions/<session_id>/report.json")
def session_report_json(session_id):
    session = next(
        (item for item in LAB_SESSIONS if item.get("id") == session_id),
        None,
    )
    if not session:
        abort(404)
    report = session.get("report") or build_session_report(session_id)
    if not report:
        abort(404)
    return jsonify(report)


@app.route("/scenarios")
def scenarios():
    return redirect(url_for("tests"))


@app.route("/traffic-security")
def traffic_security():
    return redirect(url_for("tests"))


@app.route("/analytics")
def analytics():
    cfg = load_config()
    return render_template(
        "analytics.html",
        page="analytics",
        links=build_link_states(cfg),
        sla_profile=get_sla_profile(cfg),
        probes=probe_snapshot(cfg),
        telemetry_retention_hours=TELEMETRY_RETENTION_HOURS,
        events=list(reversed(EVENT_LOG[-80:])),
    )


@app.route("/probes/save", methods=["POST"])
def probe_save():
    cfg = load_config()
    existing_id = (request.form.get("probe_id") or "").strip() or None
    raw = {
        "name": request.form.get("name"),
        "link_id": request.form.get("link_id"),
        "kind": request.form.get("kind"),
        "target": request.form.get("target"),
        "port": request.form.get("port"),
        "resolver": request.form.get("resolver"),
        "source_side": request.form.get("source_side"),
        "interval_s": request.form.get("interval_s"),
        "timeout_s": request.form.get("timeout_s"),
        "enabled": request.form.get("enabled") == "on",
    }
    try:
        probe = validate_probe_definition(raw, cfg, existing_id=existing_id)
    except ValueError as exc:
        flash(f"Probe configuration is invalid: {exc}", "error")
        return redirect(url_for("analytics") + "#measurements")

    probes = [
        item for item in get_probes(cfg)
        if item.get("id") != probe["id"]
    ]
    if existing_id is None and len(probes) >= MAX_PROBES:
        flash(f"A maximum of {MAX_PROBES} active-measurement probes is supported.", "error")
        return redirect(url_for("analytics") + "#measurements")
    probes.append(probe)
    cfg["probes"] = probes[-MAX_PROBES:]
    save_config(cfg)
    log_event(
        "probe-config",
        f'Saved probe "{probe["name"]}"',
        probe_id=probe["id"],
        link_id=probe["link_id"],
        kind=probe["kind"],
    )
    flash(f'Probe "{probe["name"]}" saved.', "success")
    return redirect(url_for("analytics") + "#measurements")


@app.route("/probes/delete", methods=["POST"])
def probe_delete():
    cfg = load_config()
    probe_id = (request.form.get("probe_id") or "").strip()
    before = len(get_probes(cfg))
    cfg["probes"] = [
        item for item in get_probes(cfg)
        if item.get("id") != probe_id
    ]
    if len(cfg["probes"]) != before:
        save_config(cfg)
        with RUNTIME_LOCK:
            PROBE_RUNTIME.pop(probe_id, None)
        log_event("probe-config", f"Deleted probe {probe_id}", probe_id=probe_id)
        flash("Probe deleted.", "info")
    return redirect(url_for("analytics") + "#measurements")


@app.route("/probes/run", methods=["POST"])
def probe_run():
    cfg = load_config()
    probe_id = (request.form.get("probe_id") or "").strip()
    probe = next(
        (item for item in get_probes(cfg) if item.get("id") == probe_id),
        None,
    )
    if not probe:
        flash("Unknown probe.", "error")
        return redirect(url_for("analytics") + "#measurements")

    result = run_probe_and_record(probe, cfg)
    if result.get("success"):
        flash(
            f'{probe["name"]}: success in {result.get("latency_ms", 0):.1f} ms.',
            "success",
        )
    else:
        flash(
            f'{probe["name"]}: failed — {result.get("detail") or "unknown error"}.',
            "error",
        )
    return redirect(url_for("analytics") + "#measurements")


@app.route("/integrations")
def integrations():
    cfg = load_config()
    discovered = (
        discover_traffic_generators()
        if request.args.get("discover") == "1"
        else []
    )
    return render_template(
        "integrations.html",
        page="integrations",
        traffic_generator=traffic_generator_snapshot(include_catalog=False),
        traffic_generator_discovered=discovered,
        traffic_generator_has_key=bool(traffic_generator_api_key()),
    )


@app.route("/integrations/traffic-generator/save", methods=["POST"])
def traffic_generator_save():
    cfg = load_config()
    selected = (request.form.get("discovered_host") or "").strip()
    manual = (request.form.get("manual_host") or "").strip()
    host = manual if selected in ("", "manual") else selected

    if host.startswith("https://") or host.startswith("http://"):
        parsed = urlsplit(host)
        host = parsed.hostname or ""
        discovered_port = parsed.port
    else:
        discovered_port = None

    if not host or len(host) > 255 or not re.fullmatch(r"[A-Za-z0-9_.:\-]+", host):
        flash("Enter a valid Traffic Simulator IP address or hostname.", "error")
        return redirect(url_for("integrations") + "#traffic-simulator")

    try:
        port = int(request.form.get("port") or discovered_port or 8443)
    except ValueError:
        port = 8443
    if not 1 <= port <= 65535:
        flash("Traffic Simulator API port must be 1-65535.", "error")
        return redirect(url_for("integrations") + "#traffic-simulator")

    integration = {
        "host": host,
        "port": port,
        "allow_self_signed": request.form.get("allow_self_signed") == "on",
        "instance_name": (request.form.get("instance_name") or "").strip()[:120] or None,
        "version": (request.form.get("version") or "").strip()[:40] or None,
        "tls_sha256": (request.form.get("tls_sha256") or "").strip()[:128] or None,
    }
    cfg["traffic_generator"] = integration
    save_config(cfg)

    api_key = (request.form.get("api_key") or "").strip()
    if api_key:
        secrets_data = load_secrets()
        secrets_data["traffic_generator_api_key"] = api_key
        save_secrets(secrets_data)

    if not traffic_generator_api_key():
        flash("Traffic Simulator saved, but an API key is still required.", "warning")
    else:
        try:
            status = traffic_generator_request("/api/v1/status", timeout=3.0)
            run_state = status.get("status", "connected")
            flash(
                f"Traffic Simulator connected successfully · {run_state}.",
                "success",
            )
        except RuntimeError as exc:
            flash(f"Traffic Simulator saved, but connection test failed: {exc}", "warning")

    return redirect(url_for("integrations") + "#traffic-simulator")


@app.route("/integrations/traffic-generator/test", methods=["POST"])
def traffic_generator_test():
    try:
        status = traffic_generator_request("/api/v1/status", timeout=3.0)
        dem = status.get("dem") or {}
        score = dem.get("experience_score")
        detail = f" · DEM {score}" if score is not None else ""
        flash(
            f'Traffic Simulator API connected · {status.get("status", "unknown")}{detail}.',
            "success",
        )
    except RuntimeError as exc:
        flash(str(exc), "error")
    return redirect(url_for("integrations") + "#traffic-simulator")


@app.route("/traffic-generator/start", methods=["POST"])
def traffic_generator_start():
    payload = {
        "profile": request.form.get("profile") or "office",
        "users": request.form.get("users") or 50,
        "spawn_rate": request.form.get("spawn_rate") or 5,
        "activity": request.form.get("activity") or "normal",
        "pattern": request.form.get("pattern") or "steady",
    }
    target = (request.form.get("target") or "").strip()
    if target:
        payload["target"] = target

    try:
        status = traffic_generator_request(
            "/api/v1/workloads/start",
            method="POST",
            payload=payload,
            timeout=5.0,
        )
        run = status.get("run") or {}
        log_event(
            "traffic-generator",
            f'Corporate workload started · {payload["profile"]} · {payload["users"]} users',
            action="start",
            run_id=run.get("run_id"),
            profile=payload["profile"],
            users=int(payload["users"]),
            pattern=payload["pattern"],
        )
        flash("Corporate Traffic Simulator workload started.", "success")
    except (RuntimeError, ValueError) as exc:
        flash(str(exc), "error")
    return redirect_after("tests")


@app.route("/traffic-generator/adjust", methods=["POST"])
def traffic_generator_adjust():
    payload = {}
    for key in ("users", "spawn_rate", "activity"):
        value = request.form.get(key)
        if value not in (None, ""):
            payload[key] = value
    try:
        status = traffic_generator_request(
            "/api/v1/workloads/adjust",
            method="POST",
            payload=payload,
            timeout=5.0,
        )
        log_event(
            "traffic-generator",
            "Corporate workload adjusted",
            action="adjust",
            payload=payload,
            users=status.get("users"),
        )
        flash("Corporate workload adjusted.", "success")
    except RuntimeError as exc:
        flash(str(exc), "error")
    return redirect_after("tests")


@app.route("/traffic-generator/stop", methods=["POST"])
def traffic_generator_stop():
    try:
        status_before = traffic_generator_request("/api/v1/status", timeout=3.0)
        traffic_generator_request(
            "/api/v1/workloads/stop",
            method="POST",
            payload={},
            timeout=5.0,
        )
        dem = status_before.get("dem") or {}
        log_event(
            "traffic-generator",
            "Corporate workload stopped",
            action="stop",
            run_id=(status_before.get("run") or {}).get("run_id"),
            users=status_before.get("users"),
            experience_score=dem.get("experience_score"),
            availability_pct=dem.get("availability_pct"),
            p95_ms=dem.get("p95_ms"),
        )
        flash("Corporate workload stopped.", "info")
    except RuntimeError as exc:
        flash(str(exc), "error")
    return redirect_after("tests")


@app.route("/api/v1/traffic-generator")
def api_traffic_generator():
    snapshot = traffic_generator_snapshot(include_catalog=False)
    code = 200 if snapshot.get("connected") or not snapshot.get("configured") else 503
    return jsonify(snapshot), code


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


@app.route("/docs")
def documentation():
    return render_template(
        "docs/index.html",
        page="docs",
        docs_pages=DOCS_PAGES,
    )


@app.route("/docs/<slug>")
def documentation_page(slug):
    doc = DOCS_BY_SLUG.get(slug)
    if not doc:
        abort(404)

    index = DOCS_PAGES.index(doc)
    previous_doc = DOCS_PAGES[index - 1] if index > 0 else None
    next_doc = DOCS_PAGES[index + 1] if index + 1 < len(DOCS_PAGES) else None

    return render_template(
        "docs/page.html",
        page="docs",
        docs_pages=DOCS_PAGES,
        doc=doc,
        previous_doc=previous_doc,
        next_doc=next_doc,
    )


@app.route("/wan/quick", methods=["POST"])
def quick_wan_action():
    cfg = load_config()
    presets = get_presets(cfg)
    link_id = request.form.get("link_id") or ""
    action = request.form.get("action") or ""
    link = get_link(cfg, link_id)

    if not link:
        flash("Unknown WAN link.", "error")
        return redirect_after("overview")

    if scenario_snapshot().get("active"):
        flash("Stop the active scenario before changing the WAN manually.", "error")
        return redirect_after("overview")

    if action == "quality":
        try:
            quality = max(0, min(100, int(request.form.get("quality", "100"))))
        except ValueError:
            quality = 100
        link["quality"] = quality
        link["mode"] = "quality"
        link.pop("custom_profile", None)
        ok, msg, _effective = apply_selected_profile(link, presets)
        if ok:
            save_config(cfg)
            log_event(
                "quality",
                f'{link.get("name", link_id)} quality set to {quality}%',
                link_id=link_id,
                quality=quality,
            )
            flash(
                f'{link.get("name", "WAN")} set to {quality}% ({quality_status(quality)}).',
                "success",
            )
        else:
            flash("Failed to apply WAN quality: " + msg, "error")

    elif action in (
        "normal", "blackhole", "downstream_blackhole", "upstream_blackhole"
    ):
        ok, msg = apply_runtime_fault(link, action, presets)
        if ok:
            flash(
                "WAN restored." if action == "normal"
                else f'{link.get("name", "WAN")}: {action.replace("_", " ")} applied.',
                "success",
            )
        else:
            flash("Failed to apply runtime fault: " + msg, "error")

    elif action == "bandwidth":
        try:
            download = max(1, min(100000, int(request.form.get("download_mbit", "1"))))
            upload = max(1, min(100000, int(request.form.get("upload_mbit", "1"))))
        except ValueError:
            flash("Bandwidth values must be whole-number Mbit/s values.", "error")
            return redirect_after("overview")

        link["bandwidth_download_mbit"] = download
        link["bandwidth_upload_mbit"] = upload
        ok, msg, _effective = apply_selected_profile(link, presets)
        if ok:
            save_config(cfg)
            log_event(
                "bandwidth",
                f'{link.get("name", link_id)} line rate set to {download}/{upload} Mbit/s',
                link_id=link_id,
                download_mbit=download,
                upload_mbit=upload,
            )
            flash(
                f'{link.get("name", "WAN")} nominal rate set to {download}/{upload} Mbit/s.',
                "success",
            )
        else:
            flash("Failed to apply bandwidth limit: " + msg, "error")

    elif action == "mtu":
        try:
            mtu = int(request.form.get("mtu", "0"))
        except ValueError:
            mtu = 0
        ok, msg = apply_mtu_limit(link, mtu)
        if ok:
            flash(
                "Path MTU restored." if mtu == 0
                else f"Path MTU limited to {mtu} bytes.",
                "success",
            )
        else:
            flash("Failed to change path MTU: " + msg, "error")

    else:
        flash("Unknown quick action.", "error")

    return redirect_after("overview")


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
                "step_count": len(scenario.get("steps", [])),
                "step_label": "Starting",
                "step_action": None,
                "condition": None,
                "result": None,
                "error": None,
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
            "session": session_snapshot(),
            "capture": capture_snapshot(),
            "probes": probe_snapshot(cfg),
            "sla_profile": get_sla_profile(cfg),
            "events": EVENT_LOG[-20:],
        }
    )


@app.route("/api/v1/telemetry")
def api_telemetry():
    """
    Return raw interface counters plus a vendor-neutral directional view.

    For the transparent WAN topology:
      * download traffic exits the inner interface toward the appliance
      * upload traffic exits the outer interface toward the upstream router

    Raw inner/outer counters remain exposed for diagnostics and backwards
    compatibility. The directional counters are the preferred UI/API surface.
    """
    cfg = load_config()
    links = []
    for link in cfg.get("wan_links", []):
        link_id = link.get("id") or link.get("bridge")
        inner_if = link.get("inner")
        outer_if = link.get("outer")
        inner = interface_counters(inner_if)
        outer = interface_counters(outer_if)
        inner_status = interface_runtime_status(inner_if)
        outer_status = interface_runtime_status(outer_if)

        links.append(
            {
                "id": link_id,
                "name": link.get("name", "WAN"),
                "inner": {
                    "interface": inner_if,
                    "counters": inner,
                    **inner_status,
                },
                "outer": {
                    "interface": outer_if,
                    "counters": outer,
                    **outer_status,
                },
                "traffic": {
                    "download": {
                        "interface": inner_if,
                        "bytes": inner["tx_bytes"],
                        "packets": inner["tx_packets"],
                        "dropped": inner["tx_dropped"],
                        "errors": inner["tx_errors"],
                    },
                    "upload": {
                        "interface": outer_if,
                        "bytes": outer["tx_bytes"],
                        "packets": outer["tx_packets"],
                        "dropped": outer["tx_dropped"],
                        "errors": outer["tx_errors"],
                    },
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


@app.route("/api/v1/history")
def api_history():
    cfg = load_config()
    link_id = (request.args.get("link_id") or "").strip()
    if not get_link(cfg, link_id):
        return jsonify({"error": "Unknown WAN link."}), 404

    try:
        minutes = max(1, min(TELEMETRY_RETENTION_HOURS * 60, int(request.args.get("minutes", "60"))))
        max_points = max(50, min(5000, int(request.args.get("max_points", "1200"))))
    except ValueError:
        return jsonify({"error": "minutes and max_points must be integers."}), 400

    since = time.time() - minutes * 60
    return jsonify(
        {
            "timestamp": time.time(),
            "link_id": link_id,
            "minutes": minutes,
            "samples": query_telemetry_history(link_id, since, max_points=max_points),
            "probes": query_probe_history(link_id=link_id, since=since, limit=max_points),
        }
    )


@app.route("/api/v1/probes")
def api_probes():
    cfg = load_config()
    return jsonify(
        {
            "timestamp": time.time(),
            "probes": probe_snapshot(cfg),
        }
    )


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
        page="settings",
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
        page="settings",
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
        return redirect_after("wan_links")

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
        return redirect_after("wan_links")

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

    return redirect_after("wan_links")

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
        page="settings",
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
        return redirect_after("wan_links")

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
    return redirect_after("wan_links")


if __name__ == "__main__":
    restore_runtime_state()
    start_background_workers()
    app.run(host="0.0.0.0", port=8081, debug=False)