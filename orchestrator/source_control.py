"""Where a project's code is kept, in plain words: the choices, what each is good and bad at, and how to set one up.

The Git setup page shows `OPTIONS` in a dropdown with the selected one's strengths, weaknesses and steps. `current_key` says which one the
project uses now (or "" when none has been chosen), and `help_prompt` is the optional AI step that turns the generic steps into ones for this
project's actual state. Nothing here changes a repository.
"""
from __future__ import annotations

from typing import Any

BLURB = ("Source control keeps a history of every change to your code, so you can undo a mistake, see what changed and why, let several jobs work "
         "side by side without colliding, and keep a copy that survives a broken laptop. Orchestrator's jobs each work on their own branch, so it relies on it.")

# action: what the page's button does. github = sign in, then create or connect. connect = paste the repository's clone URL. local = keep it here.
OPTIONS: list[dict[str, Any]] = [
    {
        "key": "github", "label": "GitHub", "action": "github", "workflow": "GitHub issues and pull requests",
        "tagline": "The most widely used home for code, and the one Orchestrator works with most fully.",
        "good": ["Orchestrator opens an issue and a draft pull request for every job, and labels them as the job moves along",
                 "Pull requests make review, comments and merging easy to follow",
                 "Free private repositories, and built-in CI with GitHub Actions",
                 "Almost every tool, hosting service and teammate already knows it"],
        "watch": ["Cloud-hosted: running your own server needs the paid Enterprise edition",
                  "Some team controls (protected branches on private repositories, for one) need a paid plan",
                  "Needs the GitHub CLI installed and signed in on this computer"],
        "steps": ["Make a free account at **github.com** if you don't have one.",
                  "Install the GitHub CLI: `brew install gh`.",
                  "Press **Sign in to GitHub** below and approve the code it shows.",
                  "Press **Create a GitHub repository** (it names it after this project and pushes your code), or **Connect an existing one** if the repository already exists.",
                  "Orchestrator creates its job labels and opens draft pull requests from then on."],
        "docs": "https://docs.github.com/en/get-started/start-your-journey/hello-world",
    },
    {
        "key": "gitlab", "label": "GitLab", "action": "connect", "workflow": "Git branches, with a link to open a merge request",
        "tagline": "Source control with CI/CD, issue boards and project tracking in one product.",
        "good": ["Built-in CI/CD pipelines, issue boards and a container registry in one place",
                 "You can run it yourself for free (the Community Edition), or use gitlab.com",
                 "Fine-grained permissions suit larger teams"],
        "watch": ["Orchestrator uses plain Git here: jobs work on branches and merge on this computer, with a link to open a merge request, not automatic issues",
                  "The interface has a lot in it and takes longer to learn",
                  "Self-hosting means you maintain the server"],
        "steps": ["Sign in at **gitlab.com** (or your company's GitLab) and choose **New project, Create blank project**. Leave \"Initialize with a README\" off.",
                  "Set up access from this computer: either an SSH key (`ssh-keygen -t ed25519`, then add the public key under Preferences, SSH Keys) or a personal access token for HTTPS.",
                  "Copy the project's **clone URL** from the Clone button.",
                  "Press **Connect a repository** below, paste the URL, and save.",
                  "Press **Push** under Advanced Git tools to upload your code."],
        "docs": "https://docs.gitlab.com/ee/tutorials/make_first_git_commit/",
    },
    {
        "key": "bitbucket", "label": "Bitbucket", "action": "connect", "workflow": "Git branches, with a link to open a pull request",
        "tagline": "Atlassian's Git hosting, a good fit if your team already lives in Jira.",
        "good": ["Works closely with Jira and Trello (a branch or pull request can update its ticket)",
                 "Free for small teams, with built-in pipelines",
                 "Simple and quick to set up"],
        "watch": ["A smaller ecosystem than GitHub or GitLab",
                  "Orchestrator uses plain Git here: branches and local merges, with a link to open a pull request",
                  "Pricing is per user as a team grows"],
        "steps": ["Sign in at **bitbucket.org** and choose **Create, Repository**. Leave the README option off.",
                  "Set up access: add an SSH key under Personal settings, SSH keys, or create an app password for HTTPS.",
                  "Copy the repository's **clone URL**.",
                  "Press **Connect a repository** below, paste the URL, and save.",
                  "Press **Push** under Advanced Git tools to upload your code."],
        "docs": "https://support.atlassian.com/bitbucket-cloud/docs/create-a-git-repository/",
    },
    {
        "key": "azure", "label": "Azure DevOps", "action": "connect", "workflow": "Git branches, with a link to open a pull request",
        "tagline": "Microsoft's platform for repositories, boards and pipelines, common in larger companies.",
        "good": ["Boards, repositories, pipelines and test plans together, with enterprise sign-in and controls",
                 "Fits teams already on Azure or Microsoft 365",
                 "Free for small teams"],
        "watch": ["More moving parts: an organization, then a project, then the repository",
                  "Orchestrator uses plain Git here: branches and local merges",
                  "Less common outside Microsoft-centred teams, so fewer examples to lean on"],
        "steps": ["Sign in at **dev.azure.com**, pick or create an **organization**, then a **project**.",
                  "Open **Repos** and create a repository (or use the one the project starts with).",
                  "Set up access: add an SSH key under User settings, SSH public keys, or create a personal access token for HTTPS.",
                  "Copy the repository's **clone URL** from the Clone button.",
                  "Press **Connect a repository** below, paste the URL, and save, then **Push**."],
        "docs": "https://learn.microsoft.com/en-us/azure/devops/repos/git/create-new-repo",
    },
    {
        "key": "other", "label": "Another Git host", "action": "connect", "workflow": "Git branches and local merges",
        "tagline": "Gitea, Forgejo, a company server, or any host that gives you a Git address.",
        "good": ["You choose where the code lives, including a server you run yourself",
                 "Gitea and Forgejo are light and free to host",
                 "Anything that speaks Git works"],
        "watch": ["You look after the server, its backups and its access control",
                  "Orchestrator uses plain Git: branches and local merges, without issues or pull requests",
                  "You need a working address and credentials before you start"],
        "steps": ["Create an empty repository on your host (no README), and note its **clone URL**.",
                  "Make sure this computer can reach it: add an SSH key to your account there, or have a token ready for HTTPS.",
                  "Press **Connect a repository** below, paste the URL, and save.",
                  "Press **Push** under Advanced Git tools to upload your code."],
        "docs": "https://git-scm.com/book/en/v2/Git-on-the-Server-Setting-Up-the-Server",
    },
    {
        "key": "local", "label": "On this computer only", "action": "local", "workflow": "Git branches and local merges",
        "tagline": "Git history on this Mac, with no remote. You can connect one later.",
        "good": ["Nothing to set up, no account, works offline",
                 "Private by default",
                 "Jobs still get their own branches, and you can undo and compare freely"],
        "watch": ["There is no backup: if this computer's disk fails, the code and its history are gone",
                  "You can't share it, review it from elsewhere, or build on another machine",
                  "No pull requests, so you review each job on its own page here"],
        "steps": ["If this folder isn't a repository yet, press **Initialize repository**.",
                  "Make sure there is a first commit (Orchestrator includes it when it initializes).",
                  "Press **Use this computer only** below.",
                  "Back the folder up some other way, and connect a host here whenever you're ready."],
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
