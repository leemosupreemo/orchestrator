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

  function renderRows(projects) {
    return projects.map((project) => {
      const kind = project.configured ? "Orchestrated" : (project.type === "swift" ? "Xcode/Swift" : "Git");
      return `
        <div class="project-discovery-row">
          <div class="project-discovery-info">
            <div class="project-discovery-title">${escapeHtml(project.name)}</div>
            <div class="project-discovery-path" title="${escapeHtml(project.root)}">${escapeHtml(project.root)}</div>
          </div>
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
