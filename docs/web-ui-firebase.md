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
