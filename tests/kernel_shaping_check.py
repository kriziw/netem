"""Exercise real tc only in a disposable Linux network namespace (run as root)."""
import os
import json
import socket
import time
from pathlib import Path
import subprocess
import sys
import uuid


def check_inside():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import app
    assert app.apply_netem("engine-test", 5, 1, .1, 100)[0]
    output = subprocess.check_output(["tc", "qdisc", "show", "dev", "engine-test"], text=True)
    assert all(kind in output for kind in ("netem", "tbf", "fq_codel")), output
    assert "100Mbit" in output and "delay 5ms" in output, output
    structured = json.loads(subprocess.check_output(["tc", "-j", "qdisc", "show", "dev", "engine-test"], text=True))
    fair = next(row for row in structured if row['kind'] == 'fq_codel')['options']
    assert fair['limit'] == 65536 and fair['memory_limit'] == 67108864, structured
    # A verified no-op must not call the destructive application function.
    original = app._apply_netem
    def forbidden(*args):
        raise AssertionError("Identical profile rebuilt queues")
    app._apply_netem = forbidden
    try:
        assert app.apply_netem("engine-test", 5, 1, .1, 100)[0]
    finally:
        app._apply_netem = original
    assert not app.apply_netem("engine-test", 0, 30, 0, 100)[0]
    after = subprocess.check_output(["tc", "qdisc", "show", "dev", "engine-test"], text=True)
    assert after == output
    original_clear = app.clear_qdisc
    app.clear_qdisc = lambda *_: (_ for _ in ()).throw(AssertionError("Transition rebuilt the hierarchy"))
    try:
        assert app.apply_netem("engine-test", 40, 5, 3, 18)[0]
        assert app.apply_netem("engine-test", 0, 0, 0, 100)[0]
        cleared = subprocess.check_output(["tc", "qdisc", "show", "dev", "engine-test"], text=True)
        assert "delay" not in cleared and "loss" not in cleared, cleared
        assert app.apply_netem("engine-test", 40, 5, 3, 18)[0]
    finally:
        app.clear_qdisc = original_clear
    output = subprocess.check_output(["tc", "qdisc", "show", "dev", "engine-test"], text=True)
    assert "18Mbit" in output and "delay 40ms" in output, output
    print("PASS: real Linux shaping, verified no-op and validation-before-mutation")
    check_queue_bursts(app)


def check_queue_bursts(app):
    # Both veths live only in this disposable namespace. Raw Ethernet crosses the
    # egress qdiscs without routing shortcuts, socket pacing, or a production target.
    subprocess.run(['ip', 'link', 'add', 'queue-source', 'type', 'veth', 'peer', 'name', 'queue-sink'], check=True)
    for name in ('queue-source', 'queue-sink'):
        subprocess.run(['ip', 'link', 'set', name, 'up'], check=True)
    results = []
    for rate in (18, 42):
        for size in (160, 1500):
            for budget in ('previous', 'generous'):
                app.clear_qdisc('queue-source')
                assert app.apply_netem('queue-source', 0, 0, 0, rate)[0]
                if budget == 'previous':
                    memory = max(65536, int(rate * 125 * 25))
                    subprocess.run(['tc', 'qdisc', 'change', 'dev', 'queue-source', 'parent', '10:1',
                                    'handle', '20:', 'fq_codel', 'limit', '65536', 'memory_limit', str(memory),
                                    'target', '5ms', 'interval', '100ms'], check=True)
                frame = bytes.fromhex('02000000000202000000000188b5') + bytes(size - 14)
                began = time.monotonic()
                with socket.socket(socket.AF_PACKET, socket.SOCK_RAW) as sender:
                    sender.bind(('queue-source', 0))
                    for _ in range(512):
                        sender.send(frame)
                time.sleep(1)
                counters = app.qdisc_counters('queue-source')
                fair = counters['queues']['fair_queue']
                result = {'rate_mbps': rate, 'packet_bytes': size, 'budget': budget,
                          'average_offered_mbps': round(512 * size * 8 / (time.monotonic() - began) / 1e6, 3),
                          'drops': fair['drops'], 'drop_overmemory': fair.get('drop_overmemory'),
                          'drop_overlimit': fair.get('drop_overlimit')}
                results.append(result)
                assert fair.get('drop_overmemory') is not None, fair
                if budget == 'generous':
                    assert fair['drop_overmemory'] == 0 and fair['drop_overlimit'] == 0, result
    print('QUEUE_BUDGET_RESULTS ' + json.dumps(results))
    print('PASS: generous resource guards avoid memory/packet-limit drops for low-average burst traffic')


if __name__ == "__main__":
    if "--inside" in sys.argv:
        check_inside()
    else:
        assert sys.platform == "linux" and os.geteuid() == 0
        namespace = "netem-engine-" + uuid.uuid4().hex[:10]
        subprocess.run(["ip", "netns", "add", namespace], check=True)
        try:
            subprocess.run(["ip", "-n", namespace, "link", "add", "engine-test", "type", "dummy"], check=True)
            subprocess.run(["ip", "-n", namespace, "link", "set", "engine-test", "up"], check=True)
            subprocess.run(["ip", "netns", "exec", namespace, sys.executable, str(Path(__file__).resolve()), "--inside"], check=True)
        finally:
            subprocess.run(["ip", "netns", "delete", namespace], check=True)
