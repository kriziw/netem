// Same-origin simulator controls: keep the live WAN page mounted behind the dialog.
(() => {
  const dialog=document.getElementById('traffic-simulator-dialog');
  if(!dialog)return;
  const content=dialog.querySelector('[data-simulator-content]');
  const status=dialog.querySelector('[data-simulator-feedback]');
  let currentURL, mode='workload', busy=false, generation=0, timer, previousState;
  const setStatus=(text,error=false)=>{status.textContent=text;status.classList.toggle('bad-text',error);};
  function close(){generation++;clearInterval(timer);dialog.close();}
  function extract(doc){
    const marker=doc.getElementById(mode==='integration'?'traffic-simulator':'corporate-traffic');
    const section=marker?.nextElementSibling;
    if(!section)throw new Error('Simulator controls could not be loaded.');
    return mode==='integration'?section.querySelector('section.card'):section;
  }
  async function render(response,token){
    if(!response.ok)throw new Error('Request failed ('+response.status+').');
    const text=await response.text();if(token!==generation||!dialog.open)return;
    const doc=new DOMParser().parseFromString(text,'text/html');
    // A workload without an integration offers configuration in this same dialog.
    if(mode==='workload'&&!doc.querySelector('#corporate-traffic + section form')){
      const configure=doc.querySelector('#corporate-traffic + section a[href*="integrations"]');
      if(configure){mode='integration';currentURL=new URL(configure.getAttribute('href'),location.href).href;
        return render(await fetch(currentURL,{cache:'no-store'}),token);}
    }
    const section=extract(doc);
    if(!section)throw new Error('Simulator configuration could not be loaded.');
    if(token!==generation||!dialog.open)return;
    content.replaceChildren(section);
    const messages=[...doc.querySelectorAll('.flash')].map(el=>el.textContent.trim());
    setStatus(messages.join(' '));
    previousState=section.querySelector('[data-tg-status]')?.textContent.trim().toLowerCase();
    dialog.querySelector('[data-simulator-title]').textContent=mode==='integration'?'Configure Traffic Simulator':'Traffic Simulator controls';
  }
  async function load(url){
    if(busy)return;
    const destination=new URL(url,location.href);
    if(destination.origin!==location.origin)return;
    currentURL=destination.href;busy=true;const token=generation;
    setStatus('Loading…');
    try{await render(await fetch(currentURL,{cache:'no-store'}),token);}
    catch(error){if(token===generation)setStatus(error.message,true);}
    finally{busy=false;}
  }
  document.addEventListener('click',event=>{
    const link=event.target.closest('[data-simulator-modal]');if(!link)return;
    event.preventDefault();if(busy)return;
    mode=link.dataset.simulatorModal||'workload';generation++;
    dialog.showModal();content.replaceChildren();load(link.getAttribute('href'));
    clearInterval(timer);timer=setInterval(poll,2500);
  });
  dialog.querySelector('[data-simulator-close]').addEventListener('click',close);
  dialog.addEventListener('cancel',()=>{generation++;clearInterval(timer);});
  dialog.addEventListener('click',event=>{if(event.target!==dialog)return;const r=dialog.getBoundingClientRect();
    if(event.clientX<r.left||event.clientX>r.right||event.clientY<r.top||event.clientY>r.bottom)close();});
  content.addEventListener('click',event=>{
    const link=event.target.closest('a[href]');if(!link||link.hasAttribute('data-simulator-modal'))return;
    const url=new URL(link.getAttribute('href'),location.href);
    if(url.origin===location.origin&&url.pathname==='/integrations'){event.preventDefault();if(busy)return;mode='integration';load(url.href);}
  });
  content.addEventListener('change',event=>{
    if(event.target.id!=='traffic-generator-discovered')return;
    const option=event.target.selectedOptions[0];if(!option||option.value==='manual')return;
    for(const [id,key,fallback] of [['port','port',8443],['instance','name',''],['version','version',''],['fingerprint','fingerprint','']]){
      const input=content.querySelector('#traffic-generator-'+id);if(input)input.value=option.dataset[key]||fallback;
    }
  });
  content.addEventListener('submit',async event=>{
    if(event.defaultPrevented)return;
    const form=event.target;if(!(form instanceof HTMLFormElement))return;
    event.preventDefault();event.stopPropagation();if(busy)return;
    const action=new URL(form.getAttribute('action')||currentURL,location.href);
    if(action.origin!==location.origin){setStatus('Simulator controls must use this NetEm instance.',true);return;}
    busy=true;const token=generation,data=new FormData(form);
    if(event.submitter?.name)data.append(event.submitter.name,event.submitter.value);
    const buttons=[...content.querySelectorAll('button')],disabled=buttons.map(b=>b.disabled);buttons.forEach(b=>b.disabled=true);
    setStatus('Applying…');
    try{await render(await fetch(action.href,{method:'POST',body:data}),token);}
    catch(error){if(token===generation)setStatus(error.message+' Check live status before retrying; the action may have applied.',true);}
    finally{buttons.forEach((b,i)=>b.disabled=disabled[i]);busy=false;}
  });
  async function poll(){
    if(!dialog.open||busy||mode!=='workload')return;const token=generation;
    try{
      const response=await fetch('/api/v1/traffic-generator',{cache:'no-store'});if(!response.ok)return;
      const snapshot=await response.json();if(token!==generation||!dialog.open)return;
      const state=snapshot.connected?snapshot.status?.status:'disconnected';
      if(previousState&&previousState!==state){await load(currentURL);return;}
      const data=snapshot.status||{},dem=data.dem||{};
      for(const [selector,value] of [['status',String(state||'unknown').toUpperCase()],['users',data.users??0],['score',dem.experience_score??'—'],
        ['availability',dem.availability_pct==null?'—':Number(dem.availability_pct).toFixed(2)+'%'],['p95',dem.p95_ms==null?'—':Number(dem.p95_ms).toFixed(0)+' ms']])
        content.querySelectorAll('[data-tg-'+selector+']').forEach(el=>el.textContent=value);
    }catch(_){}
  }
})();
