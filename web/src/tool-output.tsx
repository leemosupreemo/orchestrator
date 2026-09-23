import menu from './menu.json';

type Data = Record<string, any>;
const title = (value:string) => value.replaceAll('_',' ').replace(/\b\w/g,letter=>letter.toUpperCase());
const plain = (value:string) => value.replace(/\x1b\[[0-9;?]*[A-Za-z]/g,'').replace(/\r/g,'\n').trim();
const statusText:Record<string,string> = {queued:'Waiting for Mac worker',running:'Running on Mac',succeeded:'Completed',failed:'Failed',cancelled:'Cancelled'};
const hidden = new Set(['output','exit_code','checklist','fleet','models']);
const providerName:Record<string,string> = {openai:'OpenAI',claude:'Claude',gemini:'Gemini'};

export function ModelList({models,selected=[],onToggle}:{models:Data[];selected?:string[];onToggle?:(id:string)=>void}) {
  if(!models.length)return <p>No models are available in the catalog.</p>;
  return <div className="model-list" aria-label="Available LLM models">{models.map(model=>{
    const checked=selected.includes(model.id);
    const content=<><span className="model-card-heading"><strong>{model.id}</strong>{checked&&<span className="model-selected">Selected</span>}</span>
      <span className="model-card-meta">{providerName[model.family]||title(String(model.family||'Unknown provider'))} · {title(String(model.tier||'Unrated').toLowerCase())} tier</span>
      {Array.isArray(model.capabilities)&&model.capabilities.length>0&&<span className="model-card-tags">{model.capabilities.map((capability:string)=><span key={capability}>{title(capability)}</span>)}</span>}
      {Array.isArray(model.required_clis)&&model.required_clis.length>0&&<small>Requires {model.required_clis.join(', ')} CLI</small>}</>;
    return onToggle?<label className={`model-card selectable${checked?' selected':''}`} key={model.id}><input type="checkbox" checked={checked} onChange={()=>onToggle(model.id)}/><span>{content}</span></label>:<div className="model-card" key={model.id}>{content}</div>;
  })}</div>;
}

export function RunLog({command}:{command:Data}) {
  const running=command.status==='running'||command.status==='queued';
  const output=plain(String(running?command.output||'':command.result?.output||command.output||''));
  const lines=output?output.split('\n').length:0;
  return <details className="run-log" open={command.status==='running'||command.status==='failed'}>
    <summary>{running?'Live log':'Technical log'}{lines?` · ${lines} ${lines===1?'line':'lines'}`:''}</summary>
    <pre role="log" aria-live={running?'polite':'off'}>{output|| (running?'Waiting for output from the Mac worker…':'No log output was produced.')}</pre>
  </details>;
}

function summary(command:Data):string {
  const result=command.result||{};
  if(command.status==='queued')return 'Waiting for the Mac worker to start this action.';
  if(command.status==='running')return 'The Mac worker is running this action. The log updates as output arrives.';
  if(command.status==='cancelled')return 'This action was cancelled before it ran.';
  if(result.error||command.error)return String(result.error||command.error);
  if(command.status==='failed'&&result.sync?.success===false)return result.sync.message||'Model discovery failed.';
  if(command.status==='failed')return result.exit_code!==undefined?`The command exited with code ${result.exit_code}. Open the log for the error.`:'The action did not complete.';
  if(command.operation==='tool.self_tests') {
    const tests=String(result.output||'').match(/Ran (\d+) tests? in/);
    return tests?`${tests[1]} tests completed successfully.`:'The selected test run completed.';
  }
  if(command.operation==='tool.distribution'&&command.inputs?.action==='smoke_test')return 'The build and delivery smoke test completed.';
  if(command.operation==='tool.email'&&command.inputs?.action==='test')return 'The test notification command completed.';
  if(command.operation==='tool.update'&&command.inputs?.action!=='status')return 'The update command completed. Review the log for package changes.';
  if(command.operation==='tool.fleet'&&command.inputs?.action==='purge')return `${result.purged||0} stale process or cache item${result.purged===1?'':'s'} cleared.`;
  if(command.operation==='tool.fleet'&&command.inputs?.action==='probe')return `${result.probes?.length||0} machine${result.probes?.length===1?'':'s'} checked.`;
  if(command.operation==='tool.fleet'&&command.inputs?.action==='discover')return `${result.candidates?.suitable?.length||0} suitable remote machine${result.candidates?.suitable?.length===1?'':'s'} found.`;
  if(command.operation==='tool.models'&&result.sync)return result.sync.message|| (result.sync.success?'Model catalog refreshed.':'Model discovery did not complete.');
  if(command.operation==='tool.keys')return 'Provider sign-in and fallback key status checked on the Mac.';
  if(command.operation==='tool.fleet'&&Array.isArray(result.machines))return `${result.machines.length} machine${result.machines.length===1?'':'s'} configured.`;
  if(command.operation==='tool.email'&&Array.isArray(result.notification_emails))return `${result.notification_emails.length} notification recipient${result.notification_emails.length===1?'':'s'} configured.`;
  if(command.operation==='tool.instructions'&&result.files)return 'Instruction files checked. Items needing attention are marked below.';
  if(command.operation==='tool.distribution'&&command.inputs?.action!=='smoke_test')return 'Delivery setup checked. Review the readiness items below.';
  if(command.operation==='tool.base_branch')return `Default branch: ${result.branch||'not set'}.`;
  if(command.operation==='tool.project')return `Current project: ${result.current||'not set'}.`;
  if(command.operation==='tool.archive')return result.restored?`Restored ${result.restored}.`:`${result.jobs?.length||0} archived job${result.jobs?.length===1?'':'s'} found.`;
  if(command.operation==='tool.docs')return result.name?`Opened ${result.name}.`:`${result.documents?.length||0} document${result.documents?.length===1?'':'s'} available.`;
  if(command.operation==='tool.update'&&command.inputs?.action==='status')return `Mac package version ${result.version||'unknown'}.`;
  return 'The action completed. Details are below.';
}

function valueText(value:any):string {
  if(value===true)return 'Yes';
  if(value===false)return 'No';
  if(value===null||value===undefined||value==='')return 'Not set';
  if(Array.isArray(value))return value.length?value.map(valueText).join(', '):'None';
  return String(value);
}

function Detail({label,value}:{label:string;value:any}) {
  if(Array.isArray(value)&&value.every(item=>item===null||typeof item!=='object')) {
    return <div className="run-detail wide"><span>{title(label)} · {value.length}</span>{value.length?<ul className="run-value-list">{value.map((item,i)=><li key={i}>{valueText(item)}</li>)}</ul>:<strong>None</strong>}</div>;
  }
  if(Array.isArray(value)&&value.length>0&&typeof value[0]==='object') {
    return <div className="run-detail wide"><span>{title(label)} · {value.length}</span><div className="run-object-list">{value.slice(0,20).map((item:Data,i:number)=>{const bins=Object.entries(item.binaries||{});const missing=bins.filter(([,present])=>!present).map(([name])=>name);return <div key={i}><strong>{item.name||item.machine||item.hostname||item.id||`Item ${i+1}`}</strong><small>{Object.entries(item).filter(([key])=>!['name','machine','hostname','id','binaries','mcp_plugins','hw','stale_processes'].includes(key)).slice(0,5).map(([key,entry])=>`${title(key)}: ${valueText(entry)}`).join(' · ')}</small>{bins.length>0&&<small>Tools not found: {missing.join(', ')||'None'}</small>}</div>})}</div></div>;
  }
  if(value&&typeof value==='object'&&!Array.isArray(value)) {
    return <div className="run-detail wide"><span>{title(label)}</span><div className="run-facts">{Object.entries(value).map(([key,entry])=><Detail key={key} label={key} value={entry}/>)}</div></div>;
  }
  return <div className="run-detail"><span>{title(label)}</span><strong>{valueText(value)}</strong></div>;
}

function CheckList({items}:{items:{label:string;ok:boolean;detail?:string;optional?:boolean}[]}) {
  return <div className="result-checks">{items.map(item=><div className={`result-check ${item.ok?'ready':item.optional?'optional':'missing'}`} key={item.label}><span aria-hidden="true">{item.ok?'✓':item.optional?'·':'!'}</span><div><strong>{item.label}</strong>{item.detail&&<small>{item.detail}</small>}</div></div>)}</div>;
}

function ResultSections({command,result}:{command:Data;result:Data}) {
  if(command.operation==='tool.keys') {
    const cli=result.cli_status||{};
    const keys=result.configured||{};
    return <><h4>CLI sign-in</h4><CheckList items={Object.entries(cli).map(([name,state])=>{const signIn=result.sign_in_commands?.[name==='gh'?'github':name];return {label:providerName[name]||title(name),ok:state==='Ready',detail:String(state)+(state==='Ready'||!signIn?'':` · On the Mac: ${signIn}`)};})}/><h4>Fallback API keys</h4><CheckList items={Object.entries(keys).map(([name,configured])=>({label:providerName[name]||title(name),ok:Boolean(configured),optional:true,detail:configured?'Configured on Mac':'Not configured (optional if CLI sign-in works)'}))}/>{result.setup&&<p>{result.setup}</p>}</>;
  }
  if(command.operation==='tool.fleet') {
    const machines:Data[]=result.machines||[];
    const probes:Data[]=result.probes||[];
    const candidates:Data=result.candidates||{};
    return <>{machines.length>0&&<><h4>Configured machines</h4><div className="result-cards">{machines.map(machine=><div className="result-card" key={machine.name}><strong>{machine.name}</strong><span>{machine.execution_mode==='ssh'?'Remote':'Local'} · {machine.enabled===false?'Disabled':'Enabled'}{result.selected?.includes(machine.name)?' · Selected':''}</span>{machine.ssh_target&&<small>SSH: {valueText(machine.ssh_target)}</small>}{machine.repo_path&&<small>Project path: {machine.repo_path}</small>}</div>)}</div></>}{probes.length>0&&<><h4>Machine checks</h4><div className="result-cards">{probes.map((probe,i)=>{const missing=Object.entries(probe.binaries||{}).filter(([,ready])=>!ready).map(([name])=>name);const path=probe.repo_path_ok===true?'found':probe.repo_path_ok===false?'missing':'unknown';return <div className="result-card" key={probe.machine||i}><strong>{probe.machine||`Machine ${i+1}`}</strong><span>{probe.reachable?'Online':'Offline'} · Project path {path}</span><small>Tools not found: {missing.join(', ')||'None'}</small>{probe.error&&<small>{probe.error}</small>}</div>})}</div></>}{Object.keys(candidates).length>0&&<><h4>Discovery</h4>{Object.entries(candidates).map(([group,items])=><div key={group}><p>{title(group)} · {Array.isArray(items)?items.length:0}</p>{Array.isArray(items)&&items.length>0&&<div className="result-cards">{items.map((item:Data,i:number)=><div className="result-card" key={item.host||i}><strong>{item.hostname||item.host||`Machine ${i+1}`}</strong><span>{item.status||title(group)}</span>{item.error&&<small>{item.error}</small>}</div>)}</div>}</div>)}</>}</>;
  }
  if(command.operation==='tool.instructions') {
    const files=result.files||{};
    return <>{Object.keys(files).length>0&&<><h4>Instruction files</h4><CheckList items={Object.entries(files).map(([name,entry])=>{const state=typeof entry==='object'?(entry as Data).status:entry;return {label:typeof entry==='object'?(entry as Data).path||name:name,ok:state==='valid',detail:state==='missing'?'File is missing. Use Create missing files.':state==='misconfigured'?'Required links are missing. Use Repair missing links.':'Ready'};})}/></>}{Array.isArray(result.role_prompts)&&<><h4>Role prompts · {result.role_prompts.length}</h4>{result.role_prompts.length?<ul className="run-value-list">{result.role_prompts.map((name:string)=><li key={name}>{name}</li>)}</ul>:<p>No role prompts copied.</p>}</>}{typeof result.content==='string'&&<><h4>{result.name||'Instruction content'}</h4><pre className="result-content">{result.content}</pre></>}</>;
  }
  if(command.operation==='tool.email'&&result.notification_emails) {
    return <><h4>Sender</h4><CheckList items={[{label:result.notification_provider==='resend'?'Resend':'Gmail',ok:Boolean(result.sender_ready),detail:result.sender_ready?'Sender credentials ready':'Sender credentials need setup on the Mac'}]}/><p>{result.sender_email||'No sender address set'}{result.display_name?` · ${result.display_name}`:''}</p><h4>Recipients · {result.notification_emails.length}</h4>{result.notification_emails.length?<ul className="run-value-list">{result.notification_emails.map((email:string)=><li key={email}>{email}</li>)}</ul>:<p>No recipients added.</p>}</>;
  }
  if(command.operation==='tool.docs')return <>{typeof result.content==='string'&&<><h4>{result.name}</h4><pre className="result-content">{result.content}</pre></>}</>;
  if(command.operation==='tool.distribution'&&command.inputs?.action!=='smoke_test') {
    return <><h4>Delivery readiness</h4><CheckList items={[{label:'Distribution configured',ok:Boolean(result.configured)},{label:'Firebase CLI installed',ok:Boolean(result.firebase_cli)},{label:'Firebase signed in',ok:Boolean(result.firebase_logged_in)},{label:'Google service file',ok:Boolean(result.google_service_info)},{label:'Export options',ok:Boolean(result.export_options)},{label:'Headless signing',ok:Boolean(result.headless_signing),optional:true,detail:'Needed for unattended signing; manual signing can use another setup.'}]}/>{result.setup&&<p>{result.setup}</p>}</>;
  }
  return null;
}

const specializedKeys:Record<string,string[]>={
  'tool.keys':['cli_status','configured','cli_available','sign_in_commands','setup'],
  'tool.fleet':['machines','selected','probes','candidates'],
  'tool.instructions':['files','role_prompts','content'],
  'tool.email':['notification_emails','notification_provider','sender_email','display_name','sender_ready'],
  'tool.docs':['content'],
  'tool.distribution':['configured','firebase_cli','firebase_logged_in','google_service_info','export_options','headless_signing','setup'],
};

export function ToolOutput({command,showModels=true}:{command:Data;showModels?:boolean}) {
  const result=command.result||{};
  const label=menu.tools.find(item=>item.operation===command.operation)?.label||title(command.operation||'Tool');
  const action=command.inputs?.action||command.inputs?.suite||(command.inputs?.live_discovery?'live discovery':command.inputs?.refresh?'refresh':undefined);
  const customKeys=specializedKeys[command.operation]||[];
  const details=Object.entries(result).filter(([key])=>!hidden.has(key)&&!customKeys.includes(key)&&!(command.operation==='tool.models'&&key==='selected'));
  const hasLog=Boolean(command.output||result.output||['queued','running'].includes(command.status));
  return <div className="tool-result organized-result">
    <div className="run-heading"><div><div className="eyebrow">{label}{action?` · ${title(String(action))}`:''}</div><h3>{statusText[command.status]||title(command.status||'Status')}</h3></div><span className={`badge ${command.status}`}>{command.status}</span></div>
    <p>{summary(command)}</p>
    {command.operation==='tool.models'&&Array.isArray(result.models)&&<><p>{result.models.length} model{result.models.length===1?'':'s'} in the catalog.</p>{showModels&&<ModelList models={result.models} selected={result.selected||[]}/>}</>}
    {command.operation==='tool.fleet'&&Array.isArray(result.fleet)&&<p>{result.fleet.length} machines in the fleet.</p>}
    <ResultSections command={command} result={result}/>
    {details.length>0&&<div className="run-facts">{details.map(([key,value])=><Detail key={key} label={key} value={value}/>)}</div>}
    {hasLog&&<RunLog command={command}/>}
    {Object.keys(result).length>0&&<details className="run-log"><summary>Full result data</summary><pre>{JSON.stringify(result,null,2)}</pre></details>}
  </div>;
}
