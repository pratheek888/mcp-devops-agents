"""Backends behind the MCP servers: `mock` (scenario fixtures) or `live` (real APIs)."""

from __future__ import annotations

import os

from .base import CIBackend, KubernetesBackend, MetricsBackend


def backend_mode() -> str:
    mode = os.environ.get("DEVOPS_BACKEND", "mock").lower()
    if mode not in {"mock", "live"}:
        raise ValueError(f"DEVOPS_BACKEND must be 'mock' or 'live', got {mode!r}")
    return mode


def kubernetes_backend() -> KubernetesBackend:
    if backend_mode() == "live":
        from .live import LiveKubernetes

        return LiveKubernetes()
    from .mock import MockKubernetes, load_scenario

    return MockKubernetes(load_scenario())


def ci_backend() -> CIBackend:
    if backend_mode() == "live":
        from .live import LiveJenkins

        return LiveJenkins.from_env()
    from .mock import MockJenkins, load_scenario

    return MockJenkins(load_scenario())


def metrics_backend() -> MetricsBackend:
    if backend_mode() == "live":
        from .live import LivePrometheus

        return LivePrometheus.from_env()
    from .mock import MockPrometheus, load_scenario

    return MockPrometheus(load_scenario())


__all__ = [
    "CIBackend",
    "KubernetesBackend",
    "MetricsBackend",
    "backend_mode",
    "ci_backend",
    "kubernetes_backend",
    "metrics_backend",
]
