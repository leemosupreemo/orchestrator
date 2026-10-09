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
    feedbackText = "",
    platform = "Web",
    appVersion = "1.0.0",
    attemptNumber = 1,
    apiClient = null
  } = {}) {
    const cleanText = String(feedbackText || "").trim();
    if (!cleanText) {
      throw new Error("Feedback text is required");
    }
    const cleanName = String(playerName || userName || "").trim() || "User";
    const payload = {
      userName: cleanName,
      playerName: cleanName,
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

  function createModalController() {
    let currentStep = "feedback"; // 'prompt' | 'feedback' | 'thankyou'
    let text = "";
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
      } else if (currentStep === "feedback") {
        contentHtml = `
          <button type="button" class="feedback-close-btn" id="feedback-modal-close" aria-label="Close">✕</button>
          <h2 class="feedback-title" style="text-align: center;">How can we improve?</h2>
          <p class="feedback-subtitle" style="text-align: left;">
            Tell us what felt off or share any ideas. Your feedback gets sent directly to our team at <span class="feedback-email">${SUPPORT_EMAIL}</span>.
          </p>
          <textarea
            id="feedback-modal-text"
            class="feedback-textarea"
            placeholder="Tell us what could be better, report an issue, or share any suggestions..."
          >${escapeHtml(text)}</textarea>
          ${!hasText ? '<p id="feedback-empty-hint" class="feedback-empty-hint">Write something above to send.</p>' : ""}
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
      } else if (currentStep === "thankyou") {
        const isFailed = deliveryState === "failed";
        const isQueued = deliveryState === "queued";
        const icon = isFailed ? "📡" : (isQueued ? "📥" : "💌");
        const title = isFailed ? "Couldn't Send" : "Thank You!";
        let message = "We will review your message as soon as we can.";
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
            <h2 class="feedback-status-title ${isFailed ? "error" : ""}">${title}</h2>
            <p class="feedback-status-msg">${message}</p>
            <div class="feedback-status-actions">
              ${isFailed ? '<button type="button" class="btn primary" id="feedback-modal-retry">Try Again</button>' : ""}
              <button type="button" class="btn ${isFailed ? "ghost" : "primary"}" id="feedback-modal-done">${isFailed ? "Not Now" : "Done"}</button>
            </div>
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

      const retryBtn = dlg.querySelector("#feedback-modal-retry");
      if (retryBtn) {
        retryBtn.onclick = () => {
          deliveryState = "sent";
          currentStep = "feedback";
          render();
          const ta = dlg.querySelector("#feedback-modal-text");
          if (ta) ta.focus();
        };
      }

      const betterBtn = dlg.querySelector("#feedback-btn-better");
      if (betterBtn) {
        betterBtn.onclick = () => {
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
          const sendBtn = dlg.querySelector("#feedback-modal-send");
          const hint = dlg.querySelector("#feedback-empty-hint");
          const has = text.trim().length > 0;
          if (sendBtn) {
            sendBtn.disabled = submitting || !has;
            if (has) {
              sendBtn.removeAttribute("title");
              sendBtn.removeAttribute("aria-describedby");
            } else {
              sendBtn.setAttribute("title", "Write something in the field above before sending.");
              sendBtn.setAttribute("aria-describedby", "feedback-empty-hint");
            }
          }
          if (hint) {
            hint.hidden = has;
          }
        };
      }

      const sendBtn = dlg.querySelector("#feedback-modal-send");
      if (sendBtn) {
        sendBtn.onclick = async () => {
          if (submitting || text.trim().length === 0) return;
          submitting = true;
          render();

          let outcome = "failed";
          try {
            const report = await submitFeedback({
              userName: currentUserName,
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

    function open({ initialStep = "feedback", userName = "", attemptNumber = 1 } = {}) {
      currentStep = initialStep;
      text = "";
      submitting = false;
      deliveryState = "sent";
      queuedOffline = false;
      currentUserName = userName;
      currentAttempt = attemptNumber;

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
      getText: () => text,
      setText: (val) => { text = val; },
      setStep: (s) => { currentStep = s; }
    };
  }

  const modal = createModalController();

  const api = {
    SUPPORT_EMAIL,
    FEEDBACK_SUBJECT_PREFIX,
    submitFeedback,
    modal,
    open: (opts) => modal.open(opts),
    close: () => modal.close()
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
  root.Feedback = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
