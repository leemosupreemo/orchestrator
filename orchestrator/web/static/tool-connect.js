(function (root) {
  "use strict";
  const steps = [["check", "Check this computer"], ["prerequisites", "Prepare the installer"], ["install", "Install the AI tool"], ["sign_in", "Sign in through your browser"], ["ready", "Ready for jobs"]];
  const hosts = ["openai.com", "chatgpt.com", "claude.ai", "claude.com", "anthropic.com", "poolside.ai", "poolside.com", "openrouter.ai", "antigravity.google", "google.com"];
  // Every AI tool that has a sign-in (ai_providers.SIGN_IN_COMMANDS keeps the same ids; a test checks they match).
  // `match` finds the tool in a failed job's log; `paste` is for tools whose sign-in ends by asking for a code from the browser.
  const providers = {
    claude: {label: "Claude", match: /claude|anthropic/, paste: true},
    codex: {label: "Codex", match: /codex|openai/},
    agy: {label: "Antigravity", match: /antigravity|\bagy\b/, paste: true},
    pool: {label: "Pool", match: /\bpool\b|poolside/, paste: true},
  };
  const providerFromText = text => {
    const lower = String(text || "").toLowerCase();
    const id = Object.keys(providers).find(key => providers[key].match.test(lower));
    return id ? {id, label: providers[id].label} : null;
  };
  // The tool's own prompt for a code, so any tool that asks gets the paste box, not only the ones listed above.
  const codePrompt = /paste.{0,40}(?:code|key)|copy the code.{0,40}paste|enter.{0,30}(?:authori[sz]ation |verification )?code|enter api key|code from (?:the|your) browser/i;
  const clean = s => s.replace(/\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)/g, "").replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, "").replace(/\r/g, "\n");
  function progressState(text, previous = {step: "check", detail: "Starting…", urls: []}, provider = "") {
    const output = clean(text);
    let step = previous.step, detail = previous.detail, signInOffset = 0, rawSignInOffset = 0, position = 0;
    for (const line of output.split("\n")) {
      const lineStart = position; position += line.length + 1;
      const at = line.indexOf("@@ORCHESTRATOR_CONNECT@@");
      if (at < 0) continue;
      try {
        const event = JSON.parse(line.slice(at + 24));
        if (steps.some(([id]) => event.step === id) || event.step === "error") {
          step = event.step; detail = String(event.detail || "");
          if (step === "sign_in") signInOffset = lineStart;
        }
      } catch { /* an output chunk can end in the middle of a marker */ }
    }
    const rawSignInAt = String(text || "").lastIndexOf('"step": "sign_in"');
    const rawSignInAtCompact = String(text || "").lastIndexOf('"step":"sign_in"');
    rawSignInOffset = Math.max(0, rawSignInAt, rawSignInAtCompact);
    const urls = [];
    const addUrl = rawUrl => {
      try {
        const url = new URL(rawUrl.replace(/[),.;]+$/, ""));
        if (url.username || url.password || !hosts.some(h => url.hostname === h || url.hostname.endsWith("." + h))) return;
        const prefixIdx = urls.findIndex(u => url.href.startsWith(u));
        if (prefixIdx >= 0) { urls[prefixIdx] = url.href; return; }
        if (!urls.some(u => u === url.href || u.startsWith(url.href))) urls.push(url.href);
      } catch { /* only complete HTTPS URLs are usable */ }
    };
    if (step === "sign_in") {
      for (const match of String(text || "").slice(rawSignInOffset).matchAll(/\x1b\]8;[^;\x07\x1b]*;(https:\/\/[^\x07\x1b]+)(?:\x07|\x1b\\)/g)) addUrl(match[1]);
      for (const match of output.slice(signInOffset).matchAll(/https:\/\/[^\s<>"'`]+/g)) addUrl(match[0]);
    }
    if (step === "sign_in" && !urls.length && previous.step === step) urls.push(...(previous.urls || []));
    const tail = output.slice(-2000);
    const signInText = output.slice(signInOffset);
    const cliTail = signInText.replace(/^@@ORCHESTRATOR_CONNECT@@.*$/gm, "").replace(/^Signing in to .*$/gm, "").slice(-2000);
    const codeText = signInText.replace(/https:\/\/[^\s<>"'`]+/g, "");
    const code = step === "sign_in" ? [...codeText.matchAll(/\b[A-Z0-9]{4,5}-[A-Z0-9]{4,5}\b/g)].at(-1)?.[0] || "" : "";
    const needsCode = step === "sign_in" && !code && urls.length > 0
      && (Boolean(providers[provider]?.paste) || codePrompt.test(signInText));
    const details = output.replace(/^@@ORCHESTRATOR_CONNECT@@.*$/gm, "").replace(/\n{3,}/g, "\n\n").trim();
    return {step, detail, urls, code, needsCode, needsAgreement: /\[(?:Y\/n|y\/N|yes\/no)\]\s*$/i.test(tail.trim()),
      needsContinue: step === "sign_in" && !urls.length && !needsCode
        && /press.{0,15}enter|Log in to Poolside|Select login method|enter\s+(?:select|submit)|select.{0,25}(?:option|platform|login method)/i.test(cliTail),
      output, details};
  }

  async function open(provider, name, {api, formDialog, onClose = () => {}, onConnected = null}) {
    const doc = root.document, esc = root.Html.escape;
    let closed = false, finished = false, checkingCode = false, runId, log = "", offset = 0;
    // Two views in one dialog. Getting the tool ready shows the checklist. Signing in replaces it with just what you do:
    // open the page, then paste the code it gives you (or enter the code shown here). Short, so Submit is never off screen.
    const promise = formDialog(`Connect ${name}`, `<p class="muted" id="connect-intro">Orchestrator gets ${esc(name)} ready on this computer, then you approve it in your browser.</p>
      <ol class="ai-steps" id="connect-steps">${steps.map(([id,label]) => `<li class="ai-step" data-connect-step="${id}"><span class="ai-step-icon" aria-hidden="true"></span><span class="ai-step-text"><strong>${esc(label)}</strong><small class="muted" data-step-detail></small></span></li>`).join("")}</ol>
      <div id="connect-status" role="status" aria-live="polite"></div>
      <pre id="connect-question" class="doc connect-question" hidden></pre>
      <div id="connect-browser" class="stack"></div>
      <div id="connect-code-row" class="stack" hidden><label class="field"><span>2. Paste the code it shows</span><input id="connect-code" autocomplete="off" spellcheck="false" placeholder="Paste code or key"></label><div><button type="button" class="btn primary" id="connect-code-send">Submit</button></div></div>
      <div class="row gap-8 wrap"><button type="button" class="btn primary" id="connect-agree" hidden>Agree and continue</button><button type="button" class="btn" id="connect-continue" hidden>Continue</button></div>
      `, "Done", {cancelLabel: "Close"});
    promise.then(() => {closed = true; onClose();});
    const el = id => doc.getElementById(id);
    const ok = el("dialog-ok"); if (ok) ok.hidden = true;
    const hideControls = () => {for (const id of ["connect-agree", "connect-continue", "connect-code-row", "connect-question"]) if (el(id)) el(id).hidden = true;};
    const stopSteps = () => {doc.querySelectorAll("[data-connect-step]").forEach(row => {if (String(row.className).includes("is-active")) row.className = "ai-step";});};
    const showError = message => { if (!closed && el("connect-status")) {el("connect-status").className="notice bad";el("connect-status").textContent=message;} };
    const send = async data => {
      if (!runId || finished) return;
      try {await api(`runs/${encodeURIComponent(runId)}/input`, {method:"POST", body:{data}});} catch (error) {showError(error.message);}
    };
    el("connect-agree").onclick = () => send("y\r");
    el("connect-continue").onclick = () => send("\r");
    const sendCode = () => {const input=el("connect-code"),value=input?.value.trim();if(value){checkingCode=true;send(value+"\r");input.value="";if(el("connect-status"))el("connect-status").textContent="Checking the code…";}};
    el("connect-code-send").onclick = sendCode;
    el("connect-code").onkeydown = event => {if(event.key==="Enter"){event.preventDefault();sendCode();}};
    let reached = 0, lastProgress, shownLinks = "";
    try {
      const response = await api("runs", {method:"POST",body:{action:"ai_connect",params:{provider,title:`Connect ${name}`}}});
      runId=response.run.id;
      const decoder = new TextDecoder();
      while (!closed && !finished) {
        const out=await api(`runs/${encodeURIComponent(runId)}/output?offset=${offset}&wait=1`);
        offset=out.offset;
        const bytes=Uint8Array.from(root.atob(out.data||""), c=>c.charCodeAt(0));
        if(bytes.length)checkingCode=false;
        log=(log+decoder.decode(bytes,{stream:!out.finished})).slice(-100000);
        if(closed||finished)break;
        const state=progressState(log,lastProgress,provider), at=steps.findIndex(([id])=>id===state.step);
        lastProgress=state;
        if(at>=0)reached=Math.max(reached,at);
        for(let i=0;i<steps.length;i++){
          const row=doc.querySelector(`[data-connect-step="${steps[i][0]}"]`);
          row.className=`ai-step ${i<reached?"is-done":i===reached?(state.step==="error"?"is-failed":"is-active"):""}`;
          row.querySelector("[data-step-detail]").textContent=i===reached?state.detail:"";
        }
        const signingIn=state.step==="sign_in"&&(state.urls.length>0||state.code);
        el("connect-steps").hidden=signingIn; // the checklist has done its job: only the sign-in is left
        el("connect-intro").hidden=signingIn;
        const asking=state.needsAgreement||state.needsContinue; // show what it asks, so Agree or Continue is an informed click
        el("connect-question").hidden=!asking;
        if(asking)el("connect-question").textContent=state.details.split("\n").filter(l=>l.trim()).slice(-6).join("\n");
        el("connect-status").textContent=checkingCode?"Checking the code…":state.detail;
        const links=JSON.stringify([state.code,state.urls]);
        if(links!==shownLinks){ // only when it changes: the page is redrawn twice a second and would drop a half-pasted link
          shownLinks=links;
          const url=state.urls.at(-1);
          el("connect-browser").innerHTML=url?`<div class="stack gap-8"><span>1. Open the sign-in page and approve</span>
            <div class="row gap-8 wrap"><a class="btn primary" href="${esc(url)}" target="_blank" rel="noopener noreferrer">Open</a><button type="button" class="btn" data-connect-copy="${esc(url)}">Copy link</button></div>
            ${state.code?`<span>2. Enter this code there: <strong class="mono connect-approval-code">${esc(state.code)}</strong></span>`:""}</div>`:"";
          el("connect-browser").querySelectorAll("[data-connect-copy]").forEach(btn=>{btn.onclick=async()=>{ // to sign in on another device
            try{await root.navigator.clipboard.writeText(btn.dataset.connectCopy);btn.textContent="Copied";}
            catch{root.prompt?.("Copy this link",btn.dataset.connectCopy);}
          };});
        }
        const codeRow=el("connect-code-row"), showCode=state.needsCode&&!checkingCode;
        if(showCode&&codeRow.hidden){codeRow.hidden=false;codeRow.scrollIntoView?.({block:"nearest"});} // keep Submit in view
        else if(!showCode)codeRow.hidden=true;
        el("connect-agree").hidden=!state.needsAgreement;
        el("connect-continue").hidden=!state.needsContinue||state.needsAgreement;
        if(out.finished){
          finished=true;
          hideControls();
          el("connect-steps").hidden=false;el("connect-browser").innerHTML="";shownLinks=""; // back to the checklist, with the result
          if(ok)ok.hidden=false;
          if(out.exit_code===0){
            el("connect-status").className="notice good";
            el("connect-status").textContent=`${name} is connected. Its models are ready for jobs.`;
            doc.querySelectorAll("[data-connect-step]").forEach(row=>row.className="ai-step is-done");
            if (typeof onConnected === "function") onConnected();
          }else{
            stopSteps();
            showError(state.step==="error"?state.detail:`${name} did not finish connecting. Check the sign-in details, then try Connect again.`);
          }
          return;
        }
        await new Promise(resolve=>setTimeout(resolve,500));
      }
    }catch(error){
      finished=true;hideControls();stopSteps();
      showError(error.message);
      if(ok&&!closed)ok.hidden=false;
    }
  }
  root.ToolConnect={steps,providers,providerFromText,progressState,open};
})(typeof globalThis!=="undefined"?globalThis:this);
