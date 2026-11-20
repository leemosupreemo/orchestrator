# Project download Home implementation plan

**Goal:** Selecting a GitHub-only project immediately opens its Home view with download status and an indeterminate loading bar, retained through activation and initial page loading.

**Architecture:** A project-picker controller owns download phases and serializes project activation. The Home renderer shows the selected download without old-project actions/data, and clears it only after the real project's Home data or setup page is ready. Native project options use Installed locally then Download. Existing background tasks report clone stages; the download watcher allows the server's 15-minute clone window.

**Constraints:** Preserve the in-progress branch dropdown work. No fake percentages. Register background-completed downloads without changing the current project. A newer project/computer selection wins over late responses. Failed downloads stay on the selected project's error view and can retry without duplicating an active clone task.

- [x] Test pending selection, phases, activation races, errors/retries, backend changes and accessible progress markup with controlled promises.
- [x] Implement controller and connect it to the picker and Home lifecycle.
- [x] Add clone stage reporting and bounded task-watcher options.
- [x] Validate project, Home, Git and design-convention tests plus syntax/compilation/diff checks.
- [x] Review the combined change and fix findings.

**Review rulings:** Keep clone jobs by backend/repository independently of per-selection state, so returning to an in-flight download reuses it without reviving stale Home requests. Ignore stale activation and route errors. A failed selection rerenders the current page; an orphaned progress element cannot stop refresh. Hide prior-project setup actions while loading.

**Evidence:** Final 249-test focused run passes; an earlier broader run including plain-Git pipelines passed 262 tests. JavaScript syntax, Python compilation and diff checks pass. Final independent review reports no remaining actionable findings.
