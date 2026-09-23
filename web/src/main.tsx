import React, { useEffect, useMemo, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { onAuthStateChanged, type User } from 'firebase/auth';
import { auth, db, signIn, logOut, collection, doc, onSnapshot, query, orderBy, limit, setDoc, updateDoc, serverTimestamp } from './firebase';
import menu from './menu.json';
import './style.css';

type Data = Record<string, any>;
type View = 'jobs' | 'new' | 'tests' | 'tools' | 'activity';
type Action = {label:string; operation:string};
const safeMessage = (error: unknown) => error instanceof Error ? error.message : String(error);
const age = (value: any) => value?.toDate ? Date.now() - value.toDate().getTime() : Infinity;
const jobActions = (job: Data): Action[] => Array.isArray(job.actions) ? job.actions : [];
const danger = new Set(['job.merge','job.discard','job.rerun','job.close_issue','job.deliver','tool.update']);

function App() {
  const [user,setUser] = useState<User|null|undefined>(undefined);
  const [view,setView] = useState<View>('jobs');
  const [worker,setWorker] = useState<Data>({});
  const [project,setProject] = useState('themis');
  const [jobs,setJobs] = useState<Data[]>([]);const [archives,setArchives] = useState<Data[]>([]);const [showArchived,setShowArchived] = useState(false);
  const [commands,setCommands] = useState<Data[]>([]);
  const [selected,setSelected] = useState<string|null>(null);
  const [tool,setTool] = useState<Action>(menu.tools[0]);
  const [error,setError] = useState('');
  const [busy,setBusy] = useState(false);
  const [search,setSearch] = useState('');
  const [tick,setTick] = useState(0);
  useEffect(() => onAuthStateChanged(auth,setUser),[]);
  useEffect(() => { const timer=setInterval(()=>setTick(x=>x+1),15000); return ()=>clearInterval(timer); },[]);
  useEffect(() => {
    if (!user) return;
    const unsubs = [
      onSnapshot(doc(db,'workers','mac'),snap=>setWorker(snap.data() || {}),e=>setError(safeMessage(e))),
      onSnapshot(collection(db,'projects',project,'jobs'),snap=>setJobs(snap.docs.map(x=>x.data())),e=>setError(safeMessage(e))),
      onSnapshot(collection(db,'projects',project,'archives'),snap=>setArchives(snap.docs.map(x=>x.data())),e=>setError(safeMessage(e))),
      onSnapshot(query(collection(db,'commands'),orderBy('created_at','desc'),limit(30)),snap=>setCommands(snap.docs.map(x=>({id:x.id,...x.data()}))),e=>setError(safeMessage(e)))
    ];
    return ()=>unsubs.forEach(unsub=>unsub());
  },[user,project]);
  const online = tick >= 0 && age(worker.last_seen) < 30000;
  const activeJob = (showArchived?archives:jobs).find(x=>x.job_id===selected);
  const filtered = useMemo(()=>(showArchived?archives:jobs).filter(x=>`${x.title || ''} ${x.job_id} ${x.status}`.toLowerCase().includes(search.toLowerCase())),[jobs,archives,showArchived,search]);
  async function submit(operation:string,inputs:Data={},job?:Data,stay=false) {
    if (!user?.email || busy) return;
    if (danger.has(operation) && !window.confirm(`Run ${operation} on ${job?.job_id || project}? This may change local files or external services.`)) return;
    setBusy(true); setError('');
    try {
      const id=crypto.randomUUID();
      const payload:Data={status:'queued',requested_by:user.email,project_id:project,operation,inputs,created_at:serverTimestamp()};
      if (job) { payload.job_id=job.job_id; payload.expected_revision=job.revision; }
      await setDoc(doc(db,'commands',id),payload);
      if (!stay) setView('activity');
    } catch(e) { setError(safeMessage(e)); }
    finally { setBusy(false); }
  }
  if (user === undefined) return <main className="auth"><div className="logo">O<span>●</span></div><p>Loading Orchestrator…</p></main>;
  if (!user) return <main className="auth"><div className="logo">O<span>●</span></div><div className="eyebrow">ORCHESTRATOR / WEB</div><h1>Keep work moving.</h1><p>Sign in to view jobs and send real commands to your Mac worker.</p><button className="primary" onClick={()=>signIn().catch(e=>setError(safeMessage(e)))}>Continue with Google</button>{error && <p className="error">{error}</p>}</main>;
  return <div className="app"><aside className="sidebar"><div className="brand"><div className="brand-icon">O<span>●</span></div><div><strong>Orchestrator</strong><small>CONTROL ROOM</small></div></div><div className="sidebar-label">WORKSPACE</div><nav>
    <button className={view==='jobs'?'active':''} onClick={()=>setView('jobs')}><span>▦</span> Jobs <b>{jobs.length}</b></button>
    <button className={view==='new'?'active':''} onClick={()=>setView('new')}><span>＋</span> New Job</button>
    <button className={view==='tests'?'active':''} onClick={()=>setView('tests')}><span>✓</span> Run Unit Tests</button>
    <button className={view==='tools'?'active':''} onClick={()=>setView('tools')}><span>⚙</span> Configuration & Tools</button>
    <button onClick={()=>submit('project.refresh')}><span>⟳</span> Refresh & Sync</button>
    <button className={view==='activity'?'active':''} onClick={()=>setView('activity')}><span>◷</span> Activity <b>{commands.filter(x=>['queued','running'].includes(x.status)).length}</b></button>
  </nav><div className="sidebar-bottom"><div className={'worker '+(online?'online':'offline')}><i/>{online?'Mac worker online':'Mac worker offline'}</div><button className="account" onClick={()=>logOut()}><span className="avatar">{user.email?.[0].toUpperCase()}</span><span>{user.email}<small>Sign out</small></span></button></div></aside>
  <main className="content"><header><div className="breadcrumbs">WORKSPACE <span>/</span> {view==='jobs'?(activeJob?'JOB DETAIL':'JOBS'):view.toUpperCase()}</div><div className="header-right"><select aria-label="Active project" value={project} onChange={e=>{setProject(e.target.value);setSelected(null)}}>{(worker.project_ids?.length?worker.project_ids:[project]).map((id:string)=><option key={id}>{id}</option>)}</select><span className={'header-status '+(online?'online':'offline')}>{online?'● CONNECTED':'● OFFLINE'}</span></div></header>
  {error && <div className="error-banner" role="alert">{error}<button onClick={()=>setError('')}>×</button></div>}
  {view==='jobs' && (activeJob?<JobDetail job={activeJob} archived={showArchived} project={project} back={()=>setSelected(null)} onAction={submit}/>:<><div className="page-heading"><div><div className="eyebrow">YOUR WORK</div><h1>Jobs</h1><p>Every job in the CLI, synced from your Mac.</p></div><button className="primary" onClick={()=>setView('new')}>＋ New job</button></div><div className="metrics"><div><span>Total jobs</span><strong>{jobs.length}</strong></div><div><span>In progress</span><strong>{jobs.filter(x=>['executing','scheduled','debugging'].includes(x.status)).length}</strong></div><div><span>Needs review</span><strong>{jobs.filter(x=>['review-needed','human-needed'].includes(x.status)).length}</strong></div><div><span>Worker</span><strong className={online?'good':'warn'}>{online?'Online':'Offline'}</strong></div></div><div className="panel"><div className="panel-head"><h2>{showArchived?'Archived jobs':'Job history'}</h2><div className="history-controls"><button className="secondary" onClick={()=>{setShowArchived(!showArchived);setSelected(null)}}>{showArchived?'Active jobs':`Archived (${archives.length})`}</button><input value={search} onChange={e=>setSearch(e.target.value)} placeholder="Search jobs…" aria-label="Search jobs"/></div></div>{filtered.length?<div className="job-list">{filtered.map(job=><button className="job-row" key={job.job_id} onClick={()=>setSelected(job.job_id)}><span className="job-glyph">{job.type?.startsWith('bug')?'⌁':job.type?.startsWith('test')?'✓':'◇'}</span><span className="job-main"><strong>{job.title || job.job_id}</strong><small>{job.job_id} · {job.type}</small></span><span className={'badge '+job.status}>{job.status}</span><span className="row-arrow">→</span></button>)}</div>:<div className="empty"><div>◇</div><h3>No jobs here yet</h3><p>Create a job to start planning and running work.</p><button className="primary" onClick={()=>setView('new')}>Create first job</button></div>}</div></>)}
  {view==='tests' && <AppTests project={project} commands={commands} onSubmit={(op,inputs,stay)=>submit(op,inputs,undefined,stay)} busy={busy}/>}
  {view==='new' && <NewJob onSubmit={inputs=>submit('job.create',inputs)} busy={busy} defaultModels={commands.find(x=>x.operation==='tool.models'&&x.project_id===project&&x.status==='succeeded'&&x.result?.selected)?.result?.selected||['codex']} defaultMachines={commands.find(x=>x.operation==='tool.fleet'&&x.project_id===project&&x.status==='succeeded'&&x.result?.selected?.length)?.result?.selected||['local']}/>}
  {view==='tools' && <div className="page"><div className="page-heading"><div><div className="eyebrow">CLI CONFIGURATION</div><h1>Configuration & Tools</h1><p>Manage the same tools available in the terminal.</p></div></div><div className="tools-grid"><label className="field tool-picker"><span>Tool</span><select value={tool.operation} onChange={e=>setTool(menu.tools.find(item=>item.operation===e.target.value)||menu.tools[0])}>{menu.tools.map(item=><option key={item.operation} value={item.operation}>{item.label}</option>)}</select></label><ToolPanel item={tool} project={project} onSubmit={(op,inputs)=>submit(op,inputs,undefined,true)} busy={busy} commands={commands} projects={worker.project_ids||[project]} onProject={setProject}/></div></div>}
  {view==='activity' && <div className="page"><div className="page-heading"><div><div className="eyebrow">COMMAND HISTORY</div><h1>Activity</h1><p>Queued commands wait for the Mac worker. Results appear here when they finish.</p></div></div><div className="panel activity">{commands.length?commands.map(cmd=><div className="activity-row" key={cmd.id}><div><strong>{cmd.operation}</strong><small>{cmd.project_id}{cmd.job_id?' · '+cmd.job_id:''} · {cmd.id}</small></div><span className={'badge '+cmd.status}>{cmd.status}</span>{cmd.status==='queued'&&<button className="secondary" onClick={()=>updateDoc(doc(db,'commands',cmd.id),{status:'cancelled'}).catch(e=>setError(safeMessage(e)))}>Cancel</button>}{cmd.output && <pre>{cmd.output}</pre>}{cmd.result && <pre>{JSON.stringify(cmd.result,null,2)}</pre>}</div>):<div className="empty"><h3>No commands yet</h3><p>Actions you run from this interface appear here.</p></div>}</div></div>}
  </main></div>;
}
function AppTests({project,commands,onSubmit,busy}:{project:string;commands:Data[];onSubmit:(operation:string,inputs?:Data,stay?:boolean)=>void;busy:boolean}) {
  const [plan,setPlan]=useState('');
  const listing=commands.find(x=>x.operation==='tests.run'&&x.project_id===project&&x.status==='succeeded'&&x.inputs?.action==='list');
  const plans:string[]=listing?.result?.plans||[];
  return <div className="page narrow"><div className="page-heading"><div><div className="eyebrow">APP TESTS</div><h1>Run Unit Tests</h1><p>Choose an Xcode test plan or run a simulator visual check, as in the CLI.</p></div></div><div className="panel section"><h2>Available test plans</h2><p>{listing?plans.length?`${plans.length} plan(s) found in ${listing.result.test_target}/TestPlans.`:'No Xcode test plans are configured in this project.':'Load the test plans from your Mac worker.'}</p><button className="secondary" disabled={busy} onClick={()=>onSubmit('tests.run',{action:'list'},true)}>Load test plans</button>{plans.length>0&&<div className="test-run"><label className="field"><span>Test plan</span><select value={plan} onChange={e=>setPlan(e.target.value)}><option value="">Select a plan</option>{plans.map(name=><option key={name}>{name}</option>)}</select></label><button className="primary" disabled={busy||!plan} onClick={()=>onSubmit('tests.run',{action:'plan',plan})}>Run selected plan →</button></div>}<div className="test-run"><h2>Simulator visual check</h2><p>Build, launch, and capture simulator screenshots.</p><button className="primary" disabled={busy} onClick={()=>onSubmit('tests.run',{action:'visual'})}>Run visual check →</button></div></div></div>;
}
function Field({label,value,onChange,placeholder,textarea=false}: {label:string;value:string;onChange:(value:string)=>void;placeholder?:string;textarea?:boolean}) {return <label className="field"><span>{label}</span>{textarea?<textarea value={value} onChange={e=>onChange(e.target.value)} placeholder={placeholder}/>:<input value={value} onChange={e=>onChange(e.target.value)} placeholder={placeholder}/>}</label>}
function NewJob({onSubmit,busy,defaultModels,defaultMachines}:{onSubmit:(inputs:Data)=>void;busy:boolean;defaultModels:string[];defaultMachines:string[]}) {
  const [kind,setKind]=useState('feature'); const [prompt,setPrompt]=useState(''); const [models,setModels]=useState(defaultModels.join(',')); const [machines,setMachines]=useState(defaultMachines.join(',')); const [branch,setBranch]=useState('new'); const [spec,setSpec]=useState(''); const [preset,setPreset]=useState('balanced'); const [yolo,setYolo]=useState(false); const [stitch,setStitch]=useState(false); const [review,setReview]=useState(false); const [repro,setRepro]=useState('');const [expected,setExpected]=useState('');const [subsystems,setSubsystems]=useState('');const [vibe,setVibe]=useState('');const [planner,setPlanner]=useState('');const [builder,setBuilder]=useState('');const [reviewer,setReviewer]=useState('');
  useEffect(()=>setModels(defaultModels.join(',')),[defaultModels.join(',')]);useEffect(()=>setMachines(defaultMachines.join(',')),[defaultMachines.join(',')]);
  const choices=[['feature','Feature / iteration'],['quick','Quick change'],['bug','Bug fix'],['design','Design prototype'],['coverage','Coverage audit']];
  const inputs={job_type:kind,prompt,allowed_models:models.split(',').map(x=>x.trim()).filter(Boolean),allowed_machines:machines.split(',').map(x=>x.trim()).filter(Boolean),branch_mode:branch,preset,spec_url:spec||undefined,yolo,stitch,repro:kind==='bug'?repro:undefined,expected:kind==='bug'?expected:undefined,subsystems:kind==='coverage'?subsystems:undefined,vibe:kind==='design'||stitch?vibe:undefined,planner:planner||undefined,builder:builder||undefined,reviewer:reviewer||undefined};
  return <div className="page narrow"><div className="page-heading"><div><div className="eyebrow">START SOMETHING</div><h1>New job</h1><p>Choose the same workflow and options offered by the CLI.</p></div></div><div className="panel form-panel"><h2>1. What kind of work?</h2><div className="choice-grid">{choices.map(([value,label])=><button key={value} className={kind===value?'chosen':''} onClick={()=>setKind(value)}>{label}</button>)}</div><h2>2. Describe the job</h2><Field label="Request" value={prompt} onChange={setPrompt} textarea placeholder="What should Orchestrator do?"/><Field label="External specification (optional)" value={spec} onChange={setSpec} placeholder="docs/spec.md or https://…"/>{kind==='bug'&&<><Field label="Reproduction steps" value={repro} onChange={setRepro} textarea/><Field label="Expected behavior" value={expected} onChange={setExpected} textarea/></>}{kind==='coverage'&&<Field label="Subsystems to audit" value={subsystems} onChange={setSubsystems}/ >}{(kind==='design'||stitch)&&<Field label="Preferred visual vibe" value={vibe} onChange={setVibe}/ >}<h2>3. Execution settings</h2><div className="form-grid"><Field label="Allowed model IDs, comma separated" value={models} onChange={setModels}/><Field label="Allowed machine IDs, comma separated" value={machines} onChange={setMachines}/><label className="field"><span>Branch mode</span><select value={branch} onChange={e=>setBranch(e.target.value)}><option value="new">New branch</option><option value="current">Current branch</option><option value="manual">Manual / no Git actions</option></select></label><label className="field"><span>Model preset</span><select value={preset} onChange={e=>setPreset(e.target.value)}><option value="balanced">Balanced</option><option value="fast">Fast</option><option value="hard-bug">Hard bug</option><option value="architecture">Architecture</option></select></label></div><details className="advanced"><summary>Advanced model roles</summary><div className="form-grid"><Field label="Planner model ID (optional)" value={planner} onChange={setPlanner}/><Field label="Builder model ID (optional)" value={builder} onChange={setBuilder}/><Field label="Reviewer model ID (optional)" value={reviewer} onChange={setReviewer}/></div></details><label className="check"><input type="checkbox" checked={stitch} onChange={e=>setStitch(e.target.checked)}/> Design first with Stitch</label><label className="check"><input type="checkbox" checked={yolo} onChange={e=>setYolo(e.target.checked)}/> YOLO mode: dispatch after planning</label>{review && <div className="review"><strong>Review command</strong><pre>{JSON.stringify(inputs,null,2)}</pre></div>}<div className="form-actions"><button className="secondary" onClick={()=>setReview(!review)}>{review?'Hide review':'Review settings'}</button><button className="primary" disabled={busy||!prompt.trim()||!inputs.allowed_models.length||!inputs.allowed_machines.length} onClick={()=>onSubmit(inputs)}>Create real job →</button></div></div></div>;
}
function JobDetail({job,project,archived,back,onAction}:{job:Data;project:string;archived:boolean;back:()=>void;onAction:(operation:string,inputs?:Data,job?:Data)=>void}) {
  const [action,setAction]=useState<Action|null>(null);const [value,setValue]=useState('');const [extra,setExtra]=useState('');const [iterationCount,setIterationCount]=useState('5');const [rerunBranch,setRerunBranch]=useState('new');const [file,setFile]=useState<File|null>(null);const [uploading,setUploading]=useState(false);const [uploadError,setUploadError]=useState('');
  const actions=archived?[]:jobActions(job);
  const inputFor=(op:string):Data=>{if(['job.revise','job.iterate','job.reopen_bug'].includes(op)) return {feedback:value}; if(op==='job.link_logs') return {log_paths:value.split('\n').map(x=>x.trim()).filter(Boolean)};if(op==='job.ask_ai') return {question:value,model:extra};if(op==='job.models')return {allowed_models:value.split(',').map(x=>x.trim()).filter(Boolean)};if(op==='job.attach_reference')return {source_url:value,description:extra};if(op==='job.debug')return {feedback:value||undefined,max_iterations:Number(iterationCount)};if(op==='job.rerun')return {branch_mode:rerunBranch};return {}};
  async function runSelected() {
    if (!action) return;
    if (action.operation !== 'job.attach_reference' || !file) { onAction(action.operation,inputFor(action.operation),job); return; }
    setUploading(true);setUploadError('');
    try {
      if (file.size > 10*1024*1024 || file.size === 0 || !['image/png','image/jpeg','image/webp','application/pdf','text/plain'].includes(file.type)) throw new Error('Choose a PNG, JPEG, WebP, PDF, or text file under 10 MB.');
      const id=crypto.randomUUID();const count=Math.ceil(file.size/120000);
      await setDoc(doc(db,'projects',project,'artifacts',id),{name:file.name,content_type:file.type,size:file.size,chunk_count:count,created_at:serverTimestamp()});
      for(let i=0;i<count;i++) {
        const bytes=new Uint8Array(await file.slice(i*120000,(i+1)*120000).arrayBuffer());
        let binary='';for(const byte of bytes) binary+=String.fromCharCode(byte);
        await setDoc(doc(db,'projects',project,'artifacts',id,'chunks',String(i).padStart(4,'0')),{data:btoa(binary)});
      }
      onAction(action.operation,{artifact_id:id,description:extra},job);
    } catch(e) {setUploadError(safeMessage(e));} finally {setUploading(false);}
  }
  return <div className="page"><button className="back" onClick={back}>← All jobs</button><div className="page-heading"><div><div className="eyebrow">{job.job_id} · {job.type}</div><h1>{job.title||job.job_id}</h1><p>{job.plan?.summary||'Job detail and available actions.'}</p></div><span className={'badge large '+job.status}>{job.status}</span></div><div className="detail-grid"><div><div className="panel section"><h2>Plan & progress</h2><p>{job.plan?.summary||'No plan summary published yet.'}</p>{job.plan?.tasks?.length>0 && <ol className="tasks">{job.plan.tasks.map((task:any,i:number)=><li key={i} className={job.completed_task_indices?.includes(i)?'done':''}>{typeof task==='string'?task:task.title||task.description||JSON.stringify(task)}</li>)}</ol>}</div><div className="panel section"><h2>Context</h2><div className="facts">{[['Branch',job.branch],['Issue',job.issue_number],['Pull request',job.pr_number],['Planner',job.planner],['Builder',job.builder],['Reviewer',job.reviewer],['Models',job.allowed_models?.join(', ')],['Machines',job.allowed_machines?.join(', ')],['Logs',job.last_manual_log_paths?.join(', ')]].filter(x=>x[1]).map(([label,value])=><div key={label}><span>{label}</span><strong>{value}</strong></div>)}</div></div></div><div className="panel section action-panel"><h2>{archived?'Archived job':'Available actions'}</h2><p>{archived?'Restore this job from Configuration & Tools to run new actions.':'Actions follow this job\'s CLI status.'}</p><div className="action-list">{actions.map((item,i)=><button key={item.operation+i} onClick={()=>{setAction(item);setValue('');setExtra('')}}>{item.label}<span>→</span></button>)}</div>{action && <div className="action-form"><div className="eyebrow">SELECTED ACTION</div><h3>{action.label}</h3>{['job.revise','job.iterate','job.reopen_bug','job.ask_ai','job.link_logs','job.models','job.attach_reference','job.debug'].includes(action.operation) && <Field label={action.operation==='job.ask_ai'?'Question':action.operation==='job.link_logs'?'Project-relative log paths, one per line':action.operation==='job.models'?'Model IDs, comma separated':action.operation==='job.attach_reference'?'HTTP(S) reference URL':'Feedback'} value={value} onChange={setValue} textarea={['job.revise','job.iterate','job.ask_ai'].includes(action.operation)}/ >}{action.operation==='job.debug'&&<Field label="Maximum debug iterations (1–20)" value={iterationCount} onChange={setIterationCount}/ >}{action.operation==='job.rerun'&&<label className="field"><span>Branch mode</span><select value={rerunBranch} onChange={e=>setRerunBranch(e.target.value)}><option value="new">New branch</option><option value="current">Current branch</option><option value="manual">Manual</option></select></label>}{action.operation==='job.attach_reference'&&<label className="field"><span>Or upload a PNG, JPEG, WebP, PDF, or text file (10 MB max)</span><input type="file" accept="image/png,image/jpeg,image/webp,application/pdf,text/plain" onChange={e=>setFile(e.target.files?.[0]||null)}/></label>}{uploadError&&<p className="error">{uploadError}</p>}{['job.ask_ai','job.attach_reference'].includes(action.operation)&&<Field label={action.operation==='job.ask_ai'?'Model ID':'Description'} value={extra} onChange={setExtra}/>}<button className="primary" disabled={uploading} onClick={runSelected}>{uploading?'Uploading…':'Run action →'}</button></div>}</div></div></div>;
}
const TOOL_ACTIONS:Record<string,[string,string][]> = {
  'tool.models':[['list','Model catalog'],['select','Select default models'],['refresh','Sync catalog'],['live_discovery','Discover live models']],
  'tool.keys':[['status','Provider readiness'],['clear','Clear saved fallback API keys']],
  'tool.fleet':[['list','Fleet status'],['select','Select active machines'],['discover','Discover remote machines'],['add','Add remote machine'],['probe','Probe machines'],['rename','Rename machine'],['remove','Remove machine'],['enable','Enable machine'],['disable','Disable machine'],['purge','Purge stale processes & cache']],
  'tool.audit':[['run','Run prerequisite audit']],
  'tool.base_branch':[['show','Show base branch'],['set','Set base branch']],
  'tool.project':[['show','Current project']],
  'tool.archive':[['list','Archived jobs'],['restore','Restore job']],
  'tool.instructions':[['list','Instruction status'],['create_missing','Create missing files'],['repair','Repair missing links'],['read','Read file'],['write','Edit file'],['copy_role_prompts','Copy role prompt templates'],['reset_role_prompts','Reset role prompts']],
  'tool.email':[['list','Email settings'],['add','Add recipient'],['import','Import recipients'],['remove','Remove recipient'],['provider','Configure sender'],['test','Send test email']],
  'tool.docs':[['list','Documentation index'],['read','Read document']],
  'tool.distribution':[['status','Distribution readiness'],['smoke_test','Build & delivery smoke test']],
  'tool.self_tests':[['workflow','Full integrated workflow'],['tooling','Tooling & build logic'],['probing','Machine probing'],['resume','Resume & recovery'],['all','All Python tests'],['console','Console UI'],['models','Model registry'],['fleet','Fleet health'],['fleet_sync','GitHub metadata sync'],['model_ping','Local model pings'],['delivery','Build & delivery'],['setup','Setup logic']],
  'tool.update':[['status','Version & source status'],['local','Update Mac package'],['fleet','Update remote workers']]
};
function ToolPanel({item,project,onSubmit,busy,commands,projects,onProject}:{item:Action;project:string;onSubmit:(operation:string,inputs?:Data)=>void;busy:boolean;commands:Data[];projects:string[];onProject:(id:string)=>void}) {
  const op=item.operation;
  const [action,setAction]=useState('list');const [name,setName]=useState('');const [value,setValue]=useState('');const [content,setContent]=useState('');const [extra,setExtra]=useState('');
  useEffect(()=>{setAction(TOOL_ACTIONS[item.operation]?.[0]?.[0]||'list');setName('');setValue('');setContent('');setExtra('')},[item.operation]);
  const latest=commands.find(x=>x.operation===op&&x.project_id===project);
  const listing=commands.find(x=>x.operation===op&&x.project_id===project&&x.status==='succeeded'&&['list','status','show'].includes(x.inputs?.action||'list'))?.result;
  const models:Data[]=listing?.models||latest?.result?.models||[];
  const machineNames:string[]=(listing?.machines||latest?.result?.machines||[]).map((m:Data)=>m.name);
  const files:string[]=[...new Set([...Object.keys(listing?.files||{}),...(listing?.role_prompts||[]),...(latest?.result?.role_prompts||[])])];
  const docs:string[]=listing?.documents||latest?.result?.documents||[];
  const recipients:string[]=listing?.notification_emails||latest?.result?.notification_emails||[];
  const archived:string[]=listing?.jobs||latest?.result?.jobs||[];
  const opts=TOOL_ACTIONS[op]||[['list','Show status']];
  function run() {
    const input:Data={};
    if(op==='tool.models'){if(action==='select')input.allowed_models=value.split(',').map(x=>x.trim()).filter(Boolean);if(action==='refresh')input.refresh=true;if(action==='live_discovery')input.live_discovery=true;}
    if(op==='tool.keys')input.action=action;
    if(op==='tool.fleet'){input.action=action;if(action==='select')input.machine_ids=value.split(',').map(x=>x.trim()).filter(Boolean);if(['rename','remove','enable','disable'].includes(action))input.name=name;if(action==='rename')input.new_name=value;if(action==='add'){input.name=name;input.ssh_target=value;input.repo_path=content;}}
    if(op==='tool.base_branch'&&action==='set')input.branch=value;
    if(op==='tool.archive'){input.action=action;if(action==='restore')input.job_id=name;}
    if(op==='tool.instructions'){input.action=action;if(['read','write'].includes(action))input.name=name;if(action==='write')input.content=content;}
    if(op==='tool.email'){input.action=action;if(['add','remove'].includes(action))input.email=name;if(action==='import')input.content=content;if(action==='provider'){input.provider=value;input.sender_email=name||undefined;input.display_name=extra||undefined;}}
    if(op==='tool.docs'&&action==='read')input.name=name;
    if(op==='tool.distribution')input.action=action;
    if(op==='tool.self_tests')input.suite=action;
    if(op==='tool.update')input.action=action;
    if(['tool.keys:clear','tool.fleet:remove','tool.fleet:purge','tool.instructions:reset_role_prompts','tool.email:test','tool.distribution:smoke_test','tool.self_tests:delivery'].includes(`${op}:${action}`) && !window.confirm(`Run ${opts.find(x=>x[0]===action)?.[1]}?`))return;
    onSubmit(op,input);
  }
  const needsName=(op==='tool.fleet'&&['add','rename','remove','enable','disable'].includes(action))||(op==='tool.archive'&&action==='restore')||(op==='tool.instructions'&&['read','write'].includes(action))||(op==='tool.email'&&['add','remove'].includes(action))||(op==='tool.docs'&&action==='read');
  const suggestions=op==='tool.fleet'?machineNames:op==='tool.archive'?archived:op==='tool.instructions'?files:op==='tool.email'?recipients:docs;
  return <div className="panel section tool-panel"><div className="eyebrow">SETUP & TOOLS</div><h2>{item.label}</h2>
    {op==='tool.project'&&<p>Switch among projects configured on the Mac worker. To add a project, configure it locally and restart the worker with that project in its allowlist.</p>}
    {op==='tool.keys'&&<p>Provider sign-in and fallback API keys are managed in the local CLI on the Mac. This reports readiness without sending credentials to Firestore.</p>}
    {op==='tool.email'&&<p>Sender passwords and API keys are entered through the local CLI on the Mac. Recipient and nonsecret sender settings are available here.</p>}
    {op==='tool.distribution'&&<p>Firebase login and signing credentials are set up locally on the Mac. Check readiness here, then run a real delivery smoke test.</p>}
    <label className="field"><span>Action</span><select value={action} onChange={e=>{setAction(e.target.value);setName('');setValue('');setContent('');setExtra('')}}>{opts.map(([id,label])=><option key={id} value={id}>{label}</option>)}</select></label>
    {op==='tool.project'&&<label className="field"><span>Active project</span><select value={project} onChange={e=>onProject(e.target.value)}>{projects.map(id=><option key={id}>{id}</option>)}</select></label>}
    {needsName&&<label className="field"><span>{op==='tool.archive'?'Job ID':op==='tool.email'?'Recipient email':op==='tool.docs'?'Document':'Name'}</span>{suggestions.length&&action!=='add'&&action!=='write'?<select value={name} onChange={e=>setName(e.target.value)}><option value="">Select…</option>{suggestions.map(x=><option key={x}>{x}</option>)}</select>:<input value={name} onChange={e=>setName(e.target.value)}/>}</label>}
    {op==='tool.email'&&action==='provider'&&<><label className="field"><span>Provider</span><select value={value} onChange={e=>setValue(e.target.value)}><option value="">Select…</option><option value="gmail">Gmail</option><option value="resend">Resend</option></select></label><Field label="Sender email" value={name} onChange={setName}/>{value==='resend'&&<Field label="Display name" value={extra} onChange={setExtra}/>}</>}
    {((op==='tool.fleet'&&['select','add','rename'].includes(action))||(op==='tool.models'&&action==='select')||(op==='tool.base_branch'&&action==='set'))&&<Field label={op==='tool.models'?'Model IDs, comma separated':op==='tool.fleet'&&action==='select'?'Machine names, comma separated':op==='tool.fleet'&&action==='add'?'SSH target':op==='tool.fleet'?'New machine name':'Base branch'} value={value} onChange={setValue} placeholder={op==='tool.models'?models.slice(0,3).map(m=>m.id).join(', '):op==='tool.fleet'&&action==='select'?machineNames.join(', '):undefined}/>}
    {((op==='tool.fleet'&&action==='add')||(op==='tool.instructions'&&action==='write')||(op==='tool.email'&&action==='import'))&&<Field label={op==='tool.fleet'?'Absolute remote repository path':op==='tool.instructions'?'Instruction content':'Paste CSV or email addresses'} value={content} onChange={setContent} textarea={op!=='tool.fleet'}/>}
    {op==='tool.models'&&models.length>0&&<p>Available: {models.map(m=>m.id).join(', ')}</p>}
    <button className="primary" disabled={busy||(needsName&&!name)||(op==='tool.email'&&action==='provider'&&!value)||(op==='tool.fleet'&&['select','add','rename'].includes(action)&&!value)||(op==='tool.models'&&action==='select'&&!value)||(op==='tool.base_branch'&&action==='set'&&!value)} onClick={run}>Run {opts.find(x=>x[0]===action)?.[1]} →</button>
    {latest&&<div className="tool-result"><div className="eyebrow">LATEST RESULT · {latest.status}</div>{latest.error&&<p className="error">{latest.error}</p>}{latest.output&&<pre>{latest.output}</pre>}{latest.result&&<pre>{JSON.stringify(latest.result,null,2)}</pre>}</div>}
  </div>;
}
createRoot(document.getElementById('root')!).render(<React.StrictMode><App/></React.StrictMode>);
