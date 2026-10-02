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
- **Live, free models only** (`tests/e2e/test_live_free_models.py`, about 10 minutes, skipped unless asked for):

  ```bash
  ORCHESTRATOR_E2E_LIVE=1 python3 -m unittest tests.e2e.test_live_free_models -v
  ```

  Needs the `opencode` CLI signed in. `ORCHESTRATOR_E2E_MODEL` picks another free model; `ORCHESTRATOR_E2E_KEEP=1` keeps the project folder for inspection. The test fails if any model other than the one allowed is called.

## Orchestrator config check

```bash
orchestrator check-config
```
