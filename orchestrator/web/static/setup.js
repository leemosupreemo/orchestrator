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
      title: "Set up your project", sub: "Choose a folder, review its settings, then apply them.", actions: "",
      html: `<section class="card desktop-setup">
        <p>You can also choose a folder from the Orchestrator menu on your Mac.</p>
        <form id="desktop-folder"><label>Project folder<input name="root" value="${esc(selected)}" required placeholder="/Users/you/Projects/MyApp"></label>
          <button class="btn" type="submit">Review folder</button></form>
        <p><a href="#/new-project">Create a new project instead</a></p>
        ${inspection ? `<form id="desktop-apply">
          <p>${esc(inspection.stack)} · Settings are saved only when you press Apply.</p>
          ${fields.map(([key,label]) => `<label>${label}<input name="${key}" value="${esc(values[key] || "")}" ${key === "project_name" ? "required" : ""}></label>`).join("")}
          <label>AI models (comma-separated)<input name="models" value="codex" required></label>
          <p>AI tools, GitHub and project build tools have their own installation and sign-in requirements. You can review these in Setup after applying.</p>
          <button class="btn primary" type="submit">Apply and open project</button>
        </form>` : ""}
        <div id="desktop-setup-error" class="notice bad" role="alert" hidden></div>
      </section>`,
      after() {
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
