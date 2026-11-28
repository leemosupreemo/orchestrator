import tempfile
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator import blockers, disk_space


class DiskSpaceTests(unittest.TestCase):
    def test_builder_floor_uses_the_tightest_volume(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            values = [20 * disk_space.GIB, 18 * disk_space.GIB, 9 * disk_space.GIB]
            with patch.object(disk_space.shutil, "disk_usage", side_effect=[type("Usage", (), {"free": n}) for n in values]):
                report = disk_space.status(root, root / "runtime", minimum_gib=15)
            self.assertEqual(report["free_bytes"], values[-1])
            self.assertEqual(report["needed_bytes"], 6 * disk_space.GIB)
            self.assertEqual(report["volume"], "Temporary files")

    def test_disk_errors_are_machine_blockers(self):
        for reason in ("ENOSPC: write failed", "Not enough disk space to start the build", "The disk is full"):
            with self.subTest(reason=reason):
                self.assertEqual(blockers.detect(reason)["kind"], "disk_full")

    def test_cleanup_only_removes_selected_generated_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            caches = root / "DerivedData"
            caches.mkdir()
            keep = caches / "Unrecognized"
            keep.mkdir()
            for name in ("A", "B"):
                path = caches / name
                path.mkdir()
                (path / "info.plist").touch()
            (caches / "Link").symlink_to(caches / "B", target_is_directory=True)
            with patch.object(disk_space, "derived_data_root", return_value=caches), \
                 patch.object(Path, "home", return_value=root), \
                 patch.object(disk_space, "tool_is_active", return_value=False):
                candidates = disk_space.cache_candidates(root)
                self.assertEqual({item["name"] for item in candidates}, {"A", "B"})
                selected = next(item["id"] for item in candidates if item["name"] == "A")
                with self.assertRaises(ValueError):
                    disk_space.clear_caches(root, ["../user-files"])
                removed, errors = disk_space.clear_caches(root, [selected])
            self.assertEqual(removed, ["A"])
            self.assertFalse(errors)
            self.assertTrue((caches / "B").exists())
            self.assertTrue(keep.exists())

    def test_recognizes_unity_gradle_and_swiftpm_and_preserves_source(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)

            def git(*args):
                return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)

            git("init")
            (root / ".gitignore").write_text("Library/\n.gradle/\n.build/\n")
            git("add", ".gitignore")
            for path in ("Game/Library", "Game/ProjectSettings", "Game/Assets",
                         "Android/.gradle", "Tracked/.gradle",
                         ".build/arm64-apple-macosx/debug", ".build/checkouts"):
                (root / path).mkdir(parents=True)
            (root / "Game/ProjectSettings/ProjectVersion.txt").touch()
            (root / "Android/settings.gradle.kts").touch()
            (root / "Tracked/settings.gradle").touch()
            (root / "Tracked/.gradle/source").touch()
            git("add", "-f", "Tracked/.gradle/source")
            (root / "Package.swift").touch()
            with patch.object(disk_space, "derived_data_root", return_value=root / "missing"), \
                 patch.object(Path, "home", return_value=root):
                candidates = disk_space.cache_candidates(root)
                self.assertEqual({item["name"] for item in candidates},
                                 {"Game/Library", "Android/.gradle", ".build/arm64-apple-macosx/debug"})
                by_tool = {item["tool"]: item["id"] for item in candidates}
                with patch.object(disk_space, "tool_is_active", side_effect=lambda tool: tool == "Unity"):
                    with self.assertRaisesRegex(disk_space.CacheBusyError, "Unity"):
                        disk_space.clear_caches(root, [by_tool["Unity"], by_tool["Gradle"]])
                    self.assertTrue((root / "Android/.gradle").exists())
                    disk_space.clear_caches(root, [by_tool["Gradle"], by_tool["SwiftPM"]])
            self.assertTrue((root / "Tracked/.gradle/source").exists())
            self.assertTrue((root / "Game/Assets").exists())
            self.assertTrue((root / ".build/checkouts").exists())
            self.assertFalse((root / "Android/.gradle").exists())
            self.assertFalse((root / ".build/arm64-apple-macosx/debug").exists())

    def test_vscode_cleanup_leaves_workspace_history_and_backups(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            profile = root / "Library" / "Application Support" / "Code"
            for name in ("GPUCache", "Backups", "User/History", "User/workspaceStorage"):
                (profile / name).mkdir(parents=True)
            (profile / "Code Cache").symlink_to(profile / "Backups", target_is_directory=True)
            with patch.object(Path, "home", return_value=root), \
                 patch.object(disk_space, "derived_data_root", return_value=root / "missing"), \
                 patch.object(disk_space, "tool_is_active", return_value=False):
                candidates = disk_space.cache_candidates(root)
                self.assertEqual([(item["tool"], item["name"]) for item in candidates], [("VS Code", "Code/GPUCache")])
                disk_space.clear_caches(root, [candidates[0]["id"]])
            self.assertFalse((profile / "GPUCache").exists())
            for name in ("Backups", "User/History", "User/workspaceStorage"):
                self.assertTrue((profile / name).exists())

    def test_recognizes_dev_tool_caches_like_npm_homebrew_and_pip(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / ".npm" / "_cacache").mkdir(parents=True)
            (root / "Library" / "Caches" / "Homebrew").mkdir(parents=True)
            (root / ".cache" / "pip").mkdir(parents=True)
            with patch.object(Path, "home", return_value=root), \
                 patch.object(disk_space, "derived_data_root", return_value=root / "missing"):
                candidates = disk_space.cache_candidates(root)
                self.assertEqual({item["name"] for item in candidates}, {"npm cache", "Homebrew downloads", "pip cache"})
                removed, errors = disk_space.clear_caches(root, [item["id"] for item in candidates])
            self.assertFalse(errors)
            self.assertEqual(set(removed), {"npm cache", "Homebrew downloads", "pip cache"})

    def test_separates_very_safe_from_usually_safe_and_filters_small_entries(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / ".npm" / "_cacache").mkdir(parents=True)
            (root / ".gradle" / "caches").mkdir(parents=True)
            (root / ".nuget" / "packages").mkdir(parents=True)
            (root / ".npm" / "_cacache" / "blob").write_bytes(b"x" * (2 * disk_space.MIB))
            (root / ".gradle" / "caches" / "tiny").write_bytes(b"x" * 1024)
            (root / ".nuget" / "packages" / "pkg").write_bytes(b"x" * (2 * disk_space.MIB))
            with patch.object(Path, "home", return_value=root), \
                 patch.object(disk_space, "derived_data_root", return_value=root / "missing"):
                all_items = disk_space.cache_candidates(root)
                by_name = {item["name"]: item["safety"] for item in all_items}
                self.assertEqual(by_name["npm cache"], "very_safe")
                self.assertEqual(by_name["Gradle global cache"], "usually_safe")
                self.assertEqual(by_name["NuGet packages cache"], "usually_safe")
                filtered = disk_space.cache_candidates(root, min_bytes=disk_space.MIB)
                self.assertEqual({item["name"] for item in filtered}, {"npm cache", "NuGet packages cache"})

    def test_unity_active_check_ignores_background_licensing_daemon(self):
        seen = []

        def fake_pgrep(args, **kwargs):
            seen.append(args)
            return subprocess.CompletedProcess(args, 1, b"", b"")

        with patch.object(disk_space.subprocess, "run", side_effect=fake_pgrep):
            self.assertFalse(disk_space.tool_is_active("Unity"))
        self.assertEqual(len(seen), 1)
        pattern = seen[0][-1]
        self.assertIn("Unity", pattern)
        self.assertIn("UnityShaderCompiler", pattern)
        self.assertIn("UnityPackageManager", pattern)
        self.assertNotIn("Licensing", pattern)

    def test_clear_unity_cache_succeeds_when_editor_closed_and_blocks_when_active(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
            (root / ".gitignore").write_text("Game/Library/\n")
            subprocess.run(["git", "add", ".gitignore"], cwd=root, check=True, capture_output=True)
            for path in ("Game/Library", "Game/ProjectSettings", "Game/Assets"):
                (root / path).mkdir(parents=True)
            (root / "Game/ProjectSettings/ProjectVersion.txt").touch()
            with patch.object(disk_space, "derived_data_root", return_value=root / "missing"), \
                 patch.object(Path, "home", return_value=root):
                candidates = disk_space.cache_candidates(root)
                unity_id = next(c["id"] for c in candidates if c["tool"] == "Unity")
                real_run = subprocess.run

                def pgrep_only(rc):
                    def _run(args, *a, **kw):
                        if args and args[0] == "pgrep":
                            return subprocess.CompletedProcess(args, rc, b"123\n" if rc == 0 else b"", b"")
                        return real_run(args, *a, **kw)
                    return _run

                # Active Unity editor/compiler/package manager blocks removal.
                with patch.object(disk_space.subprocess, "run", side_effect=pgrep_only(0)):
                    with self.assertRaisesRegex(disk_space.CacheBusyError, "Finish running Unity before clearing these caches"):
                        disk_space.clear_caches(root, [unity_id])
                self.assertTrue((root / "Game/Library").exists())
                # Once no Unity editor/compiler/package manager matches pgrep, cleanup succeeds.
                with patch.object(disk_space.subprocess, "run", side_effect=pgrep_only(1)):
                    removed, errors = disk_space.clear_caches(root, [unity_id])
                self.assertEqual(removed, ["Game/Library"])
                self.assertEqual(errors, [])
                self.assertFalse((root / "Game/Library").exists())
