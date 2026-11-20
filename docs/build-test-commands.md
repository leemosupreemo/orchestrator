# Build And Test Commands

Canonical validation commands for this project.

## Package validation

```bash
python3 -m compileall -q orchestrator tests
```

## Test suite

```bash
python3 -m unittest discover -s tests
```

## End-to-end tests (a whole project, start to finish)

`tests/e2e/` builds a throwaway project (git repo with a local remote, passing tests, the product documents, orchestrator config) and drives the real scripts through plan, architect check, approval, build, test, commit, push, draft pull request and review. `gh` is replaced by `tests/e2e/fake_gh.py`, so nothing reaches GitHub.

- **Offline** (`tests/e2e/test_pipeline_offline.py`, part of the normal suite, about 5 s): a scripted `opencode` CLI (`tests/e2e/fake_opencode.py`) stands in for the model. It records every prompt, so the tests can check what each role was told, and it misbehaves on purpose (prose instead of JSON, concerns on the first verification).
- **Fresh install, through the web app** (`tests/e2e/test_fresh_install.py`, part of the normal suite, about 20 s): a computer that has never run Orchestrator, from first open to its first jobs finished. It starts the web server the way the Mac app does (no project yet) in an empty home folder, sets up existing code with the payload the setup page sends, connects OpenCode afterwards (the order the setup page asks for), then creates, runs and merges a feature job and a quick job from the New job form's payload. Every other AI tool is a stub that looks installed but signed out and records any call: the test fails if a job uses a tool that isn't ready, reaches a real model, or a step waits for input nobody in the web app can give.
- **Live, free models only** (`tests/e2e/test_live_free_models.py`, about 10 minutes, skipped unless asked for):

  ```bash
  ORCHESTRATOR_E2E_LIVE=1 python3 -m unittest tests.e2e.test_live_free_models -v
  ```

  Needs the `opencode` CLI signed in. `ORCHESTRATOR_E2E_MODEL` picks another free model; `ORCHESTRATOR_E2E_KEEP=1` keeps the project folder for inspection. The test fails if any model other than the one allowed is called.

## Authentication integration

```bash
ORCHESTRATOR_E2E_AUTH_EMULATOR=1 python3 -m unittest tests.e2e.test_auth_emulator -v
```

Uses the frontend's pinned Firebase SDK and a temporary loopback Auth emulator with a demo project. Verifies reset request, issued code, password change, sign-in and single-use enforcement. Requires Node, npm and Firebase CLI; downloads the SDK to a temporary directory. Never sends real email or changes production users. Physical-device provider sign-in and real inbox delivery need separate live verification.

## Native package manager integration

```bash
ORCHESTRATOR_E2E_NATIVE_PACKAGES=1 python3 -m unittest tests.e2e.test_native_package_updates -v
```

Downloads Bun and provisions Poetry into temporary tool directories. Exercises selected-package updates and configured checks in throwaway workspace/Python projects, verifies unrelated dependencies and manifests stay unchanged, and compares host/external environments. Requires network access; normal discovery skips these tests.

## Orchestrator config check

```bash
orchestrator check-config
```

## macOS app

```bash
swift test --package-path desktop/macos
python3 packaging/macos/build.py --arch arm64 --output dist/macos --build-number 1 --development
python3 packaging/macos/smoke.py --bundle dist/macos/arm64/Orchestrator.app --state-dir "$(mktemp -d)" --control-dir "$(mktemp -d)"
```

Building, development images and releases are covered in `docs/macos-desktop.md`.

## Coverage measurement module

The coverage runner's unit tests must cover 100% of its lines and branches.
This focused gate does not represent whole-project coverage; run the full suite above as well.

```bash
python3 -m coverage run --branch --source=orchestrator.coverage --data-file=.orchestrator/state/coverage-unit -m unittest discover -s tests -p 'test_coverage*.py'
python3 -m coverage report --data-file=.orchestrator/state/coverage-unit --fail-under=100
```

The commands require coverage.py (`python3 -m pip install coverage`).
