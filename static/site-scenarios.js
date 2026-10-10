window.NetEmSites = (() => {
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  function previewHtml(plan) {
    const load = plan.workload, targets = plan.targets;
    const total = Object.values(load.personas).reduce((sum, n) => sum + n, 0);
    const personas = Object.entries(load.personas).map(([name, count]) => '<div class="site-mix-row"><span>'+escape(name.replaceAll('_',' '))+'</span><progress max="100" value="'+(count / total * 100)+'"></progress><span>'+count+'</span></div>').join('');
    const apps = Object.entries(load.application_weights).map(([name, weight]) => '<span class="status info">'+escape(name.replaceAll('_',' '))+' ×'+weight+'</span>').join(' ');
    const mix = Object.entries(plan.application_mix || {}).sort((a,b) => b[1]-a[1]).map(([name, share]) => '<div class="site-mix-row"><span>'+escape(name.replaceAll('_',' '))+'</span><progress max="100" value="'+share+'"></progress><span>'+share.toFixed(1)+'%</span></div>').join('');
    const line = role => { const item = plan.wan_lines[role]; return '<p><strong>'+escape(role)+' WAN</strong> · '+escape(item.preset.toUpperCase())+' · '+item.download_mbit+' / '+item.upload_mbit+' Mbit/s</p>'; };
    return '<h3>'+escape(plan.label)+'</h3>'+plan.warnings.map(message => '<p class="docs-warning">'+escape(message)+'</p>').join('')+
      '<div class="grid grid-2"><div><h4>Workload</h4><p>'+load.employees+' employees · '+plan.start.users+' simulated users'+(load.scaled_to_limit ? ' (capped at 5,000)' : '')+'</p>'+personas+
      '<p class="field-help">Persona counts are relative shares when the user count is changed or capped.</p><p>Industry application emphasis</p><div class="site-apps">'+apps+'</div>'+(mix ? '<details><summary>Application selection mix</summary>'+mix+'<p class="field-help">Expected selection shares from the simulator catalog. Actual traffic rates also depend on persona activity and response time.</p></details>' : '')+'</div>'+
      '<div><h4>Pass targets</h4><p>Experience ≥ '+targets.experience_min+' / 100<br>Request success ≥ '+targets.success_min_pct+'%<br>Interactive P95 ≤ '+targets.interactive_p95_max_ms+' ms<br>Steering ≤ '+targets.steering_max_s+' s<br>Voice/video judging: '+escape(targets.media_mode)+'</p>'+line('primary')+line('backup')+
      '<p class="field-help">'+plan.wan_lines.notes.map(escape).join(' ')+'</p></div></div>';
  }
  function testsHtml(tests) {
    return tests.map((test, index) => '<div class="site-test"><div><strong>'+(index+1)+'. '+escape(test.name)+'</strong><p class="field-help">'+escape(test.description)+' · '+escape(test.role)+' WAN</p></div><button class="btn btn-sm" type="submit" name="test_id" value="'+escape(test.id)+'" data-site-run disabled>Run test</button></div>').join('');
  }
  function resultsHtml(state) {
    if (!state?.tests?.length) return '';
    return '<h4>'+escape(state.label)+' · '+escape(state.active ? 'Running' : state.result || '')+'</h4>'+state.tests.map(test => '<p><span class="status '+({passed:'good',failed:'bad',running:'info',stopped:'warn'}[test.status] || 'info')+'">'+escape(test.status.toUpperCase())+'</span> '+escape(test.name)+(test.error ? ' · '+escape(test.error) : '')+'</p>').join('');
  }
  const FIELDS = ['industry','sub_industry','function','size','criticality'];
  const DEFAULT_SELECTION = {industry:'manufacturing',sub_industry:'automotive',function:'plant',size:'large',criticality:'business_critical'};

  // Fills the five site selects of a form; sub-industries and site functions follow the industry.
  function selectors(form, options, initial, onChange) {
    const selects = Object.fromEntries(FIELDS.map(field => [field, form.elements[field]]));
    const values = () => Object.fromEntries(FIELDS.map(field => [field, selects[field].value]));
    const populate = (select, entries, preferred) => {
      select.replaceChildren(...entries.map(([value, label]) => { const option = select.ownerDocument.createElement('option'); option.value = value; option.textContent = label; return option; }));
      if (entries.some(([value]) => value === preferred)) select.value = preferred;
    };
    const cascade = preferred => {
      const industry = options.industries[selects.industry.value];
      populate(selects.sub_industry, Object.entries(industry.sub_industries).map(([key,item]) => [key,item.label]), preferred.sub_industry);
      populate(selects.function, industry.functions.map(key => [key,options.functions[key].label]), preferred.function);
    };
    const start = initial || DEFAULT_SELECTION;
    populate(selects.industry, Object.entries(options.industries).map(([key,item]) => [key,item.label]), start.industry);
    cascade(start);
    populate(selects.size, Object.entries(options.sizes), start.size);
    populate(selects.criticality, Object.entries(options.criticality).map(([key,item]) => [key,item.label]), start.criticality);
    selects.industry.addEventListener('change', () => { cascade(values()); onChange?.(); });
    FIELDS.filter(field => field !== 'industry').forEach(field => selects[field].addEventListener('change', () => onChange?.()));
    return {selects, values};
  }

  function mount(options, active, state) {
    const form = document.getElementById('site-selection');
    if (!form) return;
    const fields = FIELDS;
    const run = document.getElementById('site-run');
    let serial = 0, controller, disposed = false, ready = false, preview;
    const {selects, values} = selectors(form, options, active?.selection, () => refresh());
    function controls() {
      const saved = ready && active && fields.every(field => active.selection[field] === selects[field].value);
      const busy = run.dataset.busy === 'true';
      const distinct = run.elements.primary_link.value && run.elements.backup_link.value && run.elements.primary_link.value !== run.elements.backup_link.value;
      run.querySelectorAll('[data-site-action]').forEach(button => button.disabled = !saved || busy || !distinct);
      run.querySelectorAll('[data-site-run]').forEach(button => button.disabled = !saved || busy || !distinct || run.dataset.connected !== 'true');
      document.getElementById('site-save').disabled = !ready || busy;
      document.getElementById('site-save-hint').textContent = saved ? 'This is the active site.' : 'Save this selection to activate its targets and tests.';
      document.getElementById('site-run-hint').textContent = !saved ? 'Save the previewed site before applying WAN lines or running tests.' : busy ? 'A test is running.' : !distinct ? 'Choose two different WANs for primary and backup.' : run.dataset.connected !== 'true' ? 'Connect the Traffic Simulator to run site tests.' : 'Ready to run the active site.';
    }
    async function refresh() {
      const revision = ++serial;
      controller?.abort(); controller = new AbortController(); ready = false; controls();
      document.getElementById('site-criticality-help').textContent = options.criticality[selects.criticality.value].description;
      document.getElementById('site-preview').setAttribute('aria-busy','true');
      try {
        const response = await fetch('/api/v1/site-plan?'+new URLSearchParams(values()), {cache:'no-store', signal:controller.signal});
        const plan = await response.json();
        if (disposed || revision !== serial) return;
        if (!response.ok) throw new Error(plan.error || 'Site preview unavailable.');
        preview = plan; ready = true;
        document.getElementById('site-preview').innerHTML = previewHtml(plan);
        document.getElementById('site-test-list').innerHTML = testsHtml(plan.tests);
        run.elements.users.value = plan.start.users;
      } catch (error) {
        if (disposed || revision !== serial || error.name === 'AbortError') return;
        document.getElementById('site-preview').textContent = error.message;
      } finally { if (!disposed && revision === serial) { document.getElementById('site-preview').removeAttribute('aria-busy'); controls(); } }
    }
    run.addEventListener('change', controls);
    document.getElementById('site-results').innerHTML = resultsHtml(state);
    window.NetEmActions?.trackClient({stop() { disposed = true; controller?.abort(); }});
    refresh();
    return {update(next) { document.getElementById('site-results').innerHTML = resultsHtml(next); }};
  }
  return {previewHtml, testsHtml, resultsHtml, mount, selectors};
})();
