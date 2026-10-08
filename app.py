#!/usr/bin/env python3
import json
import os
import re
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
)

app = Flask(__name__)
app.secret_key = "techkarma-netem"  

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"

TC = "/usr/sbin/tc"
IP = "/usr/sbin/ip"
GIT = "/usr/bin/git"


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


# ---------- Config ----------

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
    Smoothly interpolate a value across quality breakpoints.

    Breakpoints are intentionally different per metric and access technology,
    so degradation is staged rather than one linear reduction of everything.
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
            # Smoothstep avoids artificial sharp corners while remaining
            # deliberately non-linear between the real-world-inspired stages.
            t = t * t * (3.0 - 2.0 * t)
            return float(v_high) + (float(v_low) - float(v_high)) * t

    return float(points[-1][1])


QUALITY_CURVES = {
    # DIA tends to stay remarkably stable until the service is genuinely
    # stressed/failing. Jitter changes before meaningful packet loss.
    "dia": {
        "delay_factor": [(100, 1.00), (80, 1.00), (60, 1.15), (35, 1.8), (10, 4.5)],
        "jitter_factor": [(100, 1.00), (90, 1.05), (70, 1.8), (40, 5.0), (10, 14.0)],
        "loss_add": [(100, 0.0), (65, 0.0), (45, 0.15), (25, 2.0), (10, 12.0)],
        "rate_factor": [(100, 1.00), (70, 1.00), (50, 0.95), (30, 0.65), (10, 0.20)],
    },
    # DSL line rate is often stable for a while; errors/jitter become visible
    # before severe line degradation forces a large throughput reduction.
    "dsl": {
        "delay_factor": [(100, 1.00), (90, 1.00), (70, 1.15), (45, 1.8), (10, 4.5)],
        "jitter_factor": [(100, 1.00), (90, 1.15), (70, 2.2), (45, 5.0), (10, 12.0)],
        "loss_add": [(100, 0.0), (85, 0.0), (65, 0.15), (40, 1.5), (10, 10.0)],
        "rate_factor": [(100, 1.00), (80, 1.00), (60, 0.90), (35, 0.55), (10, 0.18)],
    },
    # Shared fixed broadband usually shows queueing/jitter before outright
    # packet loss. Throughput starts to fall once congestion is material.
    "broadband": {
        "delay_factor": [(100, 1.00), (90, 1.00), (75, 1.20), (50, 1.8), (10, 5.0)],
        "jitter_factor": [(100, 1.00), (95, 1.05), (80, 1.8), (55, 4.5), (10, 14.0)],
        "loss_add": [(100, 0.0), (75, 0.0), (55, 0.10), (35, 1.5), (10, 12.0)],
        "rate_factor": [(100, 1.00), (90, 1.00), (75, 0.95), (50, 0.70), (10, 0.18)],
    },
    # Cellular capacity and jitter often move first as RF/congestion worsens;
    # sustained loss becomes prominent later.
    "mobile": {
        "delay_factor": [(100, 1.00), (92, 1.05), (75, 1.25), (50, 1.9), (10, 4.5)],
        "jitter_factor": [(100, 1.00), (95, 1.10), (80, 1.8), (55, 4.0), (10, 10.0)],
        "loss_add": [(100, 0.0), (80, 0.0), (60, 0.20), (40, 2.0), (10, 15.0)],
        "rate_factor": [(100, 1.00), (92, 0.95), (75, 0.72), (50, 0.40), (10, 0.10)],
    },
    # Satellite links are latency-heavy by nature. Degradation is represented
    # first by variability/jitter, then capacity, then sharp loss at poor quality.
    "satellite": {
        "delay_factor": [(100, 1.00), (90, 1.02), (70, 1.08), (45, 1.20), (10, 1.55)],
        "jitter_factor": [(100, 1.00), (95, 1.15), (80, 1.8), (55, 3.5), (10, 9.0)],
        "loss_add": [(100, 0.0), (75, 0.0), (55, 0.25), (35, 3.0), (10, 20.0)],
        "rate_factor": [(100, 1.00), (90, 0.98), (70, 0.85), (45, 0.55), (10, 0.18)],
    },
}


def calculate_profile(preset: dict, quality: int):
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
        }

    model = preset.get("quality_model", "broadband")
    curves = QUALITY_CURVES.get(model, QUALITY_CURVES["broadband"])

    delay_factor = _curve_value(q, curves["delay_factor"])
    jitter_factor = _curve_value(q, curves["jitter_factor"])
    loss_add = _curve_value(q, curves["loss_add"])
    rate_factor = _curve_value(q, curves["rate_factor"])

    delay = float(preset.get("delay_ms", 0.0)) * delay_factor
    jitter = float(preset.get("jitter_ms", 0.0)) * jitter_factor
    loss = min(100.0, float(preset.get("loss_pct", 0.0)) + loss_add)

    # tc/tbf compatibility: bandwidth is always an integer Mbit/s.
    download = int(round(max(1.0, float(preset.get("download_mbit", 0.0)) * rate_factor)))
    upload = int(round(max(1.0, float(preset.get("upload_mbit", 0.0)) * rate_factor)))

    return {
        "delay_ms": round(delay, 1),
        "jitter_ms": round(jitter, 1),
        "loss_pct": round(loss, 3),
        "download_mbit": download,
        "upload_mbit": upload,
    }


def apply_selected_profile(link: dict, presets: dict):
    """Apply either the quality-derived or custom profile stored on one WAN."""
    inner = link.get("inner")
    outer = link.get("outer")
    preset_id = link.get("preset", "broadband")
    preset = presets.get(preset_id) or presets.get("broadband")
    if not inner or not preset:
        return False, "Missing interface or preset", {}

    mode = link.get("mode", "quality")
    quality = max(0, min(100, int(link.get("quality", 100))))

    if mode == "custom" and link.get("custom_profile"):
        custom = link["custom_profile"]
        effective = {
            "delay_ms": max(0.0, float(custom.get("delay_ms", 0.0))),
            "jitter_ms": max(0.0, float(custom.get("jitter_ms", 0.0))),
            "loss_pct": min(100.0, max(0.0, float(custom.get("loss_pct", 0.0)))),
            "download_mbit": max(0.0, float(custom.get("download_mbit", 0.0))),
            "upload_mbit": max(0.0, float(custom.get("upload_mbit", 0.0))),
        }
    else:
        effective = calculate_profile(preset, quality)

    ok_down, msg_down = apply_netem(
        inner,
        effective["delay_ms"],
        effective["jitter_ms"],
        effective["loss_pct"],
        effective["download_mbit"],
    )

    ok_up, msg_up = True, "OK"
    if outer:
        ok_up, msg_up = apply_netem(
            outer, 0.0, 0.0, 0.0, effective["upload_mbit"]
        )

    if ok_down and ok_up:
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


def apply_netem(ifname: str, delay_ms: float, jitter_ms: float,
                loss_pct: float, rate_mbit: float):
    """
    Apply netem + optional tbf on interface.
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
        parts.append(f"loss {loss_pct:.3f}%")

    netem_cmd = f"{TC} qdisc add dev {ifname} root handle 1:0 " + " ".join(parts)
    rc, out, err = run_cmd(netem_cmd)
    if rc != 0:
        return False, f"Failed to apply netem: {err or out or 'unknown error'}"

    if rate_mbit and rate_mbit > 0:
        rate_str = f"{rate_mbit:.3f}mbit"
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


# ---------- Nav context ----------

@app.context_processor
def inject_nav():
    cfg = load_config()
    return {
        "nav_items": [
            {"id": "dashboard", "label": "Dashboard", "endpoint": "index"},
            {"id": "presets", "label": "Presets", "endpoint": "presets"},
            {"id": "setup", "label": "Setup", "endpoint": "setup"},
        ],
        "config": cfg,
    }


# ---------- Routes ----------

@app.route("/")
def index():
    cfg = load_config()

    # Send to setup if no links configured
    if not cfg.get("wan_links"):
        return redirect(url_for("setup"))

    mgmt = cfg.get("mgmt_interface") or guess_mgmt_interface()
    if mgmt and not cfg.get("mgmt_interface"):
        cfg["mgmt_interface"] = mgmt
        save_config(cfg)

    presets = get_presets(cfg)
    nic_states = []
    for link in cfg.get("wan_links", []):
        name = link.get("name", "WAN")
        inner = link.get("inner")
        if not inner:
            continue

        outer = link.get("outer")
        preset_id = link.get("preset", "broadband")
        if preset_id not in presets:
            preset_id = next(iter(presets), "")
        preset = presets.get(preset_id, {})
        quality = max(0, min(100, int(link.get("quality", 100))))
        mode = link.get("mode", "quality")
        if mode == "custom" and link.get("custom_profile"):
            effective = {
                "delay_ms": float(link["custom_profile"].get("delay_ms", 0.0)),
                "jitter_ms": float(link["custom_profile"].get("jitter_ms", 0.0)),
                "loss_pct": float(link["custom_profile"].get("loss_pct", 0.0)),
                "download_mbit": float(link["custom_profile"].get("download_mbit", 0.0)),
                "upload_mbit": float(link["custom_profile"].get("upload_mbit", 0.0)),
            }
        else:
            effective = calculate_profile(preset, quality) if preset else {}
            mode = "quality"

        nic_states.append(
            {
                "id": link.get("id") or link.get("bridge") or inner,
                "name": inner,
                "outer": outer,
                "label": name,
                "preset_id": preset_id,
                "quality": quality,
                "mode": mode,
                "quality_status": "Custom" if mode == "custom" else quality_status(quality),
                "effective": effective,
                "qdisc": get_qdisc_state(inner),
                "outer_qdisc": get_qdisc_state(outer) if outer else {
                    "raw": "",
                    "parsed": {
                        "kind": None,
                        "delay_ms": None,
                        "jitter_ms": None,
                        "loss_pct": None,
                        "rate_mbit": None,
                    },
                },
            }
        )

    return render_template(
        "index.html",
        page="dashboard",
        mgmt_interface=mgmt,
        nic_states=nic_states,
        wan_links=cfg.get("wan_links", []),
        presets=presets,
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
                    "mode": previous.get("mode", "quality"),
                    "custom_profile": previous.get("custom_profile"),
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
            return redirect(url_for("index"))
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
    link["mode"] = mode

    if mode == "custom":
        baseline = calculate_profile(preset, quality)

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
            "download_mbit": custom_float(
                "custom_download_mbit", baseline["download_mbit"]
            ),
            "upload_mbit": custom_float(
                "custom_upload_mbit", baseline["upload_mbit"]
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
                "delay_ms": field_float("delay_ms", existing.get("delay_ms", 0.0)),
                "jitter_ms": field_float(
                    "jitter_ms", existing.get("jitter_ms", 0.0)
                ),
                "loss_pct": min(
                    100.0,
                    field_float("loss_pct", existing.get("loss_pct", 0.0)),
                ),
                "download_mbit": field_float(
                    "download_mbit", existing.get("download_mbit", 0.0)
                ),
                "upload_mbit": field_float(
                    "upload_mbit", existing.get("upload_mbit", 0.0)
                ),
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