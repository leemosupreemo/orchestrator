import hashlib
import importlib.util
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path

PATH = Path(__file__).resolve().parents[1] / "packaging/macos/build.py"
spec = importlib.util.spec_from_file_location("macos_build", PATH)
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


class MacOSPackagingTests(unittest.TestCase):
    def test_manifest_has_exact_verified_inputs_for_both_architectures(self):
        manifest = build.load_manifest(PATH.with_name("dependencies.json"))
        for architecture in ("arm64", "x86_64"):
            for dependency in (manifest[architecture]["python"], manifest[architecture]["cloudflared"], manifest["certificates"]):
                self.assertTrue(dependency["url"].startswith("https://"))
                self.assertNotIn("latest", dependency["url"])
                self.assertRegex(dependency["sha256"], r"^[a-f0-9]{64}$")
                self.assertNotEqual(dependency["sha256"], "0" * 64)
                self.assertTrue(dependency["license"])

    def test_archive_traversal_and_link_escape_refused_before_extraction(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name, link in [("../outside", ""), ("python/link", "../../outside")]:
                archive = root / "bad.tar.gz"
                with tarfile.open(archive, "w:gz") as tar:
                    info = tarfile.TarInfo(name)
                    if link:
                        info.type = tarfile.SYMTYPE; info.linkname = link
                    else:
                        info.size = 4
                    tar.addfile(info, None if link else io.BytesIO(b"data"))
                with self.assertRaises(ValueError):
                    build.extract_archive(archive, root / "extracted")
            self.assertFalse((root / "outside").exists())

    def test_wrong_digest_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "payload"
            path.write_bytes(b"payload")
            with self.assertRaises(ValueError):
                build.verify_digest(path, "1" * 64)
            build.verify_digest(path, hashlib.sha256(b"payload").hexdigest())

    def test_release_identity_validation_precedes_build(self):
        for arch, version, number in [("other", "0.1.0", 1), ("arm64", "", 1), ("arm64", "0.1.0", 0)]:
            with self.assertRaises(ValueError):
                build.build_bundle(arch, Path("/unused"), PATH.with_name("dependencies.json"), version, number)

    def test_wheel_source_allowlist_excludes_runtime_and_secrets(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source"
            (source / "orchestrator/web/static").mkdir(parents=True)
            (source / "orchestrator/config").mkdir()
            (source / "orchestrator/__init__.py").write_text("")
            (source / "orchestrator/web/static/app.js").write_text("fixture")
            (source / "orchestrator/config/key.pem").write_text("private secret")
            (source / "orchestrator/audit.jsonl").write_text("secret log")
            (source / "pyproject.toml").write_text('[project]\nversion = "0.1.0"\n')
            dest = root / "wheel-source"
            build.stage_source(source, dest, "0.2.0")
            self.assertTrue((dest / "orchestrator/web/static/app.js").exists())
            self.assertFalse((dest / "orchestrator/config/key.pem").exists())
            self.assertFalse((dest / "orchestrator/audit.jsonl").exists())
            self.assertIn('version = "0.2.0"', (dest / "pyproject.toml").read_text())

    def test_pure_wheel_installs_without_executing_foreign_python(self):
        import zipfile
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            wheel = root / "orchestrator-0.2.0-py3-none-any.whl"
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr("orchestrator/__init__.py", '__version__ = "0.2.0"')
                archive.writestr("orchestrator-0.2.0.dist-info/WHEEL", "Root-Is-Purelib: true\nTag: py3-none-any\n")
                archive.writestr("orchestrator-0.2.0.dist-info/METADATA", "Name: orchestrator\nVersion: 0.2.0\n")
            runtime = root / "runtime"
            build.install_wheel(wheel, runtime)
            self.assertTrue((runtime / "lib/python3.12/site-packages/orchestrator/__init__.py").is_file())
            self.assertIn('python3', (runtime / "bin/orchestrator").read_text())
