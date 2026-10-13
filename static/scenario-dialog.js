// Configure scenarios without leaving the live Command Center.
(() => {
  const dialog = document.getElementById('scenario-configuration-dialog');
  if (!dialog) return;
  const content = dialog.querySelector('[data-scenario-content]');
  const feedback = dialog.querySelector('[data-scenario-feedback]');
  let generation = 0, pending = false, opener;
  const status = (message, error = false) => {
    feedback.textContent = message;
    feedback.classList.toggle('bad-text', error);
  };
  function close() {
    generation++;
    pending = false;
    dialog.close();
    if (opener?.isConnected) opener.focus();
  }
  document.addEventListener('click', async event => {
    const link = event.target.closest('[data-scenario-modal]');
    if (!link || event.defaultPrevented || event.button > 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    const url = new URL(link.dataset.scenarioModal, location.href);
    if (url.origin !== location.origin || typeof dialog.showModal !== 'function') return;
    event.preventDefault();
    opener = link;
    const token = ++generation;
    pending = true;
    content.replaceChildren();
    document.getElementById('scenario-dialog-title').textContent = 'Configure scenario';
    status('Loading scenario…');
    if (!dialog.open) dialog.showModal();
    try {
      const response = await fetch(url.href, {cache: 'no-store'});
      if (!response.ok) throw new Error('Scenario configuration could not be loaded. Try again.');
      const doc = new DOMParser().parseFromString(await response.text(), 'text/html');
      if (token !== generation || !dialog.open) return;
      const configuration = doc.querySelector('[data-scenario-configuration]');
      if (!configuration) throw new Error('Scenario configuration is unavailable. Try again.');
      content.replaceChildren(configuration);
      document.getElementById('scenario-dialog-title').textContent = configuration.querySelector('.test-card-title')?.textContent || 'Configure scenario';
      status('');
      content.querySelector('select:not(:disabled), input:not([type="hidden"]):not(:disabled)')?.focus();
    } catch (error) {
      if (token === generation && dialog.open) status(error.message, true);
    } finally {
      if (token === generation) pending = false;
    }
  });
  dialog.querySelector('[data-scenario-close]').addEventListener('click', close);
  document.addEventListener('netem:navigated', () => { if (dialog.open) close(); });
  dialog.addEventListener('cancel', () => { generation++; pending = false; });
  dialog.addEventListener('click', event => {
    if (event.target !== dialog) return;
    const box = dialog.getBoundingClientRect();
    if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) close();
  });
  dialog.addEventListener('submit', async event => {
    if (event.defaultPrevented || !(event.target instanceof HTMLFormElement)) return;
    event.preventDefault();
    event.stopPropagation();
    if (pending) return;
    const form = event.target;
    const action = new URL(form.getAttribute('action'), location.href);
    if (action.origin !== location.origin || action.pathname !== '/lab/scenario/start') {
      status('Scenario controls must use this NetEm instance.', true);
      return;
    }
    const data = new FormData(form);
    if (event.submitter?.name) data.append(event.submitter.name, event.submitter.value);
    data.set('return_to', 'overview');
    const token = generation;
    pending = true;
    const buttons = [...form.querySelectorAll('button')];
    const disabled = buttons.map(button => button.disabled);
    buttons.forEach(button => button.disabled = true);
    form.setAttribute('aria-busy', 'true');
    status('Starting scenario…');
    try {
      const response = await fetch(action.href, {method: 'POST', body: data});
      const doc = new DOMParser().parseFromString(await response.text(), 'text/html');
      const warning = doc.querySelector('#no-session-warning');
      if (warning) {
        // No lab session is running: the operator starts one, or acknowledges the warning and runs the test.
        if (token === generation && dialog.open) {
          content.replaceChildren(document.importNode(warning, true));
          document.getElementById('scenario-dialog-title').textContent = 'No session running';
          status('');
        }
        return;
      }
      const errors = [...doc.querySelectorAll('.flash.error')].map(el => el.textContent.trim());
      if (!response.ok || errors.length) throw new Error(errors.join(' ') || 'The scenario could not be started. Check the configuration and retry.');
      const started = doc.querySelector('#running-test')?.dataset.scenarioId === data.get('scenario_id');
      if (!started) throw new Error('The start was not confirmed. Check live status before retrying.');
      const message = [...doc.querySelectorAll('.flash.success')].map(el => el.textContent.trim()).join(' ') || 'Scenario started.';
      if (token === generation && dialog.open) close();
      window.NetEmActions?.notify(message);
      await window.NetEmActions?.refresh();
    } catch (error) {
      if (token === generation && dialog.open) status(error.message + (error instanceof TypeError ? ' Check live status before retrying; the start may have applied.' : ''), true);
    } finally {
      buttons.forEach((button, index) => button.disabled = disabled[index]);
      form.removeAttribute('aria-busy');
      if (token === generation) pending = false;
    }
  });
})();
