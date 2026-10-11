Add meaningful test coverage for this job using the repository's existing test
frameworks. Read AGENTS.md, manifests, existing tests and documented test commands.
Inspect production code to understand its actual behavior; it is read-only.

Only propose test files. Never fix, refactor, instrument or change production
code, manifests, build settings or dependencies. Use integration tests or
test-side fixtures instead of inserting tests or hooks into production files.
If a test exposes a production defect, report it as a separate bug; do not fix it.
Never weaken assertions or change expected behavior to make a failing test pass.

Do not write files or commit. Return complete contents of proposed test files as
JSON. Orchestrator validates the paths, saves the tests and runs the configured
test command with the project read-only. Use temporary directories for test data.

Return ONLY JSON:
{"summary": "Coverage added and any production defects found", "files": [{"path": "tests/test_example.py", "content": "complete test file text"}]}

If tests require a production change, return
{"clarification_needed": "What blocks test coverage without production changes?"}.
