// Full-screen progress while NetEm installs an update and restarts, matching the Traffic Simulator.
window.NetEmUpdate = (() => {
  const STEPS = [['installing', 'Installing update'], ['restarting', 'Restarting NetEm'], ['reconnecting', 'Reconnecting']];
  const TEXT = {
    installing: ['Installing update', 'Fetching the stable release and fast-forwarding this appliance. NetEm restarts next.'],
    restarting: ['Restarting NetEm', 'The service is offline while it starts the new version. This page reconnects automatically.'],
    reconnecting: ['Finishing update', 'NetEm is back online. Confirming the new service before reloading.'],
    complete: ['Update complete', 'Reloading…'],
    failed: ['Update not installed', ''],
    unconfirmed: ['Update not confirmed', ''],
    verifying: ['Confirming update', 'Checking which version is running before deciding whether the update failed.'],
    login: ['Sign in to confirm the update', 'The service requires authentication. Sign in to check the installed version.'],
  };
  const SLOW_SECONDS = 120;
  let active = false;

  // status: /updates/status payload, or null while NetEm is unreachable. The restart is
  // finished once a different process has answered twice in a row.
  function matchesVersion(actual, expected) {
    if (!expected) return true;
    const parts = value => /^v?(\d+)\.(\d+)\.(\d+)$/.exec(String(value || ''))?.slice(1).map(Number);
    const a = parts(actual), e = parts(expected);
    if (!a || !e) return actual === expected;
    for (let i = 0; i < 3; i++) { if (a[i] !== e[i]) return a[i] > e[i]; }
    return true;
  }
  function phase(status, instance, answers, expected) {
    if (!status?.instance || status.instance === instance) return 'restarting';
    return answers >= 2 && matchesVersion(status.version, expected) ? 'complete' : 'reconnecting';
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
    let candidate = null, expected = to, uncertainty = null, verifyStarted = 0;
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
      if (['complete', 'failed', 'unconfirmed', 'login'].includes(next)) {
        stopped = true;
        clearInterval(clock);
        tick();
      }
      if (next === 'failed' || next === 'unconfirmed') {
        const back = el('button', 'btn btn-primary', 'Back to updates');
        back.type = 'button';
        back.addEventListener('click', () => location.reload());
        actions.replaceChildren(back);
        back.focus();
      }
    }

    function login(path = '/login', detail) {
      render('login', detail);
      const link = el('a', 'btn btn-primary', path === '/setup/admin' ? 'Set up administrator' : 'Sign in');
      link.href = path === '/login' ? '/login?next=%2Fupdates' : path;
      actions.replaceChildren(link);
    }

    async function poll() {
      if (stopped) return;
      let status = null;
      const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 5000);
      try {
        const response = await fetch(statusUrl, {cache: 'no-store', signal: controller.signal});
        const path = new URL(response.url || statusUrl, location.href).pathname;
        if (response.status === 403) { login('/login', 'Administrator access is required to confirm the update. Sign in with an administrator account.'); return; }
        if (response.status === 401 || path === '/login' || path === '/setup/admin') { login(path === '/setup/admin' ? path : '/login'); return; }
        if (response.ok && (response.headers.get('content-type') || '').includes('json')) status = await response.json();
      } catch (_) {}
      finally { clearTimeout(timer); }
      if (stopped) return;
      const key = status?.instance && status.instance !== watching && matchesVersion(status.version, expected)
        ? status.instance + ':' + status.version : null;
      answers = key ? (key === candidate ? answers + 1 : 1) : 0;
      candidate = key;
      const next = phase(status, watching, answers, expected);
      if (next === 'complete') {
        render(next, 'Now running v' + status.version + '. Reloading…');
        setTimeout(() => location.reload(), 2000);
      } else if (uncertainty) {
        if (Date.now() - verifyStarted >= 30000) {
          render('unconfirmed', uncertainty + ' The expected running version could not be confirmed. Check Updates and the service log before retrying.');
        } else {
          render('verifying', uncertainty + ' Checking the running version; do not submit the update again yet.');
          setTimeout(poll, 2000);
        }
      } else {
        render(next);
        setTimeout(poll, 2000);
      }
    }

    render('installing');

    return {
      // The update is on disk and the named process is about to exit.
      installed(restartingInstance, version) {
        if (restartingInstance) watching = restartingInstance;
        if (version) expected = version;
        render('restarting');
        setTimeout(poll, 2000);
      },
      uncertain(detail) {
        uncertainty = detail;
        verifyStarted = Date.now();
        render('verifying', detail + ' Checking the running version; do not submit the update again yet.');
        setTimeout(poll, 2000);
      },
      login,
      fail(detail) { render('failed', detail); },
    };
  }

  // Confirmed update: show progress, post the form, then follow the restart.
  async function run(form) {
    // data-from, data-to, data-mark, data-instance and data-status-url describe this update.
    const screen = show({...form.dataset});
    if (!screen) return;
    const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 120000);
    try {
      // The attribute, because the form's "action" input masks form.action.
      const url = new URL(form.getAttribute('action') || location.href, location.href);
      const data = new FormData(form);
      // Compatibility with update cards produced before they carried their own token.
      if (!data.get('integration_csrf')) {
        const token = document.querySelector('[data-all-update-check] [name="integration_csrf"]')?.value;
        if (token) data.set('integration_csrf', token);
      }
      const response = await fetch(url.href, {method: 'POST', body: data,
        headers: {Accept: 'application/json'}, signal: controller.signal});
      if (response.status === 401 || response.status === 403 || new URL(response.url || url.href).pathname === '/login') { screen.login(); return; }
      if ((response.headers?.get('content-type') || '').includes('json')) {
        const result = await response.json();
        if (response.ok && result.update_state === 'restarting' && result.instance && result.version) {
          screen.installed(result.instance, result.version);
        } else if (result.update_state === 'rejected' && result.message) {
          screen.fail(result.message);
        } else {
          screen.uncertain('NetEm returned HTTP ' + response.status + ' without an installation acknowledgement.');
        }
        return;
      }
      // Older installations acknowledge with HTML; keep their upgrade path working.
      const page = new DOMParser().parseFromString(await response.text(), 'text/html');
      const marker = page.getElementById('update-restarting');
      const refusal = page.querySelector('.flash.error, .flash.info, .flash');
      if (response.ok && marker) screen.installed(marker.dataset.instance, marker.dataset.version);
      else if (response.ok && refusal) screen.fail(refusal.textContent.trim());
      else screen.uncertain('NetEm returned HTTP ' + response.status + ' without an installation acknowledgement.');
    } catch (_) {
      screen.uncertain('The installation response was interrupted.');
    } finally { clearTimeout(timer); }
  }

  return {phase, show, run};
})();
