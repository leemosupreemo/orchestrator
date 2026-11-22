"""Local Ollama model catalog, registry manifest size lookup, and pull/delete helpers."""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

DEFAULT_HOST = "http://127.0.0.1:11434"
REGISTRY_BASE = "https://registry.ollama.ai/v2"
MANIFEST_ACCEPT = "application/vnd.docker.distribution.manifest.v2+json, application/vnd.oci.image.manifest.v1+json, application/json"
TAG_RE = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+){0,2}(?::[A-Za-z0-9._-]+)?$")
SIZE_CACHE_TTL = 3600.0
MIN_DISK_HEADROOM_BYTES = 15 * (1024 ** 3)  # keep at least 15 GiB free for builds

# Curated local models for coding and general assistant tasks (fallback_bytes used only if registry lookup fails).
CURATED_MODELS: list[dict[str, Any]] = [
    {"tag": "qwen2.5-coder:3b", "summary": "Fast lightweight coding model", "fallback_bytes": int(1.9 * 1e9), "min_ram_gb": 8, "coding": True},
    {"tag": "qwen2.5-coder:7b", "summary": "Balanced local coding model", "fallback_bytes": int(4.7 * 1e9), "min_ram_gb": 16, "coding": True},
    {"tag": "qwen2.5-coder:14b", "summary": "Strong local coding & debugging", "fallback_bytes": int(9.0 * 1e9), "min_ram_gb": 24, "coding": True},
    {"tag": "qwen2.5-coder:32b", "summary": "High-accuracy local coding (needs 48GB+ RAM)", "fallback_bytes": int(20.0 * 1e9), "min_ram_gb": 48, "coding": True},
    {"tag": "qwen3:8b", "summary": "General reasoning & coding", "fallback_bytes": int(5.2 * 1e9), "min_ram_gb": 16, "coding": True},
    {"tag": "qwen3:14b", "summary": "Stronger general reasoning & coding", "fallback_bytes": int(9.3 * 1e9), "min_ram_gb": 24, "coding": True},
    {"tag": "deepseek-r1:8b", "summary": "Local reasoning model", "fallback_bytes": int(4.9 * 1e9), "min_ram_gb": 16, "coding": False},
    {"tag": "deepseek-r1:14b", "summary": "Deeper local reasoning model", "fallback_bytes": int(9.0 * 1e9), "min_ram_gb": 24, "coding": False},
    {"tag": "gemma3:4b", "summary": "Compact fast general model", "fallback_bytes": int(3.3 * 1e9), "min_ram_gb": 8, "coding": False},
    {"tag": "gemma3:12b", "summary": "Capable mid-size general model", "fallback_bytes": int(8.1 * 1e9), "min_ram_gb": 24, "coding": False},
    {"tag": "llama3.1:8b", "summary": "Popular 8B general model", "fallback_bytes": int(4.9 * 1e9), "min_ram_gb": 16, "coding": False},
]

_size_cache: dict[str, tuple[float, int]] = {}


def validate_tag(tag: str) -> str:
    clean = str(tag or "").strip()
    if not clean or len(clean) > 160 or not TAG_RE.fullmatch(clean):
        raise ValueError("Enter a valid Ollama model tag (for example, qwen2.5-coder:7b).")
    return clean


def _manifest_url(tag: str) -> str:
    clean = validate_tag(tag)
    name, _, variant = clean.partition(":")
    variant = variant or "latest"
    repo = name if "/" in name else f"library/{name}"
    return f"{REGISTRY_BASE}/{repo}/manifests/{variant}"


def manifest_size_bytes(tag: str, *, timeout: float = 4.0) -> int | None:
    """Return total packaged size in bytes from Ollama's registry manifest (config + layers)."""
    clean = validate_tag(tag)
    now = time.time()
    cached = _size_cache.get(clean)
    if cached and now - cached[0] < SIZE_CACHE_TTL:
        return cached[1]
    req = urllib.request.Request(_manifest_url(clean), headers={"Accept": MANIFEST_ACCEPT, "User-Agent": "swift-orchestrator"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except (OSError, ValueError, urllib.error.URLError):
        return None
    if not isinstance(data, dict):
        return None
    total = 0
    config = data.get("config")
    if isinstance(config, dict) and isinstance(config.get("size"), int):
        total += config["size"]
    for layer in data.get("layers") or []:
        if isinstance(layer, dict) and isinstance(layer.get("size"), int):
            total += layer["size"]
    if total <= 0:
        return None
    _size_cache[clean] = (now, total)
    return total


def _normalize_host(host: str | None) -> str:
    base = str(host or "").strip().rstrip("/")
    if not base:
        return DEFAULT_HOST
    if not base.startswith(("http://", "https://")):
        base = f"http://{base}"
    return base


def installed_models(host: str | None = None, *, timeout: float = 3.0) -> list[dict[str, Any]]:
    """List models currently installed in Ollama via GET /api/tags."""
    url = f"{_normalize_host(host)}/api/tags"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
        models = payload.get("models") if isinstance(payload, dict) else []
        out = []
        for m in models or []:
            if not isinstance(m, dict):
                continue
            name = str(m.get("name") or m.get("model") or "").strip()
            if not name:
                continue
            out.append({
                "tag": name,
                "size_bytes": int(m.get("size") or 0),
                "modified_at": str(m.get("modified_at") or ""),
            })
        return out
    except (OSError, ValueError, urllib.error.URLError):
        return []


def catalog(host: str | None = None, *, fetch_sizes: bool = True, disk_root: Path | None = None) -> dict[str, Any]:
    """Return installed models and curated downloadable models with manifest sizes."""
    installed = installed_models(host)
    installed_tags = {m["tag"] for m in installed}
    installed_tags |= {m["tag"].removesuffix(":latest") for m in installed}

    sizes: dict[str, int | None] = {}
    if fetch_sizes:
        with ThreadPoolExecutor(max_workers=6) as pool:
            futs = {pool.submit(manifest_size_bytes, item["tag"]): item["tag"] for item in CURATED_MODELS}
            for fut in as_completed(futs):
                tag = futs[fut]
                try:
                    sizes[tag] = fut.result()
                except Exception:
                    sizes[tag] = None

    available = []
    for item in CURATED_MODELS:
        tag = item["tag"]
        live_size = sizes.get(tag)
        size_bytes = live_size if live_size is not None else item["fallback_bytes"]
        available.append({
            "tag": tag,
            "summary": item["summary"],
            "size_bytes": size_bytes,
            "size_source": "registry" if live_size is not None else "estimate",
            "installed": tag in installed_tags or f"{tag}:latest" in installed_tags,
        })

    free_bytes = None
    try:
        free_bytes = shutil.disk_usage(disk_root or Path.home()).free
    except OSError:
        pass

    specs = machine_specs(disk_root=disk_root)
    return {
        "installed": installed,
        "available": available,
        "free_bytes": free_bytes,
        "specs": specs,
        "ollama_installed": shutil.which("ollama") is not None,
    }


def machine_specs(disk_root: Path | None = None) -> dict[str, Any]:
    """Detect total RAM, CPU/chip, and free disk space on this computer."""
    ram_bytes = None
    try:
        if platform.system() == "Darwin":
            out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=2, check=False).stdout.strip()
            if out.isdigit():
                ram_bytes = int(out)
        if ram_bytes is None and hasattr(os, "sysconf"):
            pages = os.sysconf("SC_PHYS_PAGES")
            page_size = os.sysconf("SC_PAGE_SIZE")
            if isinstance(pages, int) and isinstance(page_size, int) and pages > 0 and page_size > 0:
                ram_bytes = pages * page_size
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    chip = ""
    try:
        if platform.system() == "Darwin":
            chip = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True, timeout=2, check=False).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    free_bytes = None
    try:
        free_bytes = shutil.disk_usage(disk_root or Path.home()).free
    except OSError:
        pass
    return {
        "ram_bytes": ram_bytes,
        "ram_gb": round(ram_bytes / (1024 ** 3)) if ram_bytes else None,
        "free_bytes": free_bytes,
        "chip": chip or platform.machine(),
    }


def recommendations(host: str | None = None, *, disk_root: Path | None = None, limit: int = 2) -> dict[str, Any]:
    """Recommend free local Ollama models that fit this computer's RAM and leave 15 GiB free for builds."""
    specs = machine_specs(disk_root=disk_root)
    ram_gb = specs.get("ram_gb") or 16
    free_bytes = specs.get("free_bytes")
    ollama_ok = shutil.which("ollama") is not None
    installed = installed_models(host) if ollama_ok else []
    installed_tags = {m["tag"] for m in installed}
    installed_tags |= {m["tag"].removesuffix(":latest") for m in installed}

    candidates = []
    for item in CURATED_MODELS:
        tag = item["tag"]
        if tag in installed_tags or f"{tag}:latest" in installed_tags:
            continue
        size_bytes = (_size_cache.get(tag) or (0, item["fallback_bytes"]))[1]
        if item["min_ram_gb"] > ram_gb:
            continue
        if free_bytes is not None and (free_bytes - size_bytes) < MIN_DISK_HEADROOM_BYTES:
            continue
        candidates.append({
            "tag": tag,
            "summary": item["summary"],
            "size_bytes": size_bytes,
            "min_ram_gb": item["min_ram_gb"],
            "coding": bool(item.get("coding")),
        })
    # Prefer the strongest coding models that comfortably fit this machine's RAM and disk.
    candidates.sort(key=lambda m: (m["coding"], m["min_ram_gb"], m["size_bytes"]), reverse=True)
    return {
        "specs": specs,
        "ollama_installed": ollama_ok,
        "installed_count": len(installed),
        "recommendations": candidates[:limit],
    }


def pull_model(tag: str, *, timeout: int = 1800) -> dict[str, Any]:
    """Download a model locally via `ollama pull <tag>`."""
    clean = validate_tag(tag)
    if not shutil.which("ollama"):
        raise RuntimeError("Ollama is not installed on this computer.")
    res = subprocess.run(["ollama", "pull", clean], capture_output=True, text=True, timeout=timeout, check=False)
    if res.returncode != 0:
        err = (res.stderr or res.stdout or "").strip().splitlines()
        raise RuntimeError(err[-1] if err else f"Failed to download {clean}.")
    return {"ok": True, "tag": clean, "message": f"Downloaded {clean}"}


def delete_model(tag: str, *, timeout: int = 60) -> dict[str, Any]:
    """Remove an installed model via `ollama rm <tag>`."""
    clean = validate_tag(tag)
    if not shutil.which("ollama"):
        raise RuntimeError("Ollama is not installed on this computer.")
    res = subprocess.run(["ollama", "rm", clean], capture_output=True, text=True, timeout=timeout, check=False)
    if res.returncode != 0:
        err = (res.stderr or res.stdout or "").strip().splitlines()
        raise RuntimeError(err[-1] if err else f"Failed to remove {clean}.")
    return {"ok": True, "tag": clean, "message": f"Removed {clean}"}
