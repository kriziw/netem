"""Exercise real tc only in a disposable Linux network namespace (run as root)."""
import os
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
