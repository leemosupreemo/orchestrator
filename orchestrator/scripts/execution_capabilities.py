#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from model_registry import get_model, preferred_cli


class ExecutionMode(str, Enum):
    ADVISORY = "advisory"
    GUIDED = "guided"
    AGENTIC = "agentic"


@dataclass(frozen=True)
class ExecutionProfile:
    model: str
    adapter: str
    machine: str
    execution_modes: tuple[ExecutionMode, ...]
    tools: tuple[str, ...]
    available: bool = True
    supports_sessions: bool = False
    supports_resume: bool = False
    supports_structured_handoff: bool = True
    max_parallel_tasks: int = 1
    verified_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "adapter": self.adapter,
            "machine": self.machine,
            "execution_modes": [mode.value for mode in self.execution_modes],
            "tools": list(self.tools),
            "available": self.available,
            "supports_sessions": self.supports_sessions,
            "supports_resume": self.supports_resume,
            "supports_structured_handoff": self.supports_structured_handoff,
            "max_parallel_tasks": self.max_parallel_tasks,
            "verified_at": self.verified_at,
        }


AGENTIC_ADAPTERS = {"codex", "claude", "gemini", "agy", "opencode"}
GUIDED_ADAPTERS = {"ollama", "copilot"}


def _adapter_for_model(model_name: str) -> str:
    model = get_model(model_name)
    if not model:
        return "unknown"
    if model.family == "gemini":
        return preferred_cli("gemini")
    if model.required_clis:
        return model.required_clis[0]
    return model.family or "unknown"


def _probe_has_adapter(adapter: str, probe: dict[str, Any] | None) -> bool:
    if probe is None:
        return True
    binaries = probe.get("binaries", {})
    if adapter == "agy":
        return bool(binaries.get("agy") or binaries.get("gemini"))
    return bool(binaries.get(adapter, False))


def profile_for_model(
    model_name: str,
    machine: dict[str, Any] | None = None,
    probe: dict[str, Any] | None = None,
) -> ExecutionProfile:
    model = get_model(model_name)
    resolved_name = model.id if model else model_name
    adapter = _adapter_for_model(model_name)
    machine_name = str((machine or {}).get("name") or "local")
    available = adapter != "unknown" and _probe_has_adapter(adapter, probe)
    verified_at = (probe or {}).get("verified_at") or (probe or {}).get("probed_at")

    if not available:
        return ExecutionProfile(
            model=resolved_name,
            adapter=adapter,
            machine=machine_name,
            execution_modes=(ExecutionMode.ADVISORY,),
            tools=("read",),
            available=False,
            verified_at=verified_at,
        )

    if adapter in AGENTIC_ADAPTERS:
        return ExecutionProfile(
            model=resolved_name,
            adapter=adapter,
            machine=machine_name,
            execution_modes=(ExecutionMode.ADVISORY, ExecutionMode.GUIDED, ExecutionMode.AGENTIC),
            tools=("read", "edit", "shell", "tests"),
            supports_sessions=True,
            supports_resume=True,
            max_parallel_tasks=2,
            verified_at=verified_at,
        )

    if adapter in GUIDED_ADAPTERS:
        return ExecutionProfile(
            model=resolved_name,
            adapter=adapter,
            machine=machine_name,
            execution_modes=(ExecutionMode.ADVISORY, ExecutionMode.GUIDED),
            tools=("read",),
            verified_at=verified_at,
        )

    return ExecutionProfile(
        model=resolved_name,
        adapter=adapter,
        machine=machine_name,
        execution_modes=(ExecutionMode.ADVISORY,),
        tools=("read",),
        verified_at=verified_at,
    )


def supports_mode(profile: ExecutionProfile, mode: ExecutionMode | str) -> bool:
    requested = mode if isinstance(mode, ExecutionMode) else ExecutionMode(mode)
    return requested in profile.execution_modes


def mode_for_role(profile: ExecutionProfile, role: str | None) -> ExecutionMode:
    if role in {"builder", "debugger"} and ExecutionMode.AGENTIC in profile.execution_modes:
        return ExecutionMode.AGENTIC
    if ExecutionMode.GUIDED in profile.execution_modes:
        return ExecutionMode.GUIDED
    return ExecutionMode.ADVISORY
