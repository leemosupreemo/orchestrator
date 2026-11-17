# Architecture

The Swift Orchestrator is a Python-based multi-agent system designed to automate the development lifecycle of Swift and Xcode projects. It coordinates various AI agents to perform tasks ranging from planning and design to implementation, testing, and delivery.

## System Overview

The orchestrator operates as a standalone CLI tool that can be integrated into any Swift repository. It manages state via local job files and leverages external AI providers for cognitive tasks.

### Key Components

1.  **CLI Entry Point (`orchestrator/cli.py`)**: The primary interface for users to interact with the orchestrator.
2.  **Project Config (`orchestrator/project_config.py`)**: Manages project-specific settings, Xcode configurations, and worker definitions.
3.  **Agent Loop (`orchestrator/scripts/agent_loop.py`)**: The core execution engine that sequences agent tasks.
4.  **LLM Integration (`orchestrator/scripts/llm.py`)**: Provides a unified interface to multiple AI providers (Antigravity, Claude, OpenAI, Ollama).
5.  **Job Management**: State is persisted in `.orchestrator/jobs/` as JSON files, allowing for asynchronous execution and recovery. How a job stops, recovers, is backed up and moves between computers: [Job recovery and ownership](job-recovery.md).

## Multi-Agent Workflow

The system uses a tiered agent architecture:

*   **Planner**: Analyzes requirements and breaks them down into actionable tasks.
*   **Designer**: Creates high-fidelity specs for features (Stitch AI mode).
*   **Builder**: Executes individual tasks, writing code and tests.
*   **Reviewer**: Analyzes code changes for quality and adherence to standards.
*   **Verifier**: Validates that the implementation meets the original requirements.
*   **Debugger**: Specialized agent for root-cause analysis and fixing failures.

## Execution Modes

*   **Local**: Executes tasks directly on the host machine.
*   **SSH (Fleet)**: Dispatches tasks to a fleet of remote macOS workers, enabling parallel builds and testing across different environments.

## Integration Points

*   **Xcode**: Interfaces with `xcodebuild`, `xcrun`, and `simctl`.
*   **GitHub**: Uses `gh` CLI for issue tracking, branch management, and pull requests.
*   **Firebase**: Automates app distribution for visual QA.
