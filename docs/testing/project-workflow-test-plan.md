# Multi-project workflow test plan

Status: proposed test program; this document does not claim these scenarios are implemented or passing.
Date: 2026-11-15.

## Objective and approach

Prove that Orchestrator can take different kinds of projects from discovery and setup through planning, implementation, real verification, documentation, review, and delivery. Test what happens when prerequisites are absent and when a stage fails, including whether the user can recover through the app.

The primary unit of acceptance is a **project journey with observable results**, not a successful API response or the presence of expected strings in JavaScript.

Three approaches were considered:

- Extend only the existing unit tests: fast and useful for isolated rules, but insufficient for cross-stage failures and browser runtime errors.
- Execute every combination with real AI and services: expensive, slow, difficult to reproduce, and still limited by available devices and accounts.
- Recommended: deterministic project journeys on every change, real-toolchain and browser matrices nightly, and controlled live integrations before releases. Use systematic combinations plus mandatory high-risk scenarios.

The fixture counts, schedules, and performance targets below are proposals. Calibrate them against the first recorded baseline; do not present planned coverage as measured coverage.

## Existing evidence and limits

These are inspected starting points, not an exhaustive coverage audit:

| Source | What it provides | What this plan adds |
| --- | --- | --- |
| [Current test catalog](../test-matrix.md) | Feature, UI, security, and recovery scenarios | A reproducible project-by-workflow matrix with evidence and release gates |
| [Canonical commands](../build-test-commands.md) | Package, unittest, native, authentication, and live-model commands | Scheduling and environment requirements for expanded coverage |
| [E2E harness](../../tests/e2e/harness.py) | A small Python word-scoring project, local Git remote, fake GitHub and model tools | Multiple real project fixtures and explicit scenario manifests |
| [Offline pipeline](../../tests/e2e/test_pipeline_offline.py) | Planning, building, verification, review, plain Git, and model behavior checks | Equivalent journeys across toolchains and failure combinations |
| [Fresh install](../../tests/e2e/test_fresh_install.py) | First-run setup and job lifecycle through HTTP payloads used by the UI | Actual browser clicks, page rendering, navigation, and recovery |
| [Stack detection](../../orchestrator/stack_detection.py) | Unity, Xcode, SPM, Cargo, Gradle, Python, Node, Go, and fallback paths | Verify detected commands against native tools and ambiguous repositories |
| [Platform analysis](../../orchestrator/platforms.py) | Client/server/shared/tooling classification and test references | Check mixed projects without confusing references with executed coverage |
| [Delivery adapters](../../orchestrator/delivery_adapters.py) | iOS adapter identity, Android/file distribution, web previews | End-to-end build provenance, upload failures, receipts, and installation |

Language recognition, automatic stack setup, successful native execution, and supported distribution are separate capabilities. A C# or GDScript file being recognized does not establish an automatic build or delivery workflow. Record each capability separately.

Recent incidents become permanent priority scenarios: Unity shell expansion corrupted while adding a simulator destination; job-page rendering reading `problem` before initialization. The latter has a targeted regression test; both require broader journey coverage.

## 1. Project fixture portfolio

Every fixture must be small enough to understand and contain a known behavior, an independent acceptance check, a known defect variant, and a deliberately failing-test variant. Pin dependencies/toolchains and record their versions. Keep native artifacts out of Git; build them in isolated temporary directories.

“Target” below describes the test fixture, not a new product support promise. Generic fixtures exercise explicit configured commands and honest unsupported states where autodetection is absent.

| ID | Project fixture | Language / tools | Shape and complexity | Delivery or terminal outcome |
| --- | --- | --- | --- | --- |
| P01 | Small library/CLI | Python / unittest | Single component, small | Verified local completion |
| P02 | HTTP API with persistence | Python / pytest | Server + database, medium | Service health and persistence checks; custom deployment only if configured |
| P03 | Browser client | JavaScript / npm | Client, small | Web preview loads and works |
| P04 | Typed browser client | TypeScript / pnpm | Client, medium | Web preview and browser interaction |
| P05 | HTTP service | TypeScript / Node | Server, medium | API contract and lifecycle checks |
| P06 | Full-stack workspace | TypeScript client + Python server | Client/server/shared contract, large | Both services start; browser operation persists through API |
| P07 | Swift library | Swift / SPM | Shared library, small | Native build and real tests |
| P08 | Native iOS app | Swift / Xcode | Client, medium | Simulator build/install/launch; signed device delivery in separate lane |
| P09 | Native macOS app | Swift / Xcode or SPM | Desktop client, medium | App launch and supported package smoke check |
| P10 | Android app | Kotlin / Gradle | Client, medium | Emulator run; APK/AAB delivery |
| P11 | JVM server | Java / Gradle | Server, medium | Server integration tests; no Android flags |
| P12 | Unity game | C# / Unity | Client, medium | EditMode/PlayMode; configured platform export |
| P13 | Unity + game server | C# Unity client + C# server | Client/server/shared protocol, large | Separately configured builds and protocol integration |
| P14 | HTTP service/library | Go | Server/shared, medium | Native tests and HTTP smoke check |
| P15 | CLI/library workspace | Rust / Cargo | Tooling/shared, medium | Native build, tests, CLI behavior |
| P16 | Explicit-command project | C/C++ / CMake | Generic fallback, medium | Documented/configured commands or actionable unsupported state |
| P17 | Ambiguous monorepo | Unity subfolder + Node tooling + server | Multiple manifests and nested Git, large | Correct roots, toolchains, component isolation |
| P18 | Empty folder / idea-only project | No language selected initially | New project | PRD/architecture/setup journey; no fabricated successful build |
| P19 | Legacy repository | Python/JS mix, weak docs | Large, existing failures, generated files | Baseline failures retained and changed behavior verified |
| P20 | Unknown engine/project | GDScript or unknown manifest | Generic fallback, small | Correct capability explanation and explicit-command setup |

Use one shared fixture factory contract, proposed as `create_project(spec, root) -> Fixture`, while retaining the existing `DummyProject` callers. A fixture exposes root, tool requirements, expected detected capabilities, build/test commands, acceptance oracle, artifact expectations, and teardown. Do not copy the Python-only assumptions into every fixture.

Complexity should mean dependencies and coordination, not just line count:

- Small: one deployable/component, no external service, one test suite.
- Medium: multiple modules or a persistence/service dependency, unit and integration suites.
- Large: multiple deployables/toolchains, shared contracts, nested repositories or workspaces, concurrent jobs.
- Scale variants: generated 1k, 10k, and 20k+ source-file trees, large logs and job histories; exercise classification limits, explicit truncation, responsiveness, and memory use. Do not send synthetic scale trees to paid AI by default.

## 2. Combination coverage

| Dimension | Required values |
| --- | --- |
| Project starting point | Empty folder; existing code; existing Orchestrator config; migrated config |
| Requirements | Complete PRD; absent; partial; stale/contradictory; custom location; PRD-only with no code |
| Architecture | Absent; proposed; approved; contradicts repository/toolchain |
| Tests | Absent; passing; genuine product failure; invalid test; missing runner; zero tests discovered; flaky; partly skipped |
| AI readiness | None installed; installed but signed out; ready; expires mid-run; rate limited; offline; context exhausted |
| AI response | Correct change; no change; malformed result; partial edits; tool denied; wrong scope; repeated unsuccessful repair |
| Git | No repository; no commits; local only; remote configured; auth failure; dirty; detached HEAD; conflict/diverged; protected branch |
| Work placement | Current branch; job branch; existing worktree; nested repository; two jobs editing overlapping files |
| Distribution | Off; incomplete setup; ready; missing build artifact; stale artifact; upload failure; success without receipt |
| Environment | Clean install; upgrade; missing tool; incompatible version; offline dependency cache; port conflict; low disk; process interruption |
| Interface | CLI; desktop-hosted web UI; desktop browser; narrow mobile browser; reconnect after backgrounding |
| Path/config | Spaces; Unicode; shell metacharacters; relative root; nested project; environment defaults; custom commands |

Do not multiply all columns blindly. Generate a reproducible constrained pairwise covering set over valid values. Store the seed, constraints, selected rows, and report of uncovered feasible pairs. Partition by toolchain availability; unavailable runners remain visibly unexecuted.

Each fixture runs the core journey twice: once with a PRD and once without one (40 baseline journeys). For P18, “without” starts at idea intake; “with” begins with supplied requirements. Equivalent unsupported outcomes must have explicit capability assertions, never a blanket skip. These 40 journeys are a starting target, not the complete suite.

Additionally require these triples/compound scenarios regardless of pairwise selection:

1. Unity + iOS simulator workflow + `UNITY_PATH` set/unset and a path containing spaces.
2. Unity client + independently built server + root-level Node tooling.
3. Missing PRD + existing code + no AI connection.
4. Stale PRD + approved architecture + contradictory detected stack.
5. Failing tests + expired AI sign-in + no active job run; job page must still render.
6. Tests generated by AI + zero discovery/unregistered target + apparent runner success.
7. Dirty working tree + branch switch + concurrent job.
8. Nested Git repository + shared-code change + client/server verification.
9. Passing client tests + failing server tests + attempted finalization.
10. Build failure + preexisting stale artifact + automatic delivery enabled.
11. Successful upload response + absent receipt + retry/reconnect.
12. Worker killed after edits/commit/upload + restart + resume.
13. Project switch during an in-flight API request + active polling + visible job detail.
14. Cold dependency cache + network failure + first-run setup.
15. No tests + unknown stack + generic command setup.

## 3. Core journey and stage assertions

Run the same contract for each applicable project. Assert state, filesystem, process behavior, and visible UI; return codes alone are insufficient.

| Stage | Actions | Required evidence |
| --- | --- | --- |
| Discover | Import/select project; inspect detected settings | Right root/language/toolchain/components; ambiguous choices visible; generated folders excluded |
| Setup | Configure commands, AI, environment, Git | Config validates; missing prerequisites explain the next action; repeated setup is idempotent |
| Requirements | Read/import/create PRD; resolve questions | Existing requirements preserved; missing information represented honestly; no silent approval |
| Plan | Request feature/bug/test-only task; revise and approve | Bounded tasks and acceptance criteria; correct PRD/component context; approval gates respected |
| Build | Execute changes | Expected behavior changes; unrelated files and other components preserved; no-op cannot claim a completed build |
| Verify | Run build, discovery, unit/integration/browser checks | Intended tests actually execute; failures/skips distinguishable; independent acceptance oracle passes |
| AI repair | Diagnose, propose scoped change, rerun | Correct classification; repair changes intended files; original defect eliminated; attempts bounded and visible |
| Document | Update affected docs and job record | Actual changed behavior described; unsupported “verified” claims absent |
| Finalize | Review, commit, PR/merge or local completion | Correct base/head; tests correspond to landed revision; no duplicate PR; plain Git avoids GitHub-only actions |
| Deliver | Build/package/send through configured path | Artifact from intended revision/configuration, checksum, destination, receipt; preview/install smoke check |
| Recover | Refresh, restart, pause/resume, retry, switch project | No lost work, duplicate side effects, cross-project state, orphan worker, or false completion |

Test feature, bug fix, quick job, test-only job, investigation/design-only job, and multi-task job. Non-production jobs must obey their allowed output scope. For each fixture include one known product behavior change with independently specified input/output examples.

## 4. AI-generated tests and repair quality

Scripted model tools make failure sequences deterministic; native test runners still execute real tests. Keep at least one live-model lane to detect protocol/prompt/model behavior that scripted tools cannot establish.

| Scenario | Required result |
| --- | --- |
| No tests exist | Set up the correct framework/target and execute a nonzero known set |
| Product bug causes failure | Fix product behavior; retain the original regression assertion |
| Test expectation is wrong | Explain mismatch against fixture requirements; change only the justified expectation |
| Dependency/toolchain failure | Classify setup/environment issue; avoid changing product logic to conceal it |
| Test written but unregistered | Report not run; register/include it where supported, then prove discovery |
| Cross-component defect | Run client/server contract checks; a green client suite alone is insufficient |
| Repeated ineffective fix | Stop at configured attempt/time budget; show last failure and recovery action |
| Permission/tool refusal | Mark work incomplete; retain diagnostics and allow retry after correction |
| Test-only job edits production | Reject/flag scope breach; preserve allowed test output and explain result |
| Flaky test passes on retry | Report initial failure and retry history; do not erase flakiness |

For every generated-test acceptance case: introduce a known defect, confirm the generated test fails for the intended reason, restore correct behavior, and confirm it passes. Include negative controls that delete assertions, replace meaningful checks with `assert true`, disable discovery, or skip the failing test; the acceptance oracle must reject those outcomes. Measure behavioral mutation detection separately from line coverage.

Check that repair prompts receive the actual error, project commands, relevant component, and product context, while credentials and unrelated project data are excluded. Record tool/model identity, attempts, wall time, and token/cost data when available.

## 5. Git and distribution fault matrix

Git cases must verify file contents and commit graphs against a temporary bare remote, not just mocked `git` calls:

- Initialize an empty project; first commit; missing author identity; no remote.
- Existing local history; GitHub and plain Git; custom base branch and remote name.
- Unstaged, staged, and untracked user files; ignored build output; protected files.
- Detached HEAD, existing job branch/worktree, conflict, rejected push, remote advancement.
- Two jobs touching different files, then the same file; lock contention and restart.
- Failed authentication, missing `gh`, PR already exists, merge rejected, retry after success.
- After every failure: user changes still present, active branch known, no false merge/completion.

Distribution runs in three modes: deterministic fake provider; native build with local artifact checks; live upload into a dedicated test destination. Report these as distinct evidence levels.

- iOS: build for simulator vs device, export/signing prerequisites, wrong profile/bundle ID, missing SDK/device, actual install/launch where applicable. A simulator app is not a distributable device IPA.
- Unity: invoke editor correctly; editor import/compile is not proof of an iOS export; check explicit export stage, generated Xcode project, correct SDK, build, then install. Do not append Xcode-only flags to Unity or arbitrary shell commands.
- Android: APK/AAB choice, variant, package ID, signing, artifact discovery, native test/emulator execution, provider acceptance.
- Web: correct output folder and preview channel, assets/routes/API endpoint, nonproduction environment, expired preview, frontend/backend version compatibility.
- File: valid configured artifact within project; missing/stale/wrong extension; checksum and destination match.
- Server/desktop/generic: test supported configured packaging/deployment boundaries; no automatic Firebase-mobile delivery inferred from language alone.
- All: missing CLI/auth, wrong destination, timeout, interrupted upload, provider exit zero without proof, malformed response, retry without duplicate distribution, delivery off, manual delivery.

A fake receipt proves parsing/state handling; only the live lane proves provider acceptance. A receipt alone does not prove the app launches; the native/device/browser smoke check supplies that evidence.

## 6. Browser, visual, and accessibility coverage

Add actual browser automation against the local server with fixture data and deterministic model tools. HTTP tests remain useful but do not establish that JavaScript executes successfully.

- Render every job state, with/without `problem`, sign-in problem, active run, PRD, tasks, test results, delivery receipt, and missing optional fields. Fail on uncaught page errors and unexpected console errors.
- Exercise Home, project picker, setup, requirements, job creation/detail, tests, Git, delivery, and run log pages by clicking real controls.
- Navigate all five timeline buttons forward/back; verify target content, keyboard focus, current stage vs selected section, repeated taps, mobile horizontal scrolling, and preserved status after refresh.
- Test smooth transitions with ordinary motion and reduced-motion preference. Assert destination and absence of jumps; use video/manual review for perceived smoothness rather than fragile exact-frame timing.
- Verify narrow/mobile layouts, keyboard navigation, visible focus, readable errors, dialog behavior, and no blocked primary actions offscreen.
- Simulate slow responses, rejected requests, reconnect, stale polling, and switching projects while requests are in flight.
- Capture screenshot, trace/video, console, server log, network failures, and fixture identity on failure. Mask secrets in retained artifacts.

Start with Chromium and WebKit; cover the embedded macOS web view separately. Add Firefox where it is part of the supported browser policy. Browser engines are not substitutes for native app packaging checks.

## 7. Execution lanes and release gates

| Lane | When | Work | Proposed budget / gate |
| --- | --- | --- | --- |
| L0 | Every change | Canonical compile/unit checks; command transformation and state rules | Measure current baseline first; all required tests green |
| L1 | Every PR | Deterministic journeys; minimal browser smoke; changed-component regressions | Target ≤15 min using shards; every mandatory scenario selected for this lane passes |
| L2 | Nightly | All 40 baseline journeys; constrained pairs; high-risk triples; real available toolchains | Target ≤90 min with platform shards; publish missing capabilities |
| L3 | Weekly and release candidate | Native simulator/emulator/device/package runs; controlled live AI/service integration; upgrade/recovery | Explicit runner/cost/time budgets; no silent model fallback |
| L4 | Weekly | Chaos, concurrency, scale, repeated resume/retry, flake detection | Scheduled soak with resource and failure evidence |

Host and target are separate axes. Start with the project's supported macOS host for desktop/Xcode/Unity. Use Linux for supported headless Python/Node/Go/Rust/server paths. Windows may be a managed-project target or future host compatibility experiment; do not claim full host support merely because a fixture uses Gradle or .NET. Record host architecture, target architecture, SDK, engine, package manager, and dependency cache state.

Required release gates:

1. No uncaught browser error in a required journey.
2. No false successful build, zero-discovery “pass,” or unverified delivery.
3. No lost user changes, wrong-branch merge, cross-project action, or duplicate side effect.
4. Every required fixture/capability has a fresh passing result for the release revision and runner class.
5. All mandatory high-risk combinations pass; feasible pair coverage is reported with exclusions and reasons.
6. Known failures have named ownership and disposition. A required skipped test blocks qualification on that platform; report `blocked`, `not applicable`, `unsupported`, `failed`, and `passed` separately.
7. Any quarantined flaky test remains visible with owner and expiry; it cannot satisfy a required release gate.
8. Each reported user bug receives a regression at the lowest useful layer plus a journey-level case when stages interact.

Do not use one aggregate coverage percentage as the release decision. Track required journeys passed/executed/blocked, feasible pair coverage, toolchain coverage, generated-test mutation detection, flakes, repair success/attempts, recovery success, runtime, and live cost. Static test references are not executed or line coverage.

## 8. Reproducible scenario records

Proposed manifest location: `tests/e2e/project_matrix/scenarios.json`. Each scenario stores:

- Stable ID, fixture ID/version, tags, required capabilities, host/target/tool versions.
- PRD/architecture/test/Git/AI/distribution starting states and combination seed.
- Exact actions, injected failure stage, expected visible state, allowed filesystem effects, independent acceptance check.
- Expected executed test IDs/counts, required artifacts/receipt, timeout/retry budget, cleanup policy.
- Tier and evidence level: simulated tools, real local tools, or live provider.

Proposed result: `scenario_id`, `revision`, `seed`, `versions`, `status`, `stage`, `elapsed_seconds`, `attempts`, `assertions`, `artifact_paths`, `failure_reason`. Distinguish product failure from harness/infrastructure failure. Retain failed sandboxes for replay with a bounded retention period; clean successful sandboxes and stop child processes. A replay must use the same fixture, seed, model script, and command versions.

## 9. Implementation sequence

These are proposed work items, not files already implemented. Assign an owner when scheduling each item. Each item ships with its own checks and updates the evidence inventory.

| Order | Deliverable and files | Acceptance / dependency |
| --- | --- | --- |
| T1 | Baseline report in `docs/testing/project-matrix-baseline.md`; reconcile `docs/test-matrix.md` claims against collected tests | Record current failures/skips, runtimes, host/tool versions; explicitly map automation claims to executable tests |
| T2 | Regression journeys in `tests/e2e/test_job_page_runtime.py` and `tests/e2e/test_unity_visual_workflow.py` | Reproduce the two reported failures; corrected behavior passes; missing Unity/export prerequisites remain actionable |
| T3 | Extend `tests/e2e/harness.py`; add `tests/e2e/project_matrix/fixtures.py`, `scenarios.json`, `runner.py`, `test_manifest.py` | Create/clean/replay P01/P03/P07 with real build/test execution; preserve existing E2E callers; validate manifest IDs and constraints |
| T4 | Add `tests/e2e/project_matrix/test_journeys.py` and `test_requirements.py` | PRD present/absent/partial/stale, no AI, setup-before-AI, all core stage assertions; depends on T3 |
| T5 | Add native/platform fixture definitions and `test_components.py` | Expand P02–P20; client/server/shared isolation, command quoting and native execution; depends on T3 |
| T6 | Add `test_ai_repairs.py` and deterministic responses in existing fake model harness | Known defect red/green, negative controls, zero discovery, scope guards, retry budgets; depends on T4/T5 |
| T7 | Add `test_git_journeys.py` and `test_delivery_journeys.py` | Real local Git graphs, branch/conflict recovery, artifact provenance and provider failures; depends on T4/T5 |
| T8 | Extend browser suite with `test_browser_journeys.py` | Page-state matrix, real controls, timeline, reconnect, focus and motion; expand T2 using T3 fixtures |
| T9 | Add `test_recovery.py`, `test_combinations.py`, and `test_scale.py` | Reproducible pair coverage, required triples, kill/restart checkpoints, concurrency, scan limits; depends on T4–T8 |
| T10 | Wire CI lanes and publish result summaries; update `docs/build-test-commands.md` with actual new commands | Clean machine can execute/replay scenarios; missing runner capabilities visible; live jobs only in configured test environments |

For each bug discovered: first capture a failing behavioral case, fix the smallest responsible production path, then rerun the case and affected canonical suites. Avoid tests that only assert source text exists. For browser failures, execute the full affected page as well as isolated logic.

Start with T1–T4: baseline, the two real regressions, a reusable harness, and PRD/setup journeys across Python, web, and Swift. Next prioritize P08/P12/P13/P17 because mobile, Unity, and mixed repositories expose the most cross-toolchain boundaries in the reported problems. Expand the remaining portfolio and combinations before calling the matrix comprehensive.

## 10. Validation commands and completion of this planning task

Existing executable commands remain in [build-test-commands.md](../build-test-commands.md):

```bash
python3 -m compileall -q orchestrator tests
python3 -m unittest discover -s tests
swift test --package-path desktop/macos
```

Live-model, authentication emulator, package-manager integration, and macOS packaging commands are opt-in as documented there. New matrix/browser commands must be added only when their runner exists; this plan deliberately does not present fictional runnable commands.

This planning task is complete when the portfolio, dimensions, assertions, failure cases, execution lanes, and ordered deliverables are reviewable. Implementing and executing the expanded matrix is subsequent work. Document review validates this plan; it is not evidence that the proposed journeys pass.
