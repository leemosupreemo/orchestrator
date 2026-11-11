(function (root) {
  "use strict";

  function addRequest(project) {
    return {
      path: "projects/add",
      options: {
        method: "POST",
        body: { root: project.root, name: project.name || "", active: false },
      },
    };
  }

  function availableProjects(discovered, tracked) {
    const trackedRoots = new Set((tracked || []).map((project) => project.root));
    return (discovered || []).filter((project) => !trackedRoots.has(project.root));
  }

  const escapeHtml = root.Html.escape;

  // Several folders of one project (its worktrees, or copies) all carry the project's name. Those are told apart by folder:
  // the folder is the title, and the row says it's a worktree or which project it is.
  function renderRows(projects) {
    const named = {};
    for (const p of projects) named[p.name] = (named[p.name] || 0) + 1;
    return projects.map((project) => {
      const kind = project.configured ? "Orchestrated" : (project.type === "swift" ? "Xcode/Swift" : "Git");
      const folder = project.folder || String(project.root || "").split("/").pop();
      const byFolder = Boolean(project.worktree_of) || (named[project.name] > 1 && folder && folder !== project.name);
      const main = String(project.worktree_of || "").split("/").pop();
      const note = project.worktree_of ? `Worktree of ${main}` : byFolder ? project.name : "";
      return `
        <div class="project-discovery-row">
          <div class="project-discovery-info">
            <div class="project-discovery-title">${escapeHtml(byFolder ? folder : project.name)}${note ? ` <span class="muted project-discovery-note">· ${escapeHtml(note)}</span>` : ""}</div>
            <div class="project-discovery-path" title="${escapeHtml(project.root)}">${escapeHtml(project.root)}</div>
          </div>
          ${project.worktree_of ? `<span class="pill-badge">Worktree</span>` : ""}
          <span class="pill-badge ${project.configured ? "configured-badge" : ""}">${kind}</span>
          <button type="button" class="btn small primary" data-add-project-path="${escapeHtml(project.root)}" data-project-name="${escapeHtml(project.name)}">Add</button>
        </div>`;
    }).join("");
  }

  function createAddQueue(send) {
    let tail = Promise.resolve();
    return (request) => {
      const result = tail.then(() => send(request));
      tail = result.catch(() => {});
      return result;
    };
  }

  root.ProjectPicker = { addRequest, availableProjects, renderRows, createAddQueue };
})(globalThis);
