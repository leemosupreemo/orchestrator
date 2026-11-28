"""Tests for the standalone Swift orchestrator package.

Tests never touch this computer's own Orchestrator state (~/.orchestrator: recent projects, custom models, model lists): the
whole run, and anything it starts, gets a throwaway folder instead. `discover -s tests` doesn't import this package itself,
so test_00_isolation.py imports it first.
"""
import atexit
import os
import shutil
import tempfile

if not os.environ.get("ORCHESTRATOR_USER_STATE_DIR"):
    _STATE = tempfile.mkdtemp(prefix="orchestrator-test-state-")
    os.environ["ORCHESTRATOR_USER_STATE_DIR"] = _STATE
    atexit.register(shutil.rmtree, _STATE, True)
