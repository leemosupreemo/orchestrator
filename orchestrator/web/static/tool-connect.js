(function (root) {
  "use strict";
  const steps = [["check", "Check this computer"], ["prerequisites", "Prepare the installer"], ["install", "Install the AI tool"], ["sign_in", "Sign in through your browser"], ["ready", "Ready for jobs"]];
  const hosts = ["openai.com", "chatgpt.com", "claude.ai", "anthropic.com", "poolside.ai", "poolside.com", "openrouter.ai", "antigravity.google", "google.com"];
  // Every AI tool that has a sign-in (ai_providers.SIGN_IN_COMMANDS keeps the same ids; a test checks they match).
  // `match` finds the tool in a failed job's log; `paste` is for tools whose sign-in ends by asking for a code from the browser.
  const providers = {
    claude: {label: "Claude", match: /claude|anthropic/, paste: true},
    codex: {label: "Codex", match: /codex|openai/},
    agy: {label: "Antigravity", match: /antigravity|\bagy\b/},
    pool: {label: "Pool", match: /\bpool\b|poolside/},
  };
  const providerFromText = text => {
    const lower = String(text || "").toLowerCase();
    const id = Object.keys(providers).find(key => providers[key].match.test(lower));
    return id ? {id, label: providers[id].label} : null;
  };
  // The tool's own prompt for a code, so any tool that asks gets the paste box, not only the ones listed above.
  const codePrompt = /paste.{0,40}code|enter.{0,30}(?:authori[sz]ation |verification )?code|code from (?:the|your) browser/i;
  const clean = s => s.replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, "").replace(/\r/g, "\n");
  function progressState(text, previous = {step: "check", detail: "Starting…", urls: []}, provider = "") {
    const output = clean(text);
    let step = previous.step, detail = previous.detail, signInOffset = 0, position = 0;
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
    const urls = [];
    if (step === "sign_in") {
      for (const match of output.slice(signInOffset).matchAll(/https:\/\/[^\s<>"'`]+/g)) {
        try {
          const url = new URL(match[0].replace(/[),.;]+$/, ""));
          if (!url.username && !url.password && hosts.some(h => url.hostname === h || url.hostname.endsWith("." + h)) && !urls.includes(url.href)) urls.push(url.href);
        } catch { /* only complete HTTPS URLs are usable */ }
      }
    }
    if (step === "sign_in" && !urls.length && previous.step === step) urls.push(...(previous.urls || []));
    const tail = output.slice(-2000);
    const code = step === "sign_in" ? [...output.slice(signInOffset).matchAll(/\b[A-Z0-9]{4}-[A-Z0-9]{4}\b/g)].at(-1)?.[0] || "" : "";
    const needsCode = step === "sign_in" && !code && urls.length > 0
      && (Boolean(providers[provider]?.paste) || codePrompt.test(output.slice(signInOffset)));
    return {step, detail, urls, code, needsCode, needsAgreement: /\[(?:Y\/n|y\/N|yes\/no)\]\s*$/i.test(tail.trim()),
      needsContinue: /press.{0,15}enter|Use Poolside Platform|select.{0,25}(?:option|platform)/i.test(tail), output};
  }

  async function open(provider, name, {api, formDialog, onClose = () => {}}) {
    const doc = root.document, esc = root.Html.escape;
    let closed = false, finished = false, runId, log = "", offset = 0;
    const promise = formDialog(`Connect ${name}`, `<p>Orchestrator prepares this computer and guides you through sign-in. Approve access in your browser when asked.</p>
      <ol class="ai-steps" id="connect-steps">${steps.map(([id,label]) => `<li class="ai-step" data-connect-step="${id}"><span class="ai-step-icon" aria-hidden="true"></span><span class="ai-step-text"><strong>${esc(label)}</strong><small class="muted" data-step-detail></small></span></li>`).join("")}</ol>
      <div id="connect-status" role="status" aria-live="polite"></div><div id="connect-browser" class="stack"></div>
      <div id="connect-code-row" class="stack" hidden><label class="field"><span>After approving in your browser, paste the code it shows here</span><input id="connect-code" autocomplete="off" required spellcheck="false" placeholder="Paste code"></label><div><button type="button" class="btn primary" id="connect-code-send">Submit code</button></div></div>
      <div class="row gap-8 wrap"><button type="button" class="btn primary" id="connect-agree" hidden>Agree and continue</button><button type="button" class="btn" id="connect-continue" hidden>Continue</button><button type="button" class="btn danger" id="connect-stop">Stop sign-in</button></div>
      <details id="connect-details"><summary>Sign-in details and advanced input</summary><pre id="connect-output" class="doc" style="max-height:240px;overflow:auto;white-space:pre-wrap"></pre><label class="field"><span>If the sign-in asks for an answer or code</span><input id="connect-input" autocomplete="off"></label><button type="button" class="btn small" id="connect-send">Send answer</button></details>
      <p class="muted">Closing this dialog keeps sign-in running. You can find it under Running now.</p>`, "Done", {cancelLabel: "Close"});
    promise.then(() => {closed = true; onClose();});
    const el = id => doc.getElementById(id);
    const ok = el("dialog-ok"); if (ok) ok.hidden = true;
    const showError = message => { if (!closed && el("connect-status")) {el("connect-status").className="notice bad";el("connect-status").textContent=message;} };
    const send = async data => {
      if (!runId || finished) return;
      try {await api(`runs/${encodeURIComponent(runId)}/input`, {method:"POST", body:{data}});} catch (error) {showError(error.message);}
    };
    el("connect-agree").onclick = () => send("y\r");
    el("connect-continue").onclick = () => send("\r");
    const sendAnswer = () => {const input=el("connect-input");if(input?.value){send(input.value+"\r");input.value="";}};
    el("connect-send").onclick = sendAnswer;
    const sendCode = () => {const input=el("connect-code"),value=input?.value.trim();if(value){send(value+"\r");input.value="";if(el("connect-status"))el("connect-status").textContent="Checking the code…";}};
    el("connect-code-send").onclick = sendCode;
    el("connect-code").onkeydown = event => {if(event.key==="Enter"){event.preventDefault();sendCode();}};
    el("connect-input").onkeydown = event => {if(event.key==="Enter"){event.preventDefault();sendAnswer();}};
    el("connect-stop").onclick = async () => {
      if (!runId || finished) return;
      try {await api(`runs/${encodeURIComponent(runId)}/stop`,{method:"POST",body:{}});if(!closed)el("connect-status").textContent="Stopping sign-in…";}catch(error){showError(error.message);}
    };
    let reached = 0, lastProgress, shownLinks = "";
    try {
      const response = await api("runs", {method:"POST",body:{action:"ai_connect",params:{provider,title:`Connect ${name}`}}});
      runId=response.run.id;
      const decoder = new TextDecoder();
      while (!closed) {
        const out=await api(`runs/${encodeURIComponent(runId)}/output?offset=${offset}&wait=1`);
        offset=out.offset;
        const bytes=Uint8Array.from(root.atob(out.data||""), c=>c.charCodeAt(0));
        log=(log+decoder.decode(bytes,{stream:!out.finished})).slice(-100000);
        if(closed)break;
        const state=progressState(log,lastProgress,provider), at=steps.findIndex(([id])=>id===state.step);
        lastProgress=state;
        if(at>=0)reached=Math.max(reached,at);
        for(let i=0;i<steps.length;i++){
          const row=doc.querySelector(`[data-connect-step="${steps[i][0]}"]`);
          row.className=`ai-step ${i<reached?"is-done":i===reached?(state.step==="error"?"is-failed":"is-active"):""}`;
          row.querySelector("[data-step-detail]").textContent=i===reached?state.detail:"";
        }
        el("connect-output").textContent=state.output;
        el("connect-status").textContent=state.detail;
        const links=JSON.stringify([state.code,state.urls]);
        if(links!==shownLinks){ // only when it changes: the page is redrawn twice a second and would drop a half-pasted link
          shownLinks=links;
          el("connect-browser").innerHTML=(state.code ? `<p>Approval code: <strong class="mono">${esc(state.code)}</strong></p>` : "")+state.urls.map(url=>`<a class="btn primary" href="${esc(url)}" target="_blank" rel="noopener noreferrer">Open sign-in in your browser ↗</a><label class="field"><span>Or open this link yourself</span><input class="mono" readonly value="${esc(url)}" data-connect-link></label>`).join("");
          el("connect-browser").querySelectorAll("[data-connect-link]").forEach(input=>{input.onfocus=()=>input.select();});
        }
        el("connect-code-row").hidden=!state.needsCode;
        el("connect-agree").hidden=!state.needsAgreement;
        el("connect-continue").hidden=!state.needsContinue||state.needsAgreement;
        if(state.needsAgreement)el("connect-details").open=true;
        if(out.finished){
          finished=true;
          for(const id of ["connect-agree","connect-continue","connect-stop","connect-code-row"])el(id).hidden=true;
          if(ok)ok.hidden=false;
          if(out.exit_code===0){
            el("connect-status").className="notice good";
            el("connect-status").textContent=`${name} is connected. Its models are ready for jobs.`;
            doc.querySelectorAll("[data-connect-step]").forEach(row=>row.className="ai-step is-done");
          }else showError(state.step==="error"?state.detail:`${name} did not finish connecting. Check the sign-in details, then try Connect again.`);
          return;
        }
        await new Promise(resolve=>setTimeout(resolve,500));
      }
    }catch(error){showError(error.message);if(ok&&!closed)ok.hidden=false;}
  }
  root.ToolConnect={steps,providers,providerFromText,progressState,open};
})(typeof globalThis!=="undefined"?globalThis:this);
