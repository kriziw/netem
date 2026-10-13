window.NetEmActions = (() => {
  let cleanups = [];
  let busy = false;
  // Unsaved edits in a form on the page; an automatic refresh would throw them away.
  let dirty = false;
  let editRevision = 0;
  let watching = false;
  let navigationController = null;
  let navigationGeneration = 0;
  let historyIndex = history.state?.netemIndex || 0;
  let renderedURL = location.href;
  let restoringHistory = false;
  let discardPrompt = null;
  let historyGeneration = 0;
  const WATCH_MS = 3000;
  function trackClient(client) { cleanups.push(() => client.stop()); }
  function cleanup() {
    cleanups.splice(0).forEach(stop => stop());
  }
  function mount() {
    watch();
    const template = document.getElementById('page-scripts');
    if (!template) return;
    const controller = new AbortController();
    cleanups.push(() => controller.abort());
    const scopedDocument = new Proxy(document, {
      get(target, name) {
        if (name === 'addEventListener') return (type, handler, options) => {
          target.addEventListener(type, handler, options);
          cleanups.push(() => target.removeEventListener(type, handler, options));
        };
        const value = target[name];
        return typeof value === 'function' ? value.bind(target) : value;
      }
    });
    const interval = (handler, delay) => {
      const id = window.setInterval(handler, delay);
      cleanups.push(() => window.clearInterval(id));
      return id;
    };
    const timeout = (handler, delay) => {
      const id = window.setTimeout(handler, delay);
      cleanups.push(() => window.clearTimeout(id));
      return id;
    };
    const scopedWindow = new Proxy(window, {
      get(target, name) {
        if (name === 'setInterval') return interval;
        if (name === 'setTimeout') return timeout;
        if (name === 'addEventListener') return (type, handler, options) => {
          target.addEventListener(type, handler, options);
          cleanups.push(() => target.removeEventListener(type, handler, options));
        };
        const value = target[name];
        return typeof value === 'function' ? value.bind(target) : value;
      }
    });
    const pageFetch = async (url, options = {}) => {
      const signal = options.signal && AbortSignal.any
        ? AbortSignal.any([options.signal, controller.signal]) : controller.signal;
      const response = await fetch(url, {...options, signal});
      if (controller.signal.aborted) throw new DOMException('Page changed', 'AbortError');
      return response;
    };
    template.content.querySelectorAll('script:not([src])').forEach(script => {
      new Function('document', 'setInterval', 'setTimeout', 'fetch', 'window', script.textContent)(scopedDocument, interval, timeout, pageFetch, scopedWindow);
    });
  }
  function displayError(message) {
    let notice = document.getElementById('action-error');
    if (!notice) {
      notice = document.createElement('div');
      notice.id = 'action-error';
      notice.className = 'flash error';
      notice.setAttribute('role', 'alert');
      document.querySelector('main.page')?.prepend(notice);
    }
    notice.textContent = message;
  }
  async function update(response, options = {}) {
    if (!response.ok && response.status !== 400) throw new Error('Action failed (' + response.status + ').');
    const url = new URL(response.url || location.href);
    if (url.origin !== location.origin) throw new Error('Navigation must stay within this NetEm instance.');
    const next = new DOMParser().parseFromString(await response.text(), 'text/html');
    if (options.isCurrent && !options.isCurrent()) return false;
    if (options.beforeCommit && !(await options.beforeCommit())) return false;
    if (options.isCurrent && !options.isCurrent()) return false;
    const page = next.querySelector('main.page');
    if (!response.ok && !page?.querySelector('[data-native-traffic]')) throw new Error('Action failed (' + response.status + ').');
    const scripts = next.getElementById('page-scripts');
    if (!page || !scripts) throw new Error('Unexpected response. Check the service before retrying.');
    const openDrawers = options.navigation ? [] : [...document.querySelectorAll('.drawer.open')].map(el => el.id);
    const expandedDetails = options.navigation ? [] : [...document.querySelectorAll('main.page details.command-detail[open]')].map(el => el.id);
    expandedDetails.forEach(id => {
      const detail = page.querySelector('#' + CSS.escape(id));
      if (detail?.matches('details.command-detail')) detail.open = true;
    });
    const scroll = options.navigation ? (options.scroll || 0) : window.scrollY;
    const chartEmpty = document.getElementById('throughput-empty');
    if (chartEmpty && page.querySelector('#throughput-empty'))
      page.querySelector('#throughput-empty').style.display = chartEmpty.style.display;
    cleanup();
    // Keep the rendered chart geometry and its scale while controls update around it.
    if (!options.navigation) page.querySelectorAll('[data-chart],[data-spark],.chart-y-axis,.chart-x-axis').forEach(element => {
      const key = element.id ? '#' + CSS.escape(element.id) : element.hasAttribute('data-chart')
        ? '[data-chart="' + CSS.escape(element.dataset.chart) + '"]'
        : '[data-spark="' + CSS.escape(element.dataset.spark) + '"]';
      const old = document.querySelector(key);
      if (old) element.replaceWith(old);
    });
    document.querySelector('main.page').replaceWith(page);
    document.getElementById('page-scripts').replaceWith(scripts);
    // The top bar's session, test and impairment pills and the impairment banner follow too.
    for (const id of ['top-status', 'global-alert-slot', 'inventory-alerts', 'page-guide', 'command-results']) {
      const fresh = next.getElementById(id), current = document.getElementById(id);
      if (fresh && current) current.replaceWith(fresh);
    }
    const breadcrumb = next.querySelector('.breadcrumb');
    if (breadcrumb && document.querySelector('.breadcrumb')) document.querySelector('.breadcrumb').replaceChildren(...breadcrumb.childNodes);
    const activeLinks = new Set([...next.querySelectorAll('.sidebar .nav-link.active')].map(link => link.getAttribute('href')));
    document.querySelectorAll('.sidebar .nav-link').forEach(link => link.classList.toggle('active', activeLinks.has(link.getAttribute('href'))));
    dirty = false;
    const hash = options.destination ? new URL(options.destination).hash : location.hash;
    const destination = url.pathname + url.search + hash;
    if (options.navigation && options.historyMode !== 'none') {
      historyIndex++;
      history.pushState({netemIndex: historyIndex, scroll: 0}, '', destination);
    } else if (options.historyMode === 'none') {
      historyIndex = options.index ?? historyIndex;
    } else history.replaceState({...history.state, netemIndex: historyIndex, scroll}, '', destination);
    renderedURL = location.href;
    document.title = next.title;
    mount();
    openDrawers.forEach(id => document.getElementById(id)?.classList.add('open'));
    if (openDrawers.length) document.querySelector('.drawer-backdrop')?.classList.add('open');
    window.scrollTo(0, scroll);
    if (options.navigation) {
      document.querySelector('.drawer-backdrop')?.classList.remove('open');
      document.dispatchEvent(new CustomEvent('netem:navigated'));
      page.setAttribute('tabindex', '-1');
      page.focus({preventScroll: true});
      if (options.historyMode !== 'none') scrollToAnchor(hash);
    }
    return true;
  }

  function scrollToAnchor(hash) {
    if (!hash) return;
    let id;
    try { id = decodeURIComponent(hash.slice(1)); } catch (_) { return; }
    const target = document.getElementById(id);
    if (!target) return;
    for (let parent = target; parent; parent = parent.parentElement) {
      if (parent.tagName === 'DETAILS') parent.open = true;
    }
    target.scrollIntoView?.({block: 'start'});
  }
  function isAppPage(url) {
    return url.origin === location.origin && (/^\/(?:dashboard|wan|lab|tests|sessions|scenarios|traffic-security|analytics|integrations|settings|setup|presets|updates|docs|appliances|platform-health)?$/.test(url.pathname)
      || /^\/settings\/(?:branding|traffic\/(?:network|experience|maintenance))$/.test(url.pathname)
      || /^\/docs\/[^/]+$/.test(url.pathname) || /^\/sessions\/[^/]+\/report$/.test(url.pathname));
  }
  function rememberScroll() {
    history.replaceState({...history.state, netemIndex: historyIndex, scroll: window.scrollY}, '', location.href);
  }
  function confirmDiscard() {
    const dialog = document.getElementById('navigation-confirm-dialog');
    if (!dialog || typeof dialog.showModal !== 'function') return Promise.resolve(window.confirm('Discard unsaved changes and leave this page?'));
    if (discardPrompt) return discardPrompt;
    discardPrompt = new Promise(resolve => {
      dialog.returnValue = 'stay';
      dialog.addEventListener('close', () => {
        discardPrompt = null;
        resolve(dialog.returnValue === 'leave');
      }, {once: true});
      dialog.showModal();
    });
    return discardPrompt;
  }
  document.querySelector('#navigation-confirm-dialog [data-navigation-stay]')?.addEventListener('click', () => document.getElementById('navigation-confirm-dialog').close('stay'));
  document.querySelector('#navigation-confirm-dialog [data-navigation-leave]')?.addEventListener('click', () => document.getElementById('navigation-confirm-dialog').close('leave'));
  async function navigate(destination, options = {}) {
    const url = new URL(destination, location.href);
    if (!isAppPage(url)) return;
    const current = new URL(renderedURL);
    if (url.pathname === current.pathname && url.search === current.search) {
      if (navigationController) {
        navigationController.abort();
        navigationController = null;
        navigationGeneration++;
        busy = false;
        document.querySelector('main.page')?.removeAttribute('aria-busy');
      }
      if (options.historyMode !== 'none' && url.href !== location.href) {
        rememberScroll();
        historyIndex++;
        history.pushState({netemIndex: historyIndex, scroll: window.scrollY}, '', url.href);
      }
      historyIndex = options.index ?? historyIndex;
      renderedURL = location.href;
      if (url.hash) scrollToAnchor(url.hash);
      else window.scrollTo(0, options.scroll || 0);
      document.dispatchEvent(new CustomEvent('netem:navigated'));
      return;
    }
    if (dirty && !options.skipPrompt && !(await confirmDiscard())) return;
    if (busy && !navigationController) {
      notify('An action is still completing. Try again in a moment.', 'info');
      if (options.historyMode === 'none' && options.index !== historyIndex) {
        restoringHistory = true;
        history.go(historyIndex - options.index);
      }
      return;
    }
    if (options.historyMode !== 'none') rememberScroll();
    const originalEdits = editRevision;
    navigationController?.abort();
    const controller = navigationController = new AbortController();
    const token = ++navigationGeneration;
    busy = true;
    document.querySelector('main.page')?.setAttribute('aria-busy', 'true');
    try {
      const response = await fetch(url.href, {cache: 'no-store', signal: controller.signal});
      if (token !== navigationGeneration) return;
      const applied = await update(response, {...options, navigation: true, destination: url.href,
        isCurrent: () => token === navigationGeneration,
        beforeCommit: () => !dirty || editRevision === originalEdits || confirmDiscard(),
      });
      if (!applied && token === navigationGeneration && options.historyMode === 'none' && options.index !== historyIndex) {
        restoringHistory = true;
        history.go(historyIndex - options.index);
      }
    } catch (error) {
      if (token !== navigationGeneration || error.name === 'AbortError') return;
      displayError(error.message + ' Your current page has been kept.');
      if (options.historyMode === 'none' && options.index !== historyIndex) {
        restoringHistory = true;
        history.go(historyIndex - options.index);
      }
    } finally {
      if (token === navigationGeneration) {
        navigationController = null;
        busy = false;
        document.querySelector('main.page')?.removeAttribute('aria-busy');
      }
    }
  }

  document.addEventListener('click', event => {
    const link = event.target.closest?.('a[href]');
    if (!link || event.defaultPrevented || event.button > 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey
        || link.hasAttribute('download') || (link.target && link.target !== '_self')
        || link.matches('[data-simulator-modal],[data-scenario-modal]') || link.closest('dialog')) return;
    const url = new URL(link.getAttribute('href'), location.href);
    if (!isAppPage(url)) return;
    event.preventDefault();
    navigate(url.href);
  });
  window.addEventListener('popstate', async event => {
    if (restoringHistory) { restoringHistory = false; return; }
    const token = ++historyGeneration;
    const index = event.state?.netemIndex;
    if (!Number.isInteger(index)) return;
    const current = new URL(renderedURL), target = new URL(location.href);
    const leaving = current.pathname !== target.pathname || current.search !== target.search;
    if (leaving && dirty) {
      const discard = await confirmDiscard();
      if (token !== historyGeneration) return;
      if (!discard) {
        restoringHistory = true;
        history.go(historyIndex - index);
        return;
      }
    }
    navigate(location.href, {historyMode: 'none', index, scroll: event.state.scroll || 0, skipPrompt: true});
  });
  window.addEventListener('scroll', () => { if (!busy) rememberScroll(); }, {passive: true});
  async function refresh() {
    if (busy) return;
    busy = true;
    try { await update(await fetch(location.href, {cache:'no-store'})); }
    catch (error) { displayError(error.message); }
    finally { busy = false; }
  }
  // A short message that outlives the page refresh an action triggers.
  function notify(message, tone = 'success') {
    let stack = document.getElementById('action-toasts');
    if (!stack) {
      stack = document.createElement('div');
      stack.id = 'action-toasts';
      stack.className = 'action-toasts';
      stack.setAttribute('role', 'status');
      stack.setAttribute('aria-live', 'polite');
      document.body.append(stack);
    }
    const toast = document.createElement('div');
    toast.className = 'flash ' + tone;
    toast.textContent = message;
    stack.append(toast);
    window.setTimeout(() => toast.remove(), 6000);
  }
  // Pages follow the lab: when a session, test, plan step, impairment or saved setting changes
  // anywhere, the server's state version changes and the page refreshes in place. While the
  // operator is typing or has unsaved edits, a notice offers the refresh instead.
  const editing = () => {
    const active = document.activeElement;
    return Boolean(active?.closest?.('main.page') && active.matches?.('input:not([type="button"]):not([type="submit"]),select,textarea,[contenteditable="true"]'));
  };
  function staleNotice() {
    const page = document.querySelector('main.page');
    if (!page || document.getElementById('stale-notice')) return;
    const notice = document.createElement('div');
    notice.id = 'stale-notice';
    notice.className = 'flash info stale-notice';
    notice.setAttribute('role', 'status');
    notice.append('The lab changed since this page loaded. ');
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'btn btn-sm';
    button.textContent = 'Refresh';
    button.addEventListener('click', () => { dirty = false; refresh(); });
    notice.append(button);
    page.prepend(notice);
  }
  async function check() {
    const current = document.querySelector('main.page')?.dataset.uiVersion;
    if (!current || busy || document.visibilityState === 'hidden') return;
    const response = await fetch('/api/v1/ui-version', {cache:'no-store'});
    if (!response.ok) return;
    const {version} = await response.json();
    if (!version || version === document.querySelector('main.page')?.dataset.uiVersion) return;
    if (dirty || editing() || document.querySelector('dialog[open]')) staleNotice();
    else await refresh();
  }
  function watch() {
    if (watching) return;
    watching = true;
    rememberScroll();
    const edited = event => { if (event.target.closest?.('main.page form')) { dirty = true; editRevision++; } };
    document.addEventListener('input', edited);
    document.addEventListener('change', edited);
    // Submitting is the end of an edit, whichever script handles the form.
    document.addEventListener('submit', () => { dirty = false; }, true);
    // A background tab is not checked; coming back to it catches up at once.
    document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') check().catch(() => {}); });
    const loop = async () => {
      try { await check(); } catch (_) { /* The next check retries; live widgets show connection problems. */ }
      window.setTimeout(loop, WATCH_MS);
    };
    window.setTimeout(loop, WATCH_MS);
  }
  document.addEventListener('submit', async event => {
    const form = event.target;
    if (event.defaultPrevented || form.closest?.('#traffic-simulator-dialog, #scenario-configuration-dialog') || !(form instanceof HTMLFormElement)) return;
    if (form.method.toLowerCase() === 'get') {
      const url = new URL(form.getAttribute('action') || location.href, location.href);
      if (!isAppPage(url) || (form.target && form.target !== '_self')) return;
      event.preventDefault();
      url.search = new URLSearchParams(new FormData(form)).toString();
      if (event.submitter?.name) url.searchParams.append(event.submitter.name, event.submitter.value);
      navigate(url.href);
      return;
    }
    if (form.method.toLowerCase() !== 'post') return;
    const action = new URL(form.getAttribute('action') || location.href, location.href);
    if (action.origin !== location.origin || form.target === '_blank') return;
    event.preventDefault();
    if (busy) return;
    busy = true;
    const data = new FormData(form);
    if (event.submitter?.name) data.append(event.submitter.name, event.submitter.value);
    const buttons = [...form.querySelectorAll('button,input[type="submit"]')];
    const disabled = buttons.map(button => button.disabled);
    buttons.forEach(button => button.disabled = true);
    form.setAttribute('aria-busy', 'true');
    try { await update(await fetch(action.href, {method:'POST', body:data})); }
    catch (error) { displayError(error.message + ' Check live status before retrying; the action may have applied.'); }
    finally {
      buttons.forEach((button, i) => button.disabled = disabled[i]);
      form.removeAttribute('aria-busy');
      busy = false;
    }
  });
  return {mount, refresh, navigate, trackClient, notify};
})();
