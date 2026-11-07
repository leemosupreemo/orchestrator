import menu from './menu.json';

type Data = Record<string, any>;
const title = (value:string) => value.replaceAll('_',' ').replace(/\b\w/g,letter=>letter.toUpperCase());
const plain = (value:string) => value.replace(/\x1b\[[0-9;?]*[A-Za-z]/g,'').replace(/\r/g,'\n').trim();
const statusText:Record<string,string> = {queued:'Waiting for Mac worker',running:'Running on Mac',succeeded:'Completed',failed:'Failed',cancelled:'Cancelled'};
const hidden = new Set(['output','exit_code','checklist','fleet','models']);
const providerName:Record<string,string> = {openai:'OpenAI',claude:'Anthropic (Claude)',gemini:'Google (Gemini)',opencode:'OpenCode',qwen:'Qwen',deepseek:'DeepSeek',copilot:'GitHub Copilot'};

const yoloRisk:Record<string,string>={codex:'Also turns off the Codex sandbox, so commands can reach the network and files outside the project.'};

export function ServiceSummary({services,models,selected,yolo={},onYolo,disabled=false}:{services:Data[];models:Data[];selected:string[];yolo?:Record<string,boolean>;onYolo?:(cli:string,on:boolean)=>void;disabled?:boolean}) {
  if(!services.length)return null;
  return <div className="service-summary" aria-label="Model services">{services.map(service=>{
    const enabled=models.filter(model=>model.family===service.family&&selected.includes(model.id)).length;
    const known=typeof service.available==='number';
    return <div className={`service-card${service.connected?' connected':''}`} key={service.family}>
      <div className="service-card-heading"><strong>{providerName[service.family]||title(String(service.family))}</strong><span className={`service-status ${service.connected?'ready':'missing'}`}>{service.connected?'Connected':'Not connected'}</span></div>
      <dl><div><dt>In catalog</dt><dd>{service.catalog}</dd></div><div><dt>Enabled</dt><dd>{enabled}</dd></div><div><dt>Available to you</dt><dd>{known?service.available:'—'}</dd></div></dl>
      {!known&&<small>Run Sync or live discovery to check which models your account can use.</small>}
      {service.yolo_cli&&<label className="yolo-toggle">
        <input type="checkbox" role="switch" aria-label={`YOLO mode for ${providerName[service.family]||title(String(service.family))}`} checked={Boolean(yolo[service.yolo_cli])} disabled={disabled||!onYolo}
          onChange={e=>{const on=e.target.checked;if(on&&!window.confirm(`Turn on YOLO mode for ${providerName[service.family]||title(String(service.family))}? Jobs will edit files and run commands on the Mac without asking. ${yoloRisk[service.yolo_cli]||''}`.trim()))return;onYolo?.(service.yolo_cli,on);}}/>
        <span><strong>YOLO mode {yolo[service.yolo_cli]?'on':'off'}</strong><small>{yolo[service.yolo_cli]?'Edits files and runs commands without asking.':'Asks first, so jobs are refused anything that needs permission.'}</small></span>
      </label>}
    </div>})}</div>;
}

export function ModelList({models,selected=[],highlight=[],onToggle,disabled=false}:{models:Data[];selected?:string[];highlight?:string[];onToggle?:(id:string)=>void;disabled?:boolean}) {
  if(!models.length)return <p>No models are available in the catalog.</p>;
  const groups=Object.entries(models.reduce((grouped:Record<string,Data[]>,model)=>{const family=String(model.family||'other');(grouped[family]??=[]).push(model);return grouped},{}));
  return <div className="model-groups" aria-label="Available LLM models">{groups.map(([family,items])=><section className="model-group" key={family}><div className="model-group-heading"><h4>{providerName[family]||title(family)}</h4><span>{items.length} model{items.length===1?'':'s'}</span></div><div className="model-list">{items.map(model=>{
    const checked=selected.includes(model.id);const isNew=highlight.includes(model.id);
    const content=<><span className="model-card-heading"><strong>{model.id}{isNew&&<span className="model-new-badge">New</span>}</strong><span className={`model-selected ${checked?'enabled':'disabled'}`}>{checked?'Enabled':'Disabled'}</span></span>
      <span className="model-card-meta">{providerName[model.family]||title(String(model.family||'Unknown provider'))} · {title(String(model.tier||'Unrated').toLowerCase())} tier</span>
      {Array.isArray(model.capabilities)&&model.capabilities.length>0&&<span className="model-card-tags">{model.capabilities.map((capability:string)=><span key={capability}>{title(capability)}</span>)}</span>}
      {Array.isArray(model.required_clis)&&model.required_clis.length>0&&<small>Requires {model.required_clis.join(', ')} CLI</small>}</>;
    return onToggle?<label className={`model-card selectable${checked?' selected':''}${isNew?' is-new':''}`} key={model.id}><input type="checkbox" aria-label={`Enable ${model.id}`} checked={checked} disabled={disabled||(checked&&selected.length===1)} onChange={()=>onToggle(model.id)}/><span>{content}</span></label>:<div className={`model-card${isNew?' is-new':''}`} key={model.id}>{content}</div>;
  })}</div></section>)}</div>;
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
  if(command.operation==='tool.models'&&Array.isArray(result.selected))return `${result.selected.length} model${result.selected.length===1?'':'s'} enabled for new jobs.`;
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
    const services:{name:string;cli:string;cliLabel?:string;key?:string;signIn?:string}[]=[
      {name:'claude',cli:'claude',key:'anthropic',signIn:result.sign_in_commands?.claude},
      {name:'openai',cli:'codex',key:'openai',signIn:result.sign_in_commands?.codex},
      {name:'gemini',cli:'gemini',cliLabel:'Antigravity CLI',key:'gemini',signIn:result.sign_in_commands?.gemini},
      {name:'opencode',cli:'opencode',signIn:result.sign_in_commands?.opencode},
      {name:'ollama',cli:'ollama'},
      {name:'copilot',cli:'gh',signIn:result.sign_in_commands?.github}];
    return <><div className="service-summary access-summary">{services.filter(service=>cli[service.cli]!==undefined||service.key).map(service=>{
      const state=String(cli[service.cli]??'Not installed');
      const cliReady=state==='Ready';
      const keyConfigured=service.key?Boolean(keys[service.key]):false;
      const ready=cliReady||keyConfigured;
      return <div className={`service-card${ready?' connected':''}`} key={service.name}>
        <div className="service-card-heading"><strong>{providerName[service.name]||title(service.name)}</strong><span className={`service-status ${ready?'ready':'missing'}`}>{ready?'Ready':'Not ready'}</span></div>
        <CheckList items={[{label:service.cliLabel||'CLI sign-in',ok:cliReady,optional:Boolean(service.key),detail:state+(cliReady||!service.signIn?'':` · On the Mac: ${service.signIn}`)+(service.name==='gemini'&&result.legacy_gemini_cli?' · The old Gemini CLI is also installed but is not used':'')}]}/>
        {service.key&&(!cliReady||keyConfigured)&&<CheckList items={[{label:'Fallback API key',ok:keyConfigured,optional:true,detail:keyConfigured?'Configured on Mac':'Not configured · use instead of the CLI'}]}/>}
      </div>})}</div>{result.setup&&<p>{result.setup}</p>}</>;
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
  if(command.operation==='tool.project') {
    const candidates:Data[]=result.candidates||[];
    return <>{candidates.length>0&&<><h4>Discovered projects on Mac</h4><div className="result-cards">{candidates.map((cand:Data)=><div className="result-card" key={cand.path}><strong>{cand.name} ({cand.id})</strong><span>{cand.type}{cand.has_orchestrator?' · Configured':''}</span><small>{cand.path}</small></div>)}</div></>}</>;
  }
  return null;
}

const specializedKeys:Record<string,string[]>={
  'tool.keys':['cli_status','configured','cli_available','legacy_gemini_cli','sign_in_commands','setup'],
  'tool.fleet':['machines','selected','probes','candidates'],
  'tool.instructions':['files','role_prompts','content'],
  'tool.email':['notification_emails','notification_provider','sender_email','display_name','sender_ready'],
  'tool.docs':['content'],
  'tool.distribution':['configured','firebase_cli','firebase_logged_in','google_service_info','export_options','headless_signing','setup'],
  'tool.project':['candidates','current','change','registered','projects','path'],
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
