# AI Workflow

Use `orchestrator console` from the repository root to create and manage jobs for swift-orchestrator.

Recommended flow:

1. Run `orchestrator check` after initial setup or toolchain changes.
2. Run `orchestrator check-config` after editing `.orchestrator/project.json` or machine config.
3. Create jobs from the console.
4. Review generated branches and pull requests before merging.
5. Keep generated `.orchestrator/jobs/`, `logs/`, `output/`, and `state/` files out of Git.

## Model choice within a feature job

Feature plans label each task `small`, `medium`, or `large`. The worker keeps the selected builder for medium and large tasks. For a small, localized task (for example, changing spacing or positioning), it prefers an enabled free or local coding model, then the least-cost enabled coding model. It only considers models allowed for that job; if none qualify, it uses the selected builder. If a cheaper model fails, the normal model fallback can take over.
