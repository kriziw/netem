window.NetEmTrafficSettings = {
  mount() {
    let stopped = false, busy = false, dirty = false, wasBusy = false;
    const changed = event => { if (event.target.closest('main.page form')) dirty = true; };
    document.addEventListener('input', changed);
    document.addEventListener('change', changed);
    async function poll() {
      if (stopped || busy) return;
      busy = true;
      try {
        const response = await fetch('/api/v1/traffic/settings', {cache: 'no-store'});
        const state = await response.json();
        if (stopped) return;
        const status = document.querySelector('[data-executor-task]');
        if (status) status.textContent = response.ok
          ? [state.system?.job?.state, state.system?.job?.message || 'No administration task running.'].filter(Boolean).join(' · ')
          : 'Agent unavailable. ' + (state.error || 'Reconnecting…');
        const target = document.querySelector('[data-executor-target]');
        if (target && response.ok) target.textContent = state.system?.target?.job?.message || '';
        if (response.ok) {
          const nowBusy = Boolean(state.system?.busy || state.system?.workload_active);
          if (wasBusy && !nowBusy && !dirty) await NetEmActions.refresh();
          wasBusy = nowBusy;
        }
      } catch (_) {
        const status = document.querySelector('[data-executor-task]');
        if (status && !stopped) status.textContent = 'Agent unavailable. Reconnecting…';
      } finally { busy = false; }
    }
    poll();
    const timer = setInterval(poll, 3000);
    return {stop() { stopped = true; clearInterval(timer); document.removeEventListener('input', changed); document.removeEventListener('change', changed); }};
  }
};
