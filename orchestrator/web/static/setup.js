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
    return {
      title: inspection ? "Set up your project" : "What do you want to build?",
      sub: inspection ? "Review its settings, then apply them." : "Start from your idea, or from code you already have.", actions: "",
      html: `${inspection ? "" : `<section class="card desktop-idea">
        <form id="desktop-idea"><label>What do you want to build?<input name="pitch" required maxlength="300" autocomplete="off"
          placeholder="e.g. A turn-based word game to play with friends"></label>
          <button class="btn primary" type="submit">Start</button></form>
        <p class="muted">It becomes the start of your product's plan. You can change everything later.</p>
      </section>`}
      <section class="card desktop-setup">
        <p>${inspection ? "" : "<strong>Already have code?</strong> "}Choose its folder here, or from the Orchestrator menu on your Mac.</p>
        <form id="desktop-folder"><label>Project folder<input name="root" value="${esc(selected)}" required placeholder="/Users/you/Projects/MyApp"></label>
          <button class="btn" type="submit">Review folder</button></form>
        ${inspection ? `<form id="desktop-apply">
          <p>${esc(inspection.stack)} · Settings are saved only when you press Apply.</p>
          ${fields.map(([key,label]) => `<label>${label}<input name="${key}" value="${esc(values[key] || "")}" ${key === "project_name" ? "required" : ""}></label>`).join("")}
          <label>AI models (comma-separated)<input name="models" value="codex" required></label>
          <p>AI tools, GitHub and project build tools have their own installation and sign-in requirements. You can review these in Setup after applying.</p>
          <p>Next, follow the setup checklist to connect an AI provider and check the tools this project needs. Saving this form does not mean those tools are ready yet.</p>
          <button class="btn primary" type="submit">Save and continue setup</button>
        </form>` : ""}
        <div id="desktop-setup-error" class="notice bad" role="alert" hidden></div>
      </section>`,
      after() {
        document.querySelector("#desktop-idea")?.addEventListener("submit", (event) => {
          event.preventDefault();
          const pitch = String(new FormData(event.target).get("pitch") || "").trim();
          if (pitch) location.hash = Account.ideaRoute({pitch});
        });
        const error = document.querySelector("#desktop-setup-error");
        const showError = (message) => { error.hidden = false; error.textContent = message; };
        document.querySelector("#desktop-folder").addEventListener("submit", async (event) => {
          event.preventDefault();
          selected = new FormData(event.target).get("root");
          try {
            await api("setup/inspect", {method: "POST", body: {root: selected}});
            location.hash = `#/setup?root=${encodeURIComponent(selected)}`;
          } catch (e) { showError(e.message); }
        });
        document.querySelector("#desktop-apply")?.addEventListener("submit", async (event) => {
          event.preventDefault();
          const button = event.target.querySelector("button");
          button.disabled = true;
          try {
            const form = new FormData(event.target);
            const settings = Object.fromEntries(fields.map(([key]) => [key, form.get(key) || null]));
            settings.models = String(form.get("models")).split(",").map(v => v.trim()).filter(Boolean);
            const result = await api("setup/apply", {method: "POST", body: {root: selected, values: settings}});
            if (!result.ok) { showError(result.errors.join("\n")); return; }
            await onReady();
          } catch (e) { showError(e.message); }
          finally { button.disabled = false; }
        });
      }
    };
  }
};
