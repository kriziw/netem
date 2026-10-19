"""Persistent appliance inventory and read-only vendor observations.

Inventory and evidence survive telemetry pruning. Vendor credentials are held in
private files outside SQLite; raw API replies are never persisted or exported.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import hmac
import http.client
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import ssl
import tempfile
import threading
import time
import uuid
from urllib.parse import urlencode, urlsplit

INTERVAL = 900
STALE_AFTER = 1800
BENCHMARK = ipaddress.ip_network("198.18.0.0/15")


def validate(payload, vendors):
    name = str(payload.get("name") or "").strip()
    vendor = str(payload.get("vendor") or "other")
    kind = str(payload.get("kind") or "vm")
    if not name or len(name) > 80 or vendor not in {*vendors, "other"} or kind not in ("vm", "physical"):
        raise ValueError("Provide a name (up to 80 characters), vendor and device type.")
    endpoint = str(payload.get("endpoint") or "").strip().rstrip("/")
    if endpoint:
        parsed = urlsplit(endpoint)
        try:
            port = parsed.port or 443
        except ValueError as exc:
            raise ValueError("Invalid management API port.") from exc
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.path or parsed.query or parsed.fragment or not 1 <= port <= 65535
                or not re.fullmatch(r"[A-Za-z0-9.:-]+", parsed.hostname)):
            raise ValueError("Management API address must be an HTTPS origin, without credentials or a path.")
    pin = str(payload.get("fingerprint") or "").lower().replace(":", "").strip()
    if pin and not re.fullmatch(r"[a-f0-9]{64}", pin):
        raise ValueError("Certificate fingerprint must be SHA-256 (64 hex characters).")
    self_signed = payload.get("self_signed") in (True, "1", "on")
    if self_signed and not pin:
        raise ValueError("A self-signed certificate requires its verified SHA-256 fingerprint.")
    gateway = str(payload.get("gateway") or "").strip()
    interface = str(payload.get("interface") or "").strip()
    target = str(payload.get("target") or "198.18.0.1").strip()
    if gateway or interface:
        try:
            gw, dest = ipaddress.IPv4Address(gateway), ipaddress.IPv4Address(target)
        except ValueError as exc:
            raise ValueError("Routing needs a data interface, IPv4 gateway and controlled target.") from exc
        if (not re.fullmatch(r"[A-Za-z0-9_.:-]{1,15}", interface) or interface == "lo"
                or gw.is_multicast or gw.is_unspecified or gw.is_loopback or dest not in BENCHMARK):
            raise ValueError("Choose a data interface and unicast gateway; target must be in 198.18.0.0/15.")
        gateway, target = str(gw), str(dest)
    vdom = str(payload.get("vdom") or "root")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", vdom):
        raise ValueError("Invalid VDOM name.")
    required = sorted(set(filter(None, re.split(r"[ ,]+", str(payload.get("required_licenses") or "vm")))))
    if len(required) > 20 or any(not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", key) for key in required):
        raise ValueError("Required license names must be simple API entitlement keys, such as vm or ips.")
    return {"name": name, "vendor": vendor, "kind": kind, "endpoint": endpoint, "fingerprint": pin,
            "self_signed": self_signed, "gateway": gateway, "interface": interface, "target": target,
            "vdom": vdom, "required_licenses": required,
            "model": str(payload.get("model") or "")[:80], "firmware": str(payload.get("firmware") or "")[:40],
            "hypervisor": str(payload.get("hypervisor") or "")[:80], "vm_id": str(payload.get("vm_id") or "")[:40],
            "license_mode": str(payload.get("license_mode") or "fortiflex")[:40]}


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / "inventory.db"
        self.lock = threading.RLock()
        self.initialized = False

    @contextmanager
    def connect(self):
        self.root.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                if not self.initialized:
                    conn.executescript("""
                        CREATE TABLE IF NOT EXISTS appliances (
                            id TEXT PRIMARY KEY, revision INTEGER NOT NULL, definition TEXT NOT NULL,
                            observation TEXT, checked_at REAL, archived INTEGER NOT NULL DEFAULT 0);
                        CREATE TABLE IF NOT EXISTS observations (
                            id INTEGER PRIMARY KEY, appliance_id TEXT NOT NULL, checked_at REAL NOT NULL,
                            payload TEXT NOT NULL);
                        CREATE INDEX IF NOT EXISTS inventory_history ON observations(appliance_id, checked_at);
                        CREATE TABLE IF NOT EXISTS platform_state (key TEXT PRIMARY KEY, payload TEXT NOT NULL);
                        CREATE TABLE IF NOT EXISTS alerts (
                            key TEXT PRIMARY KEY, appliance_id TEXT NOT NULL, title TEXT NOT NULL,
                            severity TEXT NOT NULL, active INTEGER NOT NULL, first_seen REAL NOT NULL,
                            last_seen REAL NOT NULL, acknowledged_at REAL);
                    """)
                    self.initialized = True
                yield conn
        finally:
            conn.close()

    def rows(self):
        with self.lock, self.connect() as conn:
            return [self.decode(row) for row in conn.execute("SELECT * FROM appliances WHERE archived=0 ORDER BY id")]

    @staticmethod
    def decode(row):
        return dict(json.loads(row["definition"]), id=row["id"], revision=row["revision"],
                    observation=json.loads(row["observation"] or "null"), checked_at=row["checked_at"])

    def get(self, appliance_id):
        return next((row for row in self.rows() if row["id"] == appliance_id), None)

    def token(self, appliance_id):
        if not re.fullmatch(r"[a-f0-9]{32}", appliance_id):
            return ""
        try:
            return (self.root / "appliance-secrets" / appliance_id).read_text().strip()
        except FileNotFoundError:
            return ""

    def save(self, definition, appliance_id=None, token=None, clear_token=False):
        with self.lock:
            old = self.get(appliance_id) if appliance_id else None
            if appliance_id and not old:
                raise ValueError("Appliance no longer exists.")
            if not old and len(self.rows()) >= 100:
                raise ValueError("Inventory is limited to 100 appliances.")
            appliance_id = appliance_id or uuid.uuid4().hex
            # Changing the API identity must not reuse a credential on another origin.
            changed = old and (old["endpoint"], old["vendor"]) != (definition["endpoint"], definition["vendor"])
            if token is not None and (len(token) > 2048 or any(ord(c) < 33 or ord(c) > 126 for c in token)):
                raise ValueError("API token must contain printable characters without whitespace.")
            if changed or clear_token:
                path = self.root / "appliance-secrets" / appliance_id
                path.unlink(missing_ok=True)
            if token:
                folder = self.root / "appliance-secrets"
                folder.mkdir(parents=True, exist_ok=True, mode=0o700)
                fd, path = tempfile.mkstemp(dir=folder)
                try:
                    with os.fdopen(fd, "w") as stream:
                        stream.write(token)
                    os.chmod(path, 0o600)
                    os.replace(path, folder / appliance_id)
                finally:
                    if os.path.exists(path):
                        os.unlink(path)
            with self.connect() as conn:
                conn.execute("INSERT INTO appliances(id,revision,definition) VALUES(?,1,?) "
                             "ON CONFLICT(id) DO UPDATE SET revision=revision+1,definition=excluded.definition,"
                             "observation=NULL,checked_at=NULL", (appliance_id, json.dumps(definition)))
                conn.execute("UPDATE alerts SET active=0 WHERE appliance_id=?", (appliance_id,))
            return appliance_id

    def archive(self, appliance_id):
        with self.lock, self.connect() as conn:
            conn.execute("UPDATE appliances SET archived=1 WHERE id=?", (appliance_id,))
            conn.execute("UPDATE alerts SET active=0 WHERE appliance_id=?", (appliance_id,))
            (self.root / "appliance-secrets" / appliance_id).unlink(missing_ok=True)

    def state(self, key, value=None):
        with self.lock, self.connect() as conn:
            if value is not None:
                conn.execute("INSERT INTO platform_state VALUES(?,?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload",
                             (key, json.dumps(value)))
            row = conn.execute("SELECT payload FROM platform_state WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else {}

    def record(self, row, observation):
        now = time.time()
        with self.lock, self.connect() as conn:
            changed = conn.execute("UPDATE appliances SET observation=?,checked_at=? WHERE id=? AND revision=? AND archived=0",
                                   (json.dumps(observation), now, row["id"], row["revision"])).rowcount
            if not changed:
                return []
            conn.execute("INSERT INTO observations(appliance_id,checked_at,payload) VALUES(?,?,?)",
                         (row["id"], now, json.dumps(observation)))
            conn.execute("DELETE FROM observations WHERE checked_at<?", (now - 90 * 86400,))
            issues = observation_issues(row, observation)
            old = {a["key"]: dict(a) for a in conn.execute("SELECT * FROM alerts WHERE appliance_id=?", (row["id"],))}
            transitions = []
            for issue in issues:
                key = row["id"] + ":" + issue["key"]
                previous = old.get(key)
                if not previous or not previous["active"]:
                    transitions.append(dict(issue, transition="detected"))
                conn.execute("INSERT INTO alerts VALUES(?,?,?,?,1,?,?,NULL) ON CONFLICT(key) DO UPDATE SET "
                             "active=1,title=excluded.title,severity=excluded.severity,last_seen=excluded.last_seen,"
                             "acknowledged_at=CASE WHEN alerts.active=0 THEN NULL ELSE alerts.acknowledged_at END",
                             (key, row["id"], issue["title"], issue["severity"], now, now))
            keys = {row["id"] + ":" + item["key"] for item in issues}
            known_licenses = {item["key"] for item in observation["licenses"] if item["state"] != "unknown"}
            for key, previous in old.items():
                if previous["active"] and key not in keys:
                    category = key[len(row["id"]) + 1:]
                    # Missing measurements are not recovery evidence.
                    if category == "health" and observation["health"]["state"] == "unknown":
                        continue
                    if category.startswith("license:") and category.split(":")[1] not in known_licenses:
                        continue
                    conn.execute("UPDATE alerts SET active=0 WHERE key=?", (key,))
                    transitions.append(dict(previous, transition="cleared"))
            return transitions

    def alerts(self):
        with self.lock, self.connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM alerts WHERE active=1 ORDER BY severity,first_seen")]

    def acknowledge(self, key):
        with self.lock, self.connect() as conn:
            conn.execute("UPDATE alerts SET acknowledged_at=? WHERE key=? AND active=1", (time.time(), key))

    def history(self, appliance_id):
        with self.lock, self.connect() as conn:
            return [dict(checked_at=row["checked_at"], **json.loads(row["payload"])) for row in conn.execute(
                "SELECT checked_at,payload FROM observations WHERE appliance_id=? ORDER BY id DESC LIMIT 100", (appliance_id,))]


class APIError(RuntimeError):
    def __init__(self, category, message):
        super().__init__(message)
        self.category = category


class FortiGateClient:
    PATHS = {"system/status", "system/performance/status", "license/status"}

    def __init__(self, row, token):
        self.row, self.token = row, token

    def get(self, path):
        if path not in self.PATHS:
            raise ValueError("Unsupported read-only FortiGate endpoint.")
        result = self._request("GET", "/api/v2/monitor/" + path)
        if not isinstance(result.get("results"), dict):
            raise APIError("api_error", "Vendor API did not return a successful monitor object.")
        return result

    def _request(self, method, path, payload=None):
        """Shared bounded transport; concrete clients restrict the endpoint allowlist."""
        parsed = urlsplit(self.row["endpoint"])
        context = ssl._create_unverified_context() if self.row["self_signed"] else ssl.create_default_context()
        conn = http.client.HTTPSConnection(parsed.hostname, parsed.port or 443, timeout=4, context=context)
        try:
            conn.connect()
            pin = hashlib.sha256(conn.sock.getpeercert(binary_form=True)).hexdigest()
            if self.row["fingerprint"] and not hmac.compare_digest(self.row["fingerprint"], pin):
                raise APIError("tls_error", "Certificate fingerprint changed. Verify certificate trust before reconnecting.")
            conn.request(method, path + "?" + urlencode({"vdom": self.row["vdom"]}),
                         body=json.dumps(payload).encode() if payload is not None else None,
                         headers={"Authorization": "Bearer " + self.token, "Accept": "application/json",
                                  "Content-Type": "application/json"})
            response = conn.getresponse()
            if response.status in (401, 403):
                raise APIError("auth_error", "API access denied. Check the token, trusted hosts and permissions.")
            if response.status in (404, 405):
                raise APIError("unsupported", "This API endpoint is unsupported on the appliance.")
            if response.status not in (200, 201):
                raise APIError("api_error", f"Vendor API returned HTTP {response.status}.")
            raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise APIError("api_error", "Vendor response exceeds the size limit.")
            result = json.loads(raw)
            if not isinstance(result, dict) or result.get("status") != "success":
                code = result.get("error") if isinstance(result, dict) else None
                suffix = f" (FortiOS error {code})" if isinstance(code, int) else ""
                raise APIError("api_error", "Vendor API rejected the request" + suffix + ".")
            return result
        except ssl.SSLError as exc:
            raise APIError("tls_error", "TLS verification failed. Check certificate trust.") from exc
        except (OSError, http.client.HTTPException) as exc:
            raise APIError("unreachable", "Management API could not be reached; device power state is unknown.") from exc
        except (ValueError, UnicodeError) as exc:
            raise APIError("api_error", "Vendor API returned unreadable data.") from exc
        finally:
            conn.close()


def expiry_timestamp(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) and number > 0 else None
    except (ValueError, TypeError):
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return parsed.replace(tzinfo=timezone.utc).timestamp() if not parsed.tzinfo else parsed.timestamp()
        except ValueError:
            return None


def normalize_licenses(results, now=None):
    now = time.time() if now is None else now
    licenses = []
    for key, value in results.items():
        if not isinstance(value, dict) or "status" not in value:
            continue
        raw = str(value["status"]).lower()
        expires = expiry_timestamp(value.get("expires"))
        days = math.ceil((expires - now) / 86400) if expires is not None else None
        if raw in ("expired", "vm_expired", "vm_eval_expired"):
            state = "expired"
        elif raw in ("invalid", "vm_invalid", "no_license", "free_license_exhausted", "licensed_exhausted"):
            state = "invalid"
        elif raw in ("valid", "licensed", "vm_valid", "vm_eval", "free_license", "registered", "cloud_logged_in"):
            state = "valid"
        elif raw in ("warning", "expires_soon", "vm_expires_soon", "vm_grace_period", "vm_validation_overdue", "no_support"):
            state = "warning"
        elif raw in ("pending", "vm_pending"):
            state = "pending"
        else:
            state = "unknown"
        if expires is not None and expires <= now:
            state = "expired"
        elif state == "valid" and days is not None and days <= 30:
            state = "warning"
        licenses.append({"key": str(key)[:64], "state": state, "vendor_status": raw[:64],
                         "type": str(value.get("type") or "")[:64], "expires_at": expires, "days_left": days})
    return licenses


def health_status(results):
    metrics = {}
    for key in ("cpu", "memory"):
        raw = results.get(key)
        if isinstance(raw, dict):
            raw = raw.get("usage")
        if isinstance(raw, (float, int)) and not isinstance(raw, bool) and math.isfinite(raw) and 0 <= raw <= 100:
            metrics[key] = raw
    conserve = results.get("conserve_mode")
    if conserve is True or conserve == "on":
        state = "critical"
    elif any(value >= 95 for value in metrics.values()):
        state = "critical"
    elif any(value >= 85 for value in metrics.values()):
        state = "warning"
    elif len(metrics) == 2:
        state = "healthy"
    else:
        state = "unknown"
    return {"state": state, "metrics": metrics}


def observe(row, token, client_factory=FortiGateClient):
    observation = {"availability": "unknown", "api": "not_configured", "health": {"state": "unknown", "metrics": {}},
                   "licenses": [], "errors": [], "identity": {}}
    if row["vendor"] != "fortinet":
        observation["api"] = "unsupported"
        observation["errors"] = ["Inventory and routing supported; vendor monitoring adapter not yet available."]
        return observation
    if not row["endpoint"] or not token:
        observation["errors"] = ["Configure the management HTTPS endpoint and read-only API token."]
        return observation
    client = client_factory(row, token)
    answered = False
    for path in ("system/status", "system/performance/status", "license/status"):
        try:
            reply = client.get(path)
            answered = True
            observation.update(availability="reachable", api="connected")
            data = reply["results"]
            if path == "system/status":
                observation["identity"] = {"model": str(data.get("model") or data.get("model_name") or "")[:80],
                                           "firmware": str(reply.get("version") or data.get("version") or "")[:40],
                                           "serial": str(reply.get("serial") or data.get("serial") or "")[:80]}
            elif path == "system/performance/status":
                observation["health"] = health_status(data)
            else:
                observation["licenses"] = normalize_licenses(data)
                if not observation["licenses"]:
                    observation["errors"].append("License response has no recognized entitlement objects.")
        except APIError as exc:
            observation["errors"].append(f"{path}: {exc}")
            if exc.category in ("auth_error", "api_error", "unsupported"):
                observation["availability"] = "reachable"
            observation["api"] = "partial" if answered else exc.category
            if exc.category in ("tls_error", "auth_error", "unreachable"):
                break
    if answered and observation["errors"]:
        observation["api"] = "partial"
    # Device-controlled strings must never echo the token into evidence or logs.
    if token:
        def redact(value):
            if isinstance(value, str):
                return value.replace(token, "[redacted]")
            if isinstance(value, list):
                return [redact(item) for item in value]
            if isinstance(value, dict):
                return {key: redact(item) for key, item in value.items()}
            return value
        observation = redact(observation)
    return observation


def observation_issues(row, observation):
    issues = []
    if observation["api"] != "connected":
        issues.append({"key": "api", "title": "Monitoring " + observation["api"].replace("_", " "), "severity": "warning"})
    if observation["health"]["state"] in ("warning", "critical"):
        issues.append({"key": "health", "title": "Appliance health " + observation["health"]["state"],
                       "severity": observation["health"]["state"]})
    for license in observation["licenses"]:
        if license["state"] in ("expired", "invalid", "warning", "pending"):
            days = license.get("days_left")
            band = next((n for n in (7, 14, 30) if days is not None and days <= n), None)
            issues.append({"key": "license:" + license["key"] + (f":{band}" if band and days > 0 else ""),
                           "title": license["key"] + " license " + license["state"] + (f" · {days} days left" if days is not None else ""),
                           "severity": "critical" if license["key"] in row["required_licenses"] and license["state"] in ("expired", "invalid") else "warning"})
    return issues


def readiness(row, now=None):
    if not row:
        return {"critical": [], "warnings": ["No inventory appliance is selected; vendor health cannot be verified."]}
    observation = row.get("observation")
    now = time.time() if now is None else now
    if not observation or row.get("checked_at") is None or not 0 <= now - row["checked_at"] <= STALE_AFTER:
        return {"critical": [], "warnings": ["Appliance monitoring is stale or not checked."]}
    issues = observation_issues(row, observation)
    critical = [item["title"] for item in issues if item["severity"] == "critical"]
    warnings = [item["title"] for item in issues if item["severity"] != "critical"]
    present = {item["key"] for item in observation["licenses"] if item["state"] not in ("unknown", "pending")}
    for missing in set(row["required_licenses"]) - present:
        warnings.append(missing + " entitlement could not be verified.")
    if observation["health"]["state"] == "unknown":
        warnings.append("Appliance health could not be verified.")
    return {"critical": critical, "warnings": warnings}
