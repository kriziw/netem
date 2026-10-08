window.NetEmUI = (() => {
  function formatRate(mbps) {
    if (!Number.isFinite(mbps)) return "—";
    if (mbps >= 1000) return (mbps / 1000).toFixed(2) + " Gbit/s";
    if (mbps >= 100) return mbps.toFixed(0) + " Mbit/s";
    if (mbps >= 10) return mbps.toFixed(1) + " Mbit/s";
    return mbps.toFixed(2) + " Mbit/s";
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
            down = Math.max(0, link.inner.counters.tx_bytes - prev.inner.counters.tx_bytes) * 8 / dt / 1000000;
            up = Math.max(0, link.outer.counters.tx_bytes - prev.outer.counters.tx_bytes) * 8 / dt / 1000000;
            rxpps = Math.max(0, link.inner.counters.rx_packets - prev.inner.counters.rx_packets) / dt;
            txpps = Math.max(0, link.outer.counters.tx_packets - prev.outer.counters.tx_packets) / dt;
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

  return {formatRate, formatNumber, statusClass, setPath, createLiveClient, eventTime};
})();
