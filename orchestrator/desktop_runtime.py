"""Deterministic, immutable bundle environment shared by native/service launches."""
from __future__ import annotations

import os
from pathlib import Path


def is_packaged_install() -> bool:
    return bool(os.environ.get("ORCHESTRATOR_PACKAGED_APP"))


def bundle_environment(bundle: Path, user_paths: list[Path]) -> dict[str, str]:
    for path in user_paths:
        if not path.is_absolute() or ":" in str(path) or "\x00" in str(path):
            raise ValueError("Choose an absolute tool directory without ':' characters.")
    resources = bundle.resolve() / "Contents/Resources"
    certificate = resources / "certificates/cacert.pem"
    if not certificate.is_file():
        raise ValueError("The bundled TLS certificates are missing. Reinstall Orchestrator.")
    paths = [resources / "bin", resources / "runtime/bin"]
    paths.extend(map(Path, ["/usr/bin", "/bin", "/usr/sbin", "/sbin", "/opt/homebrew/bin", "/usr/local/bin"]))
    paths.append(Path.home() / ".local/bin")
    paths.extend(user_paths)
    return {"PATH": ":".join(dict.fromkeys(map(str, paths))), "ORCHESTRATOR_PACKAGED_APP": str(bundle.resolve()),
            "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1", "PYTHONSAFEPATH": "1",
            "SSL_CERT_FILE": str(certificate), "REQUESTS_CA_BUNDLE": str(certificate)}
