# Generic projects

The command-based workflow can build and test non-Xcode repositories using
explicit shell commands in `.orchestrator/project.json`. For example:

```json
{
  "project_name": "parser-service",
  "build_command": "cargo build",
  "test_command": "cargo test"
}
```

Commands run from the project root. Configure both commands; for an interpreted
project, the build command can perform a meaningful check such as
`python3 -m compileall -q src`, with `python3 -m unittest discover` as the test
command. A generic project does not require an Xcode scheme or test target.
Run `orchestrator check-config` after updating the configuration. Normal machine
and provider configuration is still required for orchestrated jobs.

Explicit configuration commands take priority over `docs/build-test-commands.md`.
When a command is not configured, the runner reads fenced `bash` blocks under
`## Build` and `## Tests` (or `## Test`). Legacy `## iOS app build` and
`## iOS app tests` headings remain supported. Xcode projects retain automatic
build/test commands, simulator destinations, and DerivedData options. Projects
without an Xcode project/workspace must supply commands through configuration or
documentation; the runner does not guess their build system.

Agents can select focused tests using the same executable as the base test
command, such as `cargo test parser` or `npm test -- --runInBand`. Existing
Python/shell overrides remain supported. Xcode selection flags are only appended
to an Xcode base command. Command recognition is a formatting check, not a
security boundary: configured and agent-selected commands execute as shell code.

Default planning, implementation, review, and debugging prompts now derive stack
guidance from the repository. Existing `.orchestrator/prompts/` overrides still
take precedence; update custom SwiftUI prompts when adapting another stack.

The orchestrator automatically detects common stacks (Rust, Python, Node/TypeScript, Go, and Swift SPM/Xcode) when running `orchestrator init` or `orchestrator wizard`. The wizard automatically adapts its prompts to configure `build_command` and `test_command` for non-Xcode projects, bypassing Xcode scheme and signing setup. Multi-language test discovery automatically catalogs test suites across Python, Rust, Go, JavaScript/TypeScript, and Swift for agent context. Remote worker probes and visual simulator checks remain Apple-focused.
