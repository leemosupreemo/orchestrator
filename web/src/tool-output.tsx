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

export function ToolOutput({command,showModels=true}:{command:Data;showModels?:boolean}) {
  const result=command.result||{};
  const label=menu.tools.find(item=>item.operation===command.operation)?.label||title(command.operation||'Tool');
  const action=command.inputs?.action||command.inputs?.suite||(command.inputs?.live_discovery?'live discovery':command.inputs?.refresh?'refresh':undefined);
  const details=Object.entries(result).filter(([key])=>!hidden.has(key)&&!(command.operation==='tool.models'&&key==='selected'));
  const hasLog=Boolean(command.output||result.output||['queued','running'].includes(command.status));
  return <div className="tool-result organized-result">
    <div className="run-heading"><div><div className="eyebrow">{label}{action?` · ${title(String(action))}`:''}</div><h3>{statusText[command.status]||title(command.status||'Status')}</h3></div><span className={`badge ${command.status}`}>{command.status}</span></div>
    <p>{summary(command)}</p>
    {command.operation==='tool.models'&&Array.isArray(result.models)&&<><p>{result.models.length} model{result.models.length===1?'':'s'} in the catalog.</p>{showModels&&<ModelList models={result.models} selected={result.selected||[]}/>}</>}
    {command.operation==='tool.fleet'&&Array.isArray(result.fleet)&&<p>{result.fleet.length} machines in the fleet.</p>}
    {details.length>0&&<div className="run-facts">{details.map(([key,value])=><Detail key={key} label={key} value={value}/>)}</div>}
    {hasLog&&<RunLog command={command}/>}
    {Object.keys(result).length>0&&<details className="run-log"><summary>Full result data</summary><pre>{JSON.stringify(result,null,2)}</pre></details>}
  </div>;
}
