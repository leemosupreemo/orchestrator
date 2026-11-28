"""What a pasted package link is: its name, ecosystem, latest version and what it does, read from where it lives.

Like Xcode adding a Swift package from a repository URL, but for any ecosystem:

- a registry page (npm, PyPI, crates.io, RubyGems, pub.dev, CocoaPods, pkg.go.dev, Maven, OpenUPM) asks that registry's API;
- any Git repository (GitHub, GitLab, Bitbucket, a self-hosted server, an SSH address) is read with git itself: its tags give the
  latest version, and the manifests at its top (Package.swift, a podspec, package.json, Cargo.toml, pyproject.toml, go.mod,
  pubspec.yaml, a gemspec, a Gradle or Maven build) say which ecosystem it's for and what the package is called.

Only https is fetched, nothing is run from the repository, and every step has a short time limit. What can't be found is left
blank for the person to fill in.
"""
from __future__ import annotations

import json
import os
import re
import ssl
import subprocess
import tempfile
import tomllib
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

from orchestrator.packages import from_url

TIMEOUT = 15
USER_AGENT = "Orchestrator package lookup (https://github.com/leemosupreemo/orchestrator)"

# Manifests at the top of a repository, in the order they decide the ecosystem (a Swift package with a podspec is SwiftPM first).
MANIFESTS = ("Package.swift", "*.podspec", "package.json", "Cargo.toml", "pyproject.toml", "setup.py", "go.mod", "pubspec.yaml",
             "*.gemspec", "build.gradle.kts", "build.gradle", "pom.xml")


class PackageLookupError(ValueError):
    pass


def _ssl() -> ssl.SSLContext:
    try:
        import certifi  # Python from python.org has no certificates of its own until its installer step is run
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def fetch_json(url: str) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=TIMEOUT, context=_ssl()) as reply:  # noqa: S310 (https only, built below)
        return json.loads(reply.read(3_000_000))


def git(*args: str, cwd: str | None = None) -> str:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ALLOW_PROTOCOL": "https", "GCM_INTERACTIVE": "never"}
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=TIMEOUT * 2, env=env, stdin=subprocess.DEVNULL)
    if done.returncode != 0:
        raise PackageLookupError((done.stderr or done.stdout).strip().splitlines()[-1:][0] if (done.stderr or done.stdout).strip() else "git failed")
    return done.stdout


def repository_url(text: str) -> str:
    """An https address for a Git repository written any common way: a web page of it, `git@host:owner/repo.git`, or
    `host/owner/repo`. Empty when it doesn't look like one."""
    text = text.strip()
    ssh = re.fullmatch(r"(?:ssh://)?git@([\w.-]+)[:/](.+?)(?:\.git)?/?", text)
    if ssh:
        return f"https://{ssh.group(1)}/{ssh.group(2)}"
    if not re.match(r"https?://", text):
        if not re.match(r"[\w-]+(\.[\w-]+)+/", text):
            return ""
        text = "https://" + text
    parts = urllib.parse.urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return ""
    path = parts.path.rstrip("/")
    path = re.sub(r"\.git$", "", path)
    # A page inside a repository on the common hosts (…/tree/main/x, …/-/blob/…, …/src/…): the repository is its first two parts.
    if parts.hostname in ("github.com", "www.github.com", "gitlab.com", "bitbucket.org", "codeberg.org"):
        bits = [b for b in path.split("/") if b]
        if parts.hostname == "gitlab.com" and "-" in bits:
            bits = bits[:bits.index("-")]
        elif len(bits) > 2:
            bits = bits[:2]
        if len(bits) < 2:
            return ""
        path = "/" + "/".join(bits)
    return f"https://{parts.hostname.replace('www.', '', 1) if parts.hostname == 'www.github.com' else parts.hostname}{path}"


def _version_key(tag: str) -> tuple[int, ...] | None:
    found = re.fullmatch(r"v?(\d+)\.(\d+)(?:\.(\d+))?", tag)
    return tuple(int(x or 0) for x in found.groups()) if found else None


def latest_tag(tags_output: str) -> str:
    """The newest release tag from `git ls-remote --tags`: plain versions only (1.2.3 or v1.2.3), not betas."""
    tags = [line.rsplit("refs/tags/", 1)[-1] for line in tags_output.splitlines() if "refs/tags/" in line]
    versions = [(key, tag) for tag in tags if (key := _version_key(tag))]
    return max(versions)[1].lstrip("v") if versions else ""


def _first(pattern: str, text: str) -> str:
    found = re.search(pattern, text, re.S)
    return found.group(1).strip() if found else ""


def read_manifest(name: str, text: str) -> dict[str, str]:
    """{ecosystem, name, purpose} from one manifest's text (whatever of them it says)."""
    if name == "Package.swift":
        products = re.findall(r"\.library\(\s*name:\s*\"([^\"]+)\"", text)
        return {"ecosystem": "swiftpm", "name": _first(r"Package\(\s*name:\s*\"([^\"]+)\"", text),
                "products": ", ".join(products[:6])}
    if name.endswith(".podspec"):
        return {"ecosystem": "cocoapods", "name": _first(r"\.name\s*=\s*['\"]([^'\"]+)", text), "purpose": _first(r"\.summary\s*=\s*['\"]([^'\"]+)", text)}
    if name == "package.json":
        try:
            data = json.loads(text)
        except ValueError:
            return {"ecosystem": "npm"}
        return {"ecosystem": "unity" if data.get("unity") else "npm", "name": str(data.get("name") or ""),
                "purpose": str(data.get("description") or ""), "version": str(data.get("version") or "")}
    if name == "Cargo.toml" or name == "pyproject.toml":
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            data = {}
        section = data.get("package" if name == "Cargo.toml" else "project") or {}
        return {"ecosystem": "cargo" if name == "Cargo.toml" else "pip", "name": str(section.get("name") or ""),
                "purpose": str(section.get("description") or "")}
    if name == "setup.py":
        return {"ecosystem": "pip", "name": _first(r"name\s*=\s*['\"]([^'\"]+)", text), "purpose": _first(r"description\s*=\s*['\"]([^'\"]+)", text)}
    if name == "go.mod":
        return {"ecosystem": "go", "name": _first(r"^module\s+(\S+)", text) if text else ""}
    if name == "pubspec.yaml":
        return {"ecosystem": "dart", "name": _first(r"^name:\s*['\"]?([\w-]+)", text), "purpose": _first(r"^description:\s*['\"]?(.+?)['\"]?\s*$", text)}
    if name.endswith(".gemspec"):
        return {"ecosystem": "ruby", "name": _first(r"\.name\s*=\s*['\"]([^'\"]+)", text), "purpose": _first(r"\.summary\s*=\s*['\"]([^'\"]+)", text)}
    if name == "pom.xml":
        group, artifact = _first(r"<groupId>([^<]+)</groupId>", text), _first(r"<artifactId>([^<]+)</artifactId>", text)
        return {"ecosystem": "gradle", "name": f"{group}:{artifact}" if group and artifact else artifact,
                "purpose": _first(r"<description>([^<]+)</description>", text)}
    if name.startswith("build.gradle"):
        return {"ecosystem": "gradle"}
    return {}


def _matches(pattern: str, filename: str) -> bool:
    return filename.endswith(pattern[1:]) if pattern.startswith("*") else filename == pattern


def from_repository(url: str, run: Callable[..., str] = git, prefer: list[str] | None = None) -> dict[str, Any]:
    """Read a Git repository the way Xcode reads a Swift package: its tags for the version, its top-level manifests for the
    rest. Only the top of the newest commit is fetched, without file contents beyond the manifests."""
    found: dict[str, Any] = {"url": url, "version": "", "ecosystems": []}
    try:
        found["version"] = latest_tag(run("ls-remote", "--tags", "--refs", "--", url))
    except subprocess.TimeoutExpired as exc:
        raise PackageLookupError("That repository didn't answer in time. Check the address, or try again.") from exc
    except PackageLookupError as exc:
        if re.search(r"Username|Authentication|not found|Repository not found|403|404", str(exc), re.I):
            raise PackageLookupError("That repository is private or doesn't exist. Check the address; for a private one, type the name and choose the ecosystem.") from exc
        raise PackageLookupError(f"Couldn't reach that repository: {exc}") from exc
    with tempfile.TemporaryDirectory(prefix="orchestrator-pkg-") as tmp:
        try:
            run("clone", "--depth", "1", "--filter=blob:none", "--no-checkout", "--quiet", "--", url, tmp)
            files = run("ls-tree", "--name-only", "HEAD", cwd=tmp).split()
        except (PackageLookupError, subprocess.TimeoutExpired):
            files = []
        readings = []
        for pattern in MANIFESTS:
            for filename in [f for f in files if _matches(pattern, f)][:1]:
                try:
                    readings.append(read_manifest(filename, run("show", f"HEAD:{filename}", cwd=tmp)))
                except (PackageLookupError, subprocess.TimeoutExpired):
                    continue
    readings = [r for r in readings if r.get("ecosystem")]
    found["ecosystems"] = list(dict.fromkeys(r["ecosystem"] for r in readings))
    # The project's own package manager wins when the repository offers it (a Swift package that's also a pod).
    chosen = next((r for r in readings if r["ecosystem"] in (prefer or [])), readings[0] if readings else {})
    path = urllib.parse.urlsplit(url).path.strip("/")
    found["ecosystem"] = chosen.get("ecosystem", "")
    # SwiftPM and Go are added by their address; the others by the name their registry knows them by.
    if found["ecosystem"] == "swiftpm":
        found["name"] = path
        found["products"] = chosen.get("products", "")
    elif found["ecosystem"] == "go":
        found["name"] = chosen.get("name") or f"{urllib.parse.urlsplit(url).hostname}/{path}"
    else:
        found["name"] = chosen.get("name") or path
    found["purpose"] = chosen.get("purpose", "")
    found["version"] = found["version"] or chosen.get("version", "")
    return found


def _github_about(url: str, fetch: Callable[[str], Any]) -> str:
    parts = urllib.parse.urlsplit(url)
    if parts.hostname != "github.com":
        return ""
    try:
        return str(fetch(f"https://api.github.com/repos{parts.path}").get("description") or "")
    except Exception:
        return ""


def from_registry(name: str, ecosystem: str, fetch: Callable[[str], Any] = fetch_json) -> dict[str, Any]:
    """The registry's own record: latest version, description, and the source repository when it says."""
    q = urllib.parse.quote
    out: dict[str, Any] = {"name": name, "ecosystem": ecosystem, "version": "", "purpose": "", "source": ""}
    try:
        if ecosystem == "npm" or ecosystem == "unity":
            base = "https://registry.npmjs.org/" if ecosystem == "npm" else "https://package.openupm.com/"
            data = fetch(base + q(name, safe="@"))
            latest = (data.get("dist-tags") or {}).get("latest", "")
            repo = data.get("repository") or {}
            out.update(version=latest, purpose=data.get("description") or "",
                       source=repo.get("url", "") if isinstance(repo, dict) else str(repo))
        elif ecosystem == "pip":
            info = fetch(f"https://pypi.org/pypi/{q(name)}/json").get("info") or {}
            urls = info.get("project_urls") or {}
            out.update(version=info.get("version") or "", purpose=info.get("summary") or "",
                       source=next((v for k, v in urls.items() if k.lower() in ("source", "repository", "source code", "code")), ""))
        elif ecosystem == "cargo":
            crate = fetch(f"https://crates.io/api/v1/crates/{q(name)}").get("crate") or {}
            out.update(version=crate.get("max_stable_version") or crate.get("max_version") or "", purpose=crate.get("description") or "",
                       source=crate.get("repository") or "")
        elif ecosystem == "ruby":
            data = fetch(f"https://rubygems.org/api/v1/gems/{q(name)}.json")
            out.update(version=data.get("version") or "", purpose=data.get("info") or "", source=data.get("source_code_uri") or "")
        elif ecosystem == "dart":
            latest = fetch(f"https://pub.dev/api/packages/{q(name)}").get("latest") or {}
            spec = latest.get("pubspec") or {}
            out.update(version=latest.get("version") or "", purpose=spec.get("description") or "", source=spec.get("repository") or spec.get("homepage") or "")
        elif ecosystem == "cocoapods":
            data = fetch(f"https://trunk.cocoapods.org/api/v1/pods/{q(name)}")
            versions = [v.get("name", "") for v in data.get("versions") or [] if isinstance(v, dict)]
            keyed = [(k, v) for v in versions if (k := _version_key(v))]
            out["version"] = max(keyed)[1] if keyed else ""
        elif ecosystem == "go":
            out["version"] = str(fetch(f"https://proxy.golang.org/{q(name.lower())}/@latest").get("Version") or "").lstrip("v")
            out["source"] = f"https://{name}" if name.startswith(("github.com/", "gitlab.com/")) else ""
        elif ecosystem == "gradle" and ":" in name:
            group, artifact = name.split(":", 1)
            docs = ((fetch(f"https://search.maven.org/solrsearch/select?q=g:%22{q(group)}%22+AND+a:%22{q(artifact)}%22&rows=1&wt=json")
                     .get("response") or {}).get("docs") or [{}])
            out["version"] = docs[0].get("latestVersion") or ""
    except Exception as exc:  # the registry is down or doesn't know it: keep what the link said
        out["error"] = f"The registry didn't answer ({exc.__class__.__name__}); the name and ecosystem come from the link."
    out["source"] = re.sub(r"^git\+|\.git$", "", str(out.get("source") or ""))
    out["purpose"] = re.sub(r"\s+", " ", str(out.get("purpose") or "")).strip()[:300]
    return out


def lookup(link: str, prefer: list[str] | None = None, fetch: Callable[[str], Any] = fetch_json, run: Callable[..., str] = git) -> dict[str, Any]:
    """Everything that can be found about a pasted link: {name, ecosystem, version, purpose, url, ecosystems, ...}."""
    link = str(link or "").strip()
    if not link:
        raise PackageLookupError("Paste a link to a package or its repository.")
    known = from_url(link if re.match(r"https?://", link) else f"https://{link}")
    if known and known["ecosystem"]:
        found = from_registry(known["name"], known["ecosystem"], fetch)
        found["url"] = link if link.startswith("https://") else f"https://{link}"
        if not found["purpose"] and found.get("source"):
            found["purpose"] = _github_about(repository_url(found["source"]), fetch)
        return found
    repo = repository_url(link)
    if not repo:
        raise PackageLookupError("That doesn't look like a package page or a Git repository. Paste a registry page (npm, PyPI, crates.io…) "
                           "or a repository address (GitHub, GitLab, Bitbucket, or any git URL).")
    found = from_repository(repo, run, prefer)
    if not found["purpose"]:
        found["purpose"] = _github_about(repo, fetch)
    if not found["ecosystem"]:
        found["error"] = "The repository doesn't have a manifest Orchestrator knows at its top level, so choose the ecosystem."
    return found
