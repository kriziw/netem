window.NetEmActions = (() => {
  let cleanups = [];
  let busy = false;
  function trackClient(client) { cleanups.push(() => client.stop()); }
  function cleanup() {
    cleanups.splice(0).forEach(stop => stop());
  }
  function mount() {
    const template = document.getElementById('page-scripts');
    if (!template) return;
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
    template.content.querySelectorAll('script:not([src])').forEach(script => {
      new Function('document', 'setInterval', 'setTimeout', script.textContent)(scopedDocument, interval, timeout);
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
  async function update(response) {
    if (!response.ok) throw new Error('Action failed (' + response.status + ').');
    const next = new DOMParser().parseFromString(await response.text(), 'text/html');
    const page = next.querySelector('main.page');
    const scripts = next.getElementById('page-scripts');
    if (!page || !scripts) throw new Error('Unexpected response. Check the service before retrying.');
    const openDrawers = [...document.querySelectorAll('.drawer.open')].map(el => el.id);
    const scroll = window.scrollY;
    const chartEmpty = document.getElementById('throughput-empty');
    if (chartEmpty && page.querySelector('#throughput-empty'))
      page.querySelector('#throughput-empty').style.display = chartEmpty.style.display;
    cleanup();
    // Keep the rendered chart geometry and its scale while controls update around it.
    page.querySelectorAll('[data-chart],[data-spark],.chart-y-axis,.chart-x-axis').forEach(element => {
      const key = element.id ? '#' + CSS.escape(element.id) : element.hasAttribute('data-chart')
        ? '[data-chart="' + CSS.escape(element.dataset.chart) + '"]'
        : '[data-spark="' + CSS.escape(element.dataset.spark) + '"]';
      const old = document.querySelector(key);
      if (old) element.replaceWith(old);
    });
    document.querySelector('main.page').replaceWith(page);
    document.getElementById('page-scripts').replaceWith(scripts);
    const url = new URL(response.url || location.href);
    if (url.origin === location.origin) history.replaceState(null, '', url.pathname + url.search + location.hash);
    document.title = next.title;
    mount();
    openDrawers.forEach(id => document.getElementById(id)?.classList.add('open'));
    if (openDrawers.length) document.querySelector('.drawer-backdrop')?.classList.add('open');
    window.scrollTo(0, scroll);
  }
  async function refresh() {
    if (busy) return;
    busy = true;
    try { await update(await fetch(location.href, {cache:'no-store'})); }
    catch (error) { displayError(error.message); }
    finally { busy = false; }
  }
  document.addEventListener('submit', async event => {
    const form = event.target;
    if (event.defaultPrevented || !(form instanceof HTMLFormElement) || form.method.toLowerCase() !== 'post') return;
    if (new URL(form.action).origin !== location.origin || form.target === '_blank') return;
    event.preventDefault();
    if (busy) return;
    busy = true;
    const data = new FormData(form);
    if (event.submitter?.name) data.append(event.submitter.name, event.submitter.value);
    const buttons = [...form.querySelectorAll('button,input[type="submit"]')];
    const disabled = buttons.map(button => button.disabled);
    buttons.forEach(button => button.disabled = true);
    form.setAttribute('aria-busy', 'true');
    try { await update(await fetch(form.action, {method:'POST', body:data})); }
    catch (error) { displayError(error.message + ' Check live status before retrying; the action may have applied.'); }
    finally {
      buttons.forEach((button, i) => button.disabled = disabled[i]);
      form.removeAttribute('aria-busy');
      busy = false;
    }
  });
  return {mount, refresh, trackClient};
})();
