# GitHub, GitLab, Bitbucket and other git hosts

Orchestrator works with any git repository. GitHub gets the deepest integration, with issues and pull requests. Every other host, or no remote at all, works through plain git branches.

## Two modes

Set `code_host` in `.orchestrator/project.json`:

| `code_host` | What a job is | When it finishes | Merge & complete |
|---|---|---|---|
| `"github"` | A GitHub issue (its number names the job and its branch, `ai/issue-<n>-…`) | The branch is pushed and a draft pull request is opened. The AI review is posted on it. | Merges the pull request with `gh`. |
| `"git"` | A local job with a short reference (`ai/job-<ref>-…`) | The branch is pushed to `origin` if there is one, with a link to open a merge request on GitLab or Bitbucket. The AI review reads the branch's own diff. | Merges the branch into the base branch **on this computer**. It never pushes the base branch for you. |
| `"auto"` (default) | GitHub when `origin` points at github.com, plain git otherwise. | | |

Planning, building, testing, the AI review, remote workers and delivery all work the same in both modes.

```json
{
  "project_name": "My App",
  "code_host": "git"
}
```

Run `orchestrator check-config` after changing it.

## GitHub

What you need:

- `origin` on github.com.
- The GitHub CLI, signed in: `brew install gh`, then `gh auth login`.

The setup checklist and the start-of-job check both stop you if `gh` is missing or signed out, because a GitHub job starts as an issue. To use a GitHub repository without issues and pull requests, set `"code_host": "git"`.

GitHub-only extras: CI status and re-running failed checks, closing issues from the web app, and language stats on project cards.

## GitLab

`origin` on gitlab.com or a self-hosted GitLab (any host with `gitlab` in its name) works out of the box in plain git mode.

1. A finished job's branch is pushed to `origin`. The job and the terminal show a link that opens a new merge request from that branch into the base branch.
2. Then either:
   - **Review and merge on GitLab** (keeps your approvals and CI), then choose **Mark complete** on the job.
   - Or choose **Merge & complete** to merge the branch locally, then `git push origin <base>` yourself.

Pushing uses your normal git credentials for GitLab: an SSH key, or a credential helper for HTTPS. Orchestrator doesn't need a GitLab token.

## Bitbucket

Same as GitLab: the branch is pushed, and the job links to Bitbucket's new pull request page with the branch and base filled in. Merge there and mark the job complete, or merge locally.

## Azure DevOps, Gitea, Forgejo, Gerrit, self-hosted servers

Plain git mode works with any remote git can push to. There's no ready-made merge request link for these hosts. Open the request from the pushed branch the way your host normally does.

## No remote at all

Leave out `origin`. Jobs still run on their own `ai/job-…` branches. Review the branch locally, then **Merge & complete** merges it into your base branch. Add a remote any time with `git remote add origin <url>`.

## Branches

Orchestrator only deletes branches it created (`ai/issue-…` and `ai/job-…`), after they're merged or when a job is discarded. Branches you name yourself are kept.

## Switching modes

You can change `code_host` at any time. Jobs that already exist keep how they were created: a GitHub job keeps its issue and pull request, and a plain git job keeps its branch. New jobs follow the new setting.
