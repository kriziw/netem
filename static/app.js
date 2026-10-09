window.NetEmUI = (() => {
  function formatRate(mbps) {
    const value = Number(mbps);
    if (mbps == null || !Number.isFinite(value)) return "—";
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
    if (pps == null || !Number.isFinite(value)) return "—";
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
    if (value == null || !Number.isFinite(n)) return "—";
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
    const clean = values.filter(v => v != null).map(Number).filter(Number.isFinite);
    if (!clean.length) return "";
    const max = fixedMax || Math.max(...clean, 1);
    const min = fixedMax ? 0 : Math.min(...clean, 0);
    const span = Math.max(.0001, max - min);
    return seriesPath(values, width, height - 4, height - 8, min, span);
  }

  function seriesPath(values, width, baseline, height, min, span) {
    let connected = false;
    return values.map((raw, index) => {
      const value = Number(raw);
      if (raw == null || !Number.isFinite(value)) {
        connected = false;
        return "";
      }
      const x = values.length === 1 ? 0 : index * width / (values.length - 1);
      const y = baseline - ((value - min) / span) * height;
      const command = connected ? "L" : "M";
      connected = true;
      return command + x.toFixed(2) + "," + y.toFixed(2);
    }).filter(Boolean).join(" ");
  }

  const animations = new WeakMap();
  const scales = new WeakMap();
  function drawPath(element, path, animate = false) {
    const pending = animations.get(element);
    if (pending) window.cancelAnimationFrame(pending);
    const before = element.getAttribute?.("d") || "";
    const reduced = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    const start = before.match(/[ML]-?[\d.]+,-?[\d.]+/g) || [];
    const end = path.match(/[ML]-?[\d.]+,-?[\d.]+/g) || [];
    // Never interpolate across a missing-data gap.
    const compatible = start.length && end.length &&
      start.filter(v => v[0] === "M").length === 1 && end.filter(v => v[0] === "M").length === 1;
    if (!animate || reduced || !compatible || !window.requestAnimationFrame) {
      element.setAttribute("d", path);
      return;
    }
    const from = end.map((_, i) => start[Math.min(i, start.length - 1)].slice(1).split(",").map(Number));
    const to = end.map(v => v.slice(1).split(",").map(Number));
    let began;
    const frame = now => {
      began ??= now;
      const t = Math.min(1, (now - began) / 600);
      const eased = t * t * (3 - 2 * t);
      element.setAttribute("d", end.map((v, i) => v[0] + to[i].map((n, j) =>
        (from[i][j] + (n - from[i][j]) * eased).toFixed(2)).join(",")).join(" "));
      if (t < 1) animations.set(element, window.requestAnimationFrame(frame));
      else { element.setAttribute("d", path); animations.delete(element); }
    };
    animations.set(element, window.requestAnimationFrame(frame));
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
    const flat = arrays.flat().filter(v => v != null).map(Number).filter(Number.isFinite);
    let max = options.fixedMax != null
      ? Number(options.fixedMax)
      : niceMax(Math.max(...flat, 0));
    const scaleKey = options.axis || paths[0];
    if (options.stableScale && scaleKey && options.fixedMax == null) {
      max = Math.max(max, scales.get(scaleKey) || 0);
      scales.set(scaleKey, max);
    }
    const min = options.fixedMin != null ? Number(options.fixedMin) : 0;
    const span = Math.max(.0001, max - min);

    arrays.forEach((values, index) => {
      const element = paths[index];
      if (!element) return;
      const slots = Math.max(values.length, options.slots || values.length);
      const padded = Array(Math.max(0, slots - values.length)).fill(null).concat(values);
      const path = seriesPath(padded, 100, 96, 92, min, span);
      drawPath(element, path, options.animate);
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
      const last = Number(options.timestamps[options.timestamps.length - 1]);
      const spacing = options.timestamps.length > 1
        ? (last - Number(options.timestamps[0])) / (options.timestamps.length - 1) : 1.5;
      const first = options.slots > options.timestamps.length
        ? last - spacing * (options.slots - 1) : Number(options.timestamps[0]);
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

  function trafficRates(link, previous, telemetry, previousTelemetry) {
    const missing = {down: null, up: null, rxpps: null, txpps: null};
    if (!previous || !previousTelemetry || telemetry.sampler_id !== previousTelemetry.sampler_id
        || !link.counters_valid || !previous.counters_valid) return missing;
    for (const side of ["inner", "outer"]) {
      if (link[side]?.interface !== previous[side]?.interface
          || link[side]?.ifindex !== previous[side]?.ifindex) return missing;
    }
    const dt = link.monotonic_timestamp - previous.monotonic_timestamp;
    if (!Number.isFinite(dt) || dt <= 0) return missing;
    const deltas = [];
    for (const [direction, counter] of [["download", "bytes"], ["upload", "bytes"],
                                      ["download", "packets"], ["upload", "packets"]]) {
      const current = link.traffic?.[direction]?.[counter];
      const before = previous.traffic?.[direction]?.[counter];
      if (current == null || before == null || !Number.isFinite(current)
          || !Number.isFinite(before) || current < before) return missing;
      deltas.push((current - before) / dt);
    }
    return {down: deltas[0] * 8 / 1000000, up: deltas[1] * 8 / 1000000,
            rxpps: deltas[2], txpps: deltas[3]};
  }

  function createLiveClient(options = {}) {
    const interval = Math.max(500, options.interval || 1500);
    const maxPoints = Math.max(20, options.maxPoints || 180);
    const key = options.historyKey || window.location?.pathname || "default";
    const cache = window.__netemLiveHistory ||= {};
    const saved = cache[key] ||= {history: {}, previousTelemetry: null};
    const history = saved.history;
    let previousTelemetry = saved.previousTelemetry;
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
        if (stopped) return;
        const stateById = Object.fromEntries((state.links || []).map(link => [link.id, link]));
        const prevById = previousTelemetry
          ? Object.fromEntries((previousTelemetry.links || []).map(link => [link.id, link]))
          : {};

        (telemetry.links || []).forEach(link => {
          const prev = prevById[link.id];
          const currentState = stateById[link.id];
          if (!currentState) return;

          const {down, up, rxpps, txpps} = trafficRates(link, prev, telemetry, previousTelemetry);

          const h = bucket(link.id);
          push(h.down, down);
          push(h.up, up);
          push(h.rxpps, rxpps);
          push(h.txpps, txpps);
          push(h.latency, Number(currentState.effective?.delay_ms || 0));
          push(h.jitter, Number(currentState.effective?.jitter_ms || 0));
          push(h.loss, Number(currentState.effective?.loss_pct || 0));
          push(h.quality, Number(currentState.quality ?? 100));
          push(h.timestamps, link.timestamp);
        });

        previousTelemetry = telemetry;
        saved.previousTelemetry = telemetry;
        if (options.onSample) options.onSample({telemetry, state, history});
        if (options.onStatus) options.onStatus(true);
      } catch (error) {
        if (options.onStatus) options.onStatus(false, error);
      } finally {
        if (!stopped) timer = setTimeout(sample, interval);
      }
    }

    sample();
    const client = {
      stop() {
        stopped = true;
        if (timer) clearTimeout(timer);
      },
      history
    };
    window.NetEmActions?.trackClient(client);
    return client;
  }

  function eventTime(timestamp) {
    if (!timestamp) return "—";
    return new Date(Number(timestamp) * 1000).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", second: "2-digit"});
  }

  return {trafficRates, formatRate, formatPps, formatAge, formatNumber, statusClass, setPath, renderSeries, createLiveClient, eventTime};
})();
