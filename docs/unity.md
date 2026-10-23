# Unity

Orchestrator works on Unity projects in three ways: it builds and tests them, it can pull failed Unity Cloud builds into a job, and an Editor plugin sends problems to it from inside Unity.

## Building and testing a Unity project

`orchestrator init` (and the setup wizard) recognise a Unity project by `ProjectSettings/ProjectVersion.txt` plus an `Assets/` folder, and fill in:

| Command | What it runs |
| --- | --- |
| Build | The editor in batchmode with `-quit`, which imports the project and fails on script compile errors. Unity has no compile-only switch. |
| Test | The Unity Test Framework's EditMode tests, with results in `Temp/unity-editmode-results.xml`. |

Both call the editor as `"${UNITY_PATH:-<Hub default for the project's version>}"`. Set `UNITY_PATH` to the `Unity` binary on any computer that keeps the editor somewhere else. The computer needs an active Unity licence for batchmode to start.

Single test suites run with `-testFilter`, on PlayMode when the file sits under a `Runtime` or `PlayMode` folder and on EditMode otherwise. Edit `build_command` and `test_command` in `.orchestrator/project.json` for a custom build method or for PlayMode tests (`orchestrator check-config` after).

## Unity Build Automation (Unity Cloud builds)

**Connections > Unity Build Automation** links failed cloud builds to jobs, like Jira tickets or Sentry issues. Connect with:

- **Organization ID**: the number after `/organizations/` in your Unity Cloud dashboard address.
- **Project ID** (optional): limits the list to one project. It is required to open a single build.
- **API key**: the Build Automation API key from your Unity Cloud project settings.

Searching lists failed builds (type `all`, `success`, `canceled` and so on to change that). Open one by choosing it, or type `my-target/12` (the build target's id and the build number). The job gets the build's status, platform, branch and revision, the errors found in the build log (`error CS...`, exceptions, failures) and the end of the log. It uses the Build Automation v1 API at `build-api.cloud.unity3d.com`.

## Editor plugin

A Unity package that lets you send console errors and failing tests to Orchestrator as bug jobs, and see the jobs from the Editor. See the [package README](../orchestrator/unity_plugin/com.leemosupreemo.orchestrator/README.md) for what it sends and how it connects.

```bash
orchestrator unity-plugin install            # run in the Unity project, or pass its path
orchestrator unity-plugin install --force    # replace a copy that differs from this version
```

It copies the package to `Packages/com.leemosupreemo.orchestrator` (commit it with the project). It talks to the same local web API as the browser, with the same access token: from `~/.orchestrator/ui_token` on the same computer, or typed into the window's **Connection** section for an Orchestrator on another computer (https only). Jobs it sends are planned and then wait for you, unless **Start building right away** is ticked.

The package's tests run in Unity's Test Runner (EditMode). Its logic that doesn't touch the Editor is also covered by `tests/test_unity_plugin.py`.

## MCP server

A Unity MCP server lets Codex or Claude read the Editor console and inspect scenes while working on the repo. See [recommended-mcp-plugins.md](recommended-mcp-plugins.md); `orchestrator check` reports whether one is configured.
