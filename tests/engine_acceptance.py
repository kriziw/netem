"""Cross-repository acceptance in disposable namespaces; never touches host routing.

Run as root with SIMULATOR_SOURCE pointing to the companion checkout. Results
describe this kernel/runner, not a deployed appliance or a capacity certificate.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
SIMULATOR = Path(os.environ["SIMULATOR_SOURCE"]).resolve()
sys.path[:0] = [str(ROOT), str(SIMULATOR)]


def command(*args):
    result = subprocess.run(list(args), capture_output=True, text=True)
    if result.returncode:
        print(result.stdout, result.stderr, file=sys.stderr)
        result.check_returncode()
    return result.stdout


def client(server_namespace, output):
    # Import Locust first, before networking libraries, to initialize gevent safely.
    from trafficgen.engine import CorporateUser, WorkloadController
    import gevent
    from locust.env import Environment
    from trafficgen.config import load_settings
    from trafficgen.database import init_db
    from trafficgen.packet_metrics import percentile
    from trafficgen.dem import summarize_rows
    import app as platform

    results = {"kernel": command("uname", "-r").strip(), "packet_cases": [], "load_cases": [],
               "scope": "isolated synthetic paths; no vendor appliance", "appliance_acceptance": "not available"}
    original_tc = platform.TC

    def shape(wan, delay=0, jitter=0, loss=0, down=100, up=100):
        platform.TC = original_tc
        ok, message = platform.apply_netem("c" + str(wan), delay, jitter, loss, up)
        assert ok, message
        platform.TC = f"ip netns exec {server_namespace} {original_tc}"
        try:
            ok, message = platform.apply_netem("s" + str(wan), 0, 0, 0, down)
            assert ok, message
        finally:
            platform.TC = original_tc

    def route(wan):
        command("ip", "route", "replace", "198.18.0.2/32", "via", f"198.18.{wan}.2", "dev", f"c{wan}")

    environment = Environment()
    events = []
    environment.events.request.add_listener(lambda **event: events.append(event))
    config = dict(run_id="namespace", personas={"knowledge_worker": 1}, applications={"voice": 1},
                  target="http://198.18.0.2:8090", udp_port=9000,
                  policy={"voice": {"deadline_ms": 50}})
    user_class = type("PacketUser", (CorporateUser,), {"abstract": False, "host": config["target"], "runtime_config": config})
    user = user_class(environment)
    user.on_start()
    for wan in (1, 2):
        route(wan)
        for delay, jitter, loss in ((0, 0, 0), (5, 0, 0), (40, 5, .1), (100, 5, 1), (350, 0, 3), (0, 0, 100)):
            shape(wan, delay, jitter, loss)
            for size in (160, 1200):
                events.clear()
                user._udp_burst("voice", size, 500, .002, full_echo=True)
                event, evidence = events[0], events[0]["context"]
                received = evidence["packets_sent"] - evidence["packets_lost"]
                p50 = percentile(evidence["packet_rtt_histogram"], .50)
                if loss == 100:
                    assert received == 0 and event["exception"] is not None
                else:
                    assert received >= 400, (wan, delay, loss, received)
                    assert p50 is not None and p50 >= delay * .8, (delay, p50)
                    if not loss:
                        assert received == 500, (wan, delay, received)
                results["packet_cases"].append(dict(wan=wan, delay_ms=delay, jitter_ms=jitter,
                    loss_pct=loss, payload_bytes=size, received=received, late=evidence["packets_late"],
                    p50_upper_ms=p50, bytes_down=event["response_length"], bytes_up=evidence["bytes_up"]))
    Path(output).write_text(json.dumps(results, indent=2, default=str))
    # A healthy second WAN stays usable while the first is blackholed. This is
    # explicit routing, not an assertion that a vendor appliance chose to switch.
    shape(1, loss=100)
    shape(2)
    route(2)
    user._udp_burst("voice", 160, 10, .01, full_echo=True)
    assert events[-1]["exception"] is None

    # Sustained sessions emit successive one-second windows for all media types.
    user.runtime_config.update(media_pattern="sustained", media_session_s=2)
    results["sustained_sessions"] = []
    for application, size, packets, interval, camera in (("voice", 160, 50, .02, False),
            ("video", 1200, 200, .005, False), ("cctv_backhaul", 1200, 200, .005, True)):
        events.clear()
        started = time.monotonic()
        user._media_action(application, size, packets, interval, camera=camera)
        assert len(events) == 2
        assert sum(event["context"]["packets_sent"] for event in events) == packets * 2
        assert all(event["exception"] is None for event in events)
        results["sustained_sessions"].append(dict(application=application, windows=len(events),
            duration_s=time.monotonic() - started, packets=packets * 2))
    Path(output).write_text(json.dumps(results, indent=2, default=str))

    with tempfile.TemporaryDirectory() as directory:
        os.environ.update(TRAFFICGEN_DATABASE=directory + "/engine.db", TRAFFICGEN_RUNTIME_DIR=directory,
                          TRAFFICGEN_CONFIG_DIR=directory, TRAFFICGEN_DEFAULT_TARGET=config["target"],
                          PYTHONPATH=os.pathsep.join([str(SIMULATOR), str(ROOT)]))
        os.environ.pop("TRAFFICGEN_DEM_WORKER", None)
        settings = load_settings()
        init_db(settings.database_path)
        controller = WorkloadController(settings)
        try:
            for observers in (False, True):
                for users, rate in ((1, 5), (16, 100)):
                    controller.start(dict(users=users, spawn_rate=users, rate_down_mbps=rate,
                                          rate_up_mbps=rate / 2, completion_grace_s=1, seed=42))
                    gevent.sleep(2)
                    before = controller.rate_scheduler.snapshot()
                    cpu = time.process_time()
                    started = time.monotonic()
                    def observe():
                        while True:
                            controller.status()
                            controller.query_dem(run_id=controller.run["run_id"], window_seconds=30)
                            gevent.sleep(.1)
                    observer = gevent.spawn(observe) if observers else None
                    try:
                        gevent.sleep(6)
                        controller.adjust({"users": users * 2})
                        gevent.sleep(6)
                    finally:
                        if observer:
                            observer.kill()
                    elapsed = time.monotonic() - started
                    after = controller.rate_scheduler.snapshot()
                    directions = {}
                    for direction in ("down", "up"):
                        a, b = before["directions"][direction], after["directions"][direction]
                        directions[direction] = {key: (b[key] - a[key]) * 8 / elapsed / 1e6
                            for key in ("offered_bytes", "admitted_bytes", "started_bytes", "delivered_bytes")}
                        tolerance = b["target_mbps"] * .05 + after["chunk_bytes"] * 8 / elapsed / 1e6
                        assert abs(directions[direction]["offered_bytes"] - b["target_mbps"]) <= tolerance
                        if rate == 5:
                            assert directions[direction]["admitted_bytes"] >= directions[direction]["offered_bytes"] - tolerance, (direction, directions)
                        assert directions[direction]["offered_bytes"] > 0
                        assert directions[direction]["delivered_bytes"] > 0, (direction, before, after, controller.telemetry())
                        assert b["target_mbps"] == a["target_mbps"]
                    results["load_cases"].append(dict(observers=observers, users=users, rate_mbps=rate,
                        duration_s=elapsed, generator_cpu_seconds=time.process_time() - cpu,
                        directions_mbps=directions, health={"persistence_error": controller.persistence_error, "dropped_transactions": controller.dropped_transactions}))
                    controller.stop()
        finally:
            controller.shutdown()

    partial = [dict(application="web_saas", success=True, response_time_ms=297, response_length=100)] * 108
    partial += [dict(application="voice", success=True, response_time_ms=100, response_length=100)] * 4
    partial += [dict(application="voice", success=False, response_time_ms=0, response_length=0)]
    summary = summarize_rows(partial, application_shares={"web_saas": .95, "voice": .05})
    outcome = platform.showroom_outcome({"traffic_generator": {"connected": True, "status": {"dem": summary}}}, None)
    assert outcome["experience"]["experience_score"] > 0
    assert outcome["experience"]["critical_alerts"]
    results["showcase_partial_failure"] = outcome["experience"]
    Path(output).write_text(json.dumps(results, indent=2, default=str))
    print("PASS cross-repository engine, real packets, independent WANs, population resizing and observer comparison")


def main():
    assert sys.platform == "linux" and os.geteuid() == 0
    marker = uuid.uuid4().hex[:8]
    left, right = "eng-c-" + marker, "eng-t-" + marker
    namespaces, target = [], None
    try:
        for namespace in (left, right):
            command("ip", "netns", "add", namespace)
            namespaces.append(namespace)
            command("ip", "-n", namespace, "link", "set", "lo", "up")
        command("ip", "-n", right, "address", "add", "198.18.0.2/32", "dev", "lo")
        for wan in (1, 2):
            a, b = "ec" + marker + str(wan), "et" + marker + str(wan)
            command("ip", "link", "add", a, "type", "veth", "peer", "name", b)
            for namespace, old, name, host in ((left, a, f"c{wan}", 1), (right, b, f"s{wan}", 2)):
                command("ip", "link", "set", old, "netns", namespace)
                command("ip", "-n", namespace, "link", "set", old, "name", name)
                command("ip", "-n", namespace, "address", "add", f"198.18.{wan}.{host}/30", "dev", name)
                command("ip", "-n", namespace, "link", "set", name, "up")
        env = dict(os.environ, PYTHONPATH=str(SIMULATOR))
        target = subprocess.Popen(["ip", "netns", "exec", right, sys.executable, "-m", "trafficgen.target"], env=env)
        command("ip", "-n", left, "route", "add", "198.18.0.2/32", "via", "198.18.1.2")
        for _ in range(50):
            probe = subprocess.run(["ip", "netns", "exec", left, "curl", "--noproxy", "*", "-fsS", "--max-time", "1",
                                    "http://198.18.0.2:8090/health"], capture_output=True)
            if probe.returncode == 0:
                break
            time.sleep(.1)
        else:
            raise AssertionError("Target did not become ready")
        command("ip", "netns", "exec", left, sys.executable, str(Path(__file__).resolve()),
                "--client", right, str(ROOT / "engine-acceptance-results.json"))
    finally:
        if target:
            target.terminate()
            target.wait(timeout=10)
        for namespace in reversed(namespaces):
            command("ip", "netns", "delete", namespace)


if __name__ == "__main__":
    if "--client" in sys.argv:
        client(sys.argv[2], sys.argv[3])
    else:
        main()
