"""Inventory UI, monitoring lifecycle and simulator route orchestration."""
from __future__ import annotations

import threading
import time
import ipaddress
import uuid

from flask import Response, abort, flash, g, jsonify, redirect, render_template, request, url_for

from appliance_inventory import INTERVAL, STALE_AFTER, Store, observe, readiness, validate

START_ENDPOINTS = {"traffic_generator_start", "lab_scenario_start", "site_run"}


class Platform:
    def __init__(self, netem):
        self.netem = netem
        self.store = Store(netem.RUNTIME_DIR)
        self.operations = threading.RLock()
        self.checks = threading.Lock()
        self.platform_checks = threading.Lock()
        self.thread = None
        self.platform_thread = None

    def configured(self):
        return bool(self.netem.traffic_generator_config().get("host") and self.netem.traffic_generator_api_key())

    def check_appliance(self, row):
        with self.checks:
            current = self.store.get(row["id"])
            if not current:
                raise ValueError("Appliance no longer exists.")
            token = self.store.token(row["id"])
            result = observe(current, token)
            for issue in self.store.record(current, result):
                self.netem.log_event("appliance-health", f"{current['name']}: {issue['transition']} · {issue['title']}",
                                     appliance_id=current["id"], severity=issue["severity"])
            return self.store.get(row["id"])

    def check_platform(self):
        with self.platform_checks:
            result = {"checked_at": time.time(), "configured": self.configured(), "connected": False,
                      "simulator": {}, "network": {}, "path": {}, "target_management": {}, "path_health": {}, "errors": []}
            if result["configured"]:
                for name, path in (("simulator", "/api/v1/health"), ("network", "/api/v1/network"),
                                   ("path", "/api/v1/network/readiness"), ("target_management", "/api/v1/target/health")):
                    try:
                        result[name] = self.netem.traffic_generator_request(path, timeout=10 if name == "target_management" else 4)
                    except RuntimeError as exc:
                        result["errors"].append(str(exc))
                result["connected"] = result["simulator"].get("service") == "netem-traffic-simulator"
                selected = result["network"].get("selected") or {}
                match = next((row for row in self.store.rows() if selected and self.route_matches(row, selected)), None)
                if result["network"]:
                    previous = self.store.state("selection")
                    selection = {"id": match["id"] if match else None, "checked_at": time.time(),
                                 "mode": selected.get("mode", "appliance"), "name": selected.get("name"), "path_id": selected.get("id"),
                                 "route": {key: selected.get(key) for key in ("interface", "gateway", "target", "source", "wan_id", "policy_table")},
                                 "active": bool((result["network"].get("route_health") or {}).get("active"))}
                    self.store.state("selection", selection)
                    if (previous.get("id"), previous.get("path_id"), previous.get("mode")) != (selection["id"], selection["path_id"], selection["mode"]):
                        self.netem.APPLIANCE_CACHE.update(checked=None, payload=None)
                        self.netem.log_event("appliance-selection", "Selected traffic appliance changed",
                                             appliance_id=selection["id"], route=selection["route"])
                try:
                    result["path_health"] = self.netem.traffic_generator_request("/api/v1/network/paths/health", timeout=12)
                except RuntimeError:
                    result["path_health"] = {"paths": [], "message": "Independent path checks unavailable; update/configure the simulator."}
            old = self.store.state("platform")
            # Report connectivity transitions once, rather than on every polling cycle.
            if old.get("connected") != result["connected"] and result["configured"]:
                self.netem.log_event("platform-health", "Traffic Simulator " + ("connected" if result["connected"] else "unavailable"))
            self.store.state("platform", result)
            return result

    @staticmethod
    def route_matches(row, selected):
        return bool(row.get("gateway") and row.get("mode", "appliance") == selected.get("mode", "appliance")
                    and (not row.get("source") or row["source"] == selected.get("source"))
                    and all(row.get(key) == selected.get(key) for key in ("interface", "gateway", "target")))

    def snapshot(self):
        rows = self.store.rows()
        selection = self.store.state("selection")
        platform = self.store.state("platform")
        now = time.time()
        platform_stale = not platform.get("checked_at") or not 0 <= now - platform["checked_at"] <= 90
        selection_verified = bool(not platform_stale and platform.get("connected") and selection.get("id")
                                  and platform.get("network", {}).get("selected")
                                  and (platform.get("network", {}).get("route_health") or {}).get("active")
                                  and any(row["id"] == selection.get("id") and self.route_matches(row, platform["network"]["selected"])
                                          for row in rows))
        for row in rows:
            row["has_token"] = bool(self.store.token(row["id"]))
            row["selected"] = row["id"] == selection.get("id")
            row["stale"] = row["checked_at"] is None or not 0 <= now - row["checked_at"] <= STALE_AFTER
            row["readiness"] = readiness(row)
        names = {row["id"]: row["name"] for row in rows}
        alerts = [dict(alert, appliance_name=names.get(alert["appliance_id"], "Archived appliance")) for alert in self.store.alerts()]
        return {"appliances": rows, "selection": selection, "platform": platform,
                "platform_stale": platform_stale, "selection_verified": selection_verified,
                "alerts": alerts, "interval_s": INTERVAL, "traffic_paths": self.store.state("traffic_paths").get("paths", [])}

    def selected(self):
        return self.store.get(self.store.state("selection").get("id"))

    def ensure_idle(self):
        if self.netem.scenario_snapshot().get("active") or self.netem.site_plan_snapshot().get("active"):
            raise ValueError("Finish the running test or plan before changing appliances.")
        status = self.netem.traffic_generator_request("/api/v1/status", timeout=4)
        if status.get("status") not in ("idle", "stopped", "interrupted", "failed"):
            raise ValueError("Finish the running workload before changing appliances.")
        if self.store.state("platform").get("network", {}).get("busy"):
            raise ValueError("Wait for the pending simulator operation to finish.")

    def select(self, appliance_id, wait=35):
        row = self.store.get(appliance_id)
        if not row or not row["gateway"]:
            raise ValueError("Configure this appliance's traffic gateway and data interface first.")
        self.check_platform()
        self.ensure_idle()
        if self.route_matches(row, self.store.state("platform").get("network", {}).get("selected") or {}):
            if (self.store.state("selection").get("active")
                    and (self.store.state("platform").get("network", {}).get("selected") or {}).get("policy_table")):
                return "This appliance's route is already active."
        remote_id = row.get("simulator_id") or row["id"]
        payload = {key: row[key] for key in ("name", "model", "firmware", "interface", "gateway", "target")}
        payload["vendor"] = (self.netem.SDWAN_VENDORS.get(row["vendor"]) or {}).get("name", "Other")
        try:
            self.netem.traffic_generator_request("/api/v1/network/appliances/" + remote_id,
                                                method="PUT", payload=payload, timeout=5)
        except RuntimeError as exc:
            if "HTTP 404" in str(exc) or "HTTP 405" in str(exc):
                raise ValueError("Update the Traffic Simulator to a release with the inventory API before selecting this appliance.") from exc
            raise
        job = self.netem.traffic_generator_request("/api/v1/network/select", method="POST",
                                                  payload={"appliance_id": remote_id}, timeout=5)
        return self.wait_selection(job, row, wait)

    def wait_selection(self, job, row, wait=35, selecting=True):
        if not job.get("id"):
            raise ValueError("Simulator did not return a routing job ID. Check Platform Health.")
        self.store.state("pending", {"id": job["id"], "appliance_id": row["id"], "started_at": time.time()})
        deadline = time.monotonic() + wait
        pause = threading.Event()
        while time.monotonic() < deadline:
            pause.wait(1)
            platform = self.check_platform()
            state = (platform.get("network") or {}).get("job") or {}
            if state.get("id") != job["id"]:
                continue
            if state.get("state") == "failed":
                self.store.state("pending", {})
                self.netem.log_event("appliance-selection", "Appliance switch failed; simulator restored the previous route",
                                     appliance_id=row["id"], job_id=job["id"])
                raise ValueError("Route verification failed. The simulator attempted to restore the previous route; inspect Platform Health.")
            if state.get("state") == "completed":
                self.store.state("pending", {})
                if not selecting:
                    return "Path configured and verified. Independent health checks are active; workload selection is unchanged."
                if (self.route_matches(row, platform["network"].get("selected") or {})
                        and (platform["network"].get("route_health") or {}).get("active")
                        and platform["path"].get("ready") is True):
                    self.netem.TRAFFIC_PATH_CACHE.update(checked=None, payload=None)
                    return "Workload path selected and verified. Full HTTP/UDP traffic is pinned here; independent health checks keep their own paths."
                raise ValueError("Routing job completed but the target path is not verified. Check Platform Health.")
        raise ValueError("Route verification is still pending. Check Platform Health before starting tests.")

    def configure_path(self, form):
        definition = validate(dict(form, vendor="other", kind="physical"), self.netem.SDWAN_VENDORS)
        mode = form.get("mode")
        if mode not in ("direct_netem", "direct_baseline") or not definition["gateway"]:
            raise ValueError("Choose a direct path mode, data interface and router gateway.")
        wan_id = form.get("wan_id", "") if mode == "direct_netem" else ""
        if mode == "direct_netem" and not self.netem.get_link(self.netem.load_config(), wan_id):
            raise ValueError("Choose the NetEm WAN this path is physically wired through.")
        source = str(form.get("source") or "").strip()
        if source:
            try:
                source = str(ipaddress.IPv4Address(source))
            except ValueError as exc:
                raise ValueError("Source must be an IPv4 address assigned to that data interface.") from exc
        paths = self.store.state("traffic_paths").get("paths", [])
        if len(paths) >= 8:
            raise ValueError("Up to eight traffic paths can be configured.")
        row = {key: definition[key] for key in ("name", "gateway", "interface", "target")}
        row.update(id=uuid.uuid4().hex, mode=mode, wan_id=wan_id, source=source)
        self.check_platform()
        self.ensure_idle()
        payload = {key: value for key, value in row.items() if key != "id"}
        try:
            job = self.netem.traffic_generator_request("/api/v1/network/paths/" + row["id"], method="PUT", payload=payload, timeout=5)
        except RuntimeError as exc:
            raise ValueError("Path registration failed. Update the simulator for purpose-based paths, and verify the NIC addresses and router return routes.") from exc
        self.store.state("traffic_paths", {"paths": paths + [row]})
        message = self.wait_selection(job, row, selecting=False)
        self.netem.log_event("traffic-path", "Independent traffic path configured", traffic_path=row)
        return message

    def select_path(self, path_id):
        row = next((row for row in self.store.state("traffic_paths").get("paths", []) if row["id"] == path_id), None)
        if not row:
            raise ValueError("Choose a configured traffic path.")
        self.check_platform()
        self.ensure_idle()
        job = self.netem.traffic_generator_request("/api/v1/network/paths/select", method="POST", payload={"id": path_id}, timeout=5)
        return self.wait_selection(job, row)

    def remove_path(self, path_id):
        paths = self.store.state("traffic_paths").get("paths", [])
        row = next((row for row in paths if row["id"] == path_id), None)
        if not row:
            raise ValueError("Choose a configured path.")
        current = self.check_platform()
        self.ensure_idle()
        if (current.get("network", {}).get("selected") or {}).get("id") == path_id:
            raise ValueError("Choose another workload path before removing this one.")
        remote = self.netem.traffic_generator_request("/api/v1/network/paths", timeout=5)
        if any(path["id"] == path_id for path in remote.get("paths", [])):
            job = self.netem.traffic_generator_request("/api/v1/network/paths/" + path_id, method="DELETE", timeout=5)
            self.wait_selection(job, row, selecting=False)
        self.store.state("traffic_paths", {"paths": [path for path in paths if path["id"] != path_id]})
        self.netem.log_event("traffic-path", "Unused direct path removed", traffic_path=row)
        return "Unused path removed; workload/management routes remain unchanged."

    def preflight(self, form):
        if not self.store.rows() and not self.store.state("traffic_paths"):
            return
        platform = self.check_platform()
        pending = self.store.state("pending")
        job = platform.get("network", {}).get("job") or {}
        if pending and job.get("id") == pending.get("id") and job.get("state") in ("completed", "failed"):
            self.store.state("pending", {})
            pending = {}
        if pending or platform.get("network", {}).get("busy"):
            raise ValueError("Wait for appliance route verification to finish before starting a test.")
        selected = platform.get("network", {}).get("selected") or {}
        mode = selected.get("mode", self.store.state("selection").get("mode", "appliance"))
        if mode != "appliance":
            if not platform.get("path", {}).get("ready"):
                raise ValueError("The direct path is not verified. Check Platform Health before starting traffic.")
            if request.endpoint in ("site_run", "lab_scenario_start"):
                if mode == "direct_baseline":
                    raise ValueError("Clean baseline bypasses NetEm. Run the full workload here; choose a direct NetEm path to tune WAN scenarios.")
                if request.endpoint == "site_run":
                    raise ValueError("Site plans include appliance/multi-WAN behavior. Use generic/custom tests on the directly wired NetEm WAN.")
                if form.get("link_id") != selected.get("wan_id"):
                    raise ValueError("Run the scenario on the WAN assigned to this direct NetEm path.")
                scenario = next((item for item in self.netem.get_scenarios(self.netem.load_config()) if item["id"] == form.get("scenario_id")), {})
                if any((step.get("condition") or {}).get("type") == "steering" for step in scenario.get("steps", [])):
                    raise ValueError("Appliance steering assertions are not applicable on a direct-router path.")
            self.netem.log_event("traffic-path", f"{mode}: {selected.get('name')} workload/test preflight verified", traffic_path=selected,
                                 scope="generator/router/target" if mode == "direct_baseline" else "generator/NetEm/router/target")
            return
        row = self.selected()
        if row:
            row = self.check_appliance(row)
        checks = readiness(row)
        if checks["critical"]:
            reason = str(form.get("appliance_override_reason") or "").strip()
            if form.get("appliance_override") != "1" or not 5 <= len(reason) <= 300:
                raise ValueError("Appliance not ready: " + "; ".join(checks["critical"])
                                 + ". Review Appliances or provide an explicit override with a reason (5–300 characters).")
            self.netem.log_event("appliance-override", f"Readiness override for {row['name']}: {reason}",
                                 appliance_id=row["id"], critical=checks["critical"], endpoint=request.endpoint,
                                 observation=row["observation"])
        self.netem.log_event("appliance-readiness", "Appliance checked before test start", appliance_id=(row or {}).get("id"),
                             critical=checks["critical"], warnings=checks["warnings"], observation=(row or {}).get("observation"))
        if checks["warnings"]:
            flash("Appliance readiness warning: " + "; ".join(checks["warnings"]), "warning")

    def worker(self):
        next_vendor = {}
        stop = self.netem.BACKGROUND_STOP
        while not stop.is_set():
            now = time.monotonic()
            try:
                for row in self.store.rows():
                    key = (row["id"], row["revision"])
                    if time.monotonic() >= next_vendor.get(key, 0):
                        self.check_appliance(row)
                        next_vendor[key] = time.monotonic() + INTERVAL
                    if stop.is_set():
                        break
                live = {(row["id"], row["revision"]) for row in self.store.rows()}
                next_vendor = {key: due for key, due in next_vendor.items() if key in live}
            except Exception:
                # Keep the worker alive without logging vendor payloads or credentials.
                self.netem.log_event("platform-health", "Monitoring cycle failed; check service logs and inventory storage.")
            stop.wait(5)

    def platform_worker(self):
        stop = self.netem.BACKGROUND_STOP
        while not stop.is_set():
            try:
                self.check_platform()
            except Exception:
                self.netem.log_event("platform-health", "Platform monitoring failed; retained observations are last known.")
            stop.wait(30)

    def start(self):
        if self.thread is None or not self.thread.is_alive():
            self.thread = threading.Thread(target=self.worker, name="netem-appliance-monitor", daemon=True)
            self.thread.start()
        if self.platform_thread is None or not self.platform_thread.is_alive():
            self.platform_thread = threading.Thread(target=self.platform_worker, name="netem-platform-monitor", daemon=True)
            self.platform_thread.start()


def init_app(app, netem):
    platform = Platform(netem)
    app.extensions["appliance_platform"] = platform
    mutating = {"inventory_save", "inventory_archive", "inventory_check", "inventory_select", "inventory_import",
                "inventory_acknowledge", "platform_check", "platform_self_test", "platform_calibrate", "traffic_path_save", "traffic_path_select", "traffic_path_remove"}

    @app.before_request
    def protect_inventory():
        if request.method != "POST" or request.endpoint not in mutating | START_ENDPOINTS:
            return None
        # The older scenario forms now share the same protection as integrations.
        import hmac
        from flask import session
        supplied, expected = request.form.get("integration_csrf", ""), session.get("integration_csrf", "")
        require_token = request.endpoint in mutating or bool(platform.store.rows()) or bool(platform.store.state("traffic_paths"))
        if require_token and (not supplied or not expected or not hmac.compare_digest(supplied.encode(), expected.encode())):
            abort(400, "Invalid form token. Reload the page and retry.")
        platform.operations.acquire()
        g.inventory_lock = True
        if request.endpoint in START_ENDPOINTS:
            try:
                platform.preflight(request.form)
            except (ValueError, RuntimeError) as exc:
                flash(str(exc), "error")
                return redirect(url_for("tests"))
        return None

    @app.teardown_request
    def release_inventory(_error):
        if getattr(g, "inventory_lock", False):
            g.inventory_lock = False
            platform.operations.release()

    @app.context_processor
    def inventory_context():
        snapshot = platform.snapshot()
        return {"inventory_summary": {"selected": platform.selected(),
                "critical": sum(1 for item in snapshot["alerts"] if item["severity"] == "critical"),
                "warnings": sum(1 for item in snapshot["alerts"] if item["severity"] != "critical")},
                "inventory_enabled": bool(snapshot["appliances"])}

    @app.get("/appliances", endpoint="inventory")
    def inventory():
        snapshot = platform.snapshot()
        edit = platform.store.get(request.args.get("edit")) or {}
        detail = platform.store.get(request.args.get("detail")) or {}
        return render_template("appliances.html", page="inventory", inventory=snapshot, edit=edit, detail=detail,
                               history=platform.store.history(detail["id"]) if detail else [], vendors=netem.SDWAN_VENDORS)

    @app.get("/platform-health", endpoint="platform_health")
    def platform_health():
        return render_template("platform_health.html", page="platform_health", inventory=platform.snapshot(),
                               path_links=netem.load_config().get("wan_links", []), host=netem.host_headroom(),
                               self_test=netem.last_platform_self_test(), group_titles=netem.platform_test.GROUP_TITLES,
                               calibration=netem.last_bandwidth_calibration(), calibration_state=netem.CALIBRATION_STATE)

    @app.post("/platform-health/self-test", endpoint="platform_self_test")
    def platform_self_test():
        report = netem.run_platform_self_test()
        if report is None:
            flash("A platform self-test is already running.", "error")
        else:
            counts = report["counts"]
            flash(f"Platform self-test {report['status']}: {counts['fail']} failed, {counts['warn']} warnings.",
                  "error" if report["status"] == "fail" else "success")
        return redirect(url_for("platform_health"))

    @app.get("/platform-health/self-test/<kind>", endpoint="platform_self_test_download")
    def platform_self_test_download(kind):
        saved = netem.platform_self_test_file(kind)
        if saved is None:
            abort(404)
        body, media, name = saved
        return Response(body, mimetype=media, headers={"Content-Disposition": f"attachment; filename={name}"})

    @app.post("/platform-health/calibrate", endpoint="platform_calibrate")
    def platform_calibrate():
        link_id = request.form.get("link_id") or ""
        known = {str(item.get("id") or item.get("bridge")) for item in netem.load_config().get("wan_links", [])}
        if link_id not in known:
            flash("Choose a configured WAN to calibrate.", "error")
        elif not netem.start_bandwidth_calibration(link_id, unshaped=request.form.get("unshaped") == "1"):
            flash("A bandwidth calibration is already running.", "error")
        else:
            flash("Bandwidth calibration started. It takes several minutes; reload this page for the result.", "success")
        return redirect(url_for("platform_health"))

    @app.get("/platform-health/calibration/<kind>", endpoint="calibration_download")
    def calibration_download(kind):
        saved = netem.calibration_file(kind)
        if saved is None:
            abort(404)
        body, media, name = saved
        return Response(body, mimetype=media, headers={"Content-Disposition": f"attachment; filename={name}"})

    @app.get("/api/v1/appliances", endpoint="api_inventory")
    def api_inventory():
        response = jsonify(platform.snapshot())
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/v1/appliances/<appliance_id>/history", endpoint="api_inventory_history")
    def api_inventory_history(appliance_id):
        if not platform.store.get(appliance_id):
            abort(404)
        return jsonify({"observations": platform.store.history(appliance_id)})

    def result(action, destination="inventory"):
        try:
            message = action()
            flash(message, "success")
        except (ValueError, RuntimeError) as exc:
            flash(str(exc), "error")
        return redirect(url_for(destination))

    @app.post("/appliances/save", endpoint="inventory_save")
    def inventory_save():
        def save():
            appliance_id = request.form.get("id") or None
            old = platform.store.get(appliance_id) if appliance_id else None
            if old and old["id"] == platform.store.state("selection").get("id"):
                if netem.scenario_snapshot().get("active") or netem.site_plan_snapshot().get("active"):
                    raise ValueError("Finish the test before editing the selected appliance.")
                platform.ensure_idle()
            definition = validate(request.form, netem.SDWAN_VENDORS)
            if old:
                definition["simulator_id"] = old.get("simulator_id")
            # Duplicate traffic paths would make selection and evidence ambiguous.
            if definition["gateway"] and any(row["id"] != appliance_id and platform.route_matches(row, definition) for row in platform.store.rows()):
                raise ValueError("This traffic path already belongs to another inventory appliance.")
            saved_id = platform.store.save(definition, appliance_id, request.form.get("api_token") or None,
                                            request.form.get("clear_token") == "1")
            netem.log_event("appliance-inventory", "Appliance saved", appliance_id=saved_id, name=definition["name"])
            return "Appliance saved. Monitoring starts automatically; selecting it verifies the traffic path."
        return result(save)

    @app.post("/appliances/archive", endpoint="inventory_archive")
    def inventory_archive():
        def archive():
            row = platform.store.get(request.form.get("id"))
            if not row:
                raise ValueError("Appliance no longer exists.")
            if row["id"] in (platform.store.state("selection").get("id"), platform.store.state("pending").get("appliance_id")):
                raise ValueError("Select another appliance before archiving the active or pending appliance.")
            platform.store.archive(row["id"])
            netem.log_event("appliance-inventory", "Appliance archived; history retained", appliance_id=row["id"])
            return "Appliance archived. Observations retained; its stored credential was removed."
        return result(archive)

    @app.post("/appliances/check", endpoint="inventory_check")
    def inventory_check():
        def check():
            row = platform.store.get(request.form.get("id"))
            if not row:
                raise ValueError("Appliance no longer exists.")
            platform.check_appliance(row)
            return "Appliance check completed. Review the separate health and license results."
        return result(check)

    @app.post("/appliances/select", endpoint="inventory_select")
    def inventory_select():
        return result(lambda: platform.select(request.form.get("id")))

    @app.post("/appliances/import", endpoint="inventory_import")
    def inventory_import():
        def import_rows():
            snapshot = platform.check_platform()
            if not snapshot["network"]:
                raise ValueError("Cannot read simulator inventory. Configure its connection in Integrations.")
            count = 0
            for row in snapshot["network"].get("appliances") or []:
                if any(platform.route_matches(existing, row) for existing in platform.store.rows()):
                    continue
                definition = validate(dict(row, vendor=netem.vendor_id(row.get("vendor")) or "other",
                                           kind="vm", hypervisor="Proxmox", required_licenses="vm"), netem.SDWAN_VENDORS)
                definition["simulator_id"] = row["id"]
                platform.store.save(definition)
                count += 1
            platform.check_platform()
            return f"Imported {count} saved appliances. Add management API credentials to enable vendor monitoring."
        return result(import_rows)

    @app.post("/appliances/alerts/acknowledge", endpoint="inventory_acknowledge")
    def inventory_acknowledge():
        platform.store.acknowledge(request.form.get("key", ""))
        return redirect(url_for("inventory"))

    @app.post("/platform-health/check", endpoint="platform_check")
    def platform_check():
        return result(lambda: (platform.check_platform() and "Platform check completed."), "platform_health")

    @app.post("/traffic-paths/save", endpoint="traffic_path_save")
    def traffic_path_save():
        return result(lambda: platform.configure_path(request.form), "platform_health")

    @app.post("/traffic-paths/select", endpoint="traffic_path_select")
    def traffic_path_select():
        return result(lambda: platform.select_path(request.form.get("id")), "platform_health")

    @app.post("/traffic-paths/remove", endpoint="traffic_path_remove")
    def traffic_path_remove():
        return result(lambda: platform.remove_path(request.form.get("id")), "platform_health")

    return platform
