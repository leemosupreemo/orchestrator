"""Where a project's code is kept, in plain words: the choices, what each is good and bad at, and how to set one up.

The Git setup page shows `OPTIONS` in a dropdown with the selected one's strengths, weaknesses and steps. `current_key` says which one the
project uses now (or "" when none has been chosen), and `help_prompt` is the optional AI step that turns the generic steps into ones for this
project's actual state. Nothing here changes a repository.
"""
from __future__ import annotations

from typing import Any

BLURB = "Source control keeps a history of every change, so you can undo mistakes and jobs can work side by side on their own branches."

# action: what the page's button does. github = sign in, then create or connect. connect = paste the repository's clone URL. local = keep it here.
OPTIONS: list[dict[str, Any]] = [
    {
        "key": "github", "label": "GitHub", "action": "github", "workflow": "GitHub issues and draft pull requests",
        "tagline": "Full Orchestrator integration with automatic issues, labels, and draft PRs.",
        "good": ["Automatic issues, labels, and draft PRs for every job",
                 "Free private repos and GitHub Actions CI"],
        "watch": ["Requires GitHub CLI (`gh`) signed in on this computer",
                  "Cloud-hosted (self-hosting requires Enterprise)"],
        "steps": ["Install GitHub CLI (`brew install gh`) and click **Sign in to GitHub**.",
                  "Click **Create a GitHub repository** or **Connect an existing one**.",
                  "Orchestrator handles labels and draft pull requests automatically."],
        "docs": "https://docs.github.com/en/get-started/start-your-journey/hello-world",
    },
    {
        "key": "gitlab", "label": "GitLab", "action": "connect", "workflow": "Git branches with a merge request link",
        "tagline": "Git hosting with built-in CI/CD, issue boards, and optional self-hosting.",
        "good": ["Built-in CI/CD and free self-hosting option",
                 "Fine-grained team permissions"],
        "watch": ["Uses plain Git branches and local merges (no automatic issues)",
                  "Self-hosting requires maintaining your own server"],
        "steps": ["Create a blank project on **gitlab.com** (no README) and copy its clone URL.",
                  "Click **Connect a repository**, paste the URL, and save.",
                  "Click **Push** to upload your code (requires SSH key or HTTPS token)."],
        "docs": "https://docs.gitlab.com/ee/tutorials/make_first_git_commit/",
    },
    {
        "key": "bitbucket", "label": "Bitbucket", "action": "connect", "workflow": "Git branches with a pull request link",
        "tagline": "Atlassian Git hosting built to pair with Jira and Trello.",
        "good": ["Tight Jira and Trello integration",
                 "Free for small teams with built-in pipelines"],
        "watch": ["Uses plain Git branches and local merges (no automatic issues)",
                  "Smaller ecosystem than GitHub or GitLab"],
        "steps": ["Create an empty repository on **bitbucket.org** (no README) and copy its clone URL.",
                  "Click **Connect a repository**, paste the URL, and save.",
                  "Click **Push** to upload your code (requires SSH key or app password)."],
        "docs": "https://support.atlassian.com/bitbucket-cloud/docs/create-a-git-repository/",
    },
    {
        "key": "azure", "label": "Azure DevOps", "action": "connect", "workflow": "Git branches with a pull request link",
        "tagline": "Microsoft Git repos, boards, and pipelines for Azure/M365 teams.",
        "good": ["Enterprise Microsoft 365 / Entra sign-in and controls",
                 "Boards, repos, and pipelines in one place"],
        "watch": ["Uses plain Git branches and local merges (no automatic issues)",
                  "More setup layers (organization → project → repo)"],
        "steps": ["Create a repo under **Repos** at **dev.azure.com** and copy its clone URL.",
                  "Click **Connect a repository**, paste the URL, and save.",
                  "Click **Push** to upload your code (requires SSH key or PAT)."],
        "docs": "https://learn.microsoft.com/en-us/azure/devops/repos/git/create-new-repo",
    },
    {
        "key": "other", "label": "Another Git host", "action": "connect", "workflow": "Git branches and local merges",
        "tagline": "Gitea, Forgejo, or any custom Git server.",
        "good": ["Full control over hosting and data",
                 "Works with any standard Git remote URL"],
        "watch": ["Uses plain Git branches only (no issues or PR links)",
                  "You manage server uptime, backups, and credentials"],
        "steps": ["Create an empty repository on your Git host and copy its clone URL.",
                  "Click **Connect a repository**, paste the URL, and save.",
                  "Click **Push** to upload your code (requires SSH key or HTTPS token)."],
        "docs": "https://git-scm.com/book/en/v2/Git-on-the-Server-Setting-Up-the-Server",
    },
    {
        "key": "local", "label": "On this computer only", "action": "local", "workflow": "Local Git branches only",
        "tagline": "Keep Git history on this Mac with no remote; add one anytime.",
        "good": ["Zero setup, works offline, private by default",
                 "Full branch history and undo per job"],
        "watch": ["No remote backup if this computer's disk fails",
                  "Can't share code or sync jobs to other machines"],
        "steps": ["Click **Initialize repository** if this folder isn't a Git repo yet.",
                  "Click **Use this computer only** below.",
                  "Back up the folder separately, or connect a remote later."],
        "docs": "https://git-scm.com/book/en/v2/Getting-Started-What-is-Git%3F",
    },
]
BY_KEY = {o["key"]: o for o in OPTIONS}


def current_key(host_kind: str, origin: str, code_host: str | None) -> str:
    """Which option the project uses now. "" means none chosen: no remote, and the person hasn't picked "on this computer" either."""
    if origin:
        return host_kind if host_kind in BY_KEY and host_kind != "local" else "other"
    return "local" if code_host == "git" else ""


def help_prompt(key: str, facts: dict[str, Any]) -> str:
    """The optional AI step: setup steps for the chosen option, written for where this project is now."""
    option = BY_KEY.get(key)
    if not option:
        raise ValueError("Choose one of the options first.")
    state = [
        f"- This folder {'is' if facts.get('is_repo') else 'is not'} a Git repository" + (f", with {'commits' if facts.get('has_commits') else 'no commits yet'}" if facts.get("is_repo") else ""),
        f"- Remote ('origin'): {facts.get('origin') or 'none'}" + (f" ({facts.get('provider')})" if facts.get("origin") and facts.get("provider") else ""),
        f"- Project: {facts.get('project') or 'unnamed'}" + (f", {facts['stack']}" if facts.get("stack") else ""),
        f"- Computer: {facts.get('system') or 'unknown'}",
    ]
    if key == "github":
        gh = facts.get("github") or {}
        state.append(f"- GitHub CLI: {'signed in as ' + gh['user'] if gh.get('user') else 'installed, not signed in' if gh.get('installed') else 'not installed'}")
    return "\n".join([
        f"Someone is setting up source control for a software project and chose: {option['label']}.",
        f"What Orchestrator does with it: {option['workflow']}.",
        "",
        "Where they are now:", *state,
        "",
        "Write the steps for them, starting from exactly where they are. Rules:",
        "- One short sentence first that says where they're starting from and what's left to do.",
        "- Then a numbered list of at most 8 steps, one action each, in plain words for someone who may be new to Git.",
        "- Name the buttons on Orchestrator's Git setup page where they apply: Initialize repository, Sign in to GitHub, Create a GitHub repository, "
        "Connect a repository, Use this computer only, Push.",
        "- Put terminal commands in backticks, and only when needed. Skip steps they've already done.",
        "- Don't invent web addresses. For a sign-up or clone address, say where on the host to find it.",
        "- If they are already set up the way they asked, say so and list what to check instead.",
        "- Plain text only: no headings, no tables, no bold except for button names.",
    ])
