# Recommended MCP Servers, Plugins, and Extensions

These integrations are optional. Swift Orchestrator runs through local scripts and CLIs; it does not require MCP servers or Codex plugins at runtime. The items below improve the assistant experience when Codex is operating the repo, debugging apps, or inspecting external systems.

## Recommended

| Integration | Why it helps | Runtime requirement |
| --- | --- | --- |
| GitHub MCP or GitHub plugin | Inspect issues, PRs, review comments, and checks with richer context than shell output alone. | Optional; orchestrator runtime uses `gh`. |
| XcodeBuildMCP or build-ios-apps plugin | Build, run, inspect, and screenshot iOS Simulator apps from Codex workflows. | Optional; orchestrator runtime uses `xcodebuild` and `xcrun simctl`. |
| Sentry MCP or Sentry plugin | Inspect production issues, events, stack traces, and release health during debug jobs. | Optional; useful only if the project uses Sentry. |
| Playwright MCP | Inspect and automate web UIs, dashboards, docs portals, and browser-based admin tools. | Optional; useful only for web/browser workflows. |

## Detection

`orchestrator check` performs a best-effort optional audit for these Codex-side integrations by checking:

- `~/.codex/config.toml` for matching MCP server or plugin entries.
- `~/.codex/plugins/cache` for installed plugin cache directories.

Missing entries are reported as recommendations, not failures.

## Security

Keep MCP/plugin access narrow. Prefer high-trust servers, project-scoped filesystem permissions, and read-only access where possible. Avoid broad connectors unless a workflow clearly needs them.
