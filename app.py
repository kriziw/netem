#!/usr/bin/env python3
import copy
import csv
import io
import ipaddress
import hashlib
import hmac
import http.client
import secrets
import tempfile
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
import uuid
from pathlib import Path
from urllib.parse import urlsplit
from urllib import error as urllib_error
from urllib import request as urllib_request

import site_catalog
from showroom import create_showroom_app
import branding

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
    session,
)

app = Flask(__name__)


BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
VERSION_PATH = BASE_DIR / "version.txt"
RUNTIME_DIR = BASE_DIR / "runtime"
EVENT_LOG_PATH = RUNTIME_DIR / "events.jsonl"
SESSIONS_PATH = RUNTIME_DIR / "sessions.json"
TELEMETRY_DB_PATH = RUNTIME_DIR / "telemetry.db"
SECRETS_PATH = RUNTIME_DIR / "secrets.json"
CAPTURE_DIR = RUNTIME_DIR / "captures"

BRANDING_DIR = os.environ.get("NETEM_BRANDING_DIR")
if not BRANDING_DIR and (RUNTIME_DIR / "branding" / "branding.json").is_file():
    BRANDING_DIR = str(RUNTIME_DIR / "branding")
branding.init_app(app, BRANDING_DIR)

def _session_secret():
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    path = RUNTIME_DIR / "session.secret"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_text().strip()
    with os.fdopen(fd, "w") as stream:
        value = secrets.token_urlsafe(48)
        stream.write(value)
    return value


app.secret_key = _session_secret()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")


@app.context_processor
def integration_form_token():
    session.setdefault("integration_csrf", secrets.token_urlsafe(32))
    return {"integration_csrf": session["integration_csrf"]}


@app.before_request
def protect_integration_forms():
    if request.method == "POST" and request.endpoint in (
        "traffic_generator_save", "traffic_generator_test", "traffic_generator_start",
        "traffic_generator_adjust", "traffic_generator_stop", "traffic_generator_repair",
        "site_save", "site_apply_wan", "site_run", "site_stop",
    ):
        supplied = request.form.get("integration_csrf", "")
        expected = session.get("integration_csrf", "")
        if not supplied or not expected or not hmac.compare_digest(supplied.encode(), expected.encode()):
            abort(400, "Invalid form token. Reload the page and retry.")


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
    "site": None,
}
CONFIG_LOCK = threading.RLock()
SCENARIO_STOP = threading.Event()
# Set while an operator holds the running test in its current phase.
SCENARIO_PAUSE = threading.Event()
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
    "phases": [],
    "phase_index": None,
    "phase": None,
    "planned_s": None,
    "paused": False,
    "paused_at": None,
    "paused_total_s": 0.0,
    "phase_started_s": None,
    # The simulator run a test started, while it should be running.
    "workload_run_id": None,
    # Monotonic origins: wall-clock steps (NTP corrections) must not bend test timing.
    "clock_start": None,
    "paused_clock": None,
}
ORIGINAL_MTUS = {}
CAPTURE_PROCESS = None
BACKGROUND_STOP = threading.Event()
TELEMETRY_THREAD = None
PROBE_THREAD = None
TELEMETRY_PREVIOUS = {}
TELEMETRY_SAMPLER_ID = uuid.uuid4().hex
# Identifies this process, so the update screen can tell when the restarted service answers.
PROCESS_INSTANCE = uuid.uuid4().hex
BOTTLENECK_COLUMNS = tuple(
    f"{direction}_{name}" for direction in ("down", "up")
    for name in ("util_pct", "queue_drops_ps", "injected_drops_ps", "drop_pct", "backlog_bytes")
)
NET_SYSFS = Path("/sys/class/net")
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
        "release_commit": "",
        "release_behind": 0,
        "release_reachable": False,
        "unreleased": 0,
        "update_available": False,
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

    # Release Please changes version.txt only in the commit that publishes a release, so the
    # latest commit touching it is the latest release. Commits merged after it wait for the next one.
    rc, release_commit, _ = run_process([GIT, "log", "-1", "--format=%h", remote_ref, "--", "version.txt"])
    if rc == 0 and release_commit:
        status["release_commit"] = release_commit
        rc, count, _ = run_process([GIT, "rev-list", "--count", f"HEAD..{release_commit}"])
        status["release_behind"] = int(count or 0) if rc == 0 else 0
        rc, count, _ = run_process([GIT, "rev-list", "--count", f"{release_commit}..{remote_ref}"])
        status["unreleased"] = int(count or 0) if rc == 0 else 0
        rc, _out, _err = run_process([GIT, "merge-base", "--is-ancestor", "HEAD", release_commit])
        status["release_reachable"] = rc == 0
    remote, installed = version_tuple(status["remote_version"]), version_tuple(status["installed_version"])
    newer = remote > installed if remote and installed else status["release_behind"] > 0
    status["update_available"] = bool(newer and status["release_behind"] > 0)
    status["ok"] = True
    return status


def version_tuple(value):
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", str(value or "").strip())
    return tuple(int(part) for part in match.groups()) if match else None


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
    with CONFIG_LOCK:
        fd, name = tempfile.mkstemp(prefix="config.", dir=CONFIG_PATH.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(cfg, stream, indent=2)
            os.replace(name, CONFIG_PATH)
        finally:
            if os.path.exists(name):
                os.unlink(name)

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
    fd, name = tempfile.mkstemp(prefix="secrets.", dir=SECRETS_PATH.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(secrets_data, stream, indent=2)
        os.replace(name, SECRETS_PATH)
    finally:
        if os.path.exists(name):
            os.unlink(name)
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
    integration = traffic_generator_config(cfg)
    key = traffic_generator_api_key()
    if not integration.get("host"):
        raise RuntimeError("Traffic Simulator is not configured.")
    if not key:
        raise RuntimeError("Traffic Simulator API key is not configured.")
    if not path.startswith("/api/v1/"):
        raise RuntimeError("Invalid Traffic Simulator API path.")
    allow_self_signed = bool(integration.get("allow_self_signed", True))
    context = ssl._create_unverified_context() if allow_self_signed else ssl.create_default_context()
    connection = None
    try:
        connection = http.client.HTTPSConnection(
            integration["host"], int(integration.get("port") or 8443),
            timeout=timeout, context=context,
        )
        # Verify/pin before putting the Bearer key on the wire. A direct
        # connection also avoids environment proxies and credential redirects.
        connection.connect()
        fingerprint = hashlib.sha256(connection.sock.getpeercert(binary_form=True)).hexdigest()
        expected = str(integration.get("tls_sha256") or "").lower().replace(":", "")
        if expected and not hmac.compare_digest(expected.encode(), fingerprint.encode()):
            raise RuntimeError("Traffic Simulator TLS certificate fingerprint changed. Verify and update the trusted fingerprint in Integrations.")
        if allow_self_signed and not expected:
            with CONFIG_LOCK:
                current = load_config()
                current_integration = traffic_generator_config(current)
                if (current_integration.get("host"), current_integration.get("port", 8443)) != (integration.get("host"), integration.get("port", 8443)):
                    raise RuntimeError("Traffic Simulator configuration changed during connection. Retry.")
                concurrent_pin = current_integration.get("tls_sha256")
                if concurrent_pin and concurrent_pin != fingerprint:
                    raise RuntimeError("Traffic Simulator TLS certificate fingerprint changed during connection.")
                current["traffic_generator"] = dict(current_integration, tls_sha256=fingerprint)
                save_config(current)
        headers = {"Accept": "application/json", "Authorization": f"Bearer {key}",
                   "User-Agent": f"NetEm-WAN-Lab/{get_app_version()}"}
        body = None
        if payload is not None:
            body = json.dumps(payload, allow_nan=False).encode()
            headers["Content-Type"] = "application/json"
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read(2 * 1024 * 1024 + 1)
        if not 200 <= response.status < 300:
            # Redirects are never followed with the credential.
            detail = raw[:8192].decode("utf-8", "replace").replace(key, "[redacted]")
            raise RuntimeError(f"Traffic Simulator returned HTTP {response.status}: {detail[:300]}")
        if len(raw) > 2 * 1024 * 1024:
            raise RuntimeError("Traffic Simulator response exceeds 2 MiB.")
        result = json.loads(raw.decode("utf-8"))
        if not isinstance(result, dict):
            raise RuntimeError("Traffic Simulator returned an invalid JSON object.")
        if path == "/api/v1/status":
            dem = result.get("dem")
            metrics = ("experience_score", "availability_pct", "p50_ms", "p95_ms", "requests_per_second", "failures_per_second")
            if (result.get("status") not in ("idle", "starting", "running", "stopping", "stopped", "interrupted", "failed")
                    or not isinstance(dem, dict) or not set(metrics) <= dem.keys()
                    or not isinstance(result.get("users"), int)
                    or any(dem[name] is not None and (not isinstance(dem[name], (int, float)) or not math.isfinite(dem[name])) for name in metrics)):
                raise RuntimeError("Traffic Simulator returned an invalid status payload.")
        return result
    except (OSError, TimeoutError, ValueError, UnicodeError, http.client.HTTPException) as exc:
        raise RuntimeError(f"Traffic Simulator connection failed: {exc}") from exc
    finally:
        if connection is not None:
            connection.close()


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


# ---------- Traffic path readiness ----------
#
# Simulated users only measure the WANs when their traffic reaches the controlled target
# through the appliance. The simulator checks and repairs that path; NetEm asks before it
# starts traffic, repairs what the simulator can repair, and shows the result.

TRAFFIC_PATH_CACHE = {"checked": None, "payload": None}
TRAFFIC_PATH_CACHE_SECONDS = 15
REPAIR_WAIT_SECONDS = 30


def legacy_traffic_path():
    """Simulators without the readiness API: judge the selected appliance route from the network state."""
    network = traffic_generator_request("/api/v1/network", timeout=8.0)
    selected = network.get("selected") or None
    health = network.get("route_health") or {}
    match = next((row for row in network.get("appliances") or []
                  if selected and all(row.get(key) == selected.get(key) for key in ("interface", "gateway", "target"))), None)
    if not selected:
        return {"ready": None, "repairable": False, "legacy": True, "job": network.get("job") or {},
                "message": "No appliance route is selected on the Traffic Simulator. This simulator version cannot tell "
                           "whether its traffic bypasses the appliance; update it."}
    ready = bool(health.get("active"))
    return {"ready": ready, "repairable": not ready and match is not None, "legacy": True,
            "appliance_id": (match or {}).get("id"), "job": network.get("job") or {},
            "path": {key: selected.get(key) for key in ("interface", "gateway", "target", "source")},
            "message": (f"Traffic to {selected.get('target')} goes through the appliance at {selected.get('gateway')} on "
                        f"{selected.get('interface')}." if ready else health.get("message") or "The selected appliance route is not active.")}


def traffic_path_summary(path):
    if not path.get("available"):
        return "Traffic path unknown: " + (path.get("message") or "the Traffic Simulator did not answer.")
    if path.get("ready"):
        route = path.get("path") or {}
        target = path.get("target") or {}
        return (f"Traffic path ready: via {route.get('gateway')} on {route.get('interface')}"
                + (" · target answers" if target.get("ok") else "") + ".")
    if path.get("ready") is None:
        return path.get("message") or "Traffic path not checked."
    return "Traffic path not ready: " + (path.get("message") or "simulated traffic would not cross the appliance.")


def traffic_path_readiness(max_age=TRAFFIC_PATH_CACHE_SECONDS):
    """Whether the simulator's traffic reaches the target through the appliance (cached briefly)."""
    cached = dict(TRAFFIC_PATH_CACHE)
    if max_age and cached["payload"] is not None and cached["checked"] is not None and time.monotonic() - cached["checked"] <= max_age:
        return cached["payload"]
    if not (traffic_generator_config(load_config()).get("host") and traffic_generator_api_key()):
        payload = {"configured": False, "available": False, "ready": None, "repairable": False,
                   "message": "Traffic Simulator is not configured."}
    else:
        try:
            payload = dict(traffic_generator_request("/api/v1/network/readiness", timeout=8.0), legacy=False)
        except RuntimeError as exc:
            payload = None
            if "HTTP 404" in str(exc):
                try:
                    payload = legacy_traffic_path()
                except RuntimeError as inner:
                    exc = inner
            if payload is None:
                payload = {"available": False, "ready": None, "repairable": False, "message": str(exc)}
        payload.setdefault("available", True)
        payload["configured"] = True
    payload["summary"] = traffic_path_summary(payload)
    payload["checked_at"] = time.time()
    TRAFFIC_PATH_CACHE.update(checked=time.monotonic(), payload=payload)
    return payload


def repair_traffic_path(wait=REPAIR_WAIT_SECONDS):
    """Ask the simulator to restore its traffic path, wait for its job and return the new readiness."""
    readiness = traffic_path_readiness(max_age=0)
    if readiness.get("ready"):
        return readiness
    if not readiness.get("repairable"):
        raise RuntimeError(readiness.get("message") or "The traffic path cannot be repaired automatically.")
    if readiness.get("legacy"):
        job = traffic_generator_request("/api/v1/network/select", method="POST",
                                        payload={"appliance_id": readiness["appliance_id"]}, timeout=8.0)
    else:
        job = traffic_generator_request("/api/v1/network/repair", method="POST", payload={}, timeout=8.0)
        if job.get("repair") == "not_needed":
            return traffic_path_readiness(max_age=0)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline and not SCENARIO_STOP.wait(2):
        readiness = traffic_path_readiness(max_age=0)
        state = readiness.get("job") or {}
        if state.get("id") == job.get("id") and state.get("state") in ("completed", "failed"):
            break
    return readiness


def ensure_traffic_path(context):
    """Before NetEm starts simulated users: make sure their traffic will cross the appliance, repairing it if the
    simulator can. Unknown readiness (older simulator, connection trouble) does not block: the start reports it."""
    readiness = traffic_path_readiness(max_age=0)
    if readiness.get("ready") is not False or not readiness.get("available"):
        return readiness
    if readiness.get("repairable"):
        log_event("traffic-generator", f"{context}: traffic path not ready, repairing · {readiness.get('message')}", action="repair")
        readiness = repair_traffic_path()
        if readiness.get("ready"):
            log_event("traffic-generator", f"{context}: traffic path repaired", action="repair")
            return readiness
    raise RuntimeError("Traffic path not ready: " + (readiness.get("message") or "simulated traffic would not cross the appliance."))


def workload_finding(status, signals, expected_run=None):
    """When the simulator's traffic and what NetEm measures cannot both be right."""
    common = {"source": "platform", "severity": "bad", "wans": [], "unattributed": 0, "candidates": []}
    run = (status or {}).get("run") or {}
    if expected_run and ((status or {}).get("status") not in ("starting", "running") or run.get("run_id") != expected_run):
        reported = (status or {}).get("status") or "no status"
        return dict(common, id="workload_missing", title="The test's workload is not running on the Traffic Simulator",
                    detail=f"NetEm started workload {expected_run} for this test, but the simulator reports {reported}"
                           + (f" with run {run['run_id']}" if run.get("run_id") else "")
                           + ". It may have restarted, or NetEm is connected to a different simulator.",
                    hint="Check Settings → Integrations and the simulator's service log.")
    dem = (status or {}).get("dem") or {}
    measured = [sum((item.get("directions") or {}).get(direction, {}).get("rate_mbps") or 0 for direction in ("down", "up"))
                for item in signals if any((item.get("directions") or {}).get(direction, {}).get("rate_mbps") is not None
                                           for direction in ("down", "up"))]
    if (status or {}).get("status") == "running" and (dem.get("requests") or 0) >= 20 and measured and max(measured) < 0.05:
        return dict(common, id="workload_bypass", title="Simulated traffic is not crossing NetEm",
                    detail=f"The Traffic Simulator completed {dem['requests']} transactions in the last {dem.get('window_seconds') or 60} s, "
                           "but no WAN carried traffic: its route to the target bypasses the appliance and NetEm.",
                    hint="Check the traffic path on the Command Center and repair it.")
    return None


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

        deadline = time.monotonic() + max(0.25, min(3.0, float(timeout)))
        while time.monotonic() < deadline:
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
            if not isinstance(payload, dict) or payload.get("service") != "netem-traffic-simulator":
                continue
            if payload.get("protocol") != TRAFFIC_GENERATOR_DISCOVERY_MAGIC:
                continue
            if payload.get("nonce") != nonce:
                continue
            port = payload.get("api_port", 8443)
            if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
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

# Built-in tests run about 3-5 minutes in named phases, long enough for SD-WAN health
# checks to react and for experience measurements to settle; length is adjustable at start.
DEFAULT_SCENARIOS = [
    {
        "id": "progressive_brownout",
        "name": "Progressive brownout",
        "description": "Gradually degrades one WAN, holds it in a poor state, then restores it.",
        "steps": [
            {"after": 0, "action": "quality", "value": 100, "label": "Nominal", "phase": "Baseline"},
            {"after": 60, "action": "quality", "value": 80, "label": "Minor degradation", "phase": "Degradation"},
            {"after": 40, "action": "quality", "value": 60, "label": "Noticeable degradation", "phase": "Degradation"},
            {"after": 40, "action": "quality", "value": 40, "label": "Severe brownout", "phase": "Severe brownout"},
            {"after": 60, "action": "quality", "value": 70, "label": "Partial recovery", "phase": "Recovery"},
            {"after": 30, "action": "quality", "value": 100, "label": "Recovered", "phase": "Recovery"},
            {"after": 30, "action": "phase", "label": "Settled", "phase": "Recovery"},
        ],
    },
    {
        "id": "sla_failover",
        "name": "SLA failover",
        "description": "Starts healthy, blackholes the WAN while link state remains up, then restores it.",
        "steps": [
            {"after": 0, "action": "quality", "value": 100, "label": "Nominal", "phase": "Baseline"},
            {"after": 60, "action": "fault", "value": "blackhole", "label": "Blackhole", "phase": "Outage"},
            {
                "after": 0,
                "action": "assert",
                "condition": {"type": "sla", "state": "fail"},
                "timeout": 5,
                "label": "Expected SLA detects failure",
                "phase": "Outage",
            },
            {"after": 90, "action": "fault", "value": "normal", "label": "Connectivity restored", "phase": "Recovery"},
            {
                "after": 0,
                "action": "assert",
                "condition": {"type": "sla", "state": "pass"},
                "timeout": 5,
                "label": "Expected SLA recovers",
                "phase": "Recovery",
            },
            {"after": 60, "action": "phase", "label": "Settled", "phase": "Recovery"},
        ],
    },
    {
        "id": "flaky_underlay",
        "name": "Flaky underlay",
        "description": "Alternates between healthy and one-way failure to exercise SLA hysteresis.",
        "steps": [
            {"after": 0, "action": "quality", "value": 100, "label": "Nominal", "phase": "Baseline"},
            {"after": 60, "action": "fault", "value": "downstream_blackhole", "label": "Downstream failure", "phase": "Flapping"},
            {"after": 20, "action": "fault", "value": "normal", "label": "Recovered", "phase": "Flapping"},
            {"after": 20, "action": "fault", "value": "upstream_blackhole", "label": "Upstream failure", "phase": "Flapping"},
            {"after": 20, "action": "fault", "value": "normal", "label": "Recovered", "phase": "Flapping"},
            {"after": 20, "action": "fault", "value": "downstream_blackhole", "label": "Downstream failure", "phase": "Flapping"},
            {"after": 20, "action": "fault", "value": "normal", "label": "Recovered", "phase": "Flapping"},
            {"after": 20, "action": "fault", "value": "upstream_blackhole", "label": "Upstream failure", "phase": "Flapping"},
            {"after": 20, "action": "fault", "value": "normal", "label": "Recovered", "phase": "Recovery"},
            {"after": 45, "action": "phase", "label": "Settled", "phase": "Recovery"},
        ],
    },
    {
        "id": "availability_stress",
        "name": "Availability stress / DDoS impact",
        "description": "Safely emulates the WAN impact of a saturation event without generating attack traffic.",
        "steps": [
            {"after": 0, "action": "quality", "value": 100, "label": "Nominal", "phase": "Baseline"},
            {"after": 60, "action": "quality", "value": 60, "label": "Congestion begins", "phase": "Congestion"},
            {"after": 45, "action": "quality", "value": 30, "label": "Heavy saturation impact", "phase": "Saturation"},
            {"after": 45, "action": "quality", "value": 10, "label": "Severe availability impact", "phase": "Saturation"},
            {"after": 45, "action": "quality", "value": 70, "label": "Attack subsides", "phase": "Recovery"},
            {"after": 30, "action": "quality", "value": 100, "label": "Recovered", "phase": "Recovery"},
            {"after": 30, "action": "phase", "label": "Settled", "phase": "Recovery"},
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
    for item in scenarios:
        phases, item["planned_s"] = scenario_phases(item["steps"])
        item["phases"] = [phase["name"] for phase in phases]
    return scenarios


def validate_condition(raw, step_index):
    if not isinstance(raw, dict):
        raise ValueError(f"Step {step_index}: condition must be an object.")

    condition_type = str(raw.get("type") or "").strip().lower()
    if condition_type not in ("sla", "probe", "traffic", "dem", "steering"):
        raise ValueError(
            f"Step {step_index}: condition type must be sla, probe, traffic, dem or steering."
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
        if not math.isfinite(value):
            raise ValueError(f"Step {step_index}: comparison must be finite.")
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
            "interactive_p95_ms",
            "realtime_availability_pct",
            "interactive_availability_pct",
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
        if not math.isfinite(value):
            raise ValueError(f"Step {step_index}: comparison must be finite.")
        try:
            window = int(raw.get("window", 60))
        except (TypeError, ValueError, OverflowError):
            raise ValueError(f"Step {step_index}: DEM window must be an integer.") from None
        condition.update(
            {
                "field": field,
                "op": op,
                "value": value,
                "window": max(10, min(3600, window)),
            }
        )

    elif condition_type == "steering":
        # Passes once the SD-WAN appliance keeps this traffic class off impaired WANs.
        traffic_class = str(raw.get("class") or "realtime").strip().lower()
        if traffic_class not in ("realtime", "interactive", "bulk"):
            raise ValueError(f"Step {step_index}: steering class must be realtime, interactive or bulk.")
        condition["class"] = traffic_class
        if "within" in raw:
            try:
                within = float(raw["within"])
                if not math.isfinite(within) or not 1 <= within <= 600:
                    raise ValueError()
            except (TypeError, ValueError):
                raise ValueError(f"Step {step_index}: steering within must be 1-600 seconds.") from None
            condition["within"] = within

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
        if action not in (
            "quality", "fault", "mtu", "traffic_generator", "wait", "assert", "phase"
        ):
            raise ValueError(
                f"Step {index}: action must be quality, fault, mtu, "
                "traffic_generator, wait, assert or phase."
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
        if step.get("phase"):
            validated_step["phase"] = str(step["phase"]).strip()[:40]

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

        elif action == "traffic_generator":
            value = step.get("value")
            if not isinstance(value, dict):
                raise ValueError(
                    f"Step {index}: traffic_generator value must be an object."
                )
            operation = str(value.get("operation") or "").strip().lower()
            if operation not in ("start", "adjust", "stop"):
                raise ValueError(
                    f"Step {index}: traffic_generator operation must be "
                    "start, adjust or stop."
                )
            if operation == "adjust" and set(value) - {"operation", "users", "spawn_rate", "activity", "personas", "applications", "media_mode"}:
                raise ValueError(f"Step {index}: adjust supports users, spawn_rate, activity, personas, applications and media_mode only.")
            cleaned = {"operation": operation}

            if operation in ("start", "adjust"):
                if "users" in value:
                    try:
                        cleaned["users"] = int(value["users"])
                        if not 1 <= cleaned["users"] <= 5000 or float(value["users"]) != cleaned["users"]:
                            raise ValueError()
                    except (TypeError, ValueError):
                        raise ValueError(
                            f"Step {index}: traffic-generator users must be an integer."
                        )
                if "spawn_rate" in value:
                    try:
                        cleaned["spawn_rate"] = float(value["spawn_rate"])
                        if not math.isfinite(cleaned["spawn_rate"]) or not 0.1 <= cleaned["spawn_rate"] <= 1000:
                            raise ValueError()
                    except (TypeError, ValueError):
                        raise ValueError(
                            f"Step {index}: traffic-generator spawn_rate must be numeric."
                        )
                for key in ("profile", "activity", "pattern"):
                    if key in value:
                        cleaned[key] = str(value[key]).strip()[:80]
                if "media_mode" in value:
                    if value["media_mode"] not in ("strict", "realistic"):
                        raise ValueError(f"Step {index}: media_mode must be strict or realistic.")
                    cleaned["media_mode"] = value["media_mode"]
                if operation == "start" and value.get("label"):
                    cleaned["label"] = str(value["label"]).strip()[:120]
                if "target" in value:
                    target = str(value["target"]).strip()
                    parsed = urlsplit(target)
                    if parsed.scheme not in ("http", "https") or not parsed.hostname:
                        raise ValueError(
                            f"Step {index}: traffic-generator target must be an HTTP(S) URL."
                        )
                    cleaned["target"] = target[:512]
                for key in ("personas", "applications"):
                    if key in value:
                        if not isinstance(value[key], dict):
                            raise ValueError(
                                f"Step {index}: {key} must be an object."
                            )
                        try:
                            weights = {str(name)[:80]: float(weight) for name, weight in value[key].items()}
                            if len(weights) > 30 or not weights or any(not math.isfinite(weight) or weight < 0 for weight in weights.values()) or sum(weights.values()) <= 0:
                                raise ValueError()
                        except (TypeError, ValueError, OverflowError):
                            raise ValueError(f"Step {index}: {key} needs finite nonnegative weights with a positive total.") from None
                        cleaned[key] = weights

            if operation == "start":
                cleaned.setdefault("profile", "office")
                cleaned.setdefault("users", 50)
                cleaned.setdefault("spawn_rate", 5.0)
                cleaned.setdefault("activity", "normal")
                cleaned.setdefault("pattern", "steady")

            validated_step["value"] = cleaned

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
        "source": "impairment_model",
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

    lines = raw.splitlines()
    root = next((line for line in lines if " root " in line), lines[0])
    kind = re.search(r"qdisc\s+(\S+)\s+[0-9a-fA-F]+:", root)
    if not kind:
        return info
    info["parsed"]["kind"] = kind.group(1)
    netem = next((line for line in lines if re.match(r"qdisc\s+netem\s", line)), "")
    if netem:
        info["parsed"]["kind"] = "netem"
        # tc prints us for sub-ms values and seconds for larger delays.
        delay = re.search(r"delay\s+([0-9.]+)(us|ms|s)(?:\s+([0-9.]+)(us|ms|s))?", netem)
        if delay:
            units = {"us": 0.001, "ms": 1, "s": 1000}
            info["parsed"]["delay_ms"] = float(delay.group(1)) * units[delay.group(2)]
            info["parsed"]["jitter_ms"] = (float(delay.group(3)) * units[delay.group(4)]
                                             if delay.group(3) else 0.0)
        else:
            info["parsed"]["delay_ms"] = info["parsed"]["jitter_ms"] = 0.0
        loss = re.search(r"loss(?:\s+random)?\s+([0-9.]+)%", netem)
        info["parsed"]["loss_pct"] = float(loss.group(1)) if loss else 0.0

    for line in lines:
        rate = re.search(r"\btbf\b.*?\brate\s+([0-9.]+)([kKmMgG]?)bit", line)
        if rate:
            scale = {"": 0.000001, "k": 0.001, "m": 1, "g": 1000}
            info["parsed"]["rate_mbit"] = float(rate.group(1)) * scale[rate.group(2).lower()]
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


SHAPER_QUEUE_MS = 50
SHAPER_BURST_MS = 1
UNSHAPED_SIZING_MBIT = 1000


def shaper_queue_sizes(rate_mbit: float, delay_ms: float, jitter_ms: float):
    """Queue sizes that scale with the configured rate instead of fixed bytes.

    A fixed 32 KB rate-limiter queue held under 3 ms at 100 Mbit/s, so TCP's
    window bursts overflowed it on an otherwise idle link and every transfer paid
    for retransmissions. Like a real access link, the queue now holds about
    50 ms of data. netem also keeps every packet for its delay, so its packet
    queue must hold rate × (delay + jitter) or it drops on long-delay links.
    """
    bytes_per_ms = (rate_mbit if rate_mbit and rate_mbit > 0 else UNSHAPED_SIZING_MBIT) * 125
    hold_ms = max(0.0, float(delay_ms or 0)) + 3 * max(0.0, float(jitter_ms or 0))
    netem_limit = max(1000, math.ceil(bytes_per_ms * hold_ms * 1.5 / 1000))
    burst = max(3200, int(bytes_per_ms * SHAPER_BURST_MS))
    limit = max(65536, int(bytes_per_ms * SHAPER_QUEUE_MS))
    return netem_limit, burst, limit


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

    netem_limit, burst_bytes, limit_bytes = shaper_queue_sizes(rate_mbit, delay_ms, jitter_ms)
    parts = ["netem", f"limit {netem_limit}"]
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
            f"rate {rate_str} buffer {burst_bytes} limit {limit_bytes}"
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
        "rx_bytes": None,
        "tx_bytes": None,
        "rx_packets": None,
        "tx_packets": None,
        "rx_dropped": None,
        "tx_dropped": None,
        "rx_errors": None,
        "tx_errors": None,
    }
    if not ifname:
        return result

    stats_dir = NET_SYSFS / ifname / "statistics"
    for key in result:
        try:
            result[key] = int((stats_dir / key).read_text().strip())
        except (OSError, ValueError):
            result[key] = None
    return result


def interface_runtime_status(ifname: str):
    if not ifname:
        return {
            "available": False,
            "operstate": "unknown",
            "carrier": None,
            "ifindex": None,
        }

    base = NET_SYSFS / ifname
    available = base.exists()
    operstate = "unknown"
    carrier = None
    ifindex = None

    if available:
        try:
            operstate = (base / "operstate").read_text().strip() or "unknown"
        except OSError:
            pass
        try:
            carrier = (base / "carrier").read_text().strip() == "1"
        except OSError:
            carrier = None

        try:
            ifindex = int((base / "ifindex").read_text().strip())
        except (OSError, ValueError):
            pass

    return {
        "available": available,
        "operstate": operstate,
        "carrier": carrier,
        "ifindex": ifindex,
    }




def traffic_snapshot(link: dict):
    """Read directional counters and identify the devices behind a link mapping."""
    sides = {}
    identities = []
    valid = True
    for side in ("inner", "outer"):
        ifname = link.get(side)
        before = interface_runtime_status(ifname)
        counters = interface_counters(ifname)
        status = interface_runtime_status(ifname)
        readable = (
            status["available"] and status["ifindex"] is not None
            and before["ifindex"] == status["ifindex"]
            and all(counters[key] is not None and counters[key] >= 0
                    for key in ("tx_bytes", "tx_packets"))
        )
        valid = valid and readable
        sides[side] = {"interface": ifname, "counters": counters,
                       **status, "counters_valid": bool(readable)}
        identities.append((ifname, status["ifindex"]))
    return {
        **sides,
        "identity": tuple(identities),
        "valid": bool(valid),
        "monotonic_timestamp": time.monotonic(),
        "timestamp": time.time(),
        "down_bytes": sides["inner"]["counters"]["tx_bytes"],
        "up_bytes": sides["outer"]["counters"]["tx_bytes"],
        "down_packets": sides["inner"]["counters"]["tx_packets"],
        "up_packets": sides["outer"]["counters"]["tx_packets"],
    }


def traffic_rates(current: dict, previous: dict | None):
    """Unknown intervals need a new baseline, never a manufactured idle rate."""
    keys = ("down_bytes", "up_bytes", "down_packets", "up_packets")
    if (not previous or not current["valid"] or not previous["valid"]
            or current["identity"] != previous["identity"]):
        return None
    dt = current["monotonic_timestamp"] - previous["monotonic_timestamp"]
    if dt <= 0 or any(current[key] < previous[key] for key in keys):
        return None
    deltas = [(current[key] - previous[key]) / dt for key in keys]
    return (deltas[0] * 8 / 1_000_000, deltas[1] * 8 / 1_000_000,
            deltas[2], deltas[3])


def parse_qdisc_stats(raw: str):
    """Parse `tc -s qdisc show dev <if>` into one counter dict per qdisc."""
    qdiscs = []
    current = None
    scale = {"": 1, "k": 1024, "m": 1024 * 1024, "g": 1024 ** 3}
    for line in (raw or "").splitlines():
        head = re.match(r"\s*qdisc\s+(\S+)\s+([0-9a-fA-F]+:)", line)
        if head:
            current = {"kind": head.group(1), "handle": head.group(2), "root": " root " in f" {line} ",
                       "bytes": None, "packets": None, "drops": None, "overlimits": None,
                       "backlog_bytes": None, "backlog_packets": None}
            qdiscs.append(current)
            continue
        if current is None:
            continue
        sent = re.search(r"Sent\s+(\d+)\s+bytes\s+(\d+)\s+pkt\s+\(dropped\s+(\d+),\s+overlimits\s+(\d+)", line)
        if sent:
            current.update(bytes=int(sent.group(1)), packets=int(sent.group(2)),
                           drops=int(sent.group(3)), overlimits=int(sent.group(4)))
        backlog = re.search(r"backlog\s+([0-9.]+)([KkMmGg]?)b\s+(\d+)p", line)
        if backlog:
            current.update(backlog_bytes=int(float(backlog.group(1)) * scale[backlog.group(2).lower()]),
                           backlog_packets=int(backlog.group(3)))
    return qdiscs


def qdisc_counters(ifname: str):
    """Drop counters for one direction: the root qdisc plus its rate limiter, if any."""
    if not ifname:
        return None
    rc, out, _err = run_cmd(f"{TC} -s qdisc show dev {ifname}")
    if rc != 0:
        return None
    qdiscs = parse_qdisc_stats(out)
    root = next((item for item in qdiscs if item["root"]), None)
    if not root or root["packets"] is None or root["drops"] is None:
        return None
    netem = next((item for item in qdiscs if item["kind"] == "netem"), None)
    tbf = next((item for item in qdiscs if item["kind"] == "tbf"), None)
    return {
        "kind": root["kind"],
        "packets": root["packets"],
        "netem_drops": (netem or {}).get("drops") or 0,
        "queue_drops": (tbf or {}).get("drops") or 0,
        "backlog_bytes": sum(item["backlog_bytes"] or 0 for item in qdiscs),
        "shaped": tbf is not None,
    }


def qdisc_rates(current, previous, dt):
    """Per-second drops for one direction, or None across a reset or unknown read.

    The rate limiter (tbf) drops only when its small queue is full. netem's own
    counter adds its injected random loss to the overflow handed back by tbf.
    """
    if not current or not previous or dt <= 0 or current["kind"] != previous["kind"]:
        return None
    sent = current["packets"] - previous["packets"]
    netem_drops = current["netem_drops"] - previous["netem_drops"]
    queue_drops = current["queue_drops"] - previous["queue_drops"]
    if min(sent, netem_drops, queue_drops) < 0:
        return None
    dropped = max(netem_drops, queue_drops)
    offered = sent + dropped
    return {
        "queue_drops_ps": queue_drops / dt,
        "injected_drops_ps": max(0, netem_drops - queue_drops) / dt,
        "drop_pct": dropped * 100.0 / offered if offered else 0.0,
        "backlog_bytes": current["backlog_bytes"],
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
                session_id TEXT,
                rate_valid INTEGER NOT NULL DEFAULT 1
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

        # Serialize the migration across concurrent workers on first upgrade.
        conn.execute("BEGIN IMMEDIATE")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(telemetry_samples)")}
        if "rate_valid" not in columns:
            # Preserve existing history; its original sampler did not record validity.
            conn.execute("ALTER TABLE telemetry_samples ADD COLUMN rate_valid INTEGER NOT NULL DEFAULT 1")
        for name in BOTTLENECK_COLUMNS:
            if name not in columns:
                # Older samples have no bottleneck measurements; NULL means unknown, not zero.
                conn.execute(f"ALTER TABLE telemetry_samples ADD COLUMN {name} REAL")


def active_session_id():
    with RUNTIME_LOCK:
        return ACTIVE_SESSION.get("id") if ACTIVE_SESSION.get("active") else None


def bottleneck_values(current: dict, previous: dict | None, rates, effective: dict, fault: str):
    """Utilization of each shaped direction and its drops since the previous sample."""
    values = dict.fromkeys(BOTTLENECK_COLUMNS)
    dt = current["monotonic_timestamp"] - previous["monotonic_timestamp"] if previous else 0
    for direction, index, limit_key in (("down", 0, "download_mbit"), ("up", 1, "upload_mbit")):
        counters = (current.get("qdisc") or {}).get(direction)
        before = ((previous or {}).get("qdisc") or {}).get(direction)
        drops = qdisc_rates(counters, before, dt)
        if drops:
            for key, value in drops.items():
                values[f"{direction}_{key}"] = value
        limit = float(effective.get(limit_key) or 0)
        # Utilization only means something while the rate limiter is in place.
        if rates is not None and fault == "normal" and limit > 0 and counters and counters["shaped"]:
            values[f"{direction}_util_pct"] = rates[index] * 100.0 / limit
    return values


def collect_telemetry_sample():
    cfg = load_config()
    states = {
        item["id"]: item
        for item in build_link_states(cfg)
    }
    rows = []
    active_links = set()

    for link in cfg.get("wan_links", []):
        link_id = link.get("id") or link.get("bridge")
        state = states.get(link_id)
        if not state:
            continue
        active_links.add(link_id)
        current = traffic_snapshot(link)
        current["qdisc"] = {"down": qdisc_counters(link.get("inner")), "up": qdisc_counters(link.get("outer"))}
        previous = TELEMETRY_PREVIOUS.get(link_id)
        rates = traffic_rates(current, previous)
        down_mbps, up_mbps, down_pps, up_pps = rates or (0.0, 0.0, 0.0, 0.0)
        TELEMETRY_PREVIOUS[link_id] = current
        now = current["timestamp"]
        effective = state.get("effective", {})
        row = {
            "timestamp": now,
            "link_id": link_id,
            "down_mbps": down_mbps,
            "up_mbps": up_mbps,
            "down_pps": down_pps,
            "up_pps": up_pps,
            "delay_ms": float(effective.get("delay_ms", 0.0)),
            "jitter_ms": float(effective.get("jitter_ms", 0.0)),
            "loss_pct": float(effective.get("loss_pct", 0.0)),
            "quality": float(state.get("runtime_quality", 100)),
            "sla_pass": 1 if state.get("sla", {}).get("pass") else 0,
            "fault": state.get("fault", "normal"),
            "session_id": active_session_id(),
            "rate_valid": int(rates is not None),
        }
        row.update(bottleneck_values(current, previous, rates, effective, row["fault"]))
        rows.append(row)

    for removed in set(TELEMETRY_PREVIOUS) - active_links:
        TELEMETRY_PREVIOUS.pop(removed, None)

    if rows:
        columns = list(rows[0])
        with telemetry_connect() as conn:
            conn.executemany(
                f"INSERT INTO telemetry_samples ({', '.join(columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)})",
                [tuple(row[name] for name in columns) for row in rows],
            )
    return rows


def prune_telemetry_history():
    cutoff = time.time() - TELEMETRY_RETENTION_HOURS * 3600
    with telemetry_connect() as conn:
        conn.execute("DELETE FROM telemetry_samples WHERE timestamp < ?", (cutoff,))
        conn.execute("DELETE FROM probe_samples WHERE timestamp < ?", (cutoff,))


# A wall clock that moves back further than this (an NTP correction of a clock that ran ahead)
# leaves samples dated in the future; they would hide every newer sample.
CLOCK_STEP_SECONDS = 5.0
CLOCK_STEPS = []


def discard_future_samples(now=None):
    """Delete samples dated after now: they were recorded while the clock ran ahead."""
    now = time.time() if now is None else now
    with telemetry_connect() as conn:
        removed = conn.execute("DELETE FROM telemetry_samples WHERE timestamp > ?", (now + 1,)).rowcount
        removed += conn.execute("DELETE FROM probe_samples WHERE timestamp > ?", (now + 1,)).rowcount
    return removed


def note_clock_step(step_s, removed):
    CLOCK_STEPS.append({"at": time.time(), "clock": time.monotonic(), "step_s": round(step_s, 1), "discarded": removed})
    del CLOCK_STEPS[:-10]
    log_event("telemetry", f"System clock moved {'back' if step_s < 0 else 'forward'} {abs(step_s):.0f} s"
              + (f"; discarded {removed} samples dated in the future" if removed else ""),
              step_s=round(step_s, 1), discarded=removed)


def telemetry_worker():
    init_telemetry_db()
    removed = discard_future_samples()
    if removed:
        note_clock_step(0.0, removed)
    next_prune = time.monotonic() + 300
    wall, clock = time.time(), time.monotonic()
    while not BACKGROUND_STOP.is_set():
        started = time.monotonic()
        try:
            # Wall time should advance with the monotonic clock; a difference is a clock step.
            step = (time.time() - wall) - (time.monotonic() - clock)
            wall, clock = time.time(), time.monotonic()
            if step <= -CLOCK_STEP_SECONDS:
                note_clock_step(step, discard_future_samples(wall))
            elif step >= CLOCK_STEP_SECONDS * 12:
                note_clock_step(step, 0)
            collect_telemetry_sample()
            if time.monotonic() >= next_prune:
                prune_telemetry_history()
                next_prune = time.monotonic() + 300
        except Exception as exc:
            # Telemetry persistence must never stop the control plane.
            log_event("telemetry", "Persistent telemetry sample failed", error=str(exc)[:240])
        elapsed = time.monotonic() - started
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
                AVG(CASE WHEN rate_valid = 1 THEN down_mbps END) AS down_mbps,
                AVG(CASE WHEN rate_valid = 1 THEN up_mbps END) AS up_mbps,
                AVG(CASE WHEN rate_valid = 1 THEN down_pps END) AS down_pps,
                AVG(CASE WHEN rate_valid = 1 THEN up_pps END) AS up_pps,
                AVG(delay_ms) AS delay_ms,
                AVG(jitter_ms) AS jitter_ms,
                AVG(loss_pct) AS loss_pct,
                AVG(quality) AS quality,
                MIN(sla_pass) AS sla_pass
            FROM telemetry_samples
            WHERE link_id = ? AND timestamp >= ? AND timestamp <= ?
            GROUP BY CAST((timestamp - ?) / ? AS INTEGER)
            ORDER BY timestamp ASC
            """,
            (link_id, since, now + 1, since, bucket_seconds),
        ).fetchall()
    return [dict(row) for row in rows]


def latest_telemetry_sample(link_id: str):
    # A plain read: schema setup takes a write lock and the telemetry worker already did it.
    try:
        with telemetry_connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM telemetry_samples
                WHERE link_id = ? AND timestamp <= ?
                ORDER BY timestamp DESC LIMIT 1
                """,
                (link_id, time.time() + 1),
            ).fetchone()
    except sqlite3.OperationalError:
        # No telemetry table until the worker's first start.
        return None
    if not row:
        return None
    sample = dict(row)
    if not sample["rate_valid"]:
        for key in ("down_mbps", "up_mbps", "down_pps", "up_pps"):
            sample[key] = None
    return sample


# ---------- Live bottleneck diagnosis ----------
#
# NetEm measures each WAN's path (utilization against the shaped rate, rate-limiter
# queue drops, injected loss and delay). The Traffic Simulator reports what its
# users experienced and the appliance egress address the target saw for each
# transaction. Mapping those addresses to WANs connects each symptom to the WAN
# that carried it and to the path condition that explains it.

DIAGNOSIS_WINDOW_SECONDS = 12
DIAGNOSIS_INTERVAL_SECONDS = 3.0
DIAGNOSIS_CLEAR_SECONDS = 30
SATURATION_PCT = 90.0
EGRESS_LEARN_INTERVAL_SECONDS = 60
DIAGNOSIS_LOCK = threading.Lock()
# "checked" is monotonic: after the wall clock steps back, a wall-clock age turns negative
# and the cache would never refresh.
DIAGNOSIS_CACHE = {"timestamp": 0.0, "checked": None, "payload": None}
DIAGNOSIS_ACTIVE = {}
DIAGNOSIS_THREAD = None
EGRESS_LEARNED = {}
EGRESS_LEARN_STATE = {"running": False, "last_run": 0.0, "last_clock": None, "error": None, "target": None}
SEVERITY_ORDER = {"bad": 0, "warn": 1, "info": 2}
# Path conditions that can explain each simulator symptom.
SYMPTOM_CAUSES = {
    "media_loss": ("fault", "injected_loss", "queue_drops"),
    "media_no_reply": ("fault", "injected_loss", "queue_drops"),
    "timeouts": ("fault", "queue_drops", "injected_loss"),
    "connection_errors": ("fault",),
    "bandwidth_bound": ("saturated", "queue_drops"),
    # Saturation alone cannot: NetEm's shallow queue adds only milliseconds of delay.
    "slow_wait": ("injected_delay", "queue_drops", "injected_loss"),
}
# Added delay explains a slow first byte only when it is a meaningful share of the wait.
DELAY_SHARE = 0.25


def recent_telemetry_samples(link_id: str, seconds=DIAGNOSIS_WINDOW_SECONDS):
    try:
        with telemetry_connect() as conn:
            rows = conn.execute(
                "SELECT * FROM telemetry_samples WHERE link_id = ? AND timestamp >= ? AND timestamp <= ? ORDER BY timestamp",
                (link_id, time.time() - seconds, time.time() + 1),
            ).fetchall()
    except sqlite3.Error:
        return []
    return [dict(row) for row in rows]


def _mean(values):
    values = [float(value) for value in values if value is not None]
    return sum(values) / len(values) if values else None


def path_signals(state: dict, samples: list):
    """Summarize one WAN's recent path measurements and the conditions they show."""
    effective = state.get("effective") or {}
    fault = state.get("fault", "normal")
    signals = {
        "link_id": state["id"],
        "label": state.get("label") or state["id"],
        "fault": fault,
        "injected": {key: float(effective.get(key) or 0.0) for key in ("delay_ms", "jitter_ms", "loss_pct")},
        "directions": {},
        "causes": [],
    }
    if fault != "normal":
        signals["causes"].append({"kind": "fault", "direction": None, "severity": "bad",
                                  "text": f"{fault.replace('_', ' ')} active"})
    valid = [sample for sample in samples if sample.get("rate_valid")]
    for direction, limit_key, name in (("down", "download_mbit", "Download"), ("up", "upload_mbit", "Upload")):
        info = {
            "rate_mbps": _mean(sample.get(f"{direction}_mbps") for sample in valid),
            "limit_mbit": float(effective.get(limit_key) or 0) or None,
        }
        for key in ("util_pct", "queue_drops_ps", "injected_drops_ps", "drop_pct", "backlog_bytes"):
            info[key] = _mean(sample.get(f"{direction}_{key}") for sample in samples)
        info["queue_delay_ms"] = (info["backlog_bytes"] * 8 / (info["limit_mbit"] * 1000)
                                  if info["backlog_bytes"] is not None and info["limit_mbit"] else None)
        signals["directions"][direction] = info
        if info["util_pct"] is not None and info["util_pct"] >= SATURATION_PCT:
            signals["causes"].append({"kind": "saturated", "direction": direction, "severity": "warn",
                                      "text": f"{name} at {info['util_pct']:.0f}% of {info['limit_mbit']:g} Mbit/s"})
        if (info["queue_drops_ps"] or 0) >= 1:
            signals["causes"].append({
                "kind": "queue_drops", "direction": direction, "severity": "warn",
                "text": f"{name} queue full: {info['queue_drops_ps']:.0f} packets/s dropped ({info['drop_pct'] or 0:.1f}%)",
            })
    loss = signals["injected"]["loss_pct"]
    if loss > 0 and fault == "normal":
        dropped = signals["directions"]["down"]["injected_drops_ps"]
        signals["causes"].append({
            "kind": "injected_loss", "direction": "down", "severity": "warn",
            "text": f"{loss:g}% random loss injected on download" + (f" ({dropped:.1f} packets/s)" if dropped else ""),
        })
    delay, jitter = signals["injected"]["delay_ms"], signals["injected"]["jitter_ms"]
    if (delay > 0 or jitter > 0) and fault == "normal":
        signals["causes"].append({
            "kind": "injected_delay", "direction": "down", "severity": "info",
            "text": f"{delay:g} ms" + (f" ± {jitter:g} ms" if jitter else "") + " delay added to every round trip",
        })
    signals["full"] = sorted({cause["direction"] for cause in signals["causes"]
                              if cause["kind"] in ("saturated", "queue_drops")})
    # Health decides where a well-behaved SD-WAN appliance should steer traffic.
    sla = state.get("sla") or {}
    failing = [name for name, ok in (sla.get("checks") or {}).items() if not ok and name != "data_plane"]
    if fault != "normal":
        signals["health"], signals["health_reason"] = "failed", f"{fault.replace('_', ' ')} active"
    elif not sla.get("pass", True):
        signals["health"], signals["health_reason"] = "degraded", "Model SLA fails on " + ", ".join(failing or ["impairment"])
    elif signals["full"]:
        signals["health"] = "congested"
        signals["health_reason"] = "; ".join(cause["text"] for cause in signals["causes"]
                                             if cause["kind"] in ("saturated", "queue_drops"))
    else:
        signals["health"], signals["health_reason"] = "healthy", None
    return signals


def configured_egress(cfg: dict):
    entries = []
    for link in cfg.get("wan_links", []):
        for value in link.get("appliance_addresses") or []:
            try:
                entries.append((ipaddress.ip_network(str(value), strict=False), link.get("id") or link.get("bridge")))
            except ValueError:
                continue
    return entries


def resolve_egress(address: str, cfg: dict):
    """Map an appliance egress address to its WAN: entered addresses first, then learned ones."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return None
    for network, link_id in configured_egress(cfg):
        if ip.version == network.version and ip in network:
            return {"link_id": link_id, "source": "manual"}
    learned = EGRESS_LEARNED.get(str(ip))
    if not learned:
        return None
    links = sorted(learned["links"])
    if len(links) == 1:
        return {"link_id": links[0], "source": "learned"}
    # One address on several WANs: the appliance is not translating to per-WAN addresses.
    return {"link_id": None, "source": "ambiguous", "links": links}


def parse_tcpdump_sources(output: str):
    sources = set()
    for line in (output or "").splitlines():
        match = re.search(r"\bIP6?\s+(\S+)\.\d+\s+>\s", line)
        if match:
            try:
                sources.add(str(ipaddress.ip_address(match.group(1))))
            except ValueError:
                continue
    return sources


def capture_sources(interface: str, target_ip: str, seconds=3.0):
    """Briefly watch a WAN's upstream side for packets to the target; return their sources."""
    tcpdump = shutil.which("tcpdump")
    if not tcpdump or not interface:
        return set(), "tcpdump unavailable"
    try:
        proc = subprocess.Popen(
            [tcpdump, "-i", interface, "-nn", "-q", "-l", "-c", "50", "-s", "96", "dst", "host", target_ip],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
    except OSError as exc:
        return set(), str(exc)[:200]
    try:
        out, err = proc.communicate(timeout=seconds)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            out, err = proc.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
    sources = parse_tcpdump_sources(out)
    if not sources and proc.returncode not in (0, None, -15) and "listening on" not in (err or ""):
        return set(), (err or "tcpdump failed").strip().splitlines()[-1][:200]
    return sources, None


def learn_egress_addresses(cfg: dict, target_ip: str):
    found, errors = {}, []
    for link in cfg.get("wan_links", []):
        link_id = link.get("id") or link.get("bridge")
        sources, error = capture_sources(link.get("outer"), target_ip)
        if error:
            errors.append(f"{link_id}: {error}")
        for address in sources:
            found.setdefault(address, set()).add(link_id)
    now = time.time()
    with DIAGNOSIS_LOCK:
        for address, links in found.items():
            EGRESS_LEARNED[address] = {"links": links, "seen_at": now}
        EGRESS_LEARN_STATE.update(running=False, last_run=now, last_clock=time.monotonic(), target=target_ip,
                                  error="; ".join(errors)[:300] or None)


def maybe_learn_egress(cfg: dict, status, unmapped):
    """Learn unknown egress addresses in the background while a workload runs."""
    run = (status or {}).get("run") or {}
    if not unmapped or (status or {}).get("status") not in ("starting", "running") or not run.get("target"):
        return
    if not shutil.which("tcpdump"):
        return
    with DIAGNOSIS_LOCK:
        last = EGRESS_LEARN_STATE.get("last_clock")
        if EGRESS_LEARN_STATE["running"] or (last is not None and time.monotonic() - last < EGRESS_LEARN_INTERVAL_SECONDS):
            return
        EGRESS_LEARN_STATE["running"] = True
    try:
        host = urlsplit(str(run["target"])).hostname or ""
        target_ip = socket.getaddrinfo(host, None)[0][4][0]
    except (OSError, ValueError, IndexError) as exc:
        with DIAGNOSIS_LOCK:
            EGRESS_LEARN_STATE.update(running=False, last_run=time.time(), last_clock=time.monotonic(),
                                      error=f"Cannot resolve target: {exc}"[:200])
        return
    threading.Thread(target=learn_egress_addresses, args=(cfg, target_ip),
                     name="netem-egress-learn", daemon=True).start()


def _egress_count(value):
    if isinstance(value, (int, float)):
        return int(value)
    return int((value or {}).get("transfers") or (value or {}).get("requests") or 0)


def correlate_findings(dem_diagnosis, signals: list, cfg: dict):
    """Attach each simulator symptom to the WANs that carried it and the path conditions there."""
    by_link = {item["link_id"]: item for item in signals}
    findings, explained = [], set()
    for symptom in (dem_diagnosis or {}).get("findings") or []:
        relevant = SYMPTOM_CAUSES.get(symptom.get("id"), ())
        direction = {"download": "down", "upload": "up"}.get(symptom.get("direction"))
        waits = [value.get("p95_wait_ms") for value in (symptom.get("by_egress") or {}).values()
                 if isinstance(value, dict) and value.get("p95_wait_ms")]

        def explains(link, cause):
            if cause["kind"] not in relevant or (direction and cause["direction"] not in (None, direction)):
                return False
            if cause["kind"] == "injected_delay" and waits:
                added = link["injected"]["delay_ms"] + link["injected"]["jitter_ms"] + sum(
                    item.get("queue_delay_ms") or 0 for item in link["directions"].values())
                return added >= DELAY_SHARE * max(waits)
            return True

        def matching(link_id):
            link = by_link.get(link_id)
            return [cause for cause in link["causes"] if explains(link, cause)] if link else []

        per_link, unattributed = {}, 0
        for address, value in (symptom.get("by_egress") or {}).items():
            mapped = resolve_egress(address, cfg) if address != "unknown" else None
            if mapped and mapped.get("link_id") in by_link:
                per_link[mapped["link_id"]] = per_link.get(mapped["link_id"], 0) + _egress_count(value)
            else:
                unattributed += _egress_count(value)
        wans = []
        for link_id, affected in sorted(per_link.items(), key=lambda item: -item[1]):
            causes = matching(link_id)
            explained.update((link_id, cause["kind"], cause["direction"]) for cause in causes)
            # A full queue is the saturation's effect, so the saturation is explained with it.
            explained.update((link_id, "saturated", cause["direction"]) for cause in causes if cause["kind"] == "queue_drops")
            wans.append({"link_id": link_id, "label": by_link[link_id]["label"], "affected": affected,
                         "causes": [cause["text"] for cause in causes]})
        candidates = [item["label"] for item in signals if matching(item["link_id"])] if unattributed else []
        hint = None
        if symptom.get("id") == "http_errors":
            hint = "Returned by the appliance or the target, not caused by network quality."
        elif relevant and not any(wan["causes"] for wan in wans) and not candidates:
            hint = "NetEm's impairment does not explain this. Check the appliance (policy, inspection, CPU) or the target."
        findings.append({
            "source": "experience", "id": symptom.get("id"), "severity": symptom.get("severity", "warn"),
            "title": symptom.get("title", ""), "detail": symptom.get("detail", ""), "wans": wans,
            "unattributed": unattributed, "candidates": candidates, "hint": hint,
        })
    for item in signals:
        for cause in item["causes"]:
            if cause["kind"] == "injected_delay" or (item["link_id"], cause["kind"], cause["direction"]) in explained:
                continue
            findings.append({
                "source": "path", "id": cause["kind"],
                "severity": cause["severity"] if cause["kind"] == "fault" or dem_diagnosis is None else "info",
                "title": f"{item['label']}: {cause['text']}",
                "detail": ("No simulated-user impact traced to it in this window." if dem_diagnosis is not None
                           else "Path measurement only. Connect the Traffic Simulator to see the effect on users."),
                "wans": [{"link_id": item["link_id"], "label": item["label"], "affected": 0, "causes": [cause["text"]]}],
                "unattributed": 0, "candidates": [], "hint": None,
            })
    findings.sort(key=lambda finding: (SEVERITY_ORDER.get(finding["severity"], 9), finding["source"] != "experience"))
    return findings


def wan_experience(dem_diagnosis, signals: list, cfg: dict):
    """Simulated-user experience per WAN from the simulator's per-egress breakdown."""
    totals = {item["link_id"]: {"requests": 0, "failures": 0, "apps": {}} for item in signals}
    unattributed = {"requests": 0, "failures": 0}
    for address, item in ((dem_diagnosis or {}).get("egress") or {}).items():
        mapped = resolve_egress(address, cfg) if address != "unknown" else None
        bucket = totals.get((mapped or {}).get("link_id"))
        if bucket is None:
            unattributed["requests"] += item.get("requests") or 0
            unattributed["failures"] += item.get("failures") or 0
            continue
        bucket["requests"] += item.get("requests") or 0
        bucket["failures"] += item.get("failures") or 0
        for app_name, app in (item.get("applications") or {}).items():
            counts = bucket["apps"].setdefault(app_name, [0, 0])
            counts[0] += app.get("requests") or 0
            counts[1] += app.get("failures") or 0
    result = {}
    for link_id, bucket in totals.items():
        if not bucket["requests"]:
            result[link_id] = None
            continue
        failing = [(name, (total - failed) * 100.0 / total) for name, (total, failed) in bucket["apps"].items() if failed]
        worst = min(failing, key=lambda entry: entry[1]) if failing else None
        result[link_id] = {
            "requests": bucket["requests"],
            "availability_pct": round((bucket["requests"] - bucket["failures"]) * 100.0 / bucket["requests"], 2),
            "worst_app": worst[0] if worst else None,
            "worst_availability_pct": round(worst[1], 1) if worst else None,
        }
    return result, unattributed


def egress_mapping_finding(addresses: dict, learn_state: dict, tcpdump: bool):
    unmapped = sorted(address for address, mapped in addresses.items() if not mapped or not mapped.get("link_id"))
    if not unmapped:
        return None
    ambiguous = [address for address in unmapped if (addresses[address] or {}).get("source") == "ambiguous"]
    if ambiguous:
        detail = (f"{', '.join(ambiguous)} appears on several WANs, so the appliance is not translating traffic to "
                  "per-WAN addresses. Enable SNAT to each WAN interface address for per-WAN attribution.")
    elif not tcpdump:
        detail = ("Enter the appliance's WAN addresses under WAN links → a WAN → Diagnostics, or install tcpdump "
                  "and grant CAP_NET_RAW so NetEm can detect them.")
    elif learn_state.get("last_run") and not learn_state.get("error"):
        detail = ("NetEm did not see these addresses on any WAN. The upstream router may translate addresses "
                  "before the target; enter the appliance's WAN addresses manually.")
    else:
        detail = learn_state.get("error") or "Detecting which WAN uses these addresses…"
    return {
        "source": "mapping", "id": "egress_unmapped", "severity": "info",
        "title": f"Can't tell which WAN carried traffic from {', '.join(unmapped[:3])}" + ("…" if len(unmapped) > 3 else ""),
        "detail": detail, "wans": [], "unattributed": 0, "candidates": [], "hint": None,
    }


def track_diagnosis_events(findings: list, now=None):
    """Log when a problem appears and when it has been gone for a while."""
    now = now or time.monotonic()
    seen = set()
    for finding in findings:
        if finding["severity"] not in ("bad", "warn"):
            continue
        wans = [wan["label"] for wan in finding["wans"] if wan.get("affected") or finding["source"] == "path"]
        key = f"{finding['source']}:{finding['id']}:{','.join(sorted(wans))}"
        seen.add(key)
        if key in DIAGNOSIS_ACTIVE:
            DIAGNOSIS_ACTIVE[key]["last_seen"] = now
            continue
        DIAGNOSIS_ACTIVE[key] = {"title": finding["title"], "since": now, "last_seen": now}
        log_event("diagnosis", f"Detected: {finding['title']}" + (f" · {', '.join(wans)}" if wans else ""),
                  finding=finding["id"], severity=finding["severity"], wans=wans)
    for key, entry in list(DIAGNOSIS_ACTIVE.items()):
        if key not in seen and now - entry["last_seen"] >= DIAGNOSIS_CLEAR_SECONDS:
            DIAGNOSIS_ACTIVE.pop(key, None)
            log_event("diagnosis", f"Cleared: {entry['title']}", finding=key.split(":")[1],
                      duration_seconds=round(entry["last_seen"] - entry["since"]))


# ---------- SD-WAN steering assessment ----------
#
# A well-behaved appliance moves traffic off an impaired WAN onto a healthy one.
# Per traffic class, compare where transactions went in the last seconds with
# each WAN's health, judge the user impact on impaired WANs, and time how long
# the appliance took to move the class after a WAN became impaired.

STEERING_CLASSES = (("realtime", "Voice & video"), ("interactive", "Web, collaboration & DNS"), ("bulk", "File transfers"))
DEFAULT_APP_CLASSES = {"voice": "realtime", "video": "realtime", "ot_telemetry": "realtime",
                       "web_saas": "interactive", "collaboration": "interactive", "dns": "interactive", "mes": "interactive",
                       "erp": "interactive", "pos": "interactive", "wms_scan": "interactive", "emr": "interactive",
                       "core_banking": "interactive", "file_sync": "bulk", "developer": "bulk", "updates": "bulk",
                       "backup": "bulk", "plm_cad": "bulk", "pacs_imaging": "bulk", "cctv_backhaul": "bulk",
                       "guest_internet": "bulk"}
STEERED_AWAY_PCT = 10.0
STEERING_GRACE_SECONDS = 60
# Latency on an impaired WAN counts as user impact once it is this much worse than on a healthy one.
STEERING_LATENCY_FACTOR = 1.5
STEERING_LATENCY_MARGIN_MS = 50
STEERING_STATE = {"links": {}, "classes": {}, "baseline": {}}


def track_link_health(signals: list, now: float):
    links = STEERING_STATE["links"]
    for item in signals:
        current = links.get(item["link_id"])
        if not current or current["health"] != item["health"]:
            links[item["link_id"]] = {"health": item["health"], "since": now}
    active = {(item["link_id"], links[item["link_id"]]["since"]) for item in signals}
    for key in [key for key in STEERING_STATE["classes"] if (key[1], key[2]) not in active]:
        STEERING_STATE["classes"].pop(key, None)


def track_steering_reaction(cls: str, label: str, link: dict, pct, now: float):
    """Time how long the appliance took to move a class off a WAN that became impaired."""
    state = STEERING_STATE["links"][link["link_id"]]
    key = (cls, link["link_id"], state["since"])
    entry = STEERING_STATE["classes"].get(key)
    if entry is None:
        # Only traffic that used this WAN while it was healthy can be steered away from it.
        baseline = STEERING_STATE["baseline"].get((cls, link["link_id"]))
        entry = STEERING_STATE["classes"][key] = {
            "used": baseline is not None and baseline > STEERED_AWAY_PCT, "steered_at": None, "warned": False,
        }
    elapsed = now - state["since"]
    if entry["used"] and pct is not None and entry["steered_at"] is None:
        if pct <= STEERED_AWAY_PCT:
            entry["steered_at"] = now
            recorder_note("reactions", {"traffic_class": label, "wan": link["label"], "health": link["health"],
                                        "seconds": round(elapsed)})
            log_event("steering", f"SD-WAN moved {label.lower()} off {link['label']} {round(elapsed)} s after it became {link['health']}",
                      link_id=link["link_id"], traffic_class=cls, seconds=round(elapsed))
        elif not entry["warned"] and elapsed >= STEERING_GRACE_SECONDS:
            entry["warned"] = True
            log_event("steering", f"SD-WAN still sends {pct:.0f}% of {label.lower()} over {link['health']} {link['label']} after {round(elapsed)} s",
                      link_id=link["link_id"], traffic_class=cls, share_pct=round(pct, 1))
    return {
        "link_id": link["link_id"], "label": link["label"], "health": link["health"],
        "impaired_for_seconds": round(elapsed), "was_used": entry["used"],
        "steered_after_seconds": round(entry["steered_at"] - state["since"]) if entry["steered_at"] else None,
    }


def assess_steering(dem: dict, signals: list, cfg: dict, now=None):
    """Per traffic class: where it goes now, the health there, the user impact and a verdict."""
    now = now or time.monotonic()
    diagnosis = (dem or {}).get("diagnosis") or {}
    recent = diagnosis.get("egress_recent") or {}
    if not isinstance(recent.get("egress"), dict):
        return None
    window = recent.get("window_seconds", 10)
    applications = (dem or {}).get("applications") or {}
    by_link = {item["link_id"]: item for item in signals}
    track_link_health(signals, now)

    def class_of(app):
        return (applications.get(app) or {}).get("class") or DEFAULT_APP_CLASSES.get(app, "interactive")

    def link_of(address):
        mapped = resolve_egress(address, cfg) if address != "unknown" else None
        return (mapped or {}).get("link_id")

    classes = []
    for cls, label in STEERING_CLASSES:
        shares, unattributed = dict.fromkeys(by_link, 0), 0
        unknown_source, unmapped_addresses = 0, set()
        for address, per_app in recent["egress"].items():
            count = sum(item.get("requests") or 0 for app, item in per_app.items() if class_of(app) == cls)
            if link_of(address) in shares:
                shares[link_of(address)] += count
            else:
                unattributed += count
                if count and address == "unknown":
                    unknown_source += count
                elif count:
                    unmapped_addresses.add(address)
        impact = {link_id: {"requests": 0, "failures": 0, "p95_ms": None} for link_id in by_link}
        for address, item in (diagnosis.get("egress") or {}).items():
            bucket = impact.get(link_of(address))
            for app, counts in (item.get("applications") or {}).items():
                if bucket is None or class_of(app) != cls:
                    continue
                bucket["requests"] += counts.get("requests") or 0
                bucket["failures"] += counts.get("failures") or 0
                if counts.get("p95_ms") is not None:
                    bucket["p95_ms"] = max(bucket["p95_ms"] or 0, counts["p95_ms"])
        total = sum(shares.values())
        observed_total = total + unattributed
        for link_id, count in shares.items():
            if total and not unattributed and by_link[link_id]["health"] == "healthy":
                STEERING_STATE["baseline"][(cls, link_id)] = count * 100.0 / total

        unhealthy = [link_id for link_id in by_link if by_link[link_id]["health"] != "healthy"]
        healthy = [link_id for link_id in by_link if link_id not in unhealthy]
        reactions = [] if unattributed else [track_steering_reaction(cls, label, by_link[link_id],
                                             shares[link_id] * 100.0 / total if total else None, now)
                     for link_id in unhealthy]
        names = lambda ids: ", ".join(by_link[link_id]["label"] for link_id in ids)
        split = " · ".join(f"{by_link[link_id]['label']} {count * 100.0 / total:.0f}%" for link_id, count in shares.items()) if total else ""
        if unattributed:
            verdict, severity = ("partial" if total else "unattributed"), "warn"
            text = (f"{observed_total} {label.lower()} transactions in the last {window} s; "
                    f"{total} mapped to a WAN, {unattributed} unattributed. Steering cannot be verified.")
            if unknown_source:
                text += f" {unknown_source} have no target-reported source address; check replies and target source metadata."
            if unmapped_addresses:
                text += (f" Map observed addresses {', '.join(sorted(unmapped_addresses))} under "
                         "WAN links → Diagnostics → Appliance WAN addresses; verify per-WAN SNAT and upstream NAT.")
        elif not total:
            verdict, severity, text = "idle", "info", f"No {label.lower()} transactions in the last {window} s."
        elif not unhealthy:
            verdict, severity, text = "balanced", "good", f"All WANs healthy · {split}"
        elif not healthy:
            verdict, severity, text = "no_healthy", "warn", f"Every WAN is impaired, so there is no healthy path to steer to · {split}"
        else:
            on_bad = sum(shares[link_id] for link_id in unhealthy) * 100.0 / total
            failures = sum(impact[link_id]["failures"] for link_id in unhealthy)
            requests_bad = sum(impact[link_id]["requests"] for link_id in unhealthy)
            bad_p95 = max((impact[link_id]["p95_ms"] or 0 for link_id in unhealthy), default=0)
            good_p95 = max((impact[link_id]["p95_ms"] or 0 for link_id in healthy), default=0)
            slower = bool(good_p95) and bad_p95 > good_p95 * STEERING_LATENCY_FACTOR and bad_p95 - good_p95 > STEERING_LATENCY_MARGIN_MS
            if on_bad <= STEERED_AWAY_PCT and not any(reaction["was_used"] for reaction in reactions):
                verdict, severity = "unaffected", "good"
                text = f"Not using impaired {names(unhealthy)}, and was not using it before · {split}"
            elif on_bad <= STEERED_AWAY_PCT:
                verdict, severity = "steered", "good"
                text = f"Steered to {names(healthy)}: {100 - on_bad:.0f}% avoids impaired {names(unhealthy)}."
            elif failures or slower:
                verdict, severity = "stuck_impact", "bad"
                effects = []
                if failures:
                    effects.append(f"{failures} of {requests_bad} failed there in the last minute")
                if slower:
                    effects.append(f"P95 {bad_p95:.0f} ms there vs {good_p95:.0f} ms on {names(healthy)}")
                text = f"{on_bad:.0f}% still on impaired {names(unhealthy)}; " + "; ".join(effects) + "."
            else:
                verdict, severity = "stuck", "warn"
                text = f"{on_bad:.0f}% still on impaired {names(unhealthy)}; no user impact measured there yet."
        classes.append({
            "class": cls, "label": label, "verdict": verdict, "severity": severity, "text": text,
            "unattributed": unattributed, "requests": observed_total,
            "unattributed_pct": round(unattributed * 100.0 / observed_total, 1) if observed_total else None,
            "reactions": reactions,
            "shares": [{"link_id": link_id, "label": by_link[link_id]["label"], "health": by_link[link_id]["health"],
                        "health_reason": by_link[link_id]["health_reason"], "requests": count, "pct": round(count * 100.0 / observed_total, 1) if observed_total else None,
                        "failures": impact[link_id]["failures"], "p95_ms": impact[link_id]["p95_ms"]}
                       for link_id, count in shares.items()],
        })
    return {"window_seconds": window, "steered_away_pct": STEERED_AWAY_PCT, "classes": classes}


def steering_findings(steering):
    findings = []
    for item in (steering or {}).get("classes", []):
        if item["severity"] not in ("bad", "warn"):
            continue
        attribution_missing = item["verdict"] in ("unattributed", "partial")
        impaired = [] if attribution_missing else [share for share in item["shares"] if share["health"] != "healthy" and share["requests"]]
        findings.append({
            "source": "steering", "id": f"steering_{item['verdict']}_{item['class']}", "severity": item["severity"],
            "title": f"Cannot verify SD-WAN steering for {item['label'].lower()}" if attribution_missing
                     else f"SD-WAN keeps {item['label'].lower()} on an impaired WAN" if item["verdict"].startswith("stuck")
                     else f"No healthy WAN for {item['label'].lower()}",
            "detail": item["text"],
            "wans": [{"link_id": share["link_id"], "label": share["label"], "affected": share["requests"],
                      "causes": [share["health_reason"] or share["health"]]} for share in impaired],
            "unattributed": item.get("unattributed", 0), "candidates": [], "hint": None,
        })
    return findings


def build_diagnosis():
    cfg = load_config()
    snapshot = traffic_generator_snapshot()
    status = snapshot.get("status") if snapshot.get("connected") else None
    dem = (status or {}).get("dem") or {}
    dem_diagnosis = dem.get("diagnosis") if isinstance(dem.get("diagnosis"), dict) else None
    signals = [path_signals(state, recent_telemetry_samples(state["id"])) for state in build_link_states(cfg)]
    findings = correlate_findings(dem_diagnosis, signals, cfg)
    steering = assess_steering(dem, signals, cfg)
    findings.extend(steering_findings(steering))
    findings.sort(key=lambda finding: (SEVERITY_ORDER.get(finding["severity"], 9), finding["source"] != "experience"))
    experience, unattributed = wan_experience(dem_diagnosis, signals, cfg)
    for item in signals:
        item["experience"] = experience.get(item["link_id"])
    addresses = {address: resolve_egress(address, cfg)
                 for address in ((dem_diagnosis or {}).get("egress") or {}) if address != "unknown"}
    maybe_learn_egress(cfg, status, [address for address, mapped in addresses.items() if not mapped])
    with DIAGNOSIS_LOCK:
        learn_state = dict(EGRESS_LEARN_STATE)
    tcpdump = bool(shutil.which("tcpdump"))
    mapping = egress_mapping_finding(addresses, learn_state, tcpdump)
    if mapping:
        findings.append(mapping)
    with RUNTIME_LOCK:
        expected_run = SCENARIO_STATE.get("workload_run_id") if SCENARIO_STATE.get("active") else None
    workload = workload_finding(status, signals, expected_run) if snapshot.get("configured") else None
    if workload:
        findings.insert(0, workload)
    clock = clock_finding(platform_clock_status())
    if clock:
        findings.append(clock)
    track_diagnosis_events(findings)
    return {
        "timestamp": time.time(),
        "links": signals,
        "findings": findings,
        "steering": steering,
        "unattributed_experience": unattributed,
        "egress": {"addresses": addresses, "learning": learn_state, "tcpdump_available": tcpdump},
        "simulator": {
            "diagnosis_available": dem_diagnosis is not None,
            "media_mode": (dem_diagnosis or {}).get("media_mode"),
            "interactive_p95_ms": dem.get("interactive_p95_ms"),
        },
        "traffic_generator": snapshot,
        "traffic_path": traffic_path_readiness() if snapshot.get("configured") else None,
    }


def current_diagnosis(max_age=DIAGNOSIS_INTERVAL_SECONDS + 1):
    with DIAGNOSIS_LOCK:
        cached = dict(DIAGNOSIS_CACHE)
    if cached["payload"] is not None and cached.get("checked") is not None and time.monotonic() - cached["checked"] <= max_age:
        return cached["payload"]
    payload = build_diagnosis()
    with DIAGNOSIS_LOCK:
        DIAGNOSIS_CACHE.update(timestamp=time.time(), checked=time.monotonic(), payload=payload)
    return payload


def diagnosis_worker():
    # Keeps findings (and their Detected/Cleared events) current without an open browser.
    while not BACKGROUND_STOP.is_set():
        try:
            record_test_sample(current_diagnosis(max_age=0))
        except Exception as exc:
            with DIAGNOSIS_LOCK:
                DIAGNOSIS_CACHE["error"] = str(exc)[:240]
        BACKGROUND_STOP.wait(DIAGNOSIS_INTERVAL_SECONDS)


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
    # Samples recorded while the clock ran ahead must not mask newer ones.
    clauses.append("timestamp <= ?")
    values.append(time.time() + 1)
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
    sock = None

    try:
        if kind == "icmp":
            cmd = [PING, "-n", "-c", "1", "-W", str(timeout_s)]
            if ifname:
                cmd += ["-I", ifname]
            cmd.append(target)
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout_s + 1.5,
                env={**os.environ, "LC_ALL": "C"},
            )
            if proc.returncode != 0:
                raise OSError((proc.stderr or proc.stdout or "ICMP probe failed").strip())
            match = re.search(r"time=([0-9.]+)\s*ms", proc.stdout or "")
            if match:
                latency_ms = float(match.group(1))
            else:
                summary = re.search(r"(?:rtt|round-trip).*?=\s*[0-9.]+/([0-9.]+)/", proc.stdout or "")
                if not summary:
                    raise OSError("ICMP reply did not contain a measurable RTT.")
                latency_ms = float(summary.group(1))
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
            # Connected UDP accepts replies only from the selected resolver.
            sock.connect(sockaddr)
            sock.send(packet)
            response = sock.recv(4096)
            sock.close()
            latency_ms = (time.perf_counter() - started) * 1000
            if len(response) < 12 or int.from_bytes(response[:2], "big") != transaction_id:
                raise OSError("DNS response did not match the query.")
            if not response[2] & 0x80 or response[2] & 0x78:
                raise OSError("DNS packet was not a standard query response.")
            if response[2] & 0x02:
                raise OSError("DNS response was truncated; resolution not verified.")
            rcode = response[3] & 0x0F
            if rcode != 0:
                raise OSError(f"DNS response code {rcode}")
            answers = int.from_bytes(response[6:8], "big")
            if answers == 0:
                raise OSError("DNS response contained no answers.")
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

    finally:
        if sock is not None:
            sock.close()


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
        # Monotonic: a wall-clock step back would postpone every probe by the size of the step.
        now = time.monotonic()

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


# ---------- Platform clocks ----------
#
# NetEm, the Traffic Simulator and the controlled target compare timestamps: events, telemetry
# samples, DEM windows and test summaries. Their clocks must agree and follow a time server.

CLOCK_TOLERANCE_S = 2.0
CLOCK_CHECK_SECONDS = 60
CLOCK_STATUS_CACHE = {"checked": None, "payload": None}
TIMEDATECTL = shutil.which("timedatectl") or "/usr/bin/timedatectl"


def describe_seconds(seconds):
    seconds = round(abs(seconds))
    if seconds < 120:
        return f"{seconds} s"
    if seconds < 3600:
        return f"{seconds // 60} min"
    return f"{seconds // 3600} h {seconds % 3600 // 60} min"


def local_time_sync():
    """This host's NTP state from systemd-timedated; None where it cannot be read."""
    rc, out, _err = run_process([TIMEDATECTL, "show", "--property=NTP", "--property=NTPSynchronized"], timeout=5)
    values = dict(line.split("=", 1) for line in out.splitlines() if "=" in line) if rc == 0 else {}
    return {"ntp": values["NTP"] == "yes" if "NTP" in values else None,
            "synchronized": values["NTPSynchronized"] == "yes" if "NTPSynchronized" in values else None}


def ensure_time_sync():
    """Turn on NTP for this host when it is off, so NetEm's clock cannot drift or be stepped later."""
    if local_time_sync()["ntp"] is not False:
        return False
    rc, out, err = run_process([TIMEDATECTL, "set-ntp", "true"], timeout=10)
    if rc == 0:
        log_event("platform", "Turned on time sync (NTP) for NetEm")
        return True
    log_event("platform", "NetEm could not turn on time sync (NTP)", error=(err or out or "timedatectl failed")[:200])
    return False


def simulator_clock():
    """The simulator's clock offset from NetEm (positive: ahead), its NTP state and its target's offset."""
    sent = time.time()
    health = traffic_generator_request("/api/v1/health", timeout=3.0)
    received = time.time()
    remote = health.get("time")
    if not isinstance(remote, (int, float)) or not math.isfinite(remote):
        return None
    clock = health.get("clock") if isinstance(health.get("clock"), dict) else {}
    return {"offset_s": round(remote - (sent + received) / 2, 2), "uncertainty_s": round((received - sent) / 2, 2),
            "ntp": clock.get("ntp"), "synchronized": clock.get("synchronized"), "container": clock.get("container"),
            "target_offset_s": clock.get("target_offset_s")}


def platform_clock_status(max_age=CLOCK_CHECK_SECONDS):
    """Whether NetEm, the simulator and the target agree on the time and follow a time server."""
    cached = dict(CLOCK_STATUS_CACHE)
    if cached["payload"] is not None and cached["checked"] is not None and time.monotonic() - cached["checked"] <= max_age:
        return cached["payload"]
    components = [dict(name="NetEm", offset_s=0.0, **local_time_sync())]
    simulator_error = None
    if traffic_generator_config(load_config()).get("host") and traffic_generator_api_key():
        try:
            remote = simulator_clock()
        except RuntimeError as exc:
            remote, simulator_error = None, str(exc)
        if remote:
            components.append({"name": "Traffic Simulator", "offset_s": remote["offset_s"], "ntp": remote["ntp"],
                               "synchronized": remote["synchronized"], "container": remote["container"]})
            if isinstance(remote["target_offset_s"], (int, float)):
                components.append({"name": "Controlled target", "offset_s": round(remote["offset_s"] + remote["target_offset_s"], 2),
                                   "ntp": None, "synchronized": None})
        elif not simulator_error:
            components.append({"name": "Traffic Simulator", "offset_s": None, "ntp": None, "synchronized": None,
                               "note": "Update the simulator to compare clocks."})
    issues = []
    for item in components:
        offset = item.get("offset_s")
        if offset is not None and abs(offset) > CLOCK_TOLERANCE_S:
            issues.append(f"The {item['name']} clock is {describe_seconds(offset)} {'ahead of' if offset > 0 else 'behind'} NetEm.")
        if item.get("synchronized") is False:
            issues.append(f"{item['name']} is not synchronized to a time server"
                          + (": time sync is turned off." if item.get("ntp") is False else "."))
    for step in [step for step in CLOCK_STEPS if time.monotonic() - step["clock"] <= 3600][-1:]:
        when = time.strftime("%H:%M", time.localtime(step["at"]))
        if step["step_s"]:
            issues.append(f"NetEm's clock moved {'back' if step['step_s'] < 0 else 'forward'} {describe_seconds(step['step_s'])} at {when}"
                          + (f"; {step['discarded']} samples dated in the future were discarded." if step["discarded"] else "."))
        elif step["discarded"]:
            issues.append(f"At {when} NetEm discarded {step['discarded']} samples dated in the future: its clock had run ahead.")
    payload = {"ok": not issues, "issues": issues, "components": components, "simulator_error": simulator_error,
               "tolerance_s": CLOCK_TOLERANCE_S, "checked_at": time.time()}
    CLOCK_STATUS_CACHE.update(checked=time.monotonic(), payload=payload)
    return payload


def clock_finding(status):
    if status["ok"]:
        return None
    return {"source": "platform", "id": "clock_sync", "severity": "warn", "title": "Clocks out of sync",
            "detail": " ".join(status["issues"]), "wans": [], "unattributed": 0, "candidates": [],
            "hint": "Keep every component on a time server: see Settings → Time sync."}


def start_background_workers():
    global TELEMETRY_THREAD, PROBE_THREAD, DIAGNOSIS_THREAD
    init_telemetry_db()
    BACKGROUND_STOP.clear()
    ensure_time_sync()
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
    if DIAGNOSIS_THREAD is None or not DIAGNOSIS_THREAD.is_alive():
        DIAGNOSIS_THREAD = threading.Thread(
            target=diagnosis_worker,
            name="netem-diagnosis",
            daemon=True,
        )
        DIAGNOSIS_THREAD.start()



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
    summaries = []

    for event in events:
        details = event.get("details", {})
        if event.get("kind") == "test-summary" and isinstance(details.get("summary"), dict):
            summaries.append(details["summary"])
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
                SUM(rate_valid) AS valid_rate_samples,
                AVG(CASE WHEN rate_valid = 1 THEN down_mbps END) AS avg_down_mbps,
                MAX(CASE WHEN rate_valid = 1 THEN down_mbps END) AS max_down_mbps,
                AVG(CASE WHEN rate_valid = 1 THEN up_mbps END) AS avg_up_mbps,
                MAX(CASE WHEN rate_valid = 1 THEN up_mbps END) AS max_up_mbps,
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
            "site": session.get("site"),
        },
        "generated_at": time.time(),
        "result": result,
        "assertions": assertions,
        "tests": tests,
        "test_summaries": summaries,
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
        state = dict(SCENARIO_STATE)
    if state.get("active") and (state.get("clock_start") is not None or state.get("started_at")):
        held = state.get("paused_total_s") or 0.0
        if state.get("clock_start") is not None:
            if state.get("paused") and state.get("paused_clock") is not None:
                held += time.monotonic() - state["paused_clock"]
            ran = time.monotonic() - state["clock_start"]
        else:
            if state.get("paused") and state.get("paused_at"):
                held += time.time() - state["paused_at"]
            ran = time.time() - state["started_at"]
        # Elapsed test time excludes pauses so progress matches the planned phases.
        state["elapsed_s"] = round(max(0.0, ran - held), 1)
        phases, index = state.get("phases") or [], state.get("phase_index")
        if index is not None and index < len(phases) and state.get("phase_started_s") is not None:
            # Counted from when the phase really began, since waits on conditions can stretch a phase.
            state["phase_elapsed_s"] = round(max(0.0, state["elapsed_s"] - state["phase_started_s"]), 1)
            state["phase_remaining_s"] = round(max(0.0, phases[index]["planned_s"] - state["phase_elapsed_s"]), 1)
            state["next_phase"] = phases[index + 1]["name"] if index + 1 < len(phases) else None
    return state


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
    if kind == "steering":
        return f'{condition.get("class", "realtime")} traffic steered off impaired WANs'
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
        if not sample.get("rate_valid", True) or time.time() - sample["timestamp"] > TELEMETRY_SAMPLE_SECONDS * 3:
            return False, None, "Traffic telemetry unavailable or stale"
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
        if field != "active_users" and payload.get("truncated"):
            return False, None, "DEM transaction window exceeded the sample limit"
        experience = payload.get("endpoint_experience") or {}
        if field in ("realtime_availability_pct", "interactive_availability_pct"):
            actual = class_availability(payload.get("applications") or {}, field.split("_", 1)[0])
        elif field == "active_users":
            actual = payload.get("active_users")
        else:
            actual = experience.get("score" if field == "experience_score" else field)
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

    if kind == "steering":
        steering = current_diagnosis(max_age=0).get("steering")
        if not steering:
            return False, None, "Steering needs per-WAN data from Traffic Simulator v0.7 or later"
        item = next((entry for entry in steering["classes"] if entry["class"] == condition.get("class")), None)
        if not item:
            return False, None, "Unknown traffic class"
        passed = item["verdict"] in ("steered", "unaffected", "balanced")
        within = condition.get("within")
        if within is not None:
            late = any(reaction.get("was_used") and reaction.get("steered_after_seconds") is not None
                       and reaction["steered_after_seconds"] > within for reaction in item.get("reactions", []))
            passed = passed and not late
        return passed, item["verdict"], item["text"]

    return False, None, "Unsupported condition"


def class_availability(applications: dict, traffic_class: str):
    """Request success of one traffic class from the simulator's per-application summary."""
    requests = successes = 0
    for name, item in applications.items():
        if (item.get("class") or DEFAULT_APP_CLASSES.get(name)) == traffic_class:
            requests += item.get("requests") or 0
            successes += item.get("successes") or 0
    return round(successes * 100.0 / requests, 3) if requests else None


def wait_for_scenario_condition(
    condition: dict,
    default_link_id: str,
    timeout_s: float,
    poll_s: float,
):
    started = time.time()
    clock = time.monotonic()
    deadline = clock + max(1.0, float(timeout_s))
    last_observed = None
    last_detail = None

    with RUNTIME_LOCK:
        SCENARIO_STATE["condition"] = {
            "description": condition_summary(condition),
            "started_at": started,
            "timeout": timeout_s,
            "observed": None,
        }

    while time.monotonic() <= deadline:
        if SCENARIO_STOP.is_set():
            return False, last_observed, "stopped", time.monotonic() - clock
        if SCENARIO_PAUSE.is_set():
            paused = time.monotonic()
            SCENARIO_STOP.wait(0.25)
            deadline += time.monotonic() - paused
            continue

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
            return True, observed, detail, time.monotonic() - clock
        SCENARIO_STOP.wait(max(0.25, min(5.0, float(poll_s))))

    passed, observed, detail = evaluate_scenario_condition(
        condition, default_link_id
    )
    return passed, observed, detail or last_detail, time.monotonic() - clock


# ---------- Test recording and summary ----------
#
# While a test runs, the diagnosis worker records what users experienced, how
# much traffic each WAN carried and where the appliance steered traffic, tagged
# with the current phase. When the test ends this becomes its summary: what
# happened, performance per phase, SD-WAN remediation and the checks.

TEST_RECORDER = {"active": False}
TEST_RECORDER_LOCK = threading.Lock()
LAST_TEST_SUMMARY_PATH = RUNTIME_DIR / "last-test-summary.json"
BASELINE_PHASES = ("Baseline", "Steady state")


def load_last_test_summary():
    try:
        summary = json.loads(LAST_TEST_SUMMARY_PATH.read_text())
        return summary if isinstance(summary, dict) else None
    except (OSError, ValueError):
        return None


LAST_TEST_SUMMARY = load_last_test_summary()


def recorder_start(scenario: dict, link_id: str, phases: list):
    with TEST_RECORDER_LOCK:
        TEST_RECORDER.clear()
        TEST_RECORDER.update(active=True, scenario_id=scenario.get("id"), name=scenario.get("name"), link_id=link_id,
                             started_at=time.time(), phases=[phase["name"] for phase in phases],
                             phase_marks=[], samples=[], assertions=[], reactions=[])


def recorder_note(kind: str, item: dict):
    with TEST_RECORDER_LOCK:
        if TEST_RECORDER.get("active"):
            TEST_RECORDER[kind].append(item)


def record_test_sample(payload: dict):
    """One sample of the running test, taken with each diagnosis refresh."""
    if not TEST_RECORDER.get("active"):
        return
    scenario = scenario_snapshot()
    if not scenario.get("active"):
        return
    status = (payload.get("traffic_generator") or {}).get("status") or {}
    dem = status.get("dem") or {}
    has_data = bool(dem.get("requests"))
    sample = {
        "t": scenario.get("elapsed_s"), "phase": scenario.get("phase"), "paused": bool(scenario.get("paused")),
        "experience": dem.get("experience_score") if has_data else None,
        "success": dem.get("availability_pct") if has_data else None,
        "interactive_p95_ms": dem.get("interactive_p95_ms") if has_data else None,
        "links": [{"id": item["link_id"], "label": item["label"], "health": item.get("health"),
                   "down_mbps": ((item.get("directions") or {}).get("down") or {}).get("rate_mbps"),
                   "up_mbps": ((item.get("directions") or {}).get("up") or {}).get("rate_mbps")}
                  for item in payload.get("links") or []],
        "steering": {item["label"]: {"verdict": item.get("verdict"),
                                     "impaired": [share["label"] for share in item.get("shares") or []
                                                  if share.get("health") not in (None, "healthy") and share.get("pct")]}
                     for item in (payload.get("steering") or {}).get("classes") or []},
    }
    with TEST_RECORDER_LOCK:
        if TEST_RECORDER.get("active"):
            TEST_RECORDER["samples"] = (TEST_RECORDER["samples"] + [sample])[-2000:]


def _average(values):
    values = [float(value) for value in values if value is not None]
    return round(sum(values) / len(values), 1) if values else None


def summarize_test(record: dict, result: str, error, targets=None, site_label=None, ended_at=None):
    """Turn a recorded test into what happened, performance per phase, remediation and checks."""
    ended_at = ended_at or time.time()
    samples = record.get("samples") or []
    marks = record.get("phase_marks") or []
    phases = []
    for name in record.get("phases") or []:
        rows = [sample for sample in samples if sample.get("phase") == name]
        measured = [sample for sample in rows if sample.get("experience") is not None]
        last = measured[-1] if measured else {}
        mark = next((item for item in marks if item["name"] == name), None)
        following = next((item for item in marks if mark and item["at"] > mark["at"]), None)
        links = {}
        for sample in rows:
            for link in sample.get("links") or []:
                links.setdefault(link["id"], {"label": link["label"], "down": [], "up": [], "health": None})
                links[link["id"]]["down"].append(link.get("down_mbps"))
                links[link["id"]]["up"].append(link.get("up_mbps"))
                links[link["id"]]["health"] = link.get("health") or links[link["id"]]["health"]
        phases.append({
            "name": name, "reached": mark is not None,
            "duration_s": round((following["at"] if following else ended_at) - mark["at"]) if mark else None,
            "experience_score": last.get("experience"), "success_pct": last.get("success"),
            "interactive_p95_ms": last.get("interactive_p95_ms"),
            "worst_success_pct": min((sample["success"] for sample in measured if sample.get("success") is not None), default=None),
            "wans": [{"label": item["label"], "down_mbps": _average(item["down"]), "up_mbps": _average(item["up"]),
                      "health": item["health"]} for item in links.values()],
            "steering": {label: entry["verdict"] for label, entry in ((rows[-1] if rows else {}).get("steering") or {}).items()},
        })
    assertions = record.get("assertions") or []
    failed = [item for item in assertions if not item.get("passed")]
    steering_target = (targets or {}).get("steering_max_s")
    remediation = [dict(item, within_target=None if steering_target is None else item["seconds"] <= steering_target)
                   for item in record.get("reactions") or []]

    conclusion = [f"{record.get('name') or 'Test'} {result}" +
                  (f": {len(assertions) - len(failed)} of {len(assertions)} checks passed." if assertions else ".")]
    if error and result != "passed":
        conclusion.append(str(error)[:200])
    measured_phases = [phase for phase in phases if phase["experience_score"] is not None]
    if measured_phases:
        base = next((phase for phase in measured_phases if phase["name"] in BASELINE_PHASES), measured_phases[0])
        others = [phase for phase in measured_phases if phase is not base]
        worst = min(others, key=lambda phase: phase["experience_score"]) if others else None
        if worst and worst["experience_score"] < base["experience_score"]:
            text = (f"Experience was {base['experience_score']:.0f} in {base['name']} and fell to "
                    f"{worst['experience_score']:.0f} during {worst['name']}")
            if measured_phases[-1] is not worst:
                text += f", ending at {measured_phases[-1]['experience_score']:.0f} in {measured_phases[-1]['name']}"
            conclusion.append(text + ".")
        else:
            conclusion.append(f"Experience held at {base['experience_score']:.0f} or better through the test.")
    else:
        conclusion.append("No simulated user traffic was measured, so user impact and steering were not assessed.")
    # Remediation lines are kept apart so a report can show them in their own section.
    reacted = []
    for item in remediation:
        text = f"The appliance moved {item['traffic_class'].lower()} off {item['wan']} in {item['seconds']} s"
        if item["within_target"] is not None:
            text += f" (target ≤ {steering_target} s {'met' if item['within_target'] else 'missed'})"
        reacted.append(text + ".")
    narrative = list(conclusion)
    stuck = {}
    for phase in phases:
        for label, verdict in phase["steering"].items():
            if verdict in ("stuck", "stuck_impact"):
                stuck.setdefault(label, phase["name"])
    for label, phase_name in stuck.items():
        if not any(item["traffic_class"] == label for item in remediation):
            narrative.append(f"{label} stayed on an impaired WAN during {phase_name}.")
    if failed:
        narrative.append("Missed: " + "; ".join(item["label"] for item in failed[:4]) + ".")
    conclusion = narrative[:len(conclusion)] + reacted + narrative[len(conclusion):]
    return {
        "name": record.get("name"), "scenario_id": record.get("scenario_id"), "link_id": record.get("link_id"),
        "site": site_label, "result": result, "started_at": record.get("started_at"), "ended_at": ended_at,
        "duration_s": round(ended_at - (record.get("started_at") or ended_at)),
        "phases": phases, "remediation": remediation, "steering_target_s": steering_target,
        "assertions": {"passed": len(assertions) - len(failed), "total": len(assertions),
                       "items": [{key: item.get(key) for key in ("label", "passed", "observed", "phase")} for item in assertions]},
        "conclusion": conclusion, "narrative": narrative,
    }


def recorder_finish(result: str, error):
    global LAST_TEST_SUMMARY
    with TEST_RECORDER_LOCK:
        record = copy.deepcopy(TEST_RECORDER)
        TEST_RECORDER.clear()
        TEST_RECORDER["active"] = False
    if not record.get("active"):
        return None
    cfg = load_config()
    plan = active_site_plan(cfg)
    summary = summarize_test(record, result, error, (plan or {}).get("targets"), (plan or {}).get("label"))
    link = get_link(cfg, record.get("link_id") or "")
    summary["link"] = (link or {}).get("name") or record.get("link_id")
    LAST_TEST_SUMMARY = summary
    try:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        LAST_TEST_SUMMARY_PATH.write_text(json.dumps(summary))
    except OSError:
        pass
    log_event("test-summary", summary["conclusion"][0], summary=summary)
    return summary


def scenario_sleep(seconds: float):
    """Wait out a step delay. Paused time does not count, and a paused test holds here
    in its current phase until resumed. Returns True when the test was stopped."""
    remaining = max(0.0, float(seconds))
    while remaining > 0 or SCENARIO_PAUSE.is_set():
        if SCENARIO_STOP.is_set():
            return True
        started = time.monotonic()
        SCENARIO_STOP.wait(0.25 if SCENARIO_PAUSE.is_set() else min(0.25, remaining))
        if not SCENARIO_PAUSE.is_set():
            remaining -= time.monotonic() - started
    return SCENARIO_STOP.is_set()


def scenario_phases(steps):
    """Planned phase timeline: each phase runs from its first step until the next phase starts."""
    phases, elapsed = [], 0
    for step in steps:
        elapsed += int(step.get("after", 0))
        name = step.get("phase") or (phases[-1]["name"] if phases else "Test")
        if not phases or phases[-1]["name"] != name:
            phases.append({"name": name, "start_s": elapsed})
    for current, following in zip(phases, phases[1:] + [{"start_s": elapsed}]):
        current["planned_s"] = following["start_s"] - current["start_s"]
    return phases, elapsed


def step_phase_indexes(steps):
    """Phase index of each step, starting a new phase exactly where scenario_phases does."""
    indexes, index, current = [], -1, None
    for step in steps:
        name = step.get("phase") or current or "Test"
        if name != current:
            index, current = index + 1, name
        indexes.append(index)
    return indexes


def scale_scenario_steps(steps, length_s: float):
    """Stretch or shrink a test to about length_s seconds; DEM windows follow, timeouts do not."""
    planned = scenario_phases(steps)[1]
    if planned <= 0:
        return [dict(step) for step in steps]
    factor = max(0.25, min(4.0, float(length_s) / planned))
    scaled = []
    for step in steps:
        item = copy.deepcopy(step)
        item["after"] = max(0, min(3600, round(int(step.get("after", 0)) * factor)))
        condition = item.get("condition")
        if isinstance(condition, dict) and condition.get("type") == "dem" and condition.get("window"):
            condition["window"] = max(10, min(3600, round(condition["window"] * factor)))
        scaled.append(item)
    return scaled


def requested_length_s(raw):
    """Optional test length from a form (minutes); None keeps the designed length."""
    if raw in (None, ""):
        return None
    minutes = float(raw)
    if not math.isfinite(minutes) or not 1 <= minutes <= 60:
        raise ValueError("Test length must be 1 to 60 minutes.")
    return minutes * 60


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
    scenario_workload_id = None
    scenario_error = None
    started_at = time.time()
    steps = scenario.get("steps", [])
    phases, planned_s = scenario_phases(steps)
    phase_indexes = step_phase_indexes(steps)
    with RUNTIME_LOCK:
        SCENARIO_STATE.update(phases=[{"name": phase["name"], "planned_s": phase["planned_s"]} for phase in phases],
                              phase_index=0 if phases else None, phase=phases[0]["name"] if phases else None,
                              planned_s=planned_s, paused=False, paused_at=None, paused_total_s=0.0,
                              phase_started_s=0.0 if phases else None)
    recorder_start(scenario, link_id, phases)
    if phases:
        recorder_note("phase_marks", {"name": phases[0]["name"], "at": time.time()})

    log_event(
        "scenario",
        f'{scenario["name"]} started',
        scenario_id=scenario.get("id"),
        link_id=link_id,
        stage_count=len(scenario.get("steps", [])),
    )

    try:
        for index, step in enumerate(scenario.get("steps", []), start=1):
            if scenario_sleep(max(0, int(step.get("after", 0)))):
                scenario_result = "stopped"
                break

            action = step.get("action")
            label = step.get("label") or action
            phase_index = phase_indexes[index - 1] if phase_indexes else None
            with RUNTIME_LOCK:
                previous_phase = SCENARIO_STATE.get("phase_index")
                if phase_index is not None and phase_index != previous_phase:
                    origin = SCENARIO_STATE.get("clock_start")
                    ran = (time.monotonic() - origin if origin is not None
                           else time.time() - (SCENARIO_STATE.get("started_at") or started_at))
                    SCENARIO_STATE["phase_started_s"] = round(max(0.0, ran - (SCENARIO_STATE.get("paused_total_s") or 0.0)), 1)
                SCENARIO_STATE.update(
                    {
                        "step": index,
                        "step_label": label,
                        "step_action": action,
                        "condition": None,
                        "phase_index": phase_index,
                        "phase": phases[phase_index]["name"] if phase_index is not None else None,
                    }
                )
            if phase_index is not None and phase_index != previous_phase:
                recorder_note("phase_marks", {"name": phases[phase_index]["name"], "at": time.time()})
                log_event("scenario", f'{scenario["name"]}: phase {phases[phase_index]["name"]}',
                          scenario_id=scenario.get("id"), link_id=link_id, phase=phases[phase_index]["name"])

            if action == "phase":
                pass

            elif action == "quality":
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

            elif action == "traffic_generator":
                payload = dict(step["value"])
                operation = payload.pop("operation")
                if operation == "start":
                    ensure_traffic_path(scenario["name"])
                result = traffic_generator_request(
                    f"/api/v1/workloads/{operation}", method="POST",
                    payload=payload, timeout=5.0,
                )
                if operation == "start":
                    scenario_workload_id = (result.get("run") or {}).get("run_id")
                elif operation == "stop":
                    scenario_workload_id = None
                with RUNTIME_LOCK:
                    SCENARIO_STATE["workload_run_id"] = scenario_workload_id
                log_event("traffic-generator", f'{scenario["name"]}: {label}',
                          scenario_id=scenario.get("id"), link_id=link_id,
                          action=operation, run_id=(result.get("run") or {}).get("run_id"),
                          users=result.get("users"))

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
                    recorder_note("assertions", {"label": label, "passed": bool(passed), "observed": observed,
                                                 "phase": SCENARIO_STATE.get("phase")})
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
        if scenario_workload_id:
            try:
                status = traffic_generator_request("/api/v1/status", timeout=3.0)
                if (status.get("run") or {}).get("run_id") == scenario_workload_id and status.get("status") in ("starting", "running"):
                    traffic_generator_request("/api/v1/workloads/stop", method="POST", payload={}, timeout=5.0)
                    log_event("traffic-generator", "Scenario-owned workload stopped during cleanup", scenario_id=scenario.get("id"), run_id=scenario_workload_id, action="stop")
            except RuntimeError as exc:
                scenario_result = "failed"
                scenario_error = f"{scenario_error + '; ' if scenario_error else ''}Workload cleanup failed: {exc}"[:240]
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
        try:
            recorder_finish(scenario_result, scenario_error)
        except Exception as exc:
            log_event("scenario", "Test summary could not be built", error=str(exc)[:240])
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
                    "phases": [],
                    "phase_index": None,
                    "phase": None,
                    "planned_s": None,
                    "paused": False,
                    "paused_at": None,
                    "paused_total_s": 0.0,
                    "phase_started_s": None,
                    "workload_run_id": None,
                    "clock_start": None,
                    "paused_clock": None,
                }
            )
        SCENARIO_PAUSE.clear()
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
            "appliance_addresses": link.get("appliance_addresses") or [],
            "learned_addresses": sorted(address for address, item in EGRESS_LEARNED.items()
                                        if link_id in item["links"]),
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
        "slug": "diagnosis",
        "title": "Diagnosing experience & steering",
        "category": "Operate",
        "summary": "See why simulated users suffer, which WAN carried it, what on that WAN explains it, and whether the appliance steers away.",
        "template": "docs/articles/diagnosis.html",
        "keywords": "diagnosis bottleneck cause queue drops saturation steering sd-wan failover reaction dem egress snat wan attribution media strict realistic",
    },
    {
        "slug": "site-scenarios",
        "title": "Site scenarios",
        "category": "Operate",
        "summary": "Model a client site with industry traffic, experience targets, WAN lines and an ordered test plan.",
        "template": "docs/articles/site-scenarios.html",
        "keywords": "industry sub-industry site function size criticality manufacturing automotive personas workload failover",
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
        "slug": "showroom",
        "title": "Showroom display",
        "category": "Observe",
        "summary": "A read-only demo screen: the client site, the running test and its phases, WAN performance and what users get.",
        "template": "docs/articles/showroom.html",
        "keywords": "showroom dashboard display demo screen kiosk customer read-only 8082 phases report summary",
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
        "slug": "branding",
        "title": "Appliance branding",
        "category": "Configure",
        "summary": "Give the operator interface, reports and showroom your name, logo, font and colours.",
        "template": "docs/articles/branding.html",
        "keywords": "branding brand logo favicon font colours colors theme palette white label showroom",
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
        traffic_path=traffic_path_readiness() if traffic_generator_config(cfg).get("host") else None,
        site_plan=active_site_plan(cfg),
        site_state=site_plan_snapshot(),
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
        site_options=site_catalog.catalog(),
        traffic_path=traffic_path_readiness() if traffic_generator.get("configured") else None,
        site_plan=active_site_plan(cfg, traffic_generator.get("catalog")),
        site_state=site_plan_snapshot(),
        last_test=LAST_TEST_SUMMARY,
        events=list(reversed([
            event for event in EVENT_LOG
            if event.get("kind") in (
                "scenario", "scenario-config", "fault", "mtu",
                "capture", "security-test", "traffic-generator", "site", "site-test", "test-summary"
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
    site_profile = load_config().get("site_profile")
    return render_template(
        "sessions.html",
        page="sessions",
        active_session=active,
        active_events=active_events,
        sessions=session_rows(),
        site_options=site_catalog.catalog(),
        site_selection=site_profile if isinstance(site_profile, dict) else None,
    )


@app.route("/sessions/start", methods=["POST"])
def session_start():
    name = (request.form.get("name") or "Lab session").strip()[:100]
    # A session validates one client site; without fields it uses the active site profile.
    cfg = load_config()
    submitted = {key: request.form.get(key) for key in SITE_FIELDS}
    try:
        if any(submitted.values()):
            selection = site_catalog.validate_selection(submitted)
        elif isinstance(cfg.get("site_profile"), dict):
            selection = site_catalog.validate_selection(cfg["site_profile"])
        else:
            raise ValueError("Choose the industry, sub-industry, site function, size and criticality this session validates.")
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect_after("sessions")
    site = dict(selection, label=site_catalog.site_label(selection))
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
                "site": site,
            }
        )
        LAB_SESSIONS.append(
            {
                "id": session_id,
                "name": ACTIVE_SESSION["name"],
                "started_at": ACTIVE_SESSION["started_at"],
                "ended_at": None,
                "status": "active",
                "site": site,
            }
        )
        del LAB_SESSIONS[:-100]
        save_session_history()

    if cfg.get("site_profile") != selection:
        # The session's site becomes the active site so its targets judge what is recorded.
        cfg["site_profile"] = selection
        save_config(cfg)
        log_event("site", f"Active site: {site['label']}", selection=selection, sla_applied=False)
    log_event("session", f'Lab session started: {ACTIVE_SESSION["name"]} · {site["label"]}', site=site)
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
                "site": None,
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

    try:
        discovered_port = None
        if host.startswith("https://") or host.startswith("http://"):
            parsed = urlsplit(host)
            if parsed.username is not None or parsed.password is not None:
                raise ValueError("Do not include credentials in the simulator URL.")
            host = parsed.hostname or ""
            discovered_port = parsed.port
        host = host.strip("[]")
        if not host or len(host) > 255 or not re.fullmatch(r"[A-Za-z0-9_.:\-]+", host):
            raise ValueError("Enter a valid Traffic Simulator IP address or hostname.")
        port = int(discovered_port if discovered_port is not None else request.form.get("port") or 8443)
        if not 1 <= port <= 65535:
            raise ValueError("Traffic Simulator API port must be 1-65535.")
        api_key = (request.form.get("api_key") or "").strip()
        if api_key and not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", api_key):
            raise ValueError("Enter the raw simulator API key without a Bearer prefix or whitespace.")
        fingerprint = (request.form.get("tls_sha256") or "").strip().lower().replace(":", "")
        old = traffic_generator_config(cfg)
        if (host, port) != (old.get("host"), old.get("port", 8443)) and fingerprint == old.get("tls_sha256"):
            fingerprint = ""
        if fingerprint and not re.fullmatch(r"[a-f0-9]{64}", fingerprint):
            raise ValueError("TLS SHA-256 fingerprint must contain 64 hexadecimal digits.")
    except (ValueError, OverflowError) as exc:
        flash(str(exc), "error")
        return redirect(url_for("integrations") + "#traffic-simulator")

    integration = {
        "host": host,
        "port": port,
        "allow_self_signed": request.form.get("allow_self_signed") == "on",
        "instance_name": (request.form.get("instance_name") or "").strip()[:120] or None,
        "version": (request.form.get("version") or "").strip()[:40] or None,
        "tls_sha256": fingerprint or None,
    }
    cfg["traffic_generator"] = integration
    save_config(cfg)

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
    # Only simulators that offer media modes get the field; older ones reject unknown fields.
    if request.form.get("media_mode"):
        payload["media_mode"] = request.form.get("media_mode")

    try:
        ensure_traffic_path("Corporate workload")
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
    for key in ("users", "spawn_rate", "activity", "media_mode"):
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
        try:
            status_before = traffic_generator_request("/api/v1/status", timeout=3.0)
        except RuntimeError:
            status_before = {}
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


@app.route("/api/v1/diagnosis")
def api_diagnosis():
    return jsonify(current_diagnosis())


@app.route("/wan/egress", methods=["POST"])
def wan_egress():
    """Save the appliance's WAN addresses so simulator traffic maps to this WAN."""
    link_id = request.form.get("link_id") or ""
    entries = [item for item in re.split(r"[\s,]+", request.form.get("appliance_addresses") or "") if item]
    try:
        addresses = [str(ipaddress.ip_network(item, strict=False)) if "/" in item else str(ipaddress.ip_address(item))
                     for item in entries]
    except ValueError:
        flash("Enter IP addresses or prefixes separated by commas.", "error")
        return redirect_after("wan_links")
    if len(addresses) > 16:
        flash("Enter at most 16 addresses or prefixes per WAN.", "error")
        return redirect_after("wan_links")
    cfg = load_config()
    link = get_link(cfg, link_id)
    if not link:
        flash("Unknown WAN link.", "error")
        return redirect_after("wan_links")
    if addresses:
        link["appliance_addresses"] = addresses
    else:
        link.pop("appliance_addresses", None)
    save_config(cfg)
    log_event("config", f"{link_id}: appliance WAN addresses {', '.join(addresses) or 'cleared'}",
              link_id=link_id, appliance_addresses=addresses)
    flash("Appliance WAN addresses saved." if addresses else "Appliance WAN addresses cleared.", "success")
    return redirect_after("wan_links")


@app.route("/integrations/traffic-generator/repair", methods=["POST"])
def traffic_generator_repair():
    try:
        readiness = repair_traffic_path()
        if readiness.get("ready"):
            log_event("traffic-generator", "Traffic path repaired", action="repair")
            flash(readiness["summary"], "success")
        else:
            flash(readiness["summary"], "error")
    except RuntimeError as exc:
        flash(f"Traffic path not repaired: {exc}", "error")
    return redirect_after("overview")


@app.route("/api/v1/traffic-generator/path")
def api_traffic_path():
    return jsonify(traffic_path_readiness())


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
        showroom_url=showroom_url(),
        clock_status=platform_clock_status(),
    )


@app.route("/settings/time-sync", methods=["POST"])
def time_sync_enable():
    rc, out, err = run_process([TIMEDATECTL, "set-ntp", "true"], timeout=10)
    if rc == 0:
        log_event("platform", "Turned on time sync (NTP) for NetEm")
        flash("Time sync is on for NetEm. Synchronizing can take a minute.", "success")
    else:
        flash(f"NetEm could not turn on time sync ({(err or out or 'timedatectl failed')[:160]}). "
              "Run sudo timedatectl set-ntp true on the NetEm VM.", "error")
    CLOCK_STATUS_CACHE.update(checked=None)
    return redirect_after("settings")


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
    messages = []
    asynchronous = request.headers.get("Accept") == "application/json"

    def feedback(message, category):
        messages.append({"message": message, "category": category})
        if not asynchronous:
            flash(message, category)

    def finish():
        if asynchronous:
            ok = not any(item["category"] == "error" for item in messages)
            return jsonify({"ok": ok, "messages": messages}), (200 if ok else 400)
        return redirect_after("overview")

    cfg = load_config()
    presets = get_presets(cfg)
    link_id = request.form.get("link_id") or ""
    action = request.form.get("action") or ""
    link = get_link(cfg, link_id)

    if not link:
        feedback("Unknown WAN link.", "error")
        return finish()

    if scenario_snapshot().get("active"):
        feedback("Stop the active scenario before changing the WAN manually.", "error")
        return finish()

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
            feedback(
                f'{link.get("name", "WAN")} set to {quality}% ({quality_status(quality)}).',
                "success",
            )
        else:
            feedback("Failed to apply WAN quality: " + msg, "error")

    elif action in (
        "normal", "blackhole", "downstream_blackhole", "upstream_blackhole"
    ):
        ok, msg = apply_runtime_fault(link, action, presets)
        if ok:
            feedback(
                "WAN restored." if action == "normal"
                else f'{link.get("name", "WAN")}: {action.replace("_", " ")} applied.',
                "success",
            )
        else:
            feedback("Failed to apply runtime fault: " + msg, "error")

    elif action == "bandwidth":
        try:
            download = max(1, min(100000, int(request.form.get("download_mbit", "1"))))
            upload = max(1, min(100000, int(request.form.get("upload_mbit", "1"))))
        except ValueError:
            feedback("Bandwidth values must be whole-number Mbit/s values.", "error")
            return finish()

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
            feedback(
                f'{link.get("name", "WAN")} nominal rate set to {download}/{upload} Mbit/s.',
                "success",
            )
        else:
            feedback("Failed to apply bandwidth limit: " + msg, "error")

    elif action == "mtu":
        try:
            mtu = int(request.form.get("mtu", "0"))
        except ValueError:
            mtu = 0
        ok, msg = apply_mtu_limit(link, mtu)
        if ok:
            feedback(
                "Path MTU restored." if mtu == 0
                else f"Path MTU limited to {mtu} bytes.",
                "success",
            )
        else:
            feedback("Failed to change path MTU: " + msg, "error")

    else:
        feedback("Unknown quick action.", "error")

    return finish()


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


# ---------- Site scenarios ----------
#
# A saved site profile (industry, sub-industry, function, size, criticality)
# supplies the simulated workload, the pass/fail targets, typical WAN lines and an
# ordered test plan; see site_catalog.py. The plan runs test by test through the
# scenario engine, so each test restores its WAN like any other scenario.

SITE_PLAN_STATE = {"active": False, "label": None, "tests": [], "current": None,
                   "started_at": None, "finished_at": None, "result": None}
SITE_PLAN_STOP = threading.Event()
SIMULATOR_CATALOG_CACHE = {"timestamp": 0.0, "checked": None, "catalog": None}
SITE_FIELDS = ("industry", "sub_industry", "function", "size", "criticality")


def simulator_catalog(max_age=60):
    """The connected simulator's catalog, briefly cached; None when unavailable."""
    if SIMULATOR_CATALOG_CACHE.get("checked") is not None and time.monotonic() - SIMULATOR_CATALOG_CACHE["checked"] <= max_age:
        return SIMULATOR_CATALOG_CACHE["catalog"]
    catalog = None
    if traffic_generator_config().get("host") and traffic_generator_api_key():
        try:
            catalog = traffic_generator_request("/api/v1/catalog", timeout=2.0)
        except RuntimeError:
            catalog = None
    SIMULATOR_CATALOG_CACHE.update(timestamp=time.time(), checked=time.monotonic(), catalog=catalog)
    return catalog


def active_site_plan(cfg=None, catalog=None):
    selection = (cfg if cfg is not None else load_config()).get("site_profile")
    if not isinstance(selection, dict):
        return None
    try:
        return site_catalog.build_site_plan(selection, catalog)
    except ValueError:
        return None


def site_plan_snapshot():
    with RUNTIME_LOCK:
        return copy.deepcopy(SITE_PLAN_STATE)


def run_site_plan(plan: dict, tests: list, link_for_role: dict):
    """Run site tests one after another; each is an ordinary scenario on its WAN."""
    log_event("site-test", f"Site test plan started · {plan['label']}", tests=[test["id"] for test in tests])
    results = []
    for test in tests:
        if SITE_PLAN_STOP.is_set():
            break
        link_id = link_for_role[test["role"]]
        scenario = {"id": f"site_{test['id']}", "name": test["name"], "steps": validate_scenario_steps(test["steps"])}
        with RUNTIME_LOCK:
            if SITE_PLAN_STOP.is_set():
                break
            SITE_PLAN_STATE["current"] = test["id"]
            for item in SITE_PLAN_STATE["tests"]:
                if item["id"] == test["id"]:
                    item.update(status="running", link_id=link_id)
            SCENARIO_STOP.clear()
            SCENARIO_STATE.update({
                "active": True, "scenario_id": scenario["id"], "scenario_name": f"{plan['label']}: {test['name']}",
                "link_id": link_id, "started_at": time.time(), "clock_start": time.monotonic(), "step": 0,
                "step_count": len(scenario["steps"]),
                "step_label": "Starting", "step_action": None, "condition": None, "result": None, "error": None,
            })
        try:
            run_scenario(link_id, scenario)
        except Exception as exc:
            # Keep plan state recoverable even if the scenario runner fails before cleanup.
            with RUNTIME_LOCK:
                SCENARIO_STATE.update(active=False, result="failed", error=str(exc)[:240])
        finished = scenario_snapshot()
        result = finished.get("result") or "failed"
        with RUNTIME_LOCK:
            for item in SITE_PLAN_STATE["tests"]:
                if item["id"] == test["id"]:
                    item.update(status=result, error=finished.get("error"))
        log_event("site-test", f"{test['name']} — {result.upper()}", test_id=test["id"], link_id=link_id,
                  result=result, error=finished.get("error"), site=plan["label"])
        results.append(result)
        if result == "stopped":
            break
    stopped = SITE_PLAN_STOP.is_set() or "stopped" in results
    overall = "stopped" if stopped else ("passed" if results and all(item == "passed" for item in results) else "failed")
    with RUNTIME_LOCK:
        for item in SITE_PLAN_STATE["tests"]:
            if item["status"] == "pending":
                item["status"] = "skipped"
        SITE_PLAN_STATE.update(active=False, current=None, finished_at=time.time(), result=overall)
    SITE_PLAN_STOP.clear()
    log_event("site-test", f"Site test plan finished — {overall.upper()} · {plan['label']}", result=overall)


@app.route("/api/v1/site-plan")
def api_site_plan():
    try:
        plan = site_catalog.build_site_plan({key: request.args.get(key) for key in SITE_FIELDS}, simulator_catalog())
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(plan)


@app.route("/site/save", methods=["POST"])
def site_save():
    if site_plan_snapshot().get("active") or scenario_snapshot().get("active"):
        flash("Stop the site plan before changing its profile.", "error")
        return redirect_after("tests")
    try:
        plan = site_catalog.build_site_plan({key: request.form.get(key) for key in SITE_FIELDS})
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect_after("tests")
    cfg = load_config()
    cfg["site_profile"] = plan["selection"]
    apply_sla = bool(request.form.get("apply_sla"))
    if apply_sla:
        cfg["sla_profile"] = plan["sla_profile"]
    save_config(cfg)
    log_event("site", f"Active site: {plan['label']}", selection=plan["selection"], sla_applied=apply_sla)
    flash(f"Active site: {plan['label']}." + (" WAN SLA thresholds set from its criticality." if apply_sla else ""), "success")
    return redirect_after("tests")


@app.route("/site/wan", methods=["POST"])
def site_apply_wan():
    """Shape the chosen WANs like the active site's typical primary and backup lines."""
    cfg = load_config()
    plan = active_site_plan(cfg)
    if not plan:
        flash("Save a site first.", "error")
        return redirect_after("tests")
    if scenario_snapshot().get("active") or site_plan_snapshot().get("active"):
        flash("Wait for the running test to finish before changing WAN lines.", "error")
        return redirect_after("tests")
    role_ids = [request.form.get(f"{role}_link") or "" for role in ("primary", "backup")]
    if len(set(role_ids)) != 2 or any(not get_link(cfg, link_id) for link_id in role_ids):
        flash("Choose two different existing WANs for primary and backup.", "error")
        return redirect_after("tests")
    presets = get_presets(cfg)
    applied = []
    for role in ("primary", "backup"):
        link = get_link(cfg, request.form.get(f"{role}_link") or "")
        line = plan["wan_lines"][role]
        if not link or line["preset"] not in presets:
            continue
        link.update(preset=line["preset"], mode="quality", quality=100,
                    bandwidth_download_mbit=line["download_mbit"], bandwidth_upload_mbit=line["upload_mbit"])
        link.pop("custom_profile", None)
        applied.append((link, line))
    save_config(cfg)
    for link, line in applied:
        ok, msg, _effective = apply_selected_profile(link, presets)
        label = f"{presets[line['preset']].get('name', line['preset'])} {line['download_mbit']}/{line['upload_mbit']} Mbit/s"
        log_event("site", f"{link.get('name', link.get('id'))} set to {label} ({line['role']})", link_id=link.get("id"), ok=ok)
        if not ok:
            flash(f"{link.get('name', link.get('id'))}: {msg}", "error")
    flash("WAN lines applied for " + plan["label"] + "." if applied else "Choose WANs for the primary and backup lines.",
          "success" if applied else "error")
    return redirect_after("tests")


@app.route("/site/run", methods=["POST"])
def site_run():
    cfg = load_config()
    plan = active_site_plan(cfg, simulator_catalog(max_age=0))
    if not plan:
        flash("Save a site first.", "error")
        return redirect_after("tests")
    if request.form.get("users"):
        try:
            users = int(request.form["users"])
            if not 1 <= users <= site_catalog.MAX_SIMULATED_USERS:
                raise ValueError()
        except (TypeError, ValueError):
            flash("Simulated users must be an integer from 1 to 5000.", "error")
            return redirect_after("tests")
        plan["start"]["users"] = users
        plan["start"]["spawn_rate"] = max(5.0, round(users / 30.0, 1))
        plan["tests"] = site_catalog.test_plan(plan["selection"], plan["start"])
    if not traffic_generator_snapshot().get("connected"):
        flash("Site tests need a connected Traffic Simulator.", "error")
        return redirect_after("tests")
    try:
        length_s = requested_length_s(request.form.get("length_min"))
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect_after("tests")
    if length_s:
        plan["tests"] = [dict(test, steps=scale_scenario_steps(test["steps"], length_s)) for test in plan["tests"]]
    test_id = request.form.get("test_id") or "all"
    tests = [test for test in plan["tests"] if test_id in ("all", test["id"])]
    links = {role: request.form.get(f"{role}_link") or "" for role in ("primary", "backup")}
    if not tests or any(not get_link(cfg, links[role]) for role in ("primary", "backup")):
        flash("Choose the site test and existing WANs for its primary/backup roles.", "error")
        return redirect_after("tests")
    if links["primary"] == links["backup"]:
        flash("Primary and backup must be different WANs.", "error")
        return redirect_after("tests")
    with RUNTIME_LOCK:
        if SCENARIO_STATE["active"] or SITE_PLAN_STATE["active"]:
            flash("A test is already running.", "error")
            return redirect_after("tests")
        SITE_PLAN_STOP.clear()
        SITE_PLAN_STATE.update(
            active=True, label=plan["label"], current=None, started_at=time.time(), finished_at=None, result=None,
            tests=[{"id": test["id"], "name": test["name"], "role": test["role"], "status": "pending",
                    "link_id": None, "error": None} for test in tests],
        )
    threading.Thread(target=run_site_plan, args=(plan, tests, links), name="netem-site-plan", daemon=True).start()
    flash(f"Running {len(tests)} site test{'s' if len(tests) != 1 else ''} for {plan['label']}.", "success")
    return redirect_after("tests")


@app.route("/site/stop", methods=["POST"])
def site_stop():
    if site_plan_snapshot().get("active"):
        SITE_PLAN_STOP.set()
        SCENARIO_STOP.set()
        flash("Stopping the site test plan; the running test restores its WAN.", "info")
    return redirect_after("tests")


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
    try:
        length_s = requested_length_s(request.form.get("length_min"))
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect_after("scenarios")
    if length_s:
        scenario = dict(scenario, steps=scale_scenario_steps(scenario["steps"], length_s))

    with RUNTIME_LOCK:
        if SCENARIO_STATE["active"] or SITE_PLAN_STATE["active"]:
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
                "clock_start": time.monotonic(),
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


@app.route("/lab/scenario/pause", methods=["POST"])
def lab_scenario_pause():
    """Hold the running test in its current phase, impairment applied, until resumed."""
    with RUNTIME_LOCK:
        active = SCENARIO_STATE["active"] and not SCENARIO_STATE["paused"]
        if active:
            SCENARIO_PAUSE.set()
            SCENARIO_STATE.update(paused=True, paused_at=time.time(), paused_clock=time.monotonic())
            name, phase = SCENARIO_STATE["scenario_name"], SCENARIO_STATE.get("phase")
    if active:
        log_event("scenario", f"{name}: paused" + (f" in phase {phase}" if phase else ""), phase=phase)
        flash("Test paused. It stays in its current phase until you resume it.", "info")
    return redirect_after("tests")


@app.route("/lab/scenario/resume", methods=["POST"])
def lab_scenario_resume():
    with RUNTIME_LOCK:
        active = SCENARIO_STATE["active"] and SCENARIO_STATE["paused"]
        if active:
            started = SCENARIO_STATE.get("paused_clock")
            held = time.monotonic() - started if started is not None else time.time() - (SCENARIO_STATE["paused_at"] or time.time())
            SCENARIO_STATE.update(paused=False, paused_at=None, paused_clock=None,
                                  paused_total_s=SCENARIO_STATE["paused_total_s"] + held)
            SCENARIO_PAUSE.clear()
            name = SCENARIO_STATE["scenario_name"]
    if active:
        log_event("scenario", f"{name}: resumed after {round(held)} s", paused_seconds=round(held))
        flash("Test resumed.", "info")
    return redirect_after("tests")


@app.route("/lab/scenario/stop", methods=["POST"])
def lab_scenario_stop():
    if site_plan_snapshot().get("active"):
        SITE_PLAN_STOP.set()
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
            "site_plan": site_plan_snapshot(),
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
        snapshot = traffic_snapshot(link)
        inner = snapshot["inner"]["counters"]
        outer = snapshot["outer"]["counters"]

        links.append(
            {
                "id": link_id,
                "name": link.get("name", "WAN"),
                "timestamp": snapshot["timestamp"],
                "monotonic_timestamp": snapshot["monotonic_timestamp"],
                "counters_valid": snapshot["valid"],
                "inner": snapshot["inner"],
                "outer": snapshot["outer"],
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
    return jsonify({"timestamp": time.time(), "sampler_id": TELEMETRY_SAMPLER_ID, "links": links})


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
            if counters["rx_bytes"] is None or counters["tx_bytes"] is None:
                continue
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
            elif not status["update_available"]:
                flash("NetEm already runs the latest release." + (
                    f' {status["unreleased"]} merged commit(s) on main will come with the next release.'
                    if status["unreleased"] else ""), "info")
            elif not status["release_reachable"]:
                flash(
                    "Update blocked because this installation has commits that are not in the release.",
                    "error",
                )
            else:
                # Install the release itself, not commits merged after it.
                rc, out, err = run_process(
                    [GIT, "merge", "--ff-only", status["release_commit"]],
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
        process_instance=PROCESS_INSTANCE,
    )


@app.route("/updates/status")
def updates_status():
    """Which NetEm process answers and its version, polled while an update restarts the service."""
    response = jsonify({"version": get_app_version(), "instance": PROCESS_INSTANCE})
    response.headers["Cache-Control"] = "no-store"
    return response


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


def showroom_snapshot():
    """Publish only presentation data, never configuration, addresses or secrets."""
    now = time.time()
    cfg = load_config()
    links = []
    for state in build_link_states(cfg):
        sample = latest_telemetry_sample(state["id"])
        # Judge age when the sample is read: a sample written while the snapshot is being
        # built is newer than the snapshot's start, not from the future.
        age = time.time() - sample["timestamp"] if sample else None
        fresh = bool(sample and sample.get("rate_valid") and
                     0 <= age <= TELEMETRY_SAMPLE_SECONDS * 3 and
                     all(isinstance(sample.get(key), (int, float)) and
                         math.isfinite(sample[key]) and sample[key] >= 0
                         for key in ("down_mbps", "up_mbps")))
        links.append({
            "id": state["id"], "name": state["label"],
            "profile": state["preset_name"], "quality": state["runtime_quality"],
            "fault": state["fault"], "sla_pass": state["sla"]["pass"],
            "delay_ms": state["effective"]["delay_ms"],
            "jitter_ms": state["effective"]["jitter_ms"],
            "loss_pct": state["effective"]["loss_pct"],
            "download_limit_mbit": state["effective"].get("download_mbit"),
            "upload_limit_mbit": state["effective"].get("upload_mbit"),
            "sample_timestamp": sample["timestamp"] if sample else None,
            "traffic_available": fresh,
            "down_mbps": sample.get("down_mbps") if fresh else None,
            "up_mbps": sample.get("up_mbps") if fresh else None,
        })
    scenario = scenario_snapshot()
    lab_session = session_snapshot()
    try:
        diagnosis = current_diagnosis()
    except Exception:
        diagnosis = {}
    # A running session names the site it validates; otherwise show the active site profile.
    session_site = lab_session.get("site") if lab_session.get("active") else None
    site_plan = active_site_plan({"site_profile": session_site} if session_site else cfg)
    signals = {item["link_id"]: item for item in diagnosis.get("links") or []}
    for link in links:
        item = signals.get(link["id"]) or {}
        directions = item.get("directions") or {}
        experience = item.get("experience") or {}
        link.update({
            "health": item.get("health"),
            "health_reason": showroom_text(item.get("health_reason")) or None,
            "full": list(item.get("full") or []),
            "down_util_pct": (directions.get("down") or {}).get("util_pct"),
            "up_util_pct": (directions.get("up") or {}).get("util_pct"),
            "users_success_pct": experience.get("availability_pct"),
            "worst_app": showroom_app_label(experience.get("worst_app")) if experience.get("worst_app") else None,
            "worst_app_success_pct": experience.get("worst_availability_pct"),
        })
    plan = site_plan_snapshot()
    return {
        "timestamp": now, "links": links,
        "scenario": dict({key: scenario.get(key) for key in (
            "active", "scenario_name", "step", "step_count", "step_label", "phases", "phase_index", "phase",
            "planned_s", "elapsed_s", "paused", "phase_elapsed_s", "phase_remaining_s", "next_phase")},
            link=showroom_link_label(cfg, scenario.get("link_id"))),
        # Once a test ends, its summary stays on screen until the next one starts.
        "last_test": showroom_summary(LAST_TEST_SUMMARY) if not scenario.get("active") else None,
        "session": {"active": bool(lab_session.get("active")), "name": lab_session.get("name"),
                    "site": (session_site or {}).get("label")},
        "site": showroom_site(site_plan),
        "plan": {"active": plan.get("active"), "label": plan.get("label"), "result": plan.get("result"),
                 "tests": [{"name": test.get("name"), "role": test.get("role"), "status": test.get("status")}
                           for test in plan.get("tests") or []]},
        **showroom_outcome(diagnosis, site_plan),
    }


# The showroom publishes presentation data only. Findings and steering texts are
# generated from measurements and may mention addresses, so those are removed.
SHOWROOM_ADDRESS = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b|\b[0-9a-fA-F]{1,4}(?::[0-9a-fA-F]{0,4}){2,7}\b")
# Without an active site the showroom judges against the Command Center's lab thresholds.
SHOWROOM_LAB_TARGETS = {"experience_min": 75, "success_min_pct": 99.0, "interactive_p95_max_ms": 400, "steering_max_s": None}


def showroom_text(value, limit=240):
    return SHOWROOM_ADDRESS.sub("[address]", str(value or ""))[:limit]


def showroom_link_label(cfg, link_id):
    link = get_link(cfg, link_id or "") if link_id else None
    return (link or {}).get("name") or None


def showroom_summary(summary):
    """The finished test's summary without internal identifiers or addresses."""
    if not summary:
        return None
    return {
        "name": summary.get("name"), "link": summary.get("link"), "site": summary.get("site"),
        "result": summary.get("result"), "ended_at": summary.get("ended_at"), "duration_s": summary.get("duration_s"),
        "conclusion": [showroom_text(line) for line in summary.get("narrative") or summary.get("conclusion") or []],
        "phases": [{key: phase.get(key) for key in ("name", "reached", "duration_s", "experience_score", "success_pct",
                                                    "worst_success_pct", "interactive_p95_ms", "steering")}
                   | {"wans": [{key: wan.get(key) for key in ("label", "down_mbps", "up_mbps", "health")}
                               for wan in phase.get("wans") or []]}
                   for phase in summary.get("phases") or []],
        "remediation": [{key: item.get(key) for key in ("traffic_class", "wan", "seconds", "within_target")}
                        for item in summary.get("remediation") or []],
        "steering_target_s": summary.get("steering_target_s"),
        "assertions": {"passed": (summary.get("assertions") or {}).get("passed"),
                       "total": (summary.get("assertions") or {}).get("total")},
    }


def showroom_app_label(name):
    catalog = SIMULATOR_CATALOG_CACHE.get("catalog") or {}
    label = ((catalog.get("applications") or {}).get(name) or {}).get("label")
    return label or str(name).replace("_", " ").capitalize()


def showroom_site(plan):
    """Who the demonstration models: the active site profile, its targets and typical lines."""
    if not plan:
        return None
    selection = plan["selection"]
    industry = site_catalog.INDUSTRIES[selection["industry"]]
    load = plan["workload"]
    return {
        "label": plan["label"],
        "industry": industry["label"],
        "sub_industry": industry["sub_industries"][selection["sub_industry"]]["label"],
        "function": site_catalog.SITE_FUNCTIONS[selection["function"]]["label"],
        "size": site_catalog.SIZES[selection["size"]]["label"],
        "criticality": site_catalog.CRITICALITY[selection["criticality"]]["label"],
        "criticality_description": site_catalog.CRITICALITY[selection["criticality"]]["description"],
        "employees": load["employees"],
        "simulated_users": plan["start"]["users"],
        "devices": {name: count for name, count in (load.get("devices") or {}).items() if count},
        "targets": plan["targets"],
        "wan_lines": {role: {"preset": line["preset"], "download_mbit": line["download_mbit"],
                             "upload_mbit": line["upload_mbit"]}
                      for role, line in plan["wan_lines"].items() if role in ("primary", "backup")},
    }


def showroom_verdict(value, target, higher_is_better=True):
    if value is None or target is None:
        return "unknown"
    return "pass" if (value >= target if higher_is_better else value <= target) else "fail"


def showroom_outcome(diagnosis, site_plan):
    """What the simulated users get: traffic running, result against targets, where and why."""
    generator = diagnosis.get("traffic_generator") or {}
    status = generator.get("status") if generator.get("connected") else None
    dem = (status or {}).get("dem") or {}
    run = (status or {}).get("run") or {}
    targets = dict((site_plan or {}).get("targets") or SHOWROOM_LAB_TARGETS)
    apps = {name: item for name, item in (dem.get("applications") or {}).items() if item.get("requests")}
    total = sum(item["requests"] for item in apps.values())
    has_data = bool(dem.get("requests"))
    score, success, interactive = (dem.get(key) if has_data else None
                                   for key in ("experience_score", "availability_pct", "interactive_p95_ms"))
    steering = diagnosis.get("steering") or {}
    return {
        "traffic": {
            "configured": bool(generator.get("configured")), "connected": bool(generator.get("connected")),
            "status": (status or {}).get("status"), "users": (status or {}).get("users"),
            "label": showroom_text(run.get("label"), 120) or None, "activity": run.get("activity"),
            "media_mode": run.get("media_mode"), "requests": dem.get("requests"), "window_seconds": dem.get("window_seconds"),
            "applications": [{"name": showroom_app_label(name), "class": item.get("class") or DEFAULT_APP_CLASSES.get(name),
                              "share_pct": round(item["requests"] * 100.0 / total, 1),
                              "success_pct": item.get("availability_pct")}
                             for name, item in sorted(apps.items(), key=lambda entry: -entry[1]["requests"])[:6]],
        },
        "experience": {
            "available": has_data, "targets_source": "site" if site_plan else "lab", "targets": targets,
            "experience_score": score, "success_pct": success, "interactive_p95_ms": interactive,
            "verdicts": {
                "experience": showroom_verdict(score, targets.get("experience_min")),
                "success": showroom_verdict(success, targets.get("success_min_pct")),
                "interactive": showroom_verdict(interactive, targets.get("interactive_p95_max_ms"), higher_is_better=False),
            },
        },
        "findings": [{
            "severity": item.get("severity"), "source": item.get("source"), "title": showroom_text(item.get("title")),
            "detail": showroom_text(item.get("detail")), "hint": showroom_text(item.get("hint")) or None,
            "unattributed": item.get("unattributed") or 0,
            "wans": [{"label": wan.get("label"), "affected": wan.get("affected") or 0,
                      "causes": [showroom_text(cause) for cause in wan.get("causes") or []]}
                     for wan in item.get("wans") or []],
        } for item in (diagnosis.get("findings") or []) if item.get("source") not in ("mapping", "platform")][:4],
        "steering": [{
            "label": item.get("label"), "verdict": item.get("verdict"), "severity": item.get("severity"),
            "text": showroom_text(item.get("text")), "unattributed_pct": item.get("unattributed_pct"),
            "shares": [{"label": share.get("label"), "health": share.get("health"), "pct": share.get("pct")}
                       for share in item.get("shares") or []],
            "reactions": [{"label": reaction.get("label"), "health": reaction.get("health"),
                           "steered_after_seconds": reaction.get("steered_after_seconds"),
                           "impaired_for_seconds": reaction.get("impaired_for_seconds"),
                           "within_target": None if reaction.get("steered_after_seconds") is None or not targets.get("steering_max_s")
                                            else reaction["steered_after_seconds"] <= targets["steering_max_s"]}
                          for reaction in item.get("reactions") or [] if reaction.get("was_used")],
        } for item in steering.get("classes") or []],
    }


showroom_app = create_showroom_app(showroom_snapshot, BRANDING_DIR)


def showroom_listener():
    """The showroom's bind address and port; port 0 disables it."""
    port = int(os.environ.get("NETEM_SHOWROOM_PORT", "8082"))
    if port != 0 and (not 1 <= port <= 65535 or port == 8081):
        raise ValueError("NETEM_SHOWROOM_PORT must be 0 (disabled) or a port other than 8081")
    return os.environ.get("NETEM_SHOWROOM_HOST", "0.0.0.0"), port


def showroom_url():
    """Where an operator's browser reaches the showroom, or None when it is disabled."""
    try:
        host, port = showroom_listener()
    except ValueError:
        return None
    if not port:
        return None
    if host in ("", "0.0.0.0", "::"):
        # Bound to every address: the one the operator used for this page reaches it too.
        host = urlsplit(request.host_url).hostname or "localhost"
    return f"http://{f'[{host}]' if ':' in host else host}:{port}/"


def run_servers():
    """Share runtime state and workers; bind the viewer to its own HTTP listener."""
    from werkzeug.serving import make_server

    host, port = showroom_listener()
    viewer = make_server(host, port, showroom_app, threaded=True) if port else None
    viewer_thread = None
    try:
        restore_runtime_state()
        start_background_workers()
        if viewer:
            viewer_thread = threading.Thread(target=viewer.serve_forever,
                                             name="netem-showroom", daemon=True)
            viewer_thread.start()
        app.run(host="0.0.0.0", port=8081, debug=False)
    finally:
        if viewer:
            if viewer_thread and viewer_thread.is_alive():
                viewer.shutdown()
                viewer_thread.join(timeout=5)
            viewer.server_close()


if __name__ == "__main__":
    run_servers()
