window.NetEmUI = (() => {
  function formatRate(mbps) {
    const value = Number(mbps);
    if (!Number.isFinite(value)) return "—";
    if (value <= 0) return "0 bit/s";
    if (value >= 1000) return (value / 1000).toFixed(value >= 10000 ? 1 : 2) + " Gbit/s";
    if (value >= 1) return value.toFixed(value >= 100 ? 0 : value >= 10 ? 1 : 2) + " Mbit/s";

    const kbit = value * 1000;
    if (kbit >= 1) return kbit.toFixed(kbit >= 100 ? 0 : kbit >= 10 ? 1 : 2) + " Kbit/s";

    const bit = value * 1000000;
    return bit.toFixed(bit >= 100 ? 0 : bit >= 10 ? 1 : 2) + " bit/s";
  }

  function formatPps(pps) {
    const value = Number(pps);
    if (!Number.isFinite(value)) return "—";
    if (value <= 0) return "0 pps";
    if (value >= 1000000) return (value / 1000000).toFixed(value >= 10000000 ? 1 : 2) + " Mpps";
    if (value >= 1000) return (value / 1000).toFixed(value >= 100000 ? 0 : value >= 10000 ? 1 : 2) + " kpps";
    return value.toFixed(value >= 100 ? 0 : value >= 10 ? 1 : 2) + " pps";
  }

  function formatAge(timestamp, nowSeconds = Date.now() / 1000) {
    if (!timestamp) return "No traffic seen";
    const age = Math.max(0, Number(nowSeconds) - Number(timestamp));
    if (age < 1.5) return "<1s ago";
    if (age < 60) return Math.round(age) + "s ago";
    if (age < 3600) return Math.round(age / 60) + "m ago";
    return Math.round(age / 3600) + "h ago";
  }

  function formatNumber(value, digits = 1) {
    const n = Number(value);
    if (!Number.isFinite(n)) return "—";
    return n.toFixed(digits);
  }

  function statusClass(link) {
    if (!link) return "info";
    if (link.fault && link.fault !== "normal") return "bad";
    if (!link.sla || !link.sla.pass) return "bad";
    const q = Number(link.quality ?? 100);
    if (q < 50) return "bad";
    if (q < 75) return "warn";
    return "good";
  }

  function pathFor(values, width = 100, height = 100, fixedMax = null) {
    const clean = values.map(v => Number(v)).filter(Number.isFinite);
    if (!clean.length) return "";
    const max = fixedMax || Math.max(...clean, 1);
    const min = fixedMax ? 0 : Math.min(...clean, 0);
    const span = Math.max(.0001, max - min);
    return values.map((raw, index) => {
      const value = Number(raw);
      const x = values.length === 1 ? 0 : index * width / (values.length - 1);
      const y = height - ((value - min) / span) * (height - 8) - 4;
      return (index ? "L" : "M") + x.toFixed(2) + "," + y.toFixed(2);
    }).join(" ");
  }

  function setPath(element, values, fixedMax = null) {
    if (!element) return;
    element.setAttribute("d", pathFor(values, 100, 100, fixedMax));
  }

  function niceMax(value) {
    const n = Number(value);
    if (!Number.isFinite(n) || n <= 0) return 1;
    const magnitude = Math.pow(10, Math.floor(Math.log10(n)));
    const normalized = n / magnitude;
    const nice = normalized <= 1 ? 1 : normalized <= 2 ? 2 : normalized <= 5 ? 5 : 10;
    return nice * magnitude;
  }

  function renderSeries(paths, series, options = {}) {
    const arrays = series.filter(Array.isArray);
    const flat = arrays.flat().map(Number).filter(Number.isFinite);
    const max = options.fixedMax != null
      ? Number(options.fixedMax)
      : niceMax(Math.max(...flat, 0));
    const min = options.fixedMin != null ? Number(options.fixedMin) : 0;
    const span = Math.max(.0001, max - min);

    arrays.forEach((values, index) => {
      const element = paths[index];
      if (!element) return;
      const path = values.map((raw, pointIndex) => {
        const value = Number(raw);
        const x = values.length === 1 ? 0 : pointIndex * 100 / (values.length - 1);
        const y = 96 - ((value - min) / span) * 92;
        return (pointIndex ? "L" : "M") + x.toFixed(2) + "," + y.toFixed(2);
      }).join(" ");
      element.setAttribute("d", path);
    });

    if (options.axis) {
      const unit = options.unit || "";
      const digits = options.digits ?? (max < 10 ? 1 : 0);
      const values = [max, max * .75, max * .5, max * .25, min];
      options.axis.innerHTML = values.map(value => {
        if (options.rateAxis) return '<span>' + formatRate(value) + '</span>';
        return '<span>' + Number(value).toFixed(digits) + unit + '</span>';
      }).join("");
    }

    if (options.timeAxis && Array.isArray(options.timestamps) && options.timestamps.length) {
      const first = Number(options.timestamps[0]);
      const last = Number(options.timestamps[options.timestamps.length - 1]);
      const spanSec = Math.max(0, last - first);
      const left = spanSec >= 172800
        ? "−" + (spanSec / 86400).toFixed(spanSec >= 864000 ? 0 : 1) + "d"
        : spanSec >= 7200
          ? "−" + (spanSec / 3600).toFixed(spanSec >= 36000 ? 0 : 1) + "h"
          : spanSec >= 120
            ? "−" + Math.round(spanSec / 60) + "m"
            : spanSec >= 10
              ? "−" + Math.round(spanSec) + "s"
              : "Start";
      options.timeAxis.innerHTML = '<span>' + left + '</span><span>Now</span>';
    }

    return {min, max};
  }

  function createLiveClient(options = {}) {
    const interval = Math.max(500, options.interval || 1500);
    const maxPoints = Math.max(20, options.maxPoints || 180);
    const history = {};
    let previousTelemetry = null;
    let timer = null;
    let stopped = false;

    function bucket(id) {
      if (!history[id]) {
        history[id] = {
          down: [], up: [], rxpps: [], txpps: [],
          latency: [], jitter: [], loss: [], quality: [], timestamps: []
        };
      }
      return history[id];
    }

    function push(arr, value) {
      arr.push(value);
      if (arr.length > maxPoints) arr.splice(0, arr.length - maxPoints);
    }

    async function sample() {
      if (stopped) return;
      try {
        const [telemetryResponse, stateResponse] = await Promise.all([
          fetch("/api/v1/telemetry", {cache: "no-store"}),
          fetch("/api/v1/state", {cache: "no-store"})
        ]);
        if (!telemetryResponse.ok || !stateResponse.ok) throw new Error("Live API unavailable");

        const telemetry = await telemetryResponse.json();
        const state = await stateResponse.json();
        const stateById = Object.fromEntries((state.links || []).map(link => [link.id, link]));
        const prevById = previousTelemetry
          ? Object.fromEntries((previousTelemetry.links || []).map(link => [link.id, link]))
          : {};

        (telemetry.links || []).forEach(link => {
          const prev = prevById[link.id];
          const currentState = stateById[link.id];
          if (!currentState) return;

          let down = 0, up = 0, rxpps = 0, txpps = 0;
          if (prev) {
            const dt = Math.max(.001, telemetry.timestamp - previousTelemetry.timestamp);

            const currentDownBytes = Number(link.traffic?.download?.bytes ?? link.inner?.counters?.tx_bytes ?? 0);
            const currentUpBytes = Number(link.traffic?.upload?.bytes ?? link.outer?.counters?.tx_bytes ?? 0);
            const previousDownBytes = Number(prev.traffic?.download?.bytes ?? prev.inner?.counters?.tx_bytes ?? 0);
            const previousUpBytes = Number(prev.traffic?.upload?.bytes ?? prev.outer?.counters?.tx_bytes ?? 0);

            const currentDownPackets = Number(link.traffic?.download?.packets ?? link.inner?.counters?.tx_packets ?? 0);
            const currentUpPackets = Number(link.traffic?.upload?.packets ?? link.outer?.counters?.tx_packets ?? 0);
            const previousDownPackets = Number(prev.traffic?.download?.packets ?? prev.inner?.counters?.tx_packets ?? 0);
            const previousUpPackets = Number(prev.traffic?.upload?.packets ?? prev.outer?.counters?.tx_packets ?? 0);

            down = Math.max(0, currentDownBytes - previousDownBytes) * 8 / dt / 1000000;
            up = Math.max(0, currentUpBytes - previousUpBytes) * 8 / dt / 1000000;
            rxpps = Math.max(0, currentDownPackets - previousDownPackets) / dt;
            txpps = Math.max(0, currentUpPackets - previousUpPackets) / dt;
          }

          const h = bucket(link.id);
          push(h.down, down);
          push(h.up, up);
          push(h.rxpps, rxpps);
          push(h.txpps, txpps);
          push(h.latency, Number(currentState.effective?.delay_ms || 0));
          push(h.jitter, Number(currentState.effective?.jitter_ms || 0));
          push(h.loss, Number(currentState.effective?.loss_pct || 0));
          push(h.quality, Number(currentState.quality ?? 100));
          push(h.timestamps, telemetry.timestamp);
        });

        previousTelemetry = telemetry;
        if (options.onSample) options.onSample({telemetry, state, history});
        if (options.onStatus) options.onStatus(true);
      } catch (error) {
        if (options.onStatus) options.onStatus(false, error);
      } finally {
        if (!stopped) timer = setTimeout(sample, interval);
      }
    }

    sample();
    return {
      stop() {
        stopped = true;
        if (timer) clearTimeout(timer);
      },
      history
    };
  }

  function eventTime(timestamp) {
    if (!timestamp) return "—";
    return new Date(Number(timestamp) * 1000).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", second: "2-digit"});
  }

  return {formatRate, formatPps, formatAge, formatNumber, statusClass, setPath, renderSeries, createLiveClient, eventTime};
})();
