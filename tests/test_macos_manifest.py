import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

PATH = Path(__file__).resolve().parents[1] / "packaging/macos/prepare_manifest.py"
spec = importlib.util.spec_from_file_location("macos_manifest", PATH)
manifest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manifest)


class MacManifestTests(unittest.TestCase):
    def test_only_matching_release_images_and_acceptance_can_be_published(self):
        commit = "a" * 40
        acceptance = {"commit": commit, **{a: {"passed": True, "notes": "fixture acceptance"} for a in ("arm64", "x86_64")}}
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            receipts = {}
            for arch in ("arm64", "x86_64"):
                filename = f"Orchestrator-0.2.0-{arch}.dmg"
                payload = arch.encode()
                (folder / filename).write_bytes(payload)
                receipts[arch] = {"architecture": arch, "version": "0.2.0", "build": 2,
                    "development": False, "commit": commit, "file": filename, "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
                (folder / f"{arch}.json").write_text(json.dumps(receipts[arch]))
            result = manifest.prepare(folder, "https://example.com/releases", commit, acceptance)
            self.assertEqual(result["build"], 2)
            self.assertEqual(result["arm64"]["url"], "https://example.com/releases/Orchestrator-0.2.0-arm64.dmg")
            for evidence in ({}, {**acceptance, "commit": "b" * 40}, {**acceptance, "arm64": {"passed": False}}):
                with self.assertRaises(ValueError):
                    manifest.prepare(folder, "https://example.com/releases", commit, evidence)
            for change in ({"development": True}, {"commit": "b" * 40}, {"build": 3}, {"file": "../outside.dmg"}, {"sha256": "0" * 64}):
                (folder / "arm64.json").write_text(json.dumps({**receipts["arm64"], **change}))
                with self.assertRaises(ValueError):
                    manifest.prepare(folder, "https://example.com/releases", commit, acceptance)
            (folder / "arm64.json").write_text(json.dumps(receipts["arm64"]))
            for url in ("http://example.com", "https://user:secret@example.com", "https://example.com/?token=x"):
                with self.assertRaises(ValueError):
                    manifest.prepare(folder, url, commit, acceptance)
