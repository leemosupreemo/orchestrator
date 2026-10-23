"use strict";

// Native onboarding hands a selected folder to this explicit review/apply form.
window.DesktopSetup = {
  async render({api, esc, root, onReady}) {
    const status = await api("bootstrap");
    let selected = root || status.selected_root || "";
    let inspection = selected ? await api("setup/inspect", {method: "POST", body: {root: selected}}) : null;
    const fields = [
      ["project_name", "Project name"], ["base_branch", "Base branch"],
      ["build_command", "Build command"], ["test_command", "Test command"],
      ["xcode_project", "Xcode project (optional)"], ["xcode_workspace", "Xcode workspace (optional)"],
      ["scheme", "Xcode scheme (optional)"], ["test_target", "Xcode test target (optional)"],
    ];
    const values = inspection?.inferred || {};
    const needsCommands = !values.build_command && !values.xcode_project && !values.xcode_workspace;
    const needsTests = !values.test_command && (!values.xcode_project || !values.test_target);
    const needsAttention = Boolean(needsCommands || needsTests);
    return {
      title: inspection ? "Set up your project" : "What do you want to build?",
      sub: inspection ? "Review your project settings and continue setup." : "Start from an idea, or from code you already have.",
      actions: "",
      html: `
        <div id="desktop-setup-error" class="notice bad mb-16" role="alert" hidden tabindex="-1"></div>
        ${inspection ? "" : `
        <section class="card mb-16 desktop-idea">
          <div class="card-h">
            <h2>Start with an idea</h2>
          </div>
          <div class="card-b stack">
            <p class="muted text-sm">Describe what you want to build. You can change everything later.</p>
            <form id="desktop-idea" class="stack gap-12">
              <label class="field">
                <span>What do you want to build?</span>
                <input type="text" name="pitch" required maxlength="300" autocomplete="off" placeholder="e.g. A turn-based word game to play with friends">
              </label>
              <div>
                <button class="btn primary" type="submit">Start</button>
              </div>
            </form>
          </div>
        </section>`}
        <section class="card mb-16 desktop-setup">
          <div class="card-h">
            <h2>${inspection ? "Project settings" : "Existing project"}</h2>
            ${inspection?.stack ? `<span class="pill done">${esc(inspection.stack)}</span>` : ""}
          </div>
          <div class="card-b stack">
            ${inspection ? `
              <div class="setup-detected-summary stack gap-8">
                <div class="row items-center justify-between wrap gap-8">
                  <div>
                    <strong>${esc(values.project_name || "Project")}</strong>
                    <span class="muted text-sm mono ml-8">${esc(selected)}</span>
                  </div>
                  <a class="btn small ghost" href="#/setup">Change folder</a>
                </div>
                <p class="muted text-sm">Automated configuration detected for your project. Review or refine settings below.</p>
              </div>
              <form id="desktop-folder" hidden><input type="hidden" name="root" value="${esc(selected)}"></form>
              <form id="desktop-apply" class="stack gap-16">
                <label class="field">
                  <span>Project name</span>
                  <input type="text" name="project_name" value="${esc(values.project_name || "")}" required placeholder="My Project">
                </label>
                <details class="fold advanced-settings" ${needsAttention ? "open" : ""}>
                  <summary class="card-h">
                    <span>Build, test &amp; branch settings</span>
                    <span class="muted text-xs">${needsAttention ? "Configure required commands" : `Automated (${esc(inspection.stack || "detected")})`}</span>
                  </summary>
                  <div class="card-b stack gap-12">
                    <label class="field">
                      <span>Base branch</span>
                      <input type="text" name="base_branch" value="${esc(values.base_branch || "main")}">
                    </label>
                    <label class="field">
                      <span>Build command</span>
                      <input type="text" name="build_command" class="mono text-sm" value="${esc(values.build_command || "")}" placeholder="e.g. swift build or npm run build">
                    </label>
                    <label class="field">
                      <span>Test command</span>
                      <input type="text" name="test_command" class="mono text-sm" value="${esc(values.test_command || "")}" placeholder="e.g. swift test or pytest">
                    </label>
                    <div class="row gap-12 wrap">
                      <label class="field flex-1">
                        <span>Xcode project (optional)</span>
                        <input type="text" name="xcode_project" value="${esc(values.xcode_project || "")}">
                      </label>
                      <label class="field flex-1">
                        <span>Xcode workspace (optional)</span>
                        <input type="text" name="xcode_workspace" value="${esc(values.xcode_workspace || "")}">
                      </label>
                    </div>
                    <div class="row gap-12 wrap">
                      <label class="field flex-1">
                        <span>Xcode scheme (optional)</span>
                        <input type="text" name="scheme" value="${esc(values.scheme || "")}">
                      </label>
                      <label class="field flex-1">
                        <span>Xcode test target (optional)</span>
                        <input type="text" name="test_target" value="${esc(values.test_target || "")}">
                      </label>
                    </div>
                    <input type="hidden" name="models" value="codex">
                  </div>
                </details>
                <div class="setup-actions row items-center gap-12 wrap mt-8">
                  <button class="btn primary" type="submit">Save and continue setup</button>
                  <span class="muted text-sm">Next, connect an AI provider and check tools.</span>
                </div>
              </form>
            ` : `
              <p class="muted text-sm">Choose a project folder on your computer to inspect and set up.</p>
              <form id="desktop-folder" class="stack gap-12">
                <label class="field">
                  <span>Project folder</span>
                  <div class="row gap-8 wrap">
                    <input type="text" name="root" class="mono text-sm flex-1" value="${esc(selected)}" required placeholder="/Users/you/Projects/MyApp">
                    <button class="btn" type="submit">Review folder</button>
                  </div>
                </label>
              </form>
            `}
          </div>
        </section>`,
      after() {
        const error = document.querySelector("#desktop-setup-error");
        const showError = (message) => {
          if (!error) return;
          const details = document.querySelector(".advanced-settings");
          if (details) details.open = true;
          error.hidden = false;
          error.textContent = message;
          if (typeof window !== "undefined" && typeof window.scrollTo === "function") {
            window.scrollTo({ top: 0, behavior: "smooth" });
          }
          error.scrollIntoView?.({ behavior: "smooth", block: "start" });
          error.focus?.({ preventScroll: true });
        };
        const clearError = () => {
          if (!error) return;
          error.hidden = true;
          error.textContent = "";
        };

        document.querySelector("#desktop-idea")?.addEventListener("submit", (event) => {
          event.preventDefault();
          clearError();
          const pitch = String(new FormData(event.target).get("pitch") || "").trim();
          if (pitch) location.hash = Account.ideaRoute({pitch});
        });

        document.querySelector("#desktop-folder")?.addEventListener("submit", async (event) => {
          event.preventDefault();
          clearError();
          selected = new FormData(event.target).get("root");
          try {
            await api("setup/inspect", {method: "POST", body: {root: selected}});
            location.hash = `#/setup?root=${encodeURIComponent(selected)}`;
          } catch (e) { showError(e.message); }
        });

        document.querySelector("#desktop-apply")?.addEventListener("submit", async (event) => {
          event.preventDefault();
          clearError();
          const form = new FormData(event.target);
          const bCmd = String(form.get("build_command") || "").trim();
          const tCmd = String(form.get("test_command") || "").trim();
          const xProj = String(form.get("xcode_project") || "").trim();
          const xWs = String(form.get("xcode_workspace") || "").trim();
          if (!xProj && !xWs && !bCmd) {
            showError("Please specify a Build command (e.g. swift build or npm run build) or Xcode project.");
            event.target.querySelector("[name='build_command']")?.focus();
            return;
          }
          if (!tCmd && (!xProj || !String(form.get("test_target") || "").trim())) {
            showError("Please specify a Test command (e.g. swift test or npm test).");
            event.target.querySelector("[name='test_command']")?.focus();
            return;
          }
          const button = event.target.querySelector("button[type='submit']");
          if (button) button.disabled = true;
          try {
            const settings = Object.fromEntries(fields.map(([key]) => [key, form.get(key) || null]));
            settings.models = String(form.get("models") || "codex").split(",").map(v => v.trim()).filter(Boolean);
            const result = await api("setup/apply", {method: "POST", body: {root: selected, values: settings}});
            if (!result.ok) { showError(result.errors.join("\n")); return; }
            await onReady();
          } catch (e) { showError(e.message); }
          finally { if (button) button.disabled = false; }
        });
      }
    };
  }
};
