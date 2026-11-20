(function (root) {
  "use strict";
  const TERMINAL = new Set(["current", "cancelled", "failed", "unavailable", "expired"]);
  const messages = {
    requested: "Update requested. Waiting for your computer…",
    checking: "Checking for an update…",
    downloading: "Downloading the update…",
    waiting_for_work: "Waiting for current jobs to finish. Your work will not be stopped.",
    updating: "Finishing installation—this step cannot be cancelled safely.",
    reconnecting: "Reconnecting to your computer…",
    cancelling: "Cancelling… Waiting for your computer to confirm.",
    cancelled: "Update cancelled. Your work has not been stopped.",
    failed: "The update did not finish. Check Computers for details, then try again.",
    unavailable: "This installation needs a one-time upgrade to the latest Orchestrator app before automatic recovery is available.",
    expired: "The update request expired. Check Computers before trying again.",
  };
  function missing(error, runner) {
    return error?.status === 404 && runner?.capabilities?.preview_remote === false;
  }
  function create({request, context, reconnect, probe, render, storage, sleep = (ms) => new Promise(r => setTimeout(r, ms)), now = Date.now}) {
    let pending = null, attempt = null, operation = null, machine = null, cancelRequested = false, cancelling = null;
    const identity = (c) => c?.machineId && c?.project ? `${c.machineId}/${c.project}` : "";
    const key = (c) => `orchestrator.preview-update/${identity(c)}`;
    const same = (c) => identity(context()) === identity(c) && context()?.owner === true;
    const load = (c) => { try { return JSON.parse(storage?.getItem(key(c)) || "null"); } catch { return null; } };
    const save = () => { try { storage?.setItem(key(attempt.context), JSON.stringify(attempt)); } catch { /* memory still deduplicates this visit */ } };
    const show = (state, message, done = false) => render({state, message: message || messages[state], done,
      name: machine?.name || "your computer", canRetry: done && (machine?.update_control === 1 || attempt?.supported === true) && !["updating", "unavailable"].includes(state),
      canCancel: !done && !["updating", "cancelling"].includes(state) && !["updating", "current"].includes(operation?.state)});
    const finish = (state, message) => { attempt.ended = true; attempt.resultState = state; attempt.resultMessage = message; save(); show(state, message, true); return false; };
    async function machines() {
      const reply = await request("machines", {signal: AbortSignal.timeout(8000)});
      return (reply.machines || []).find(m => m.id === attempt.context.machineId);
    }
    async function cancel() {
      if (!attempt || attempt.ended || !same(attempt.context) || ["updating", "current"].includes(operation?.state)) return false;
      cancelRequested = true; attempt.cancelRequested = true; save();
      show("cancelling");
      if (!operation || cancelling) return false;
      cancelling = (async () => {
        try {
          const reply = await request("machines/cancel-update", {method: "POST", body: {
            machine_id: attempt.context.machineId, operation_id: operation.id}, signal: AbortSignal.timeout(8000)});
          operation = reply.update_operation;
          return true;
        } catch (error) {
          if (error.status === 409) {
            // The install claim won. Wait for the next authoritative machine status.
            cancelRequested = false; attempt.cancelRequested = false; save();
            show("updating");
          } else if ([401, 403, 404].includes(error.status)) {
            return finish("failed", "Cancellation was not accepted. Check Computers for the update's actual status.");
          } else show("cancelling", "Cancellation is pending. Waiting for the computer connection to return…");
          return false;
        } finally { cancelling = null; }
      })();
      return cancelling;
    }
    async function monitor() {
      while (same(attempt.context) && !attempt.ended) {
        if (now() >= attempt.deadline) {
          // Preserve the operation for resume: an installation timeout is not a cancelled update.
          show(operation?.state === "updating" ? "updating" : "failed", "This is taking longer than expected. Check Computers for the update's actual status.", true);
          return false;
        }
        try {
          machine = await machines();
          if (!same(attempt.context)) break;
          if (!machine) return finish("failed", "This computer was removed from your account.");
          const fresh = machine.update_operation;
          if (!attempt.operationId && attempt.awaitingOperation) {
            if (fresh && (!TERMINAL.has(fresh.state) || fresh.created * 1000 >= attempt.startedAt - 1000)) {
              attempt.operationId = fresh.id; save();
            } else {
              show(cancelRequested ? "cancelling" : "reconnecting", "Confirming whether the update request reached your computer…");
              await sleep(2000); continue;
            }
          }
          if (!fresh || fresh.id !== attempt.operationId) return finish("failed", "The update request is no longer available. Check Computers before trying again.");
          operation = fresh;
          if (cancelRequested && !TERMINAL.has(operation.state) && operation.state !== "updating") await cancel();
          if (attempt.ended) return false;
          if (operation.state === "current") {
            show("reconnecting");
            await reconnect(machine, () => same(attempt.context));
            if (!same(attempt.context)) break;
            if (!await probe()) return finish("failed", "The installed release still does not support this preview. A compatible release is needed.");
            if (!same(attempt.context)) break;
            attempt.ended = true; attempt.resultState = "current"; save();
            show("current", "Orchestrator updated. Opening Preview…", true);
            return true;
          }
          if (TERMINAL.has(operation.state)) return finish(operation.state);
          show(cancelRequested ? "cancelling" : operation.state);
        } catch (error) {
          if ([401, 403, 404].includes(error.status)) return finish("failed", "Sign in to your account again to check this update. Its status has been preserved.");
          show(cancelRequested ? "cancelling" : operation?.state === "updating" ? "updating" : "reconnecting",
            "Waiting for the computer connection. The update's status will appear here when it returns…");
        }
        await sleep(2000);
      }
      if (!attempt.ended) show("unavailable", "The update remains linked to the original computer and project. Return there to check its progress.", true);
      return false;
    }
    function run(work) {
      pending = work().catch(error => finish("failed", error.message || messages.failed)).finally(() => { pending = null; });
      return pending;
    }
    function recover(error, runner, {force = false} = {}) {
      const legacy = error?.status === 404 && error.message === "Not found" && !runner?.capabilities;
      if (!missing(error, runner) && !legacy) return Promise.resolve(false);
      const c = context();
      if (!identity(c) || !c.owner) return Promise.resolve(false);
      if (pending) return pending;
      const saved = load(c);
      if (!force && (saved?.ended || (attempt?.ended && identity(attempt.context) === identity(c)))) {
        attempt = saved || attempt;
        show(attempt.resultState || "failed", attempt.resultMessage, true);
        return Promise.resolve(false);
      }
      if (!force && saved?.operationId) return resume();
      attempt = {context: c, startedAt: now(), deadline: now() + 1800000, ended: false};
      cancelRequested = false; operation = null;
      if (legacy) return Promise.resolve(finish("unavailable", "This installation does not report preview compatibility. Open Computers for one-time upgrade instructions."));
      return run(async () => {
        machine = await machines();
        if (!same(c)) return false;
        if (machine?.capabilities?.preview_remote !== false) return false;
        if (!machine?.online || !machine.updatable || machine.update_control !== 1) return finish("unavailable");
        attempt.supported = true;
        attempt.awaitingOperation = true; save(); show("requested");
        let reply;
        try {
          reply = await request("machines/update-recovery", {method: "POST", body: {machine_id:c.machineId}, signal: AbortSignal.timeout(8000)});
        } catch (error) {
          if ([400, 401, 403, 404, 409].includes(error.status)) throw error;
          // The server may have committed. Keep the attempt durable and find the operation; never submit twice.
          show("reconnecting", "Confirming the update request. Its response was interrupted…");
          return monitor();
        }
        operation = reply.update_operation;
        if (!operation?.id) return finish("unavailable", "This control plane needs an update before automatic recovery is available.");
        attempt.operationId = operation.id; save();
        if (cancelRequested) await cancel();
        return monitor();
      });
    }
    function resume() {
      if (pending) return pending;
      const c = context(), saved = load(c);
      if (!identity(c) || !c.owner || !saved || saved.ended || identity(saved.context) !== identity(c)) return Promise.resolve(false);
      attempt = saved; cancelRequested = saved.cancelRequested === true;
      // A fresh monitoring window after reload must not create a new update operation.
      attempt.deadline = now() + 1800000;
      return run(async () => {
        machine = await machines(); operation = machine?.update_operation;
        if (!operation && attempt.awaitingOperation && !attempt.operationId) return monitor();
        if (!operation || (attempt.operationId && operation.id !== attempt.operationId)) return finish("failed");
        attempt.operationId = operation.id; save();
        return monitor();
      });
    }
    return {recover, resume, cancel, get active() { return Boolean(pending); }};
  }
  root.PreviewUpdateRecovery = {create, missing, messages};
  if (typeof module !== "undefined") module.exports = root.PreviewUpdateRecovery;
})(globalThis);
