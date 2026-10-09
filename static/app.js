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
      const digits = options.digits ?? (max < 1 ? Math.min(6, Math.ceil(-Math.log10(max)) + 1) : max < 10 ? 1 : 0);
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

    if (options.hoverLabels && paths[0]?.ownerDocument) {
      const svg = paths[0].ownerSVGElement;
      const doc = paths[0].ownerDocument;
      const ns = 'http://www.w3.org/2000/svg';
      let samples = svg.querySelector('[data-sample-labels]');
      if (!samples) {
        samples = doc.createElementNS(ns, 'g');
        samples.setAttribute('data-sample-labels', '');
        svg.append(samples);
      }
      const slots = Math.max(options.slots || 0, ...arrays.map(values => values.length));
      const timestamps = options.timestamps || [];
      const points = [];
      for (let index = 0; index < slots; index++) {
        const values = arrays.map((series, seriesIndex) => {
          const value = series[index - (slots - series.length)];
          return value == null || !Number.isFinite(Number(value)) ? null
            : options.hoverLabels[seriesIndex] + ': ' + (options.sampleUnit === 'Mbit/s' ? formatRate(value)
              : Number(value).toFixed(2) + ' ' + (options.sampleUnit || ''));
        }).filter(Boolean);
        if (!values.length) continue;
        const timestamp = timestamps[index - (slots - timestamps.length)];
        const description = (timestamp == null ? '' : eventTime(timestamp) + ' · ') + values.join(' · ');
        const point = doc.createElementNS(ns, 'line');
        const x = slots > 1 ? index / (slots - 1) * 100 : 0;
        point.setAttribute('x1', x); point.setAttribute('x2', x);
        point.setAttribute('y1', 0); point.setAttribute('y2', 100);
        point.setAttribute('stroke', 'transparent'); point.setAttribute('stroke-width', '6');
        point.setAttribute('vector-effect', 'non-scaling-stroke');
        point.setAttribute('pointer-events', 'stroke');
        point.setAttribute('aria-label', description);
        const title = doc.createElementNS(ns, 'title'); title.textContent = description;
        point.append(title); points.push(point);
      }
      samples.replaceChildren(...points);
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

  function renderDemSummary(root, dem = {}, connected = true, targets = null) {
    if (!root) return;
    const hasData = connected && Number(dem.requests) > 0;
    const number = (value, max = Infinity) => value != null && Number.isFinite(Number(value)) && Number(value) >= 0 && Number(value) <= max ? Number(value) : null;
    const score = hasData ? number(dem.experience_score, 100) : null;
    const success = hasData ? number(dem.availability_pct, 100) : null;
    // Interactive P95 leaves out large transfers, whose time is mostly bandwidth.
    const interactive = hasData && dem.interactive_p95_ms != null;
    const latency = hasData ? number(targets || interactive ? dem.interactive_p95_ms : dem.p95_ms) : null;
    const scope = interactive ? 'web, collaboration and DNS transactions' : 'timed transactions';
    const blended = interactive && number(dem.p95_ms) != null ? ' All applications including large transfers: '+number(dem.p95_ms).toFixed(0)+' ms.' : '';
    const missing = connected ? 'Waiting for measured transactions.' : 'Simulator unavailable; impact cannot be measured.';
    const rows = [
      {key:'experience', value:score == null ? '—' : score.toFixed(0)+'/100',
        tone:score == null ? 'unknown' : score >= 75 ? 'good' : score >= 55 ? 'warn' : 'bad',
        explanation:score == null ? missing : score >= 75 ? 'Requests are completing with a good overall experience.' : score >= 55 ? 'Slower responses or failed requests are affecting users.' : 'Delays or failed requests are seriously affecting users.'},
      {key:'success', value:success == null ? '—' : success.toFixed(2)+'%',
        tone:success == null ? 'unknown' : success >= 99 ? 'good' : success >= 95 ? 'warn' : 'bad',
        explanation:success == null ? missing : (100-success).toFixed(2)+'% of simulated transactions failed. '+(success >= 99 ? 'Few or no request failures.' : success >= 95 ? 'Some users may need to retry.' : 'Frequent failures interrupt simulated work.')},
      {key:'response', value:latency == null ? '—' : latency.toFixed(0)+' ms',
        tone:latency == null ? 'unknown' : latency <= 400 ? 'good' : latency <= 1800 ? 'warn' : 'bad',
        explanation:latency == null ? (hasData ? 'No successful responses with timing data.' : missing) : '95% of successful '+scope+' finished within '+latency.toFixed(0)+' ms. '+(latency <= 400 ? 'Short waits for most requests.' : latency <= 1800 ? 'Users may notice waiting.' : 'Long waits for successful requests.')+blended}
    ];
    if (targets) {
      const checks = [[score, targets.experience_min, '≥', ' / 100'], [success, targets.success_min_pct, '≥', '%'], [latency, targets.interactive_p95_max_ms, '≤', ' ms']];
      rows.forEach((row, index) => {
        const [actual, target, op, unit] = checks[index];
        row.tone = actual == null ? 'unknown' : (op === '≥' ? actual >= target : actual <= target) ? 'good' : 'bad';
        const measurement = index === 0 ? 'The simulator combines request success and application response scores.' : index === 1 ? (100-actual).toFixed(2)+'% of simulated transactions failed.' : '95% of successful interactive application transactions finished within '+actual+' ms.'+blended;
        row.explanation = actual == null ? (connected ? 'Waiting for the measurements required by the active site.' : missing) : 'Active site target: '+op+' '+target+unit+'. '+(row.tone === 'good' ? 'Target met. ' : 'Target missed. ')+measurement;
      });
    }
    for (const row of rows) {
      const element = root.querySelector('[data-dem-indicator="'+row.key+'"]');
      if (!element) continue;
      element.className = 'dem-indicator '+row.tone;
      element.querySelector('[data-dem-symbol]').textContent = {good:'✓',warn:'!',bad:'×',unknown:'—'}[row.tone];
      element.querySelector('[data-dem-value]').textContent = row.value;
      element.querySelector('[data-dem-status]').textContent = (targets ? {good:'Target met',bad:'Target missed',unknown:'No data'} : {good:'Good',warn:'Degraded',bad:'Poor',unknown:'No data'})[row.tone];
      element.querySelector('[data-dem-explanation]').textContent = row.explanation;
    }
    const window = root.querySelector('[data-dem-window]');
    if (window) window.textContent = connected ? 'Last '+(number(dem.window_seconds) || 60)+' seconds · '+(number(dem.requests) || 0)+' measured transactions'+(dem.truncated ? ' · sample limit reached' : '') : 'Measurements unavailable';
  }

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  }

  // Findings pair what simulated users experienced with the WAN that carried it and the path condition there.
  function diagnosisFindingsHtml(payload) {
    const findings = (payload && payload.findings) || [];
    if (!findings.length) return '<li class="diag-empty">No problems detected right now.</li>';
    return findings.map(finding => {
      const wans = (finding.wans || []).filter(wan => wan.affected || finding.source !== 'experience').map(wan =>
        '<div class="diag-wan"><strong>'+escapeHtml(wan.label)+'</strong>'+(wan.affected ? ' · '+wan.affected+' affected' : '')+
        (wan.causes && wan.causes.length ? ' ← '+wan.causes.map(escapeHtml).join('; ') : finding.source === 'experience' ? ' · no NetEm impairment explains it' : '')+'</div>');
      if (finding.unattributed) {
        wans.push('<div class="diag-wan muted">'+finding.unattributed+' without a reply to show the WAN'+
          (finding.candidates && finding.candidates.length ? ' · likely '+finding.candidates.map(escapeHtml).join(' or ') : '')+'</div>');
      }
      return '<li class="diag-finding '+escapeHtml(finding.severity)+'"><span class="diag-dot" aria-hidden="true"></span><div class="diag-body">'+
        '<div class="diag-title">'+escapeHtml(finding.title)+'</div>'+wans.join('')+
        (finding.detail ? '<div class="diag-detail">'+escapeHtml(finding.detail)+'</div>' : '')+
        (finding.hint ? '<div class="diag-hint">'+escapeHtml(finding.hint)+'</div>' : '')+'</div></li>';
    }).join('');
  }

  // Per-WAN bottleneck view: how full each shaped direction is, its drops, and its users' experience.
  function linkBottleneck(link) {
    const directions = (link && link.directions) || {};
    const direction = (key, arrow) => {
      const item = directions[key] || {};
      const pct = item.util_pct == null ? null : Math.max(0, Math.min(100, Number(item.util_pct)));
      return {
        pct,
        tone: pct == null ? 'unknown' : pct >= 90 ? 'bad' : pct >= 70 ? 'warn' : 'good',
        text: arrow+' '+(pct == null ? '—' : Math.round(pct)+'%')+(item.limit_mbit ? ' of '+formatNumber(item.limit_mbit, 0)+' Mbit/s' : ' · unshaped'),
      };
    };
    const drops = item => item && item.queue_drops_ps != null ? formatNumber(item.queue_drops_ps, item.queue_drops_ps < 10 ? 1 : 0)+'/s' : '—';
    const injected = directions.down && directions.down.injected_drops_ps;
    const experience = link && link.experience;
    return {
      down: direction('down', '↓'),
      up: direction('up', '↑'),
      drops: 'Queue drops ↓ '+drops(directions.down)+' ↑ '+drops(directions.up)+(injected ? ' · injected loss '+formatNumber(injected, 1)+'/s' : ''),
      experience: experience ? 'Users on this WAN: '+formatNumber(experience.availability_pct, 1)+'% success'+
        (experience.worst_app ? ' · worst '+experience.worst_app.replaceAll('_', ' ')+' '+formatNumber(experience.worst_availability_pct, 0)+'%' : '') :
        'No simulated users traced to this WAN yet',
      badge: ((link && link.full) || []).map(key => key === 'down' ? '↓ full' : '↑ full').join(' '),
    };
  }

  // SD-WAN steering: per traffic class, where it goes now (bar segments colored by WAN health) and the verdict.
  function steeringHtml(steering, targets = null) {
    if (!steering) {
      return '<div class="diag-empty">Steering is assessed once the Traffic Simulator reports per-WAN traffic (v0.7 or later with its target updated) and NetEm can map the appliance WAN addresses.</div>';
    }
    const verdicts = {steered:'Steered away', unaffected:'Unaffected', balanced:'Healthy', stuck:'On impaired WAN', stuck_impact:'Users affected', no_healthy:'No healthy WAN', idle:'No traffic', unattributed:'WAN unknown', partial:'Partial attribution'};
    return (steering.classes || []).map(item => {
      const segments = (item.shares || []).filter(share => share.pct).map(share =>
        '<span class="steer-seg '+escapeHtml(share.health)+'" style="width:'+Number(share.pct)+'%" title="'+
        escapeHtml(share.label+' · '+formatNumber(share.pct, 0)+'% · '+share.health+(share.health_reason ? ' ('+share.health_reason+')' : ''))+'">'+
        (share.pct >= 14 ? escapeHtml(share.label)+' '+formatNumber(share.pct, 0)+'%' : '')+'</span>').join('')+
        (item.unattributed_pct ? '<span class="steer-seg empty" style="width:'+Number(item.unattributed_pct)+'%" title="WAN unknown · '+Number(item.unattributed || 0)+' transactions">'+(item.unattributed_pct >= 14 ? 'Unknown '+formatNumber(item.unattributed_pct, 0)+'%' : '')+'</span>' : '');
      const reactions = (item.reactions || []).filter(reaction => reaction.was_used).map(reaction =>
        reaction.steered_after_seconds != null ? 'moved off '+reaction.label+' ≈'+reaction.steered_after_seconds+' s after it became '+reaction.health
          : reaction.label+' '+reaction.health+' for '+reaction.impaired_for_seconds+' s');
      const late = targets && (item.reactions || []).some(reaction => reaction.was_used && Number(reaction.steered_after_seconds ?? reaction.impaired_for_seconds) > targets.steering_max_s);
      return '<div class="steer-row"><div class="steer-head"><strong>'+escapeHtml(item.label)+'</strong>'+
        '<span class="status '+(late ? 'bad' : {good:'good', warn:'warn', bad:'bad'}[item.severity] || 'info')+'">'+(late ? 'Steering target missed' : verdicts[item.verdict] || escapeHtml(item.verdict))+'</span></div>'+
        '<div class="steer-bar">'+(segments || '<span class="steer-seg empty" style="width:100%"></span>')+'</div>'+
        '<div class="steer-text">'+escapeHtml(item.text)+(targets ? ' · target ≤ '+targets.steering_max_s+' s' : '')+(reactions.length ? '<br>'+reactions.map(escapeHtml).join(' · ') : '')+'</div></div>';
    }).join('');
  }

  return {trafficRates, formatRate, formatPps, formatAge, formatNumber, statusClass, setPath, renderSeries, createLiveClient, eventTime, renderDemSummary, diagnosisFindingsHtml, linkBottleneck, steeringHtml};
})();
