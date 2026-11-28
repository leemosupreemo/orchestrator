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

  function createDownloads(deps) {
    let active = null, version = 0;
    const jobs = new Map();
    const valid = choice => choice.version === version && choice.backend === deps.backend();
    const selected = op => active === op && valid(op);
    const queue = createAddQueue(({project, choice}) => valid(choice) ? deps.activate(project) : null);
    const register = createAddQueue(({project, backend}) => {
      if (backend !== deps.backend()) throw new DOMException('Aborted', 'AbortError');
      return deps.register(project);
    });
    const pending = () => {
      if (active && active.backend !== deps.backend()) { active = null; version++; }
      return active;
    };
    const leave = () => { active = null; version++; };
    const fail = async (op, error) => {
      if (!selected(op)) return false;
      op.phase = 'error'; op.error = error.message || 'The project could not be loaded.';
      await deps.change(op, false);
      return true;
    };
    const obtain = job => {
      if (job.registered) return Promise.resolve(job.project);
      if (job.running) return job.promise;
      job.running = true;
      job.promise = (async () => {
        try {
          if (job.backend !== deps.backend()) throw new DOMException('Aborted', 'AbortError');
          if (!job.project) {
            if (!job.task) job.task = (await deps.start(job.repo)).task;
            if (job.backend !== deps.backend()) throw new DOMException('Aborted', 'AbortError');
            job.project = await deps.wait({task:job.task}, {timeoutMs:16 * 60 * 1000,
              isCurrent:() => job.backend === deps.backend(),
              onProgress: progress => {
                const shown = active;
                if (!shown || shown.job !== job || !selected(shown) || !progress.stage
                    || shown.stage === progress.stage && shown.detail === progress.detail) return;
                shown.stage = progress.stage; shown.detail = progress.detail || '';
                deps.change(shown, false);
              }});
          }
          if (job.backend !== deps.backend()) throw new DOMException('Aborted', 'AbortError');
          await register({project:job.project, backend:job.backend});
          job.registered = true;
          return job.project;
        } catch (error) {
          if (error.taskFailed || error.status === 404) job.task = null;
          throw error;
        } finally { job.running = false; }
      })();
      return job.promise;
    };
    const download = (repo, resume = null) => {
      const previous = pending();
      if (previous?.repo === repo && previous.phase !== 'error') return previous.promise;
      const backend = deps.backend(), key = JSON.stringify([backend, repo.toLowerCase()]);
      let job = jobs.get(key);
      if (!job) {
        job = {repo, backend, project:resume?.project || null, task:resume?.task || null};
        jobs.set(key, job);
      }
      const op = {repo, name:repo.split('/').pop(), backend, version:++version, phase:'downloading', job};
      active = op;
      op.promise = (async () => {
        try {
          await deps.change(op, true); // Render the selected project's Home before starting the request.
          op.project = await obtain(job);
          op.name = op.project.name || op.name;
          if (selected(op)) { op.phase = 'preparing'; await deps.change(op, false); }
          if (!selected(op)) return;
          await queue({project:op.project, choice:op});
          if (!selected(op)) return;
          await deps.ready(op, () => selected(op));
          if (!selected(op)) return;
          op.phase = 'loading';
          await deps.change(op, false);
          return op.project; // Home clears the loading state only after its own data is ready.
        } catch (error) { await fail(op, error); }
      })();
      return op.promise;
    };
    return {pending, download, leave, fail,
      complete: op => {
        if (!selected(op) || op.phase !== 'loading') return false;
        active = null;
        return true;
      },
      select: async root => {
        leave();
        const choice = {version, backend:deps.backend()};
        try {
          const response = await queue({project:{root}, choice});
          return valid(choice) && response ? {response, isCurrent:() => valid(choice)} : null;
        } catch (error) {
          if (valid(choice)) throw error;
          return null;
        }
      },
    };
  }

  function downloadStatus(op) {
    const failed = op.phase === 'error';
    const stage = failed ? 'Project could not be loaded' : op.phase === 'loading' ? 'Loading project'
      : op.phase === 'preparing' ? 'Opening project' : op.stage || 'Downloading project';
    const detail = failed ? op.error : op.phase === 'loading' ? 'Loading your project information…' : op.detail || op.repo;
    return {failed, stage, detail};
  }

  function downloadMarkup(op) {
    const {failed, stage, detail} = downloadStatus(op);
    return `<section class="banner run-status ${failed ? 'failed' : 'working'}" data-project-download
        role="${failed ? 'alert' : 'status'}" aria-busy="${!failed}">
      <strong data-project-download-stage>${escapeHtml(stage)}</strong>
      <p class="t-secondary" data-project-download-detail>${escapeHtml(detail || '')}</p>
      ${failed ? '<button type="button" class="btn primary" data-retry-project-download>Try again</button>'
        : `<progress class="run-status-progress" aria-label="Loading ${escapeHtml(op.name)}"></progress>`}
    </section>`;
  }

  root.ProjectPicker = { addRequest, availableProjects, renderRows, createAddQueue, createDownloads, downloadStatus, downloadMarkup };
})(globalThis);
