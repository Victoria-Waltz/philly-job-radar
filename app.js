const $ = (selector, parent = document) => parent.querySelector(selector);
const all = (selector, parent = document) => [...parent.querySelectorAll(selector)];
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const form = $('#search-form');
form.inert = true;
const sortOptions = ['Best Match','Newest Posted','Recently Discovered','Closest','Highest Published Salary'];
const statuses = ['Interested','Applied','Interviewing','Rejected','Offer','Withdrawn'];
const options = values => values.map(v => Array.isArray(v) ? `<option value="${esc(v[0])}">${esc(v[1])}</option>` : `<option>${esc(v)}</option>`).join('');
const field = (name,label,type='text',placeholder='') => `<label class="field">${label}<input name="${name}" type="${type}" placeholder="${placeholder}" ${type==='number'?'min="0"':''}></label>`;
const select = (name,label,values) => `<label class="field">${label}<select name="${name}">${options(values)}</select></label>`;
const toggle = (name,label) => `<label class="toggle"><input name="${name}" type="checkbox">${label}</label>`;
const group = (name,label,values) => `<fieldset><legend>${label}</legend>${values.map(v=>`<label><input type="checkbox" name="${name}" value="${esc(v)}">${esc(v)}</label>`).join('')}</fieldset>`;
const groups = ['work','experience','employment'];
$('#basic').innerHTML = group('work','Work arrangement',['Remote','Hybrid','On-site','Unspecified'])
  + select('date','Date posted',[['any','Any time'],['1h','Past hour'],['24h','Past 24 hours'],['3d','Past 3 days'],['7d','Past 7 days'],['30d','Past month'],['custom','Custom range']])
  + '<div id="date-range" hidden>'+field('date_start','From','date')+field('date_end','Through','date')+'</div>'
  + toggle('unknown_date','Include unknown posting dates')
  + group('experience','Experience',['New Grad','Entry Level','Associate','Mid-Level','Senior','Unspecified'])
  + group('employment','Employment',['Full-time','Part-time','Internship','Contract','Temporary','Unspecified'])
  + field('start','Start month, season or year','text','June 2027')+toggle('unknown_start','Include unspecified start dates');
$('#advanced').innerHTML = field('must','Must include','text','Java, SQL')+field('exclude','Exclude','text','Internship, senior')
  + toggle('descriptions','Search descriptions as well as titles')
  + field('secondary','Secondary locations','text','Wayne, PA; Wilmington, DE')
  + select('scope','Location scope',[['exact','Exact city'],['metro','Metro area'],...['5','10','25','50','100'].map(n=>[n,n+' miles']),['nationwide','Nationwide']])
  + toggle('national_remote','Include nationwide remote jobs')+field('state','Remote eligibility state','text','PA')
  + field('max_years','Maximum required years','number')+field('grad','Graduation month','month')+toggle('unknown_grad','Include unspecified graduation windows')
  + field('companies','Include companies')+field('exclude_companies','Exclude companies')
  + field('salary','Minimum published annual salary (USD)','number')+toggle('unknown_salary','Include jobs without comparable salary')
  + field('industry','Industry or category')+field('source','Listing sources');
form.elements.sort.innerHTML = options(sortOptions);
let state = {}, jobs = [], activeName = '', loading = false, sequence = 0;
let filterTimer;
function toast(message) {
  const box = $('#toast');
  box.textContent = message;
  box.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { box.hidden = true; }, 4500);
}
async function api(path, data) {
  const response = await fetch('/api/'+path, data === undefined ? {} : {
    method:'POST', headers:{'Content-Type':'application/json'},body:JSON.stringify(data)
  });
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || 'Request failed');
  return value;
}
function filters() {
  const data = Object.fromEntries(groups.map(name => [name,[]]));
  for (const input of all('input,select',form).filter(input => input.name)) {
    if (groups.includes(input.name)) {
      if (input.checked) data[input.name].push(input.value);
    } else data[input.name] = input.type === 'checkbox' ? input.checked : input.value.trim();
  }
  return data;
}
function applyFilters(values) {
  form.reset();
  for (const input of all('input,select',form).filter(input => input.name)) {
    const value = values[input.name];
    if (groups.includes(input.name)) input.checked = (value || []).includes(input.value);
    else if (input.type === 'checkbox') input.checked = Boolean(value);
    else if (value !== undefined) input.value = value;
  }
  chips();
}
function chips() {
  const f = filters();
  $('#date-range').hidden = f.date !== 'custom';
  const values = Object.entries(f).flatMap(([k,v]) => {
    if (groups.includes(k)) return v.map(item=>[k,item,item]);
    if (['sort','any'].includes(k)||!v||v==='any'||v==='25'||k.startsWith('unknown_')) return [];
    const input = form.elements[k];
    const label = input.closest('label')?.textContent.trim() || k;
    return [[k,'',input.type==='checkbox'?label:input.tagName==='SELECT'?input.selectedOptions[0]?.textContent:v]];
  });
  $('#chips').innerHTML = values.map(([k,v,label])=>`<button type="button" class="pill chip" data-chip="${k}" data-value="${esc(v)}" aria-label="Remove ${esc(label)}">${esc(label)} ×</button>`).join('');
}
function displayDate(value) {
  if (!value) return '';
  const date = new Date(value.length===10 ? value+'T12:00:00' : value);
  return Number.isNaN(+date) ? '' : date.toLocaleDateString(undefined,{month:'short',day:'numeric',year:'numeric'});
}
function discoveryDate(value) {
  const date = new Date(value);
  return date.toDateString() === new Date().toDateString() ? 'today' : displayDate(value);
}
function freshness(job) {
  const value = job.posted_at;
  if (!value) return 'Posting date unavailable';
  if (job.precision === 'day') {
    const local = new Date();
    const today = [local.getFullYear(),String(local.getMonth()+1).padStart(2,'0'),String(local.getDate()).padStart(2,'0')].join('-');
    return value === today ? 'Posted today' : 'Posted '+displayDate(value);
  }
  const hours = (Date.now()-new Date(value))/3600000;
  const qualifier = job.posting_source==='Adzuna'?' · Provider-reported':'';
  if (hours >= 0 && hours < 24) return (hours<1?'Just posted':`Just posted · ${Math.floor(hours)}h ago`)+qualifier;
  return 'Posted '+displayDate(value)+qualifier;
}
function link(value) {
  try { return new URL(value).protocol === 'https:' ? esc(value) : ''; }
  catch { return ''; }
}
function tracker(job, record, mode) {
  return `<form class="tracker" data-track="${job.id}">
    ${mode==='applications' ? `<label class="field">Status<select name="status">${options(statuses)}</select></label>
    <label class="field">Application date<input name="date" type="date" value="${esc(record.date)}"></label>` : ''}
    <label class="field">Notes<textarea name="notes">${esc(record.notes)}</textarea></label>
    <div class="actions"><button>Save changes</button>${mode==='saved'&&!record.status?`<button type="button" data-action="track" data-id="${job.id}">Track application</button>`:''}</div></form>`;
}
function card(job, mode='search') {
  const record = state.tracking?.[job.id] || {};
  const place = [job.location||'Location unspecified',job.distance!=null?`${Math.round(job.distance)} mi`:''].filter(Boolean).join(' · ');
  const details = [job.experience==='Unspecified'?'Experience unspecified':job.experience+' (title-based)',job.employment!=='Unspecified'?job.employment:'',
    job.required!=null?job.required+' years required':'',job.preferred!=null?job.preferred+' years preferred':'',job.start||'Start date unspecified',job.salary].filter(Boolean).join(' · ');
  const original = link(job.link);
  const apply = link(job.apply || job.link);
  return `<article class="job"><div class="mark" aria-hidden="true">${esc(job.company.split(/\s+/).map(s=>s[0]).join('').slice(0,2).toUpperCase())}</div>
  <div><h3>${esc(job.title)}</h3><p class="company">${esc(job.company)}</p>
  <p class="meta">${esc(place)} <span class="pill">${esc(job.work)}</span></p><p class="meta">${esc(details)}</p>
  ${job.skills.length?`<p class="extra">Lists ${esc(job.skills.join(', '))}</p>`:''}
  ${[job.graduation,job.restriction,job.office].filter(Boolean).map(x=>`<p class="extra">${esc(x)}</p>`).join('')}
  <p class="extra">${esc(job.source)} · Found ${esc(discoveryDate(job.first_seen_at))}${job.availability!=='Open'?' · '+esc(job.availability):''}</p>
  ${mode!=='search'?tracker(job,record,mode):''}</div>
  <div class="job-side"><span class="pill">${esc(freshness(job))}</span><div class="actions">
  <button type="button" data-action="details" data-id="${job.id}">Details</button>
  ${original?`<a class="button" href="${original}" target="_blank" rel="noopener noreferrer">${job.employer?'View posting':'Provider listing'}</a>`:''}
  ${apply?`<a class="button primary" href="${apply}" target="_blank" rel="noopener noreferrer">Apply</a>`:''}
  <button type="button" data-action="save" data-id="${job.id}" aria-pressed="${Boolean(record.saved)}">${record.saved?'Saved':'Save'}</button>
  ${mode==='search'?`<button type="button" data-action="hide" data-id="${job.id}">Hide</button>`:''}</div></div></article>`;
}
const empty = text => `<div class="empty">${esc(text)}</div>`;
function render() {
  const entries = Object.entries(state.sources || {});
  const searched = entries.filter(([,s])=>s.state==='searched').length;
  const failed = entries.filter(([,s])=>s.state==='unavailable').length;
  const hasRun = entries.some(([,s])=>['searched','unavailable'].includes(s.state));
  const pending = entries.filter(([,s])=>s.state==='awaiting credentials').length;
  $('#refresh').disabled = loading;
  $('#refresh').textContent = loading ? 'Searching live sources…' : 'Search live jobs';
  $('#count').textContent = (state.count ?? jobs.length)+' matches';
  $('#notice').textContent = loading ? 'Refreshing sources. Previous results stay visible while the search runs.' : hasRun
    ? `${state.total ?? '—'} unique listings in the index · ${searched} sources searched${failed||pending?` · Partial coverage (${failed} unavailable, ${pending} awaiting credentials)`:''}`
    : 'Run a live search to load employer postings.';
  $('#cards').innerHTML = jobs.length ? jobs.map(job=>card(job)).join('')+(state.count>jobs.length?empty('Showing the first 250 matches. Refine the filters for a shorter list.'):'')
    : empty(hasRun?'No listings match these filters.':'Your matches will appear here after a live search.');
  $('#search-list').innerHTML = Object.entries(state.searches || {}).map(([name,item]) => `<article class="panel"><h3>${esc(name)}</h3>
    <p class="meta">${esc([item.filters.location,item.filters.scope==='nationwide'?'Nationwide':item.filters.scope,item.filters.work?.join(', '),item.filters.any].filter(Boolean).join(' · '))}</p>
    <p class="meta">${item.new} new matches · ${item.last_refresh?'Last refreshed '+displayDate(item.last_refresh):'Awaiting first refresh'}</p>
    <div class="actions"><button data-action="load" data-name="${esc(name)}">Open search</button><button data-action="remove-search" data-name="${esc(name)}">Remove</button></div></article>`).join('')||empty('Saved searches will appear here.');
  const tracked = Object.values(state.tracked_jobs || {});
  $('#saved-list').innerHTML = tracked.filter(j=>state.tracking[j.id].saved).map(j=>card(j,'saved')).join('')||empty('Save a listing to keep it here.');
  $('#application-list').innerHTML = tracked.filter(j=>state.tracking[j.id].status).map(j=>card(j,'applications')).join('')||empty('Track an application from a saved job or its details.');
  for (const element of all('[data-track] select')) element.value = state.tracking[element.form.dataset.track].status;
}
async function search(live=false,view=false) {
  if (live && loading) return;
  const token = ++sequence;
  if (live) {loading=true;render();}
  try {
    const value = await api(live?'search':'filter',{filters:filters(),name:activeName,view});
    if (token===sequence) {
      state={...state,...value};
      jobs=value.jobs;
    } else if (live) {
      await search();
    }
  } catch(error) { toast(error.message); }
  finally { if (live) loading=false;render(); }
}
function useTab(tab) {
  for (const name of ['search','searches','saved','applications']) $('#'+name).hidden=name!==tab;
  for (const button of all('[data-tab]')) {
    button.classList.toggle('active',button.dataset.tab===tab);
    button.setAttribute('aria-current',button.dataset.tab===tab?'page':'false');
  }
}
function dialog(title, content) {
  $('#dialog-title').textContent=title;
  $('#dialog-body').innerHTML=content;
  if (!$('#dialog').open) $('#dialog').showModal();
}
function sourceDialog() {
  const sources=Object.entries(state.sources || {});
  dialog('Search sources',`<p class="meta">Public employer boards work without keys. Set ADZUNA_APP_ID, ADZUNA_APP_KEY and BRAVE_SEARCH_API_KEY on the server to expand coverage.</p>
  ${sources.length?sources.map(([name,s])=>`<div class="source-row"><strong>${esc(name)}</strong> · ${esc(s.state)}${s.count!=null?' · '+s.count+' listings':''}${s.last_success?' · Last success '+esc(new Date(s.last_success).toLocaleString()):''}${s.detail?' · '+esc(s.detail):''}</div>`).join(''):'<p>Employer boards are configured. Run a search to see their status.</p>'}
  <h3>Add an employer board</h3><form id="board-form">${field('url','Greenhouse, Lever or Ashby URL','url','https://jobs.ashbyhq.com/company')}${field('name','Employer name')}<button class="primary">Add board</button></form>
  ${(state.boards||[]).map(([provider,slug,name])=>`<div class="source-row">${esc(name)} · ${esc(provider)} <button data-action="remove-board" data-url="https://${{'Greenhouse':'job-boards.greenhouse.io','Lever':'jobs.lever.co','Ashby':'jobs.ashbyhq.com'}[provider]}/${esc(slug)}">Remove</button></div>`).join('')}
  <details><summary>Hidden jobs</summary>${Object.values(state.tracked_jobs||{}).filter(j=>state.tracking[j.id].hidden).map(j=>`<div class="source-row">${esc(j.title)} · ${esc(j.company)} <button data-action="unhide" data-id="${j.id}">Show</button></div>`).join('')||'No hidden jobs.'}</details>`);
}
function changed() {
  activeName='';
  chips();
  clearTimeout(filterTimer);
  filterTimer=setTimeout(()=>search(),140);
}
form.addEventListener('input',changed);
form.addEventListener('submit',event=>{
  event.preventDefault();
  clearTimeout(filterTimer);
  search(true);
});
$('#close-dialog').onclick=()=>$('#dialog').close();
$('#sources-button').onclick=sourceDialog;
$('#open-filters').onclick=()=>{
  $('#filter-panel').classList.add('open');
  $('#close-filters').focus();
};
$('#close-filters').onclick=()=>{
  $('#filter-panel').classList.remove('open');
  $('#open-filters').focus();
};
document.addEventListener('keydown',event=>{
  const panel=$('#filter-panel');
  if(!panel.classList.contains('open')) return;
  if(event.key==='Escape') $('#close-filters').click();
  if(event.key!=='Tab') return;
  const controls=all('button,input,select,summary',panel).filter(control=>control.getClientRects().length);
  const edge=event.shiftKey?controls[0]:controls.at(-1);
  if(document.activeElement===edge){
    event.preventDefault();
    (event.shiftKey?controls.at(-1):controls[0]).focus();
  }
});
$('#clear').onclick=()=>{applyFilters({date:'any',scope:'25',sort:'Best Match',unknown_date:true});changed();};
$('#save-search').onclick=()=>dialog('Save this search',`<form id="name-form">${field('name','Search name','text','Philly Hybrid Software')}<button class="primary">Save search</button></form>`);
document.addEventListener('click',async event=>{
  const button=event.target.closest('button');
  if(!button) return;
  const {tab,chip,value,action,id,name}=button.dataset;
  if(tab) useTab(tab);
  if(chip) {
    for(const input of all(`[name="${chip}"]`,form)) {
      if(input.type==='checkbox') {if(!value||input.value===value)input.checked=false;}
      else if(input.tagName==='SELECT') input.selectedIndex=0;
      else input.value='';
    }
    changed();
  }
  if(!action) return;
  try {
    const job=jobs.find(j=>j.id===id)||state.tracked_jobs?.[id];
    const record=state.tracking?.[id]||{};
    if(action==='details'){
      dialog(job.title,`<p>${esc(job.company)} · ${esc(job.location)} · ${esc(job.work)}</p><p>${esc(freshness(job))} · Found ${esc(discoveryDate(job.first_seen_at))}</p>
      <p class="meta">Sources · ${esc(job.sources.join(', '))}</p><p class="meta">Last checked ${esc(displayDate(job.last_checked_at))}${job.updated_at?' · Source updated '+esc(displayDate(job.updated_at)):''}${job.expires_at?' · Closes '+esc(displayDate(job.expires_at)):''}</p>
      <p>${esc(job.description.slice(0,2500))}${job.description.length>2500?'…':''}</p><div class="actions">
      ${link(job.link)?`<a class="button" href="${link(job.link)}" target="_blank" rel="noopener noreferrer">Open listing</a>`:''}
      <button data-action="track" data-id="${id}">Track application</button></div>`);
    }
    if(action==='load'){
      activeName=name; applyFilters(state.searches[name].filters);useTab('search');await search(false,true);
    }
    if(action==='remove-search'){
      state={...state,...await api('saved-search',{name,remove:true})};if(activeName===name)activeName='';render();
    }
    if(action==='remove-board'){
      state={...state,...await api('board',{url:button.dataset.url,remove:true})};sourceDialog();
    }
    if(['save','hide','unhide','track'].includes(action)){
      const patch={save:{saved:!record.saved},hide:{hidden:true},unhide:{hidden:false},track:{status:record.status||'Interested'}}[action];
      state={...state,...await api('track',{id,...patch})};
      if(action==='track'){ $('#dialog').close();useTab('applications');render(); }
      else if(action==='hide'||action==='unhide'){await search();if(action==='unhide')sourceDialog();}
      else {render();toast(patch.saved?'Job saved':'Job removed from saved jobs');}
    }
  }catch(error){toast(error.message);}
});
document.addEventListener('submit',async event=>{
  const target=event.target;
  if(target===form) return;
  event.preventDefault();
  const data=Object.fromEntries(new FormData(target));
  try {
    if(target.id==='name-form'){
      state={...state,...await api('saved-search',{name:data.name,filters:filters()})};
      activeName=data.name.trim().slice(0,100);$('#dialog').close();render();toast('Search saved');
    }else if(target.id==='board-form'){
      state={...state,...await api('board',data)};sourceDialog();toast('Board added');
    }else if(target.dataset.track){
      state={...state,...await api('track',{id:target.dataset.track,...data})};render();toast('Changes saved');
    }
  }catch(error){toast(error.message);}
});
(async()=>{
  try {
    state=await api('state');
    activeName=state.searches['My May 2027 search']?'My May 2027 search':'';
    applyFilters(state.searches[activeName]?.filters||state.default);
    await search();
  } catch(error){toast(error.message);}
  finally { form.inert = false; }
})();
