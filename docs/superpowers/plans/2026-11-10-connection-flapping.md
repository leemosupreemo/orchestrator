# Connection flapping repair

Observed: the Mac service and account heartbeat stay up, but its public tunnel requests time out before reaching the local API. Snapshot polling equates a fresh heartbeat with browser connectivity, enters the live opening flow, then calls the snapshot fallback on any failure. This repeatedly replaces the visible screen and claims the Mac is offline.

- [x] Reproduce the snapshot loop, state-load failure, single-flight/backoff, access denial and navigation cancellation with runtime tests.
- [x] Keep the saved snapshot visible during bounded background connection attempts. Report success only after actual authentication and state load; retain true offline recovery and explicit retry/back controls.
- [x] Probe tunnel routing with isolated clients/transports; preserve the live service and avoid speculative permanent configuration changes.
- [x] Review and run focused/full verification; prepare the frontend fix for publication. Verify live assets after deployment and report external tunnel connectivity separately from UI behavior.

Runtime regression tests reproduce the original screen loop, early unlock on state failure, access denial, overlapping retries and late ticket/auth/state results after Back. Ten snapshot tests and thirteen existing connection recovery tests pass; combined mobile/recovery selection passes 33 tests. Independent source review found no material issue.

Tunnel diagnostics: the remote process and heartbeats remain stable, and cloudflared reports a registered edge connection. The remote public route completes TLS but times out before the request reaches the local API. Both the existing and newly created local-Mac tunnels return the expected unauthorized API response promptly. Isolated remote tests with HTTP/2 over IPv4 and the latest verified cloudflared client also timed out. Those diagnostic processes were stopped and temporary downloads removed; no permanent tunnel configuration change was made. This locates the remaining failure on the remote public tunnel path but does not establish its underlying provider/network cause.

A longer public probe returned HTTP 522 after about 20 seconds, confirming an origin-connection timeout rather than a service-process exit. The exact cause of the remote tunnel path failure remains unresolved.

Final verification: 2,603 tests passed with nine optional tests skipped; 33 focused recovery/mobile tests passed; independent review reran all ten snapshot tests. Python compilation, JavaScript syntax and diff checks passed.
