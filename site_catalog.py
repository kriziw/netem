"""Site scenarios: model a customer site and derive what to test there.

A selection of industry, sub-industry, site function, size and criticality
yields the site's simulated traffic, its pass/fail targets, the WAN lines such a
site typically has, and an ordered test plan written as scenario steps.
Everything here is data plus pure functions; app.py runs the result.
"""
from __future__ import annotations

MAX_SIMULATED_USERS = 5000
# The simulator reports where traffic goes over a 10-second window, so steering
# assertions allow for that much measurement delay on top of the target.
STEERING_WINDOW_S = 10
SETTLE_S = 60

SIZES = {
    "small": {"label": "Small"},
    "medium": {"label": "Medium"},
    "large": {"label": "Large"},
    "very_large": {"label": "Very large"},
}

CRITICALITY = {
    "standard": {
        "label": "Standard",
        "description": "Users notice outages but work continues; minutes of degradation are tolerable.",
        "targets": {"experience_min": 70, "success_min_pct": 98.0, "interactive_p95_max_ms": 800, "steering_max_s": 60},
        "sla": {"latency_ms": 150.0, "jitter_ms": 40.0, "loss_pct": 2.0},
        "media_mode": "realistic",
    },
    "business_critical": {
        "label": "Business-critical",
        "description": "Revenue or operations stop during outages; recovery is expected within seconds.",
        "targets": {"experience_min": 80, "success_min_pct": 99.0, "interactive_p95_max_ms": 400, "steering_max_s": 30},
        "sla": {"latency_ms": 100.0, "jitter_ms": 30.0, "loss_pct": 1.0},
        "media_mode": "realistic",
    },
    "mission_critical": {
        "label": "Mission-critical",
        "description": "Safety, production or patient care depend on the site; users must not notice a WAN failure.",
        "targets": {"experience_min": 90, "success_min_pct": 99.9, "interactive_p95_max_ms": 200, "steering_max_s": 10},
        "sla": {"latency_ms": 50.0, "jitter_ms": 15.0, "loss_pct": 0.5},
        "media_mode": "strict",
    },
}

# Sub-industries weight the applications their sites lean on (1.0 = unchanged).
# "strict_media" judges voice, video and OT strictly whatever the criticality.
INDUSTRIES = {
    "manufacturing": {"label": "Manufacturing", "sub_industries": {
        "automotive": {"label": "Automotive", "apps": {"plm_cad": 1.6, "ot_telemetry": 1.4, "mes": 1.3}},
        "pharma": {"label": "Pharma & life sciences", "apps": {"ot_telemetry": 1.3, "erp": 1.2, "mes": 1.2}, "strict_media": True},
        "food_beverage": {"label": "Food & beverage", "apps": {"ot_telemetry": 1.2, "erp": 1.2, "wms_scan": 1.3}},
        "electronics": {"label": "Electronics & semiconductors", "apps": {"plm_cad": 1.5, "mes": 1.3, "ot_telemetry": 1.2}, "strict_media": True},
        "chemicals": {"label": "Chemicals", "apps": {"ot_telemetry": 1.5, "erp": 1.1}, "strict_media": True},
        "machinery": {"label": "Industrial machinery", "apps": {"plm_cad": 1.4, "erp": 1.2}},
    }},
    "retail": {"label": "Retail", "sub_industries": {
        "grocery": {"label": "Grocery", "apps": {"pos": 1.4, "wms_scan": 1.2}},
        "fashion": {"label": "Fashion & apparel", "apps": {"pos": 1.2, "guest_internet": 1.3, "video": 1.1}},
        "consumer_electronics": {"label": "Consumer electronics", "apps": {"pos": 1.2, "guest_internet": 1.4}},
        "home_improvement": {"label": "Home improvement", "apps": {"pos": 1.2, "wms_scan": 1.4}},
    }},
    "healthcare": {"label": "Healthcare", "sub_industries": {
        "hospital_network": {"label": "Hospital network", "apps": {"emr": 1.3, "pacs_imaging": 1.4, "voice": 1.2}, "strict_media": True},
        "outpatient": {"label": "Outpatient clinics", "apps": {"emr": 1.3, "video": 1.2}},
        "diagnostics": {"label": "Diagnostics & labs", "apps": {"pacs_imaging": 1.6, "ot_telemetry": 1.2}},
    }},
    "financial_services": {"label": "Financial services", "sub_industries": {
        "retail_banking": {"label": "Retail banking", "apps": {"core_banking": 1.4, "video": 1.1}, "strict_media": True},
        "insurance": {"label": "Insurance", "apps": {"web_saas": 1.2, "erp": 1.1, "file_sync": 1.1}},
        "capital_markets": {"label": "Capital markets", "apps": {"voice": 1.3, "video": 1.2, "core_banking": 1.2}, "strict_media": True},
    }},
    "logistics": {"label": "Logistics & transport", "sub_industries": {
        "third_party_logistics": {"label": "Third-party logistics", "apps": {"wms_scan": 1.5, "erp": 1.2}},
        "parcel": {"label": "Parcel & courier", "apps": {"wms_scan": 1.4, "erp": 1.1, "voice": 1.1}},
    }},
    "energy_utilities": {"label": "Energy & utilities", "sub_industries": {
        "power": {"label": "Power generation & grid", "apps": {"ot_telemetry": 1.6}, "strict_media": True},
        "water": {"label": "Water & wastewater", "apps": {"ot_telemetry": 1.5}, "strict_media": True},
        "oil_gas": {"label": "Oil & gas", "apps": {"ot_telemetry": 1.6, "cctv_backhaul": 1.2}, "strict_media": True},
    }},
    "public_education": {"label": "Public sector & education", "sub_industries": {
        "government": {"label": "Government agency", "apps": {"web_saas": 1.1, "video": 1.1}},
        "education": {"label": "Education", "apps": {"guest_internet": 1.5, "video": 1.3}},
    }},
    "professional_services": {"label": "Professional services", "sub_industries": {
        "consulting": {"label": "Consulting", "apps": {"video": 1.3, "collaboration": 1.2}},
        "legal": {"label": "Legal", "apps": {"file_sync": 1.3, "web_saas": 1.1}},
        "engineering_firm": {"label": "Engineering & architecture", "apps": {"plm_cad": 1.4, "file_sync": 1.2}},
    }},
    "hospitality": {"label": "Hospitality", "sub_industries": {
        "hotels": {"label": "Hotels & resorts", "apps": {"guest_internet": 1.6, "pos": 1.1}},
        "restaurants": {"label": "Restaurants", "apps": {"pos": 1.5}},
    }},
}

ALL = "*"


def _per_size(small, medium, large, very_large):
    return {"small": small, "medium": medium, "large": large, "very_large": very_large}


def _line(preset, down, up):
    return {"preset": preset, "download_mbit": down, "upload_mbit": up}


# Each function: which industries have it, who works there (persona shares of the
# active staff), how many employees and devices per size, how busy they are and
# the WAN lines such a site usually has (primary, backup).
SITE_FUNCTIONS = {
    "headquarters": {
        "wan_class": "core", "label": "Headquarters / campus", "industries": ALL, "activity": "normal", "concurrency": 0.4,
        "staff": {"knowledge_worker": 50, "collaboration_user": 25, "developer": 8, "heavy_cloud": 12, "background": 5},
        "employees": _per_size(150, 500, 1500, 4000),
        "devices": {"camera": _per_size(10, 25, 60, 120), "guest": _per_size(5, 20, 50, 120)},
    },
    "regional_office": {
        "wan_class": "office", "label": "Regional office", "industries": ALL, "activity": "normal", "concurrency": 0.4,
        "staff": {"knowledge_worker": 55, "collaboration_user": 25, "heavy_cloud": 10, "background": 10},
        "employees": _per_size(40, 120, 300, 700),
        "devices": {"camera": _per_size(4, 8, 16, 30), "guest": _per_size(2, 5, 10, 20)},
    },
    "branch_office": {
        "wan_class": "office", "label": "Branch office", "industries": ALL, "activity": "normal", "concurrency": 0.45,
        "staff": {"knowledge_worker": 60, "collaboration_user": 25, "background": 15},
        "employees": _per_size(8, 25, 60, 120),
        "devices": {"camera": _per_size(2, 4, 6, 10)},
    },
    "plant": {
        "wan_class": "core", "label": "Manufacturing plant", "industries": ["manufacturing", "energy_utilities"], "activity": "busy", "concurrency": 0.5,
        "staff": {"shop_floor": 55, "engineer": 12, "knowledge_worker": 18, "collaboration_user": 5, "background": 10},
        "employees": _per_size(80, 300, 1200, 3000),
        "devices": {"ot_device": _per_size(20, 80, 300, 700), "camera": _per_size(8, 20, 60, 150)},
    },
    "rnd_center": {
        "wan_class": "core", "label": "R&D / engineering center", "industries": ["manufacturing", "healthcare", "professional_services"],
        "activity": "busy", "concurrency": 0.5,
        "staff": {"engineer": 45, "developer": 20, "knowledge_worker": 20, "collaboration_user": 10, "heavy_cloud": 5},
        "employees": _per_size(30, 120, 400, 1000),
        "devices": {"camera": _per_size(3, 8, 20, 40)},
    },
    "warehouse": {
        "wan_class": "frontline", "label": "Warehouse / distribution center", "industries": ["manufacturing", "retail", "logistics"],
        "activity": "busy", "concurrency": 0.6,
        "staff": {"warehouse_operator": 65, "knowledge_worker": 15, "collaboration_user": 5, "background": 15},
        "employees": _per_size(25, 80, 250, 600),
        "devices": {"camera": _per_size(10, 25, 60, 120), "ot_device": _per_size(5, 15, 40, 100)},
    },
    "retail_store": {
        "wan_class": "frontline", "label": "Retail store", "industries": ["retail"], "activity": "busy", "concurrency": 0.7,
        "staff": {"store_associate": 75, "knowledge_worker": 15, "background": 10},
        "employees": _per_size(6, 20, 60, 150),
        "devices": {"camera": _per_size(4, 10, 24, 48), "guest": _per_size(5, 20, 60, 150)},
    },
    "restaurant": {
        "wan_class": "frontline", "label": "Restaurant", "industries": ["hospitality"], "activity": "busy", "concurrency": 0.7,
        "staff": {"store_associate": 70, "knowledge_worker": 10, "background": 20},
        "employees": _per_size(5, 15, 30, 60),
        "devices": {"camera": _per_size(2, 4, 8, 12), "guest": _per_size(10, 30, 60, 100)},
    },
    "hotel": {
        "wan_class": "frontline", "label": "Hotel", "industries": ["hospitality"], "activity": "normal", "concurrency": 0.5,
        "staff": {"store_associate": 35, "knowledge_worker": 35, "collaboration_user": 10, "background": 20},
        "employees": _per_size(20, 60, 200, 500),
        "devices": {"camera": _per_size(10, 30, 80, 160), "guest": _per_size(30, 120, 400, 900)},
    },
    "hospital": {
        "wan_class": "core", "label": "Hospital", "industries": ["healthcare"], "activity": "busy", "concurrency": 0.45,
        "staff": {"clinician": 60, "knowledge_worker": 20, "collaboration_user": 10, "background": 10},
        "employees": _per_size(200, 600, 2000, 5000),
        "devices": {"ot_device": _per_size(20, 60, 200, 500), "camera": _per_size(20, 50, 150, 300), "guest": _per_size(20, 60, 200, 400)},
    },
    "clinic": {
        "wan_class": "frontline", "label": "Clinic", "industries": ["healthcare"], "activity": "normal", "concurrency": 0.55,
        "staff": {"clinician": 65, "knowledge_worker": 25, "background": 10},
        "employees": _per_size(8, 20, 50, 120),
        "devices": {"camera": _per_size(2, 4, 8, 12), "guest": _per_size(3, 8, 20, 40)},
    },
    "laboratory": {
        "wan_class": "core", "label": "Laboratory", "industries": ["healthcare"], "activity": "busy", "concurrency": 0.5,
        "staff": {"clinician": 40, "engineer": 20, "knowledge_worker": 30, "background": 10},
        "employees": _per_size(15, 50, 150, 400),
        "devices": {"ot_device": _per_size(10, 30, 80, 200), "camera": _per_size(2, 6, 12, 24)},
    },
    "bank_branch": {
        "wan_class": "frontline", "label": "Bank branch", "industries": ["financial_services"], "activity": "normal", "concurrency": 0.6,
        "staff": {"banker": 70, "knowledge_worker": 20, "background": 10},
        "employees": _per_size(5, 12, 30, 60),
        "devices": {"camera": _per_size(4, 8, 12, 20), "guest": _per_size(2, 5, 10, 20)},
    },
    "field_site": {
        "wan_class": "remote", "label": "Field / remote site", "industries": ["energy_utilities", "logistics"], "activity": "normal", "concurrency": 0.5,
        "staff": {"knowledge_worker": 40, "shop_floor": 40, "background": 20},
        "employees": _per_size(2, 5, 15, 40),
        "devices": {"ot_device": _per_size(10, 40, 120, 300), "camera": _per_size(2, 4, 8, 16)},
    },
    "school": {
        "wan_class": "office", "label": "School / campus", "industries": ["public_education"], "activity": "normal", "concurrency": 0.5,
        "staff": {"knowledge_worker": 30, "collaboration_user": 20, "guest": 50},
        "employees": _per_size(60, 200, 800, 2500),
        "devices": {"camera": _per_size(6, 15, 40, 80)},
    },
}

# Typical lines by site category, size and criticality: (primary, backup). Core
# sites (headquarters, plants, hospitals, labs, R&D) move to dual DIA once the
# business depends on them; offices, frontline and remote sites step up gradually.
# Dual DIA lines have matching bandwidth so either can carry the whole site.
WAN_LINES = {
    "core": {
        "small": {"standard": (("dia", 200, 200), ("4g", 80, 20)),
                  "business_critical": (("dia", 200, 200), ("broadband", 300, 50)),
                  "mission_critical": (("dia", 200, 200), ("dia", 200, 200))},
        "medium": {"standard": (("dia", 500, 500), ("broadband", 300, 50)),
                   "business_critical": (("dia", 500, 500), ("dia", 500, 500)),
                   "mission_critical": (("dia", 500, 500), ("dia", 500, 500))},
        "large": {"standard": (("dia", 1000, 1000), ("broadband", 500, 50)),
                  "business_critical": (("dia", 1000, 1000), ("dia", 1000, 1000)),
                  "mission_critical": (("dia", 1000, 1000), ("dia", 1000, 1000))},
        "very_large": {"standard": (("dia", 1000, 1000), ("dia", 1000, 1000)),
                       "business_critical": (("dia", 1000, 1000), ("dia", 1000, 1000)),
                       "mission_critical": (("dia", 1000, 1000), ("dia", 1000, 1000))},
    },
    "office": {
        "small": {"standard": (("broadband", 100, 20), ("4g", 80, 20)),
                  "business_critical": (("broadband", 300, 50), ("5g", 300, 50)),
                  "mission_critical": (("dia", 100, 100), ("broadband", 300, 50))},
        "medium": {"standard": (("broadband", 300, 50), ("4g", 80, 20)),
                   "business_critical": (("dia", 200, 200), ("broadband", 300, 50)),
                   "mission_critical": (("dia", 200, 200), ("dia", 200, 200))},
        "large": {"standard": (("dia", 200, 200), ("broadband", 300, 50)),
                  "business_critical": (("dia", 500, 500), ("broadband", 500, 50)),
                  "mission_critical": (("dia", 500, 500), ("dia", 500, 500))},
        "very_large": {"standard": (("dia", 500, 500), ("broadband", 500, 50)),
                       "business_critical": (("dia", 1000, 1000), ("dia", 1000, 1000)),
                       "mission_critical": (("dia", 1000, 1000), ("dia", 1000, 1000))},
    },
    "frontline": {
        "small": {"standard": (("broadband", 100, 20), ("4g", 80, 20)),
                  "business_critical": (("broadband", 300, 50), ("5g", 300, 50)),
                  "mission_critical": (("dia", 100, 100), ("5g", 300, 50))},
        "medium": {"standard": (("broadband", 300, 50), ("4g", 80, 20)),
                   "business_critical": (("broadband", 500, 50), ("5g", 300, 50)),
                   "mission_critical": (("dia", 200, 200), ("5g", 300, 50))},
        "large": {"standard": (("broadband", 500, 50), ("5g", 300, 50)),
                  "business_critical": (("dia", 200, 200), ("broadband", 500, 50)),
                  "mission_critical": (("dia", 500, 500), ("dia", 500, 500))},
        "very_large": {"standard": (("dia", 200, 200), ("broadband", 500, 50)),
                       "business_critical": (("dia", 500, 500), ("broadband", 500, 50)),
                       "mission_critical": (("dia", 1000, 1000), ("dia", 1000, 1000))},
    },
    "remote": {
        "small": {"standard": (("satellite", 100, 20), ("4g", 80, 20)),
                  "business_critical": (("5g", 300, 50), ("satellite", 100, 20)),
                  "mission_critical": (("5g", 300, 50), ("satellite", 100, 20))},
        "medium": {"standard": (("4g", 80, 20), ("satellite", 100, 20)),
                   "business_critical": (("dsl", 100, 20), ("5g", 300, 50)),
                   "mission_critical": (("broadband", 300, 50), ("5g", 300, 50))},
        "large": {"standard": (("dsl", 100, 20), ("4g", 80, 20)),
                  "business_critical": (("broadband", 300, 50), ("5g", 300, 50)),
                  "mission_critical": (("dia", 100, 100), ("5g", 300, 50))},
        "very_large": {"standard": (("broadband", 300, 50), ("4g", 80, 20)),
                       "business_critical": (("dia", 200, 200), ("5g", 300, 50)),
                       "mission_critical": (("dia", 200, 200), ("broadband", 300, 50))},
    },
}
# Applications of Traffic Simulator v0.8; the connected simulator's catalog takes precedence.
SIMULATOR_APPS = (
    "web_saas", "collaboration", "voice", "video", "file_sync", "developer", "updates", "backup", "dns",
    "ot_telemetry", "mes", "erp", "plm_cad", "pos", "wms_scan", "emr", "pacs_imaging", "core_banking",
    "cctv_backhaul", "guest_internet",
)


def catalog():
    """The choices for the selector: industries with sub-industries, functions per industry, sizes, criticality."""
    return {
        "industries": {key: {"label": item["label"],
                             "sub_industries": {sub: {"label": value["label"]} for sub, value in item["sub_industries"].items()},
                             "functions": [name for name, function in SITE_FUNCTIONS.items()
                                           if function["industries"] == ALL or key in function["industries"]]}
                       for key, item in INDUSTRIES.items()},
        "functions": {key: {"label": item["label"]} for key, item in SITE_FUNCTIONS.items()},
        "sizes": {key: item["label"] for key, item in SIZES.items()},
        "criticality": {key: {"label": item["label"], "description": item["description"]} for key, item in CRITICALITY.items()},
    }


def validate_selection(raw):
    if not isinstance(raw, dict):
        raise ValueError("Choose an industry, sub-industry, site function, size and criticality.")
    selection = {key: str(raw.get(key) or "").strip() for key in ("industry", "sub_industry", "function", "size", "criticality")}
    industry = INDUSTRIES.get(selection["industry"])
    if not industry:
        raise ValueError("Unknown industry.")
    if selection["sub_industry"] not in industry["sub_industries"]:
        raise ValueError("Unknown sub-industry for this industry.")
    function = SITE_FUNCTIONS.get(selection["function"])
    if not function or (function["industries"] != ALL and selection["industry"] not in function["industries"]):
        raise ValueError("This site function does not exist in the chosen industry.")
    if selection["size"] not in SIZES:
        raise ValueError("Unknown site size.")
    if selection["criticality"] not in CRITICALITY:
        raise ValueError("Unknown criticality.")
    return selection


def site_label(selection):
    sub = INDUSTRIES[selection["industry"]]["sub_industries"][selection["sub_industry"]]["label"]
    function = SITE_FUNCTIONS[selection["function"]]["label"]
    return (f"{sub} · {function} · {SIZES[selection['size']]['label']} · "
            f"{CRITICALITY[selection['criticality']]['label']}")


def wan_lines(selection):
    """Typical primary and backup lines for the site's category, size and criticality."""
    category = SITE_FUNCTIONS[selection["function"]]["wan_class"]
    primary, backup = (_line(*line) for line in WAN_LINES[category][selection["size"]][selection["criticality"]])
    notes = []
    if primary["preset"] == backup["preset"] == "dia":
        notes.append("Dual DIA with matching bandwidth, so either line can carry the whole site; order the second "
                     "line from a different carrier with a separate building entry.")
    if category == "remote":
        notes.append("Remote sites rarely have a second wired line; mobile or satellite backup is typical.")
    elif backup["preset"] in ("4g", "5g"):
        notes.append("Mobile backup keeps the site online but carries less traffic than the primary.")
    return {"primary": dict(primary, role="primary"), "backup": dict(backup, role="backup"), "notes": notes}


def workload(selection):
    """Simulator workload for the site: concurrent users by persona and application weights."""
    function = SITE_FUNCTIONS[selection["function"]]
    sub = INDUSTRIES[selection["industry"]]["sub_industries"][selection["sub_industry"]]
    criticality = CRITICALITY[selection["criticality"]]
    size = selection["size"]
    employees = function["employees"][size]
    active = max(1, round(employees * function["concurrency"]))
    shares = function["staff"]
    total = sum(shares.values())
    personas = {name: round(active * share / total) for name, share in shares.items()}
    for name, counts in function.get("devices", {}).items():
        personas[name] = personas.get(name, 0) + counts[size]
    personas = {name: count for name, count in personas.items() if count > 0}
    users = sum(personas.values())
    scaled = users > MAX_SIMULATED_USERS
    if scaled:
        users = MAX_SIMULATED_USERS
    return {
        "employees": employees,
        "devices": {name: counts[size] for name, counts in function.get("devices", {}).items()},
        "users": users,
        "scaled_to_limit": scaled,
        "personas": personas,
        "application_weights": dict(sub["apps"]),
        "activity": function["activity"],
        "media_mode": "strict" if sub.get("strict_media") else criticality["media_mode"],
        "label": site_label(selection),
    }


def fit_to_simulator(load, catalog_payload):
    """Start payload limited to what the connected simulator offers, with warnings.

    Application weights are always sent: without them the simulator falls back to
    its generic office mix and the site's personas would lose their applications.
    """
    personas_known = set(((catalog_payload or {}).get("personas") or {}))
    apps_known = set(((catalog_payload or {}).get("applications") or {})) or set(SIMULATOR_APPS)
    warnings = []
    personas = {name: count for name, count in load["personas"].items() if not personas_known or name in personas_known}
    if personas_known and set(load["personas"]) - personas_known:
        warnings.append("The connected Traffic Simulator lacks some site personas ("
                        + ", ".join(sorted(set(load["personas"]) - personas_known))
                        + "); update it for industry applications.")
    missing_apps = set(load["application_weights"]) - apps_known
    if missing_apps:
        warnings.append("Update the simulator for industry applications: " + ", ".join(sorted(missing_apps)) + ".")
    if not personas:
        personas = {"knowledge_worker": load["users"]}
    payload = {
        "operation": "start",
        "users": max(1, min(MAX_SIMULATED_USERS, sum(personas.values()))),
        "spawn_rate": max(5.0, round(load["users"] / 30.0, 1)),
        "activity": load["activity"],
        "pattern": "steady",
        "personas": personas,
        "media_mode": load["media_mode"],
        "label": load["label"],
        "applications": {app: float(load["application_weights"].get(app, 1.0)) for app in sorted(apps_known)},
    }
    if catalog_payload is not None and "media_modes" not in catalog_payload:
        payload.pop("media_mode")
    # Run labels shipped with the industry catalog; older start APIs reject them.
    if catalog_payload is not None and "ot_telemetry" not in apps_known:
        payload.pop("label", None)
    return payload, warnings


def _dem_assert(label, field, op, value, after=0, window=60, timeout=30):
    return {"after": after, "action": "assert", "label": label, "on_fail": "continue", "timeout": timeout,
            "condition": {"type": "dem", "field": field, "op": op, "value": value, "window": window}}


def _steering_assert(label, traffic_class, within):
    return {"after": 0, "action": "assert", "label": label, "on_fail": "continue", "timeout": within, "poll": 1,
            "condition": {"type": "steering", "class": traffic_class, "within": within - STEERING_WINDOW_S}}


def _phase(phase, label, after=0):
    return {"after": after, "action": "phase", "label": label, "phase": phase}


def _in_phase(phase, *steps):
    return [dict(step, phase=phase) for step in steps]


def test_plan(selection, start_value):
    """Ordered site tests as scenario definitions; each names the WAN role it runs on.

    Every test runs about 3.5-4.5 minutes in named phases: the workload warms up, a
    baseline is measured, the impairment holds long enough for SD-WAN health checks
    and experience windows to react, and recovery is measured before stopping.
    """
    targets = CRITICALITY[selection["criticality"]]["targets"]
    critical = selection["criticality"] != "standard"
    mission = selection["criticality"] == "mission_critical"
    react = targets["steering_max_s"] + STEERING_WINDOW_S
    experience = f"Experience ≥ {targets['experience_min']}"
    success = f"Request success ≥ {targets['success_min_pct']:g}%"
    interactive = f"Interactive P95 ≤ {targets['interactive_p95_max_ms']} ms"
    steered = f"steered within {targets['steering_max_s']} s"

    def warm_up():
        return [{"after": 0, "action": "traffic_generator", "label": "Start site workload",
                 "value": dict(start_value), "phase": "Warm-up"}]

    def baseline(hold=SETTLE_S):
        return [_phase("Baseline", "Measuring normal experience", after=SETTLE_S),
                *_in_phase("Baseline",
                           _dem_assert(experience, "experience_score", ">=", targets["experience_min"], after=hold, window=hold),
                           _dem_assert(success, "availability_pct", ">=", targets["success_min_pct"], window=hold))]

    def stop(after=5, phase="Recovery"):
        return [{"after": after, "action": "traffic_generator", "label": "Stop site workload",
                 "value": {"operation": "stop"}, "phase": phase}]

    tests = [
        {"id": "baseline", "role": "primary", "name": "Baseline experience",
         "description": "Both WANs healthy: the site workload must meet its targets over a steady period.",
         "steps": [*warm_up(),
                   _phase("Steady state", "Measuring a steady site workload", after=SETTLE_S),
                   *_in_phase("Steady state",
                              _dem_assert(experience, "experience_score", ">=", targets["experience_min"], after=150, window=120),
                              _dem_assert(success, "availability_pct", ">=", targets["success_min_pct"], window=120),
                              _dem_assert(interactive, "interactive_p95_ms", "<=", targets["interactive_p95_max_ms"], window=120)),
                   *stop(phase="Steady state")]},
        {"id": "primary_brownout", "role": "primary", "name": "Primary WAN brownout",
         "description": "The primary WAN degrades: targets must hold, or voice/video must move to the backup.",
         "steps": [*warm_up(), *baseline(),
                   *_in_phase("Brownout",
                              {"after": 0, "action": "quality", "value": 60, "label": "Primary at 60% quality"},
                              _steering_assert(f"Voice & video {steered}", "realtime", react),
                              _dem_assert(experience, "experience_score", ">=", targets["experience_min"], after=60, window=60)),
                   *_in_phase("Recovery",
                              {"after": 30, "action": "quality", "value": 100, "label": "Primary restored"},
                              _dem_assert(f"Recovered: {experience}", "experience_score", ">=", targets["experience_min"], after=45, window=30)),
                   *stop()]},
        {"id": "primary_outage", "role": "primary", "name": "Primary WAN outage",
         "description": "The primary WAN fails with its link still up: the appliance must move traffic within the target.",
         "steps": [*warm_up(), *baseline(),
                   *_in_phase("Outage",
                              {"after": 0, "action": "fault", "value": "blackhole", "label": "Primary blackholed"},
                              _steering_assert(f"Voice & video {steered}", "realtime", react),
                              _steering_assert(f"Interactive apps {steered}", "interactive", react),
                              _dem_assert(success, "availability_pct", ">=", targets["success_min_pct"], after=45, window=45)),
                   *_in_phase("Recovery",
                              {"after": 30, "action": "fault", "value": "normal", "label": "Primary restored"},
                              _dem_assert(f"Recovered: {success}", "availability_pct", ">=", targets["success_min_pct"], after=45, window=30)),
                   *stop()]},
    ]
    if critical:
        tests.append({
            "id": "backup_outage", "role": "backup", "name": "Backup WAN outage",
            "description": "The backup fails while the primary is healthy: users must not notice.",
            "steps": [*warm_up(), *baseline(),
                      *_in_phase("Backup outage",
                                 {"after": 0, "action": "fault", "value": "blackhole", "label": "Backup blackholed"},
                                 _dem_assert(experience, "experience_score", ">=", targets["experience_min"], after=75, window=60),
                                 _dem_assert(success, "availability_pct", ">=", targets["success_min_pct"], window=60)),
                      *_in_phase("Recovery",
                                 {"after": 15, "action": "fault", "value": "normal", "label": "Backup restored"},
                                 _phase("Recovery", "Backup recovering", after=40)),
                      *stop()]})
        tests.append({
            "id": "primary_saturation", "role": "primary", "name": "Primary WAN saturation",
            "description": "The primary is congested: voice/video and interactive apps must keep working while bulk traffic slows.",
            "steps": [*warm_up(), *baseline(),
                      *_in_phase("Saturation",
                                 {"after": 0, "action": "quality", "value": 30, "label": "Primary at 30% quality"},
                                 _dem_assert(f"Voice & video {success.lower()}", "realtime_availability_pct", ">=", targets["success_min_pct"], after=75, window=60),
                                 _dem_assert(interactive, "interactive_p95_ms", "<=", targets["interactive_p95_max_ms"], window=60)),
                      *_in_phase("Recovery",
                                 {"after": 15, "action": "quality", "value": 100, "label": "Primary restored"},
                                 _phase("Recovery", "Primary recovering", after=40)),
                      *stop()]})
    if mission:
        flaps = []
        for _cycle in range(2):
            flaps += [{"after": 0 if not flaps else 15, "action": "fault", "value": "downstream_blackhole", "label": "Downstream failure"},
                      {"after": 15, "action": "fault", "value": "normal", "label": "Recovered"},
                      {"after": 15, "action": "fault", "value": "upstream_blackhole", "label": "Upstream failure"},
                      {"after": 15, "action": "fault", "value": "normal", "label": "Recovered"}]
        tests.append({
            "id": "flaky_primary", "role": "primary", "name": "Flaky primary WAN",
            "description": "The primary fails one way, then the other, in short bursts: the site must stay within its success target.",
            "steps": [*warm_up(), *baseline(hold=30),
                      *_in_phase("Flapping", *flaps),
                      *_in_phase("Recovery",
                                 _dem_assert(success, "availability_pct", ">=", targets["success_min_pct"], after=30, window=150),
                                 _phase("Recovery", "Primary stable", after=30)),
                      *stop()]})
    return tests


def build_site_plan(raw_selection, simulator_catalog=None):
    """Everything the selection implies, ready for preview, saving and running."""
    selection = validate_selection(raw_selection)
    load = workload(selection)
    start, warnings = fit_to_simulator(load, simulator_catalog)
    criticality = CRITICALITY[selection["criticality"]]
    application_mix = {}
    persona_catalog = (simulator_catalog or {}).get("personas") or {}
    persona_total = sum(start["personas"].values())
    for name, share in start["personas"].items():
        base = (persona_catalog.get(name) or {}).get("applications") or {}
        weighted = {app: weight * base.get(app, 0) for app, weight in start["applications"].items()}
        total = sum(weighted.values())
        if not total:
            continue
        for app, weight in weighted.items():
            if weight:
                application_mix[app] = application_mix.get(app, 0) + weight / total * share / persona_total * 100
    return {
        "selection": selection,
        "label": load["label"],
        "workload": load,
        "start": start,
        "warnings": warnings,
        "application_mix": application_mix,
        "targets": dict(criticality["targets"], media_mode=load["media_mode"]),
        "sla_profile": dict(criticality["sla"], name=load["label"][:80]),
        "wan_lines": wan_lines(selection),
        "tests": test_plan(selection, start),
    }
