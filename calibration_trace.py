"""What the Traffic Simulator was doing during each second of a calibration step, read beside the WAN.

The simulator reports how late its event loop woke, its cumulative byte and request counters, how many paced requests
it has started and completed, how long its status, DEM and database writes took, and how long the controlled target
took to answer. NetEm polls its health endpoint once a second for the length of the measured window, so each second of
the WAN timeline can be read next to what the generator was doing. This module only joins and summarises those
readings; app.py does the polling and bottlenecks.py judges the result.
"""

import threading
import time

POLL_INTERVAL_S = 1.0
POLL_TIMEOUT_S = 2.0      # a poll still unanswered after this long counts as no answer
QUIET_MBPS = 0.1          # a second carrying less than this on the WAN carried no traffic
LOOP_STALL_MS = 100.0     # the simulator's loop held this long is treated as a stall for the quiet-second split
TARGET_SLOW_MS = 100.0    # the target answering its health check this slowly is treated as slow


def _samples(items, allow_none=False):
    """[unix time, value] pairs from a simulator reply. Anything malformed is dropped rather than trusted."""
    clean = []
    for item in items or []:
        if not isinstance(item, (list, tuple)) or len(item) != 2 or not isinstance(item[0], (int, float)):
            continue
        stamp, value = float(item[0]), item[1]
        if isinstance(value, (int, float)):
            clean.append([stamp, float(value)])
        elif value is None and allow_none:
            clean.append([stamp, None])
    return clean


def poll_record(sent, received, reply=None, error=None):
    """One poll of the simulator: when it was asked, how long it took, and what it reported. The offset is the
    simulator's clock minus this host's clock at the middle of the poll, so its timestamps can be moved onto this host."""
    reply = reply if isinstance(reply, dict) else {}
    counters = reply.get("counters") if isinstance(reply.get("counters"), dict) else None
    loop = reply.get("loop") if isinstance(reply.get("loop"), dict) else {}
    probe = reply.get("target_probe") if isinstance(reply.get("target_probe"), dict) else {}
    middle = (sent + received) / 2.0
    server_time = reply.get("time")
    return {
        "sent": round(sent, 3),
        "mid": round(middle, 3),
        "latency_ms": round((received - sent) * 1000.0, 1),
        "error": error,
        "offset_s": round(server_time - middle, 3) if isinstance(server_time, (int, float)) else None,
        "counters": counters,
        "loop": _samples(loop.get("samples")),
        "target": _samples(probe.get("samples"), allow_none=True),
    }


class SimulatorPoller:
    """Polls the simulator's health endpoint on its own thread until stopped. A failed poll is recorded with its error
    and how long it took, never raised: the calibration reads the record afterwards."""

    def __init__(self, fetch, interval_s=POLL_INTERVAL_S, clock=time.time):
        self._fetch = fetch
        self._interval_s = interval_s
        self._clock = clock
        self._polls = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="netem-calibration-poll", daemon=True)

    def start(self):
        self._thread.start()
        return self

    def _run(self):
        while True:
            sent = self._clock()
            try:
                reply = self._fetch()
                self._polls.append(poll_record(sent, self._clock(), reply))
            except Exception as exc:  # recorded, so one bad poll cannot end the trace
                self._polls.append(poll_record(sent, self._clock(), error=str(exc)[:200] or type(exc).__name__))
            if self._stop.wait(self._interval_s):
                return

    def stop(self):
        """Stops polling and returns every poll made. Waits at most one poll timeout for a poll already in flight."""
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=POLL_TIMEOUT_S + 1.0)
        return list(self._polls)


def rate_intervals(polls):
    """Simulator throughput and request rate between consecutive polls. A counter that went backwards means the
    simulator restarted: that interval is dropped and counted rather than shown as a negative rate."""
    usable = [poll for poll in polls if not poll["error"] and poll["counters"]]
    intervals, restarts = [], 0
    for earlier, later in zip(usable, usable[1:]):
        before, after = earlier["counters"], later["counters"]
        if any(before.get(key) is None or after.get(key) is None for key in ("bytes", "requests")):
            continue
        seconds = later["mid"] - earlier["mid"]
        if seconds <= 0:
            continue
        if after["bytes"] < before["bytes"] or after["requests"] < before["requests"]:
            restarts += 1
            continue
        intervals.append({"mid": (earlier["mid"] + later["mid"]) / 2.0,
                          "mbps": round((after["bytes"] - before["bytes"]) * 8 / seconds / 1_000_000, 2),
                          "requests_ps": round((after["requests"] - before["requests"]) / seconds, 2)})
    return intervals, restarts


def in_flight(counters):
    """Paced requests the simulator has started and not yet completed, or None when it does not report them."""
    if not counters or counters.get("paced_started") is None or counters.get("paced_completed") is None:
        return None
    return counters["paced_started"] - counters["paced_completed"]


def _local(polls, key):
    """One kind of reading (loop lateness or target latency) in this host's clock, each reading kept once."""
    seen = {}
    for poll in polls:
        offset = poll["offset_s"] or 0.0
        for stamp, value in poll[key]:
            seen[round(stamp, 3)] = (stamp - offset, value)
    return sorted(seen.values())


def seconds_table(wan_rows, start, polls, loop, target, intervals):
    """One row per second of the WAN timeline: what the WAN carried beside what the simulator sent, how late its loop
    ran, how long its polls and the target took, and how many paced requests were waiting on the WAN."""
    rows = []
    for wan in wan_rows:
        high = float(wan["timestamp"])
        low = high - 1.0

        def within(stamp):
            return low < stamp <= high

        rate = next((item for item in intervals if within(item["mid"])), None)
        lags = [value for stamp, value in loop if within(stamp)]
        probe = next((poll for poll in polls if within(poll["mid"])), None)
        answers = [value for stamp, value in target if within(stamp)]
        wan_down = wan.get("down_mbps")
        backlog = wan.get("down_backlog_bytes")
        rows.append({
            "offset_s": round(high - start, 1),
            "wan_down_mbps": wan_down,
            "wan_up_mbps": wan.get("up_mbps"),
            "wan_backlog_kb": None if backlog is None else round(backlog / 1024.0, 1),
            "wan_drops_ps": wan.get("down_queue_drops_ps"),
            "sim_mbps": rate["mbps"] if rate else None,
            "sim_requests_ps": rate["requests_ps"] if rate else None,
            "in_flight": in_flight(probe["counters"]) if probe else None,
            "loop_max_ms": max(lags) if lags else None,
            "poll_ms": probe["latency_ms"] if probe else None,
            "poll_error": probe["error"] if probe else None,
            "target_ms": max((value for value in answers if value is not None), default=None),
            "target_no_answer": sum(1 for value in answers if value is None),
            "quiet": wan_down is not None and wan_down < QUIET_MBPS,
        })
    return rows


def _quiet_cause(row):
    """Why a quiet second carried nothing, in the order the evidence is judged: the simulator's loop, then the target,
    then requests waiting on the WAN, and last a generator that was not asking for traffic."""
    if (row["loop_max_ms"] or 0.0) >= LOOP_STALL_MS:
        return "loop"
    if row["target_no_answer"] or (row["target_ms"] or 0.0) >= TARGET_SLOW_MS:
        return "target"
    if (row["in_flight"] or 0) > 0:
        return "waiting"
    return "idle"


def summarise(rows, polls, intervals, restarts, start, end):
    """The step's simulator figures: throughput and rates over the window, the worst stalls and delays, and why each
    quiet second was quiet."""
    inside = [poll for poll in polls if start <= poll["mid"] <= end]
    answered = [poll for poll in inside if not poll["error"]]
    usable = [poll for poll in answered if poll["counters"] and poll["counters"].get("bytes") is not None]
    span = usable[-1]["mid"] - usable[0]["mid"] if len(usable) >= 2 else 0.0
    mbps = rps = None
    if span > 0:
        first, last = usable[0]["counters"], usable[-1]["counters"]
        mbps = round((last["bytes"] - first["bytes"]) * 8 / span / 1_000_000, 2)
        rps = round((last["requests"] - first["requests"]) / span, 2)
    lags = [row["loop_max_ms"] for row in rows if row["loop_max_ms"] is not None]
    targets = [row["target_ms"] for row in rows if row["target_ms"] is not None]
    quiet = [row for row in rows if row["quiet"]]
    causes = {"loop": 0, "target": 0, "waiting": 0, "idle": 0}
    for row in quiet:
        causes[_quiet_cause(row)] += 1

    def peak(name):
        values = [poll["counters"][name] for poll in answered
                  if poll["counters"] and poll["counters"].get(name) is not None]
        return max(values) if values else None

    flights = [row["in_flight"] for row in rows if row["in_flight"] is not None]
    return {
        "polls": len(inside),
        "poll_errors": sum(1 for poll in inside if poll["error"]),
        "poll_max_ms": max((poll["latency_ms"] for poll in answered), default=None),
        "sim_mbps_mean": mbps,
        "sim_requests_ps_mean": rps,
        "in_flight_max": max(flights) if flights else None,
        "loop_max_ms": max(lags) if lags else None,
        "loop_stall_seconds": sum(1 for lag in lags if lag >= LOOP_STALL_MS),
        "status_ms_max": peak("status_ms"),
        "dem_ms_max": peak("dem_ms"),
        "write_ms_max": peak("write_ms_max"),
        "target_max_ms": max(targets) if targets else None,
        "target_no_answer_seconds": sum(1 for row in rows if row["target_no_answer"]),
        "quiet_seconds": len(quiet),
        "quiet_loop_seconds": causes["loop"],
        "quiet_target_seconds": causes["target"],
        "quiet_waiting_seconds": causes["waiting"],
        "quiet_idle_seconds": causes["idle"],
        "quiet_offsets": [row["offset_s"] for row in quiet][:12],
        "restarts": restarts,
    }


def build(polls, wan_rows, start, end):
    """The per-second trace of one step and its summary. The polls are whatever SimulatorPoller recorded."""
    loop = _local(polls, "loop")
    target = _local(polls, "target")
    intervals, restarts = rate_intervals(polls)
    rows = seconds_table(wan_rows, start, polls, loop, target, intervals)
    return {
        "summary": summarise(rows, polls, intervals, restarts, start, end),
        "seconds": rows,
        "polls": [{"sent": poll["sent"], "latency_ms": poll["latency_ms"], "error": poll["error"],
                   "offset_s": poll["offset_s"], "counters": poll["counters"]} for poll in polls],
        "loop": [[round(stamp, 3), value] for stamp, value in loop if start - 1 <= stamp <= end + 1],
        "target": [[round(stamp, 3), value] for stamp, value in target if start - 1 <= stamp <= end + 1],
    }
