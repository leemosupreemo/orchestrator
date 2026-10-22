Add meaningful test coverage for this job using the repository's existing test
frameworks. Read AGENTS.md, manifests, existing tests and documented test commands.
Inspect production code to understand its actual behavior; it is read-only.

Only propose test files. Never fix, refactor, instrument or change production
code, manifests, build settings or dependencies. Use integration tests or
test-side fixtures instead of inserting tests or hooks into production files.
If a test would fail only because the production code is wrong (the test states what the
code should do and it does not), report it as a separate bug; do not fix it, and leave that
test out of your files rather than shipping a failing one. List it under "defects".
Never weaken assertions or change expected behavior to make a failing test pass.

Do not write files or commit. Return complete contents of proposed test files as
JSON. Orchestrator validates the paths, saves the tests and runs the configured
test command with the project read-only. Use temporary directories for test data.

Return ONLY JSON:
{"summary": "Coverage added and any production defects found", "files": [{"path": "tests/test_example.py", "content": "complete test file text"}],
 "defects": [{"test": "name of the test left out", "summary": "one line: what is wrong in the code", "expected": "what it should do",
 "actual": "what it does", "file": "path of the production file", "code": "the test's source, to add once fixed"}],
 "skipped": [{"case": "TC-12-03", "reason": "plain words: why no test was written for this planned case"}]}
("defects" and "skipped" are optional: omit them, or use [], when there are none. When the plan lists test cases, every one is
either covered by a test carrying its id in a comment or named under "skipped".)

If tests require a production change, return
{"clarification_needed": "What blocks test coverage without production changes?"}.
