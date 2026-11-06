import React, { useEffect, useMemo, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { onAuthStateChanged, type User } from 'firebase/auth';
import { auth, db, signIn, logOut, collection, doc, onSnapshot, query, orderBy, limit, setDoc, updateDoc, serverTimestamp } from './firebase';
import menu from './menu.json';
import { ModelList, RunLog, ToolOutput } from './tool-output';
import { apiFetch } from './local-api';
import './style.css';

type Data = Record<string, any>;
type View = 'jobs' | 'new' | 'tests' | 'tools' | 'activity';
type Action = {label:string; operation:string};
const safeMessage = (error: unknown) => error instanceof Error ? error.message : String(error);
const age = (value: any) => value?.toDate ? Date.now() - value.toDate().getTime() : Infinity;
const jobActions = (job: Data): Action[] => Array.isArray(job.actions) ? job.actions : [];
const danger = new Set(['job.merge','job.discard','job.rerun','job.close_issue','job.deliver','tool.update']);

type NavState = { view: View; selected: string | null };

interface ActionButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  disabledReason?: string;
  tooltipPosition?: 'top' | 'bottom';
  wrapStyle?: React.CSSProperties;
  wrapClassName?: string;
}

function ActionButton({
  children,
  disabled,
  disabledReason,
  tooltipPosition = 'top',
  className = '',
  onClick,
  wrapStyle,
  wrapClassName = '',
  title,
  type = 'button',
  ...props
}: ActionButtonProps) {
  const [showTapTip, setShowTapTip] = useState(false);
  const tipTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const activeReason = disabled ? disabledReason : undefined;
  const computedTitle = activeReason || title;

  const handleWrapClick = (e: React.MouseEvent) => {
    if (disabled && disabledReason) {
      e.preventDefault();
      e.stopPropagation();
      if (tipTimerRef.current) clearTimeout(tipTimerRef.current);
      setShowTapTip(true);
      tipTimerRef.current = setTimeout(() => {
        setShowTapTip(false);
      }, 3500);
    }
  };

  useEffect(() => {
    return () => {
      if (tipTimerRef.current) clearTimeout(tipTimerRef.current);
    };
  }, []);

  return (
    <div
      className={`tooltip-btn-wrap ${disabled ? 'is-disabled' : ''} ${wrapClassName}`.trim()}
      style={wrapStyle}
      onClick={handleWrapClick}
      title={computedTitle}
    >
      <button
        {...props}
        type={type}
        className={className}
        disabled={disabled}
        onClick={onClick}
      >
        {children}
      </button>
      {showTapTip && activeReason && (
        <div className={`btn-tap-tooltip ${tooltipPosition === 'bottom' ? 'pos-bottom' : ''}`.trim()} role="tooltip">
          <span className="tooltip-icon">ℹ</span> {activeReason}
        </div>
      )}
    </div>
  );
}

interface MultiSelectOption {
  id: string;
  label: string;
  desc?: string;
}

interface MultiSelectDropdownProps {
  label: string;
  hint?: string;
  options: MultiSelectOption[];
  selected: string[];
  onChange: (selected: string[]) => void;
  placeholder?: string;
  ariaLabel?: string;
}

function MultiSelectDropdown({
  label,
  hint,
  options,
  selected,
  onChange,
  placeholder = 'Select items…',
  ariaLabel
}: MultiSelectDropdownProps) {
  const [isOpen, setIsOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!isOpen) return;
    const handleClickOutside = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setIsOpen(false);
      }
    };
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, [isOpen]);

  const toggleOption = (id: string) => {
    if (selected.includes(id)) {
      if (selected.length > 1) {
        onChange(selected.filter(x => x !== id));
      }
    } else {
      onChange([...selected, id]);
    }
  };

  const handleSelectAll = (e: React.MouseEvent) => {
    e.stopPropagation();
    onChange(options.map(o => o.id));
  };

  const handleDeselectAll = (e: React.MouseEvent) => {
    e.stopPropagation();
    if (options.length > 0) {
      onChange([options[0].id]);
    }
  };

  const displayText = useMemo(() => {
    if (selected.length === 0) return placeholder;
    if (selected.length === options.length && options.length > 1) {
      return `All ${label.toLowerCase()} selected (${selected.length})`;
    }
    if (selected.length <= 2) {
      return selected.map(id => options.find(o => o.id === id)?.label || id).join(', ');
    }
    return `${selected.length} selected: ${selected.slice(0, 2).map(id => options.find(o => o.id === id)?.label || id).join(', ')}…`;
  }, [selected, options, label, placeholder]);

  return (
    <div className="field multi-select-field" ref={containerRef}>
      <div className="multi-select-header">
        <span>{label} ({selected.length} active)</span>
        {hint && <small className="field-hint">{hint}</small>}
      </div>
      <div
        className="multi-select-trigger"
        onClick={() => setIsOpen(!isOpen)}
        tabIndex={0}
        role="button"
        aria-label={ariaLabel || label}
        aria-expanded={isOpen}
      >
        <span className="multi-select-display">{displayText}</span>
        <span className={`multi-select-caret ${isOpen ? 'open' : ''}`}>▾</span>
      </div>
      {isOpen && (
        <div className="multi-select-menu" role="listbox">
          <div className="multi-select-actions">
            <button
              type="button"
              className="multi-select-action-btn"
              disabled={selected.length === options.length}
              onClick={handleSelectAll}
            >
              Select all
            </button>
            <span className="multi-select-divider">·</span>
            <button
              type="button"
              className="multi-select-action-btn"
              disabled={selected.length <= 1}
              onClick={handleDeselectAll}
            >
              Deselect all
            </button>
          </div>
          <div className="multi-select-list">
            {options.map(opt => {
              const isChecked = selected.includes(opt.id);
              return (
                <div
                  key={opt.id}
                  className={`multi-select-item ${isChecked ? 'selected' : ''}`}
                  onClick={() => toggleOption(opt.id)}
                  role="option"
                  aria-selected={isChecked}
                >
                  <input
                    type="checkbox"
                    checked={isChecked}
                    readOnly
                    tabIndex={-1}
                  />
                  <div className="multi-select-item-text">
                    <strong>{opt.label}</strong>
                    {opt.desc && <small>{opt.desc}</small>}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}

const isLocalHost = (h: string) =>
  h === 'localhost' ||
  h === '127.0.0.1' ||
  h === '0.0.0.0' ||
  h.endsWith('.local') ||
  h.endsWith('.internal') ||
  h.endsWith('.ts.net') ||
  /^192\.168\.\d+\.\d+$/.test(h) ||
  /^10\.\d+\.\d+\.\d+$/.test(h) ||
  /^172\.(1[6-9]|2\d|3[01])\.\d+\.\d+$/.test(h) ||
  /^100\.\d+\.\d+\.\d+$/.test(h);

const IS_LOCALHOST = typeof window !== 'undefined' && (
  isLocalHost(window.location.hostname) ||
  window.location.protocol === 'file:'
);

const CLOUD_AUTH_ENABLED = import.meta.env.VITE_ENABLE_CLOUD_AUTH === 'true' || (!IS_LOCALHOST && import.meta.env.VITE_ENABLE_CLOUD_AUTH !== 'false');

function App() {
  const [isLocalApi, setIsLocalApi] = useState(IS_LOCALHOST);
  const [user, setUser] = useState<User | null | undefined>(
    !CLOUD_AUTH_ENABLED || IS_LOCALHOST
      ? ({ email: 'local@mac.internal', displayName: 'Local Operator' } as any)
      : undefined
  );
  const [view,setView] = useState<View>('jobs');
  const [worker,setWorker] = useState<Data>({});
  const [project,setProject] = useState('themis');
  const [jobs,setJobs] = useState<Data[]>([]);const [archives,setArchives] = useState<Data[]>([]);const [showArchived,setShowArchived] = useState(false);
  const [commands,setCommands] = useState<Data[]>([]);
  const [selected,setSelected] = useState<string|null>(null);
  const [history,setHistory] = useState<NavState[]>([]);
  const [tool,setTool] = useState<Action>(menu.tools[0]);
  const [error,setError] = useState('');
  const [busy,setBusy] = useState(false);
  const [search,setSearch] = useState('');
  const [tick,setTick] = useState(0);
  const [showAddProject,setShowAddProject] = useState(false);
  const [showWorkflowModal,setShowWorkflowModal] = useState(false);
  const lastSynced = useRef<Record<string, number>>({});

  const navigate = (nextView: View, nextSelected: string | null = null) => {
    if (view === nextView && selected === nextSelected) return;
    setHistory(prev => [...prev, { view, selected }]);
    setView(nextView);
    setSelected(nextSelected);
  };

  const handleBack = () => {
    if (history.length > 0) {
      const prev = history[history.length - 1];
      setHistory(h => h.slice(0, -1));
      setView(prev.view);
      setSelected(prev.selected);
    } else if (selected !== null) {
      setSelected(null);
    } else if (view !== 'jobs') {
      setView('jobs');
      setSelected(null);
    }
  };

  const canGoBack = history.length > 0 || selected !== null || view !== 'jobs';

  useEffect(() => {
    if (IS_LOCALHOST) {
      (window as any).__IS_LOCAL_API__ = true;
    }
    apiFetch('/api/worker')
      .then(r => {
        if (r.status === 401) setError('Access token required. Open the Network link printed by "orchestrator ui" on your Mac.');
        return r.json();
      })
      .then(data => {
        if (data && data.local_mode) {
          (window as any).__IS_LOCAL_API__ = true;
          setIsLocalApi(true);
          setUser({ email: 'local@mac.internal', displayName: 'Local Operator' } as any);
        } else if (CLOUD_AUTH_ENABLED) {
          onAuthStateChanged(auth, setUser);
        }
      })
      .catch(() => {
        if (CLOUD_AUTH_ENABLED) {
          onAuthStateChanged(auth, setUser);
        }
      });
  }, []);

  useEffect(() => { const timer=setInterval(()=>setTick(x=>x+1),15000); return ()=>clearInterval(timer); },[]);
  useEffect(() => {
    if (!user) return;
    if (isLocalApi) {
      const poll = () => {
        apiFetch('/api/worker')
          .then(r => r.json())
          .then(data => {
            if (data) {
              setWorker(data);
              if (data.project_ids?.length && !data.project_ids.includes(project)) {
                setProject(data.project_ids[0]);
              }
            }
          })
          .catch(() => {});

        apiFetch(`/api/projects/${encodeURIComponent(project)}/jobs`)
          .then(r => r.json())
          .then(data => setJobs(Array.isArray(data) ? data : []))
          .catch(() => {});

        apiFetch(`/api/projects/${encodeURIComponent(project)}/archives`)
          .then(r => r.json())
          .then(data => setArchives(Array.isArray(data) ? data : []))
          .catch(() => {});

        apiFetch('/api/commands')
          .then(r => r.json())
          .then(data => setCommands(Array.isArray(data) ? data : []))
          .catch(() => {});
      };
      poll();
      const timer = setInterval(poll, 2500);
      return () => clearInterval(timer);
    } else {
      const unsubs = [
        onSnapshot(doc(db,'workers','mac'),snap=>setWorker(snap.data() || {}),e=>setError(safeMessage(e))),
        onSnapshot(collection(db,'projects',project,'jobs'),snap=>setJobs(snap.docs.map(x=>x.data())),e=>setError(safeMessage(e))),
        onSnapshot(collection(db,'projects',project,'archives'),snap=>setArchives(snap.docs.map(x=>x.data())),e=>setError(safeMessage(e))),
        onSnapshot(query(collection(db,'commands'),orderBy('created_at','desc'),limit(30)),snap=>setCommands(snap.docs.map(x=>({id:x.id,...x.data()}))),e=>setError(safeMessage(e)))
      ];
      return ()=>unsubs.forEach(unsub=>unsub());
    }
  },[user,project,isLocalApi]);
  const [jobPage, setJobPage] = useState(1);
  const JOBS_PAGE_SIZE = 20;

  const online = isLocalApi || (tick >= 0 && age(worker.last_seen) < 30000);
  const activeJob = (showArchived?archives:jobs).find(x=>x.job_id===selected);
  const filtered = useMemo(()=>(showArchived?archives:jobs).filter(x=>`${x.title || ''} ${x.job_id} ${x.status}`.toLowerCase().includes(search.toLowerCase())),[jobs,archives,showArchived,search]);
  const totalJobPages = Math.ceil(filtered.length / JOBS_PAGE_SIZE) || 1;
  const paginatedJobs = useMemo(() => {
    const start = (jobPage - 1) * JOBS_PAGE_SIZE;
    return filtered.slice(start, start + JOBS_PAGE_SIZE);
  }, [filtered, jobPage]);

  useEffect(() => {
    setJobPage(1);
  }, [search, showArchived, project]);
  async function submit(operation:string,inputs:Data={},job?:Data,stay=false) {
    if (!user?.email || busy) return;
    if (danger.has(operation) && !window.confirm(`Run ${operation} on ${job?.job_id || project}? This may change local files or external services.`)) return;
    setBusy(true); setError('');
    try {
      const id=crypto.randomUUID();
      const payload:Data={id,status:'queued',requested_by:user.email,project_id:project,operation,inputs,created_at:serverTimestamp()};
      if (job) { payload.job_id=job.job_id; payload.expected_revision=job.revision; }
      if (isLocalApi) {
        await apiFetch('/api/commands', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
      } else {
        await setDoc(doc(db,'commands',id),payload);
      }
      if (!stay) navigate('activity');
    } catch(e) { setError(safeMessage(e)); }
    finally { setBusy(false); }
  }
  useEffect(() => {
    if (!user || !project || !online) return;
    const now = Date.now();
    if (now - (lastSynced.current[project] || 0) > 60000) {
      lastSynced.current[project] = now;
      submit('project.refresh', {}, undefined, true);
    }
  }, [user, project, online]);
  const detectedSubsystems = useMemo(() => {
    const set = new Set<string>([
      'Document Processing',
      'Authentication',
      'Networking & API',
      'Persistence & CoreData',
      'ViewModels & State',
      'PDF Export',
      'UI Components'
    ]);
    for (const job of [...jobs, ...archives]) {
      if (typeof job.subsystems === 'string') {
        job.subsystems.split(',').map((s: string) => s.trim()).filter(Boolean).forEach((s: string) => set.add(s));
      }
      if (Array.isArray(job.plan?.target_subsystems)) {
        job.plan.target_subsystems.forEach((s: string) => set.add(s));
      }
      if (Array.isArray(job.plan?.tasks)) {
        for (const t of job.plan.tasks) {
          if (Array.isArray(t.likely_files)) {
            for (const f of t.likely_files) {
              const base = f.split('/').pop()?.replace(/\.swift$/i, '');
              if (base && !base.includes('Tests') && !base.includes('Mock') && base.length > 2) {
                set.add(base);
              }
            }
          }
        }
      }
    }
    return Array.from(set).slice(0, 16);
  }, [jobs, archives]);

  if (user === undefined) return <main className="auth"><div className="logo">O<span>●</span></div><p>Loading Orchestrator…</p></main>;
  if (!user) return <main className="auth"><div className="logo">O<span>●</span></div><h1>Keep work moving.</h1><p>Sign in to view jobs and send real commands to your Mac worker.</p><button className="primary" onClick={()=>signIn().catch(e=>setError(safeMessage(e)))}>Continue with Google</button>{error && <p className="error">{error}</p>}</main>;
  return <div className="app"><aside className="sidebar"><div className="brand"><div className="brand-icon">O<span>●</span></div><div><strong>Orchestrator</strong><small>{isLocalApi?'LOCAL MAC':'CONTROL ROOM'}</small></div></div><div className="sidebar-label">WORKSPACE</div><nav>
    <button className={view==='jobs'?'active':''} onClick={()=>navigate('jobs',null)}><span>▦</span> Jobs <b>{jobs.length}</b></button>
    <button className={view==='new'?'active':''} onClick={()=>navigate('new')}><span>＋</span> New Job</button>
    <button className={view==='tests'?'active':''} onClick={()=>navigate('tests')}><span>✓</span> Run Unit Tests</button>
    <button className={view==='tools'?'active':''} onClick={()=>navigate('tools')}><span>⚙</span> Configuration & Tools</button>
    <button className={view==='activity'?'active':''} onClick={()=>navigate('activity')}><span>◷</span> Activity <b>{commands.filter(x=>['queued','running'].includes(x.status)).length}</b></button>
  </nav><div className="sidebar-bottom"><div className={'worker '+(online?'online':'offline')}><i/>{isLocalApi ? 'Local Mac engine active' : online ? 'Mac worker online' : 'Mac worker offline'}</div><button className="account" onClick={()=>isLocalApi ? undefined : logOut()}><span className="avatar">{user.email?.[0].toUpperCase()}</span><span>{user.email}<small>{isLocalApi?'Localhost mode':'Sign out'}</small></span></button></div></aside>
  <main className="content"><header><div className="header-left"><ActionButton className="nav-back-btn" onClick={handleBack} disabled={!canGoBack} disabledReason="No previous screen to return to" tooltipPosition="bottom" title={canGoBack?'Back to previous screen':'At start'} aria-label="Back to previous screen"><span aria-hidden="true">←</span> Back</ActionButton><div className="breadcrumbs">WORKSPACE <span>/</span> {view==='jobs'?(activeJob?'JOB DETAIL':'JOBS'):view.toUpperCase()}</div></div><div className="header-right"><button type="button" className="help-btn" onClick={()=>setShowWorkflowModal(true)} title="View Orchestrator Lifecycle & Status Guide"><span className="help-icon">?</span> Workflow</button><select aria-label="Active project" value={project} onChange={e=>{if(e.target.value==='__add__'){setShowAddProject(true);}else{setProject(e.target.value);setSelected(null);setHistory([]);}}}>{(worker.project_ids?.length?worker.project_ids:[project]).map((id:string)=><option key={id} value={id}>{id}</option>)}<option value="__add__" className="add-project-option" style={{ color: '#38bdf8', fontWeight: 600 }}>＋ Add new project…</option></select><span className={'header-status '+(online?'online':'offline')}>{online?'● CONNECTED':'● OFFLINE'}</span></div></header>
  {error && <div className="error-banner" role="alert">{error}<button onClick={()=>setError('')}>×</button></div>}
  {view==='jobs' && (activeJob?<JobDetail job={activeJob} archived={showArchived} project={project} back={handleBack} onAction={submit} onOpenWorkflow={()=>setShowWorkflowModal(true)}/>:<div className="page"><div className="page-heading"><div><h1>Jobs</h1></div><button className="primary" onClick={()=>navigate('new')}>＋ New job</button></div><div className="metrics"><div><span>Total jobs</span><strong>{jobs.length}</strong></div><div><span>In progress</span><strong>{jobs.filter(x=>['executing','scheduled','debugging'].includes(x.status)).length}</strong></div><div><span>Needs review</span><strong>{jobs.filter(x=>['review-needed','human-needed'].includes(x.status)).length}</strong></div><div><span>Worker</span><strong className={online?'good':'warn'}>{online?'Online':'Offline'}</strong></div></div><div className="panel"><div className="panel-head"><h2>{showArchived?'Archived jobs':'Job history'}</h2><div className="history-controls"><button className="secondary" onClick={()=>{setShowArchived(!showArchived);setSelected(null)}}>{showArchived?'Active jobs':`Archived (${archives.length})`}</button><input value={search} onChange={e=>setSearch(e.target.value)} placeholder="Search jobs…" aria-label="Search jobs"/></div></div>{filtered.length?<><div className="job-list">{paginatedJobs.map(job=><button className="job-row" key={job.job_id} onClick={()=>navigate('jobs',job.job_id)}><span className="job-glyph">{job.type?.startsWith('bug')?'⌁':job.type?.startsWith('test')?'✓':'◇'}</span><span className="job-main"><strong>{job.title || job.job_id}</strong><small>{job.job_id} · {job.type}</small></span><span className={'badge '+job.status}>{job.status}</span><span className="row-arrow">→</span></button>)}</div>{totalJobPages>1&&<div className="pagination"><span className="pagination-info">Showing <strong>{(jobPage-1)*JOBS_PAGE_SIZE+1}–{Math.min(jobPage*JOBS_PAGE_SIZE,filtered.length)}</strong> of <strong>{filtered.length}</strong> jobs</span><div className="pagination-buttons"><ActionButton type="button" className="secondary" disabled={jobPage<=1} disabledReason="Already on first page" onClick={()=>setJobPage(p=>Math.max(1,p-1))} aria-label="Previous page">← Previous</ActionButton><span className="pagination-page-indicator">Page <b>{jobPage}</b> of <b>{totalJobPages}</b></span><ActionButton type="button" className="secondary" disabled={jobPage>=totalJobPages} disabledReason="Already on last page" onClick={()=>setJobPage(p=>Math.min(totalJobPages,p+1))} aria-label="Next page">Next →</ActionButton></div></div>}</>:<div className="empty"><div>◇</div><h3>No jobs here yet</h3><p>Create a job to start planning and running work.</p><button className="primary" onClick={()=>navigate('new')}>Create first job</button></div>}</div></div>)}
  {view==='tests' && <AppTests project={project} commands={commands} onSubmit={(op,inputs,stay)=>submit(op,inputs,undefined,stay)} busy={busy} workerTestPlans={worker.test_plans?.[project]}/>}
  {view==='new' && <NewJob project={project} onSubmit={inputs=>submit('job.create',inputs)} busy={busy} availableModels={commands.find(x=>x.operation==='tool.models'&&x.project_id===project&&x.status==='succeeded'&&Array.isArray(x.result?.models))?.result?.models||[]} defaultModels={commands.find(x=>x.operation==='tool.models'&&x.project_id===project&&x.status==='succeeded'&&x.result?.selected)?.result?.selected||['codex']} availableMachines={(commands.find(x=>x.operation==='tool.fleet'&&x.project_id===project&&x.status==='succeeded')?.result?.machines||[]).map((m:any)=>m.name||m.id||m)} defaultMachines={commands.find(x=>x.operation==='tool.fleet'&&x.project_id===project&&x.status==='succeeded'&&x.result?.selected?.length)?.result?.selected||['local']} suggestedSubsystems={detectedSubsystems}/>}
  {view==='tools' && <div className="page"><div className="page-heading"><div><h1>Configuration & Tools</h1><p>Manage project settings, models, fleet machines, and diagnostic tools.</p></div></div><div className="tools-grid"><label className="field tool-picker"><span>Tool</span><select value={tool.operation} onChange={e=>setTool(menu.tools.find(item=>item.operation===e.target.value)||menu.tools[0])}>{menu.tools.map(item=><option key={item.operation} value={item.operation}>{item.label}</option>)}</select></label><ToolPanel item={tool} project={project} onSubmit={(op,inputs)=>submit(op,inputs,undefined,true)} busy={busy} commands={commands} projects={worker.project_ids||[project]} onProject={setProject} onAddProject={()=>setShowAddProject(true)}/></div></div>}
  {view==='activity' && <ActivityFeed commands={commands} jobs={[...jobs, ...archives]} onNavigateJob={jobId => navigate('jobs', jobId)} onCancelCommand={cmdId => isLocalApi ? apiFetch(`/api/commands/${cmdId}/cancel`,{method:'POST'}).catch(e=>setError(safeMessage(e))) : updateDoc(doc(db,'commands',cmdId),{status:'cancelled'}).catch(e=>setError(safeMessage(e)))}/>}
  {showAddProject && <AddProjectModal onClose={()=>setShowAddProject(false)} onSubmit={(op,inputs,stay)=>submit(op,inputs,undefined,stay)} busy={busy} commands={commands} onAdded={newId=>{setProject(newId);setSelected(null);setHistory([]);setShowAddProject(false);}}/>}
  {showWorkflowModal && <WorkflowGuideModal onClose={()=>setShowWorkflowModal(false)}/>}
  </main></div>;
}
function AppTests({project,commands,onSubmit,busy,workerTestPlans}:{project:string;commands:Data[];onSubmit:(operation:string,inputs?:Data,stay?:boolean)=>void;busy:boolean;workerTestPlans?:string[]}) {
  const [plan,setPlan]=useState('');
  const listing=commands.find(x=>x.operation==='tests.run'&&x.project_id===project&&x.status==='succeeded'&&x.inputs?.action==='list');
  const plans:string[]=useMemo(() => {
    if (Array.isArray(workerTestPlans) && workerTestPlans.length > 0) return workerTestPlans;
    return listing?.result?.plans || [];
  }, [workerTestPlans, listing?.result?.plans]);
  const hasPlans = plans.length > 0;
  const isLoadingPlans=commands.some(x=>x.operation==='tests.run'&&x.project_id===project&&['queued','running'].includes(x.status)&&x.inputs?.action==='list');
  const isRunningPlan=commands.some(x=>x.operation==='tests.run'&&x.project_id===project&&['queued','running'].includes(x.status)&&x.inputs?.action==='plan');
  const isRunningVisual=commands.some(x=>x.operation==='tests.run'&&x.project_id===project&&['queued','running'].includes(x.status)&&x.inputs?.action==='visual');
  const latestRun=commands.find(x=>x.operation==='tests.run'&&x.project_id===project&&['plan','visual'].includes(x.inputs?.action||''));
  const hasRequested=useRef<string|null>(null);

  useEffect(()=>{
    if (!hasPlans && !listing && !isLoadingPlans && !busy && hasRequested.current !== project) {
      hasRequested.current = project;
      onSubmit('tests.run', { action: 'list' }, true);
    }
  }, [project, hasPlans, listing, isLoadingPlans, busy, onSubmit]);

  useEffect(()=>{
    if (plans.length > 0 && (!plan || !plans.includes(plan))) {
      setPlan(plans[0]);
    }
  }, [plans.join(','), plan]);

  return <div className="page narrow"><div className="page-heading"><div><h1>Run Unit Tests</h1><p>Choose an Xcode test plan or run a simulator visual check, as in the CLI.</p></div></div><div className="panel section"><h2>Available test plans</h2><p>{isLoadingPlans ? 'Loading test plans from your Mac worker…' : plans.length ? `${plans.length} plan(s) available in project.` : 'No Xcode test plans detected in this project yet.'}</p>{plans.length>0?<div className="test-run"><label className="field"><span>Test plan</span><select value={plan} onChange={e=>setPlan(e.target.value)}>{plans.map(name=><option key={name} value={name}>{name}</option>)}</select></label><ActionButton className="primary" disabled={busy||!plan||isRunningPlan} disabledReason={isRunningPlan ? 'Test plan is currently executing…' : !plan ? 'Select a test plan first' : 'Worker is busy'} onClick={()=>onSubmit('tests.run',{action:'plan',plan},true)}>{isRunningPlan ? <><span className="spin">⟳</span> Running test plan…</> : 'Run selected plan →'}</ActionButton></div>:<ActionButton className="secondary" disabled={busy || isLoadingPlans} disabledReason={isLoadingPlans ? 'Loading test plans from Mac…' : 'Worker is busy'} onClick={()=>onSubmit('tests.run',{action:'list'},true)}>{isLoadingPlans ? <><span className="spin">⟳</span> Loading test plans…</> : 'Scan for test plans'}</ActionButton>}<div className="test-run"><h2>Simulator visual check</h2><p>Build, launch, and capture simulator screenshots.</p><ActionButton className="primary" disabled={busy || isRunningVisual} disabledReason={isRunningVisual ? 'Visual check is currently running…' : 'Worker is busy'} onClick={()=>onSubmit('tests.run',{action:'visual'},true)}>{isRunningVisual ? <><span className="spin">⟳</span> Running visual check…</> : 'Run visual check →'}</ActionButton></div>{latestRun&&<div style={{marginTop:'1.5rem'}}><div className="eyebrow">LATEST TEST EXECUTION · {latestRun.status.toUpperCase()}</div>{latestRun.error&&<p className="error">{latestRun.error}</p>}<RunLog command={latestRun}/></div>}</div></div>;
}
function Field({label,value,onChange,placeholder,textarea=false}: {label:string;value:string;onChange:(value:string)=>void;placeholder?:string;textarea?:boolean}) {return <label className="field"><span>{label}</span>{textarea?<textarea value={value} onChange={e=>onChange(e.target.value)} placeholder={placeholder}/>:<input value={value} onChange={e=>onChange(e.target.value)} placeholder={placeholder}/>}</label>}
async function uploadArtifact(project: string, file: File): Promise<string> {
  if (file.size > 10 * 1024 * 1024 || file.size === 0) {
    throw new Error('Choose a file under 10 MB.');
  }
  const id = crypto.randomUUID();
  const contentType = file.type || (file.name.endsWith('.md') ? 'text/markdown' : file.name.endsWith('.json') ? 'application/json' : 'text/plain');

  if ((window as any).__IS_LOCAL_API__) {
    const buffer = await file.arrayBuffer();
    const bytes = new Uint8Array(buffer);
    let binary = '';
    for (const byte of bytes) binary += String.fromCharCode(byte);
    const b64 = btoa(binary);
    const res = await apiFetch(`/api/projects/${encodeURIComponent(project)}/artifacts`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        id,
        name: file.name,
        content_type: contentType,
        data: b64,
        size: file.size
      })
    });
    if (!res.ok) {
      throw new Error(`Failed to upload artifact locally: ${res.statusText}`);
    }
    return id;
  }

  const count = Math.ceil(file.size / 120000);
  await setDoc(doc(db, 'projects', project, 'artifacts', id), {
    name: file.name,
    content_type: contentType,
    size: file.size,
    chunk_count: count,
    created_at: serverTimestamp()
  });
  for (let i = 0; i < count; i++) {
    const bytes = new Uint8Array(await file.slice(i * 120000, (i + 1) * 120000).arrayBuffer());
    let binary = '';
    for (const byte of bytes) binary += String.fromCharCode(byte);
    await setDoc(doc(db, 'projects', project, 'artifacts', id, 'chunks', String(i).padStart(4, '0')), {
      data: btoa(binary)
    });
  }
  return id;
}
function NewJob({
  project,
  onSubmit,
  busy,
  availableModels = [],
  defaultModels = ['codex'],
  availableMachines = ['local'],
  defaultMachines = ['local'],
  suggestedSubsystems = []
}: {
  project: string;
  onSubmit: (inputs: Data) => void;
  busy: boolean;
  availableModels?: Data[];
  defaultModels: string[];
  availableMachines?: string[];
  defaultMachines: string[];
  suggestedSubsystems?: string[];
}) {
  const [kind, setKind] = useState('feature');
  const [prompt, setPrompt] = useState('');
  const [selectedModels, setSelectedModels] = useState<string[]>(defaultModels.length ? defaultModels : ['codex']);
  const [selectedMachines, setSelectedMachines] = useState<string[]>(defaultMachines.length ? defaultMachines : ['local']);
  const [branch, setBranch] = useState('new');
  const [specFile, setSpecFile] = useState<File | null>(null);
  const [isDragging, setIsDragging] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState('');
  const [preset, setPreset] = useState('balanced');
  const [yolo, setYolo] = useState(false);
  const [stitch, setStitch] = useState(false);
  const [review, setReview] = useState(false);
  const [repro, setRepro] = useState('');
  const [expected, setExpected] = useState('');
  const [subsystems, setSubsystems] = useState('');
  const [vibe, setVibe] = useState('');
  const [planner, setPlanner] = useState('');
  const [builder, setBuilder] = useState('');
  const [reviewer, setReviewer] = useState('');
  const fileInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (defaultModels.length) setSelectedModels(defaultModels);
  }, [defaultModels.join(',')]);

  useEffect(() => {
    if (defaultMachines.length) setSelectedMachines(defaultMachines);
  }, [defaultMachines.join(',')]);

  const choices = [
    ['feature', 'Full feature'],
    ['quick', 'Small tweak'],
    ['bug', 'Bug fix'],
    ['design', 'Design prototype'],
    ['coverage', 'Coverage audit']
  ];

  const catalogList: Array<{ id: string; name?: string }> = [
    ...(availableModels.map(m => ({ id: typeof m === 'string' ? m : m.id, name: typeof m === 'string' ? m : m.name || m.id }))),
    ...defaultModels.map(id => ({ id, name: id })),
    { id: 'codex', name: 'OpenAI Codex' },
    { id: 'gemini-3.1-pro-preview', name: 'Gemini 3.1 Pro' },
    { id: 'gemini-2.5-flash', name: 'Gemini 2.5 Flash' },
    { id: 'claude-3-7-sonnet-20250219', name: 'Claude 3.7 Sonnet' },
    { id: 'gpt-5', name: 'GPT-5' }
  ];
  const allModelOptions = Array.from(new Map(catalogList.map(item => [item.id, item])).values());

  const allMachineOptions = Array.from(new Set([
    ...availableMachines,
    ...defaultMachines,
    'local'
  ].filter(Boolean)));

  const inputs = {
    job_type: kind,
    prompt,
    allowed_models: selectedModels,
    allowed_machines: selectedMachines,
    branch_mode: branch,
    preset,
    spec_file_name: specFile?.name || undefined,
    yolo,
    stitch,
    repro: kind === 'bug' ? repro : undefined,
    expected: kind === 'bug' ? expected : undefined,
    subsystems: kind === 'coverage' ? subsystems : undefined,
    vibe: kind === 'design' || stitch ? vibe : undefined,
    planner: planner || undefined,
    builder: builder || undefined,
    reviewer: reviewer || undefined
  };

  const toggleSubsystem = (item: string) => {
    const current = subsystems.split(',').map(s => s.trim()).filter(Boolean);
    if (current.includes(item)) {
      setSubsystems(current.filter(s => s !== item).join(', '));
    } else {
      setSubsystems([...current, item].join(', '));
    }
  };

  async function handleCreate() {
    setUploadError('');
    if (specFile) {
      setUploading(true);
      try {
        const id = await uploadArtifact(project, specFile);
        onSubmit({ ...inputs, artifact_id: id });
      } catch (e) {
        setUploadError(safeMessage(e));
      } finally {
        setUploading(false);
      }
    } else {
      onSubmit(inputs);
    }
  }

  return (
    <div className="page narrow">
      <div className="page-heading">
        <div>
          <h1>New job</h1>
          <p>Choose the same workflow and options offered by the CLI.</p>
        </div>
      </div>
      <div className="panel form-panel">
        <h2>1. What kind of work?</h2>
        <div className="choice-grid">
          {choices.map(([value, label]) => (
            <button
              key={value}
              className={kind === value ? 'chosen' : ''}
              onClick={() => setKind(value)}
            >
              {label}
            </button>
          ))}
        </div>

        <h2>2. Describe the job</h2>
        <Field label="Request" value={prompt} onChange={setPrompt} textarea placeholder="What should Orchestrator do?" />

        <div className="spec-section">
          <span className="spec-label">External specification (optional)</span>
          <div
            className={'spec-dropzone ' + (isDragging ? 'dragover ' : '') + (specFile ? 'has-file ' : '')}
            onDragOver={e => { e.preventDefault(); setIsDragging(true); }}
            onDragLeave={e => { e.preventDefault(); setIsDragging(false); }}
            onDrop={e => {
              e.preventDefault();
              setIsDragging(false);
              const f = e.dataTransfer.files?.[0];
              if (f) { setSpecFile(f); }
            }}
          >
            <input
              type="file"
              ref={fileInputRef}
              style={{ display: 'none' }}
              accept=".md,.markdown,.txt,.json,.yaml,.yml,.pdf,.swift,text/*"
              onChange={e => {
                const f = e.target.files?.[0];
                if (f) { setSpecFile(f); }
              }}
            />
            {specFile ? (
              <div className="spec-file-info">
                <span className="file-icon">📄</span>
                <div className="file-details">
                  <strong>{specFile.name}</strong>
                  <small>{(specFile.size / 1024).toFixed(1)} KB</small>
                </div>
                <button
                  type="button"
                  className="remove-file-btn"
                  onClick={() => { setSpecFile(null); if (fileInputRef.current) fileInputRef.current.value = ''; }}
                >
                  ✕ Remove
                </button>
              </div>
            ) : (
              <div className="spec-dropzone-prompt">
                <span className="drop-icon">📁</span>
                <p><strong>Drag & drop specification file here</strong> or</p>
                <button type="button" className="secondary browse-btn" onClick={() => fileInputRef.current?.click()}>
                  Browse files
                </button>
                <small>Markdown (.md), Text, PDF, JSON up to 10 MB</small>
              </div>
            )}
          </div>
          {uploadError && <p className="error">{uploadError}</p>}
        </div>

        {kind === 'bug' && (
          <>
            <Field label="Reproduction steps" value={repro} onChange={setRepro} textarea />
            <Field label="Expected behavior" value={expected} onChange={setExpected} textarea />
          </>
        )}

        {kind === 'coverage' && (
          <div>
            <Field label="Subsystems to audit" value={subsystems} onChange={setSubsystems} placeholder="e.g. DocumentProcessor, Authentication, Networking" />
            <div className="subsystem-chips">
              <span>Suggestions:</span>
              {suggestedSubsystems.map(s => {
                const active = subsystems.split(',').map(x => x.trim()).includes(s);
                return (
                  <button
                    type="button"
                    key={s}
                    className={'subsystem-chip ' + (active ? 'active' : '')}
                    onClick={() => toggleSubsystem(s)}
                  >
                    {active ? '✓ ' : ''}{s}
                  </button>
                );
              })}
            </div>
          </div>
        )}

        {(kind === 'design' || stitch) && (
          <Field label="Preferred visual vibe" value={vibe} onChange={setVibe} />
        )}

        <h2>3. Execution settings</h2>
        <div className="form-grid">
          <label className="field">
            <span>Branch mode</span>
            <select value={branch} onChange={e => setBranch(e.target.value)}>
              <option value="new">New branch</option>
              <option value="current">Current branch</option>
              <option value="manual">Manual / no Git actions</option>
            </select>
          </label>
          <label className="field">
            <span>Model preset</span>
            <select value={preset} onChange={e => setPreset(e.target.value)}>
              <option value="balanced">Balanced (Standard default)</option>
              <option value="fast">Fast (Quick fixes)</option>
              <option value="hard-bug">Hard bug (Deep reasoning)</option>
              <option value="architecture">Architecture (Major refactor)</option>
            </select>
            <small className="field-hint">
              {preset === 'fast' && 'Fast: Lowest latency and token cost. Uses Flash models for planning and review. Best for minor tweaks and quick fixes.'}
              {preset === 'hard-bug' && 'Hard bug: Deep reasoning tier. Uses frontier reasoning for builder to investigate subtle state, race conditions, and memory leaks.'}
              {preset === 'architecture' && 'Architecture: Highest capability. Uses frontier models across planning and building for major system design and refactoring.'}
              {preset === 'balanced' && 'Balanced: High-context planning with specialized coding. Recommended default for regular features and standard fixes.'}
            </small>
          </label>
        </div>
        <label className="check">
          <input type="checkbox" checked={stitch} onChange={e => setStitch(e.target.checked)} /> Design first with Stitch
        </label>
        <label className="check">
          <input type="checkbox" checked={yolo} onChange={e => setYolo(e.target.checked)} /> YOLO mode: dispatch after planning
        </label>

        <details className="advanced">
          <summary>Advanced settings (models, machines, roles)</summary>
          <div className="advanced-fields">
            <MultiSelectDropdown
              label="Allowed models"
              hint="Baseline from your LLM configuration. Tweak models allowed for this job."
              options={allModelOptions.map(m => ({
                id: m.id,
                label: m.id,
                desc: m.name && m.name !== m.id ? m.name : undefined
              }))}
              selected={selectedModels}
              onChange={setSelectedModels}
              ariaLabel="Allowed models"
            />

            <MultiSelectDropdown
              label="Allowed machines"
              hint="Select machines from your configured fleet."
              options={allMachineOptions.map(name => ({
                id: name,
                label: name
              }))}
              selected={selectedMachines}
              onChange={setSelectedMachines}
              ariaLabel="Allowed machines"
            />

            <div className="role-grid">
              <label className="field">
                <span>Planner model role</span>
                <small className="field-hint">Plans tasks and identifies likely files</small>
                <select value={planner} onChange={e => setPlanner(e.target.value)} aria-label="Planner model override">
                  <option value="">Default (Follows Preset)</option>
                  {allModelOptions.map(m => (
                    <option key={m.id} value={m.id}>
                      {m.name && m.name !== m.id ? `${m.name} (${m.id})` : m.id}
                    </option>
                  ))}
                </select>
              </label>
              <label className="field">
                <span>Builder model role</span>
                <small className="field-hint">Writes Swift code & runs build loops</small>
                <select value={builder} onChange={e => setBuilder(e.target.value)} aria-label="Builder model override">
                  <option value="">Default (Follows Preset)</option>
                  {allModelOptions.map(m => (
                    <option key={m.id} value={m.id}>
                      {m.name && m.name !== m.id ? `${m.name} (${m.id})` : m.id}
                    </option>
                  ))}
                </select>
              </label>
              <label className="field">
                <span>Reviewer model role</span>
                <small className="field-hint">Verifies criteria & checks regressions</small>
                <select value={reviewer} onChange={e => setReviewer(e.target.value)} aria-label="Reviewer model override">
                  <option value="">Default (Follows Preset)</option>
                  {allModelOptions.map(m => (
                    <option key={m.id} value={m.id}>
                      {m.name && m.name !== m.id ? `${m.name} (${m.id})` : m.id}
                    </option>
                  ))}
                </select>
              </label>
            </div>

            <div style={{ marginTop: 'var(--space-4)', paddingTop: 'var(--space-3)', borderTop: '1px solid var(--border-subtle)' }}>
              <button
                type="button"
                className="secondary"
                style={{ padding: '4px 10px', fontSize: 'var(--text-xs)' }}
                onClick={() => setReview(!review)}
              >
                {review ? 'Hide command JSON' : 'Preview command JSON'}
              </button>
              {review && (
                <div className="review" style={{ marginTop: 'var(--space-2)' }}>
                  <strong>Command payload</strong>
                  <pre>{JSON.stringify(inputs, null, 2)}</pre>
                </div>
              )}
            </div>
          </div>
        </details>

        <div className="form-actions">
          <ActionButton
            className="primary"
            disabled={busy || uploading || !prompt.trim() || !selectedModels.length || !selectedMachines.length}
            disabledReason={
              uploading
                ? 'Uploading specification file…'
                : busy
                ? 'Worker is currently busy processing another command'
                : !prompt.trim()
                ? 'Enter a request description to create a job'
                : !selectedModels.length
                ? 'Allow at least one model in Advanced settings'
                : !selectedMachines.length
                ? 'Allow at least one machine in fleet'
                : undefined
            }
            onClick={handleCreate}
          >
            {uploading ? 'Uploading spec…' : 'Create real job →'}
          </ActionButton>
        </div>
      </div>
    </div>
  );
}
interface StatusInfo {
  title: string;
  owner: string;
  desc: string;
  badgeClass: string;
  transitions: Array<{
    action: string;
    label: string;
    target: string;
    desc: string;
  }>;
}

const STATUS_DEFINITIONS: Record<string, StatusInfo> = {
  designing: {
    title: 'Designing',
    owner: 'Planner Agent',
    desc: 'Generating technical requirements, implementation scope, and sub-task plan.',
    badgeClass: 'designing',
    transitions: [
      { action: 'job.approve_subtasks', label: 'Approve Plan & Sub-tasks', target: 'planned', desc: 'Accept generated tasks and mark job ready for execution.' },
      { action: 'job.revise', label: 'Revise Plan', target: 'designing', desc: 'Provide requirements feedback to regenerate plan.' }
    ]
  },
  planned: {
    title: 'Planned',
    owner: 'Human Operator',
    desc: 'Plan and sub-tasks are finalized. Ready to schedule on the worker and assign models.',
    badgeClass: 'planned',
    transitions: [
      { action: 'job.schedule', label: 'Schedule & Dispatch', target: 'scheduled', desc: 'Enqueue the job on the Mac worker engine.' },
      { action: 'job.approve_subtasks', label: 'Adjust Sub-tasks', target: 'planned', desc: 'Modify and re-confirm subtask selections.' },
      { action: 'job.revise', label: 'Revise Plan', target: 'designing', desc: 'Send the plan back to the planner with updated requirements.' }
    ]
  },
  scheduled: {
    title: 'Scheduled',
    owner: 'Mac Worker Engine',
    desc: 'Job is queued waiting for execution on the local Mac worker fleet.',
    badgeClass: 'scheduled',
    transitions: [
      { action: 'job.execute', label: 'Start Execution', target: 'executing', desc: 'Launch builder model agents on the assigned worktree.' },
      { action: 'job.revise', label: 'Revise & Cancel Run', target: 'planned', desc: 'Pull back from the queue to modify parameters.' }
    ]
  },
  executing: {
    title: 'Executing',
    owner: 'Builder Agent',
    desc: 'Agent is writing code, applying changes, and running project builds.',
    badgeClass: 'executing',
    transitions: [
      { action: 'job.resume', label: 'Resume Execution', target: 'executing', desc: 'Continue running remaining sub-tasks in sequence.' },
      { action: 'job.debug', label: 'Switch to Debug Loop', target: 'debugging', desc: 'Trigger automated compiler and unit test fix loop.' },
      { action: 'job.reopen_bug', label: 'Report Bug / Failure', target: 'debugging', desc: 'Log unexpected errors or regression failures.' }
    ]
  },
  debugging: {
    title: 'Debugging',
    owner: 'Debugger Agent',
    desc: 'Analyzing compiler errors, runtime crash diagnostics, or failing unit tests.',
    badgeClass: 'debugging',
    transitions: [
      { action: 'job.debug', label: 'Run Auto-Fix Iteration', target: 'debugging', desc: 'Run automated fix loop to resolve compiler or test issues.' },
      { action: 'job.resume', label: 'Resume Standard Run', target: 'executing', desc: 'Return to normal task execution.' },
      { action: 'job.revise', label: 'Provide Manual Guidance', target: 'debugging', desc: 'Give diagnostic hints or override assumptions.' }
    ]
  },
  'review-needed': {
    title: 'Needs Review',
    owner: 'Reviewer Agent / Human',
    desc: 'Code changes complete and tests passing. Ready for PR review and final verification.',
    badgeClass: 'review-needed',
    transitions: [
      { action: 'job.merge', label: 'Merge & Complete', target: 'completed', desc: 'Approve PR and merge code branch to main.' },
      { action: 'job.iterate', label: 'Request Iteration', target: 'executing', desc: 'Send feedback to builder agent for refinements.' },
      { action: 'job.reopen_bug', label: 'Reopen Bug', target: 'debugging', desc: 'Flag regressions or broken acceptance criteria.' }
    ]
  },
  'human-needed': {
    title: 'Human Needed',
    owner: 'Human Operator Required',
    desc: 'Autonomous run paused. Blocked on credentials, clarification, or human approval.',
    badgeClass: 'human-needed',
    transitions: [
      { action: 'job.resume', label: 'Resume Execution', target: 'executing', desc: 'Continue execution after unblocking.' },
      { action: 'job.iterate', label: 'Provide Guidance', target: 'executing', desc: 'Supply specific instructions and resume builder agent.' },
      { action: 'job.revise', label: 'Revise Requirements', target: 'designing', desc: 'Redesign task parameters to bypass blocker.' }
    ]
  },
  completed: {
    title: 'Completed',
    owner: 'Shipped',
    desc: 'Job has been verified, merged to main, or closed.',
    badgeClass: 'completed',
    transitions: [
      { action: 'job.rerun', label: 'Re-run Job', target: 'planned', desc: 'Execute this job again on a fresh branch.' }
    ]
  }
};

interface StatusTransitionDropdownProps {
  job: Data;
  archived: boolean;
  onSelectAction: (operation: string) => void;
  onOpenWorkflow?: () => void;
}

function StatusTransitionDropdown({
  job,
  archived,
  onSelectAction,
  onOpenWorkflow
}: StatusTransitionDropdownProps) {
  const [isOpen, setIsOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  const currentStatus = String(job.status || 'unknown').toLowerCase();
  const def = STATUS_DEFINITIONS[currentStatus] || {
    title: job.status || 'Unknown',
    owner: 'System',
    desc: `Status: ${job.status}`,
    badgeClass: job.status || 'unknown',
    transitions: []
  };

  useEffect(() => {
    if (!isOpen) return;
    const handleClickOutside = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setIsOpen(false);
      }
    };
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setIsOpen(false);
    };
    document.addEventListener('mousedown', handleClickOutside);
    document.addEventListener('keydown', handleKeyDown);
    return () => {
      document.removeEventListener('mousedown', handleClickOutside);
      document.removeEventListener('keydown', handleKeyDown);
    };
  }, [isOpen]);

  const transitions = def.transitions || [];

  return (
    <div className="status-dropdown-container" ref={containerRef}>
      <button
        type="button"
        className={`badge large ${def.badgeClass} status-badge-btn ${isOpen ? 'open' : ''}`}
        onClick={() => setIsOpen(!isOpen)}
        aria-expanded={isOpen}
        aria-haspopup="dialog"
        title="Click to view status details and next transitions"
      >
        <span>{job.status}</span>
        <span className="caret" aria-hidden="true">▾</span>
      </button>

      {isOpen && (
        <div className="status-popover" role="dialog" aria-label="Status transitions">
          <div className="status-popover-header">
            <small>CURRENT STATUS</small>
            <strong>
              <span className={`badge ${def.badgeClass}`}>{job.status}</span>
              <span className="status-owner-pill">{def.owner}</span>
            </strong>
            <p className="status-desc-short" style={{ margin: '6px 0 0' }}>{def.desc}</p>
          </div>

          <div className="status-popover-section-label">Next Available Transitions</div>
          <div className="status-transitions-list">
            {archived ? (
              <p className="status-no-transitions">Archived job (transitions disabled)</p>
            ) : transitions.length > 0 ? (
              transitions.map(t => {
                return (
                  <button
                    key={t.action + t.target}
                    type="button"
                    className="status-transition-item"
                    onClick={() => {
                      setIsOpen(false);
                      onSelectAction(t.action);
                    }}
                  >
                    <div className="status-transition-top">
                      <span className="status-action-name">{t.label}</span>
                      <span className={`badge ${t.target} status-transition-target`}>→ {t.target}</span>
                    </div>
                    <span className="status-transition-desc">{t.desc}</span>
                  </button>
                );
              })
            ) : (
              <p className="status-no-transitions">No next transitions defined</p>
            )}
          </div>

          {onOpenWorkflow && (
            <div className="status-popover-footer">
              <button
                type="button"
                className="workflow-help-link"
                onClick={() => {
                  setIsOpen(false);
                  onOpenWorkflow();
                }}
              >
                <span className="help-icon" style={{ width: 14, height: 14, fontSize: 9 }}>?</span> View Full Workflow Guide →
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function WorkflowGuideModal({ onClose }: { onClose: () => void }) {
  const [activeTab, setActiveTab] = useState<'pipeline' | 'statuses'>('pipeline');

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [onClose]);

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal workflow-modal" onClick={e => e.stopPropagation()}>
        <div className="modal-header">
          <div className="modal-header-title">
            <div className="workflow-modal-icon">⎇</div>
            <div>
              <h2>Orchestrator Workflow & Status Guide</h2>
              <p>Autonomous lifecycle, transition states, and agent handoffs</p>
            </div>
          </div>
          <button type="button" className="modal-close-btn" onClick={onClose} aria-label="Close guide">×</button>
        </div>

        <div className="workflow-tabs">
          <button
            type="button"
            className={activeTab === 'pipeline' ? 'active' : ''}
            onClick={() => setActiveTab('pipeline')}
          >
            Visual Pipeline
          </button>
          <button
            type="button"
            className={activeTab === 'statuses' ? 'active' : ''}
            onClick={() => setActiveTab('statuses')}
          >
            Status Definitions
          </button>
        </div>

        <div className="workflow-modal-body">
          {activeTab === 'pipeline' ? (
            <div>
              <div className="workflow-pipeline">
                <div className="pipeline-stage">
                  <div className="stage-num">1</div>
                  <div className="stage-card">
                    <span className="badge designing">designing</span>
                    <strong>Plan & Strategy</strong>
                    <p>Planner agent inspects repo and generates sub-task breakdown.</p>
                  </div>
                  <div className="stage-arrow">➔</div>
                </div>

                <div className="pipeline-stage">
                  <div className="stage-num">2</div>
                  <div className="stage-card">
                    <span className="badge planned">planned</span>
                    <strong>Review & Dispatch</strong>
                    <p>Human operator confirms tasks and selects allowed models.</p>
                  </div>
                  <div className="stage-arrow">➔</div>
                </div>

                <div className="pipeline-stage">
                  <div className="stage-num">3</div>
                  <div className="stage-card">
                    <span className="badge scheduled">scheduled</span>
                    <strong>Queue on Worker</strong>
                    <p>Enqueued for execution on the local Mac worker fleet.</p>
                  </div>
                  <div className="stage-arrow">➔</div>
                </div>

                <div className="pipeline-stage">
                  <div className="stage-num">4</div>
                  <div className="stage-card primary-exec">
                    <span className="badge executing">executing</span>
                    <strong>Builder Agent Run</strong>
                    <p>Agent writes Swift code, edits files, and runs workspace builds.</p>
                  </div>
                </div>
              </div>

              <div className="workflow-branches-grid">
                <div className="workflow-branch-card warn">
                  <div className="branch-header">
                    <span className="badge debugging">debugging</span>
                    <strong>Auto-Fix Loop</strong>
                  </div>
                  <p>Triggered on compiler errors or broken unit tests. Agent analyzes build output and iterates on fixes.</p>
                  <span className="branch-tag">Loops back to executing or review-needed</span>
                </div>

                <div className="workflow-branch-card danger">
                  <div className="branch-header">
                    <span className="badge human-needed">human-needed</span>
                    <strong>Intervention</strong>
                  </div>
                  <p>Paused for human decision, ambiguous requirements, or manual credentials.</p>
                  <span className="branch-tag">Resumes to executing with guidance</span>
                </div>

                <div className="workflow-branch-card success">
                  <div className="branch-header">
                    <span className="badge review-needed">review-needed</span>
                    <strong>Sign-off & PR</strong>
                  </div>
                  <p>All sub-tasks implemented and tests passing. Ready for PR merge or review feedback.</p>
                  <span className="branch-tag">Merges to completed or iterates</span>
                </div>
              </div>

              <div className="workflow-terminal-row">
                <div className="stage-arrow-down">⬇</div>
                <div className="pipeline-terminal-card">
                  <span className="badge completed">completed</span>
                  <strong>Merged & Shipped</strong>
                  <p>Code is merged into main branch, worktree is cleaned up, and issue is resolved.</p>
                </div>
              </div>
            </div>
          ) : (
            <div className="workflow-statuses-grid">
              {Object.entries(STATUS_DEFINITIONS).map(([key, def]) => (
                <div key={key} className="status-def-card">
                  <div className="status-def-header">
                    <span className={`badge ${def.badgeClass}`}>{key}</span>
                    <span className="status-owner-pill">{def.owner}</span>
                  </div>
                  <h4>{def.title}</h4>
                  <p>{def.desc}</p>
                  <div className="status-def-actions">
                    <small>AVAILABLE TRANSITIONS</small>
                    <div className="actions-chips">
                      {def.transitions.map(t => (
                        <span key={t.action} className="action-chip" title={t.desc}>
                          {t.label} (→ {t.target})
                        </span>
                      ))}
                    </div>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        <div className="modal-footer">
          <span className="workflow-tip">💡 Tip: Tap the status badge on any job detail to quickly jump to next transition actions.</span>
          <button type="button" className="secondary" onClick={onClose}>Close</button>
        </div>
      </div>
    </div>
  );
}

function JobDetail({job,project,archived,back,onAction,onOpenWorkflow}:{job:Data;project:string;archived:boolean;back:()=>void;onAction:(operation:string,inputs?:Data,job?:Data)=>void;onOpenWorkflow?:()=>void}) {
  const [action,setAction]=useState<Action|null>(null);const [value,setValue]=useState('');const [extra,setExtra]=useState('');const [iterationCount,setIterationCount]=useState('5');const [rerunBranch,setRerunBranch]=useState('new');const [file,setFile]=useState<File|null>(null);const [uploading,setUploading]=useState(false);const [uploadError,setUploadError]=useState('');
  const actions=archived?[]:jobActions(job);
  const inputFor=(op:string):Data=>{if(['job.revise','job.iterate','job.reopen_bug'].includes(op)) return {feedback:value}; if(op==='job.link_logs') return {log_paths:value.split('\n').map(x=>x.trim()).filter(Boolean)};if(op==='job.ask_ai') return {question:value,model:extra};if(op==='job.models')return {allowed_models:value.split(',').map(x=>x.trim()).filter(Boolean)};if(op==='job.attach_reference')return {source_url:value,description:extra};if(op==='job.debug')return {feedback:value||undefined,max_iterations:Number(iterationCount)};if(op==='job.rerun')return {branch_mode:rerunBranch};return {}};
  async function runSelected() {
    if (!action) return;
    if (action.operation !== 'job.attach_reference' || !file) { onAction(action.operation,inputFor(action.operation),job); return; }
    setUploading(true);setUploadError('');
    try {
      if (file.size > 10*1024*1024 || file.size === 0 || !['image/png','image/jpeg','image/webp','application/pdf','text/plain'].includes(file.type)) throw new Error('Choose a PNG, JPEG, WebP, PDF, or text file under 10 MB.');
      const id = await uploadArtifact(project, file);
      onAction(action.operation,{artifact_id:id,description:extra},job);
    } catch(e) {setUploadError(safeMessage(e));} finally {setUploading(false);}
  }
  return <div className="page"><button className="back" onClick={back}>← All jobs</button><div className="page-heading"><div><h1>{job.title||job.job_id}</h1></div><StatusTransitionDropdown job={job} archived={archived} onSelectAction={chosenOp => { const chosen = actions.find(a => a.operation === chosenOp); if (chosen) { setAction(chosen); setValue(''); setExtra(''); } }} onOpenWorkflow={onOpenWorkflow}/></div><div className="detail-grid"><div><div className="panel section"><h2>Plan & progress</h2><p>{job.plan?.summary||'No plan summary published yet.'}</p>{job.plan?.tasks?.length>0 && <ol className="tasks">{job.plan.tasks.map((task:any,i:number)=><li key={i} className={job.completed_task_indices?.includes(i)?'done':''}>{typeof task==='string'?task:task.title||task.description||JSON.stringify(task)}</li>)}</ol>}</div><div className="panel section"><h2>Context</h2><div className="facts">{[['Branch',job.branch],['Issue',job.issue_number],['Pull request',job.pr_number],['Planner',job.planner],['Builder',job.builder],['Reviewer',job.reviewer],['Models',job.allowed_models?.join(', ')],['Machines',job.allowed_machines?.join(', ')],['Logs',job.last_manual_log_paths?.join(', ')]].filter(x=>x[1]).map(([label,value])=><div key={label}><span>{label}</span><strong>{value}</strong></div>)}</div></div></div><div className="panel section action-panel"><h2>{archived?'Archived job':'Job actions'}</h2><p>{archived?'Restore this job from Configuration & Tools to run new actions.':'Actions follow this job\'s CLI status.'}</p>{!archived&&actions.length>0&&<label className="field"><span>Select action</span><select value={action?.operation||''} onChange={e=>{const chosen=actions.find(a=>a.operation===e.target.value)||null;setAction(chosen);setValue('');setExtra('')}}><option value="">Select an action…</option>{actions.map((item,i)=><option key={item.operation+i} value={item.operation}>{item.label}</option>)}</select></label>}{action && <div className="action-form"><h3>{action.label}</h3>{['job.revise','job.iterate','job.reopen_bug','job.ask_ai','job.link_logs','job.models','job.attach_reference','job.debug'].includes(action.operation) && <Field label={action.operation==='job.ask_ai'?'Question':action.operation==='job.link_logs'?'Project-relative log paths, one per line':action.operation==='job.models'?'Model IDs, comma separated':action.operation==='job.attach_reference'?'HTTP(S) reference URL':'Feedback'} value={value} onChange={setValue} textarea={['job.revise','job.iterate','job.ask_ai'].includes(action.operation)}/ >}{action.operation==='job.debug'&&<Field label="Maximum debug iterations (1–20)" value={iterationCount} onChange={setIterationCount}/ >}{action.operation==='job.rerun'&&<label className="field"><span>Branch mode</span><select value={rerunBranch} onChange={e=>setRerunBranch(e.target.value)}><option value="new">New branch</option><option value="current">Current branch</option><option value="manual">Manual</option></select></label>}{action.operation==='job.attach_reference'&&<label className="field"><span>Or upload a PNG, JPEG, WebP, PDF, or text file (10 MB max)</span><input type="file" accept="image/png,image/jpeg,image/webp,application/pdf,text/plain" onChange={e=>setFile(e.target.files?.[0]||null)}/></label>}{uploadError&&<p className="error">{uploadError}</p>}{['job.ask_ai','job.attach_reference'].includes(action.operation)&&<Field label={action.operation==='job.ask_ai'?'Model ID':'Description'} value={extra} onChange={setExtra}/>}<ActionButton className="primary" disabled={uploading} disabledReason="Uploading reference artifact…" onClick={runSelected}>{uploading?'Uploading…':'Run action →'}</ActionButton></div>}</div></div></div>;
}
function ToolPanel({item,project,onSubmit,busy,commands,projects,onProject,onAddProject}:{item:Action;project:string;onSubmit:(operation:string,inputs?:Data)=>void;busy:boolean;commands:Data[];projects:string[];onProject:(id:string)=>void;onAddProject:()=>void}) {
  const op=item.operation;
  const [name,setName]=useState('');const [value,setValue]=useState('');const [content,setContent]=useState('');const [extra,setExtra]=useState('');const [modelSelection,setModelSelection]=useState<string[]|null>(null);
  const catalogRequested=useRef<string|null>(null);
  useEffect(()=>{setName('');setValue('');setContent('');setExtra('');setModelSelection(null);catalogRequested.current=null},[item.operation,project]);
  const latest=commands.find(x=>x.operation===op&&x.project_id===project);
  const listing=commands.find(x=>x.operation===op&&x.project_id===project&&x.status==='succeeded'&&['list','status','show'].includes(x.inputs?.action||'list'))?.result;
  const modelResult=commands.find(x=>x.operation==='tool.models'&&x.project_id===project&&x.status==='succeeded'&&Array.isArray(x.result?.models))?.result;
  const models:Data[]=modelResult?.models||[];
  const savedModels:string[]=modelResult?.selected||[];
  const selectedModels:string[]=modelSelection??savedModels;

  const isPending = (targetOp: string, matchInputs?: ((inputs: Data) => boolean) | Data) =>
    commands.some(x => {
      if (x.operation !== targetOp || x.project_id !== project || !['queued', 'running'].includes(x.status)) return false;
      if (!matchInputs) return true;
      if (typeof matchInputs === 'function') return matchInputs(x.inputs || {});
      const inp = x.inputs || {};
      return Object.entries(matchInputs).every(([k, v]) => inp[k] === v);
    });

  const modelLoading=isPending('tool.models', i => !i.refresh && !i.live_discovery && !Array.isArray(i.allowed_models));
  const modelSyncing=isPending('tool.models', i => i.refresh === true);
  const modelDiscovering=isPending('tool.models', i => i.live_discovery === true);
  const modelSaving=isPending('tool.models', i => Array.isArray(i.allowed_models));
  const modelChanges=selectedModels.length!==savedModels.length||selectedModels.some(id=>!savedModels.includes(id));
  useEffect(()=>setModelSelection(null),[modelResult?.selected?.join(',')]);
  useEffect(()=>{if(op==='tool.models'&&!modelResult&&!latest&&!busy&&catalogRequested.current!==project){catalogRequested.current=project;onSubmit('tool.models',{})}},[op,project,modelResult,latest,busy,onSubmit]);
  const machineNames:string[]=(listing?.machines||latest?.result?.machines||[]).map((m:Data)=>m.name);
  const files:string[]=[...new Set([...Object.keys(listing?.files||{}),...(listing?.role_prompts||[]),...(latest?.result?.role_prompts||[])])];
  const docs:string[]=listing?.documents||latest?.result?.documents||[];
  const recipients:string[]=listing?.notification_emails||latest?.result?.notification_emails||[];
  const archived:string[]=listing?.jobs||latest?.result?.jobs||[];

  return <div className="panel section tool-panel"><h2>{item.label}</h2>
    {op==='tool.models'&&<>
      <p>Sync the model catalog or discover live models across configured providers.</p>
      <div className="tool-action-buttons">
        <button className="secondary" disabled={busy||modelLoading||modelSyncing||modelDiscovering} onClick={()=>onSubmit(op,{})}>
          {modelLoading ? <><span className="spin">⟳</span> Reloading catalog…</> : 'Reload catalog'}
        </button>
        <button className="secondary" disabled={busy||modelLoading||modelSyncing||modelDiscovering} onClick={()=>onSubmit(op,{refresh:true})}>
          {modelSyncing ? <><span className="spin">⟳</span> Syncing catalog…</> : 'Sync catalog'}
        </button>
        <button className="primary" disabled={busy||modelLoading||modelSyncing||modelDiscovering} onClick={()=>onSubmit(op,{live_discovery:true})}>
          {modelDiscovering ? <><span className="spin">⟳</span> Discovering live models…</> : 'Discover live models →'}
        </button>
      </div>
      {latest&&latest.status==='succeeded'&&(
        <div className="catalog-status-banner">
          <span>✓ <strong>{latest.inputs?.live_discovery?'Live model discovery completed':latest.inputs?.refresh?'Catalog sync completed':'Catalog reloaded'}</strong>: {models.length} model{models.length===1?'':'s'} in catalog ({selectedModels.length} enabled)</span>
          {latest.result?.sync?.message&&<small>{latest.result.sync.message}</small>}
        </div>
      )}
      {models.length>0&&<>
        <div style={{display:'flex',justifyContent:'space-between',alignItems:'center',flexWrap:'wrap',gap:'8px',marginTop:'1.25rem'}}>
          <div>
            <h3 style={{margin:0}}>Enabled models · {selectedModels.length} of {models.length}</h3>
            {modelChanges&&<span style={{color:'var(--accent-primary)',fontSize:'var(--text-xs)',fontWeight:'var(--fw-semibold)'}}>Unsaved changes</span>}
          </div>
          <div style={{display:'flex',gap:'6px'}}>
            <button
              type="button"
              className="secondary"
              style={{padding:'4px 10px',fontSize:'var(--text-xs)',margin:0}}
              disabled={busy||modelSaving||selectedModels.length===models.length}
              onClick={()=>setModelSelection(models.map(m=>m.id))}
            >
              Select all
            </button>
            <button
              type="button"
              className="secondary"
              style={{padding:'4px 10px',fontSize:'var(--text-xs)',margin:0}}
              disabled={busy||modelSaving||selectedModels.length<=1}
              onClick={()=>setModelSelection([models[0]?.id].filter(Boolean))}
            >
              Deselect all
            </button>
          </div>
        </div>
        <p style={{marginTop:'0.5rem'}}>Switch models on or off for new jobs. At least one model must stay enabled.</p>
        <ModelList models={models} selected={selectedModels} disabled={busy||modelSaving} onToggle={id=>setModelSelection(current=>{const chosen=current??savedModels;return chosen.includes(id)?chosen.filter(model=>model!==id):[...chosen,id]})}/>
        <button className="primary" disabled={busy||modelSaving||!modelChanges||!selectedModels.length} onClick={()=>onSubmit('tool.models',{allowed_models:selectedModels})}>
          {modelSaving?<><span className="spin">⟳</span> Saving models…</>:'Save enabled models →'}
        </button>
      </>}
      {!models.length&&<p>{modelLoading?'Loading the model catalog from the Mac worker…':'Run Reload catalog to load the available models.'}</p>}
    </>}
    {op==='tool.keys'&&<>
      <p>Provider sign-in and fallback API keys are managed in the local CLI on the Mac. This reports readiness without sending credentials to Firestore.</p>
      <div className="tool-action-buttons">
        <button className="primary" disabled={busy||isPending(op,i=>!i.action||i.action==='status')} onClick={()=>onSubmit(op,{action:'status'})}>
          {isPending(op,i=>!i.action||i.action==='status') ? <><span className="spin">⟳</span> Checking readiness…</> : 'Check provider readiness →'}
        </button>
        <button className="secondary" disabled={busy||isPending(op,{action:'clear'})} onClick={()=>{if(window.confirm('Clear saved fallback API keys from this project on the Mac?'))onSubmit(op,{action:'clear'})}}>
          {isPending(op,{action:'clear'}) ? <><span className="spin">⟳</span> Clearing keys…</> : 'Clear saved fallback API keys'}
        </button>
      </div>
    </>}
    {op==='tool.fleet'&&<>
      <p>Manage and probe worker machines configured for remote execution.</p>
      <div className="tool-action-buttons">
        <button className="primary" disabled={busy||isPending(op,i=>!i.action||i.action==='list')} onClick={()=>onSubmit(op,{action:'list'})}>
          {isPending(op,i=>!i.action||i.action==='list') ? <><span className="spin">⟳</span> Checking fleet…</> : 'Fleet status →'}
        </button>
        <button className="secondary" disabled={busy||isPending(op,{action:'probe'})} onClick={()=>onSubmit(op,{action:'probe'})}>
          {isPending(op,{action:'probe'}) ? <><span className="spin">⟳</span> Probing machines…</> : 'Probe machines'}
        </button>
        <button className="secondary" disabled={busy||isPending(op,{action:'discover'})} onClick={()=>onSubmit(op,{action:'discover'})}>
          {isPending(op,{action:'discover'}) ? <><span className="spin">⟳</span> Discovering remote machines…</> : 'Discover remote machines'}
        </button>
        <button className="secondary" disabled={busy||isPending(op,{action:'purge'})} onClick={()=>{if(window.confirm('Purge stale processes & cache across fleet machines?'))onSubmit(op,{action:'purge'})}}>
          {isPending(op,{action:'purge'}) ? <><span className="spin">⟳</span> Purging stale processes…</> : 'Purge stale processes'}
        </button>
      </div>
      <Field label="Select active machines (comma separated)" value={value} onChange={setValue} placeholder={machineNames.join(', ')}/>
      <button className="secondary" disabled={busy||!value.trim()||isPending(op,{action:'select'})} onClick={()=>{onSubmit(op,{action:'select',machine_ids:value.split(',').map(x=>x.trim()).filter(Boolean)});setValue('')}}>
        {isPending(op,{action:'select'}) ? <><span className="spin">⟳</span> Saving active machines…</> : 'Save active machines'}
      </button>
      <details className="advanced"><summary>Add remote machine</summary>
        <div className="form-grid">
          <Field label="Machine name" value={name} onChange={setName} placeholder="builder-1"/>
          <Field label="SSH target" value={extra} onChange={setExtra} placeholder="user@host or host"/>
        </div>
        <Field label="Absolute remote repository path" value={content} onChange={setContent} placeholder="/Users/.../project"/>
        <button className="primary" disabled={busy||!name.trim()||!extra.trim()||!content.trim()||isPending(op,{action:'add'})} onClick={()=>{onSubmit(op,{action:'add',name:name.trim(),ssh_target:extra.trim(),repo_path:content.trim()});setName('');setExtra('');setContent('')}}>
          {isPending(op,{action:'add'}) ? <><span className="spin">⟳</span> Adding machine…</> : 'Add machine →'}
        </button>
      </details>
      {machineNames.length>0&&<details className="advanced"><summary>Manage machine by name</summary>
        <label className="field"><span>Machine</span><select value={name} onChange={e=>setName(e.target.value)}><option value="">Select machine…</option>{machineNames.map(x=><option key={x}>{x}</option>)}</select></label>
        <div className="tool-action-buttons">
          <button className="secondary" disabled={busy||!name||isPending(op,{action:'enable'})} onClick={()=>onSubmit(op,{action:'enable',name})}>
            {isPending(op,{action:'enable'}) ? <><span className="spin">⟳</span> Enabling machine…</> : 'Enable machine'}
          </button>
          <button className="secondary" disabled={busy||!name||isPending(op,{action:'disable'})} onClick={()=>onSubmit(op,{action:'disable',name})}>
            {isPending(op,{action:'disable'}) ? <><span className="spin">⟳</span> Disabling machine…</> : 'Disable machine'}
          </button>
          <button className="secondary" disabled={busy||!name||isPending(op,{action:'remove'})} onClick={()=>{if(window.confirm(`Remove machine ${name}?`))onSubmit(op,{action:'remove',name})}}>
            {isPending(op,{action:'remove'}) ? <><span className="spin">⟳</span> Removing machine…</> : 'Remove machine'}
          </button>
        </div>
        <Field label="Rename to" value={extra} onChange={setExtra} placeholder="new-machine-name"/>
        <button className="secondary" disabled={busy||!name||!extra.trim()||isPending(op,{action:'rename'})} onClick={()=>{onSubmit(op,{action:'rename',name,new_name:extra.trim()});setExtra('')}}>
          {isPending(op,{action:'rename'}) ? <><span className="spin">⟳</span> Renaming machine…</> : 'Rename machine'}
        </button>
      </details>}
    </>}
    {op==='tool.audit'&&<>
      <p>Run an automated check of project requirements, tools, and connected worker machines.</p>
      <div className="tool-action-buttons">
        <button className="primary" disabled={busy||isPending(op)} onClick={()=>onSubmit(op,{})}>
          {isPending(op) ? <><span className="spin">⟳</span> Running prerequisite audit…</> : 'Run prerequisite audit →'}
        </button>
      </div>
    </>}
    {op==='tool.base_branch'&&<>
      <p>Check the current Git base branch or update it for new jobs in this project.</p>
      <Field label="Base branch" value={value} onChange={setValue} placeholder={latest?.result?.branch||'e.g. main'}/>
      <div className="tool-action-buttons">
        <button className="primary" disabled={busy||!value.trim()||isPending(op,i=>Boolean(i.branch))} onClick={()=>onSubmit(op,{branch:value.trim()})}>
          {isPending(op,i=>Boolean(i.branch)) ? <><span className="spin">⟳</span> Setting base branch…</> : 'Set base branch →'}
        </button>
        <button className="secondary" disabled={busy||isPending(op,i=>!i.branch)} onClick={()=>onSubmit(op,{})}>
          {isPending(op,i=>!i.branch) ? <><span className="spin">⟳</span> Checking base branch…</> : 'Show base branch'}
        </button>
      </div>
    </>}
    {op==='tool.project'&&<>
      <p>Switch among projects configured on the Mac worker, scan for local repositories, or register a new project.</p>
      <label className="field"><span>Active project</span><select value={project} onChange={e=>{if(e.target.value==='__add__')onAddProject();else onProject(e.target.value)}}>{projects.map(id=><option key={id} value={id}>{id}</option>)}<option value="__add__" className="add-project-option" style={{ color: '#38bdf8', fontWeight: 600 }}>＋ Add new project…</option></select></label>
      <div className="tool-action-buttons">
        <button className="primary" disabled={busy||isPending(op,i=>!i.action||i.action==='show')} onClick={()=>onSubmit(op,{})}>
          {isPending(op,i=>!i.action||i.action==='show') ? <><span className="spin">⟳</span> Checking project info…</> : 'Show current project info →'}
        </button>
        <button className="secondary" disabled={busy||isPending(op,{action:'discover'})} onClick={()=>onSubmit(op,{action:'discover'})}>
          {isPending(op,{action:'discover'}) ? <><span className="spin">⟳</span> Scanning Mac…</> : 'Scan for projects on Mac'}
        </button>
        <button className="secondary" onClick={onAddProject}>
          ＋ Register new project…
        </button>
      </div>
      <details className="advanced">
        <summary>Advanced: Force workspace sync</summary>
        <p>Your Mac worker continuously auto-syncs local jobs and repository state every 5 seconds. You can also trigger an immediate manual synchronization.</p>
        <button
          type="button"
          className="secondary"
          disabled={busy || isPending('project.refresh')}
          onClick={() => onSubmit('project.refresh', {})}
        >
          {isPending('project.refresh') ? <><span className="spin">⟳</span> Syncing workspace…</> : 'Force sync & refresh workspace'}
        </button>
      </details>
    </>}
    {op==='tool.archive'&&<>
      <p>View archived jobs or restore an archived job back to active status.</p>
      <label className="field"><span>Archived job</span>{archived.length?<select value={name} onChange={e=>setName(e.target.value)}><option value="">Select a job to restore…</option>{archived.map(x=><option key={x}>{x}</option>)}</select>:<input value={name} onChange={e=>setName(e.target.value)} placeholder="Job ID (e.g. 20260923-120000-feature-1)"/>}</label>
      <div className="tool-action-buttons">
        <button className="primary" disabled={busy||!name.trim()||isPending(op,{action:'restore'})} onClick={()=>{onSubmit(op,{action:'restore',job_id:name.trim()});setName('')}}>
          {isPending(op,{action:'restore'}) ? <><span className="spin">⟳</span> Restoring job…</> : 'Restore job →'}
        </button>
        <button className="secondary" disabled={busy||isPending(op,i=>!i.action||i.action==='list')} onClick={()=>onSubmit(op,{action:'list'})}>
          {isPending(op,i=>!i.action||i.action==='list') ? <><span className="spin">⟳</span> Loading archives…</> : 'List archived jobs'}
        </button>
      </div>
    </>}
    {op==='tool.instructions'&&<>
      <p>Configure AI instruction files and role prompt templates for agent workflows.</p>
      <div className="tool-action-buttons">
        <button className="primary" disabled={busy||isPending(op,i=>!i.action||i.action==='list')} onClick={()=>onSubmit(op,{action:'list'})}>
          {isPending(op,i=>!i.action||i.action==='list') ? <><span className="spin">⟳</span> Checking instructions…</> : 'Instruction status →'}
        </button>
        <button className="secondary" disabled={busy||isPending(op,{action:'create_missing'})} onClick={()=>onSubmit(op,{action:'create_missing'})}>
          {isPending(op,{action:'create_missing'}) ? <><span className="spin">⟳</span> Creating missing files…</> : 'Create missing files'}
        </button>
        <button className="secondary" disabled={busy||isPending(op,{action:'repair'})} onClick={()=>onSubmit(op,{action:'repair'})}>
          {isPending(op,{action:'repair'}) ? <><span className="spin">⟳</span> Repairing links…</> : 'Repair missing links'}
        </button>
        <button className="secondary" disabled={busy||isPending(op,{action:'copy_role_prompts'})} onClick={()=>onSubmit(op,{action:'copy_role_prompts'})}>
          {isPending(op,{action:'copy_role_prompts'}) ? <><span className="spin">⟳</span> Copying role prompts…</> : 'Copy role prompts'}
        </button>
        <button className="secondary" disabled={busy||isPending(op,{action:'reset_role_prompts'})} onClick={()=>{if(window.confirm('Reset role prompts to package defaults?'))onSubmit(op,{action:'reset_role_prompts'})}}>
          {isPending(op,{action:'reset_role_prompts'}) ? <><span className="spin">⟳</span> Resetting prompts…</> : 'Reset role prompts'}
        </button>
      </div>
      <label className="field"><span>Instruction file</span>{files.length?<select value={name} onChange={e=>setName(e.target.value)}><option value="">Select an instruction file…</option>{files.map(x=><option key={x}>{x}</option>)}</select>:<input value={name} onChange={e=>setName(e.target.value)} placeholder="AGENTS.md"/>}</label>
      <Field label="Content (for edit)" value={content} onChange={setContent} textarea placeholder="File contents when writing/editing..."/>
      <div className="tool-action-buttons">
        <button className="secondary" disabled={busy||!name.trim()||isPending(op,{action:'read'})} onClick={()=>onSubmit(op,{action:'read',name:name.trim()})}>
          {isPending(op,{action:'read'}) ? <><span className="spin">⟳</span> Reading file…</> : 'Read file'}
        </button>
        <button className="primary" disabled={busy||!name.trim()||!content.trim()||isPending(op,{action:'write'})} onClick={()=>onSubmit(op,{action:'write',name:name.trim(),content})}>
          {isPending(op,{action:'write'}) ? <><span className="spin">⟳</span> Saving file…</> : 'Save instruction file →'}
        </button>
      </div>
    </>}
    {op==='tool.email'&&<>
      <p>Sender passwords and API keys are entered through the local CLI on the Mac. Recipient and nonsecret sender settings are available here.</p>
      <div className="tool-action-buttons">
        <button className="primary" disabled={busy||isPending(op,i=>!i.action||i.action==='list')} onClick={()=>onSubmit(op,{action:'list'})}>
          {isPending(op,i=>!i.action||i.action==='list') ? <><span className="spin">⟳</span> Checking email settings…</> : 'Email settings →'}
        </button>
        <button className="secondary" disabled={busy||isPending(op,{action:'test'})} onClick={()=>{if(window.confirm('Send test email to configured recipients?'))onSubmit(op,{action:'test'})}}>
          {isPending(op,{action:'test'}) ? <><span className="spin">⟳</span> Sending test email…</> : 'Send test email'}
        </button>
      </div>
      <Field label="Add recipient email" value={name} onChange={setName} placeholder="engineer@example.com"/>
      <button className="secondary" disabled={busy||!name.trim()||isPending(op,{action:'add'})} onClick={()=>{onSubmit(op,{action:'add',email:name.trim()});setName('')}}>
        {isPending(op,{action:'add'}) ? <><span className="spin">⟳</span> Adding recipient…</> : 'Add recipient'}
      </button>
      {recipients.length>0&&<><label className="field"><span>Remove recipient</span><select value={extra} onChange={e=>setExtra(e.target.value)}><option value="">Select recipient to remove…</option>{recipients.map(x=><option key={x}>{x}</option>)}</select></label><button className="secondary" disabled={busy||!extra||isPending(op,{action:'remove'})} onClick={()=>{onSubmit(op,{action:'remove',email:extra});setExtra('')}}>{isPending(op,{action:'remove'}) ? <><span className="spin">⟳</span> Removing recipient…</> : 'Remove recipient'}</button></>}
      <Field label="Import recipients (paste text or CSV)" value={content} onChange={setContent} textarea placeholder="user1@example.com, user2@example.com"/>
      <button className="secondary" disabled={busy||!content.trim()||isPending(op,{action:'import'})} onClick={()=>{onSubmit(op,{action:'import',content});setContent('')}}>
        {isPending(op,{action:'import'}) ? <><span className="spin">⟳</span> Importing recipients…</> : 'Import recipients'}
      </button>
      <details className="advanced"><summary>Configure notification sender</summary>
        <label className="field"><span>Provider</span><select value={value||'gmail'} onChange={e=>setValue(e.target.value)}><option value="gmail">Gmail</option><option value="resend">Resend</option></select></label>
        <Field label="Sender email" value={name} onChange={setName} placeholder="alerts@example.com"/>
        {value==='resend'&&<Field label="Display name" value={extra} onChange={setExtra} placeholder="Orchestrator"/>}
        <button className="secondary" disabled={busy||isPending(op,{action:'provider'})} onClick={()=>onSubmit(op,{action:'provider',provider:value||'gmail',sender_email:name||undefined,display_name:extra||undefined})}>
          {isPending(op,{action:'provider'}) ? <><span className="spin">⟳</span> Saving sender settings…</> : 'Save sender settings'}
        </button>
      </details>
    </>}
    {op==='tool.docs'&&<>
      <p>Browse documentation and architecture guides available in this repository.</p>
      <label className="field"><span>Document</span>{docs.length?<select value={name} onChange={e=>setName(e.target.value)}><option value="">Select a document…</option>{docs.map(x=><option key={x}>{x}</option>)}</select>:<input value={name} onChange={e=>setName(e.target.value)} placeholder="docs/architecture.md"/>}</label>
      <div className="tool-action-buttons">
        <button className="primary" disabled={busy||!name.trim()||isPending(op,i=>Boolean(i.name))} onClick={()=>onSubmit(op,{name:name.trim()})}>
          {isPending(op,i=>Boolean(i.name)) ? <><span className="spin">⟳</span> Opening document…</> : 'Read document →'}
        </button>
        <button className="secondary" disabled={busy||isPending(op,i=>!i.name)} onClick={()=>onSubmit(op,{})}>
          {isPending(op,i=>!i.name) ? <><span className="spin">⟳</span> Loading index…</> : 'Documentation index'}
        </button>
      </div>
    </>}
    {op==='tool.distribution'&&<>
      <p>Firebase login and signing credentials are set up locally on the Mac. Check readiness here, then run a real delivery smoke test.</p>
      <div className="tool-action-buttons">
        <button className="primary" disabled={busy||isPending(op,i=>!i.action||i.action==='status')} onClick={()=>onSubmit(op,{action:'status'})}>
          {isPending(op,i=>!i.action||i.action==='status') ? <><span className="spin">⟳</span> Checking delivery readiness…</> : 'Check delivery readiness →'}
        </button>
        <button className="secondary" disabled={busy||isPending(op,{action:'smoke_test'})} onClick={()=>{if(window.confirm('Run real build & delivery smoke test on Mac worker?'))onSubmit(op,{action:'smoke_test'})}}>
          {isPending(op,{action:'smoke_test'}) ? <><span className="spin">⟳</span> Running smoke test…</> : 'Run delivery smoke test'}
        </button>
      </div>
    </>}
    {op==='tool.self_tests'&&<>
      <p>Run diagnostic test suites on the Mac worker to verify system health and subsystem integrations.</p>
      <div className="tool-action-buttons">
        {[
          ['all','All Python tests →','primary'],
          ['workflow','Workflow','secondary'],
          ['tooling','Tooling','secondary'],
          ['probing','Machine probing','secondary'],
          ['resume','Resume & recovery','secondary'],
          ['console','Console UI','secondary'],
          ['models','Model registry','secondary'],
          ['fleet','Fleet health','secondary'],
          ['fleet_sync','Fleet sync','secondary'],
          ['model_ping','Model ping','secondary'],
          ['setup','Setup logic','secondary'],
          ['delivery','Build & delivery','secondary']
        ].map(([suite,label,cls])=>{
          const pending=isPending(op,i=>i.suite===suite);
          return <button key={suite} className={cls} disabled={busy||isPending(op)} onClick={()=>{
            if (suite==='delivery' && !window.confirm('Run delivery test?')) return;
            onSubmit(op,{suite});
          }}>
            {pending ? <><span className="spin">⟳</span> Running {suite}…</> : label}
          </button>;
        })}
      </div>
    </>}
    {op==='tool.update'&&<>
      <p>Check version and Git source status, or update the Orchestrator package on your Mac worker and remote fleet.</p>
      <div className="tool-action-buttons">
        <button className="primary" disabled={busy||isPending(op,i=>!i.action||i.action==='status')} onClick={()=>onSubmit(op,{action:'status'})}>
          {isPending(op,i=>!i.action||i.action==='status') ? <><span className="spin">⟳</span> Checking version & status…</> : 'Check version & status →'}
        </button>
        <button className="secondary" disabled={busy||isPending(op,{action:'local'})} onClick={()=>onSubmit(op,{action:'local'})}>
          {isPending(op,{action:'local'}) ? <><span className="spin">⟳</span> Updating Mac package…</> : 'Update Mac package'}
        </button>
        <button className="secondary" disabled={busy||isPending(op,{action:'fleet'})} onClick={()=>onSubmit(op,{action:'fleet'})}>
          {isPending(op,{action:'fleet'}) ? <><span className="spin">⟳</span> Updating remote workers…</> : 'Update remote workers'}
        </button>
      </div>
    </>}
    {latest&&(op==='tool.audit'?<AuditResult command={latest}/>:<ToolOutput command={latest} showModels={op!=='tool.models'||!models.length}/>)}
  </div>;
}
function AuditResult({command}:{command:Data}) {
  const result=command.result||{};
  const checks:Data[]=result.checklist||[];
  const machines:Data[]=result.fleet||[];
  const attention=checks.filter(check=>check.status==='needs_attention');
  const machineIssues=machines.flatMap(machine=>[
    ...(!machine.reachable?[{name:`${machine.machine||'A machine'} is offline`,next_step:'Check that the machine is on and its SSH connection works.'}]:[]),
    ...(machine.repo_path_ok===false?[{name:`Project folder missing on ${machine.machine||'a machine'}`,next_step:`The configured folder ${machine.repo_path||'in the fleet settings'} does not exist on this machine. Update its repo path in Manage Machine Fleet.`}]:[])
  ]);
  const issueCount=attention.length+machineIssues.length;
  return <div className="tool-result audit-result"><div className="eyebrow">PREREQUISITE AUDIT · {command.status}</div>
    {command.error&&<p className="error">{command.error}</p>}
    {['queued','running'].includes(command.status)?<><p>The Mac worker is checking this project and its machines. Results will appear here.</p><RunLog command={command}/></>:<>
      <h3>{!checks.length?'Audit details unavailable':issueCount?`${issueCount} item${issueCount===1?'':'s'} need attention`:'Core setup looks ready'}</h3>
      <p>{!checks.length?'The audit did not return a readable checklist. Review the technical output below.':issueCount?'Work through the items below, then run the audit again. Some optional tools may be absent without blocking your work.':'The audit found no required setup problems. Optional tools may still be unconfigured.'}</p>
      <h3>Setup checklist</h3>
      {checks.length?<div className="audit-checklist">{checks.map((check,i)=><div key={`${check.name}-${i}`} className={`audit-check ${check.status}`}><span className="audit-icon" aria-hidden="true">{check.status==='ready'?'✓':check.status==='needs_attention'?'!':'○'}</span><div><strong>{check.name}</strong><small>{check.status==='ready'?'Ready':check.status==='needs_attention'?'Needs attention':check.status==='skipped'?'Not needed for this project':'Optional'} · {check.detail}</small></div></div>)}</div>:<p>No checklist was returned. Open the technical output below for details.</p>}
      <h3>What needs attention</h3>
      {issueCount?<ul className="audit-missing">{[...attention,...machineIssues].map((check,i)=><li key={`${check.name}-${i}`}><strong>{check.name}</strong><span>{check.next_step}</span></li>)}</ul>:<p>Nothing required is missing.</p>}
      {machines.length>0&&<><h3>Machines</h3><div className="audit-machines">{machines.map((machine,i)=>{const available=Object.entries(machine.binaries||{}).filter(([,present])=>present).map(([name])=>name);const missing=Object.entries(machine.binaries||{}).filter(([,present])=>!present).map(([name])=>name);return <div key={`${machine.machine}-${i}`} className="audit-machine"><strong>{machine.machine||`Machine ${i+1}`} · {machine.reachable?'Online':'Offline'}</strong><small>Project folder: {machine.repo_path_ok===false?'Missing':machine.repo_path_ok===true?'Found':'Unknown'}</small><small>Available tools: {available.join(', ')||'None reported'}</small><small>Tools not found: {missing.join(', ')||'None reported'} (some are optional)</small></div>})}</div></>}
      <RunLog command={command}/>
    </>}
  </div>;
}
function AddProjectModal({
  onClose,
  onSubmit,
  busy,
  commands,
  onAdded
}: {
  onClose: () => void;
  onSubmit: (op: string, inputs?: Data, stay?: boolean) => void;
  busy: boolean;
  commands: Data[];
  onAdded: (id: string) => void;
}) {
  const [projectId, setProjectId] = useState('');
  const [path, setPath] = useState('');
  const [hasDiscovered, setHasDiscovered] = useState(false);

  const isDiscovering = commands.some(
    x => x.operation === 'tool.project' && ['queued', 'running'].includes(x.status) && x.inputs?.action === 'discover'
  );
  const isAdding = commands.some(
    x => x.operation === 'tool.project' && ['queued', 'running'].includes(x.status) && x.inputs?.action === 'add'
  );

  const latestDiscover = commands.find(
    x => x.operation === 'tool.project' && x.status === 'succeeded' && x.inputs?.action === 'discover'
  );
  const candidates: Data[] = latestDiscover?.result?.candidates || [];

  const latestAdd = commands.find(
    x => x.operation === 'tool.project' && x.status === 'succeeded' && x.inputs?.action === 'add'
  );

  useEffect(() => {
    if (!hasDiscovered && !latestDiscover && !isDiscovering) {
      setHasDiscovered(true);
      onSubmit('tool.project', { action: 'discover' }, true);
    }
  }, [hasDiscovered, latestDiscover, isDiscovering, onSubmit]);

  useEffect(() => {
    if (latestAdd?.result?.registered) {
      onAdded(latestAdd.result.registered);
    }
  }, [latestAdd?.id]);

  const handleSelectCandidate = (cand: Data) => {
    setProjectId(cand.id || '');
    setPath(cand.path || '');
  };

  const handleRegister = (e: React.FormEvent) => {
    e.preventDefault();
    if (!projectId.trim() || !path.trim()) return;
    onSubmit('tool.project', { action: 'add', project_id: projectId.trim(), path: path.trim() }, true);
  };

  return (
    <div className="modal-overlay" onClick={e => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="modal-card">
        <button type="button" className="modal-close" onClick={onClose} aria-label="Close">×</button>
        <h2>Add project to Orchestrator</h2>
        <p>Register a repository from your Mac worker to plan jobs, run unit tests, and manage workflows.</p>

        <form onSubmit={handleRegister} className="form-grid" style={{ gridTemplateColumns: '1fr', gap: 'var(--space-3)', marginTop: 'var(--space-4)' }}>
          <Field
            label="Project ID"
            value={projectId}
            onChange={setProjectId}
            placeholder="e.g. my-swift-app"
          />
          <Field
            label="Mac repository folder path"
            value={path}
            onChange={setPath}
            placeholder="e.g. /Users/neemaameskin/projects/my-swift-app"
          />

          {isDiscovering && (
            <div style={{ marginTop: 'var(--space-2)', padding: '10px 14px', background: 'var(--bg-surface-2)', border: '1px dashed var(--accent-border)', borderRadius: 'var(--radius-md)', display: 'flex', alignItems: 'center', gap: 'var(--space-3)' }}>
              <span className="spin" style={{ color: 'var(--accent-primary)', fontSize: '16px' }}>⟳</span>
              <div style={{ display: 'flex', flexDirection: 'column' }}>
                <strong style={{ fontSize: 'var(--text-xs)', color: 'var(--text-primary)' }}>Scanning Mac worker…</strong>
                <small style={{ color: 'var(--text-muted)', fontSize: 'var(--text-2xs)' }}>Discovering local Git & Swift projects</small>
              </div>
            </div>
          )}

          {!isDiscovering && candidates.length > 0 && (
            <div style={{ marginTop: 'var(--space-2)' }}>
              <span className="field-hint" style={{ fontWeight: 600, color: 'var(--text-secondary)' }}>
                Discovered on Mac worker (tap to autofill):
              </span>
              <div className="subsystem-chips" style={{ marginTop: '4px' }}>
                {candidates.map(cand => (
                  <button
                    type="button"
                    key={cand.path}
                    className={'subsystem-chip ' + (path === cand.path ? 'active' : '')}
                    onClick={() => handleSelectCandidate(cand)}
                    title={`${cand.name} (${cand.type}) - ${cand.path}`}
                  >
                    {cand.name} <small style={{ opacity: 0.7 }}>({cand.type})</small>
                  </button>
                ))}
              </div>
            </div>
          )}

          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginTop: 'var(--space-4)' }}>
            <ActionButton
              type="button"
              className="secondary"
              disabled={busy || isDiscovering}
              disabledReason={isDiscovering ? 'Scanning Mac for git repos…' : 'Worker is busy'}
              onClick={() => onSubmit('tool.project', { action: 'discover' }, true)}
            >
              {isDiscovering ? <><span className="spin">⟳</span> Scanning Mac…</> : 'Scan for projects'}
            </ActionButton>
            <ActionButton
              type="submit"
              className="primary"
              disabled={busy || isAdding || !projectId.trim() || !path.trim()}
              disabledReason={
                !projectId.trim()
                  ? 'Enter a project identifier'
                  : !path.trim()
                  ? 'Enter an absolute repository path'
                  : isAdding
                  ? 'Registering project…'
                  : 'Worker is busy'
              }
            >
              {isAdding ? <><span className="spin">⟳</span> Registering…</> : 'Register project →'}
            </ActionButton>
          </div>
        </form>
      </div>
    </div>
  );
}

function parseTimestamp(ts: any): Date | null {
  if (!ts) return null;
  if (typeof ts.toDate === 'function') {
    try { return ts.toDate(); } catch {}
  }
  if (typeof ts.seconds === 'number') {
    return new Date(ts.seconds * 1000 + (ts.nanoseconds || 0) / 1000000);
  }
  if (typeof ts === 'string' || typeof ts === 'number') {
    const d = new Date(ts);
    if (!isNaN(d.getTime())) return d;
  }
  return null;
}

function formatTimestamp(ts: any): { relative: string; full: string } {
  const date = parseTimestamp(ts);
  if (!date) return { relative: 'Recently', full: 'Unknown time' };
  const now = Date.now();
  const diffMs = Math.max(0, now - date.getTime());
  const diffSec = Math.floor(diffMs / 1000);
  const diffMin = Math.floor(diffSec / 60);
  const diffHours = Math.floor(diffMin / 60);
  const diffDays = Math.floor(diffHours / 24);

  let relative = '';
  if (diffSec < 15) {
    relative = 'Just now';
  } else if (diffSec < 60) {
    relative = `${diffSec}s ago`;
  } else if (diffMin < 60) {
    relative = `${diffMin}m ago`;
  } else if (diffHours < 24) {
    relative = `${diffHours}h ago`;
  } else if (diffDays === 1) {
    relative = 'Yesterday';
  } else if (diffDays < 7) {
    relative = `${diffDays}d ago`;
  } else {
    relative = date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
  }

  const full = date.toLocaleString(undefined, {
    weekday: 'short',
    month: 'short',
    day: 'numeric',
    year: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
    second: '2-digit'
  });

  return { relative, full };
}

function commandTitle(cmd: Data): string {
  const op = cmd.operation || 'command';
  const inp = cmd.inputs || {};
  if (op === 'job.create') {
    return inp.prompt ? `Create Job: ${inp.prompt.length > 50 ? inp.prompt.slice(0, 47) + '…' : inp.prompt}` : 'Create Job';
  }
  if (op === 'job.status') return 'Check Job Status';
  if (op === 'job.revise') return 'Revise Job Plan';
  if (op === 'job.iterate') return 'Iterate Implementation';
  if (op === 'job.rerun') return 'Rerun Job';
  if (op === 'job.cancel') return 'Cancel Job';
  if (op === 'job.patch') return 'Export Patch';
  if (op === 'job.archive') return 'Archive Job';
  if (op === 'job.ask_ai') return inp.question ? `Ask AI: ${inp.question.length > 40 ? inp.question.slice(0, 37) + '…' : inp.question}` : 'Ask AI';
  if (op === 'job.debug') return 'Debug Job Regression';
  if (op === 'job.link_logs') return 'Link Execution Logs';
  if (op === 'job.models') return 'Set Job Models';
  if (op === 'job.attach_reference') return 'Attach Reference File / Spec';
  if (op === 'job.reopen_bug') return 'Reopen Bug';
  if (op === 'tests.run') {
    if (inp.action === 'visual') return 'Simulator Visual Check';
    if (inp.action === 'list') return 'List Xcode Test Plans';
    return inp.plan ? `Run Xcode Test Plan (${inp.plan})` : 'Run Unit Tests';
  }
  if (op === 'project.refresh') return 'Refresh & Sync Workspace';
  if (op === 'tool.audit') return 'Prerequisite Setup Audit';
  if (op === 'tool.models') {
    if (inp.live_discovery) return 'Live Model Discovery';
    if (inp.refresh) return 'Sync Model Catalog';
    if (Array.isArray(inp.allowed_models)) return `Save Enabled Models (${inp.allowed_models.length})`;
    return 'Model Catalog Status';
  }
  if (op === 'tool.fleet') {
    if (inp.action === 'probe') return 'Probe Machine Fleet';
    if (inp.action === 'discover') return 'Discover Remote Machines';
    if (inp.action === 'purge') return 'Purge Stale Fleet Processes';
    if (inp.action === 'add') return `Add Fleet Machine (${inp.name || 'unnamed'})`;
    if (inp.action === 'select') return 'Update Active Fleet Machines';
    return 'Machine Fleet Status';
  }
  if (op === 'tool.project') {
    if (inp.action === 'discover') return 'Scan Mac for Projects';
    if (inp.action === 'add') return `Register Project (${inp.project_id || 'unnamed'})`;
    return 'Project Info';
  }
  if (op === 'tool.instructions') {
    if (inp.action === 'create_missing') return 'Create Missing Instruction Files';
    if (inp.action === 'repair') return 'Repair Instruction Links';
    if (inp.action === 'copy_role_prompts') return 'Copy Role Prompts';
    if (inp.action === 'reset_role_prompts') return 'Reset Role Prompts';
    if (inp.action === 'write') return `Save Instruction File (${inp.name || ''})`;
    if (inp.action === 'read') return `Read Instruction File (${inp.name || ''})`;
    return 'AI Instructions Status';
  }
  if (op === 'tool.email') {
    if (inp.action === 'test') return 'Send Test Email Notification';
    if (inp.action === 'add') return `Add Notification Email (${inp.email || ''})`;
    if (inp.action === 'remove') return `Remove Notification Email (${inp.email || ''})`;
    if (inp.action === 'import') return 'Import Notification Emails';
    if (inp.action === 'provider') return `Configure Notification Sender (${inp.provider || 'gmail'})`;
    return 'Email Notification Status';
  }
  if (op === 'tool.distribution') {
    if (inp.action === 'smoke_test') return 'Delivery Build & Smoke Test';
    return 'Delivery Readiness Check';
  }
  if (op === 'tool.self_tests') return `Self Diagnostic Tests (${inp.suite || 'all'})`;
  if (op === 'tool.update') {
    if (inp.action === 'local') return 'Update Mac Orchestrator Package';
    if (inp.action === 'fleet') return 'Update Remote Worker Packages';
    return 'Version & Package Status';
  }
  if (op === 'tool.keys') {
    if (inp.action === 'clear') return 'Clear Fallback API Keys';
    return 'API Keys & Sign-In Status';
  }
  if (op === 'tool.base_branch') {
    if (inp.branch) return `Set Base Branch (${inp.branch})`;
    return 'Base Branch Status';
  }
  if (op === 'tool.docs') return inp.name ? `Read Document (${inp.name})` : 'Documentation Index';
  if (op === 'tool.archive') return inp.action === 'restore' ? `Restore Job (${inp.job_id || ''})` : 'Archived Jobs List';

  return op.replace(/^[a-z]+\./, '').replaceAll('_', ' ').replace(/\b\w/g, (l: string) => l.toUpperCase());
}

function ActivityCard({
  cmd,
  onNavigateJob,
  onCancel
}: {
  cmd: Data;
  onNavigateJob: (jobId: string) => void;
  onCancel: (cmdId: string) => void;
}) {
  const isQueued = cmd.status === 'queued';
  const isRunning = cmd.status === 'running';
  const { relative, full } = formatTimestamp(cmd.created_at);
  const titleText = commandTitle(cmd);

  return (
    <div className={`activity-card ${cmd.status || ''}`} key={cmd.id}>
      <div className="activity-card-header">
        <div className="activity-card-title-group">
          <div className="activity-card-title">
            <strong>{titleText}</strong>
            <span className="activity-op-badge">{cmd.operation}</span>
          </div>
          <div className="activity-meta-tags">
            {cmd.job_id && (
              <button
                type="button"
                className="activity-tag job-tag"
                onClick={() => onNavigateJob(cmd.job_id)}
                title={`Jump to Job ${cmd.job_id}`}
              >
                ◈ {cmd.job_id} ↗
              </button>
            )}
            <span className="activity-tag project-tag" title="Project">
              📁 {cmd.project_id || 'default'}
            </span>
            <span className="activity-tag time-tag" title={full}>
              ⏱ {relative}
            </span>
            <span className="activity-tag id-tag" title={`Command ID: ${cmd.id}`}>
              #{cmd.id?.slice(0, 8)}
            </span>
          </div>
        </div>

        <div className="activity-card-actions">
          <span className={`badge ${cmd.status}`}>
            {(isQueued || isRunning) && <span className="spin" style={{ marginRight: '4px' }}>⟳</span>}
            {cmd.status}
          </span>
          {isQueued && (
            <button
              type="button"
              className="secondary cancel-cmd-btn"
              onClick={() => onCancel(cmd.id)}
              title="Cancel queued command"
            >
              Cancel
            </button>
          )}
        </div>
      </div>

      {cmd.inputs?.prompt && (
        <div className="activity-prompt-box">
          <span className="prompt-label">Prompt:</span>
          <p>{cmd.inputs.prompt}</p>
        </div>
      )}

      {cmd.inputs?.feedback && (
        <div className="activity-prompt-box">
          <span className="prompt-label">Feedback:</span>
          <p>{cmd.inputs.feedback}</p>
        </div>
      )}

      {cmd.inputs?.question && (
        <div className="activity-prompt-box">
          <span className="prompt-label">Question:</span>
          <p>{cmd.inputs.question}</p>
        </div>
      )}

      {cmd.error && <p className="error" style={{ margin: 'var(--space-2) 0' }}>{cmd.error}</p>}

      {cmd.operation?.startsWith('tool.') ? (
        cmd.operation === 'tool.audit' ? (
          <AuditResult command={cmd} />
        ) : (
          <ToolOutput command={cmd} />
        )
      ) : (
        <>
          <RunLog command={cmd} />
          {cmd.result && Object.keys(cmd.result).length > 0 && (
            <details className="run-log" style={{ marginTop: 'var(--space-2)' }}>
              <summary>Command result payload</summary>
              <pre>{JSON.stringify(cmd.result, null, 2)}</pre>
            </details>
          )}
        </>
      )}
    </div>
  );
}

function ActivityFeed({
  commands,
  jobs,
  onNavigateJob,
  onCancelCommand
}: {
  commands: Data[];
  jobs: Data[];
  onNavigateJob: (jobId: string) => void;
  onCancelCommand: (cmdId: string) => void;
}) {
  const [search, setSearch] = useState('');
  const [category, setCategory] = useState<'all' | 'jobs' | 'tests' | 'tools' | 'sync'>('all');
  const [statusFilter, setStatusFilter] = useState<'all' | 'active' | 'succeeded' | 'failed'>('all');
  const [selectedJob, setSelectedJob] = useState<string>('all');
  const [groupByJob, setGroupByJob] = useState(true);

  const distinctJobIds = useMemo(() => {
    const set = new Set<string>();
    commands.forEach(c => { if (c.job_id) set.add(c.job_id); });
    jobs.forEach(j => { if (j.job_id) set.add(j.job_id); });
    return Array.from(set);
  }, [commands, jobs]);

  const filteredCommands = useMemo(() => {
    return commands.filter(cmd => {
      if (search.trim()) {
        const q = search.toLowerCase();
        const text = [
          cmd.operation,
          cmd.job_id,
          cmd.id,
          cmd.inputs?.prompt,
          cmd.inputs?.plan,
          cmd.inputs?.action,
          cmd.inputs?.question,
          cmd.inputs?.feedback,
          cmd.inputs?.email,
          cmd.inputs?.name,
          cmd.error,
          cmd.output
        ].filter(Boolean).join(' ').toLowerCase();
        if (!text.includes(q)) return false;
      }

      if (category === 'jobs') {
        if (!cmd.operation?.startsWith('job.') && !cmd.job_id) return false;
      } else if (category === 'tests') {
        if (cmd.operation !== 'tests.run') return false;
      } else if (category === 'tools') {
        if (!cmd.operation?.startsWith('tool.')) return false;
      } else if (category === 'sync') {
        if (cmd.operation !== 'project.refresh') return false;
      }

      if (statusFilter === 'active') {
        if (!['queued', 'running'].includes(cmd.status)) return false;
      } else if (statusFilter === 'succeeded') {
        if (cmd.status !== 'succeeded') return false;
      } else if (statusFilter === 'failed') {
        if (!['failed', 'cancelled'].includes(cmd.status)) return false;
      }

      if (selectedJob !== 'all') {
        if (selectedJob === '__none__') {
          if (cmd.job_id) return false;
        } else if (cmd.job_id !== selectedJob) {
          return false;
        }
      }

      return true;
    });
  }, [commands, search, category, statusFilter, selectedJob]);

  const grouped = useMemo(() => {
    const map = new Map<string, Data[]>();
    for (const cmd of filteredCommands) {
      const key = cmd.job_id || '__workspace__';
      if (!map.has(key)) map.set(key, []);
      map.get(key)!.push(cmd);
    }
    return Array.from(map.entries()).map(([key, list]) => {
      const jobInfo = jobs.find(j => j.job_id === key);
      return {
        key,
        jobId: key === '__workspace__' ? null : key,
        jobTitle: jobInfo?.title || null,
        jobStatus: jobInfo?.status || null,
        commands: list
      };
    });
  }, [filteredCommands, jobs]);

  return (
    <div className="page">
      <div className="page-heading">
        <div>
          <h1>Activity</h1>
          <p>Live stream of commands, tool executions, and job runs dispatched to the Mac worker.</p>
        </div>
      </div>

      <div className="activity-controls">
        <div className="activity-controls-row">
          <input
            type="search"
            className="activity-search-input"
            value={search}
            onChange={e => setSearch(e.target.value)}
            placeholder="Search commands, jobs, prompts, outputs…"
            aria-label="Search activity"
          />
          <div className="activity-view-toggle">
            <button
              type="button"
              className={`activity-toggle-btn ${groupByJob ? 'active' : ''}`}
              onClick={() => setGroupByJob(true)}
            >
              Group by Job Run
            </button>
            <button
              type="button"
              className={`activity-toggle-btn ${!groupByJob ? 'active' : ''}`}
              onClick={() => setGroupByJob(false)}
            >
              Timeline
            </button>
          </div>
        </div>

        <div className="activity-controls-row">
          <div className="activity-filters-group">
            <select
              value={category}
              onChange={e => setCategory(e.target.value as any)}
              aria-label="Filter by type"
            >
              <option value="all">All types</option>
              <option value="jobs">Jobs & Actions</option>
              <option value="tests">Unit Tests</option>
              <option value="tools">Configuration & Tools</option>
              <option value="sync">Workspace Sync</option>
            </select>

            <select
              value={statusFilter}
              onChange={e => setStatusFilter(e.target.value as any)}
              aria-label="Filter by status"
            >
              <option value="all">All statuses</option>
              <option value="active">Active (Queued / Running)</option>
              <option value="succeeded">Succeeded</option>
              <option value="failed">Failed / Cancelled</option>
            </select>

            <select
              value={selectedJob}
              onChange={e => setSelectedJob(e.target.value)}
              aria-label="Filter by job run"
            >
              <option value="all">All job runs</option>
              <option value="__none__">Workspace only (no job)</option>
              {distinctJobIds.map(id => (
                <option key={id} value={id}>
                  Job: {id}
                </option>
              ))}
            </select>
          </div>

          <span style={{ color: 'var(--text-muted)', fontSize: 'var(--text-xs)' }}>
            Showing <strong>{filteredCommands.length}</strong> of {commands.length} command{commands.length === 1 ? '' : 's'}
          </span>
        </div>
      </div>

      <div className="panel activity">
        {filteredCommands.length === 0 ? (
          <div className="empty" style={{ background: 'var(--bg-surface-1)', borderRadius: 'var(--radius-xl)', border: '1px solid var(--border-default)' }}>
            <div>◷</div>
            <h3>No matching activity</h3>
            <p>
              {commands.length === 0
                ? 'Commands you dispatch to your Mac worker will appear in real time here.'
                : 'No commands matched your current filters. Try resetting the filters or search box.'}
            </p>
            {(search || category !== 'all' || statusFilter !== 'all' || selectedJob !== 'all') && (
              <button
                type="button"
                className="secondary"
                onClick={() => { setSearch(''); setCategory('all'); setStatusFilter('all'); setSelectedJob('all'); }}
              >
                Clear all filters
              </button>
            )}
          </div>
        ) : groupByJob ? (
          <div className="activity-feed-list">
            {grouped.map(group => (
              <section className="activity-group" key={group.key}>
                <div className="activity-group-head">
                  <div className="activity-group-title">
                    <span style={{ fontSize: '18px' }}>{group.jobId ? '◈' : '⚙'}</span>
                    <div>
                      <h3>
                        {group.jobId ? `Job: ${group.jobId}` : 'Workspace & System Operations'}
                        {group.jobTitle && <span style={{ color: 'var(--text-muted)', fontWeight: 400, marginLeft: '6px' }}>— {group.jobTitle}</span>}
                      </h3>
                    </div>
                    {group.jobStatus && <span className={`badge ${group.jobStatus}`}>{group.jobStatus}</span>}
                    <span className="activity-group-badge">
                      {group.commands.length} command{group.commands.length === 1 ? '' : 's'}
                    </span>
                  </div>
                  {group.jobId && (
                    <button
                      type="button"
                      className="activity-group-link"
                      onClick={() => onNavigateJob(group.jobId!)}
                    >
                      View Job Detail →
                    </button>
                  )}
                </div>

                <div className="activity-feed-list">
                  {group.commands.map(cmd => (
                    <ActivityCard
                      key={cmd.id}
                      cmd={cmd}
                      onNavigateJob={onNavigateJob}
                      onCancel={onCancelCommand}
                    />
                  ))}
                </div>
              </section>
            ))}
          </div>
        ) : (
          <div className="activity-feed-list">
            {filteredCommands.map(cmd => (
              <ActivityCard
                key={cmd.id}
                cmd={cmd}
                onNavigateJob={onNavigateJob}
                onCancel={onCancelCommand}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

createRoot(document.getElementById('root')!).render(<React.StrictMode><App/></React.StrictMode>);
