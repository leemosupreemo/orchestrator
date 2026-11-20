# Preview update recovery validation

## Automated coverage

The approved behavior and rollout constraints are in [the design](../superpowers/specs/2026-11-11-preview-update-recovery.md).

Final validation on 2026-11-11: the whole Python suite passed **2,778 tests with 12 skips** (including the three opt-in Firestore tests, run separately below). The native suite passed **72 tests** and built successfully. The frontend suite passed **32 behavior tests**. Compilation, JavaScript syntax and diff checks passed.

| Boundary | Verification |
| --- | --- |
| Capability detection | Explicit negative capability from authenticated runner and heartbeat required for automatic update; ordinary errors and unknown metadata do not request updates. Older installations get manual upgrade guidance. |
| Account authorization | Owner-only request/cancel; machine-authenticated progress; other users and stale operation IDs rejected. |
| Cancellation vs installation | Transactional per-machine arbitration, repeated concurrent tests against both memory store and real Firestore emulator. |
| Job safety | Authenticated control-plane/agent integration preserves busy gate, then permits idle installation; claim prevents new work and rejects cancellation. |
| Delivery and persistence | Agent operation persistence, terminal acknowledgement, lost terminal progress and retry, invalid saved state, stale replies and operation replacement. |
| Native cancellation | Checking, download cancellation hook, extraction cancellation, ready reply `.skip`, late callbacks, scheduler generation changes, preparation races, restoration/journal failures. |
| Hosted progress | Real modal render/wiring exercised in Node fixture; Cancel/Close/explicit Retry, disabling cancellation during install, Computers progress, interrupted polling and reload. |
| Context safety | Original preview machine/project/backend checked before recovery; switching context during request, polling, reconnect or probe cannot retry another preview. |
| Ambiguous requests | A creation request committed with a lost response is reconciled without another POST and remains cancellable; delayed visibility and reload tested. |
| Completion | Reconnection to the same computer and capability verification before one preview retry; an insufficient release is terminal rather than a loop. |
| Shutdown | Signal tests follow the managed connection entry point; readiness is announced inside the cleanup guard. |

### Focused Python coverage

```sh
python3 -m coverage run --branch --data-file=/tmp/orchestrator-update-coverage --include='*/orchestrator/update_recovery.py,*/cloud/functions/update_recovery.py' -m unittest tests.test_update_delivery tests.test_update_recovery tests.test_update_recovery_states tests.test_update_recovery_integration -q
python3 -m coverage report --data-file=/tmp/orchestrator-update-coverage --fail-under=100
```

Both new Python recovery state modules have 100% line and branch coverage. This is not a claim of 100% coverage for the entire server or agent.

### Frontend behavior

```sh
node --experimental-test-coverage --test --test-coverage-include='**/static/update-recovery.js' tests/update-recovery-ui.test.cjs
```

The controller has 100% line/function coverage and 98.87% branch coverage in the final test run. Node measures V8 branch ranges, including completion paths in `finally`; this differs from Python branch coverage. These are behavior and modal-wiring tests, not a real-browser visual run.

### Firestore integration

```sh
ORCHESTRATOR_E2E_UPDATE_FIRESTORE=1 python3 -m unittest tests.e2e.test_update_recovery_firestore -v
```

Three tests passed against the actual local Firestore emulator and SDK, using anonymous credentials, an explicit demo project and an ephemeral loopback port. The test starts and stops its emulator. It does not use production data or credentials.

### Native tests and build

```sh
swift test --package-path desktop/macos --enable-code-coverage
swift build --package-path desktop/macos
```

The remote session, scheduler, progress reporter and request lifecycle have 100% measured line/region/function coverage. Native coordinator line coverage is 87.50%, including remote operation-ID serialization, legacy journal decoding, orphan installation claims, retained completion evidence until acknowledgement, and remote completion followed by legacy manual requests. Live service/UserDefaults wrappers and the executable-target Sparkle/MenuController runtime do not have full unit coverage.

## Independent review fixes

- Late preview request failures are discarded before recovery if the original machine, project or backend changed.
- Orphan installation claims are reconciled on startup, and completed journal evidence survives until the agent accepts the result.
- Automatic updates require explicit negative capability evidence from both the authenticated runner and machine heartbeat; historical unknown metadata only offers manual instructions.
- Lost creation responses are reconciled by querying the existing operation, including across reload, without submitting another request. Cancellation remains available.
- A remote `current` result releases its request handling so later legacy manual updates still work.

Each finding has a regression test. The full suite and native coverage run above include the fixes.

### Whole repository

```sh
python3 -m compileall -q orchestrator cloud/functions tests
python3 -m unittest discover -s tests
node --check orchestrator/web/static/app.js
node --check orchestrator/web/static/account.js
node --check orchestrator/web/static/update-recovery.js
git diff --check
```

## Release validation still required

Before enabling recovery in the hosted app, publish a compatible signed native release and control-plane protocol in the order specified by the design. Exercise an outdated packaged runner through signed update discovery, download cancellation, waiting-for-work cancellation, installation claim, service shutdown, actual replacement, relaunch, and authenticated preview retry.

This signed native lifecycle has not been exercised by these tests. The existing CLI-managed computer also requires its one-time upgrade. Neither live release readiness nor full end-to-end recovery is implied by the coverage percentages above.
