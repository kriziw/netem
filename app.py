#!/usr/bin/env python3
import copy
import csv
import io
import ipaddress
import hashlib
import hmac
import http.client
import secrets
import shlex
import tempfile
import json
import math
import os
import platform as system_platform
import re
import shutil
import socket
import sqlite3
import ssl
import sys
import subprocess
import threading
import time
import urllib.parse
import uuid
from pathlib import Path
from urllib.parse import urlsplit
from urllib import error as urllib_error
from urllib import request as urllib_request

import calibration
from experience_groups import attach_experience_groups
from experience_presentation import INVALID_EVIDENCE, display_score, target_details
import bottlenecks
import calibration_log
import calibration_trace
import host_health
import platform_test
import test_records
import site_catalog
import application_monitoring
import access_control
from component_updates import ComponentChecks
import showcase_summary
from presentation_cache import PresentationCache
from showroom import create_showroom_app
import branding
import appliance_platform

from markupsafe import escape

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    get_flashed_messages,
    jsonify,
    Response,
    send_file,
    send_from_directory,
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
# Brand packs made in Settings -> Branding stay private: runtime/ is not part of the repository.
BRANDS_DIR = Path(os.environ.get("NETEM_BRANDS_DIR") or RUNTIME_DIR / "brands")
BRAND_STORE = branding.init_app(app, branding.BrandStore(BRANDS_DIR, BRANDING_DIR))
# Brand uploads are the largest requests: two fonts, a logo and a favicon of up to 2 MB each.
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024

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
        "branding_activate", "branding_save", "branding_delete", "branding_import", "appliance_save",
        "site_save", "site_apply_wan", "site_run", "site_stop", "time_format_save", "showcase_video_save", "lab_scenario_phase",
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

TELEMETRY_SAMPLE_SECONDS = 1.0
TELEMETRY_RETENTION_HOURS = 168
MAX_PROBES = 20

RUNTIME_LOCK = threading.Lock()
ACTIVE_FAULTS = {}
RUNTIME_EFFECTIVE = {}
PROFILE_APPLY_LOCK = threading.RLock()
APPLIED_QDISCS = {}
QDISC_GENERATIONS = {}
EVENT_LOG = []
LAB_SESSIONS = []
ACTIVE_SESSION = {
    "active": False,
    "id": None,
    "name": None,
    "started_at": None,
    "site": None,
    # Captured when the session starts and fixed until it ends.
    "deadline": None,
    "appliance": None,
    "wan": None,
    "tests": {},
}
# A session ends by itself this long after it starts, unless the operator completes it sooner.
SESSION_MAX_S = 6 * 3600
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
    # The announcement before a test changes anything: what it does, and when it starts.
    "intro": False,
    "intro_clock": None,
    "description": None,
    "checks": [],
    "unsaved": False,
    "phase_jump": None,
    "phase_revision": 0,
    "navigating": False,
    "finishing": False,
}
# Every test is announced this long before it starts, so the showroom shows what is coming.
TEST_INTRO_S = 30
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
    for name in ("util_pct", "queue_drops_ps", "injected_drops_ps", "drop_pct", "backlog_bytes",
                 "queue_memory_drops_ps", "queue_packet_drops_ps", "queue_other_drops_ps")
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


# A file can be updated before this process restarts; status must name the running code.
PROCESS_VERSION = get_app_version()


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
    if path == '/api/v1/workloads/start' and method == 'POST' and isinstance(payload, dict):
        payload = application_monitoring.start_payload(payload, simulator_catalog())
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


@app.get('/simulator/', defaults={'panel_path': ''})
@app.get('/simulator/<path:panel_path>')
def simulator_controller(panel_path):
    # Preserve bookmarks only. No agent HTML, assets or browser forms are relayed.
    destination = 'experience' if panel_path == 'dem' else 'maintenance' if panel_path.startswith('settings/system') or panel_path.startswith('settings/api') else 'network'
    if panel_path in ('', 'workloads'):
        return redirect(url_for('tests') + '#corporate-traffic')
    return redirect(url_for('traffic_settings', section=destination))


PRESENTATION_READS = PresentationCache()


def presentation_source():
    # Credentials and endpoint changes cannot reuse another simulator's data.
    integration = traffic_generator_config()
    raw = [str(CONFIG_PATH), integration, hashlib.sha256(traffic_generator_api_key().encode()).hexdigest()]
    return hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()


def pending_simulator_dem(issue, window=15):
    # Keep the same nullable fields as a valid status response, including during
    # phase transitions; templates must not format undefined measurements.
    dem = {key: None for key in ('experience_score', 'availability_pct', 'p50_ms', 'p95_ms',
                                 'interactive_p95_ms', 'requests_per_second', 'failures_per_second')}
    dem.update(requests=0, scoring_complete=False, business_acceptance='unknown',
               evidence_issues=[issue], collecting_evidence=True, configured_window_seconds=window)
    return dem


def traffic_generator_snapshot(include_catalog=False, cached=False):
    source = presentation_source()
    fallback = traffic_generator_empty_snapshot()
    if not fallback['configured']:
        return fallback
    loader = _traffic_generator_snapshot
    if cached:
        result = PRESENTATION_READS.read('status', source, loader, fallback, max_age=1, max_stale=15)
    else:
        result = PRESENTATION_READS.fresh('status', source, loader)
    if cached:
        poll = result.get('poll') or {}
        result['connection_state'] = ('connected' if result.get('connected') else
            'disconnected' if result.get('error') else 'checking')
        # Retain a recent connection/run label while refreshing; live measurements
        # expire sooner. Cache age is not evidence that the connection failed.
        if result.get('connected') and (poll.get('age_seconds') or 0) > 3:
            result['connection_state'] = 'refreshing'
            result['status']['users'] = None
            result['status']['dem'] = pending_simulator_dem('status_refresh_pending')
    if result.get('connected') and result.get('status'):
        with RUNTIME_LOCK:
            expected = SCENARIO_STATE.get('workload_run_id') if SCENARIO_STATE.get('active') and SCENARIO_STATE.get('workload_supervised') else None
        if expected and ((result['status'].get('run') or {}).get('run_id') != expected or
                         (result['status'].get('dem') or {}).get('live_phase_id') != test_live_context()['phase_id'] or
                         bool((result['status'].get('dem') or {}).get('live_paused')) != bool(SCENARIO_STATE.get('paused'))):
            result['status']['dem'] = pending_simulator_dem('live_phase_pending', SCENARIO_STATE.get('live_window_seconds', 15))
        attach_experience_groups((result["status"] or {}).get("dem"), showroom_app_label)
    if include_catalog and result.get('connected'):
        result['catalog'] = simulator_catalog(cached=cached)
    return result


def traffic_generator_empty_snapshot():
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
    return result


def _traffic_generator_snapshot():
    result = traffic_generator_empty_snapshot()
    if not result['configured']:
        return result
    try:
        # The simulator may wait up to five seconds for its DEM aggregation job.
        # This read runs off the UI request path; leave time for that valid reply.
        result["status"] = traffic_generator_request("/api/v1/status", timeout=7.0)
        result["connected"] = True
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


from branding_sync import BrandingSync
BRANDING_SYNC = BrandingSync(BRAND_STORE, traffic_generator_request, BACKGROUND_STOP)


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
    text = "Traffic path not ready: " + (path.get("message") or "simulated traffic would not cross the appliance.")
    bridges = path.get("wan_bridges") or []
    if bridges:
        text += " NetEm found " + "; ".join(f"{problem['label']}: {', '.join(problem['issues'])}" for problem in bridges) + "."
    return text


def traffic_path_readiness(max_age=TRAFFIC_PATH_CACHE_SECONDS, cached=False):
    """Whether the simulator's traffic reaches the target through the appliance (cached briefly)."""
    source = presentation_source()
    if cached:
        fallback = dict(configured=bool(traffic_generator_config().get('host')), available=False,
                        ready=None, repairable=False, message='Traffic path check is pending.',
                        summary='Traffic path check is pending.')
        return PRESENTATION_READS.read('path', source, lambda: traffic_path_readiness(max_age=0),
                                       fallback, max_age=15, max_stale=30)
    cached = dict(TRAFFIC_PATH_CACHE)
    if cached.get('source') != source:
        cached = dict(checked=None, payload=None)
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
    payload["wan_bridges"] = [{key: problem[key] for key in ("link_id", "label", "issues")} for problem in wan_bridge_snapshot()]
    payload["summary"] = traffic_path_summary(payload)
    payload["checked_at"] = time.time()
    TRAFFIC_PATH_CACHE.update(checked=time.monotonic(), payload=payload, source=source)
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
    check_wan_bridges()
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
    if expected_run and not (status or {}).get("status"):
        return dict(common, id="workload_unconfirmed", severity="warn",
                    title="Simulator status temporarily unavailable",
                    detail="NetEm could not verify the test workload. A missing response does not establish that it stopped.",
                    hint="Check the simulator connection if this persists.")
    run = (status or {}).get("run") or {}
    if (run.get("traffic_path") or {}).get("mode") == "direct_baseline" and not expected_run:
        return None
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


# ---------- Appliance under test ----------
#
# Clients see the SD-WAN vendor next to the MSP brand. NetEm takes the vendor of the appliance
# selected in the Traffic Simulator; Settings can name it instead. The vendor logos are part of
# the application (static/vendors, sources in its README), unlike private MSP brand packs.

SDWAN_VENDORS = {
    "fortinet": {"name": "Fortinet", "product": "FortiGate Secure SD-WAN", "aliases": ("fortinet", "fortigate")},
    "velocloud": {"name": "VeloCloud", "product": "VeloCloud SD-WAN", "aliases": ("velocloud", "vmwarevelocloud", "aristavelocloud")},
    "cisco": {"name": "Cisco", "product": "Cisco SD-WAN", "aliases": ("cisco", "meraki", "viptela")},
    "hpe-aruba": {"name": "HPE Aruba Networking", "product": "EdgeConnect SD-WAN", "aliases": ("hpearuba", "aruba", "edgeconnect", "silverpeak")},
    "palo-alto": {"name": "Palo Alto Networks", "product": "Prisma SD-WAN", "aliases": ("paloalto", "prisma")},
    "versa": {"name": "Versa Networks", "product": "Versa SASE", "aliases": ("versa",)},
    "juniper": {"name": "Juniper Networks", "product": "Session Smart SD-WAN", "aliases": ("juniper", "mist", "128technology")},
    "check-point": {"name": "Check Point", "product": "Quantum SD-WAN", "aliases": ("checkpoint",)},
    "sophos": {"name": "Sophos", "product": "Sophos Firewall SD-WAN", "aliases": ("sophos",)},
    "forcepoint": {"name": "Forcepoint", "product": "Forcepoint SD-WAN", "aliases": ("forcepoint",)},
    "barracuda": {"name": "Barracuda", "product": "CloudGen Firewall", "aliases": ("barracuda",)},
    "peplink": {"name": "Peplink", "product": "SpeedFusion SD-WAN", "aliases": ("peplink", "pepwave")},
    "huawei": {"name": "Huawei", "product": "Huawei SD-WAN", "aliases": ("huawei",)},
    "ekinops": {"name": "Ekinops", "product": "OneOS SD-WAN", "aliases": ("ekinops", "oneaccess")},
    "cato": {"name": "Cato Networks", "product": "Cato SASE Cloud", "aliases": ("cato",)},
    "sonicwall": {"name": "SonicWall", "product": "SonicWall SD-WAN", "aliases": ("sonicwall",)},
    "watchguard": {"name": "WatchGuard", "product": "WatchGuard SD-WAN", "aliases": ("watchguard",)},
}
APPLIANCE_CACHE = {"checked": None, "payload": None}
APPLIANCE_CACHE_SECONDS = 60


def vendor_id(label):
    """The built-in vendor for a label such as the simulator's "Palo Alto Networks"."""
    key = re.sub(r"[^a-z0-9]", "", str(label or "").lower())
    return next((vendor for vendor, info in SDWAN_VENDORS.items()
                 if key and any(key.startswith(alias) for alias in info["aliases"])), None)


def appliance_view(vendor, model=None, name=None, source=None, label=None):
    info = SDWAN_VENDORS.get(vendor) or {}
    return {"vendor": vendor if info else None,
            "vendor_name": info.get("name") or (str(label)[:60] if label and label != "Other" else None),
            "product": info.get("product"), "model": str(model)[:60] if model else None,
            "name": str(name)[:60] if name else None, "source": source}


def appliance_under_test(max_age=APPLIANCE_CACHE_SECONDS, cached=False):
    """The SD-WAN appliance being tested: from Settings, else the simulator's selected appliance."""
    if "APPLIANCE_PLATFORM" in globals():
        selection = APPLIANCE_PLATFORM.store.state("selection")
        if selection.get("mode") in ("direct_netem", "direct_baseline"):
            label = "Direct NetEm path" if selection["mode"] == "direct_netem" else "Clean router baseline"
            return dict(appliance_view(None, name=selection.get("name"), source=selection["mode"]), vendor_name=label, product=None)
        selected = APPLIANCE_PLATFORM.selected()
        if selected:
            return appliance_view(selected["vendor"], selected.get("model"), selected["name"], "inventory")
    cfg = load_config()
    setting = cfg.get("appliance") if isinstance(cfg.get("appliance"), dict) else {}
    if setting.get("vendor") in SDWAN_VENDORS:
        return appliance_view(setting["vendor"], setting.get("model"), None, "setting")
    source = presentation_source()
    if cached:
        return PRESENTATION_READS.read('appliance', source, lambda: appliance_under_test(max_age=0),
                                       appliance_view(None), max_age=60, max_stale=120)
    cached = dict(APPLIANCE_CACHE)
    if cached.get('source') != source:
        cached = dict(checked=None, payload=None)
    if cached["payload"] is not None and cached["checked"] is not None and time.monotonic() - cached["checked"] <= max_age:
        return cached["payload"]
    payload = appliance_view(None)
    if traffic_generator_config(cfg).get("host") and traffic_generator_api_key():
        try:
            network = traffic_generator_request("/api/v1/network", timeout=8.0)
            selected = network.get("selected") or {}
            match = next((row for row in network.get("appliances") or [] if selected
                          and all(row.get(key) == selected.get(key) for key in ("interface", "gateway", "target"))), None)
            if match:
                payload = appliance_view(vendor_id(match.get("vendor")), match.get("model") or setting.get("model"),
                                         match.get("name"), "simulator", label=match.get("vendor"))
        except RuntimeError:
            pass
    APPLIANCE_CACHE.update(checked=time.monotonic(), payload=payload, source=source)
    return payload


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
                if "experience_policy" in value:
                    policy = value["experience_policy"]
                    if not isinstance(policy, dict) or len(policy) > 30:
                        raise ValueError(f"Step {index}: experience_policy must be an application target object.")
                    try:
                        if len(json.dumps(policy, allow_nan=False)) > 32000:
                            raise ValueError()
                    except (TypeError, ValueError):
                        raise ValueError(f"Step {index}: invalid experience policy.") from None
                    cleaned["experience_policy"] = copy.deepcopy(policy)
                if 'application_checks' in value:
                    if not isinstance(value['application_checks'], bool):
                        raise ValueError(f'Step {index}: application_checks must be a boolean.')
                    cleaned['application_checks'] = value['application_checks']
                for field in ("generation_mode", "paced_mbps", "sparse_share", "seed", "rate_down_mbps", "rate_up_mbps", "media_pattern", "media_session_s", "completion_grace_s", "live_window_seconds"):
                    if field in value:
                        cleaned[field] = value[field]
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
    with PROFILE_APPLY_LOCK:
        return _apply_selected_profile(link, presets)


def _apply_selected_profile(link: dict, presets: dict):
    """Apply either the quality-derived or custom profile stored on one WAN."""
    inner = link.get("inner")
    outer = link.get("outer")
    if not inner:
        return False, "Missing interface or preset", {}

    effective = get_effective_profile(link, presets)
    if not effective:
        return False, "Missing interface or preset", {}

    link_id = link.get("id") or link.get("bridge") or inner
    previous = copy.deepcopy(RUNTIME_EFFECTIVE.get(link_id) or {})
    if (effective.get("jitter_ms", 0) > 0 or effective.get("reorder_pct", 0) > 0) and effective.get("delay_ms", 0) <= 0:
        return False, "Jitter and reordering require a positive delay", effective
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
            "updated_at": time.time(), "apply_status": "applied", "desired": dict(effective),
            "updated_monotonic": time.monotonic(),
            "generation": previous.get("generation", 0) + (previous.get("effective") != effective),
        }
        return True, "OK", effective

    old = previous.get("effective")
    restored = False
    if old:
        down_restored, _ = apply_netem(inner, old["delay_ms"], old["jitter_ms"], old["loss_pct"], old["download_mbit"],
                                     old.get("loss_correlation_pct", 0), old.get("duplicate_pct", 0),
                                     old.get("corrupt_pct", 0), old.get("reorder_pct", 0))
        up_restored = not outer or apply_netem(outer, 0, 0, 0, old["upload_mbit"])[0]
        restored = down_restored and up_restored
    RUNTIME_EFFECTIVE[link_id] = {**previous, "desired": dict(effective),
                                "effective": old if restored else {}, "apply_status": "rolled_back" if restored else "unknown",
                                "apply_error": f"download: {msg_down}; upload: {msg_up}", "updated_at": time.time()}
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
    QDISC_GENERATIONS[ifname] = QDISC_GENERATIONS.get(ifname, 0) + 1
    APPLIED_QDISCS.pop(ifname, None)
    run_cmd(f"{TC} qdisc del dev {ifname} root")


# Resource guards scale generously with configured line rate, separately from pacing.
# CoDel controls persistent queue delay; memory exhaustion must not model WAN quality.
SHAPER_QUEUE_BUDGET_MS = 1000
SHAPER_MEMORY_HEADROOM = 4
SHAPER_QUEUE_MIN_MEMORY = 8 * 1024 * 1024
SHAPER_QUEUE_MAX_MEMORY = 512 * 1024 * 1024
SHAPER_QUEUE_MIN_PACKETS = 8192
SHAPER_QUEUE_MAX_PACKETS = 1048576
SHAPER_BURST_MS = 10
UNSHAPED_SIZING_MBIT = 1000


def shaper_queue_limits(rate_mbit: float):
    """One second of line-rate traffic, with allocation headroom and resource guards.

    Packet capacity uses a conservative 256-byte reference packet. Memory
    allocation varies by kernel and offloads; this is not a queue-delay guarantee.
    """
    rate = rate_mbit if rate_mbit and rate_mbit > 0 else UNSHAPED_SIZING_MBIT
    wire_bytes = rate * 125 * SHAPER_QUEUE_BUDGET_MS
    packets = min(SHAPER_QUEUE_MAX_PACKETS, max(SHAPER_QUEUE_MIN_PACKETS, math.ceil(wire_bytes / 256)))
    memory = min(SHAPER_QUEUE_MAX_MEMORY, max(SHAPER_QUEUE_MIN_MEMORY, math.ceil(wire_bytes * SHAPER_MEMORY_HEADROOM)))
    return packets, memory


def shaper_queue_sizes(rate_mbit: float, delay_ms: float, jitter_ms: float):
    """Generous packet/memory guards, with a rate-scaled pacing burst.

    fq_codel memory_limit accounts kernel allocation bytes, not wire bytes.
    Do not derive that resource guard from a few milliseconds of payload.
    The intentional-delay queue also needs room for the bandwidth-delay product.
    """
    bytes_per_ms = (rate_mbit if rate_mbit and rate_mbit > 0 else UNSHAPED_SIZING_MBIT) * 125
    hold_ms = max(0.0, float(delay_ms or 0)) + 3 * max(0.0, float(jitter_ms or 0))
    packets, memory = shaper_queue_limits(rate_mbit)
    netem_limit = max(packets, math.ceil(bytes_per_ms * hold_ms * 1.5 / 1000))
    burst = max(3200, int(bytes_per_ms * SHAPER_BURST_MS))
    return netem_limit, burst, memory


def apply_netem(ifname, delay_ms, jitter_ms, loss_pct, rate_mbit,
                loss_correlation_pct=0.0, duplicate_pct=0.0, corrupt_pct=0.0, reorder_pct=0.0):
    values = (delay_ms, jitter_ms, loss_pct, rate_mbit, loss_correlation_pct, duplicate_pct, corrupt_pct, reorder_pct)
    if not all(isinstance(value, (int, float)) and math.isfinite(value) and value >= 0 for value in values):
        return False, "Impairments must be finite, nonnegative numbers"
    if jitter_ms > 0 and delay_ms <= 0:
        return False, "Jitter requires a positive delay"
    if reorder_pct > 0 and delay_ms <= 0:
        return False, "Reordering requires a positive delay"
    with PROFILE_APPLY_LOCK:
        previous = APPLIED_QDISCS.get(ifname)
        observed = None
        if previous:
            rc, observed, _ = run_cmd(f"{TC} qdisc show dev {ifname}")
            if rc == 0 and observed.strip() and observed == previous["observed"] and previous["values"] == values:
                return True, "OK"
        in_place = bool(previous and observed == previous["observed"] and
                        bool(previous["values"][3]) == bool(rate_mbit))
        result = _apply_netem(ifname, *values, change=in_place) if in_place else _apply_netem(ifname, *values)
        APPLIED_QDISCS.pop(ifname, None)
        if result[0]:
            rc, observed, _ = run_cmd(f"{TC} qdisc show dev {ifname}")
            if rc == 0 and observed.strip():
                APPLIED_QDISCS[ifname] = {"values": values, "observed": observed}
        return result


def _apply_netem(
    ifname: str,
    delay_ms: float,
    jitter_ms: float,
    loss_pct: float,
    rate_mbit: float,
    loss_correlation_pct: float = 0.0,
    duplicate_pct: float = 0.0,
    corrupt_pct: float = 0.0,
    reorder_pct: float = 0.0,
    change: bool = False,
):
    """
    Apply netem + optional tbf on interface.

    Advanced impairment controls are intentionally bounded to tc/netem
    primitives and operate only on configured lab interfaces.
    """
    # Preserve backlog and counters when the verified hierarchy is unchanged.
    verb = "change" if change else "add"
    if not change:
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

    if change:
        parts = ["netem", f"limit {netem_limit}", f"delay {delay_ms:.1f}ms {jitter_ms:.1f}ms",
                 f"loss {loss_pct:.3f}% {min(100.0, loss_correlation_pct):.3f}%",
                 f"duplicate {min(100.0, duplicate_pct):.3f}%", f"corrupt {min(100.0, corrupt_pct):.3f}%",
                 f"reorder {min(100.0, reorder_pct):.3f}%"]
    netem_cmd = f"{TC} qdisc {verb} dev {ifname} root handle 1:0 " + " ".join(parts)
    rc, out, err = run_cmd(netem_cmd)
    if rc != 0:
        return False, f"Failed to apply netem: {err or out or 'unknown error'}"

    if rate_mbit and rate_mbit > 0:
        rate_value = int(round(rate_mbit))
        rate_str = f"{rate_value}mbit"
        # TBF paces; its child fair queue holds the backlog. Keep resource guards
        # generous at low WAN rates as well. CoDel may still drop to control delay.
        tbf_cmd = (
            f"{TC} qdisc {verb} dev {ifname} parent 1:1 handle 10: tbf "
            f"rate {rate_str} buffer {burst_bytes} limit {2 * limit_bytes}"
        )
        rc2, out2, err2 = run_cmd(tbf_cmd)
        if rc2 != 0:
            return False, f"Netem OK, but tbf failed: {err2 or out2 or 'unknown error'}"
        fq_cmd = (
            f"{TC} qdisc {verb} dev {ifname} parent 10:1 handle 20: fq_codel "
            f"limit {shaper_queue_limits(rate_mbit)[0]} memory_limit {limit_bytes} target 5ms interval 100ms"
        )
        rc3, out3, err3 = run_cmd(fq_cmd)
        if rc3 != 0:
            return False, f"Netem and tbf OK, but fq_codel failed: {err3 or out3 or 'unknown error'}"

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


# ---------- WAN bridge watchdog ----------
#
# Each WAN is a Linux bridge joining the appliance side (inner) and the provider side (outer).
# When a port drops out of its bridge (a failed restore, or another tool reconfiguring the
# VM's interfaces), the WAN passes nothing while its shaping still looks applied. NetEm checks
# the bridges every few seconds, puts ports back while it manages the bridges (Setup →
# Restore WAN bridges), and reports what it found in the diagnosis and the traffic path check.

BRIDGE_CHECK_SECONDS = 10
BRIDGE_REPAIR_BACKOFF_SECONDS = 60
BRIDGE_LOCK = threading.Lock()
BRIDGE_STATE = {"checked_at": None, "problems": [], "attempts": {}, "reported": {}}


def wan_bridge_problems(cfg):
    """Configured WANs whose bridge is missing or down, or whose ports are missing, down, without
    a link or outside the bridge. None when the interfaces cannot be read."""
    rc, out, _err = run_cmd(f"{IP} -j link show")
    if rc != 0:
        return None
    try:
        interfaces = {item.get("ifname"): item for item in json.loads(out or "[]")}
    except (ValueError, AttributeError):
        return None
    problems = []
    for link in cfg.get("wan_links", []):
        bridge = link.get("bridge")
        ports = [dev for dev in (link.get("inner"), link.get("outer")) if dev]
        if not bridge or not ports:
            continue
        state = {"bridge_missing": bridge not in interfaces, "bridge_down": False,
                 "missing": [], "outside": [], "down": [], "no_link": []}
        if not state["bridge_missing"]:
            state["bridge_down"] = "UP" not in (interfaces[bridge].get("flags") or [])
        for dev in ports:
            item = interfaces.get(dev)
            if item is None:
                state["missing"].append(dev)
                continue
            flags = item.get("flags") or []
            if item.get("master") != bridge:
                state["outside"].append(dev)
            if "UP" not in flags:
                state["down"].append(dev)
            elif "LOWER_UP" not in flags:
                state["no_link"].append(dev)
        issues = ([f"bridge {bridge} is missing"] if state["bridge_missing"] else []) + \
                 ([f"bridge {bridge} is down"] if state["bridge_down"] else []) + \
                 [f"{dev} is missing" for dev in state["missing"]] + \
                 [f"{dev} is not in {bridge}" for dev in state["outside"]] + \
                 [f"{dev} is down" for dev in state["down"]] + \
                 [f"{dev} has no link" for dev in state["no_link"]]
        if issues:
            # A missing interface or a port without a link needs the VM's NICs looked at; the rest NetEm can fix.
            fixable = state["bridge_missing"] or state["bridge_down"] or bool(state["outside"] or state["down"])
            problems.append(dict(state, link_id=link.get("id"), label=link.get("name") or link.get("id"),
                                 bridge=bridge, ports=ports, issues=issues, fixable=fixable))
    return problems


def repair_wan_bridge(problem):
    """Put a WAN bridge back together without disturbing ports that are already in place."""
    bridge = problem["bridge"]
    errors = []

    def step(command):
        rc, _out, err = run_cmd(command)
        if rc != 0:
            errors.append(err or command)

    if problem["bridge_missing"]:
        step(f"{IP} link add name {shlex.quote(bridge)} type bridge")
    for dev in problem["ports"]:
        if dev in problem["missing"]:
            continue
        if problem["bridge_missing"] or dev in problem["outside"]:
            step(f"{IP} link set {shlex.quote(dev)} master {shlex.quote(bridge)}")
        if dev in problem["down"]:
            step(f"{IP} link set {shlex.quote(dev)} up")
    step(f"{IP} link set {shlex.quote(bridge)} up")
    return errors


def check_wan_bridges():
    """Find broken WAN bridges, repair them while NetEm manages bridges, and log each change once.
    The telemetry loop and a starting test may both call it; one check runs at a time."""
    with BRIDGE_LOCK:
        return _check_wan_bridges()


def _check_wan_bridges():
    cfg = load_config()
    problems = wan_bridge_problems(cfg)
    if problems is None:
        return None
    manage = cfg.get("restore_bridges_on_startup", True)
    now = time.monotonic()
    attempted = []
    for problem in problems:
        last = BRIDGE_STATE["attempts"].get(problem["link_id"])
        if not manage or not problem["fixable"] or (last is not None and now - last < BRIDGE_REPAIR_BACKOFF_SECONDS):
            continue
        BRIDGE_STATE["attempts"][problem["link_id"]] = now
        errors = repair_wan_bridge(problem)
        log_event("bridge", f"{problem['label']}: bridge broken ({'; '.join(problem['issues'])}), repairing",
                  link_id=problem["link_id"], issues=problem["issues"], errors=errors[:4])
        attempted.append(problem)
    if attempted:
        problems = wan_bridge_problems(cfg) or []
    remaining = {problem["link_id"]: problem for problem in problems}
    for problem in attempted:
        if problem["link_id"] not in remaining:
            BRIDGE_STATE["attempts"].pop(problem["link_id"], None)
            log_event("bridge", f"{problem['label']}: bridge repaired", link_id=problem["link_id"])
    # Problems NetEm leaves alone are logged when they appear, change or clear.
    reported = BRIDGE_STATE["reported"]
    for link_id, problem in remaining.items():
        if reported.get(link_id) != problem["issues"] and not any(item["link_id"] == link_id for item in attempted):
            log_event("bridge", f"{problem['label']}: bridge broken ({'; '.join(problem['issues'])})",
                      link_id=link_id, issues=problem["issues"])
        reported[link_id] = problem["issues"]
    for link_id in [link_id for link_id in reported if link_id not in remaining]:
        reported.pop(link_id)
    BRIDGE_STATE.update(checked_at=time.time(), problems=problems)
    return problems


def wan_bridge_snapshot():
    with BRIDGE_LOCK:
        return list(BRIDGE_STATE["problems"])


def bridge_finding(problems):
    if not problems:
        return None
    labels = [problem["label"] for problem in problems]
    names = labels[0] if len(labels) == 1 else ", ".join(labels[:-1]) + " and " + labels[-1]
    return {"source": "platform", "id": "wan_bridge", "severity": "bad",
            "title": f"{names} pass{'es' if len(labels) == 1 else ''} no traffic: {'its bridge is' if len(labels) == 1 else 'their bridges are'} broken",
            "detail": "; ".join(f"{problem['label']}: {', '.join(problem['issues'])}" for problem in problems) + ".",
            "wans": [], "unattributed": 0, "candidates": [],
            "hint": "NetEm puts ports back into their bridge while Setup → Restore WAN bridges is on. "
                    "A missing interface or one without a link needs its virtual NIC connected in Proxmox."}


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
    if unsaved_test_active():
        event['temporary'] = True
    EVENT_LOG.append(event)
    record_id = globals().get('TEST_RECORDER', {}).get('test_id')
    if record_id and TEST_RECORDER.get('active'):
        try:
            test_records.append(RUNTIME_DIR, record_id, 'event', event)
        except OSError:
            TEST_RECORDER['record_error'] = 'Test journal could not be written'
    del EVENT_LOG[:-500]
    if event.get('temporary'):
        return

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



def session_appliance_snapshot():
    """The SD-WAN appliance under test, as a session keeps it: captured once, at start."""
    try:
        appliance = appliance_under_test() or {}
    except Exception:
        appliance = {}
    return {key: appliance.get(key) for key in ("vendor", "vendor_name", "product", "model", "name", "source")
            if appliance.get(key) is not None}


def session_wan_roles(cfg, primary=None, backup=None):
    """The WANs a session's primary and backup site-test roles use. Two configured WANs are chosen by default. A lab
    with fewer, and no explicit choice, has no roles: the session still starts, and its site tests say why they cannot."""
    ids = [link.get("id") for link in cfg.get("wan_links", [])]
    if len(ids) < 2:
        return None
    primary = primary or (ids[0] if ids else "")
    backup = backup or (ids[1] if len(ids) > 1 else "")
    if not get_link(cfg, primary):
        raise ValueError("Choose the primary WAN for this session's site tests.")
    if not get_link(cfg, backup):
        raise ValueError("Choose the backup WAN for this session's site tests.")
    if primary == backup:
        raise ValueError("Primary and backup must be different WANs.")
    labels = {link.get("id"): link.get("name") or link.get("id") for link in cfg.get("wan_links", [])}
    return {"primary": {"link_id": primary, "label": labels[primary]},
            "backup": {"link_id": backup, "label": labels[backup]}}


def end_session(reason):
    """End the lab session. Running site tests stop and restore their WAN, the session report is built and the
    record is closed. reason is "completed" when the operator stops it, "timed_out" once six hours have passed."""
    with RUNTIME_LOCK:
        if not ACTIVE_SESSION["active"]:
            return None
        session_id = ACTIVE_SESSION["id"]
        session_name = ACTIVE_SESSION["name"]
        plan_running = bool(SITE_PLAN_STATE.get("active"))
        scenario_running = bool(SCENARIO_STATE.get("active"))
    if plan_running:
        SITE_PLAN_STOP.set()
    if plan_running or scenario_running:
        SCENARIO_STOP.set()
    log_event("session", f"Lab session {'reached its six-hour limit' if reason == 'timed_out' else 'completed'}: {session_name}",
              session_id=session_id, reason=reason)
    ended_at = time.time()
    with RUNTIME_LOCK:
        for item in reversed(LAB_SESSIONS):
            if item.get("id") == session_id:
                item["ended_at"] = ended_at
                item["status"] = reason
                break

    report = build_session_report(session_id, end_time=ended_at)

    with RUNTIME_LOCK:
        for item in reversed(LAB_SESSIONS):
            if item.get("id") == session_id:
                item["report"] = report
                break
        ACTIVE_SESSION.update({"active": False, "id": None, "name": None, "started_at": None, "deadline": None,
                               "site": None, "appliance": None, "wan": None, "tests": {}})
        save_session_history()
    return {"name": session_name, "report": report, "reason": reason}


def expire_session_if_due(now=None):
    """End the session once its six-hour limit has passed, whoever is watching. Returns True when it ended."""
    with RUNTIME_LOCK:
        deadline = ACTIVE_SESSION.get("deadline") if ACTIVE_SESSION.get("active") else None
    if deadline is None or (now if now is not None else time.time()) < deadline:
        return False
    return end_session("timed_out") is not None


def set_session_test(test, status, summary=None):
    """Record a site test's latest state in the running session, for the Command Centre and the session summary."""
    with RUNTIME_LOCK:
        if not ACTIVE_SESSION.get("active"):
            return
        tests = ACTIVE_SESSION.setdefault("tests", {})
        record = tests.get(test["id"]) or {}
        record.update(name=test["name"], role=test["role"], status=status,
                      finished_at=None if status == "running" else time.time())
        if summary is not None:
            record["summary"] = summary
        tests[test["id"]] = record
        for item in LAB_SESSIONS:
            if item.get("id") == ACTIVE_SESSION.get("id"):
                item["tests"] = {key: {field: value.get(field) for field in ("name", "status", "finished_at")}
                                 for key, value in tests.items()}
                break
    if status != "running":
        save_session_history()


def session_site_tests():
    """The active session's site tests, each with the state it has in this session, for the Command Centre."""
    session = session_snapshot()
    if not session.get("active") or not session.get("site"):
        return []
    plan = active_site_plan({"site_profile": {key: session["site"][key] for key in SITE_FIELDS}})
    if not plan:
        return []
    recorded = session.get("tests") or {}
    return [{"id": test["id"], "name": test["name"], "role": test["role"],
             "description": test.get("description") or "",
             "status": (recorded.get(test["id"]) or {}).get("status") or "pending"}
            for test in plan["tests"]]


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
            # tc prints drop_overmemory only when nonzero. Advertising the
            # memory_limit option establishes support for this counter.
            if current["kind"] == "fq_codel" and re.search(r"\bmemory_limit\s", line):
                current["drop_overmemory"] = 0
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
        for key in ("drop_overlimit", "drop_overmemory", "ecn_mark", "ce_mark", "memory_used"):
            match = re.search(r"\b" + key + r"\s+(\d+)", line)
            if match:
                current[key] = int(match.group(1))
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
    fq = next((item for item in qdiscs if item["kind"] == "fq_codel"), None)
    # The fair queue holds what the rate limiter paces, so the rate limiter's backlog is already counted in it.
    # Netem root backlog semantics differ by kernel. Use only the rate queue
    # to estimate additional serialization delay, excluding intentional delay backlog.
    backlog = (fq or tbf or {}).get("backlog_bytes") or 0
    return {
        "queues": {"intentional_delay": netem, "rate_limiter": tbf, "fair_queue": fq},
        "queue_accounting": {"backlog_basis": "rate queue only", "backlog_unit": "bytes",
                              "memory_unit": "kernel allocation bytes", "injected_loss_basis": "counter difference estimate",
                              "fair_queue_drops": {key: fq.get(key) for key in ("drops", "drop_overlimit", "drop_overmemory")} if fq else None},
        "kind": root["kind"],
        "generation": QDISC_GENERATIONS.get(ifname, 0),
        "packets": root["packets"],
        "netem_drops": (netem or {}).get("drops") or 0,
        "queue_drops": (fq or tbf or {}).get("drops") or 0,
        "backlog_bytes": backlog,
        "shaped": tbf is not None,
    }


def qdisc_rates(current, previous, dt):
    """Per-second drops for one direction, or None across a reset or unknown read.

    The queue that holds traffic when the link is full (the fair queue behind the rate
    limiter, or the rate limiter alone on older shaping) drops only when it is full or
    when CoDel signals congestion. netem's own counter adds its injected random loss to
    the overflow handed back by that queue.
    """
    if not current or not previous or dt <= 0 or current["kind"] != previous["kind"] or current.get("generation", 0) != previous.get("generation", 0):
        return None
    sent = current["packets"] - previous["packets"]
    netem_drops = current["netem_drops"] - previous["netem_drops"]
    queue_drops = current["queue_drops"] - previous["queue_drops"]
    if min(sent, netem_drops, queue_drops) < 0:
        return None
    dropped = max(netem_drops, queue_drops)
    offered = sent + dropped
    rates = {
        "queue_drops_ps": queue_drops / dt,
        "injected_drops_ps": max(0, netem_drops - queue_drops) / dt,
        "drop_pct": dropped * 100.0 / offered if offered else 0.0,
        "backlog_bytes": current["backlog_bytes"],
    }
    before = (previous.get("queue_accounting") or {}).get("fair_queue_drops") or {}
    after = (current.get("queue_accounting") or {}).get("fair_queue_drops") or {}
    keys = ("drops", "drop_overlimit", "drop_overmemory")
    if all(before.get(key) is not None and after.get(key) is not None for key in keys):
        total, limited, memory = (after[key] - before[key] for key in keys)
        # Linux's overmemory count is a subset of overlimit, not an extra loss.
        if 0 <= memory <= limited <= total:
            rates.update(queue_memory_drops_ps=memory / dt,
                         queue_packet_drops_ps=(limited - memory) / dt,
                         queue_other_drops_ps=(total - limited) / dt)
    return rates


# ---------- Persistent telemetry / active measurement ----------

from contextlib import contextmanager


EPHEMERAL_TELEMETRY_LOCK = threading.RLock()
EPHEMERAL_TELEMETRY = None


def unsaved_test_active():
    return bool(SCENARIO_STATE.get('active') and SCENARIO_STATE.get('unsaved'))


def begin_unsaved_telemetry():
    global EPHEMERAL_TELEMETRY
    with EPHEMERAL_TELEMETRY_LOCK:
        if EPHEMERAL_TELEMETRY is not None:
            EPHEMERAL_TELEMETRY.close()
        EPHEMERAL_TELEMETRY = sqlite3.connect(':memory:', check_same_thread=False)
        EPHEMERAL_TELEMETRY.row_factory = sqlite3.Row
        init_telemetry_db()


def end_unsaved_telemetry():
    global EPHEMERAL_TELEMETRY
    with EPHEMERAL_TELEMETRY_LOCK:
        if EPHEMERAL_TELEMETRY is not None:
            EPHEMERAL_TELEMETRY.close()
            EPHEMERAL_TELEMETRY = None
    TELEMETRY_PREVIOUS.clear()
    # Live-only events must not enter later session reports.
    EVENT_LOG[:] = [event for event in EVENT_LOG if not event.get('temporary')]
    with DIAGNOSIS_LOCK:
        DIAGNOSIS_CACHE['checked'] = None


@contextmanager
def telemetry_connect():
    with EPHEMERAL_TELEMETRY_LOCK:
        if EPHEMERAL_TELEMETRY is not None:
            with EPHEMERAL_TELEMETRY:
                yield EPHEMERAL_TELEMETRY
            return
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(TELEMETRY_DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


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

            CREATE TABLE IF NOT EXISTS host_samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                status TEXT NOT NULL,
                reasons TEXT NOT NULL,
                sampler_lag_ms REAL,
                collect_ms REAL,
                cpu_busy_pct REAL,
                cpu_steal_pct REAL,
                cpu_iowait_pct REAL,
                mem_available_pct REAL,
                psi_cpu REAL,
                psi_memory REAL,
                psi_io REAL,
                nic_drops_ps REAL,
                disk_free_pct REAL
            );
            CREATE INDEX IF NOT EXISTS idx_host_time
                ON host_samples(timestamp);
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


HOST_STATE = {}
HOST_STATUS = {"status": None}


def record_host_sample(lag_ms: float, collect_ms: float):
    """Store one reading of the NetEm host beside the telemetry it may disturb. Never raises: measurements matter more."""
    try:
        cfg = load_config()
        interfaces = [name for link in cfg.get("wan_links", []) for name in (link.get("inner"), link.get("outer")) if name]
        reading, next_state = host_health.sample_host(HOST_STATE, interfaces, time.monotonic(), disk_path=RUNTIME_DIR)
        HOST_STATE.clear()
        HOST_STATE.update(next_state)
        reading.update(sampler_lag_ms=round(lag_ms, 1), collect_ms=round(collect_ms, 1))
        verdict = host_health.assess(reading)
        with telemetry_connect() as conn:
            conn.execute(
                """
                INSERT INTO host_samples (timestamp, status, reasons, sampler_lag_ms, collect_ms, cpu_busy_pct,
                    cpu_steal_pct, cpu_iowait_pct, mem_available_pct, psi_cpu, psi_memory, psi_io, nic_drops_ps,
                    disk_free_pct)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (time.time(), verdict["status"], json.dumps(verdict["reasons"]), reading["sampler_lag_ms"],
                 reading["collect_ms"], reading.get("cpu_busy_pct"), reading.get("cpu_steal_pct"),
                 reading.get("cpu_iowait_pct"), reading.get("mem_available_pct"), reading.get("psi_cpu"),
                 reading.get("psi_memory"), reading.get("psi_io"), reading.get("nic_drops_ps"),
                 reading.get("disk_free_pct")),
            )
        if verdict["status"] != HOST_STATUS["status"]:
            HOST_STATUS["status"] = verdict["status"]
            labels = [item["label"] for item in verdict["reasons"]]
            message = f"NetEm host {verdict['status'].replace('_', ' ')}" + (f": {', '.join(labels)}" if labels else "")
            log_event("host-health", message, status=verdict["status"], reasons=labels)
    except Exception as exc:
        log_event("host-health", "Host reading failed", error=str(exc)[:240])


def host_headroom(window_s: int = 1800, now=None):
    """The NetEm host over the last window, for the Platform Health page."""
    now = time.time() if now is None else now
    try:
        with telemetry_connect() as conn:
            rows = [dict(row) for row in conn.execute(
                "SELECT * FROM host_samples WHERE timestamp >= ? AND timestamp <= ? ORDER BY timestamp",
                (now - window_s, now + 1),
            ).fetchall()]
    except sqlite3.Error:
        rows = []
    for row in rows:
        row["reasons"] = json.loads(row["reasons"] or "[]")
    return host_health.summarize(rows, TELEMETRY_SAMPLE_SECONDS, window_s)


PLATFORM_TEST_LOCK = threading.Lock()
PLATFORM_TEST_DIR = RUNTIME_DIR
NIC_WINDOW_SECONDS = 5.0


def bridge_problem_summary(problem):
    parts = []
    if problem.get("bridge_missing"):
        parts.append("bridge missing")
    if problem.get("bridge_down"):
        parts.append("bridge down")
    for key, text in (("missing", "port missing"), ("outside", "port outside the bridge"),
                      ("down", "port down"), ("no_link", "port without a link")):
        parts.extend(f"{text} {name}" for name in problem.get(key) or [])
    return f"{problem['link_id']}: " + (", ".join(parts) or "not ready")


def platform_test_busy():
    """Why active path checks must wait, or None when the lab is idle."""
    if SCENARIO_STATE.get("active"):
        return "a scenario is running"
    if SITE_PLAN_STATE.get("active"):
        return "a site test plan is running"
    snapshot = traffic_generator_snapshot()
    status = (snapshot.get("status") or {}).get("status") if snapshot.get("connected") else None
    if status in ("starting", "running"):
        return "simulated traffic is running"
    return None


def applied_shaping(link, effective):
    """The configured rate, delay and loss beside what tc reports on each side of the WAN."""
    shaping = {}
    expected = {
        "down": {"rate_mbit": effective.get("download_mbit"), "delay_ms": effective.get("delay_ms"),
                 "loss_pct": effective.get("loss_pct")},
        "up": {"rate_mbit": effective.get("upload_mbit"), "delay_ms": 0.0, "loss_pct": 0.0},
    }
    for direction, role in (("down", "inner"), ("up", "outer")):
        device = link.get(role)
        if not device:
            continue
        rc, out, _err = run_cmd(f"{TC} qdisc show dev {device}")
        applied = parse_qdisc_output(out if rc == 0 else "")["parsed"]
        shaping[direction] = {"expected": expected[direction], "applied": {
            "rate_mbit": applied.get("rate_mbit"), "delay_ms": applied.get("delay_ms"),
            "loss_pct": applied.get("loss_pct")}, "device": device, "raw": out if rc == 0 else ""}
    return shaping

def gather_platform_facts():
    """Everything the self-test judges, read from this host now. Nothing here changes the lab."""
    cfg = load_config()
    links = cfg.get("wan_links", [])
    busy = platform_test_busy()
    host = host_headroom(window_s=1800)
    pairs = {}
    for link in links:
        link_id = link.get("id") or link.get("bridge")
        pairs[link_id] = [(role, link.get(role)) for role in ("inner", "outer") if link.get(role)]
    names = [name for items in pairs.values() for _role, name in items]
    first = {name: host_health.nic_total(host_health.SYS_NET, [name]) for name in names}
    time.sleep(NIC_WINDOW_SECONDS)
    second = {name: host_health.nic_total(host_health.SYS_NET, [name]) for name in names}
    # Read the clock after the window: sample timestamps are taken during it, so ages must be too.
    now = time.time()
    probes = get_probes(cfg)
    facts_links, evidence = [], []
    for link in links:
        link_id = link.get("id") or link.get("bridge")
        effective = (RUNTIME_EFFECTIVE.get(link_id) or {}).get("effective") or {}
        interfaces = []
        for role, name in pairs[link_id]:
            delta = None
            if first.get(name) is not None and second.get(name) is not None:
                delta = second[name] - first[name]
            interfaces.append({"role": role, "name": name, "present": (host_health.SYS_NET / name).exists(),
                               "carrier": host_health.read_carrier(host_health.SYS_NET, name),
                               "errors_delta": delta})
        fault = ACTIVE_FAULTS.get(link_id, "normal")
        if fault != "normal":
            shaping = {"skipped": f"A fault is injected on this WAN: {fault.replace('_', ' ')}."}
        elif not effective:
            shaping = {"skipped": "No profile has been applied to this WAN yet."}
        else:
            shaping = applied_shaping(link, effective)
            for direction, item in shaping.items():
                evidence.append({"name": f"{link_id} {item['device']} qdisc ({direction})", "text": item.pop("raw")})
        probe = {"skipped": f"Skipped: {busy}."} if busy else {"skipped": "No ICMP probe is configured for this WAN."}
        candidate = next((item for item in probes if item.get("link_id") == link_id and item.get("kind") == "icmp"), None)
        if not busy and candidate is not None:
            outcome = execute_probe(candidate, cfg)
            success = bool(outcome.get("success")) and outcome.get("latency_ms") is not None
            probe = {"success": success, "target": outcome.get("target"), "latency_ms": outcome.get("latency_ms"),
                     "detail": outcome.get("detail"), "configured_delay_ms": effective.get("delay_ms") or 0.0}
        sample = latest_telemetry_sample(link_id)
        facts_links.append({"id": link_id, "label": link.get("name") or link_id, "fault": fault,
                            "interfaces": interfaces, "shaping": shaping, "probe": probe,
                            "telemetry_age_s": None if sample is None else round(now - sample["timestamp"], 2)})
    ages = [item["telemetry_age_s"] for item in facts_links]
    telemetry_age = None if not ages or any(age is None for age in ages) else max(ages)
    try:
        with telemetry_connect() as conn:
            quick = conn.execute("PRAGMA quick_check").fetchone()[0]
    except sqlite3.Error as exc:
        quick = f"error: {exc}"
    problems = wan_bridge_problems(cfg)
    snapshot = traffic_generator_snapshot()
    return {"collected_at": now, "links": facts_links, "host": host,
            "clock": local_time_sync(),
            "telemetry": {"age_s": telemetry_age, "interval_s": TELEMETRY_SAMPLE_SECONDS},
            "database": {"quick_check": quick, "disk_free_pct": host_health.disk_free_pct(RUNTIME_DIR)},
            "simulator": {"connected": bool(snapshot.get("connected")) if snapshot.get("configured") else None,
                          "detail": snapshot.get("error") or ""},
            "forwarding": host_health.read_sysctl(host_health.PROC, "net/ipv4/ip_forward"),
            "bridge_problems": None if problems is None else [
                {"link_id": item["link_id"], "summary": bridge_problem_summary(item)} for item in problems],
            "busy": busy, "evidence": evidence}

def platform_environment():
    uname = system_platform.uname()
    return {"app version": get_app_version(), "python": sys.version.split()[0],
            "system": f"{uname.system} {uname.release}", "machine": uname.machine,
            "host": uname.node}


def run_platform_self_test():
    """Run the self-test once and keep its diagnostics for download. None when a run is already in progress."""
    if not PLATFORM_TEST_LOCK.acquire(blocking=False):
        return None
    try:
        facts = gather_platform_facts()
        report = platform_test.run_self_test(facts, now=time.time())
        environment = platform_environment()
        configuration = load_config()
        text = platform_test.diagnostics_text(report, facts, environment)
        bundle = platform_test.diagnostics_json(report, facts, environment, configuration)
        (PLATFORM_TEST_DIR / "platform-self-test.txt").write_text(text, encoding="utf-8")
        (PLATFORM_TEST_DIR / "platform-self-test.json").write_text(bundle, encoding="utf-8")
        counts = report["counts"]
        log_event("platform-test", f"Platform self-test {report['status']}: {counts['fail']} fail, "
                  f"{counts['warn']} warn, {counts['pass']} pass, {counts['skip']} skipped")
        return report
    finally:
        PLATFORM_TEST_LOCK.release()


def last_platform_self_test():
    path = PLATFORM_TEST_DIR / "platform-self-test.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))["report"]
    except (OSError, ValueError, KeyError):
        return None


def platform_self_test_file(kind):
    """The saved diagnostics as (text, media type, download name), or None when no run has been saved."""
    if kind == "text":
        file_name, media, name = "platform-self-test.txt", "text/plain; charset=utf-8", "netem-platform-self-test.txt"
    elif kind == "json":
        file_name, media, name = "platform-self-test.json", "application/json", "netem-platform-self-test.json"
    else:
        return None
    try:
        return (PLATFORM_TEST_DIR / file_name).read_text(encoding="utf-8"), media, name
    except OSError:
        return None


CALIBRATION_LOCK = threading.Lock()
CALIBRATION_STATE = {"running": False, "link_id": None, "target_mbps": None, "started_at": None}
CALIBRATION_FILE = RUNTIME_DIR / "bandwidth-calibration.json"


def wan_shaped_download(link_id):
    effective = (RUNTIME_EFFECTIVE.get(link_id) or {}).get("effective") or {}
    return float(effective["download_mbit"]) if effective.get("download_mbit") else None


def measure_wan(link_id, start, end):
    """Mean download rate, drop share and queue drops on one WAN between two times, from its telemetry."""
    try:
        with telemetry_connect() as conn:
            row = conn.execute(
                """
                SELECT AVG(down_mbps) AS mbps, AVG(down_drop_pct) AS drop_pct,
                       AVG(down_queue_drops_ps) AS drops_ps, COUNT(*) AS samples
                FROM telemetry_samples
                WHERE link_id = ? AND rate_valid = 1 AND timestamp >= ? AND timestamp <= ?
                """,
                (link_id, start, end),
            ).fetchone()
    except sqlite3.Error:
        return {}
    if not row or not row["samples"]:
        return {}
    return {"achieved_mbps": row["mbps"], "drop_pct": row["drop_pct"], "drops_ps": row["drops_ps"],
            "samples": row["samples"]}


def calibration_device(link_id):
    """The interfaces on each side of one WAN: the inner side carries the download, the outer side the upload."""
    link = next((item for item in load_config().get("wan_links", [])
                 if (item.get("id") or item.get("bridge")) == link_id), {})
    return {"down": link.get("inner"), "up": link.get("outer")}


def qdisc_readings(devices):
    """tc -s counters for each side of the WAN: the raw text and the counters parsed per qdisc."""
    readings = {}
    for direction, device in devices.items():
        if not device:
            continue
        rc, out, err = run_cmd(f"{TC} -s qdisc show dev {device}")
        if rc != 0:
            readings[direction] = {"device": device, "raw": f"tc failed: {err or out}", "parsed": []}
            continue
        parsed = [{key: item.get(key) for key in ("kind", "handle", "root", "bytes", "packets", "drops", "overlimits",
                                                  "backlog_bytes")}
                  for item in parse_qdisc_stats(out)]
        readings[direction] = {"device": device, "raw": out, "parsed": parsed}
    return readings


def simulator_state():
    """What the Traffic Simulator reports at this moment: what it is running and what it has recorded."""
    try:
        status = traffic_generator_request("/api/v1/status", timeout=3.0)
    except RuntimeError as exc:
        return {"error": str(exc)[:200]}
    dem = status.get("dem") or {}
    return {"status": status.get("status"), "users": status.get("users"),
            "run_id": (status.get("run") or {}).get("run_id"),
            "rate_demand": (status.get("run") or {}).get("rate_demand"),
            "dropped_transactions": status.get("dropped_transactions"),
            "persistence_error": status.get("persistence_error"),
            "requests_per_second": dem.get("requests_per_second"),
            "availability_pct": dem.get("availability_pct"), "p95_ms": dem.get("p95_ms")}


def wan_timeline(link_id, start, end):
    """The per-second telemetry of one WAN across a window."""
    try:
        with telemetry_connect() as conn:
            rows = conn.execute(
                """
                SELECT timestamp, down_mbps, up_mbps, down_queue_drops_ps, down_injected_drops_ps, down_drop_pct,
                       down_backlog_bytes, up_queue_drops_ps, up_backlog_bytes, rate_valid
                FROM telemetry_samples
                WHERE link_id = ? AND timestamp >= ? AND timestamp <= ?
                ORDER BY timestamp
                """,
                (link_id, start, end),
            ).fetchall()
    except sqlite3.Error:
        return []
    return [dict(row) for row in rows]


def host_window(start, end):
    """The NetEm host's per-second readings across a window."""
    try:
        with telemetry_connect() as conn:
            rows = conn.execute(
                """
                SELECT timestamp, status, cpu_busy_pct, cpu_steal_pct, psi_cpu, psi_io, mem_available_pct,
                       sampler_lag_ms, collect_ms, nic_drops_ps
                FROM host_samples
                WHERE timestamp >= ? AND timestamp <= ?
                ORDER BY timestamp
                """,
                (start, end),
            ).fetchall()
    except sqlite3.Error:
        return []
    return [dict(row) for row in rows]


def calibration_probe(link_id):
    """One ICMP probe through the WAN at the end of a step, when the WAN has a probe configured."""
    cfg = load_config()
    candidate = next((item for item in get_probes(cfg)
                      if item.get("link_id") == link_id and item.get("kind") == "icmp"), None)
    if candidate is None:
        return {"skipped": "No ICMP probe is configured for this WAN."}
    outcome = execute_probe(candidate, cfg)
    return {"success": bool(outcome.get("success")), "target": outcome.get("target"),
            "latency_ms": outcome.get("latency_ms"), "detail": outcome.get("detail"),
            "configured_delay_ms": ((RUNTIME_EFFECTIVE.get(link_id) or {}).get("effective") or {}).get("delay_ms") or 0.0}


def events_between(start, end, limit=2000):
    """NetEm's event log entries recorded between two times."""
    try:
        lines = EVENT_LOG_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    events = []
    for line in lines[-20000:]:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if start <= event.get("timestamp", 0) <= end:
            events.append(event)
    return events[:limit]


def calibration_file(kind):
    """The saved calibration as (body, media type, download name), or None when there is none."""
    if kind == "text":
        path, media, name = CALIBRATION_FILE.with_suffix(".txt"), "text/plain; charset=utf-8", "netem-bandwidth-calibration.txt"
    elif kind == "json":
        path, media, name = CALIBRATION_FILE, "application/json", "netem-bandwidth-calibration.json"
    else:
        return None
    try:
        return path.read_text(encoding="utf-8"), media, name
    except OSError:
        return None


def simulator_dem_window(seconds, since=None, until=None, run_id=None):
    """The Traffic Simulator's DEM for the last seconds: what the sparse OT-style traffic experienced, with its loss."""
    try:
        from urllib.parse import urlencode
        query = urlencode({"since": since, "until": until, "run_id": run_id}) if since is not None else urlencode({"window": int(seconds)})
        summary = traffic_generator_request(f"/api/v1/dem/summary?{query}", timeout=7.0)
    except RuntimeError as exc:
        return {"error": str(exc)[:200]}
    apps = summary.get("applications") or {}
    ot = apps.get("ot_telemetry") or {}
    quality = ot.get("quality") or {}
    return {"scoring_complete": summary.get("scoring_complete"), "evidence_issues": summary.get("evidence_issues"),
            "ot_loss_pct": quality.get("udp_loss_pct"), "ot_jitter_p95_ms": quality.get("udp_rtt_variation_p95_ms"),
            "ot_p95_ms": ot.get("p95_ms"), "ot_bursts": ot.get("requests"),
            "requests_per_second": summary.get("requests_per_second"), "p95_ms": summary.get("p95_ms")}


def lift_download_shaping(link_id):
    """Remove the download rate limit from one WAN for an unshaped calibration. Its configured delay and loss stay."""
    cfg = load_config()
    link = get_link(cfg, link_id)
    if not link or not link.get("inner"):
        raise RuntimeError("Unknown WAN for an unshaped calibration.")
    effective = (RUNTIME_EFFECTIVE.get(link_id) or {}).get("effective")
    if not effective:
        raise RuntimeError("Apply this WAN's profile before an unshaped calibration.")
    ok, msg = apply_netem(link["inner"], effective["delay_ms"], effective["jitter_ms"], effective["loss_pct"], 0.0)
    if not ok:
        raise RuntimeError(f"Could not lift the download shaping: {msg}")
    log_event("calibration", f"Download shaping lifted on {link_id} for an unshaped calibration")


def restore_download_shaping(link_id):
    """Put a WAN back to its configured profile after a calibration. Returns the error text, or None."""
    cfg = load_config()
    link = get_link(cfg, link_id)
    if not link:
        return "The WAN is no longer configured; restore its profile by hand."
    ok, msg, _effective = apply_selected_profile(link, get_presets(cfg))
    if not ok:
        return msg
    log_event("calibration", f"Shaping restored on {link_id} after a calibration")
    return None


def save_calibration(result):
    CALIBRATION_FILE.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    CALIBRATION_FILE.with_suffix(".txt").write_text(calibration_log.text_report(result), encoding="utf-8")
    return result


def last_bandwidth_calibration():
    try:
        return json.loads(CALIBRATION_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _timed(function, *args):
    """Calls function and returns its result with how long it took in milliseconds, for the calibration log."""
    started = time.perf_counter()
    value = function(*args)
    return value, round((time.perf_counter() - started) * 1000.0, 1)


def calibration_health():
    """The simulator's health endpoint: event-loop lateness, counters and target latency, polled once a second."""
    return traffic_generator_request("/api/v1/health", timeout=calibration_trace.POLL_TIMEOUT_S)


def run_bandwidth_calibration(link_id, unshaped=False):
    """Step paced load up the ladder through the selected traffic path. Each step is judged on what the WAN carried,
    what the shaper dropped, and the host's state. The workload is stopped after every step, even on failure."""
    if not CALIBRATION_LOCK.acquire(blocking=False):
        return None
    run_id = None
    steps = []
    session_start = time.time()
    mode = "unshaped" if unshaped else "shaped"
    restore_error = None
    lift_started = False
    sparse_ok = True
    poller = None
    CALIBRATION_STATE.update(running=True, link_id=link_id, target_mbps=None, started_at=time.time())
    try:
        busy = platform_test_busy()
        if busy:
            raise RuntimeError(f"Calibration needs an idle lab: {busy}.")
        if unshaped and session_snapshot().get("active"):
            raise RuntimeError("An unshaped calibration cannot run during a lab session: it lifts the WAN shaping.")
        ensure_traffic_path("bandwidth calibration")
        shaped = wan_shaped_download(link_id)
        if unshaped:
            lift_started = True
            lift_download_shaping(link_id)
            shaped = None
        unloaded_probe = calibration_probe(link_id)
        for target in calibration.LADDER_MBPS:
            users = max(10, min(500, int(target / 5)))
            CALIBRATION_STATE["target_mbps"] = target
            payload = {"profile": "office", "pattern": "steady", "users": users, "spawn_rate": users,
                       "paced_mbps": target, "controller_lease_seconds": 120}
            if sparse_ok:
                payload["sparse_share"] = round(min(0.2, 4.0 / users), 3)
            sparse_used = sparse_ok
            try:
                status = traffic_generator_request("/api/v1/workloads/start", method="POST", payload=payload, timeout=5.0)
            except RuntimeError as exc:
                if not sparse_used or "Unknown workload" not in str(exc):
                    raise
                # An older simulator does not know sparse_share: run the step without it and say so in the log.
                sparse_ok = sparse_used = False
                payload.pop("sparse_share")
                status = traffic_generator_request("/api/v1/workloads/start", method="POST", payload=payload, timeout=5.0)
            run_id = (status.get("run") or {}).get("run_id")
            path = (status.get("run") or {}).get("traffic_path") or {}
            if path.get("mode") != "direct_netem" or path.get("wan_id") != link_id:
                raise RuntimeError("WAN calibration requires the matching direct NetEm path; select this WAN before calibrating.")
            poller = calibration_trace.SimulatorPoller(fetch=calibration_health).start()
            time.sleep(calibration.SETTLE_S)
            devices = calibration_device(link_id)
            qdisc_before = qdisc_readings(devices)
            sim_before, sim_before_ms = _timed(simulator_state)
            start = time.time()
            time.sleep(calibration.MEASURE_S / 2)
            probe = dict(calibration_probe(link_id))
            probe["unloaded_baseline"] = unloaded_probe
            if (probe.get("success") and unloaded_probe.get("success") and
                    probe.get("target") == unloaded_probe.get("target") and
                    probe.get("latency_ms") is not None and unloaded_probe.get("latency_ms") is not None):
                probe["additional_latency_ms"] = probe["latency_ms"] - unloaded_probe["latency_ms"]
                probe["attribution"] = "additional RTT under load; queueing and endpoint contention are not separated"
            remaining = calibration.MEASURE_S - (time.time() - start)
            if remaining > 0:
                time.sleep(remaining)
            end = time.time()
            qdisc_after = qdisc_readings(devices)
            polls, poller = poller.stop(), None
            sim_after, sim_after_ms = _timed(simulator_state)
            measured, measure_ms = _timed(measure_wan, link_id, start, end)
            host = host_headroom(window_s=int(calibration.MEASURE_S), now=end)
            qdisc_delta = {direction: calibration_log.qdisc_deltas(
                                (qdisc_before.get(direction) or {}).get("parsed"),
                                (qdisc_after.get(direction) or {}).get("parsed"))
                           for direction in devices if devices.get(direction)}
            shaper = calibration_log.shaper_drops(qdisc_delta, end - start)
            shaper.update(calibration_log.tail_drops((qdisc_before.get("down") or {}).get("raw"),
                                                     (qdisc_after.get("down") or {}).get("raw")))
            verdict = calibration.judge_step(target, shaped, measured.get("achieved_mbps"), shaper["drop_pct"],
                                             host.get("status", "unknown"), host.get("late_seconds", 0),
                                             shaper["drops_ps"], require_demand=True,
                                             demand=calibration.demand_window(sim_before.get("rate_demand"), sim_after.get("rate_demand")))
            if sparse_used:
                sparse_window, dem_ms = _timed(simulator_dem_window, end - start, start, end, run_id)
            else:
                sparse_window, dem_ms = {"skipped": "The simulator does not support sparse traffic; update it."}, None
            _, stop_ms = _timed(stop_test_owned_workload, run_id)
            run_id = None
            # The users that download share the rate; the sparse users send UDP bursts instead.
            paced_users = users - (round(users * payload["sparse_share"]) if "sparse_share" in payload else 0)
            wan_rows = wan_timeline(link_id, start, end)
            verdict["diagnostics"] = {
                "sparse": sparse_window,
                "window": {"start": start, "end": end},
                "shaper": shaper,
                "qdisc_delta": qdisc_delta,
                "tc_start": {direction: reading["raw"] for direction, reading in qdisc_before.items()},
                "tc_end": {direction: reading["raw"] for direction, reading in qdisc_after.items()},
                "simulator_before": sim_before,
                "simulator_after": sim_after,
                "trace": calibration_trace.build(polls, wan_rows, start, end, paced_users=paced_users),
                "timings": {"status_before_ms": sim_before_ms, "status_after_ms": sim_after_ms,
                            "stop_workload_ms": stop_ms, "measure_wan_ms": measure_ms, "dem_window_ms": dem_ms},
                "wan_timeline": wan_rows,
                "host_timeline": host_window(start, end),
                "probe": probe,
            }
            log_event("calibration", calibration_log.step_line(verdict), target_mbps=target, status=verdict["status"])
            steps.append(verdict)
            if verdict["status"] == "fail" or (shaped and target >= shaped * calibration.REACHED_SHAPING):
                break
        result = calibration.summarise(steps, shaped)
        result.update(link_id=link_id, generated_at=time.time(), settle_s=calibration.SETTLE_S,
                      measure_s=calibration.MEASURE_S, mode=mode)
        result.update(bottlenecks.analyse(result))
    except RuntimeError as exc:
        result = {"link_id": link_id, "generated_at": time.time(), "error": str(exc), "steps": steps}
    except Exception as exc:
        log_event("calibration", "Bandwidth calibration failed", error=str(exc)[:240])
        result = {"link_id": link_id, "generated_at": time.time(), "error": "Calibration failed: " + str(exc)[:200],
                  "steps": steps}
    finally:
        if poller is not None:
            poller.stop()
        if run_id:
            try:
                stop_test_owned_workload(run_id)
            except RuntimeError:
                pass
        if lift_started:
            restore_error = restore_download_shaping(link_id)
        CALIBRATION_STATE.update(running=False, target_mbps=None)
        CALIBRATION_LOCK.release()
    if restore_error:
        result["restore_error"] = restore_error
        log_event("calibration", f"Restoring the shaping of {link_id} failed: {restore_error}")
    result["events"] = events_between(session_start, time.time())
    save_calibration(result)
    if "error" not in result:
        log_event("calibration", f"Bandwidth calibration of {link_id}: capacity {result['capacity_mbps']} Mbit/s, "
                  f"limited by {result['limited_by']}")
    return result


def start_bandwidth_calibration(link_id, unshaped=False):
    """Start a calibration in the background, since a full ladder takes several minutes. False if one is running."""
    if CALIBRATION_STATE.get("running"):
        return False
    threading.Thread(target=run_bandwidth_calibration, args=(link_id, unshaped), name="netem-calibration", daemon=True).start()
    return True


def prune_telemetry_history():
    cutoff = time.time() - TELEMETRY_RETENTION_HOURS * 3600
    with telemetry_connect() as conn:
        conn.execute("DELETE FROM telemetry_samples WHERE timestamp < ?", (cutoff,))
        conn.execute("DELETE FROM probe_samples WHERE timestamp < ?", (cutoff,))
        conn.execute("DELETE FROM host_samples WHERE timestamp < ?", (cutoff,))


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
    next_bridge_check = time.monotonic() + BRIDGE_CHECK_SECONDS
    wall, clock = time.time(), time.monotonic()
    last_started = None
    while not BACKGROUND_STOP.is_set():
        started = time.monotonic()
        # How late this pass started compared with when it was due. A late sampler means the host is not keeping up,
        # so the rates computed from its samples are less exact.
        lag_ms = 0.0 if last_started is None else max(0.0, (started - last_started - TELEMETRY_SAMPLE_SECONDS) * 1000)
        last_started = started
        if started >= next_bridge_check:
            next_bridge_check = started + BRIDGE_CHECK_SECONDS
            try:
                check_wan_bridges()
            except Exception as exc:
                log_event("bridge", "WAN bridge check failed", error=str(exc)[:240])
        try:
            # Wall time should advance with the monotonic clock; a difference is a clock step.
            step = (time.time() - wall) - (time.monotonic() - clock)
            wall, clock = time.time(), time.monotonic()
            if step <= -CLOCK_STEP_SECONDS:
                note_clock_step(step, discard_future_samples(wall))
            elif step >= CLOCK_STEP_SECONDS * 12:
                note_clock_step(step, 0)
            collect_telemetry_sample()
            record_host_sample(lag_ms, (time.monotonic() - started) * 1000)
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
DIAGNOSIS_INTERVAL_SECONDS = 1.0
DIAGNOSIS_CLEAR_SECONDS = 30
SATURATION_PCT = 90.0
# Queue-management drops alone do not establish sustained WAN pressure.
QUEUE_PRESSURE_MIN_SECONDS = 5.0
QUEUE_PRESSURE_MIN_SAMPLES = 4
QUEUE_PRESSURE_DELAY_MS = 20.0
QUEUE_PRESSURE_DROP_PCT = 1.0
EGRESS_LEARN_INTERVAL_SECONDS = 60
DIAGNOSIS_LOCK = threading.Lock()
DIAGNOSIS_BUILD_LOCK = threading.Lock()
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
# A WAN carrying at least this much (Mbit/s, either direction) right now is carrying users' traffic.
CARRIER_MIN_MBPS = 0.1


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


def sustained_queue_pressure(samples, direction, limit_mbit):
    """Corroborate drops with persistent backlog or repeated material queue loss.

    Use queue-only loss, excluding injected random loss. A short burst or one
    sampled drop cannot change WAN health or imply that its capacity is full.
    """
    rows = sorted((row for row in samples if row.get("rate_valid")
                   and isinstance(row.get("timestamp"), (int, float))
                   and row.get(f"{direction}_queue_drops_ps") is not None), key=lambda row: row["timestamp"])
    if len(rows) < QUEUE_PRESSURE_MIN_SAMPLES or rows[-1]["timestamp"] - rows[0]["timestamp"] < QUEUE_PRESSURE_MIN_SECONDS:
        return False
    # A sparse/stalled sampler cannot establish persistence between observations.
    if any(right["timestamp"] - left["timestamp"] > 3 * TELEMETRY_SAMPLE_SECONDS for left, right in zip(rows, rows[1:])):
        return False
    drops = [row[f"{direction}_queue_drops_ps"] for row in rows]
    if _mean(drops) < 1:
        return False
    backlog = sum(row.get(f"{direction}_backlog_bytes") is not None and bool(limit_mbit)
                  and row[f"{direction}_backlog_bytes"] * 8 / (limit_mbit * 1000) >= QUEUE_PRESSURE_DELAY_MS for row in rows)
    if backlog >= len(rows) / 2:
        return True
    sent = [row.get(f"{direction}_pps") for row in rows]
    if any(value is None for value in sent):
        return False
    offered = sum(sent) + sum(drops)
    return (offered > 0 and 100 * sum(drops) / offered >= QUEUE_PRESSURE_DROP_PCT
            and sum(value > 0 for value in drops) >= len(rows) / 2)


def path_signals(state: dict, samples: list):
    """Summarize one WAN's recent path measurements and the conditions they show."""
    effective = state.get("effective") or {}
    fault = state.get("fault", "normal")
    # Do not describe this phase using pressure recorded under another profile.
    expected = {key: float(effective.get(key) or 0) for key in ("delay_ms", "jitter_ms", "loss_pct")}
    if state.get("runtime_quality") is not None:
        expected["quality"] = float(state["runtime_quality"])
    samples = [sample for sample in samples if sample.get("fault", fault) == fault and all(
        sample.get(key) is None or math.isclose(float(sample[key]), value, abs_tol=1e-6)
        for key, value in expected.items())]
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
            info[key] = _mean(sample.get(f"{direction}_{key}") for sample in (valid if key == "util_pct" else samples))
        info["queue_delay_ms"] = (info["backlog_bytes"] * 8 / (info["limit_mbit"] * 1000)
                                  if info["backlog_bytes"] is not None and info["limit_mbit"] else None)
        info["pressure_sustained"] = sustained_queue_pressure(samples, direction, info["limit_mbit"])
        signals["directions"][direction] = info
        if info["util_pct"] is not None and info["util_pct"] >= SATURATION_PCT:
            signals["causes"].append({"kind": "saturated", "direction": direction, "severity": "warn",
                                      "text": f"{name} at {info['util_pct']:.0f}% of {info['limit_mbit']:g} Mbit/s"})
        if (info["queue_drops_ps"] or 0) >= 1:
            pressure = info["pressure_sustained"] or (info["util_pct"] or 0) >= SATURATION_PCT
            signals["causes"].append({
                "kind": "queue_drops" if pressure else "queue_activity", "direction": direction,
                "severity": "warn" if pressure else "info",
                "text": f"{name} queue drops: {info['queue_drops_ps']:.0f} packets/s"
                        + ("; sustained pressure measured" if info["pressure_sustained"] else
                           "; high utilization measured" if pressure else "; sustained pressure not established"),
            })
    loss = signals["injected"]["loss_pct"]
    if loss > 0 and fault == "normal":
        dropped = signals["directions"]["down"]["injected_drops_ps"]
        signals["causes"].append({
            "kind": "injected_loss", "direction": "down", "severity": "warn",
            "text": f"{loss:g}% random loss injected on download" + (f" (estimated {dropped:.1f} packets/s from counters)" if dropped else ""),
        })
    delay, jitter = signals["injected"]["delay_ms"], signals["injected"]["jitter_ms"]
    if (delay > 0 or jitter > 0) and fault == "normal":
        signals["causes"].append({
            "kind": "injected_delay", "direction": "down", "severity": "info",
            "text": f"{delay:g} ms" + (f" ± {jitter:g} ms" if jitter else "") + " delay added to every round trip",
        })
    signals["full"] = sorted({cause["direction"] for cause in signals["causes"]
                              if cause["kind"] == "saturated"})
    # Health decides where a well-behaved SD-WAN appliance should steer traffic.
    sla = state.get("sla") or {}
    failing = [name for name, ok in (sla.get("checks") or {}).items() if not ok and name != "data_plane"]
    if state.get("apply_status") == "unknown":
        signals["health"], signals["health_reason"] = "unknown", "Impairment application failed; actual state is unverified"
    elif fault != "normal":
        signals["health"], signals["health_reason"] = "failed", f"{fault.replace('_', ' ')} active"
    elif not sla.get("pass", True):
        signals["health"], signals["health_reason"] = "degraded", "Model SLA fails on " + ", ".join(failing or ["impairment"])
    elif any(cause["kind"] == "saturated" for cause in signals["causes"]):
        signals["health"] = "congested"
        signals["health_reason"] = "; ".join(cause["text"] for cause in signals["causes"]
                                             if cause["kind"] in ("saturated", "queue_drops"))
    elif any(cause["kind"] == "queue_drops" for cause in signals["causes"]):
        signals["health"] = "queue_pressure"
        signals["health_reason"] = "Queue drops corroborated by sustained backlog or material queue loss; sustained high utilization is not established"
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
    if cfg.get("_direct_wan_id"):
        return {"link_id": cfg["_direct_wan_id"], "source": "declared_direct_path"}
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


def single_carrier(signals):
    """The one WAN carrying traffic right now, or None when none or several are."""
    carriers = []
    for item in signals:
        rates = [((item.get("directions") or {}).get(direction) or {}).get("rate_mbps") or 0 for direction in ("down", "up")]
        if sum(rates) >= CARRIER_MIN_MBPS:
            carriers.append(item["link_id"])
    return carriers[0] if len(carriers) == 1 else None


def correlate_findings(dem_diagnosis, signals: list, cfg: dict, dem=None):
    """Attach each simulator symptom to the WANs that carried it and the path conditions there."""
    by_link = {item["link_id"]: item for item in signals}
    applications = (dem or {}).get("applications") or {}
    invalid_evidence = (dem or {}).get("truncated") or INVALID_EVIDENCE.intersection((dem or {}).get("evidence_issues") or [])
    findings, explained = [], set()
    symptoms = []
    for symptom in (dem_diagnosis or {}).get("findings") or []:
        if symptom.get("id") != "target_violation":
            symptoms.append(symptom)
            continue
        for app_name in symptom.get("applications") or [None]:
            evidence = symptom.get("evidence") or {}
            item = applications.get(app_name) or {}
            item = dict(item, quality=item.get("quality") or evidence.get("quality") or {},
                        experience_targets=item.get("experience_targets") or evidence.get("targets") or {})
            violations = item.get("target_violations") or [key.strip() for key in symptom.get("detail", "").split(";")[0].split(",") if key.strip()]
            # The simulator's generic target finding uses unknown egress. Recover
            # this application's own full-window path counts when available.
            egress = {address: {"requests": app["requests"]} for address, entry in (dem_diagnosis.get("egress") or {}).items()
                      if (app := (entry.get("applications") or {}).get(app_name)) and app.get("requests")}
            expected = item.get("requests") or symptom.get("affected")
            symptoms.append(dict(symptom, applications=[app_name] if app_name else [],
                                 by_egress=egress or symptom.get("by_egress") or {},
                                 path_incomplete=bool(egress and expected is not None and sum(entry['requests'] for entry in egress.values()) != expected),
                                 target_item=item, target_violations=violations,
                                 target_detail=target_details(item, violations)))
    for symptom in symptoms:
        relevant = SYMPTOM_CAUSES.get(symptom.get("id"), ())
        direction = {"download": "down", "upload": "up"}.get(symptom.get("direction"))
        target_violation = symptom.get("id") == "target_violation"
        violations = symptom.get("target_violations") or []
        if target_violation:
            kinds = {
                "loss": ("fault", "injected_loss", "queue_drops"),
                "success": ("fault", "injected_loss", "queue_drops"),
                "deadline": ("fault", "injected_delay", "injected_loss", "queue_drops"),
                "jitter": ("injected_delay", "queue_drops"),
                "throughput_down": ("fault", "saturated", "queue_drops", "injected_loss"),
                "throughput_up": ("fault", "saturated", "queue_drops", "injected_loss"),
                "sustained_down": ("fault", "saturated", "queue_drops", "injected_loss"),
                "sustained_up": ("fault", "saturated", "queue_drops", "injected_loss"),
            }
            relevant = {kind for key in violations for kind in kinds.get(key, ())}
            directions = {key.rsplit('_', 1)[-1] for key in violations if key.startswith(('throughput_', 'sustained_'))}
            if len(directions) == 1 and all(key.startswith(('throughput_', 'sustained_')) for key in violations):
                direction = next(iter(directions))
        waits = [value.get("p95_wait_ms") for value in (symptom.get("by_egress") or {}).values()
                 if isinstance(value, dict) and value.get("p95_wait_ms")]

        def explains(link, cause):
            if cause["kind"] not in relevant or (direction and cause["direction"] not in (None, direction)):
                return False
            if target_violation and cause["kind"] == "injected_loss":
                # Slow completion/transfer alone cannot establish retransmission.
                item = symptom["target_item"]
                measured_loss = (item.get("quality") or {}).get("udp_loss_pct") if 'loss' in violations else None
                if measured_loss is None and 'success' in violations and item.get("availability_pct") is not None:
                    measured_loss = 100 - item["availability_pct"]
                return measured_loss is not None and measured_loss > 0 and link["injected"]["loss_pct"] >= DELAY_SHARE * measured_loss
            if target_violation and cause["kind"] == "injected_delay":
                item = symptom["target_item"]
                quality = item.get("quality") or {}
                actual = quality.get("udp_rtt_variation_p95_ms") if 'jitter' in violations else quality.get("packet_rtt_p95_upper_ms", item.get("p95_ms"))
                added = link["injected"]["jitter_ms"] if 'jitter' in violations else link["injected"]["delay_ms"] + link["injected"]["jitter_ms"]
                return actual is not None and added > 0 and added >= DELAY_SHARE * actual
            if cause["kind"] == "injected_delay" and waits:
                added = link["injected"]["delay_ms"] + link["injected"]["jitter_ms"] + sum(
                    item.get("queue_delay_ms") or 0 for item in link["directions"].values())
                return added >= DELAY_SHARE * max(waits)
            return True

        def matching(link_id):
            link = by_link.get(link_id)
            return [cause for cause in link["causes"] if explains(link, cause)] if link else []

        per_link, unattributed, inferred = {}, 0, False
        for address, value in (symptom.get("by_egress") or {}).items():
            mapped = resolve_egress(address, cfg) if address != "unknown" or cfg.get("_direct_wan_id") else None
            if mapped and mapped.get("link_id") in by_link:
                per_link[mapped["link_id"]] = per_link.get(mapped["link_id"], 0) + _egress_count(value)
            else:
                unattributed += _egress_count(value)
        if unattributed and not per_link:
            # No reply address could be mapped to a WAN. If one WAN is the only one carrying traffic, it carried these users.
            carrier = single_carrier(signals)
            if carrier:
                per_link[carrier] = unattributed
                unattributed = 0
                inferred = True
        wans = []
        for link_id, affected in sorted(per_link.items(), key=lambda item: -item[1]):
            applied = RUNTIME_EFFECTIVE.get(link_id, {}).get("updated_at")
            since = (dem or {}).get("since")
            changed_in_window = applied is not None and since is not None and applied > since
            eligible = (len(per_link) == 1 and not unattributed and not inferred and not invalid_evidence
                        and not symptom.get("path_incomplete") and by_link[link_id].get("health") != "unknown" and not changed_in_window)
            causes = matching(link_id) if not target_violation or eligible else []
            explained.update((link_id, cause["kind"], cause["direction"]) for cause in causes)
            # A full queue is the saturation's effect, so the saturation is explained with it.
            explained.update((link_id, "saturated", cause["direction"]) for cause in causes if cause["kind"] == "queue_drops")
            wans.append({"link_id": link_id, "label": by_link[link_id]["label"], "affected": affected,
                         "causes": [cause["text"] for cause in causes], "cause_kinds": [cause["kind"] for cause in causes]})
        candidates = [item["label"] for item in signals if matching(item["link_id"])] if unattributed else []
        hint = None
        if target_violation and not any(wan["causes"] for wan in wans):
            hint = "Target miss measured; the path or a matching contributor could not be established from this window."
        elif symptom.get("id") == "http_errors":
            hint = "Returned by the appliance or the target, not caused by network quality."
        elif relevant and not any(wan["causes"] for wan in wans) and not candidates:
            hint = "NetEm's impairment does not explain this. Check the appliance (policy, inspection, CPU) or the target."
        findings.append({
            "source": "experience", "id": symptom.get("id"), "severity": symptom.get("severity", "warn"),
            "title": symptom.get("title", ""), "detail": symptom["target_detail"] if target_violation else symptom.get("detail", ""), "wans": wans,
            "unattributed": unattributed, "candidates": candidates, "hint": hint,
            "applications": list(symptom.get("applications") or []),
            **({"target_detail": symptom["target_detail"], "path_inferred": inferred,
                "path_incomplete": symptom.get("path_incomplete", False)} if target_violation else {}),
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
        mapped = resolve_egress(address, cfg) if address != "unknown" or cfg.get("_direct_wan_id") else None
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
            applied = RUNTIME_EFFECTIVE.get(item["link_id"], {}).get("updated_monotonic")
            links[item["link_id"]] = {"health": item["health"], "since": min(now, applied) if applied is not None else now}
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
        mapped = resolve_egress(address, cfg) if address != "unknown" or cfg.get("_direct_wan_id") else None
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
    traffic_path = ((status or {}).get("run") or {}).get("traffic_path") or {}
    traffic_mode = traffic_path.get("mode", "appliance")
    if traffic_mode == "direct_netem" and traffic_path.get("wan_id"):
        cfg = dict(cfg, _direct_wan_id=traffic_path["wan_id"])
    dem_diagnosis = dem.get("diagnosis") if isinstance(dem.get("diagnosis"), dict) else None
    identity = (((status or {}).get("run") or {}).get("run_id"), traffic_mode, traffic_path.get("wan_id"),
                tuple(sorted((key, value.get("generation", 0)) for key, value in RUNTIME_EFFECTIVE.items())))
    with DIAGNOSIS_LOCK:
        if STEERING_STATE.get("identity") != identity:
            previous_identity = STEERING_STATE.get("identity")
            baseline = STEERING_STATE.get("baseline", {}) if previous_identity and previous_identity[:3] == identity[:3] else {}
            STEERING_STATE.update(identity=identity, links={}, classes={}, baseline=baseline)
    signals = [path_signals(state, recent_telemetry_samples(state["id"])) for state in build_link_states(cfg)]
    findings = correlate_findings(dem_diagnosis, signals, cfg, dem)
    steering = assess_steering(dem, signals, cfg) if traffic_mode == "appliance" else None
    findings.extend(steering_findings(steering))
    for finding in findings:
        finding["observation"] = {"since": dem.get("since"), "until": dem.get("until"),
                                  "run_id": dem.get("run_id"), "path": traffic_path,
                                  "impairment_epochs": {key: value.get("generation", 0) for key, value in RUNTIME_EFFECTIVE.items()}}
        finding["confidence"] = "observed" if finding.get("source") == "experience" else "hypothesis"
        finding["causality"] = "not independently verified"
    if steering:
        steering["packet_observations"] = []
        for observed in dem.get("packet_egress_transitions", []):
            before, after = resolve_egress(observed.get("from"), cfg), resolve_egress(observed.get("to"), cfg)
            if not before or not after or before.get("link_id") == after.get("link_id"):
                continue
            applied = RUNTIME_EFFECTIVE.get(before["link_id"], {}).get("updated_at")
            observation = dict(observed, from_wan=before["link_id"], to_wan=after["link_id"])
            if applied is not None and observed["latest_at"] >= applied:
                observation["reaction_interval_seconds"] = [max(0, observed["earliest_at"] - applied), observed["latest_at"] - applied]
            steering["packet_observations"].append(observation)
        steering["timing_basis"] = "completion-window assessment with separate packet-observation bounds"
        steering["uncertainty_seconds"] = 10
        steering["impairment_epochs"] = {key: value.get("generation", 0) for key, value in RUNTIME_EFFECTIVE.items()}
    findings.sort(key=lambda finding: (SEVERITY_ORDER.get(finding["severity"], 9), finding["source"] != "experience"))
    experience, unattributed = wan_experience(dem_diagnosis, signals, cfg)
    if traffic_mode == "direct_baseline":
        findings, experience, unattributed = [], {}, 0
    for item in signals:
        item["experience"] = experience.get(item["link_id"])
    addresses = {address: resolve_egress(address, cfg)
                 for address in ((dem_diagnosis or {}).get("egress") or {}) if address != "unknown"}
    if traffic_mode == "appliance":
        maybe_learn_egress(cfg, status, [address for address, mapped in addresses.items() if not mapped])
    with DIAGNOSIS_LOCK:
        learn_state = dict(EGRESS_LEARN_STATE)
    tcpdump = bool(shutil.which("tcpdump"))
    mapping = egress_mapping_finding(addresses, learn_state, tcpdump) if traffic_mode == "appliance" else None
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
    bridges = bridge_finding(wan_bridge_snapshot())
    if bridges:
        findings.insert(0, bridges)
    track_diagnosis_events(findings)
    return {
        "timestamp": time.time(),
        "traffic_mode": traffic_mode,
        "workload_path": traffic_path,
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
        "traffic_path": traffic_path_readiness(cached=True) if snapshot.get("configured") else None,
    }


def current_diagnosis(max_age=DIAGNOSIS_INTERVAL_SECONDS + 1, cached=False):
    context = test_live_context()
    source = (presentation_source(), context['phase_id'], context['paused'], bool(SCENARIO_STATE.get('active')))
    def load():
        # Phase/source changes may leave an older read finishing. Serialize builders
        # across scopes: steering history and detected/cleared events are shared.
        with DIAGNOSIS_BUILD_LOCK:
            payload = build_diagnosis()
            with DIAGNOSIS_LOCK:
                DIAGNOSIS_CACHE.update(timestamp=time.time(), checked=time.monotonic(), payload=payload, source=source)
            return payload
    if cached:
        fallback = {'links': [], 'findings': [], 'traffic_generator': traffic_generator_empty_snapshot(),
                    'simulator': {'diagnosis_available': False, 'media_mode': None, 'interactive_p95_ms': None},
                    'steering': None, 'traffic_path': None, 'egress': {}, 'unattributed_experience': {}}
        result = PRESENTATION_READS.read('diagnosis', source, load, fallback, max_age=1, max_stale=3)
        # Connection status has its own freshness. Missing diagnosis evidence must
        # not manufacture a simulator disconnect on the dashboard/showcase.
        result['traffic_generator'] = traffic_generator_snapshot(cached=True)
        return result
    with DIAGNOSIS_LOCK:
        previous = dict(DIAGNOSIS_CACHE)
    if (previous.get('source') == source and previous['payload'] is not None and previous.get('checked') is not None
            and 0 <= time.monotonic() - previous['checked'] <= max_age and max_age):
        return copy.deepcopy(previous['payload'])
    return PRESENTATION_READS.fresh('diagnosis', source, load)


def diagnosis_worker():
    # Keeps findings (and their Detected/Cleared events) current without an open browser.
    while not BACKGROUND_STOP.is_set():
        try:
            expire_session_if_due()
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
        when = clock_label(step["at"], time_format())
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
    APPLIANCE_PLATFORM.start()
    BRANDING_SYNC.start()
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
        "appliance_evidence": [event for event in events if event.get("kind") in
                               ("appliance-readiness", "appliance-override", "appliance-selection", "appliance-health", "traffic-path")],
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
    if state.get("active") and state.get("preparing_workload"):
        state.update(elapsed_s=0.0, phase=None, phase_index=None, phase_remaining_s=None)
        return state
    if state.get("active") and state.get("intro"):
        # Announced, not started: nothing has run yet.
        state["elapsed_s"] = 0.0
        state["starts_in_s"] = round(max(0.0, (state.get("intro_clock") or 0) - time.monotonic()), 1)
        return state
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
            window = int(condition.get("window", SCENARIO_STATE.get("live_window_seconds", 15)))
            payload = traffic_generator_request(
                f"/api/v1/dem/experience?window={window}",
                timeout=2.5,
            )
        except (RuntimeError, ValueError) as exc:
            return False, None, str(exc)

        field = condition.get("field", "experience_score")
        if field != "active_users" and payload.get("truncated"):
            return False, None, "DEM transaction window exceeded the sample limit"
        if field != "active_users":
            if payload.get("scoring_complete") is not True or payload.get("evidence_issues"):
                return False, None, "DEM evidence is incomplete; a complete measurement is required"
            stamp = payload.get("timestamp")
            if not isinstance(stamp, (int, float)) or not 0 <= time.time() - stamp <= 10:
                return False, None, "DEM evidence is stale or future-dated"
            expected_run = SCENARIO_STATE.get("workload_run_id")
            if expected_run and payload.get("run_id") != expected_run:
                return False, None, "DEM belongs to a different workload"
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
        check_phase_navigation()
        check_test_workload()
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
        poll_deadline = time.monotonic() + max(0.25, min(5.0, float(poll_s)))
        while time.monotonic() < poll_deadline and not SCENARIO_STOP.is_set():
            check_phase_navigation()
            SCENARIO_STOP.wait(min(0.25, max(0, poll_deadline - time.monotonic())))

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
    if unsaved_test_active():
        with TEST_RECORDER_LOCK:
            TEST_RECORDER.clear()
            TEST_RECORDER.update(active=False)
        return
    test_id = uuid.uuid4().hex
    started_at = time.time()
    configuration = {'schema_version': 1, 'test_id': test_id, 'started_at': started_at,
                     'scenario': scenario, 'configuration': load_config(),
                     'platform_version': VERSION_PATH.read_text().strip() if VERSION_PATH.exists() else None,
                     'session_id': active_session_id(),
                     'timestamp_basis': 'Unix seconds; platform and simulator clocks must be synchronized for cross-host correlation'}
    record_error = None
    try:
        test_records.write(RUNTIME_DIR, test_id, 'configuration.json', configuration)
    except OSError as exc:
        record_error = f'Test configuration could not be saved: {exc}'
    with TEST_RECORDER_LOCK:
        TEST_RECORDER.clear()
        TEST_RECORDER.update(active=True, scenario_id=scenario.get("id"), name=scenario.get("name"), link_id=link_id,
                             started_at=started_at, test_id=test_id, record_error=record_error,
                             run_ids=[], phases=[phase["name"] for phase in phases],
                             phase_marks=[], phase_ends=[], samples=[], assertions=[], reactions=[], pauses=[], manual_navigation=[],
                             live_window_seconds=scenario.get("live_window_seconds", 15))


def recorder_note(kind: str, item: dict):
    with TEST_RECORDER_LOCK:
        if TEST_RECORDER.get("active"):
            TEST_RECORDER[kind].append(item)
            test_id = TEST_RECORDER.get('test_id')
        else:
            test_id = None
    if test_id:
        try:
            test_records.append(RUNTIME_DIR, test_id, kind, item)
        except OSError:
            TEST_RECORDER['record_error'] = 'Test journal could not be written'


# Historical minimum evidence gate; small-sample utility is displayed separately from acceptance.
MIN_PHASE_REQUESTS = 20


def phase_window_dem(seconds, since=None, until=None, run_id=None, intervals=None):
    """The Traffic Simulator's DEM over exactly the seconds one phase ran: what that phase's own transactions scored."""
    until = time.time() if until is None else until
    since = until - seconds if since is None else since
    run_id = run_id or SCENARIO_STATE.get("workload_run_id")
    if not run_id:
        return {"error": "Phase workload identity is unavailable", "scoring_complete": False}
    from urllib.parse import urlencode
    query = urlencode(dict(run_id=run_id, since=since, until=until, **({"intervals": json.dumps(intervals)} if intervals is not None else {})))
    try:
        dem = traffic_generator_request(f"/api/v1/dem/summary?{query}", timeout=7.0)
    except RuntimeError as exc:
        return {"error": str(exc)[:200]}
    applications = {key: {"class": item.get("class"), "requests": item.get("requests") or 0,
                          "success": item.get("availability_pct"), "p95_ms": item.get("p95_ms"),
                          "experience": item.get("experience_score"), "weight": item.get("weight"),
                          "scoring_complete": item.get("scoring_complete"),
                          **{field: item.get(field) for field in ("criticality", "criticality_weight", "experience_targets",
                              "quality", "score_components", "target_violations", "missing_measurements", "evidence_issues", "sample_confidence", "timing")}}
                    for key, item in (dem.get("applications") or {}).items()}
    return {"experience": dem.get("experience_score"), "success": dem.get("availability_pct"),
            "interactive_p95_ms": dem.get("interactive_p95_ms"), "requests": dem.get("requests"),
            "p95_ms": dem.get("p95_ms"), "applications": applications,
            **{key: dem.get(key) for key in ("scoring_complete", "truncated", "evidence_issues", "since", "until", "run_id", "critical_alerts", "intervals", "operation_lifecycle", "application_contributions", "business_acceptance", "coverage_pct", "evidence_coverage_pct", "sample_confidence", "timing", "score_range", "score_status") }}


def recorder_close_phase():
    """Score the phase that is ending from the simulator's window of its own duration, before the next one starts."""
    with TEST_RECORDER_LOCK:
        marks = list(TEST_RECORDER.get("phase_marks") or []) if TEST_RECORDER.get("active") else []
    if not marks:
        return
    last = marks[-1]
    with TEST_RECORDER_LOCK:
        if any(item.get("started_at") == last["at"] for item in TEST_RECORDER.get("phase_ends", [])):
            return
    end = time.time()
    with TEST_RECORDER_LOCK:
        pauses = list(TEST_RECORDER.get("pauses") or [])
    intervals, cursor = [], last["at"]
    for pause in sorted(pauses, key=lambda item: item["start"]):
        left, right = max(last["at"], pause["start"]), min(end, pause.get("end") or end)
        if left >= end or right <= cursor:
            continue
        if left > cursor:
            intervals.append([cursor, left])
        cursor = max(cursor, right)
    if cursor < end:
        intervals.append([cursor, end])
    options = {"intervals": intervals} if pauses else {}
    dem = (phase_window_dem(end - last["at"], since=last["at"], until=end, **options)
           if intervals else {"scoring_complete": False, "error": "No active phase interval"})
    recorder_note("phase_ends", {"name": last["name"], "started_at": last["at"], "ended_at": end,
                                "dem": dem})


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
        "timestamp": time.time(), "run_id": (status.get('run') or {}).get('run_id'),
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
            test_id = TEST_RECORDER.get('test_id')
            run_id = sample.get('run_id')
            if run_id and run_id not in TEST_RECORDER.setdefault('run_ids', []):
                TEST_RECORDER['run_ids'].append(run_id)
        else:
            test_id = None
    if test_id:
        try:
            test_records.append(RUNTIME_DIR, test_id, 'measurement', {'sample': sample, 'dem': dem,
                'links': payload.get('links'), 'steering': payload.get('steering'),
                'generator_health': {key: status.get(key) for key in ('persistence_error', 'dropped_transactions', 'engine_telemetry')},
                'workload': {key: (status.get('run') or {}).get(key) for key in
                    ('run_id', 'profile', 'target', 'target_users', 'activity', 'manifest', 'policy', 'seed',
                     'media_pattern', 'generation_mode', 'traffic_path', 'target_info', 'contract_epoch')}})
        except OSError:
            TEST_RECORDER['record_error'] = 'Test measurement could not be saved'


def _average(values):
    values = [float(value) for value in values if value is not None]
    return round(sum(values) / len(values), 1) if values else None


APPLICATION_LABELS = {
    "web_saas": "Web / SaaS", "collaboration": "Collaboration", "voice": "Voice", "video": "Video meeting",
    "file_sync": "Cloud file sync", "developer": "Developer / packages", "updates": "Software updates",
    "backup": "Backup", "dns": "DNS", "ot_telemetry": "OT telemetry", "mes": "MES", "erp": "ERP",
    "plm_cad": "PLM / CAD", "pos": "Point of sale", "wms_scan": "Warehouse scanning", "emr": "Medical records",
    "pacs_imaging": "Medical imaging", "core_banking": "Core banking", "cctv_backhaul": "CCTV backhaul",
    "guest_internet": "Guest internet",
}


def _phase_application(key, item):
    """Keep measured utility visible while preserving the application's acceptance gate."""
    requests = item.get("requests") or 0
    scored = item.get("scoring_complete") is True
    invalid = any(issue != 'outstanding_overdue_operations' for issue in item.get('evidence_issues') or [])
    return {"key": key, "class": item.get("class"), "requests": requests,
            "experience": item.get("experience") if not invalid else None,
            "success": item.get("success") if not invalid else None,
            "p95_ms": item.get("p95_ms") if not invalid else None, "weight": item.get("weight"), "scoring_complete": scored,
            "sample_confidence": item.get('sample_confidence'), "timing": item.get('timing')}


def _figures_text(experience, success, p95_ms, requests):
    """One cell of the application table: score, request success and P95, with the transactions behind them."""
    if experience is None and success is None:
        return f"{requests} tx, too few to score" if requests else "—"
    parts = [f"{experience:.0f}" if experience is not None else "—",
             f"{success:.1f}%" if success is not None else "—",
             f"{p95_ms:.0f} ms" if p95_ms is not None else "—"]
    return " · ".join(parts) + f" ({requests} tx)"


def application_matrix(phases):
    """Per application group and application, what each phase measured from its own window."""
    groups = []
    for traffic_class, label in STEERING_CLASSES:
        keys = sorted({key for phase in phases for key, item in phase["applications"].items()
                       if item.get("class") == traffic_class})
        if not keys:
            continue
        apps = [{"key": key, "label": APPLICATION_LABELS.get(key, key.replace("_", " ").capitalize()), "cells": {}}
                for key in keys]
        cells = {}
        for phase in phases:
            present = [phase["applications"][key] for key in keys if key in phase["applications"]]
            scored = [item for item in present if item["experience"] is not None]
            pooled = [item for item in scored if item["success"] is not None]
            weight = sum(item["requests"] for item in pooled)
            success = sum(item["success"] * item["requests"] for item in pooled) / weight if weight else None
            p95s = [item["p95_ms"] for item in scored if item["p95_ms"] is not None]
            if scored:
                weights = [item.get("weight") if item.get("weight") is not None else 1 for item in scored]
                group_score = sum(item["experience"] * weight for item, weight in zip(scored, weights)) / sum(weights) if sum(weights) else None
                cells[phase["name"]] = _figures_text(group_score, success,
                                                     max(p95s, default=None), sum(item["requests"] for item in scored)) + " [worst app P95]"
                if any(item.get('scoring_complete') is not True for item in present):
                    cells[phase['name']] += ' [limited evidence]'
            else:
                cells[phase["name"]] = _figures_text(None, None, None, sum(item["requests"] for item in present))
            for row in apps:
                item = phase["applications"].get(row["key"])
                row["cells"][phase["name"]] = (_figures_text(item["experience"], item["success"], item["p95_ms"],
                                                             item["requests"]) if item else "—")
                if item and item.get('scoring_complete') is not True and item.get('experience') is not None:
                    row['cells'][phase['name']] += ' [limited evidence]'
        groups.append({"label": label, "cells": cells, "apps": apps})
    return groups


def summarize_test(record: dict, result: str, error, targets=None, site_label=None, ended_at=None):
    """Turn a recorded test into what happened, performance per phase, remediation and checks."""
    ended_at = ended_at or time.time()
    samples = record.get("samples") or []
    marks = record.get("phase_marks") or []
    phases = []
    visits = [(item['name'], item) for item in marks]
    visits += [(name, None) for name in record.get('phases') or [] if not any(item['name'] == name for item in marks)]
    for name, mark in visits:
        following = next((item for item in marks if mark and item['at'] > mark['at']), None)
        rows = [sample for sample in samples if sample.get('phase') == name
                and (not mark or (sample.get('timestamp', mark['at']) >= mark['at']
                     and (not following or sample.get('timestamp', mark['at']) < following['at'])))]
        measured = [sample for sample in rows if sample.get("experience") is not None]
        last = measured[-1] if measured else {}
        following = next((item for item in marks if mark and item["at"] > mark["at"]), None)
        links = {}
        for sample in rows:
            for link in sample.get("links") or []:
                links.setdefault(link["id"], {"label": link["label"], "down": [], "up": [], "health": None})
                links[link["id"]]["down"].append(link.get("down_mbps"))
                links[link["id"]]["up"].append(link.get("up_mbps"))
                links[link["id"]]["health"] = link.get("health") or links[link["id"]]["health"]
        window = next((item["dem"] for item in record.get("phase_ends") or []
                       if item.get("name") == name and (not mark or item.get("started_at") == mark["at"]
                           or (item.get("started_at") is None and sum(m["name"] == name for m in marks) <= 1))
                       and "error" not in (item.get("dem") or {})), None)
        if window is not None:
            transactions = window.get("requests") or 0
            scored = window.get("scoring_complete") is True and not window.get("truncated") and not window.get("evidence_issues") and (bool(window.get("intervals")) or not any(row.get("paused") for row in rows))
            invalid = window.get('truncated') or any(issue != 'outstanding_overdue_operations' for issue in window.get('evidence_issues') or [])
            experience = window.get("experience") if not invalid else None
            success = window.get("success") if not invalid else None
            p95 = window.get("interactive_p95_ms") if not invalid else None
            note = None if scored else f"Incomplete phase evidence ({transactions} transactions); no target verdict."
        else:
            experience, success, p95 = None, None, None
            transactions, note = None, "Exact phase evidence unavailable; rolling measurements were not substituted."
        applications = {key: _phase_application(key, item)
                        for key, item in ((window or {}).get("applications") or {}).items()}
        site_misses = []
        for value, target, label, higher in (
            (success, (targets or {}).get("success_min_pct"), "Request success", True),
            (p95, (targets or {}).get("interactive_p95_max_ms"), "Application completion P95", False),
        ):
            if value is not None and target is not None and (value < target if higher else value > target):
                site_misses.append(f"{label}: {value:g} (target {'≥' if higher else '≤'} {target:g})")
        grade = display_score(window or {}, experience, (targets or {}).get("experience_min"), showroom_app_label, site_misses)
        experience = grade["score"]
        phases.append({
            "name": name, "reached": mark is not None, "applications": applications,
            "duration_s": round((following["at"] if following else ended_at) - mark["at"]) if mark else None,
            "experience_score": experience, "success_pct": success,
            "average_experience_score": grade["average_score"], "score_adjusted": grade["score_adjusted"],
            "target_failures": grade["target_failures"],
            "user_impact": showcase_summary.application_impact(window or {}),
            "interactive_p95_ms": p95, "transactions": transactions, "note": note,
            "critical_alerts": (window or {}).get("critical_alerts", []),
            **{key: (window or {}).get(key) for key in ("business_acceptance", "evidence_issues", "truncated", "coverage_pct", "evidence_coverage_pct", "sample_confidence", "timing", "score_range", "score_status")},
            "worst_success_pct": success,
            "wans": [{"label": item["label"], "down_mbps": _average(item["down"]), "up_mbps": _average(item["up"]),
                      "health": item["health"]} for item in links.values()],
            "steering": {label: entry["verdict"] for label, entry in ((rows[-1] if rows else {}).get("steering") or {}).items()},
        })
    assertions = record.get("assertions") or []
    failed = [item for item in assertions if not item.get("passed")]
    steering_target = (targets or {}).get("steering_max_s")
    remediation = [dict(item, within_target=None if steering_target is None else item["seconds"] <= steering_target)
                   for item in record.get("reactions") or []]

    evaluated = [phase for phase in phases if phase["reached"]]
    def phase_verdict(phase):
        if confirmed_critical_failure(phase):
            return "fail"
        if phase["note"] or not targets:
            return "unknown"
        if phase.get("experience_score") is not None and targets.get("experience_min") is not None and phase["experience_score"] < targets["experience_min"]:
            return "fail"
        if phase.get("business_acceptance") == "unknown":
            return "unknown"
        if phase.get("business_acceptance") == "pass":
            value, target = phase.get("experience_score"), targets.get("experience_min")
            return "unknown" if value is None or target is None else "pass" if value >= target else "fail"
        checks = [(phase["experience_score"], targets.get("experience_min"), True),
                  (phase["success_pct"], targets.get("success_min_pct"), True),
                  (phase["interactive_p95_ms"], targets.get("interactive_p95_max_ms"), False)]
        if any(actual is None or target is None for actual, target, _ in checks):
            return "unknown"
        return "pass" if all(actual >= target if higher else actual <= target for actual, target, higher in checks) else "fail"
    for phase in evaluated:
        phase["experience_verdict"] = phase_verdict(phase)
    execution_result = record.get("execution_result") or result
    if execution_result == "interrupted" and evaluated:
        # A partial last phase cannot confirm successful recovery. Independently
        # measured failures remain visible even when execution is interrupted.
        if evaluated[-1]["experience_verdict"] == "pass":
            evaluated[-1]["experience_verdict"] = "unknown"
    verdicts = [phase["experience_verdict"] for phase in evaluated]
    experience_result = "fail" if "fail" in verdicts else "pass" if execution_result != "interrupted" and verdicts and all(value == "pass" for value in verdicts) else "unknown"
    if record.get('manual_navigation') and experience_result == 'pass':
        experience_result = 'unknown'
    execution_label = ("demonstration interrupted" if execution_result == "interrupted"
                       else "execution completed" if result == "passed" and not assertions else result)
    conclusion = [f"{record.get('name') or 'Test'} {execution_label}" +
                  (f": {len(assertions) - len(failed)} of {len(assertions)} checks passed." if assertions else ".")]
    if record.get('manual_navigation'):
        conclusion.append('Manually navigated test: phases were skipped or revisited; this run does not validate the full planned sequence.')
    if error and result != "passed":
        conclusion.append(str(error)[:200])
    if execution_result == "interrupted":
        conclusion.append("Execution ended before completion. Measurements collected before interruption are retained; incomplete phases cannot confirm resilience or recovery.")
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
            scope = "during the recorded phases" if execution_result == "interrupted" else "through the test"
            conclusion.append(f"Experience stayed at {base['experience_score']:.0f} {scope}.")
    else:
        conclusion.append("No simulated user traffic was measured, so user impact and steering were not assessed.")
    for phase in phases:
        if phase.get("target_failures"):
            conclusion.append(f"{phase['name']} missed targets: " + "; ".join(phase["target_failures"]) + ".")
        if phase.get("score_adjusted"):
            conclusion.append(f"{phase['name']} uses a target-aligned experience grade of {phase['experience_score']:.0f}/100; "
                              f"the measured application average was {phase['average_experience_score']:.0f}/100. "
                              "Confirmed critical or site target misses keep the grade below the passing band.")
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
    application_groups = application_matrix(phases)
    return {
        "manual_navigation": record.get("manual_navigation") or [],
        "live_window_seconds": record.get("live_window_seconds", 15),
        "name": record.get("name"), "scenario_id": record.get("scenario_id"), "link_id": record.get("link_id"),
        "site": site_label, "result": result, "execution_result": execution_result, "experience_result": experience_result, "started_at": record.get("started_at"), "ended_at": ended_at,
        "duration_s": round(ended_at - (record.get("started_at") or ended_at)),
        "phases": phases, "user_impact": showcase_summary.observed_impact({"phases": phases}),
        "remediation": remediation, "steering_target_s": steering_target,
        "assertions": {"passed": len(assertions) - len(failed), "total": len(assertions),
                       "items": [{key: item.get(key) for key in ("label", "passed", "observed", "phase")} for item in assertions]},
        "conclusion": conclusion, "narrative": narrative,
        "application_groups": application_groups, "min_phase_requests": MIN_PHASE_REQUESTS,
    }


def recorder_finish(result: str, error):
    global LAST_TEST_SUMMARY
    recorder_close_phase()
    with TEST_RECORDER_LOCK:
        record = copy.deepcopy(TEST_RECORDER)
        TEST_RECORDER.clear()
        TEST_RECORDER["active"] = False
    if not record.get("active"):
        return None
    if SCENARIO_STATE.get('workload_run_id') and SCENARIO_STATE['workload_run_id'] not in record.setdefault('run_ids', []):
        record['run_ids'].append(SCENARIO_STATE['workload_run_id'])
    cfg = load_config()
    plan = active_site_plan(cfg)
    summary = summarize_test(record, result, error, (plan or {}).get("targets"), (plan or {}).get("label"))
    link = get_link(cfg, record.get("link_id") or "")
    summary["link"] = (link or {}).get("name") or record.get("link_id")
    if record.get('test_id'):
        summary['test_id'] = record['test_id']
        summary['record_complete'] = False  # Crash during collection must remain explicitly partial.
        try:
            test_records.write(RUNTIME_DIR, record['test_id'], 'record.json', record)
            test_records.write(RUNTIME_DIR, record['test_id'], 'summary.json', summary)
            export = test_records.collect_agent(RUNTIME_DIR, record['test_id'], record.get('run_ids', []),
                record['started_at'], summary['ended_at'], traffic_generator_request)
            # Preserve the full WAN/host/probe sample history, independently of UI polling.
            telemetry_complete = True
            with telemetry_connect() as conn:
                for table in ('telemetry_samples', 'host_samples', 'probe_samples'):
                    rows = conn.execute(f'SELECT * FROM {table} WHERE timestamp>=? AND timestamp<? ORDER BY timestamp LIMIT 100001',
                        (record['started_at'], summary['ended_at'])).fetchall()
                    test_records.write(RUNTIME_DIR, record['test_id'], table + '.json',
                        {'complete': len(rows) <= 100000, 'items': [dict(row) for row in rows[:100000]]})
                    telemetry_complete = telemetry_complete and len(rows) <= 100000
            summary['record_complete'] = (export['complete'] and telemetry_complete and not record.get('record_error')
                and not (test_records.directory(RUNTIME_DIR, record['test_id']) / 'journal-truncated.json').exists())
            test_records.write(RUNTIME_DIR, record['test_id'], 'summary.json', summary)
            folder = test_records.directory(RUNTIME_DIR, record['test_id'])
            (folder / 'README.txt').write_text(
                '\n'.join(summary.get('narrative') or summary.get('conclusion') or []) +
                '\n\nconfiguration.json: configuration at test start (credentials redacted).\n'
                'timeline.jsonl: events and detailed live measurements, including confidence and queue evidence.\n'
                'record.json: phase boundaries, pauses, full-phase summaries, checks and reactions.\n'
                'agent-*.json: raw completed transactions, operation events and scoring contracts.\n'
                'agent-export-status.json: completeness and any retrieval errors.\n'
                '*_samples.json: WAN, host and probe evidence, with explicit row-limit indicators.\n'
                'Application response time includes upload, target processing and body transfer.\n'
                'Packet RTD/RTT is UDP echo round-trip time; one-way network latency is not measured.\n'
                'Unix timestamps use each host clock. Correlation requires synchronized host clocks.\n'
                'An unfinished record has no summary; missing evidence is never a successful test.\n', encoding='utf-8')
            test_records.archive(RUNTIME_DIR, record['test_id'])
        except (OSError, ValueError, sqlite3.Error) as exc:
            summary['record_complete'] = False
            summary['record_error'] = str(exc)[:200]
    LAST_TEST_SUMMARY = summary
    try:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        LAST_TEST_SUMMARY_PATH.write_text(json.dumps(summary))
    except OSError:
        pass
    log_event("test-summary", summary["conclusion"][0], summary=summary)
    return summary


class PhaseNavigation(Exception):
    """Interrupt only the scenario worker's waits; never execute actions in an HTTP thread."""


def check_phase_navigation():
    if not SCENARIO_STOP.is_set() and SCENARIO_STATE.get('phase_jump') is not None:
        raise PhaseNavigation()


def restore_phase_prefix(original, steps, destination, presets):
    """Compute destination WAN state, then apply it without replaying intermediate outages."""
    profile, fault, mtu = copy.deepcopy(original), 'normal', 0
    # Include the first destination action so jumping straight to recovery never
    # briefly re-injects the outage from an earlier phase.
    for step in steps[:destination + 1]:
        action = step.get('action')
        if action == 'quality':
            profile = copy.deepcopy(original)
            profile.update(mode='quality', quality=int(step.get('value', 100)))
            profile.pop('custom_profile', None)
            fault = 'normal'
        elif action == 'fault':
            fault = step.get('value', 'normal')
        elif action == 'mtu':
            mtu = int(step.get('value', 0))
    ok, message = apply_mtu_limit(profile, 0)
    if not ok:
        raise RuntimeError(message)
    ok, message, _ = apply_selected_profile(profile, presets)
    if not ok:
        raise RuntimeError(message)
    ACTIVE_FAULTS.pop(original.get('id'), None)
    if fault != 'normal':
        ok, message = apply_runtime_fault(profile, fault, presets)
        if not ok:
            raise RuntimeError(message)
    if mtu:
        ok, message = apply_mtu_limit(profile, mtu)
        if not ok:
            raise RuntimeError(message)
    return profile


def accept_phase_navigation(original, steps, phase_indexes, presets):
    """Close the old visit and reset the destination's evidence identity before re-entering it."""
    with RUNTIME_LOCK:
        target = SCENARIO_STATE.get('phase_jump')
        SCENARIO_STATE.update(phase_jump=None, navigating=True)
    destination = phase_indexes.index(target)
    recorder_close_phase()
    change = {'at': time.time(), 'from': SCENARIO_STATE.get('phase'),
              'to': SCENARIO_STATE['phases'][target]['name']}
    recorder_note('manual_navigation', change)
    with RUNTIME_LOCK:
        if SCENARIO_STATE.get('paused'):
            origin = SCENARIO_STATE.get('paused_clock')
            held = time.monotonic() - origin if origin is not None else 0
            SCENARIO_STATE['paused_total_s'] = (SCENARIO_STATE.get('paused_total_s') or 0) + held
            with TEST_RECORDER_LOCK:
                if TEST_RECORDER.get('pauses') and TEST_RECORDER['pauses'][-1]['end'] is None:
                    TEST_RECORDER['pauses'][-1]['end'] = time.time()
        SCENARIO_PAUSE.clear()
        SCENARIO_STATE.update(paused=False, paused_at=None, paused_clock=None, phase_index=None,
                              condition=None, phase_revision=SCENARIO_STATE.get('phase_revision', 0) + 1)
    profile = restore_phase_prefix(original, steps, destination, presets)
    log_event('phase-navigation', f"Moved from {change['from']} to {change['to']}; destination phase restarted", **change)
    return destination + 1, profile


def scenario_sleep(seconds: float):
    """Wait out a step delay. Paused time does not count, and a paused test holds here
    in its current phase until resumed. Returns True when the test was stopped."""
    remaining = max(0.0, float(seconds))
    check_phase_navigation()
    check_test_workload()
    while remaining > 0 or SCENARIO_PAUSE.is_set():
        if SCENARIO_STOP.is_set():
            return True
        check_phase_navigation()
        check_test_workload()
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


def step_summary(step):
    """What a step does to the WAN or the workload, in a few words; None for checks and waits."""
    action, label = step.get("action"), step.get("label")
    if action == "traffic_generator" and (step.get("value") or {}).get("operation") != "start":
        return None
    if action not in ("quality", "fault", "mtu", "phase", "traffic_generator"):
        return None
    if label:
        return str(label)
    return {"quality": f"Quality {step.get('value')}%", "fault": words_of(step.get("value")),
            "mtu": f"MTU {step.get('value')}", "traffic_generator": "Start simulated users"}.get(action)


def words_of(value):
    return str(value or "").replace("_", " ").capitalize()


def scenario_preview(steps):
    """The plan announced before a test starts: each phase with what it does, and the checks."""
    phases, planned_s = scenario_phases(steps)
    indexes = step_phase_indexes(steps)
    actions = [[] for _phase in phases]
    for step, index in zip(steps, indexes):
        text = step_summary(step)
        if text and text != phases[index]["name"] and text not in actions[index]:
            actions[index].append(text)
    checks = []
    for step in steps:
        if step.get("action") == "assert" and step.get("label") and step["label"] not in checks:
            checks.append(str(step["label"]))
    return ([{"name": phase["name"], "planned_s": phase["planned_s"], "what": " → ".join(actions[index][:3])}
             for index, phase in enumerate(phases)], planned_s, checks)


def announce_test(scenario, seconds):
    """Brief the audience before the test changes anything.
    Returns False when the test was stopped during the announcement."""
    phases, planned_s, checks = scenario_preview(scenario.get("steps", []))
    cfg = load_config()
    current_session = session_snapshot()
    site = current_session.get('site') if current_session.get('active') else None
    plan = active_site_plan({'site_profile': site} if site else cfg)
    brief = showcase_summary.briefing(scenario, (plan or {}).get('targets') or SHOWROOM_LAB_TARGETS,
                                     scenario.get('live_window_seconds') or 15)
    with RUNTIME_LOCK:
        SCENARIO_STATE.update(intro=True, intro_clock=time.monotonic() + seconds, step_label=f"Starting in {seconds:g} s",
                              description=scenario.get("description"), checks=checks, phases=phases,
                              phase_index=None, phase=None, planned_s=planned_s, phase_started_s=None, briefing=brief)
    log_event("scenario", f'{scenario["name"]} announced, starting in {seconds:g} s', scenario_id=scenario.get("id"))
    return not SCENARIO_STOP.wait(seconds)


def reset_scenario_state(result, error):
    with RUNTIME_LOCK:
        # Finish teardown before admitting another session or test.
        SCENARIO_PAUSE.clear()
        SCENARIO_STOP.clear()
        if EPHEMERAL_TELEMETRY is not None:
            end_unsaved_telemetry()
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
                "result": result,
                "error": error,
                "phases": [],
                "phase_index": None,
                "phase": None,
                "planned_s": None,
                "paused": False,
                "paused_at": None,
                "paused_total_s": 0.0,
                "phase_started_s": None,
                "workload_run_id": None,
                "preparing_workload": False,
                "workload_checked": None,
                "workload_supervised": False,
                "clock_start": None,
                "paused_clock": None,
                "intro": False,
                "intro_clock": None,
                "description": None,
                "checks": [],
                "briefing": None,
                "unsaved": False,
                "phase_jump": None,
                "phase_revision": 0,
                "navigating": False,
                "finishing": False,
            }
        )


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


def wait_for_test_executor(status, run_id, timeout_s=30):
    """Do not start impairment phases until the executing agent has active users."""
    deadline = time.monotonic() + timeout_s
    while not SCENARIO_STOP.is_set():
        if (status.get("run") or {}).get("run_id") != run_id or status.get("status") not in ("starting", "running"):
            raise RuntimeError("Traffic Simulator is not executing the test's workload. No impairment phase started.")
        if status.get("users", 0) > 0:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError("Traffic Simulator did not start any users within 30 seconds.")
        if SCENARIO_STOP.wait(.5):
            break
        status = traffic_generator_request("/api/v1/controller/heartbeat", method="POST",
            payload={"run_id": run_id, "phase": "Starting workload"}, timeout=3.0)
    raise InterruptedError("Test stopped while waiting for the executing agent.")


class SimulatorHeartbeatTimeout(RuntimeError):
    """The bounded controller heartbeat retry was exhausted."""


def test_controller_heartbeat(payload):
    # Only this idempotent control request may be repeated. Never retry workload
    # starts, application traffic, HTTP rejections, or certificate/auth failures.
    for attempt in range(2):
        try:
            return traffic_generator_request("/api/v1/controller/heartbeat", method="POST",
                                             payload=payload, timeout=3.0)
        except RuntimeError as exc:
            if not isinstance(exc.__cause__, TimeoutError):
                raise
            if attempt:
                raise SimulatorHeartbeatTimeout("Simulator control timed out twice; demonstration interrupted.") from exc
            if SCENARIO_STOP.is_set():
                raise InterruptedError("Test stopped during simulator control timeout.") from exc
            log_event("traffic-generator", "Simulator heartbeat timed out; retrying once",
                      run_id=payload.get("run_id"), phase=payload.get("phase"))


def check_test_workload():
    """A phase cannot silently outlive its executing workload; check every 5 s."""
    with RUNTIME_LOCK:
        expected = SCENARIO_STATE.get("workload_run_id")
        if not SCENARIO_STATE.get("workload_supervised") or not expected or SCENARIO_STOP.is_set():
            return
        now = time.monotonic()
        checked = SCENARIO_STATE.get("workload_checked")
        if checked is not None and now - checked < 5:
            return
        SCENARIO_STATE["workload_checked"] = now
    status = test_controller_heartbeat(
        {"run_id": expected, "phase": str(SCENARIO_STATE.get('phase') or '')[:120], "live_context": test_live_context()})
    if ((status.get("run") or {}).get("run_id") != expected
            or status.get("status") not in ("starting", "running") or status.get("users", 0) <= 0):
        raise RuntimeError("Traffic Simulator stopped executing the test's workload. Test aborted and WAN baseline restored.")


def stop_test_owned_workload(run_id):
    status = traffic_generator_request("/api/v1/status", timeout=3.0)
    if (status.get("run") or {}).get("run_id") == run_id and status.get("status") in ("starting", "running"):
        traffic_generator_request("/api/v1/workloads/stop", method="POST", payload={"run_id": run_id}, timeout=5.0)


def requested_live_window(value=None):
    if value in (None, ''):
        return 15
    try:
        number = float(value)
    except (ValueError, TypeError):
        number = float('nan')
    if isinstance(value, bool) or not math.isfinite(number) or not number.is_integer() or not 5 <= number <= 120:
        raise ValueError('Live experience window must be an integer from 5 to 120 seconds.')
    return int(number)


def test_live_context():
    with RUNTIME_LOCK:
        state = dict(SCENARIO_STATE)
    return {'window_seconds': state.get('live_window_seconds', 15),
            'phase_id': f"{state.get('scenario_id')}:{state.get('started_at')}:{state.get('phase_index')}:{state.get('phase_revision', 0)}",
            'paused': bool(state.get('paused'))}


def publish_test_live_context():
    with RUNTIME_LOCK:
        run_id = SCENARIO_STATE.get('workload_run_id')
        supervised = SCENARIO_STATE.get('workload_supervised')
    if not run_id or not supervised:
        return
    context = test_live_context()
    status = test_controller_heartbeat(
        {'run_id': run_id, 'phase': str(SCENARIO_STATE.get('phase') or '')[:120], 'live_context': context})
    if ((status.get('run') or {}).get('run_id') != run_id
            or status.get('status') not in ('starting', 'running') or status.get('users', 0) <= 0):
        raise RuntimeError("Traffic Simulator stopped executing the test's workload. Test aborted and WAN baseline restored.")
    acknowledged = (status.get('dem') or {}).get('live_phase_id')
    if acknowledged != context['phase_id']:
        raise RuntimeError('The simulator did not acknowledge the current live measurement phase. Upgrade the simulator before running this test.')
    with DIAGNOSIS_LOCK:
        DIAGNOSIS_CACHE['checked'] = None
    return status


def prepare_manual_workload(cfg, scenario):
    """NetEm owns automatic starts; an existing executing workload is borrowed."""
    ensure_traffic_path(scenario["name"])
    status = traffic_generator_request("/api/v1/status", timeout=3.0)
    if status.get("status") in ("starting", "running"):
        return status, False
    if status.get("status") == "stopping":
        raise RuntimeError("Wait for the Traffic Simulator workload to stop before starting a test.")
    plan = active_site_plan(cfg, simulator_catalog())
    payload = dict(plan["start"]) if plan else {"profile": "office", "users": 50, "spawn_rate": 5, "activity": "normal", "pattern": "steady"}
    payload.pop("operation", None)
    payload['controller_lease_seconds'] = 30
    payload['live_window_seconds'] = scenario.get('live_window_seconds', 15)
    return traffic_generator_request("/api/v1/workloads/start", method="POST", payload=payload, timeout=5.0), True


def run_scenario(link_id: str, scenario: dict, intro_s: float = 0):
    cfg = load_config()
    presets = get_presets(cfg)
    link = get_link(cfg, link_id)
    if not link:
        with RUNTIME_LOCK:
            SCENARIO_STATE.update(
                {"active": False, "result": "failed", "error": "Unknown WAN"}
            )
        return
    if intro_s > 0 and not announce_test(scenario, intro_s):
        log_event("scenario", f'{scenario["name"]} cancelled before it started', scenario_id=scenario.get("id"),
                  link_id=link_id, result="stopped")
        reset_scenario_state("stopped", None)
        return

    original = copy.deepcopy(link)
    runtime_profile = copy.deepcopy(original)
    scenario_result = "passed"
    scenario_workload_id = None
    scenario_error = None
    started_at = time.time()
    steps = scenario.get("steps", [])
    preview, planned_s, checks = scenario_preview(steps)
    phases = scenario_phases(steps)[0]
    phase_indexes = step_phase_indexes(steps)
    supervised = bool(scenario.get("controller_managed"))
    explicit_workload = any(step.get("action") == "traffic_generator" for step in steps)
    if scenario.get("auto_workload") and not explicit_workload:
        with RUNTIME_LOCK:
            SCENARIO_STATE.update(preparing_workload=True, intro=False, phase=None, phase_index=None,
                                  step_label="Starting traffic on the executing agent", workload_supervised=False)
        try:
            status, owned = prepare_manual_workload(cfg, scenario)
            expected = (status.get("run") or {}).get("run_id")
            if owned:
                scenario_workload_id = expected
            if not expected:
                raise RuntimeError("Traffic Simulator did not acknowledge a workload run ID.")
            with RUNTIME_LOCK:
                SCENARIO_STATE["workload_run_id"] = expected
            wait_for_test_executor(status, expected)
            started_at = time.time()
        except Exception as exc:
            if scenario_workload_id:
                try:
                    stop_test_owned_workload(scenario_workload_id)
                except Exception as cleanup:
                    log_event("traffic-generator", "Workload cleanup failed during startup", error=str(cleanup)[:240])
            result = "stopped" if SCENARIO_STOP.is_set() else "failed"
            log_event("scenario", f'{scenario["name"]}: workload startup {result}', error=str(exc)[:240])
            reset_scenario_state(result, str(exc)[:240])
            return
    with RUNTIME_LOCK:
        # The test clock starts now, after any announcement.
        SCENARIO_STATE.update(phases=preview, phase_index=0 if phases else None, phase=phases[0]["name"] if phases else None,
                              planned_s=planned_s, paused=False, paused_at=None, paused_total_s=0.0,
                              phase_started_s=0.0 if phases else None, intro=False, intro_clock=None,
                              description=scenario.get("description"), checks=checks,
                              started_at=started_at, clock_start=time.monotonic(), step_label="Starting",
                              preparing_workload=False, workload_supervised=supervised, workload_checked=None,
                              live_window_seconds=scenario.get("live_window_seconds", 15))
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
        publish_test_live_context()
        index, skip_delay = 1, False
        while index <= len(steps):
            step = steps[index - 1]
            try:
                if scenario_sleep(0 if skip_delay else max(0, int(step.get("after", 0)))):
                    scenario_result = "stopped"
                    break

                skip_delay = False
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
                    recorder_close_phase()
                    recorder_note("phase_marks", {"name": phases[phase_index]["name"], "at": time.time()})
                    publish_test_live_context()
                    with RUNTIME_LOCK:
                        SCENARIO_STATE["navigating"] = False
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
                    if operation == "start" and SCENARIO_STATE.get('phase_revision', 0) and SCENARIO_STATE.get('workload_run_id'):
                        index += 1
                        continue
                    if operation == "start":
                        ensure_traffic_path(scenario["name"])
                        payload['live_window_seconds'] = scenario.get('live_window_seconds', 15)
                        if supervised:
                            payload['controller_lease_seconds'] = 30
                    elif operation == 'stop' and supervised and scenario_workload_id:
                        payload['run_id'] = scenario_workload_id
                    if operation == "stop":
                        recorder_close_phase()
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
                        SCENARIO_STATE["workload_checked"] = None
                    if operation == "start" and supervised:
                        if not scenario_workload_id:
                            raise RuntimeError("Traffic Simulator did not acknowledge the requested workload.")
                        wait_for_test_executor(result, scenario_workload_id)
                        publish_test_live_context()
                    with RUNTIME_LOCK:
                        SCENARIO_STATE["navigating"] = False
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
                index += 1
                with RUNTIME_LOCK:
                    check_phase_navigation()
                    if index > len(steps):
                        SCENARIO_STATE['finishing'] = True
            except PhaseNavigation:
                if SCENARIO_STOP.is_set():
                    scenario_result = 'stopped'
                    break
                index, runtime_profile = accept_phase_navigation(original, steps, phase_indexes, presets)
                skip_delay = True

        if SCENARIO_STOP.is_set() and scenario_result == "passed":
            scenario_result = "stopped"

    except Exception as exc:
        scenario_result = "stopped" if isinstance(exc, InterruptedError) and SCENARIO_STOP.is_set() else "failed"
        scenario_error = str(exc)[:240]
        with TEST_RECORDER_LOCK:
            if TEST_RECORDER.get("active") and scenario_result == "failed":
                TEST_RECORDER["execution_result"] = "interrupted"

    finally:
        with RUNTIME_LOCK:
            SCENARIO_STATE['finishing'] = True
        try:
            recorder_close_phase()
        except Exception as exc:
            log_event("scenario", "Final phase evidence unavailable", error=str(exc)[:200])
        if scenario_workload_id:
            try:
                stop_test_owned_workload(scenario_workload_id)
                log_event("traffic-generator", "Scenario-owned workload cleanup completed", scenario_id=scenario.get("id"), run_id=scenario_workload_id, action="stop")
            except Exception as exc:
                scenario_result = "failed"
                scenario_error = f"{scenario_error + '; ' if scenario_error else ''}Workload cleanup failed: {exc}"[:240]
        elif supervised and SCENARIO_STATE.get('workload_run_id'):
            try:
                traffic_generator_request('/api/v1/controller/heartbeat', method='POST', timeout=3.0,
                    payload={'run_id': SCENARIO_STATE['workload_run_id'], 'phase': '', 'live_context': {'reset': True}})
            except Exception as exc:
                log_event('traffic-generator', 'Borrowed workload phase could not be cleared', error=str(exc)[:240])
        try:
            mtu_ok, mtu_message = apply_mtu_limit(original, 0)
            profile_ok, profile_message, _ = apply_selected_profile(original, presets)
            if not mtu_ok or not profile_ok:
                raise RuntimeError('; '.join(message for ok, message in ((mtu_ok, mtu_message), (profile_ok, profile_message)) if not ok))
        except Exception as exc:
            scenario_result = "failed"
            scenario_error = f"{scenario_error + '; ' if scenario_error else ''}WAN restoration failed: {exc}"[:240]
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
        reset_scenario_state(scenario_result, scenario_error)



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
        effective = runtime.get("effective", {}) if runtime.get("apply_status") == "unknown" else runtime.get("effective") or configured_effective
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
            "apply_status": runtime.get("apply_status", "unverified"),
            "apply_error": runtime.get("apply_error"),
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
    if endpoint == "settings" and request.form.get("return_section") in {group["id"] for group in SETTINGS_GROUPS}:
        return redirect(url_for(endpoint, section=request.form["return_section"]))
    return redirect(url_for(endpoint))


DOC_HELP_BY_ENDPOINT = {
    "inventory": "appliances",
    "platform_health": "appliances",
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

SETTINGS_GROUPS = [
    {"id": "network", "label": "Traffic & network", "items": [
        ("traffic_settings", "network", "Paths & network"),
        ("traffic_settings", "experience", "Experience targets"),
        ("traffic_settings", "maintenance", "Agent & target"),
        ("setup", None, "Topology & interfaces"),
    ]},
    {"id": "profiles", "label": "Profiles & SLA", "items": [
        ("presets", None, "Access profiles"),
    ]},
    {"id": "appearance", "label": "Appearance & appliance", "items": [
        ("branding_settings", None, "Branding"),
    ]},
    {"id": "system", "label": "System & integrations", "items": [
        ("updates", None, "Updates"),
        ("integrations", None, "API & integrations"),
        ("access_settings", None, "Users & access"),
    ]},
]


@app.context_processor
def inject_settings_nav():
    endpoint = request.endpoint
    section = (request.view_args or {}).get("section")
    group_id = None
    if endpoint == "settings":
        group_id = request.args.get("section", "network")
        if group_id not in {group["id"] for group in SETTINGS_GROUPS}:
            group_id = "network"
    else:
        for group in SETTINGS_GROUPS:
            for item_endpoint, item_section, _label in group["items"]:
                if endpoint == item_endpoint and section == item_section:
                    group_id = group["id"]
    return {"settings_groups": SETTINGS_GROUPS, "settings_group": group_id}


# ---------- Time display ----------

TIME_FORMATS = ("24h", "12h")


def time_format(cfg=None):
    """How clock times are shown everywhere: 24-hour unless the operator chose 12-hour."""
    value = (cfg if cfg is not None else load_config()).get("time_format")
    return value if value in TIME_FORMATS else "24h"


def clock_label(timestamp, fmt):
    """Hours and minutes of a Unix time in the chosen format, for server-written labels."""
    moment = time.localtime(timestamp)
    if fmt == "12h":
        return time.strftime("%I:%M %p", moment).lstrip("0")
    return time.strftime("%H:%M", moment)


# ---------- Showcase video ----------
#
# An operator's video for the showcase's waiting screen: a YouTube or Vimeo link, embedded, or an uploaded MP4 or
# WebM file kept in runtime/ and served by the showcase. The showcase embeds only those two hosts.

SHOWCASE_VIDEO_DIR = RUNTIME_DIR / "showcase"
SHOWCASE_VIDEO_TYPES = {".mp4", ".webm"}
SHOWCASE_IMAGE_TYPES = {".gif"}  # an animated GIF loops by itself, with no player
SHOWCASE_MEDIA_TYPES = SHOWCASE_VIDEO_TYPES | SHOWCASE_IMAGE_TYPES
SHOWCASE_VIDEO_MAX_BYTES = 16 * 1024 * 1024
SHOWCASE_EMBED = re.compile(
    r"https://(www\.youtube-nocookie\.com/embed/[A-Za-z0-9_-]{6,20}|player\.vimeo\.com/video/[0-9]{6,})")


def parse_video_link(raw):
    """A YouTube or Vimeo link as its embeddable address. Any other host is refused."""
    text = (raw or "").strip()
    if not text or len(text) > 300:
        raise ValueError("Enter a YouTube or Vimeo link.")
    parsed = urllib.parse.urlsplit(text if "://" in text else "https://" + text)
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    parts = [part for part in parsed.path.split("/") if part]
    video_id = None
    if host in ("youtube.com", "m.youtube.com", "youtube-nocookie.com"):
        query = urllib.parse.parse_qs(parsed.query)
        if parts[:1] == ["watch"] and query.get("v"):
            video_id = query["v"][0]
        elif len(parts) >= 2 and parts[0] in ("embed", "shorts", "live"):
            video_id = parts[1]
    elif host == "youtu.be" and parts:
        video_id = parts[0]
    if host in ("youtube.com", "m.youtube.com", "youtube-nocookie.com", "youtu.be"):
        if video_id and re.fullmatch(r"[A-Za-z0-9_-]{6,20}", video_id):
            return {"provider": "youtube", "id": video_id, "src": f"https://www.youtube-nocookie.com/embed/{video_id}"}
    elif host in ("vimeo.com", "player.vimeo.com"):
        numbers = [part for part in parts if part.isdigit()]
        if numbers and len(numbers[-1]) >= 6:
            return {"provider": "vimeo", "id": numbers[-1], "src": f"https://player.vimeo.com/video/{numbers[-1]}"}
    raise ValueError("Use a YouTube or Vimeo link, or upload an MP4 or WebM file.")


def check_video_upload(filename, data):
    """The suffix of an uploaded animation, once its name, size and contents show an MP4, WebM or animated GIF."""
    suffix = Path(filename or "").suffix.lower()
    if suffix not in SHOWCASE_MEDIA_TYPES:
        raise ValueError("Upload an MP4 or WebM video or an animated GIF, or paste a YouTube or Vimeo link.")
    if len(data) > SHOWCASE_VIDEO_MAX_BYTES:
        raise ValueError("The file is larger than 16 MB. Use a YouTube or Vimeo link for longer videos.")
    is_mp4 = suffix == ".mp4" and data[4:8] == b"ftyp"
    is_webm = suffix == ".webm" and data[:4] == b"\x1aE\xdf\xa3"
    is_gif = suffix == ".gif" and data[:6] in (b"GIF87a", b"GIF89a")
    if not (is_mp4 or is_webm or is_gif):
        raise ValueError("The file is not an MP4, WebM or animated GIF.")
    return suffix


def remove_showcase_video_files():
    for suffix in SHOWCASE_MEDIA_TYPES:
        (SHOWCASE_VIDEO_DIR / f"video{suffix}").unlink(missing_ok=True)


def showcase_video_file():
    """The uploaded showcase video's path, when one is stored."""
    video = load_config().get("showcase_video")
    if not isinstance(video, dict) or video.get("kind") != "file":
        return None
    path = SHOWCASE_VIDEO_DIR / str(video.get("name") or "")
    return path if path.suffix in SHOWCASE_MEDIA_TYPES and path.is_file() else None


def showcase_video_public(cfg=None):
    """The showcase video as the showcase may publish it: an embed address, or the route that serves the file."""
    video = (cfg if cfg is not None else load_config()).get("showcase_video")
    if not isinstance(video, dict):
        return None
    if video.get("kind") == "embed" and SHOWCASE_EMBED.fullmatch(str(video.get("src") or "")):
        return {"kind": "embed", "provider": video.get("provider"), "src": video["src"]}
    path = showcase_video_file() if video.get("kind") == "file" else None
    if path:
        return {"kind": "file", "src": "/assets/showcase-video", "media": "image" if path.suffix in SHOWCASE_IMAGE_TYPES else "video"}
    return None


@app.route("/settings/showcase-video", methods=["POST"])
def showcase_video_save():
    cfg = load_config()
    back = url_for("settings", section="appearance")
    if request.form.get("remove") == "1":
        remove_showcase_video_files()
        cfg.pop("showcase_video", None)
        save_config(cfg)
        log_event("settings", "Showcase video removed")
        flash("The showcase video is removed. The waiting screen shows its animation again.", "success")
        return redirect(back)
    upload = request.files.get("video_file")
    try:
        if upload and upload.filename:
            data = upload.read()
            suffix = check_video_upload(upload.filename, data)
            remove_showcase_video_files()
            SHOWCASE_VIDEO_DIR.mkdir(parents=True, exist_ok=True)
            (SHOWCASE_VIDEO_DIR / f"video{suffix}").write_bytes(data)
            cfg["showcase_video"] = {"kind": "file", "name": f"video{suffix}", "title": Path(upload.filename).name[:120]}
            message = "Showcase animation or video uploaded."
        else:
            link = parse_video_link(request.form.get("video_link"))
            remove_showcase_video_files()
            cfg["showcase_video"] = {"kind": "embed", **link}
            message = "Showcase video linked."
    except (ValueError, OSError) as exc:
        flash(str(exc), "error")
        return redirect(back)
    save_config(cfg)
    log_event("settings", message)
    flash(message, "success")
    return redirect(back)


@app.route("/settings/time-format", methods=["POST"])
def time_format_save():
    value = request.form.get("time_format", "")
    if value not in TIME_FORMATS:
        flash("Choose 24-hour or 12-hour time.", "error")
        return redirect(url_for("settings", section="appearance"))
    cfg = load_config()
    cfg["time_format"] = value
    save_config(cfg)
    log_event("settings", f"Clock times now shown in {'24-hour' if value == '24h' else '12-hour'} format", time_format=value)
    flash(f"Clock times now use the {'24-hour' if value == '24h' else '12-hour'} format.", "success")
    return redirect(url_for("settings", section="appearance"))


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
                "label": "Infrastructure",
                "items": [
                    {"id": "inventory", "label": "Appliances", "endpoint": "inventory", "icon": "wan"},
                    {"id": "platform_health", "label": "Platform Health", "endpoint": "platform_health", "icon": "shield"},
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
        "time_format": time_format(cfg),
        "help_doc_slug": DOC_HELP_BY_ENDPOINT.get(request.endpoint),
        "global_links": build_link_states(cfg) if cfg.get("wan_links") else [],
        # Stamped on the page, so it can tell when the lab has moved on.
        "ui_version": ui_state_version(),
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
        "slug": "appliances",
        "title": "Appliances & Platform Health",
        "category": "Configure",
        "summary": "Persistent device inventory, FortiGate license/health monitoring and verified simulator routing.",
        "template": "docs/articles/appliances.html",
        "keywords": "inventory vm proxmox fortigate fortiflex license health monitor target agent routing override",
    },
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
        "keywords": "git update release version main release please permissions service account",
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
        "title": "Capabilities & roadmap",
        "category": "Reference",
        "summary": "Available now, including saved SQLite history, and planned improvements to observation, orchestration and evidence.",
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
        traffic_generator=traffic_generator_snapshot(cached=True),
        traffic_path=traffic_path_readiness(cached=True) if traffic_generator_config(cfg).get("host") else None,
        appliance=appliance_under_test(cached=True),
        site_plan=active_site_plan(cfg),
        site_state=site_plan_snapshot(),
        session_tests=session_site_tests(),
        session_wan=(session_snapshot().get("wan") or {}),
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
    traffic_generator = traffic_generator_snapshot(include_catalog=True, cached=True)
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
        traffic_path=traffic_path_readiness(cached=True) if traffic_generator.get("configured") else None,
        site_plan=active_site_plan(cfg, traffic_generator.get("catalog")),
        site_state=site_plan_snapshot(),
        last_test=LAST_TEST_SUMMARY,
        test_records=test_records.history(RUNTIME_DIR),
        events=list(reversed([
            event for event in EVENT_LOG
            if event.get("kind") in (
                "scenario", "scenario-config", "fault", "mtu",
                "capture", "security-test", "traffic-generator", "site", "site-test", "test-summary"
            )
        ][-40:])),
    )


@app.get("/tests/scenario/<scenario_id>")
def scenario_configuration(scenario_id):
    cfg = load_config()
    scenario = next((item for item in get_scenarios(cfg) if item["id"] == scenario_id), None)
    if scenario is None:
        abort(404)
    response = app.make_response(render_template(
        "scenario_configuration.html", scenario=scenario, links=build_link_states(cfg),
        scenario_state=scenario_snapshot(), site_state=site_plan_snapshot(), scenario_return_to="overview",
    ))
    response.headers["Cache-Control"] = "no-store"
    return response


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
        wan_links=load_config().get("wan_links", []),
    )


@app.route("/sessions/start", methods=["POST"])
def session_start():
    # Reject a conflicting start before configuration or appliance preparation.
    # Admission is checked again under the same lock immediately before creation.
    with RUNTIME_LOCK:
        if ACTIVE_SESSION["active"]:
            flash("A lab session is already active. Complete it before starting another.", "error")
            return redirect_after("sessions")
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
        wan = session_wan_roles(cfg, request.form.get("primary_link"), request.form.get("backup_link"))
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect_after("sessions")
    site = dict(selection, label=site_catalog.site_label(selection))
    # Captured once, here: the session keeps the appliance it started with, whatever is selected later.
    appliance = session_appliance_snapshot()
    now = time.time()
    deadline = now + SESSION_MAX_S
    with RUNTIME_LOCK:
        if SCENARIO_STATE.get('active') or SITE_PLAN_STATE.get('active'):
            flash('Stop the running test before starting a session.', 'error')
            return redirect_after('sessions')
        if ACTIVE_SESSION["active"]:
            flash("A lab session is already active.", "error")
            return redirect_after("sessions")

        session_id = f"session-{time.time_ns()}"
        ACTIVE_SESSION.update(
            {
                "active": True,
                "id": session_id,
                "name": name or "Lab session",
                "started_at": now,
                "site": site,
                "deadline": deadline,
                "appliance": appliance,
                "wan": wan,
                "tests": {},
            }
        )
        LAB_SESSIONS.append(
            {
                "id": session_id,
                "name": ACTIVE_SESSION["name"],
                "started_at": now,
                "deadline": deadline,
                "ended_at": None,
                "status": "active",
                "site": site,
                "appliance": appliance,
                "wan": wan,
                "tests": {},
            }
        )
        del LAB_SESSIONS[:-100]
        save_session_history()

    if cfg.get("site_profile") != selection:
        # The session's site becomes the active site so its targets judge what is recorded.
        cfg["site_profile"] = selection
        save_config(cfg)
        log_event("site", f"Active site: {site['label']}", selection=selection, sla_applied=False)
    log_event("session", f'Lab session started: {ACTIVE_SESSION["name"]} · {site["label"]}', site=site,
              appliance=appliance, wan=wan, ends_by=deadline)
    flash(f'Lab session "{ACTIVE_SESSION["name"]}" started.', "success")
    return redirect_after("sessions")


@app.route("/sessions/stop", methods=["POST"])
def session_stop():
    ended = end_session("completed")
    if not ended:
        flash("No lab session is active.", "info")
        return redirect_after("sessions")
    report = ended["report"]
    flash(
        f'Lab session "{ended["name"]}" completed'
        + (f' · result {report["result"].upper()}.' if report else "."),
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


@app.get('/tests/records/<test_id>/download')
def test_record_download(test_id):
    try:
        # Finished archives are immutable. An unfinished/crashed test can still
        # export its durable configuration and journal, explicitly without a verdict.
        folder = test_records.directory(RUNTIME_DIR, test_id)
        path = folder / 'record.zip'
        if not path.is_file() or not (folder / 'summary.json').is_file():
            path = test_records.archive(RUNTIME_DIR, test_id)
        return send_file(path, as_attachment=True, download_name=f'netem-test-{test_id}.zip')
    except (ValueError, FileNotFoundError):
        abort(404)


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
        traffic_generator=traffic_generator_snapshot(include_catalog=False, cached=True),
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
    if scenario_snapshot().get("active"):
        flash("The test controls this workload. Use Stop & restore before starting manual traffic.", "error")
        return redirect_after("tests")
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
        payload["live_window_seconds"] = requested_live_window(request.form.get("live_window_seconds"))
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
    if scenario_snapshot().get("active"):
        flash("The test controls this workload. Use the test controls to pause or stop it.", "error")
        return redirect_after("tests")
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
    if scenario_snapshot().get("active"):
        flash("The test controls this workload. Use the test controls to pause or stop it.", "error")
        return redirect_after("tests")
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
    return jsonify(current_diagnosis(cached=True))


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
    return jsonify(traffic_path_readiness(cached=True))


@app.route("/api/v1/traffic-generator")
def api_traffic_generator():
    snapshot = traffic_generator_snapshot(include_catalog=False, cached=True)
    code = 202 if snapshot.get("connection_state") == "checking" else 200 if snapshot.get("connected") or not snapshot.get("configured") else 503
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
        sla_profile=get_sla_profile(cfg),
        update_status=git_update_status(fetch=False),
        showroom_url=showroom_url(),
        clock_status=platform_clock_status(),
        brand_packs=BRAND_STORE.describe(),
        active_brand=BRAND_STORE.active_id(),
        appliance=appliance_under_test(cached=True),
        appliance_setting=cfg.get("appliance") if isinstance(cfg.get("appliance"), dict) else {},
        showcase_video=cfg.get("showcase_video"),
        vendors=SDWAN_VENDORS,
    )


@app.route("/settings/appliance", methods=["POST"])
def appliance_save():
    vendor = request.form.get("vendor") or None
    if vendor is not None and vendor not in SDWAN_VENDORS:
        flash("Choose a listed SD-WAN vendor.", "error")
        return redirect(url_for("settings", section="appearance", _anchor="appliance"))
    cfg = load_config()
    cfg["appliance"] = {"vendor": vendor, "model": (request.form.get("model") or "").strip()[:60] or None}
    save_config(cfg)
    APPLIANCE_CACHE.update(checked=None)
    flash(f"Appliance under test: {SDWAN_VENDORS[vendor]['name']}." if vendor
          else "The appliance under test now follows the Traffic Simulator's selected appliance.", "success")
    return redirect(url_for("settings", section="appearance", _anchor="appliance"))


@app.route("/settings/branding")
def branding_settings():
    packs = BRAND_STORE.describe()
    editing = request.args.get("pack")
    if editing not in {pack["id"] for pack in packs}:
        editing = None
    return render_template("branding.html", page="settings", packs=packs, active=BRAND_STORE.active_id(),
                           editing=editing, manifest=BRAND_STORE.manifest(editing) if editing else {},
                           palette=branding.DEFAULT_PALETTE, brands_dir=str(BRANDS_DIR), store_error=BRAND_STORE.error,
                           local_pack=branding.LOCAL_PACK, branding_sync=BRANDING_SYNC.state)


@app.route("/settings/branding/preview/<pack_id>/<path:filename>")
def branding_preview(pack_id, filename):
    """Logos of brands that are not active, for the brand menu."""
    directory = BRAND_STORE.packs().get(pack_id)
    if not directory:
        abort(404)
    try:
        _root, brand = branding.load_pack(directory)
    except (OSError, ValueError, TypeError):
        abort(404)
    if filename not in brand["assets"]:
        abort(404)
    return branding.asset_headers(send_from_directory(Path(directory) / "assets", filename))


@app.route("/settings/branding/activate", methods=["POST"])
def branding_activate():
    pack_id = request.form.get("pack") or None
    try:
        BRAND_STORE.set_active(pack_id)
        sync = BRANDING_SYNC.sync()
        if not sync["synced"]:
            flash(sync["message"], "warning")
        flash("Default look restored." if pack_id is None else "Brand switched. Open screens follow on their next refresh.", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("branding_settings"))


@app.route("/settings/branding/save", methods=["POST"])
def branding_save():
    uploads = {}
    for slot in branding.UPLOAD_SLOTS:
        upload = request.files.get(slot)
        if upload and upload.filename:
            uploads[slot] = (upload.filename, upload.read(branding.UPLOAD_LIMIT + 1))
    try:
        pack_id = BRAND_STORE.save(request.form.get("pack") or None, request.form.to_dict(), uploads)
        if request.form.get("activate"):
            BRAND_STORE.set_active(pack_id)
            sync = BRANDING_SYNC.sync()
            if not sync["synced"]:
                flash(sync["message"], "warning")
        flash("Brand saved." + (" It is now active." if request.form.get("activate") else ""), "success")
        return redirect(url_for("branding_settings", pack=pack_id))
    except (ValueError, OSError) as exc:
        flash(f"Brand not saved: {exc}", "error")
        return redirect(url_for("branding_settings", pack=request.form.get("pack") or None))


@app.post('/settings/branding/import')
def branding_import():
    upload = request.files.get('config')
    try:
        if not upload or not upload.filename.lower().endswith('.json'):
            raise ValueError('Choose a branding JSON configuration file.')
        data = upload.read(12 * 1024 * 1024 + 1)
        if len(data) > 12 * 1024 * 1024:
            raise ValueError('The branding config must be at most 12 MB.')
        pack_id = BRAND_STORE.import_config(json.loads(data.decode('utf-8-sig')))
        if request.form.get('activate'):
            BRAND_STORE.set_active(pack_id)
            sync = BRANDING_SYNC.sync()
            if not sync['synced']:
                flash(sync['message'], 'warning')
        flash('Brand configuration imported. It applies to NetEm and Showroom; simulator synchronization is automatic when active.', 'success')
        return redirect(url_for('branding_settings', pack=pack_id))
    except (ValueError, OSError, TypeError) as exc:
        flash('Brand not imported: ' + str(exc), 'error')
        return redirect(url_for('branding_settings'))


@app.get('/settings/branding/export/<pack_id>')
def branding_export(pack_id):
    if pack_id not in BRAND_STORE.packs():
        abort(404)
    response = Response(json.dumps(BRAND_STORE.export_config(pack_id), indent=2), mimetype='application/json')
    response.headers['Content-Disposition'] = 'attachment; filename="' + branding.slugify(pack_id) + '.branding.json"'
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.route("/settings/branding/delete", methods=["POST"])
def branding_delete():
    try:
        BRAND_STORE.delete(request.form.get("pack"))
        BRANDING_SYNC.sync()
        flash("Brand deleted.", "success")
    except (ValueError, OSError) as exc:
        flash(str(exc), "error")
    return redirect(url_for("branding_settings"))


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
    return redirect(url_for("settings", section="system"))


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


def simulator_catalog(max_age=60, cached=False):
    """The connected simulator's catalog, briefly cached; None when unavailable."""
    source = presentation_source()
    if cached:
        return PRESENTATION_READS.read('catalog', source, lambda: simulator_catalog(max_age=0),
                                       None, max_age=60, max_stale=120)
    if SIMULATOR_CATALOG_CACHE.get('source') == source and SIMULATOR_CATALOG_CACHE.get("checked") is not None and time.monotonic() - SIMULATOR_CATALOG_CACHE["checked"] <= max_age:
        return SIMULATOR_CATALOG_CACHE["catalog"]
    catalog = None
    if traffic_generator_config().get("host") and traffic_generator_api_key():
        try:
            catalog = traffic_generator_request("/api/v1/catalog", timeout=2.0)
        except RuntimeError:
            catalog = None
    SIMULATOR_CATALOG_CACHE.update(timestamp=time.time(), checked=time.monotonic(), catalog=catalog, source=source)
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
        state = copy.deepcopy(SITE_PLAN_STATE)
    # Once a plan has finished, its summary travels with its state (Tests page, API, showroom).
    state["summary"] = None if state.get("active") else SITE_PLAN_SUMMARY
    return state


# ---------- Site test plan summary ----------
#
# After a run sequence, one summary of the whole plan: how many tests passed, one row per
# test from its own test summary, and insights drawn from those rows.

LAST_PLAN_SUMMARY_PATH = RUNTIME_DIR / "last-plan-summary.json"
PLAN_INSIGHTS_MAX = 7


def load_last_plan_summary():
    try:
        summary = json.loads(LAST_PLAN_SUMMARY_PATH.read_text())
        return summary if isinstance(summary, dict) else None
    except (OSError, ValueError):
        return None


SITE_PLAN_SUMMARY = load_last_plan_summary()


def plan_test_row(test, summary):
    """One test of the plan: its result and what users got, from the test's own summary."""
    summary = summary or {}
    phases = [phase for phase in summary.get("phases") or [] if phase.get("experience_score") is not None]
    base = next((phase for phase in phases if phase["name"] in BASELINE_PHASES), phases[0] if phases else None)
    low = min(phases, key=lambda phase: phase["experience_score"]) if phases else None
    successes = [value for phase in phases for value in (phase.get("worst_success_pct"), phase.get("success_pct")) if value is not None]
    p95s = [phase["interactive_p95_ms"] for phase in phases if phase.get("interactive_p95_ms") is not None]
    moved = {item["traffic_class"] for item in summary.get("remediation") or []}
    stuck = []
    for phase in summary.get("phases") or []:
        for label, verdict in (phase.get("steering") or {}).items():
            if verdict in ("stuck", "stuck_impact") and label not in moved and all(item["traffic_class"] != label for item in stuck):
                stuck.append({"traffic_class": label, "phase": phase["name"]})
    checks = summary.get("assertions") or {}
    return {
        "id": test.get("id"), "name": test.get("name"), "role": test.get("role"), "result": test.get("status"),
        "link": summary.get("link"), "duration_s": summary.get("duration_s"), "measured": bool(phases),
        "checks": {"passed": checks.get("passed", 0), "total": checks.get("total", 0)},
        "user_impact": showcase_summary.observed_impact(summary),
        "missed": [item.get("label") for item in checks.get("items") or [] if not item.get("passed")],
        "experience": base["experience_score"] if base else None,
        "success": base.get("success_pct") if base else None,
        "experience_low": low["experience_score"] if low else None, "low_phase": low["name"] if low else None,
        "success_low": min(successes) if successes else None, "p95_max": max(p95s) if p95s else None,
        "moved": [{key: item.get(key) for key in ("traffic_class", "wan", "seconds", "within_target")}
                  for item in summary.get("remediation") or []],
        "stuck": stuck,
    }


def plan_insights(rows, targets):
    """What the run showed, in a few sentences, each with a tone: pass, warn or fail."""
    insights = []
    experience_min = targets.get("experience_min")
    steering_max = targets.get("steering_max_s")
    measured = [row for row in rows if row["measured"]]
    base = next((row for row in measured if row["id"] == "baseline"), measured[0] if measured else None)
    if base and base["experience"] is not None:
        healthy = experience_min is None or base["experience"] >= experience_min
        success = f" and {base['success']:.2f}% of requests succeeded" if base["success"] is not None else ""
        insights.append({"tone": "pass" if healthy else "warn",
                         "text": f"With both WANs healthy, experience was {base['experience']:.0f}{success}."})
    moves = {}
    for row in rows:
        for item in row["moved"]:
            moves.setdefault(item["traffic_class"], []).append(item)
    for label, items in moves.items():
        seconds = sorted(item["seconds"] for item in items)
        span = f"{seconds[0]} s" if seconds[0] == seconds[-1] else f"{seconds[0]}–{seconds[-1]} s"
        missed = any(item["within_target"] is False for item in items)
        target = "" if steering_max is None else (f", slower than the {steering_max} s target" if missed
                                                  else f", within the {steering_max} s target")
        insights.append({"tone": "fail" if missed else "pass",
                         "text": f"The appliance moved {label[:1].lower() + label[1:]} off an impaired WAN in {span}{target}."})
    for row in rows:
        for item in row["stuck"]:
            insights.append({"tone": "warn",
                             "text": f"{item['traffic_class']} stayed on an impaired WAN during {item['phase']} ({row['name']})."})
    lows = [row for row in measured if row["experience_low"] is not None]
    if lows:
        low = min(lows, key=lambda row: row["experience_low"])
        if experience_min is not None and low["experience_low"] >= experience_min:
            insights.append({"tone": "pass", "text": f"Experience stayed at or above the {experience_min} target in every test."})
        else:
            below = f", below the {experience_min} target" if experience_min is not None else ""
            insights.append({"tone": "warn",
                             "text": f"Lowest experience: {low['experience_low']:.0f} during {low['low_phase']} in {low['name']}{below}."})
    unnoticed = [row["name"] for row in measured if row["id"] != "baseline" and row["result"] == "passed" and
                 experience_min is not None and row["experience_low"] is not None and row["experience_low"] >= experience_min]
    if unnoticed:
        insights.append({"tone": "pass", "text": "Users did not notice: " + ", ".join(unnoticed) + "."})
    for row in rows:
        if row["missed"]:
            insights.append({"tone": "fail", "text": f"{row['name']} missed: " + "; ".join(row["missed"][:3]) + "."})
        elif not row["measured"] and row["result"] not in ("skipped", "pending"):
            insights.append({"tone": "warn", "text": f"No simulated user traffic was measured in {row['name']}."})
    # What needs attention comes first.
    order = {"fail": 0, "warn": 1, "pass": 2}
    return sorted(insights, key=lambda item: order[item["tone"]])[:PLAN_INSIGHTS_MAX]


def summarize_plan(label, targets, tests, summaries, result, started_at, ended_at):
    rows = [plan_test_row(test, summaries.get(test.get("id"))) for test in tests]
    passed = sum(1 for row in rows if row["result"] == "passed")
    ran = [row for row in rows if row["result"] not in ("skipped", "pending")]
    title = f"{passed} of {len(rows)} test{'s' if len(rows) != 1 else ''} passed"
    if result == "stopped":
        title += f" · stopped after {len(ran)}"
    return {
        "label": label, "result": result, "title": title, "started_at": started_at, "ended_at": ended_at,
        "duration_s": round(ended_at - started_at) if started_at else None,
        "checks": {"passed": sum(row["checks"]["passed"] for row in rows), "total": sum(row["checks"]["total"] for row in rows)},
        "targets": {key: targets.get(key) for key in ("experience_min", "success_min_pct", "interactive_p95_max_ms", "steering_max_s")},
        "tests": rows, "insights": plan_insights(rows, targets),
    }


def finish_plan_summary(plan, summaries, result):
    """Keep the summary of the site tests: the whole session's while one runs, otherwise this run's."""
    global SITE_PLAN_SUMMARY
    with RUNTIME_LOCK:
        state = copy.deepcopy(SITE_PLAN_STATE)
        session = copy.deepcopy(ACTIVE_SESSION) if ACTIVE_SESSION.get("active") else None
    if session:
        recorded = session.get("tests") or {}
        tests = [{"id": test["id"], "name": test["name"], "role": test["role"],
                  "status": (recorded.get(test["id"]) or {}).get("status", "pending")}
                 for test in plan.get("tests") or []]
        summaries = {test_id: record.get("summary") for test_id, record in recorded.items()}
        started = session.get("started_at")
    else:
        tests, started = state.get("tests") or [], state.get("started_at")
    summary = summarize_plan(plan.get("label"), plan.get("targets") or {}, tests, summaries, result,
                             started, state.get("finished_at") or time.time())
    SITE_PLAN_SUMMARY = summary
    try:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        LAST_PLAN_SUMMARY_PATH.write_text(json.dumps(summary))
    except OSError:
        pass
    log_event("site-test", f"Site test plan summary · {summary['title']}", summary=summary)
    return summary


def run_site_plan(plan: dict, tests: list, link_for_role: dict):
    """Run site tests one after another; each is an ordinary scenario on its WAN."""
    log_event("site-test", f"Site test plan started · {plan['label']}", tests=[test["id"] for test in tests])
    results = []
    summaries = {}
    for test in tests:
        if SITE_PLAN_STOP.is_set():
            break
        link_id = link_for_role[test["role"]]
        scenario = {"id": f"site_{test['id']}", "name": test["name"], "description": test.get("description"), "controller_managed": True,
                    "steps": validate_scenario_steps(test["steps"]), "live_window_seconds": plan.get("live_window_seconds", 15)}
        with RUNTIME_LOCK:
            if SITE_PLAN_STOP.is_set():
                break
            SITE_PLAN_STATE["current"] = test["id"]
            for item in SITE_PLAN_STATE["tests"]:
                if item["id"] == test["id"]:
                    item.update(status="running", link_id=link_id)
            SCENARIO_STOP.clear()
            SCENARIO_STATE.update({
                "active": True, "unsaved": False, "phase_jump": None, "phase_revision": 0, "navigating": False, "finishing": False, "scenario_id": scenario["id"], "scenario_name": f"{plan['label']}: {test['name']}",
                "link_id": link_id, "started_at": time.time(), "clock_start": time.monotonic(), "step": 0,
                "step_count": len(scenario["steps"]),
                "step_label": "Starting", "step_action": None, "condition": None, "result": None, "error": None,
            })
        set_session_test(test, "running")
        before = LAST_TEST_SUMMARY
        try:
            run_scenario(link_id, scenario, TEST_INTRO_S)
        except Exception as exc:
            # Keep plan state recoverable even if the scenario runner fails before cleanup.
            with RUNTIME_LOCK:
                SCENARIO_STATE.update(active=False, result="failed", error=str(exc)[:240])
        # The test's own summary, when it ran far enough to record one.
        if LAST_TEST_SUMMARY is not before and (LAST_TEST_SUMMARY or {}).get("scenario_id") == scenario["id"]:
            summaries[test["id"]] = LAST_TEST_SUMMARY
        finished = scenario_snapshot()
        result = finished.get("result") or "failed"
        with RUNTIME_LOCK:
            for item in SITE_PLAN_STATE["tests"]:
                if item["id"] == test["id"]:
                    item.update(status=result, error=finished.get("error"))
        log_event("site-test", f"{test['name']} — {result.upper()}", test_id=test["id"], link_id=link_id,
                  result=result, error=finished.get("error"), site=plan["label"])
        set_session_test(test, result, summaries.get(test["id"]))
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
    try:
        finish_plan_summary(plan, summaries, overall)
    except Exception as exc:
        log_event("site-test", "Site test plan summary could not be built", error=str(exc)[:240])


@app.route("/api/v1/site-plan")
def api_site_plan():
    try:
        plan = site_catalog.build_site_plan({key: request.args.get(key) for key in SITE_FIELDS}, simulator_catalog())
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(plan)


@app.route("/site/save", methods=["POST"])
def site_save():
    if session_snapshot().get("active"):
        flash("A lab session is running with its site fixed. Complete the session to change the site.", "error")
        return redirect_after("tests")
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
    session = session_snapshot()
    if not session.get("active") or not session.get("site"):
        flash("Start a lab session before running site tests. The session fixes the client, the appliance and the WAN roles.", "error")
        return redirect_after("tests")
    if not session.get("wan"):
        flash("This session has no primary and backup WAN, so its site tests cannot run. Start the session with two WANs.", "error")
        return redirect_after("tests")
    cfg = load_config()
    plan = active_site_plan({"site_profile": {key: session["site"][key] for key in SITE_FIELDS}}, simulator_catalog(max_age=0))
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
        live_window_seconds = requested_live_window(request.form.get("live_window_seconds"))
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect_after("tests")
    if length_s:
        plan["tests"] = [dict(test, steps=scale_scenario_steps(test["steps"], length_s)) for test in plan["tests"]]
    plan["live_window_seconds"] = live_window_seconds
    plan["start"]["live_window_seconds"] = live_window_seconds
    for test in plan["tests"]:
        for step in test["steps"]:
            if step.get("action") == "traffic_generator" and (step.get("value") or {}).get("operation") == "start":
                step["value"]["live_window_seconds"] = live_window_seconds
    test_id = request.form.get("test_id") or "all"
    tests = [test for test in plan["tests"] if test_id in ("all", test["id"])]
    links = {role: request.form.get(f"{role}_link") or (session.get("wan") or {}).get(role, {}).get("link_id", "")
             for role in ("primary", "backup")}
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


def scenario_start_wants_json():
    return (request.endpoint == "lab_scenario_start"
            and request.accept_mimetypes.best == "application/json")


def scenario_start_refused(message, status=400):
    if scenario_start_wants_json():
        return jsonify(error=message), status
    flash(message, "error")
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
        return scenario_start_refused("Unknown WAN link.")
    if not scenario:
        return scenario_start_refused("Unknown scenario.")
    try:
        length_s = requested_length_s(request.form.get("length_min"))
        live_window_seconds = requested_live_window(request.form.get("live_window_seconds"))
    except ValueError as exc:
        return scenario_start_refused(str(exc))
    if length_s:
        scenario = dict(scenario, steps=scale_scenario_steps(scenario["steps"], length_s))
    scenario = dict(scenario, auto_workload=True, controller_managed=True, live_window_seconds=live_window_seconds)

    if not session_snapshot().get("active") and request.form.get("no_session_ack") != "1":
        # Generic tests normally run inside a lab session. Without one, the operator starts a session or confirms the test.
        fields = [(key, value) for key, values in request.form.lists() for value in values
                  if key not in ("no_session_ack", "integration_csrf")]
        return render_template("no_session_warning.html", page="tests", scenario=scenario, fields=fields,
                               link_name=get_link(cfg, link_id).get("name") or link_id), 409
    with RUNTIME_LOCK:
        if SCENARIO_STATE["active"] or SITE_PLAN_STATE["active"]:
            return scenario_start_refused("A scenario is already running.", 409)
        unsaved = not ACTIVE_SESSION.get('active')
        SCENARIO_STOP.clear()
        if unsaved:
            begin_unsaved_telemetry()
        SCENARIO_STATE.update(
            {
                "active": True,
                "unsaved": unsaved,
                "phase_jump": None,
                "phase_revision": 0,
                "navigating": False,
                "finishing": False,
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
        args=(link_id, scenario, TEST_INTRO_S),
        daemon=True,
    ).start()
    message = f'Started scenario "{scenario["name"]}" on {link_id}.'
    if scenario_start_wants_json():
        return jsonify(accepted=True, scenario_id=scenario_id, message=message), 202
    flash(message, "success")
    return redirect_after("scenarios")


@app.post('/lab/scenario/phase')
def lab_scenario_phase():
    direction = request.form.get('direction')
    if direction not in ('next', 'previous'):
        abort(400, 'Choose next or previous phase.')
    try:
        expected_phase = int(request.form.get('phase_index', ''))
        expected_revision = int(request.form.get('phase_revision', ''))
        expected_start = float(request.form.get('started_at', ''))
    except ValueError:
        abort(400, 'Reload the running test before navigating.')
    with RUNTIME_LOCK:
        state = SCENARIO_STATE
        if (not state.get('active') or state.get('intro') or state.get('preparing_workload')
                or SCENARIO_STOP.is_set() or state.get('finishing') or state.get('navigating') or state.get('phase_jump') is not None
                or state.get('phase_index') is None):
            abort(409, 'The test is not ready for phase navigation.')
        if (request.form.get('scenario_id') != state.get('scenario_id')
                or expected_phase != state.get('phase_index')
                or expected_revision != state.get('phase_revision', 0)
                or expected_start != state.get('started_at')):
            abort(409, 'The running phase changed. Reload before navigating.')
        target = state['phase_index'] + (1 if direction == 'next' else -1)
        if not 0 <= target < len(state.get('phases') or []):
            abort(409, 'There is no phase in that direction.')
        state['phase_jump'] = target
    flash('Moving to the selected phase. It will restart and resume with a fresh measurement window.', 'info')
    return redirect_after('tests')


@app.route("/lab/scenario/pause", methods=["POST"])
def lab_scenario_pause():
    """Hold the running test in its current phase, impairment applied, until resumed."""
    with RUNTIME_LOCK:
        announced = SCENARIO_STATE["active"] and (SCENARIO_STATE.get("intro") or SCENARIO_STATE.get("preparing_workload"))
        active = SCENARIO_STATE["active"] and not SCENARIO_STATE["paused"] and not announced
        if active:
            SCENARIO_PAUSE.set()
            SCENARIO_STATE.update(paused=True, paused_at=time.time(), paused_clock=time.monotonic())
            name, phase = SCENARIO_STATE["scenario_name"], SCENARIO_STATE.get("phase")
            with TEST_RECORDER_LOCK:
                if TEST_RECORDER.get("active"):
                    TEST_RECORDER.setdefault("pauses", []).append({"start": SCENARIO_STATE["paused_at"], "end": None})
    if active:
        try:
            publish_test_live_context()
        except RuntimeError as exc:
            log_event('traffic-generator', 'Live window context unavailable', error=str(exc)[:240])
            flash('Live measurements are unavailable until the simulator acknowledges this change.', 'error')
        log_event("scenario", f"{name}: paused" + (f" in phase {phase}" if phase else ""), phase=phase)
        flash("Test paused. It stays in its current phase until you resume it.", "info")
    elif announced:
        flash("The test starts in a few seconds; pause it once it is running, or stop it now.", "info")
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
            with TEST_RECORDER_LOCK:
                if TEST_RECORDER.get("pauses") and TEST_RECORDER["pauses"][-1]["end"] is None:
                    TEST_RECORDER["pauses"][-1]["end"] = time.time()
            name = SCENARIO_STATE["scenario_name"]
    if active:
        try:
            publish_test_live_context()
        except RuntimeError as exc:
            log_event('traffic-generator', 'Live window context unavailable', error=str(exc)[:240])
            flash('Live measurements are unavailable until the simulator acknowledges this change.', 'error')
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


# ---------- Page freshness ----------
#
# Open pages follow the lab: they ask for this version every few seconds and refresh in
# place when it changes. It covers what a page's own live widgets do not: sessions, tests
# starting, pausing and ending, site plans moving on, impairments, captures, saved settings
# and the simulator's run. Phase progress and measurements stay with the live widgets.
# It reads only in-memory state, the config file's timestamp and the cached diagnosis.

def ui_state_version():
    with RUNTIME_LOCK:
        parts = [
            [SCENARIO_STATE.get(key) for key in ("active", "scenario_id", "intro", "paused")],
            [SITE_PLAN_STATE.get(key) for key in ("active", "current", "result", "finished_at")],
            [ACTIVE_SESSION.get("active"), ACTIVE_SESSION.get("id")],
            sorted(ACTIVE_FAULTS.items()),
            CAPTURE_STATE.get("active"),
        ]
    try:
        parts.append(CONFIG_PATH.stat().st_mtime_ns)
    except OSError:
        parts.append(None)
    parts.append(PRESENTATION_READS.version(("catalog", "path", "appliance")))
    simulator = ((DIAGNOSIS_CACHE.get("payload") or {}).get("traffic_generator") or {})
    status = simulator.get("status") or {}
    parts.append([simulator.get("connected"), status.get("status"), (status.get("run") or {}).get("run_id")])
    if "APPLIANCE_PLATFORM" in globals():
        store = APPLIANCE_PLATFORM.store
        selection = store.state("selection")
        parts.append([selection.get("id"), selection.get("path_id"), selection.get("mode"), store.state("traffic_paths"),
                      [(row["id"], row["revision"]) for row in store.rows()],
                      [(alert["key"], alert["severity"], alert["acknowledged_at"]) for alert in store.alerts()]])
    return hashlib.sha256(json.dumps(parts, default=str).encode()).hexdigest()[:16]


@app.route("/api/v1/ui-version")
def api_ui_version():
    return jsonify({"version": ui_state_version()})


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


def simulator_update_state():
    """The Traffic Simulator's agent and controlled-target releases for the Updates page, or why they are unknown."""
    if not (traffic_generator_config(load_config()).get("host") and traffic_generator_api_key()):
        return {}, "The Traffic Simulator is not configured. Connect it under Integrations first."
    try:
        return traffic_generator_request("/api/v1/controller/settings", timeout=6.0), None
    except (RuntimeError, ValueError, KeyError) as exc:
        return {}, str(exc)


COMPONENT_CHECKS = ComponentChecks(sys.modules[__name__])


def component_update_context():
    simulator, simulator_error = simulator_update_state()
    return dict(update_status=git_update_status(fetch=False), simulator=simulator,
                simulator_error=simulator_error, component_checks=COMPONENT_CHECKS.snapshot(),
                restarting=False, process_instance=PROCESS_INSTANCE)


@app.get('/updates/components')
def update_components():
    context = component_update_context()
    response = jsonify(html=render_template('_update_cards.html', **context), checks=context['component_checks'])
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.route("/updates", methods=["GET", "POST"])
def updates():
    restarting = False
    wants_json = request.accept_mimetypes.best == "application/json"

    if request.method == "POST":
        action = request.form.get("action") or "check"
        if action == 'check':
            supplied, expected = request.form.get('integration_csrf', ''), session.get('integration_csrf', '')
            if not supplied or not expected or not hmac.compare_digest(supplied.encode(), expected.encode()):
                abort(400, 'Invalid form token. Reload the page and retry.')
            COMPONENT_CHECKS.start()
            return redirect(url_for('updates'))
        if action == 'update' and COMPONENT_CHECKS.snapshot()['running']:
            if wants_json:
                return jsonify(update_state='rejected', message='Release checks are still running. Wait for them to finish before installing.'), 409
            flash('Release checks are still running. Wait for them to finish before installing.', 'error')
            return redirect(url_for('updates'))
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
                    installed_version = get_app_version()
                    # Acknowledge before slow simulator reads, git status or changed templates.
                    # The old browser client understands the minimal HTML marker as well.
                    if wants_json:
                        response = jsonify(update_state='restarting', instance=PROCESS_INSTANCE,
                                           version=installed_version)
                        response.status_code = 202
                    else:
                        # Do not load templates that the merge may just have replaced.
                        response = Response(
                            '<!doctype html><html lang="en"><head><title>NetEm update installed</title></head><body>'
                            f'<div id="update-restarting" data-instance="{escape(PROCESS_INSTANCE)}" '
                            f'data-version="{escape(installed_version)}" hidden></div>'
                            '<p>Update installed. NetEm is restarting. '
                            '<a href="/updates">Return to updates</a> after it reconnects.</p></body></html>',
                            mimetype='text/html')
                    response.headers['Cache-Control'] = 'no-store'
                    threading.Thread(
                        target=restart_after_update,
                        daemon=True,
                    ).start()
                    return response
                else:
                    flash(err or out or "Git fast-forward update failed.", "error")
    else:
        status = git_update_status(fetch=False)

    if request.method == 'POST' and action == 'update' and wants_json:
        messages = get_flashed_messages()
        return jsonify(update_state='rejected', message=' '.join(messages) or 'Update was not accepted.'), 409
    simulator, simulator_error = simulator_update_state()
    return render_template(
        "updates.html",
        page="settings",
        update_status=status,
        restarting=restarting,
        process_instance=PROCESS_INSTANCE,
        simulator=simulator,
        simulator_error=simulator_error,
        component_checks=COMPONENT_CHECKS.snapshot(),
    )


@app.route("/updates/status")
def updates_status():
    """Which NetEm process answers and its version, polled while an update restarts the service."""
    response = jsonify({"version": PROCESS_VERSION, "instance": PROCESS_INSTANCE})
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
    """Present the active session or an explicitly unsaved trial; otherwise publish the waiting screen."""
    expire_session_if_due()
    if not session_snapshot().get("active") and not unsaved_test_active():
        return showroom_idle_snapshot()
    return showroom_session_snapshot()


def showroom_idle_snapshot():
    """The showcase between sessions: the waiting screen, with no measurements, names or configuration."""
    outcome = showroom_outcome({}, None)
    return {
        "timestamp": time.time(), "links": [], "story": showroom_story({}, [], outcome), "time_format": time_format(),
        "showcase_video": showcase_video_public(),
        "appliance": {}, "scenario": {"active": False}, "last_test": None,
        "session": {"active": False, "name": None, "site": None, "wan": None},
        "site": None, "plan": {"active": False, "label": None, "result": None, "tests": []},
        "plan_summary": None,
        **outcome,
    }


def summary_belongs_to_session(summary, session):
    """Whether a finished test or plan summary happened within the running session."""
    ended = (summary or {}).get("ended_at")
    started = session.get("started_at")
    return bool(summary and isinstance(ended, (int, float)) and isinstance(started, (int, float)) and ended >= started)


SHOWROOM_SESSION_PROGRESS = {}
SHOWROOM_SESSION_PROGRESS_LOCK = threading.Lock()


def showroom_session_progress(session):
    """Read durable summary history once per finished test, never publish raw events."""
    if not session.get('active') or not session.get('id'):
        return None
    session_id = session['id']
    recent = [event for event in list(EVENT_LOG) if event.get('kind') == 'test-summary'
              and (event.get('details') or {}).get('session_id') == session_id]
    last = recent[-1] if recent else {}
    key = (session_id, session.get('started_at'), last.get('timestamp'), (LAST_TEST_SUMMARY or {}).get('ended_at'))
    with SHOWROOM_SESSION_PROGRESS_LOCK:
        if SHOWROOM_SESSION_PROGRESS.get('key') == key:
            return copy.deepcopy(SHOWROOM_SESSION_PROGRESS['value'])
        events = read_session_events(session_id) + recent
        summaries = {}
        now = time.time()
        for event in events:
            if event.get('kind') != 'test-summary' or (event.get('details') or {}).get('session_id') != session_id:
                continue
            summary = (event.get('details') or {}).get('summary')
            if not isinstance(summary, dict) or not summary_belongs_to_session(summary, session) or summary['ended_at'] > now:
                continue
            identity = summary.get('test_id') or (summary.get('scenario_id'), summary.get('started_at'), summary['ended_at'])
            summaries[identity] = summary
        value = showcase_summary.session_facts(list(summaries.values()), showroom_text)
        SHOWROOM_SESSION_PROGRESS.update(key=key, value=value)
        return copy.deepcopy(value)


def showroom_session_snapshot():
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
        diagnosis = current_diagnosis(cached=True)
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
    outcome = showroom_outcome(diagnosis, site_plan)
    brief = scenario.get('briefing') or showcase_summary.briefing(scenario,
        (site_plan or {}).get('targets') or SHOWROOM_LAB_TARGETS, scenario.get('live_window_seconds') or 15)
    execution = ((diagnosis.get('traffic_generator') or {}).get('status') or {})
    scope = (lab_session.get('id'), lab_session.get('started_at'),
             (execution.get('run') or {}).get('run_id'),
             (execution.get('dem') or {}).get('live_phase_id'))
    with STORY_LOCK:
        if SHOWROOM_STATE.get('scope') != scope:
            SHOWROOM_STATE.clear()
            SHOWROOM_STATE.update(scope=scope, values={}, impacts={})
    story = showroom_story(diagnosis, links, outcome)
    showroom_evidence_story(story, outcome['experience'], execution.get('dem') or {})
    return {
        "timestamp": now, "links": links, "story": story, "time_format": time_format(cfg),
        "showcase_video": showcase_video_public(cfg),
        "appliance": {key: value for key, value in (lab_session.get("appliance") or {}).items() if key in ("vendor", "vendor_name", "product", "model")},
        "scenario": dict({key: scenario.get(key) for key in (
            "active", "started_at", "scenario_name", "step", "step_count", "step_label", "phase_index", "phase",
            "planned_s", "elapsed_s", "paused", "unsaved", "phase_elapsed_s", "phase_remaining_s", "next_phase", "intro", "starts_in_s", "preparing_workload")},
            link=showroom_link_label(cfg, scenario.get("link_id")),
            # Shown while a test is announced: what each phase does and what is checked.
            phases=[{"name": phase.get("name"), "planned_s": phase.get("planned_s"), "what": showroom_text(phase.get("what")) or None}
                    for phase in scenario.get("phases") or []],
            description=showroom_text(scenario.get("description")) or None,
            measurements=[showroom_text(item) for item in brief['measurements']],
            watch=[showroom_text(item, 320) for item in brief['watch']],
            checks=[showroom_text(check, 120) for check in (scenario.get("checks") or [])[:6]]),
        # Once a test ends, its summary stays on screen until the next one starts.
        "last_test": showroom_summary(LAST_TEST_SUMMARY) if lab_session.get("active") and not scenario.get("active") and summary_belongs_to_session(LAST_TEST_SUMMARY, lab_session) else None,
        # After a run sequence, the whole plan's results and insights.
        "plan_summary": (showroom_plan_summary(SITE_PLAN_SUMMARY, now, lab_session)
                         if not scenario.get("active") and not plan.get("active") else None),
        "session": {"active": bool(lab_session.get("active")), "name": lab_session.get("name"),
                    "site": (session_site or {}).get("label"),
                    "wan": {role: item.get("label") for role, item in (lab_session.get("wan") or {}).items()} or None},
        "session_progress": showroom_session_progress(lab_session) if not scenario.get('active') else None,
        "site": showroom_site(site_plan),
        "plan": {"active": plan.get("active"), "label": plan.get("label"), "result": plan.get("result"),
                 "tests": [{"name": test.get("name"), "role": test.get("role"), "status": test.get("status")}
                           for test in plan.get("tests") or []]},
        **outcome,
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


# A finished plan's results stay on the showroom this long, unless a new session starts.
SHOWROOM_PLAN_SUMMARY_S = 1800


def showroom_plan_summary(summary, now, session):
    """The finished site test plan without internal identifiers or addresses, while it is current."""
    ended = (summary or {}).get("ended_at")
    if not isinstance(ended, (int, float)) or not 0 <= now - ended < SHOWROOM_PLAN_SUMMARY_S:
        return None
    if not session.get("active") or (session.get("started_at") or 0) > ended:
        return None
    return {
        "label": showroom_text(summary.get("label")), "result": summary.get("result"),
        "title": showroom_text(summary.get("title")), "ended_at": ended, "duration_s": summary.get("duration_s"),
        "checks": summary.get("checks"), "targets": summary.get("targets"),
        "tests": [{key: row.get(key) for key in ("name", "role", "result", "link", "measured", "checks", "experience",
                                                  "experience_low", "low_phase", "success_low", "p95_max", "user_impact")}
                  | {"moved": [{key: item.get(key) for key in ("traffic_class", "wan", "seconds", "within_target")}
                               for item in row.get("moved") or []]}
                  for row in summary.get("tests") or []],
        "insights": [{"tone": item.get("tone"), "text": showroom_text(item.get("text"))} for item in summary.get("insights") or []],
    }


def showroom_summary(summary):
    """The finished test's summary without internal identifiers or addresses."""
    if not summary:
        return None
    return {
        "name": summary.get("name"), "link": summary.get("link"), "site": summary.get("site"),
        "result": summary.get("result"), "execution_result": summary.get("execution_result"),
        "experience_result": summary.get("experience_result"), "ended_at": summary.get("ended_at"), "duration_s": summary.get("duration_s"),
        "user_impact": showroom_text(summary.get("user_impact")),
        "conclusion": [showroom_text(line) for line in summary.get("narrative") or summary.get("conclusion") or []],
        "phases": [{key: phase.get(key) for key in ("name", "reached", "duration_s", "experience_score",
                                                    "average_experience_score", "score_adjusted", "success_pct",
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


def confirmed_critical_failure(dem):
    # Missing/overdue observations cannot erase an independently measured failure.
    # Corrupt or mixed windows cannot establish one either.
    invalid = {"row_limit", "clock_discontinuity", "configuration_changed_in_window", "dropped_transactions", "persistence_unhealthy"}
    if dem.get("truncated") or invalid.intersection(dem.get("evidence_issues") or []):
        return False
    return dem.get("business_acceptance") == "fail" or any(
        item.get("severity", "violation") in ("violation", "outage") for item in dem.get("critical_alerts") or [])


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
    verdicts = {
        "experience": showroom_verdict(score, targets.get("experience_min")),
        "success": showroom_verdict(success, targets.get("success_min_pct")),
        "interactive": showroom_verdict(interactive, targets.get("interactive_p95_max_ms"), higher_is_better=False),
    }
    if "fail" in verdicts.values():
        verdicts["experience"] = "fail"
    elif "unknown" in verdicts.values() or dem.get("scoring_complete") is not True:
        verdicts["experience"] = "unknown"
    if dem.get("truncated") or dem.get("evidence_issues") or dem.get("scoring_complete") is not True:
        verdicts = {key: "unknown" for key in verdicts}
    confirmed_failure = confirmed_critical_failure(dem)
    utility_failure = (dem.get("scoring_complete") is True and not dem.get("truncated") and not dem.get("evidence_issues")
                       and showroom_verdict(score, targets.get("experience_min")) == "fail")
    if confirmed_failure or utility_failure:
        verdicts["experience"] = "fail"
    elif dem.get("business_acceptance") == "unknown":
        verdicts["experience"] = "unknown"
    site_misses = []
    if verdicts["success"] == "fail":
        site_misses.append(f"Request success: {success:g}% (target ≥ {targets['success_min_pct']:g}%)")
    if verdicts["interactive"] == "fail":
        site_misses.append(f"Application completion P95: {interactive:g} ms (target ≤ {targets['interactive_p95_max_ms']:g} ms)")
    grade = display_score(dem, score, targets.get("experience_min"), showroom_app_label, site_misses)
    # Application compliance cannot override a failed site target or invalid evidence.
    steering = diagnosis.get("steering") or {}
    return {
        "traffic": {
            "configured": bool(generator.get("configured")), "connected": bool(generator.get("connected")),
            "status": (status or {}).get("status"), "users": (status or {}).get("users"),
            "label": showroom_text(run.get("label"), 120) or None, "activity": run.get("activity"),
            "media_mode": run.get("media_mode"), "requests": dem.get("requests"), "window_seconds": dem.get("window_seconds"),
            "monitoring": application_monitoring.presentation(run, dem, time.time()),
            "applications": [{"name": showroom_app_label(name), "class": item.get("class") or DEFAULT_APP_CLASSES.get(name),
                              "share_pct": round(item["requests"] * 100.0 / total, 1),
                              "success_pct": item.get("availability_pct")}
                             for name, item in sorted(apps.items(), key=lambda entry: -entry[1]["requests"])[:6]],
        },
        "experience": {
            "available": has_data, "targets_source": "site" if site_plan else "lab", "targets": targets,
            "experience_score": grade["score"], "average_experience_score": grade["average_score"],
            "score_adjusted": grade["score_adjusted"], "target_failures": [showroom_text(line) for line in grade["target_failures"]],
            "success_pct": success, "interactive_p95_ms": interactive,
            "timing": {key: (dem.get('timing') or {}).get(key) for key in
                ('packet_rtd_p50_upper_ms', 'packet_rtd_p95_upper_ms', 'packet_rtd_samples', 'packet_rtd_operations',
                 'request_to_headers_p95_ms', 'response_body_p95_ms', 'one_way_latency_ms')},
            "target_rtd": {key: ((dem.get('timing') or {}).get('target_rtd') or {}).get(key) for key in
                ('p50_ms', 'p95_ms', 'last_ms', 'samples', 'attempts', 'no_reply', 'latest_at', 'complete')},
            "verdicts": verdicts,
            **{key: dem.get(key) for key in ("score_model", "scoring_complete", "evidence_issues", "application_contributions",
                                            "critical_alerts", "limiting_application", "missing_measurements", "coverage_pct", "score_range",
                                            "score_status", "sample_confidence", "evidence_coverage_pct", "at_risk", "at_risk_reason", "legacy_score", "configured_window_seconds", "window_scope", "collecting_evidence", "live_paused")},
            "application_acceptance": dem.get("business_acceptance"),
            "business_acceptance": verdicts["experience"],
            # Each traffic group's own sub-score and sentence, under the overall experience tiles.
            "groups": dem.get("experience_groups") or [],
            "user_impact": " ".join(showcase_summary.application_impact(dem)),
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


# ---------- Showroom story ----------
#
# A live audience reads the showroom from across the room. It gets one stable sentence about
# the site, the user impacts that persist with their cause in plain words, and where each kind
# of traffic goes. A change shows only once it has lasted a few seconds; an impact that ends
# stays a while, marked resolved, so the audience can follow what happened.

STORY_SETTLE_S = 8
STORY_IMPACT_SETTLE_S = 2
STORY_HOLD_S = 30
STORY_MAX_IMPACTS = 3
SHOWROOM_STATE = {"values": {}, "impacts": {}}
STORY_LOCK = threading.Lock()
CLASS_NAMES = {"realtime": "Voice and video", "interactive": "Web and business apps", "bulk": "File transfers"}
CLASS_SHORT = {"realtime": "voice and video", "interactive": "web and apps", "bulk": "file transfers"}
IMPACT_VERBS = {"target_violation": "not meeting its performance target", "media_loss": "breaking up", "media_no_reply": "calls dropping", "timeouts": "timing out",
                "connection_errors": "cannot connect", "slow_wait": "responding slowly",
                "bandwidth_bound": "transferring slowly", "http_errors": "getting errors"}
HEALTH_WORDS = {"healthy": "Healthy", "congested": "Busy", "queue_pressure": "Queue pressure", "degraded": "Degraded", "failed": "Down"}
FAULT_WORDS = {"blackhole": "Outage injected", "downstream_blackhole": "Download outage injected",
               "upstream_blackhole": "Upload outage injected"}


def settled(key, value, now, settle=STORY_SETTLE_S):
    """The value to show for key: a new value replaces the shown one once it has lasted `settle` seconds."""
    entry = SHOWROOM_STATE["values"].setdefault(key, {"shown": value, "candidate": value, "since": now})
    if value != entry["candidate"]:
        entry.update(candidate=value, since=now)
    if value != entry["shown"] and now - entry["since"] >= settle:
        entry["shown"] = value
    return entry["shown"]


def showroom_evidence_story(story, experience, dem, now=None):
    """Settle overdue narration only; leave scores and evidence verdicts untouched."""
    now = time.monotonic() if now is None else now
    issues = set(dem.get('evidence_issues') or [])
    pending = 'live_phase_pending' in issues
    failed = experience.get('business_acceptance') == 'fail'
    overdue = bool(dem.get('at_risk')) and not failed and not pending
    # Other invalid evidence, pauses and disconnects must not inherit a prior verdict.
    transient = (overdue and experience.get('available') and not dem.get('live_paused')
                 and not dem.get('truncated')
                 and not (issues - {'outstanding_overdue_operations'}))
    with STORY_LOCK:
        state = SHOWROOM_STATE.setdefault('evidence_story', {})
        if pending and not failed:
            story['status'] = 'Collecting current-phase evidence.'
        elif overdue:
            since = state.setdefault('overdue_since', now)
            if transient and now - since < STORY_SETTLE_S:
                previous = state.get('confirmed')
                story['status'] = ('Last confirmed observation: ' + previous['status'] if previous
                                   else 'Application monitoring is collecting measurements.')
                experience['user_impact'] = ('Last confirmed observation: ' + previous['impact'] if previous
                                             else 'Application monitoring is collecting measurements.')
            else:
                story['status'] = 'Application responses remain overdue; experience is not yet confirmed.'
        if not overdue:
            state.pop('overdue_since', None)
        confirmed = (experience.get('available') and experience.get('scoring_complete') is True
                     and not issues and not dem.get('at_risk') and not dem.get('live_paused')
                     and not dem.get('truncated')
                     and experience.get('business_acceptance') in ('pass', 'fail'))
        if confirmed:
            impact = experience.get('user_impact') or (
                'Application checks met the configured targets; no user impact was detected in this measurement window.'
                if not failed else 'Application checks did not meet the configured targets.')
            state['confirmed'] = {'status': story['status'], 'impact': impact}
        elif not transient:
            state.pop('confirmed', None)


def impairment_text(link):
    """What NetEm is doing to this WAN, in words an audience understands."""
    fault = link.get("fault") or "normal"
    if fault != "normal":
        return FAULT_WORDS.get(fault, fault.replace("_", " ").capitalize() + " injected")
    quality = link.get("quality")
    if quality is None or quality >= 100:
        return "No impairment"
    parts = [f"{link['delay_ms']:g} ms delay" if link.get("delay_ms") else None,
             f"{link['loss_pct']:g}% loss" if link.get("loss_pct") else None]
    parts = [part for part in parts if part]
    return f"Impaired to {quality:g}% quality" + (": " + ", ".join(parts) if parts else "")


def cause_text(kinds, link):
    name = link["name"]
    if "fault" in kinds:
        return f"{name} is down"
    if "injected_loss" in kinds:
        return f"{link.get('loss_pct') or 0:g}% packet loss on {name}"
    if "saturated" in kinds:
        return f"high utilization measured on {name}"
    if "queue_drops" in kinds:
        return f"queue drops measured on {name}"
    if "injected_delay" in kinds:
        return f"{link.get('delay_ms') or 0:g} ms delay on {name}"
    return None


def current_impacts(diagnosis, by_id, by_label):
    """User impacts in this diagnosis, keyed so the same impact keeps its identity while its numbers change."""
    impacts = {}
    for finding in diagnosis.get("findings") or []:
        verb = IMPACT_VERBS.get(finding.get("id"))
        # An information-level finding is within what users tolerate (for example a lost packet in a voice burst that
        # stays under its loss target), so it is not a user impact.
        if finding.get("source") != "experience" or not verb or finding.get("severity") == "info":
            continue
        # Transfers limited by the line speed are expected in a shaped lab unless users suffer badly.
        if finding["id"] == "bandwidth_bound" and finding.get("severity") != "bad":
            continue
        classes = sorted({DEFAULT_APP_CLASSES.get(app) for app in finding.get("applications") or []} - {None},
                         key=list(CLASS_NAMES).index)
        subject = ", ".join(CLASS_NAMES[item] for item in classes) if classes else "Users"
        target_violation = finding.get("id") == "target_violation"
        applications = finding.get("applications") or []
        if target_violation:
            subject = ", ".join(APPLICATION_LABELS.get(app) or showroom_app_label(app) for app in applications) or "Application"
        wans = [wan for wan in finding.get("wans") or [] if wan.get("affected")]
        top = wans[0] if wans else None
        link = by_id.get(top["link_id"]) if top else None
        if not top:
            cause = "the WAN that carried it is not identified"
        else:
            cause = (cause_text(top.get("cause_kinds") or [], link) if link else None) or \
                f"observed on {top['label']}; cause not established"
        if target_violation and top:
            if top.get("causes") and len(wans) == 1 and not finding.get("unattributed") and not finding.get("path_inferred"):
                cause = f"Likely contributor on {top['label']}: " + "; ".join(top["causes"])
            elif len(wans) > 1:
                cause = "Traffic used " + ", ".join(wan['label'] for wan in wans) + "; the failing path is not isolated"
            elif finding.get("path_inferred"):
                cause = f"Traffic likely used {top['label']}; cause not established"
            elif finding.get("unattributed") or finding.get("path_incomplete"):
                cause = f"Some traffic observed on {top['label']}; remaining path and cause not established"
        key = tuple(applications) if target_violation else subject
        impact = f"{subject}: {finding.get('target_detail') or 'Performance target not met; measurement details unavailable'}" if target_violation else f"{subject}: {verb}"
        impacts[("experience", finding["id"], key)] = {
            "impact": showroom_text(impact, 600) if target_violation else impact,
            "cause": showroom_text(cause, 600) if target_violation else cause, "wan": top["label"] if top else None,
            "severity": "bad" if finding.get("severity") == "bad" else "warn"}
    for item in (diagnosis.get("steering") or {}).get("classes") or []:
        if item.get("verdict") != "stuck_impact":
            continue
        impaired = next((share for share in item.get("shares") or []
                         if share.get("pct") and share.get("health") not in (None, "healthy")), None)
        name = CLASS_NAMES.get(item.get("class"), item.get("label"))
        impacts[("steering", item.get("class"))] = {
            "impact": f"{name}: kept on an impaired WAN",
            "cause": f"the appliance did not move it off {impaired['label']}" if impaired else "the appliance did not move it",
            "wan": (impaired or {}).get("label"), "severity": "bad"}
    return impacts


def showroom_story(diagnosis, links, outcome, now=None):
    """Stable presentation state: health per WAN, impacts with causes, where traffic goes, one status line."""
    now = time.monotonic() if now is None else now
    by_id = {link["id"]: link for link in links}
    by_label = {link["name"]: link for link in links}
    classes = (diagnosis.get("steering") or {}).get("classes") or []
    with STORY_LOCK:
        for link in links:
            raw = "failed" if link.get("fault", "normal") != "normal" else (
                link.get("health") or ("healthy" if link.get("sla_pass") else "degraded"))
            # An outage shows at once; other health changes once they have lasted a few seconds.
            link["display_health"] = settled(("health", link["id"]), raw, now, settle=0 if raw == "failed" else STORY_SETTLE_S)
            link["impairment"] = impairment_text(link)
        routes, carried = [], {link["name"]: set() for link in links}
        for item in classes:
            shares = [share for share in item.get("shares") or [] if share.get("pct")]
            top = max(shares, key=lambda share: share["pct"]) if shares else None
            route = "idle" if item.get("verdict") == "idle" else (top["label"] if top else "unknown")
            shown = settled(("route", item.get("class")), route, now)
            for share in shares:
                if share["pct"] >= 25 and share["label"] in carried:
                    carried[share["label"]].add(item.get("class"))
            link = by_label.get(shown)
            routes.append({"label": CLASS_NAMES.get(item.get("class"), item.get("label")),
                           "wan": shown if link else None,
                           "state": shown if shown in ("idle", "unknown") else
                                    ("good" if link and link["display_health"] == "healthy" else "warn")})
        known = any(route["wan"] for route in routes)
        for link in links:
            names = tuple(CLASS_SHORT[item] for item in CLASS_NAMES if item in carried[link["name"]])
            link["carries"] = list(settled(("carries", link["id"]), names, now)) if known else None
        seen = current_impacts(diagnosis, by_id, by_label)
        store = SHOWROOM_STATE["impacts"]
        for key, data in seen.items():
            store.setdefault(key, {"first": now})
            store[key].update(last=now, data=data)
        experience = outcome.get("experience") or {}
        complete = experience.get("scoring_complete") is True and not experience.get("evidence_issues")
        impacts = []
        for key, entry in list(store.items()):
            active = key in seen
            settled_long_enough = entry["last"] - entry["first"] >= STORY_IMPACT_SETTLE_S
            if not active and (not settled_long_enough or now - entry["last"] > STORY_HOLD_S):
                store.pop(key)
                continue
            if active and now - entry["first"] < STORY_IMPACT_SETTLE_S:
                continue
            impacts.append(dict(entry["data"], state="active" if active else "resolved" if complete else "unconfirmed"))
        impacts.sort(key=lambda item: (item["state"] != "active", item["severity"] != "bad"))
        active = [item for item in impacts if item["state"] == "active"]
        if not experience.get("available"):
            users = "Waiting for simulated users."
        elif experience.get("business_acceptance") == "fail" or (experience.get("verdicts") or {}).get("experience") == "fail":
            users = "Users are below target."
        elif (not complete or experience.get("business_acceptance") != "pass"
              or experience.get("at_risk") or any(value != "pass" for value in (experience.get("verdicts") or {}).values())):
            users = "User experience not yet confirmed."
            latency = experience.get("interactive_p95_ms")
            limit = (experience.get("targets") or {}).get("interactive_p95_max_ms")
            if latency is not None and limit is not None and latency > limit:
                users += " Observed response time is above target."
            if active:
                users += " Some transactions show problems."
        elif active:
            users = "Some transactions show problems; overall targets are met."
        else:
            users = "Users are meeting targets."
        unwell = [f"{link['name']} is {HEALTH_WORDS.get(link['display_health'], link['display_health']).lower()}"
                  for link in links if link["display_health"] != "healthy"]
        wans = ("; ".join(unwell) + ".") if unwell else ("All WANs are healthy." if len(links) > 1 else "")
        moves = [f"The appliance moved {CLASS_SHORT.get(item.get('class'), item.get('label', '').lower())} off "
                 f"{reaction['label']} in {reaction['steered_after_seconds']} s."
                 for item in outcome.get("steering") or [] for reaction in item.get("reactions") or []
                 if reaction.get("steered_after_seconds") is not None]
        status = " ".join(part for part in (users, wans, moves[0] if moves else "") if part)
    return {"status": showroom_text(status), "routes": routes,
            "impacts": [dict(item, impact=showroom_text(item["impact"]), cause=showroom_text(item["cause"]))
                        for item in impacts[:STORY_MAX_IMPACTS]]}


ACCESS_STORE = access_control.init_app(app, access_control.AccessStore(RUNTIME_DIR / "access.sqlite3"))
APPLIANCE_PLATFORM = appliance_platform.init_app(app, sys.modules[__name__])
showroom_app = create_showroom_app(showroom_snapshot, BRAND_STORE, showcase_video_file)


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


from traffic_controls import install as install_traffic_controls
install_traffic_controls(app, sys.modules[__name__])

if __name__ == "__main__":
    run_servers()
