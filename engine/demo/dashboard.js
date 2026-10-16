const $ = id => document.getElementById(id);
let workspace = localStorage.getItem('harness-workspace');
if (!/^[a-f0-9]{32}$/.test(workspace || '')) { workspace = crypto.randomUUID().replaceAll('-', ''); localStorage.setItem('harness-workspace', workspace); }
let state, taskBusy = false;
$('mission').value = localStorage.getItem('harness-mission') || $('mission').value;
$('mission').oninput = () => localStorage.setItem('harness-mission', $('mission').value);
const el = (tag, text, cls) => { const n = document.createElement(tag); if (text !== undefined) n.textContent = text; if (cls) n.className = cls; return n; };
const date = stamp => new Date(stamp * 1000).toLocaleString();
async function api(path, body) {
  const r = await fetch(path, {method: body ? 'POST' : 'GET', headers: {'Content-Type':'application/json', 'X-Workspace':workspace}, body: body ? JSON.stringify(body) : undefined});
  const data = await r.json(); if (!r.ok) throw Error(data.error || 'Request failed'); return data;
}
function error(e) { $('global-error').textContent = e.message; $('global-error').hidden = false; }
function safeLink(url, text) { const a = el('a', text); if (/^https?:\/\//i.test(url || '')) { a.href = url; a.target = '_blank'; a.rel = 'noopener noreferrer'; } return a; }
function route() { const name = location.hash.slice(1) || 'overview'; document.querySelectorAll('.page').forEach(p => p.hidden = p.id !== (['overview','ingestion','memory','validation'].includes(name) ? name : 'overview')); document.querySelectorAll('[data-page]').forEach(a => { if(a.dataset.page === name) a.setAttribute('aria-current','page'); else a.removeAttribute('aria-current'); }); }
window.addEventListener('hashchange', route); route();
function empty(root, message) { root.replaceChildren(el('p', message, 'empty')); }
function renderSignals() {
  const root = $('signals'), query = $('signal-search').value.toLowerCase(); root.replaceChildren();
  const rows = state.signals.filter(s => [s.text,s.author,s.source].join(' ').toLowerCase().includes(query));
  $('signal-count').textContent = `${state.signals.length} saved`;
  if (!rows.length) return empty(root, state.signals.length ? 'No signals match this search.' : 'No signals yet. Start with the sample source or import your own.');
  for (const s of rows) { const item=el('article',undefined,'item'), meta=el('div',undefined,'meta'); meta.append(el('span',s.source,'tag'),el('span',s.author || 'Unknown author'),el('span',s.created_at || 'Date not supplied')); item.append(meta,el('p',s.text)); const metrics=Object.entries(s.metrics).map(([k,v])=>`${k}: ${v}`).join(' · '); if(metrics)item.append(el('p',metrics,'hint')); if(s.url)item.append(safeLink(s.url,'Source ↗')); const details=el('details'), summary=el('summary','Normalized signal'); details.append(summary,el('pre',JSON.stringify(s,null,2))); item.append(details); root.append(item); }
}
function renderMemory() {
  const root=$('notes'), query=$('memory-search').value.toLowerCase(), kind=$('memory-kind').value; root.replaceChildren();
  $('note-count').textContent=`${state.notes.length} records`;
  const notes=state.notes.filter(n=>(kind==='all'||n.kind===kind)&&n.text.toLowerCase().includes(query)).slice().reverse();
  if(!notes.length)return empty(root,'No matching memory records. Save guidance or run a demo round.');
  for(const note of notes){const item=el('article',undefined,'item'),meta=el('div',undefined,'meta');meta.append(el('span',note.kind,'tag'),el('span',`${note.origin} · ${note.status}`),el('span',date(note.at)));item.append(meta,el('p',note.text));root.append(item);}
}
function renderRound() {
  const run=state.runs.at(-1), root=$('round-detail'); $('drafts').replaceChildren();
  if(!run){$('round-label').textContent='No run yet';empty(root,'Ingest signals, then run a round to inspect the pipeline.');return;}
  $('round-label').textContent=`${run.journal.status} · mock`;root.replaceChildren();
  for(const stage of run.journal.stages){const row=el('div',undefined,'stage');row.append(el('span',stage.name.replaceAll('_',' ')),el('span',`${stage.seconds.toFixed(3)}s`));root.append(row);}
  $('memory-context').textContent=run.memory_input || '(no demo memory was available for this round)';
  const details=el('details');details.append(el('summary',`${run.narratives.length} selected narratives`),el('pre',JSON.stringify(run.narratives,null,2)));root.append(details);
  $('drafts').append(el('div','Generated drafts','section-head'));
  for(const action of run.actions){const item=el('article',undefined,'item');item.append(el('div',`${action.mind} · ${action.type} · ${action.vetoed?'vetoed':'kept'} · mock score ${action.predicted_score}`,'meta'),el('p',action.text));const button=el('button','Inspect this draft →','link');button.onclick=()=>{$('post').value=action.text;$('lane').value=['post','reply','quote'].includes(action.type)?action.type:'post';$('post-mission').value=run.mission || $('mission').value;$('context').value=state.signals.map(s=>s.text).join('\n').slice(0,8000);$('validation-result').replaceChildren();$('judge-result').replaceChildren();location.hash='validation';};item.append(button);$('drafts').append(item);}
}
async function refresh() {
  state=await api('/api/dashboard');
  $('bridge-status').textContent=state.bridge_online?'● Engine bridge connected':'○ Engine bridge offline';$('bridge-status').className=state.bridge_online?'online':'';
  const counts=[[state.signals.length,'ingested signals'],[state.notes.length,'memory records'],[state.runs.length,'demo rounds'],[state.runs.at(-1)?.actions.length||0,'latest drafts']];$('stats').replaceChildren(...counts.map(([n,label])=>{const x=el('div',undefined,'stat');x.append(el('strong',String(n)),el('span',label));return x;}));
  renderSignals();renderMemory();renderRound();$('batches').replaceChildren(...state.ingestions.slice().reverse().map(b=>el('p',`${date(b.at)} · ${b.source}: ${b.received} received, ${b.added} added, ${b.duplicates} duplicates.`,'hint')));
  const judge=await api('/api/status');$('judge-status').textContent=judge.worker_online?'● Claude judge connected':'○ Claude judge offline';$('judge-status').className=judge.worker_online?'online':'';
}
async function task(payload){
  if(taskBusy)throw Error('Wait for the current ingestion or round to finish.'); taskBusy=true;$('run').disabled=true;$('ingest').disabled=true;$('task-status').hidden=false;
  try{const job=await api('/api/dashboard/tasks',payload);sessionStorage.setItem('harness-task',job.id);return await pollTask(job.id);}finally{taskBusy=false;$('run').disabled=false;$('ingest').disabled=false;}
}
async function pollTask(id){
  for(let i=0;i<100;i++){const job=await api('/api/dashboard/tasks/'+id);$('task-status').textContent=job.status==='queued'?'Waiting for the dashboard worker…':'Running the source or engine…';if(job.status==='failed'){sessionStorage.removeItem('harness-task');throw Error(job.result.error);}if(job.status==='done'){sessionStorage.removeItem('harness-task');$('task-status').textContent='Completed. Your workspace is updated.';await refresh();return job.result;}await new Promise(r=>setTimeout(r,2000));}throw Error('Still waiting. Refresh this page to reconnect to the task.');
}
$('source').onchange=()=>{$('json-input').hidden=$('source').value!=='manual';};
$('ingest').onclick=async()=>{try{$('global-error').hidden=true;const source=$('source').value;if(source==='manual'){const report=await api('/api/dashboard/ingest',{signals:JSON.parse($('signals-json').value)});$('ingest-report').textContent=`${report.added} added · ${report.duplicates} duplicates skipped`;$('ingest-report').hidden=false;await refresh();}else await task({kind:'ingest',source,mission:$('mission').value});}catch(e){error(e);}};
$('run').onclick=async()=>{try{$('global-error').hidden=true;await task({kind:'run',mission:$('mission').value});}catch(e){error(e);}};
$('note-form').onsubmit=async e=>{e.preventDefault();try{await api('/api/dashboard/notes',{kind:$('note-kind').value,text:$('note-text').value});$('note-text').value='';await refresh();}catch(e){error(e);}};
$('signal-search').oninput=renderSignals;$('memory-search').oninput=renderMemory;$('memory-kind').onchange=renderMemory;
function candidate(){return {text:$('post').value,mission:$('post-mission').value,context:$('context').value,lane:$('lane').value};}
$('validation-form').onsubmit=async e=>{e.preventDefault();try{const result=await api('/api/validate',candidate()),root=$('validation-result');root.className='list';root.replaceChildren(el('h2',result.status.replaceAll('_',' ')));for(const c of result.checks){const row=el('div',undefined,'check'),body=el('div');body.append(el('strong',c.name),el('p',c.detail));row.append(el('span',c.status.toUpperCase(),c.status),body);root.append(row);}root.append(el('p',result.note,'hint'));}catch(e){error(e);}};
$('judge').onclick=async()=>{if(!$('validation-form').reportValidity())return;$('judge').disabled=true;const root=$('judge-result');root.replaceChildren(el('p','Waiting for the model judge…','notice'));try{const job=await api('/api/jobs',candidate());for(let i=0;i<180;i++){await new Promise(r=>setTimeout(r,2000));const j=await api('/api/jobs/'+job.id);if(j.status==='failed')throw Error(j.result.error);if(j.status==='done'){const r=j.result;root.replaceChildren(el('div',`${r.score_100}/100 · ${r.grade.verdict} · ${r.grade.confidence} confidence`,'section-head'));for(const key of ['relevance','clarity','specificity','engagement_potential','grounding']){const c=r.grade[key],item=el('div',undefined,'item');item.append(el('h3',`${key.replaceAll('_',' ')} · ${c.score}/5`),el('p',c.reason));root.append(item);}root.append(el('h3','Suggestions'));for(const tip of r.grade.improvements)root.append(el('p',tip));const details=el('details');details.append(el('summary','Reference comparisons and full evaluation'),el('pre',JSON.stringify(r,null,2)));root.append(details);return;}}throw Error('The judge is taking longer than expected.');}catch(e){root.replaceChildren(el('p',e.message,'error'));}finally{$('judge').disabled=false;}};
refresh().then(async()=>{const pending=sessionStorage.getItem('harness-task');if(pending){$('task-status').hidden=false;taskBusy=true;$('run').disabled=true;$('ingest').disabled=true;try{await pollTask(pending);}finally{taskBusy=false;$('run').disabled=false;$('ingest').disabled=false;}}}).catch(error);
