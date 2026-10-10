// Full-screen progress while NetEm installs an update and restarts, matching the Traffic Simulator.
window.NetEmUpdate = (() => {
  const STEPS = [['installing', 'Installing update'], ['restarting', 'Restarting NetEm'], ['reconnecting', 'Reconnecting']];
  const TEXT = {
    installing: ['Installing update', 'Fetching the stable release and fast-forwarding this appliance. NetEm restarts next.'],
    restarting: ['Restarting NetEm', 'The service is offline while it starts the new version. This page reconnects automatically.'],
    reconnecting: ['Finishing update', 'NetEm is back online. Confirming the new service before reloading.'],
    complete: ['Update complete', 'Reloading…'],
    failed: ['Update not installed', ''],
  };
  const SLOW_SECONDS = 120;
  let active = false;

  // status: /updates/status payload, or null while NetEm is unreachable. The restart is
  // finished once a different process has answered twice in a row.
  function phase(status, instance, answers) {
    if (!status || status.instance === instance) return 'restarting';
    return answers >= 2 ? 'complete' : 'reconnecting';
  }

  function show({from, to, mark, instance, statusUrl}) {
    if (active) return null;
    active = true;
    const el = (tag, className, text) => {
      const node = document.createElement(tag);
      if (className) node.className = className;
      if (text != null) node.textContent = text;
      return node;
    };
    const screen = el('div', 'update-screen installing'), card = el('div', 'update-card'), ring = el('div', 'update-ring');
    screen.setAttribute('role', 'dialog');
    screen.setAttribute('aria-modal', 'true');
    screen.setAttribute('aria-labelledby', 'update-title');
    ring.append(el('span', 'update-mark', mark || 'N'));
    const title = el('h2', null, TEXT.installing[0]);
    title.id = 'update-title';
    const versions = el('div', 'update-versions mono', (from ? 'v' + from : 'Current version') + (to ? ' → v' + to : ''));
    const message = el('p', 'update-message', TEXT.installing[1]);
    message.setAttribute('aria-live', 'polite');
    const steps = el('ol', 'update-steps'), items = STEPS.map(([, label]) => steps.appendChild(el('li', null, label)));
    const meta = el('div', 'update-meta mono'), actions = el('div', 'update-actions');
    card.append(ring, title, versions, message, steps, meta, actions);
    card.tabIndex = -1;
    screen.append(card);
    document.body.append(screen);
    document.body.classList.add('update-active');
    card.focus();

    const started = Date.now();
    let current = 'installing', reached = 0, watching = instance, answers = 0, stopped = false;
    const tick = () => {
      const seconds = Math.floor((Date.now() - started) / 1000);
      meta.textContent = 'Elapsed ' + Math.floor(seconds / 60) + ':' + String(seconds % 60).padStart(2, '0') +
        (seconds > SLOW_SECONDS && current !== 'failed' ? ' · taking longer than usual; on the appliance run journalctl -u netem -n 50' : '');
    };
    tick();
    const clock = setInterval(tick, 1000);

    function render(next, detail) {
      current = next;
      const index = next === 'complete' ? STEPS.length : STEPS.findIndex(([key]) => key === next);
      if (index >= 0) reached = index;
      items.forEach((item, i) => item.className = i < reached ? 'done' : i === reached ? (next === 'failed' ? 'failed' : 'active') : '');
      screen.className = 'update-screen ' + next;
      title.textContent = TEXT[next][0];
      message.textContent = detail || TEXT[next][1];
      if (next === 'complete' || next === 'failed') {
        stopped = true;
        clearInterval(clock);
        tick();
      }
      if (next === 'failed') {
        const back = el('button', 'btn btn-primary', 'Back to updates');
        back.type = 'button';
        back.addEventListener('click', () => location.reload());
        actions.replaceChildren(back);
        back.focus();
      }
    }

    async function poll() {
      if (stopped) return;
      let status = null;
      try {
        const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 5000);
        const response = await fetch(statusUrl, {cache: 'no-store', signal: controller.signal});
        clearTimeout(timer);
        if (response.ok && (response.headers.get('content-type') || '').includes('json')) status = await response.json();
      } catch (_) {}
      answers = status && status.instance !== watching ? answers + 1 : 0;
      const next = phase(status, watching, answers);
      render(next, next === 'complete' ? 'Now running v' + status.version + '. Reloading…' : null);
      if (next === 'complete') setTimeout(() => location.reload(), 2000);
      else setTimeout(poll, 2000);
    }

    render('installing');

    return {
      // The update is on disk and the named process is about to exit.
      installed(restartingInstance) {
        if (restartingInstance) watching = restartingInstance;
        render('restarting');
        setTimeout(poll, 2000);
      },
      fail(detail) { render('failed', detail); },
    };
  }

  // Confirmed update: show progress, post the form, then follow the restart.
  async function run(form) {
    // data-from, data-to, data-mark, data-instance and data-status-url describe this update.
    const screen = show({...form.dataset});
    if (!screen) return;
    let response;
    try {
      // The attribute, because the form's "action" input masks form.action.
      const url = new URL(form.getAttribute('action') || location.href, location.href);
      response = await fetch(url.href, {method: 'POST', body: new FormData(form)});
    } catch (_) {
      // The connection dropped mid-request: NetEm may already be restarting, so let the status decide.
      screen.installed();
      return;
    }
    const page = new DOMParser().parseFromString(await response.text(), 'text/html');
    const marker = page.getElementById('update-restarting');
    if (response.ok && marker) screen.installed(marker.dataset.instance);
    else screen.fail((page.querySelector('.flash.error, .flash.info, .flash') || {}).textContent?.trim() ||
      'NetEm did not confirm the update (HTTP ' + response.status + '). Nothing was restarted.');
  }

  return {phase, show, run};
})();
