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

## Orchestrator config check

```bash
orchestrator check-config
```

## Coverage measurement module

The coverage runner's unit tests must cover 100% of its lines and branches.
This focused gate does not represent whole-project coverage; run the full suite above as well.

```bash
python3 -m coverage run --branch --source=orchestrator.coverage --data-file=.orchestrator/state/coverage-unit -m unittest discover -s tests -p 'test_coverage*.py'
python3 -m coverage report --data-file=.orchestrator/state/coverage-unit --fail-under=100
```

The commands require coverage.py (`python3 -m pip install coverage`).
