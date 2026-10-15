window.NetEmComponentUpdates = {
  mount({request = fetch, schedule = setTimeout} = {}) {
    const root = document.querySelector('[data-update-cards]');
    if (!root) return {stop() {}};
    const checkButton = document.querySelector('[data-all-update-check] button');
    const notice = document.querySelector('[data-component-check-status]');
    let stopped = false, signature = root.innerHTML;

    function bindInstall() {
      const form = root.querySelector('[data-update-form]');
      if (!form) return;
      form.addEventListener('submit', event => {
        event.preventDefault();
        if (confirm('Install the available stable update and restart NetEm?')) window.NetEmUpdate.run(form);
      });
    }

    async function poll() {
      if (stopped || !root.isConnected) return;
      if (document.body.classList.contains('update-active')) return;
      try {
        const response = await request(root.dataset.componentsUrl, {cache: 'no-store'});
        if (!response.ok) throw new Error('Update status unavailable. Retrying…');
        const data = await response.json();
        if (stopped) return;
        const fresh = new DOMParser().parseFromString(data.html, 'text/html').querySelector('[data-update-cards]');
        if (!fresh) throw new Error('Update status unavailable. Retrying…');
        const html = fresh.innerHTML;
        // Do not replace a submitted install form or interrupt its progress screen.
        if (!root.querySelector('form[aria-busy="true"]') && !document.body.classList.contains('update-active') && signature !== html) {
          const open = [...root.querySelectorAll('details')].map(detail => detail.open);
          root.replaceChildren(...fresh.childNodes);
          root.querySelectorAll('details').forEach((detail, index) => detail.open = Boolean(open[index]));
          signature = html;
          bindInstall();
        }
        checkButton.disabled = Boolean(data.checks.running);
        const failures = Object.values(data.checks.components || {}).filter(item => item.state === 'error').length;
        notice.textContent = data.checks.running ? 'Checking all three components…' : failures
          ? 'Check finished. Review the component messages below.' : Object.keys(data.checks.components || {}).length ? 'All components checked.' : '';
      } catch (error) {
        if (!stopped) {
          notice.textContent = error.message || 'Update status unavailable. Retrying…';
          root.querySelectorAll('.card-head .status').forEach(badge => { badge.textContent = 'Unavailable'; badge.className = 'status bad'; });
          root.querySelectorAll('button').forEach(button => button.disabled = true);
          signature = ''; // A successful retry must restore the controls even if the releases did not change.
        }
      }
      if (!stopped) schedule(poll, 2000);
    }
    bindInstall();
    schedule(poll, 2000);
    return {stop() { stopped = true; }};
  }
};
