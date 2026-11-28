# Orchestrator for Unity

Send what's broken in your Unity project to a running [Orchestrator](https://github.com/leemosupreemo/swift-orchestrator) as a bug job, and follow the jobs without leaving the Editor.

## Install

From the Unity project's folder (Orchestrator itself must be installed):

```bash
orchestrator unity-plugin install
```

This copies the package into `Packages/com.leemosupreemo.orchestrator`. Commit it with the project so everyone on the team has it.

## Use

1. Run `orchestrator ui` for the project (or keep the background service running).
2. Open **Window > Orchestrator**. The window says which Orchestrator project it is connected to.
3. Describe what's wrong, choose whether to include the console errors and any failing Test Runner tests, and press **Send to Orchestrator**.

The job is planned and then waits for you in Orchestrator. Tick **Start building right away** to skip the wait.

**Tools > Orchestrator > Send Console Problems as Bug** opens the window with the newest console error as the summary.

## Connecting

- **Same computer:** nothing to set up. The access token is read from `~/.orchestrator/ui_token`.
- **Another computer** (Orchestrator on a Mac mini, say): open **Connection** in the window and enter its address (https) and its access token. The token is stored in the Editor's preferences, never in the project.

## What gets sent

Only what you ticked: the Editor's recent console errors, exceptions and asserts (not logs or warnings), the failing tests of the latest Test Runner run, and the text you typed. It goes to the address under **Connection** and nowhere else.

## Requirements

Unity 2021.3 or newer. Failing-test capture needs the Unity Test Framework package (installed by default).
