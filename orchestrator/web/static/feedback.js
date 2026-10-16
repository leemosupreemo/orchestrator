(function (root) {
  "use strict";

  const SUPPORT_EMAIL = "support@thejauntcompany.com";
  const FEEDBACK_SUBJECT_PREFIX = "[Orchestrator Feedback]";
  const SUBMIT_TIMEOUT_MS = 10000;

  function withTimeout(promise, ms, label) {
    let timer;
    const timeout = new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error(`${label} timed out after ${ms}ms`)), ms);
    });
    return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
  }

  function isOnline() {
    return typeof navigator === "undefined" || navigator.onLine !== false;
  }

  async function submitFeedback({
    userName = "",
    playerName = "",
    contactEmail = "",
    feedbackText = "",
    category = "general",
    title = "",
    jobId = "",
    explanation = "",
    platform = "Web",
    appVersion = "1.0.0",
    attemptNumber = 1,
    apiClient = null
  } = {}) {
    const cleanText = String(feedbackText || explanation || "").trim();
    if (!cleanText) {
      throw new Error("Feedback text is required");
    }
    const cleanName = String(playerName || userName || "").trim() || "User";
    const payload = {
      userName: cleanName,
      playerName: cleanName,
      contactEmail: String(contactEmail || "").trim(),
      category: String(category || "general").trim(),
      title: String(title || "").trim(),
      jobId: String(jobId || "").trim(),
      explanation: String(explanation || "").trim(),
      feedbackText: cleanText,
      platform: String(platform || "Web"),
      appVersion: String(appVersion || "1.0.0"),
      attemptNumber: Number(attemptNumber) || 1,
      targetEmail: SUPPORT_EMAIL,
      subjectPrefix: FEEDBACK_SUBJECT_PREFIX,
      createdAtIso: new Date().toISOString()
    };

    const deliveryReport = {
      success: true,
      sent: false,
      queued: false,
      failed: false
    };

    try {
      let res;
      const apiFn = apiClient || (typeof root.api === "function" ? root.api : null);
      if (apiFn) {
        res = await withTimeout(
          apiFn("feedback", { method: "POST", body: payload }),
          SUBMIT_TIMEOUT_MS,
          "Feedback submit"
        );
      } else if (typeof fetch === "function") {
        const fetchRes = await withTimeout(
          fetch("/api/feedback", {
            method: "POST",
            headers: { "Content-Type": "application/json", "X-Orchestrator-UI": "1" },
            body: JSON.stringify(payload)
          }),
          SUBMIT_TIMEOUT_MS,
          "Feedback submit"
        );
        res = await fetchRes.json().catch(() => ({}));
      }

      if (res && (res.ok || res.success)) {
        if (res.delivered) {
          deliveryReport.sent = true;
        } else {
          deliveryReport.queued = true;
        }
        return deliveryReport;
      }
      deliveryReport.failed = true;
      deliveryReport.success = false;
      return deliveryReport;
    } catch (err) {
      if (typeof localStorage !== "undefined") {
        try {
          const list = JSON.parse(localStorage.getItem("orchestrator_queued_feedback") || "[]");
          list.push(payload);
          localStorage.setItem("orchestrator_queued_feedback", JSON.stringify(list));
          deliveryReport.queued = true;
          return deliveryReport;
        } catch (_) {}
      }
      deliveryReport.failed = true;
      deliveryReport.success = false;
      return deliveryReport;
    }
  }

  async function flushQueuedFeedback(apiClient = null) {
    if (typeof localStorage === "undefined" || !isOnline()) {
      return { flushed: 0, remaining: 0 };
    }
    let list = [];
    try {
      list = JSON.parse(localStorage.getItem("orchestrator_queued_feedback") || "[]");
    } catch (_) {
      return { flushed: 0, remaining: 0 };
    }
    if (!Array.isArray(list) || list.length === 0) {
      return { flushed: 0, remaining: 0 };
    }

    const remaining = [];
    let flushedCount = 0;
    const apiFn = apiClient || (typeof root.api === "function" ? root.api : null);

    for (const item of list) {
      try {
        let res;
        if (apiFn) {
          res = await withTimeout(
            apiFn("feedback", { method: "POST", body: item }),
            SUBMIT_TIMEOUT_MS,
            "Queued feedback flush"
          );
        } else if (typeof fetch === "function") {
          const fetchRes = await withTimeout(
            fetch("/api/feedback", {
              method: "POST",
              headers: { "Content-Type": "application/json", "X-Orchestrator-UI": "1" },
              body: JSON.stringify(item)
            }),
            SUBMIT_TIMEOUT_MS,
            "Queued feedback flush"
          );
          res = await fetchRes.json().catch(() => ({}));
        }
        if (res && (res.ok || res.success)) {
          flushedCount++;
        } else {
          remaining.push(item);
        }
      } catch (_) {
        remaining.push(item);
      }
    }

    try {
      if (remaining.length > 0) {
        localStorage.setItem("orchestrator_queued_feedback", JSON.stringify(remaining));
      } else {
        localStorage.removeItem("orchestrator_queued_feedback");
      }
    } catch (_) {}

    return { flushed: flushedCount, remaining: remaining.length };
  }

  function createModalController() {
    let currentStep = "feedback"; // 'prompt' | 'feedback' | 'improve_orchestrator' | 'thankyou'
    let currentCategory = "general"; // 'general' | 'feature_request' | 'improve_orchestrator'
    let text = "";
    let title = "";
    let contactEmail = "";
    let jobId = "";
    let submitting = false;
    let deliveryState = "sent"; // 'sent' | 'queued' | 'failed'
    let queuedOffline = false;
    let currentUserName = "";
    let currentAttempt = 1;
    let dialogElem = null;
    let previousActiveElement = null;

    function escapeHtml(str) {
      return String(str || "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
    }

    function ensureDialog() {
      if (typeof document === "undefined") return null;
      let dlg = document.getElementById("feedback-dialog");
      if (!dlg) {
        dlg = document.createElement("dialog");
        dlg.id = "feedback-dialog";
        dlg.className = "feedback-dialog";
        document.body.appendChild(dlg);
      }
      dialogElem = dlg;
      return dlg;
    }

    function render() {
      const dlg = ensureDialog();
      if (!dlg) return;

      const hasText = text.trim().length > 0;
      let contentHtml = "";

      if (currentStep === "prompt") {
        contentHtml = `
          <button type="button" class="feedback-close-btn" id="feedback-modal-close" aria-label="Close">✕</button>
          <h2 class="feedback-title">Enjoying Orchestrator?</h2>
          <p class="feedback-subtitle" style="text-align:center; padding: 0 8px;">
            ${currentUserName ? `${escapeHtml(currentUserName)}, we'd love to know if you're enjoying Orchestrator!` : "We'd love to know if you're enjoying Orchestrator!"}
          </p>
          <div class="feedback-choice-grid">
            <button type="button" class="feedback-choice-btn left" id="feedback-btn-better" aria-label="Could be better">
              <span class="feedback-choice-emoji" role="img" aria-label="Unhappy face">🙁</span>
              <span class="feedback-choice-label muted">Could be better</span>
            </button>
            <button type="button" class="feedback-choice-btn" id="feedback-btn-enjoying" aria-label="Enjoying it">
              <span class="feedback-choice-emoji" role="img" aria-label="Heart eyes face">😍</span>
              <span class="feedback-choice-label accent">Enjoying it</span>
            </button>
          </div>
        `;
      } else if (currentStep === "thankyou") {
        const isFailed = deliveryState === "failed";
        const isQueued = deliveryState === "queued";
        const isImprovement = currentCategory === "improve_orchestrator";
        const icon = isFailed ? "📡" : (isImprovement ? "🏆" : (isQueued ? "📥" : "💌"));
        const titleText = isFailed ? "Couldn't Send" : (isImprovement ? "Submission Received!" : "Thank You!");
        let message = isImprovement
          ? "Your Orchestrator improvement submission has been delivered directly to the core team. We will review your contribution for permanent inclusion in Orchestrator. If selected, we will contact your email with your prize!"
          : "We will review your message as soon as we can.";
        if (isFailed) {
          message = "We couldn't reach our servers just now. Your message is still here — try again, or come back to it later.";
        } else if (isQueued) {
          message = queuedOffline
            ? "Saved. We'll send it automatically as soon as you're back online."
            : "Saved. We're still finishing the send — it will complete on its own.";
        }

        contentHtml = `
          <div class="feedback-status-view">
            <div class="feedback-status-emoji" role="img" aria-label="${isFailed ? "Warning" : (isQueued ? "Outbox tray" : "Mailbox")}">${icon}</div>
            <h2 class="feedback-status-title ${isFailed ? "error" : ""}">${titleText}</h2>
            <p class="feedback-status-msg">${message}</p>
            <div class="feedback-status-actions">
              ${isFailed ? '<button type="button" class="btn primary" id="feedback-modal-retry">Try Again</button>' : ""}
              <button type="button" class="btn ${isFailed ? "ghost" : "primary"}" id="feedback-modal-done">${isFailed ? "Not Now" : "Done"}</button>
            </div>
          </div>
        `;
      } else if (currentStep === "improve_orchestrator" || currentCategory === "improve_orchestrator") {
        const hasEmail = contactEmail.trim().length > 0 && contactEmail.includes("@");
        const canSubmitImprovement = hasText && hasEmail && !submitting;
        contentHtml = `
          <button type="button" class="feedback-close-btn" id="feedback-modal-close" aria-label="Close">✕</button>
          <div class="feedback-type-tabs" role="tablist" aria-label="Feedback mode">
            <button type="button" class="feedback-type-tab" id="feedback-tab-feedback" role="tab" aria-selected="false" tabindex="-1">💡 Feedback & Features</button>
            <button type="button" class="feedback-type-tab active" id="feedback-tab-improve" role="tab" aria-selected="true" tabindex="0">🏆 Improve Orchestrator</button>
          </div>
          <div class="feedback-prize-banner">
            <div class="prize-badge">🏆 Prize Challenge</div>
            <p>Create and run a job locally to improve Orchestrator itself. Submit your explanation and changes here for permanent inclusion. If selected, you'll receive an <strong>awesome prize</strong> and public contributor recognition!</p>
          </div>
          <div class="feedback-job-action-box">
            <span>Want to build your improvement with a local AI job first?</span>
            <button type="button" class="btn small primary" id="feedback-create-local-job-btn"><svg class="icon icon-sm" aria-hidden="true" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"></line><line x1="5" y1="12" x2="19" y2="12"></line></svg> Create job to improve Orchestrator</button>
          </div>
          <div class="feedback-fields-stack">
            <label class="feedback-field">
              <span>Your Name</span>
              <input type="text" id="feedback-modal-name" class="feedback-input" value="${escapeHtml(currentUserName)}" placeholder="Your name or handle">
            </label>
            <label class="feedback-field">
              <span>Contact Email <strong class="accent-required">(required for prize delivery)</strong></span>
              <input type="email" id="feedback-modal-email" class="feedback-input" value="${escapeHtml(contactEmail)}" placeholder="you@example.com">
            </label>
            <label class="feedback-field">
              <span>Improvement Title</span>
              <input type="text" id="feedback-modal-title" class="feedback-input" value="${escapeHtml(title)}" placeholder="e.g. Add dark mode toggle or parallel run streaming">
            </label>
            <label class="feedback-field">
              <span>Explanation & Implementation Details</span>
              <textarea
                id="feedback-modal-text"
                class="feedback-textarea"
                placeholder="Explain what you improved in Orchestrator, why it's valuable, and how you tested it locally..."
              >${escapeHtml(text)}</textarea>
            </label>
            ${!hasText ? '<p id="feedback-empty-hint" class="feedback-empty-hint">Write an explanation above before submitting.</p>' : ""}
            <p id="feedback-email-hint" class="feedback-empty-hint" style="color: #fbbf24;" ${hasEmail || !hasText ? "hidden" : ""}>A valid contact email is required so we can reach you if your improvement is selected for a prize.</p>
            <label class="feedback-field">
              <span>Local Job ID or Branch / PR reference <span class="muted">(optional)</span></span>
              <input type="text" id="feedback-modal-job-id" class="feedback-input" value="${escapeHtml(jobId)}" placeholder="e.g. job-abc123 or branch name">
            </label>
          </div>
          <div class="feedback-actions">
            <button
              type="button"
              class="btn primary feedback-submit-btn"
              id="feedback-modal-send"
              ${!canSubmitImprovement ? "disabled" : ""}
              ${canSubmitImprovement ? "" : (hasText ? 'title="Enter a valid contact email to be eligible for the prize." aria-describedby="feedback-email-hint"' : 'title="Write an explanation in the field above before submitting." aria-describedby="feedback-empty-hint"')}
            >
              ${submitting ? '<span class="feedback-btn-spinner" aria-hidden="true"></span>' : '<span aria-hidden="true">🏆</span>'}
              <span>${submitting ? "Submitting…" : "Submit for Permanent Inclusion & Prize"}</span>
            </button>
            <button type="button" class="btn ghost" id="feedback-modal-cancel">Cancel</button>
          </div>
        `;
      } else {
        // Standard feedback and feature requests
        const isFeature = currentCategory === "feature_request";
        contentHtml = `
          <button type="button" class="feedback-close-btn" id="feedback-modal-close" aria-label="Close">✕</button>
          <div class="feedback-type-tabs" role="tablist" aria-label="Feedback mode">
            <button type="button" class="feedback-type-tab active" id="feedback-tab-feedback" role="tab" aria-selected="true" tabindex="0">💡 Feedback & Features</button>
            <button type="button" class="feedback-type-tab" id="feedback-tab-improve" role="tab" aria-selected="false" tabindex="-1">🏆 Improve Orchestrator</button>
          </div>
          <h2 class="feedback-title" style="text-align: center;">${isFeature ? "What feature is missing?" : "How can we improve?"}</h2>
          <p class="feedback-subtitle" style="text-align: left;">
            ${isFeature ? "Tell us what feature you would want in Orchestrator. We prioritize our roadmap from your requests." : "Tell us what felt off or share any ideas. Your feedback gets sent directly to our team at"} <span class="feedback-email">${SUPPORT_EMAIL}</span>.
          </p>
          <div class="feedback-fields-stack">
            <label class="feedback-field">
              <span>Contact Email <span class="muted">(optional, for follow-up)</span></span>
              <input type="email" id="feedback-modal-email" class="feedback-input" value="${escapeHtml(contactEmail)}" placeholder="you@example.com (optional)">
            </label>
            <label class="feedback-field">
              <span>${isFeature ? "Feature Title" : "Topic or Feature"} <span class="muted">(optional)</span></span>
              <input type="text" id="feedback-modal-title" class="feedback-input" value="${escapeHtml(title)}" placeholder="e.g. Missing feature, planning speed, UI suggestion...">
            </label>
            <label class="feedback-field">
              <span>${isFeature ? "Describe the missing feature" : "Your Feedback or Suggestion"}</span>
              <textarea
                id="feedback-modal-text"
                class="feedback-textarea"
                placeholder="${isFeature ? "Describe what this feature would do and why you'd like it..." : "Tell us what could be better, report an issue, or share any suggestions..."}"
              >${escapeHtml(text)}</textarea>
            </label>
            ${!hasText ? '<p id="feedback-empty-hint" class="feedback-empty-hint">Write something above to send.</p>' : ""}
          </div>
          <div class="feedback-actions">
            <button
              type="button"
              class="btn primary feedback-submit-btn"
              id="feedback-modal-send"
              ${submitting || !hasText ? "disabled" : ""}
              ${hasText ? "" : 'title="Write something in the field above before sending." aria-describedby="feedback-empty-hint"'}
            >
              ${submitting ? '<span class="feedback-btn-spinner" aria-hidden="true"></span>' : '<svg class="icon" aria-hidden="true" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"></line><polygon points="22 2 15 22 11 13 2 9 22 2"></polygon></svg>'}
              <span>${submitting ? "Sending…" : "Send Feedback"}</span>
            </button>
            <button type="button" class="btn ghost" id="feedback-modal-cancel">Cancel</button>
          </div>
        `;
      }

      dlg.innerHTML = `<div class="feedback-card" id="feedback-card-container">${contentHtml}</div>`;
      bindEvents(dlg);
    }

    function bindEvents(dlg) {
      const closeBtn = dlg.querySelector("#feedback-modal-close");
      if (closeBtn) closeBtn.onclick = () => close();

      const cancelBtn = dlg.querySelector("#feedback-modal-cancel");
      if (cancelBtn) cancelBtn.onclick = () => close();

      const doneBtn = dlg.querySelector("#feedback-modal-done");
      if (doneBtn) doneBtn.onclick = () => close();

      const tabFeedback = dlg.querySelector("#feedback-tab-feedback");
      if (tabFeedback) {
        tabFeedback.onclick = () => {
          currentCategory = "general";
          currentStep = "feedback";
          render();
          const ta = dlg.querySelector("#feedback-modal-text");
          if (ta) ta.focus();
        };
      }

      const tabImprove = dlg.querySelector("#feedback-tab-improve");
      if (tabImprove) {
        tabImprove.onclick = () => {
          currentCategory = "improve_orchestrator";
          currentStep = "improve_orchestrator";
          render();
          const ta = dlg.querySelector("#feedback-modal-text");
          if (ta) ta.focus();
        };
      }

      function updateButtonState() {
        const sendBtn = dlg.querySelector("#feedback-modal-send");
        const emptyHint = dlg.querySelector("#feedback-empty-hint");
        const emailHint = dlg.querySelector("#feedback-email-hint");
        const has = text.trim().length > 0;
        const isImprove = currentStep === "improve_orchestrator" || currentCategory === "improve_orchestrator";
        const emailOk = !isImprove || (contactEmail.trim().length > 0 && contactEmail.includes("@"));
        const ready = has && emailOk && !submitting;
        if (sendBtn) {
          sendBtn.disabled = !ready;
          if (ready) {
            sendBtn.removeAttribute("title");
            sendBtn.removeAttribute("aria-describedby");
          } else if (!has) {
            sendBtn.setAttribute("title", isImprove ? "Write an explanation in the field above before submitting." : "Write something in the field above before sending.");
            sendBtn.setAttribute("aria-describedby", "feedback-empty-hint");
          } else if (!emailOk) {
            sendBtn.setAttribute("title", "Enter a valid contact email to be eligible for the prize.");
            sendBtn.setAttribute("aria-describedby", "feedback-email-hint");
          }
        }
        if (emptyHint) emptyHint.hidden = has;
        if (emailHint) emailHint.hidden = !has || emailOk;
      }

      const createJobBtn = dlg.querySelector("#feedback-create-local-job-btn");
      if (createJobBtn) {
        createJobBtn.onclick = () => {
          close();
          const summaryParam = encodeURIComponent("Improve Orchestrator: " + (title.trim() || "Core improvement"));
          const specParam = encodeURIComponent(
            "Goal: Improve Orchestrator itself.\n\n" +
            "Describe the improvement you are making to Orchestrator:\n" +
            "- What it does:\n" +
            "- Why it's valuable:\n" +
            "- Files to change:\n" +
            "- How to test:\n\n" +
            (text.trim() ? "Notes from submission draft:\n" + text.trim() + "\n" : "")
          );
          if (typeof location !== "undefined") {
            location.hash = `#/new?type=feature&summary=${summaryParam}&spec=${specParam}`;
          }
        };
      }

      const nameInput = dlg.querySelector("#feedback-modal-name");
      if (nameInput) {
        nameInput.oninput = (e) => { currentUserName = e.target.value; };
      }

      const emailInput = dlg.querySelector("#feedback-modal-email");
      if (emailInput) {
        emailInput.oninput = (e) => {
          contactEmail = e.target.value;
          updateButtonState();
        };
      }

      const titleInput = dlg.querySelector("#feedback-modal-title");
      if (titleInput) {
        titleInput.oninput = (e) => { title = e.target.value; };
      }

      const jobIdInput = dlg.querySelector("#feedback-modal-job-id");
      if (jobIdInput) {
        jobIdInput.oninput = (e) => { jobId = e.target.value; };
      }

      const retryBtn = dlg.querySelector("#feedback-modal-retry");
      if (retryBtn) {
        retryBtn.onclick = () => {
          deliveryState = "sent";
          currentStep = currentCategory === "improve_orchestrator" ? "improve_orchestrator" : "feedback";
          render();
          const ta = dlg.querySelector("#feedback-modal-text");
          if (ta) ta.focus();
        };
      }

      const betterBtn = dlg.querySelector("#feedback-btn-better");
      if (betterBtn) {
        betterBtn.onclick = () => {
          currentCategory = "general";
          currentStep = "feedback";
          render();
          const ta = dlg.querySelector("#feedback-modal-text");
          if (ta) ta.focus();
        };
      }

      const enjoyingBtn = dlg.querySelector("#feedback-btn-enjoying");
      if (enjoyingBtn) {
        enjoyingBtn.onclick = () => {
          deliveryState = "sent";
          currentStep = "thankyou";
          render();
        };
      }

      const textarea = dlg.querySelector("#feedback-modal-text");
      if (textarea) {
        textarea.oninput = (e) => {
          text = e.target.value;
          updateButtonState();
        };
      }

      const sendBtn = dlg.querySelector("#feedback-modal-send");
      if (sendBtn) {
        sendBtn.onclick = async () => {
          const isImprove = currentStep === "improve_orchestrator" || currentCategory === "improve_orchestrator";
          const emailOk = !isImprove || (contactEmail.trim().length > 0 && contactEmail.includes("@"));
          if (submitting || text.trim().length === 0 || !emailOk) return;
          submitting = true;
          render();

          let outcome = "failed";
          try {
            const report = await submitFeedback({
              userName: currentUserName,
              contactEmail: contactEmail,
              category: currentCategory,
              title: title,
              jobId: jobId,
              explanation: text,
              feedbackText: text,
              attemptNumber: currentAttempt
            });
            if (report && (report.sent || report.cloudFunction || report.firestore)) {
              outcome = "sent";
            } else if (report && report.queued) {
              outcome = "queued";
            }
          } catch (_) {
            outcome = "failed";
          }

          submitting = false;
          deliveryState = outcome;
          if (outcome === "queued") queuedOffline = !isOnline();
          currentStep = "thankyou";
          render();
        };
      }

      dlg.onclick = (e) => {
        if (e.target === dlg) close();
      };
    }

    function open({ initialStep = "feedback", category = "general", userName = "", contactEmail: initEmail = "", title: initTitle = "", jobId: initJobId = "", attemptNumber = 1 } = {}) {
      currentStep = initialStep === "improve_orchestrator" ? "improve_orchestrator" : initialStep;
      currentCategory = category === "improve_orchestrator" || initialStep === "improve_orchestrator" ? "improve_orchestrator" : (category || "general");
      text = "";
      title = initTitle || "";
      contactEmail = initEmail || (userName.includes("@") ? userName : "") || (root.state?.you?.email || "");

      let detectedJobId = initJobId || "";
      if (!detectedJobId && typeof location !== "undefined" && location.hash) {
        const jobMatch = location.hash.match(/^#\/jobs\/([a-zA-Z0-9_.-]+)/);
        if (jobMatch && jobMatch[1]) {
          detectedJobId = jobMatch[1];
        }
      }
      jobId = detectedJobId;
      submitting = false;
      deliveryState = "sent";
      queuedOffline = false;
      currentUserName = userName || (root.state?.you?.name || "");
      currentAttempt = attemptNumber;

      flushQueuedFeedback().catch(() => {});

      const dlg = ensureDialog();
      if (!dlg) return;

      previousActiveElement = document.activeElement;
      render();

      if (!dlg.open) {
        if (typeof dlg.showModal === "function") {
          dlg.showModal();
        } else {
          dlg.setAttribute("open", "");
        }
      }

      const ta = dlg.querySelector("#feedback-modal-text");
      if (ta) {
        ta.focus();
      }
    }

    function close() {
      if (dialogElem) {
        if (typeof dialogElem.close === "function" && dialogElem.open) {
          dialogElem.close();
        } else {
          dialogElem.removeAttribute("open");
        }
      }
      if (previousActiveElement && typeof previousActiveElement.focus === "function") {
        previousActiveElement.focus();
      }
    }

    return {
      open,
      close,
      render,
      getStep: () => currentStep,
      getCategory: () => currentCategory,
      getText: () => text,
      setText: (val) => { text = val; },
      setStep: (s) => { currentStep = s; },
      setCategory: (c) => { currentCategory = c; }
    };
  }

  if (typeof window !== "undefined" && typeof window.addEventListener === "function") {
    window.addEventListener("online", () => {
      flushQueuedFeedback().catch(() => {});
    });
  }

  const modal = createModalController();

  const api = {
    SUPPORT_EMAIL,
    FEEDBACK_SUBJECT_PREFIX,
    submitFeedback,
    flushQueuedFeedback,
    modal,
    open: (opts) => modal.open(opts),
    close: () => modal.close()
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
  root.Feedback = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
