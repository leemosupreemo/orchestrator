# Managed connections implementation plan

Goal: close the approved automatic recovery and guided setup gaps using the current Python runner, heartbeat control plane and static UI.
Spec: docs/superpowers/specs/2026-11-10-managed-connections.md

- [ ] Build a tested machine-level provider store and connection manager; implement secure public proof, bounded provider repair, previously configured failover, Funnel setup/resumption and preservation of unrelated routes. Adopt persisted service URLs.
- [ ] Add owner-only acknowledged/expiring connection commands and sanitized status in the control plane; preserve legacy reset compatibility and test ownership/replay/status validation.
- [ ] Integrate CLI and desktop runners, health challenge, heartbeat status/commands and service startup. Keep work running during repairs.
- [ ] Add clear connection status, Fix connection and guided Enable Funnel flow; runtime-test progress, legacy compatibility and unsafe links.
- [ ] Review, canonical full validation, publish control plane/frontend, roll out runners while idle, and verify public authenticated access and provider persistence.

Ownership: provider/store/service worker; control-plane worker; parent server/account/desktop/UI integration and release. Use red/green tests and exact source fallback for changed graph paths.
