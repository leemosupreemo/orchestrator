# Orchestrator web UI on Firebase

The dedicated Firebase project is `swift-orch-web-20260923`. Its web app uses Google sign-in and a verified `enmeskin@gmail.com` account. Firestore rules permit that account to read job projections and create typed commands. Browser writes to job or worker state are denied. Reference files use owner-only Firestore chunks, limited to 10 MB per artifact, so the preview does not require a Cloud Storage billing upgrade.

## Start the Mac worker

Install the optional worker dependency with `python3 -m pip install -e '.[web-worker]'`. Provide Google Application Default Credentials or a service account credential file via `GOOGLE_APPLICATION_CREDENTIALS` outside this repository. The identity needs Firestore document read/write access only on this Firebase project. Set:

```sh
export GOOGLE_APPLICATION_CREDENTIALS="$HOME/.config/orchestrator/swift-orch-web-worker.json"
export ORCHESTRATOR_FIREBASE_PROJECT=swift-orch-web-20260923
export ORCHESTRATOR_WEB_OWNER_EMAIL=enmeskin@gmail.com
export ORCHESTRATOR_WEB_PROJECTS='{"themis":"/Users/neemaameskin/Documents/Themis"}'
orchestrator-web-worker
```

Keep this process running on the Mac. It polls commands, checks the operator and local job revision, runs the corresponding Orchestrator operation, and publishes sanitized job records. A stopped worker leaves the UI readable with an offline indicator. Running commands interrupted by a restart are marked failed for review.

## Build and preview

Copy `web/.env.example` to `web/.env.local` if the defaults change. Firebase web app values are public configuration, not credentials. Run `npm --prefix web ci && npm --prefix web run build`, then `firebase hosting:channel:deploy orchestrator-ui --project swift-orch-web-20260923`. Deploy rules with `firebase deploy --only auth,firestore --project swift-orch-web-20260923`. Run rule tests with `firebase emulators:exec --only firestore 'npm --prefix firebase-tests test'`.

The preview URL and verification results are recorded here after deployment.

## Preview deployed 2026-09-23

Preview URL: https://swift-orch-web-20260923--orchestrator-ui-k9t3gz5v.web.app (expires 2026-09-30). The public Hosting page returned HTTP 200. Authentication and Firestore rules are deployed. The worker service account has `roles/datastore.user`; its JSON credential is stored outside Git at `~/.config/orchestrator/swift-orch-web-worker.json` with mode 0600. A `project.refresh` command for Themis queued in Firestore was claimed by the Mac worker and completed successfully, reporting the local machine available. Browser sign-in and interactive job creation still need to be exercised by the operator in the preview.

The current Mac worker targets the CLI-configured Themis project at `/Users/neemaameskin/Documents/Themis`. Use the project picker when more local projects are registered.

## Configuration & Tools behavior

The web menu now uses typed worker commands for all 13 CLI Configuration & Tools sections. Model selection and active fleet selection become New Job defaults; fleet discovery, probes, edits and stale-process cleanup run on the Mac. Instruction actions manage the CLI's root instruction files and optional role prompts. Email actions cover recipients, pasted CSV import, sender metadata and a real test send. Docs include Orchestrator help and project docs. Audit, distribution, self-tests and updates run their local scripts and return exit status in the selected tool panel.

The browser does not accept API keys, sender passwords, Firebase login credentials, or signing secrets. Their status is shown without values, and setup stays in the Mac CLI. Project switching is limited to worker-configured projects; register additional paths on the Mac and restart the worker to expose them. Destructive cleanup and live delivery actions ask for confirmation in the browser.

Prerequisite Audit presents the Mac setup checks as a checklist, separates optional tools from items needing attention, and shows the next step for each missing item. It also summarizes machine reachability and project paths. The original terminal report remains under Technical output.

Configuration & Tools results show the action, status, a short explanation, and readable result details. Commands that run local scripts publish a live log while the Mac worker runs them. The audit keeps its setup checklist visible above the technical log. Completed logs and full result data are expandable in the tool panel and Activity history. Worker progress and results are redacted before publication; the visible log retains only its latest 20 KB.

The LLM Models catalog loads when opened and groups cards under provider subheaders. Each card shows tier, capabilities, required CLI, and an Enabled or Disabled marker. Toggle models directly in the catalog and save the enabled set; at least one must remain enabled. The worker validates saved IDs against the catalog. When no web selection has been saved, the catalog shows the model behind the `codex` fallback used by New Job as enabled.

The remaining tool outputs use the same readable result view: provider sign-in and fallback keys are separate readiness checks; fleet results show configured machines, probes, and discovery candidates; instruction files show missing or broken links with a next step; email and distribution show setup readiness; and document or instruction content has its own reading panel. Short status summaries cover branch, project, archive, document index, and package version. Full result data remains expandable.

The LLM API Keys panel exposes its two actions as direct buttons for checking provider readiness and clearing saved fallback keys. Clearing keys still requires confirmation.

After the final deployment, the worker on the main checkout also completed `tests.run` with `action: list` for Themis. It found no `.xctestplan` files under `ThemisPlaygroundTests/TestPlans`; the UI therefore offers the simulator visual check and explains that no Xcode plans are configured. The browser's signed-in flow has not been exercised in this session.

The Configuration & Tools update was deployed to the same preview channel on 2026-09-23. The page returned HTTP 200, the Mac worker was restarted, and read-only commands for Models, Keys, Fleet, Base Branch, Project, Archive, Instructions, Email, Docs, Distribution, and Update all completed successfully through Firestore. The final local run passed 183 Python tests, the web menu test, and the production web build. Interactive Google sign-in and actions with external side effects still require an operator test in the preview.
