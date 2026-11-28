"""Tests for local Ollama model catalog, manifest size lookup, and pull/delete helpers."""
from __future__ import annotations

import io
import json
import unittest
from unittest.mock import patch

from orchestrator import ollama_models


class _FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class OllamaModelsTests(unittest.TestCase):
    def setUp(self):
        ollama_models._size_cache.clear()

    def test_validate_tag_accepts_valid_and_rejects_shell_chars(self):
        self.assertEqual(ollama_models.validate_tag("qwen2.5-coder:7b"), "qwen2.5-coder:7b")
        self.assertEqual(ollama_models.validate_tag("hf.co/unsloth/GLM-4.7:Q4"), "hf.co/unsloth/GLM-4.7:Q4")
        for bad in ("", "bad tag", "qwen;rm -rf /", "a:b:c"):
            with self.assertRaises(ValueError):
                ollama_models.validate_tag(bad)

    def test_manifest_size_sums_config_and_layers(self):
        manifest = {"config": {"size": 1000}, "layers": [{"size": 2_000_000}, {"size": 3_000_000}]}
        with patch.object(ollama_models.urllib.request, "urlopen", return_value=_FakeResp(json.dumps(manifest).encode())):
            size = ollama_models.manifest_size_bytes("qwen2.5-coder:7b")
        self.assertEqual(size, 5_001_000)

    def test_installed_models_reads_api_tags(self):
        payload = {"models": [{"name": "qwen2.5-coder:7b", "size": 4_700_000_000, "modified_at": "2026-11-21"}]}
        with patch.object(ollama_models.urllib.request, "urlopen", return_value=_FakeResp(json.dumps(payload).encode())):
            items = ollama_models.installed_models("http://127.0.0.1:11434")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["tag"], "qwen2.5-coder:7b")
        self.assertEqual(items[0]["size_bytes"], 4_700_000_000)

    def test_catalog_marks_installed_models_and_uses_manifest_sizes(self):
        with patch.object(ollama_models, "installed_models", return_value=[{"tag": "qwen2.5-coder:7b", "size_bytes": 4_700_000_000, "modified_at": ""}]), \
             patch.object(ollama_models, "manifest_size_bytes", return_value=4_683_000_000):
            cat = ollama_models.catalog(fetch_sizes=True)
        by_tag = {m["tag"]: m for m in cat["available"]}
        self.assertTrue(by_tag["qwen2.5-coder:7b"]["installed"])
        self.assertFalse(by_tag["qwen2.5-coder:3b"]["installed"])
        self.assertEqual(by_tag["qwen2.5-coder:7b"]["size_bytes"], 4_683_000_000)
        self.assertEqual(by_tag["qwen2.5-coder:7b"]["size_source"], "registry")

    def test_recommendations_respect_ram_and_15gb_disk_headroom(self):
        # 16 GB RAM, 22 GB free -> can fit ~4.7 GB (leaves > 15 GiB) but NOT 9 GB or 20 GB
        specs = {"ram_bytes": 16 * 1024**3, "ram_gb": 16, "free_bytes": 22 * 1024**3, "chip": "Apple M3"}
        with patch.object(ollama_models, "machine_specs", return_value=specs), \
             patch.object(ollama_models.shutil, "which", return_value="/usr/local/bin/ollama"), \
             patch.object(ollama_models, "installed_models", return_value=[]):
            recs = ollama_models.recommendations(limit=2)
        self.assertTrue(recs["ollama_installed"])
        self.assertEqual(len(recs["recommendations"]), 2)
        for m in recs["recommendations"]:
            self.assertLessEqual(m["min_ram_gb"], 16)
            self.assertGreaterEqual(specs["free_bytes"] - m["size_bytes"], ollama_models.MIN_DISK_HEADROOM_BYTES)

    def test_llm_ollama_command_strips_prefix_and_quotes_tag(self):
        import sys
        import tempfile
        from pathlib import Path
        scripts_dir = str(Path(__file__).resolve().parents[1] / "orchestrator" / "scripts")
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        import llm
        with tempfile.NamedTemporaryFile("w", delete=False) as fh:
            fh.write("hello")
            prompt_file = fh.name
        try:
            cmd = llm._ollama_command("ollama/qwen2.5-coder:7b", prompt_file)
            self.assertIn("ollama run", cmd)
            self.assertIn("qwen2.5-coder:7b", cmd)
            self.assertNotIn("ollama/qwen2.5-coder:7b", cmd)
        finally:
            Path(prompt_file).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
