"""Scenario-driven mock backends, so the whole system runs without a cluster, Jenkins or Prometheus.

Each MCP server runs in its own process, so remediation state (e.g. "payments-api was rolled
back") is shared through a small JSON file. That lets the metrics agent see the recovery.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

DEFAULT_SCENARIO = "payments-crashloop"


def load_scenario(name_or_path: str | None = None) -> dict[str, Any]:
    ref = name_or_path or os.environ.get("DEVOPS_SCENARIO", DEFAULT_SCENARIO)
    path = Path(ref)
    if path.suffix in {".yaml", ".yml"} and path.exists():
        text = path.read_text()
    else:
        text = resources.files("devops_agents.scenarios").joinpath(f"{ref}.yaml").read_text()
    return yaml.safe_load(text)


def state_path(scenario: dict[str, Any]) -> Path:
    custom = os.environ.get("DEVOPS_MOCK_STATE")
    if custom:
        return Path(custom)
    return Path(tempfile.gettempdir()) / f"devops-agents-{scenario['name']}.state.json"


def read_state(scenario: dict[str, Any]) -> dict[str, Any]:
    p = state_path(scenario)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def write_state(scenario: dict[str, Any], state: dict[str, Any]) -> None:
    state_path(scenario).write_text(json.dumps(state, indent=2))


def reset_state(scenario: dict[str, Any] | None = None) -> None:
    p = state_path(scenario or load_scenario())
    if p.exists():
        p.unlink()


class NotFoundError(LookupError):
    pass


# --------------------------------------------------------------------------- kubernetes
class MockKubernetes:
    def __init__(self, scenario: dict[str, Any]):
        self.scenario = scenario
        self._k8s = copy.deepcopy(scenario["kubernetes"])

    def _ns(self, namespace: str) -> dict[str, Any]:
        try:
            return self._k8s["namespaces"][namespace]
        except KeyError:
            known = ", ".join(self._k8s["namespaces"])
            raise NotFoundError(f"namespace {namespace!r} not found (known: {known})") from None

    def _rolled_back(self) -> dict[str, int]:
        return read_state(self.scenario).get("rolled_back", {})

    def _deployment(self, namespace: str, name: str) -> dict[str, Any]:
        for d in self._ns(namespace)["deployments"]:
            if d["name"] == name:
                return self._apply_state(namespace, d)
        raise NotFoundError(f"deployment {namespace}/{name} not found")

    def _apply_state(self, namespace: str, d: dict[str, Any]) -> dict[str, Any]:
        rev = self._rolled_back().get(f"{namespace}/{d['name']}")
        if rev is None:
            return d
        d = copy.deepcopy(d)
        target = next(h for h in d["history"] if h["revision"] == rev)
        d.update(
            image=target["image"], ready=d["replicas"], revision=max(h["revision"] for h in d["history"]) + 1
        )
        d["history"] = [
            *d["history"],
            {**target, "revision": d["revision"], "change_cause": f"rollback to {rev}"},
        ]
        return d

    def _pods(self, namespace: str) -> list[dict[str, Any]]:
        pods = []
        rolled = self._rolled_back()
        for p in self._ns(namespace)["pods"]:
            key = f"{namespace}/{p['deployment']}"
            if key in rolled:
                d = self._deployment(namespace, p["deployment"])
                p = {
                    **p,
                    "name": p["name"].rsplit("-", 2)[0] + "-74b5c8f6d9-" + p["name"][-5:],
                    "status": "Running",
                    "ready": True,
                    "restarts": 0,
                    "image": d["image"],
                    "last_state": None,
                    "logs": next(
                        x for x in self._ns(namespace)["deployments"] if x["name"] == p["deployment"]
                    ).get("healthy_logs", "INFO ready"),
                }
            pods.append(p)
        return pods

    def list_namespaces(self) -> list[str]:
        return list(self._k8s["namespaces"])

    def list_pods(self, namespace: str, selector: str | None = None) -> list[dict[str, Any]]:
        pods = self._pods(namespace)
        if selector:
            app = selector.split("=", 1)[-1]
            pods = [p for p in pods if p["deployment"] == app]
        keys = ("name", "status", "ready", "restarts", "image", "node")
        return [{k: p.get(k) for k in keys} for p in pods]

    def describe_pod(self, namespace: str, name: str) -> dict[str, Any]:
        for p in self._pods(namespace):
            if p["name"] == name:
                return {k: v for k, v in p.items() if k != "logs"}
        raise NotFoundError(f"pod {namespace}/{name} not found")

    def pod_logs(self, namespace: str, name: str, tail: int = 50, previous: bool = False) -> str:
        for p in self._pods(namespace):
            if p["name"] == name:
                return "\n".join(p.get("logs", "").strip().splitlines()[-tail:])
        raise NotFoundError(f"pod {namespace}/{name} not found")

    def events(self, namespace: str, involved_object: str | None = None) -> list[dict[str, Any]]:
        evts = self._ns(namespace)["events"]
        if f"{namespace}/payments-api" in self._rolled_back():
            evts = [
                *evts,
                {
                    "type": "Normal",
                    "reason": "RolloutRollback",
                    "object": "deployment/payments-api",
                    "message": "Rolled back to revision 13",
                    "count": 1,
                },
            ]
        if involved_object:
            evts = [e for e in evts if involved_object in e["object"]]
        return evts

    def list_deployments(self, namespace: str) -> list[dict[str, Any]]:
        out = []
        for d in self._ns(namespace)["deployments"]:
            d = self._apply_state(namespace, d)
            out.append({k: d[k] for k in ("name", "replicas", "ready", "image", "revision")})
        return out

    def rollout_history(self, namespace: str, deployment: str) -> list[dict[str, Any]]:
        return self._deployment(namespace, deployment)["history"]

    def restart_deployment(self, namespace: str, deployment: str) -> str:
        d = self._deployment(namespace, deployment)
        return (
            f"deployment {namespace}/{deployment} restarted (rollout restart). Pods will be recreated "
            f"with the same image {d['image']}."
        )

    def rollback_deployment(self, namespace: str, deployment: str, to_revision: int | None = None) -> str:
        d = self._deployment(namespace, deployment)
        revisions = sorted(h["revision"] for h in d["history"])
        if to_revision is None:
            older = [r for r in revisions if r < d["revision"]]
            if not older:
                raise NotFoundError(f"no previous revision for {namespace}/{deployment}")
            to_revision = older[-1]
        if to_revision not in revisions:
            raise NotFoundError(f"revision {to_revision} not in history {revisions}")
        state = read_state(self.scenario)
        state.setdefault("rolled_back", {})[f"{namespace}/{deployment}"] = to_revision
        write_state(self.scenario, state)
        image = next(h["image"] for h in d["history"] if h["revision"] == to_revision)
        return f"deployment {namespace}/{deployment} rolled back to revision {to_revision} ({image})"

    def scale_deployment(self, namespace: str, deployment: str, replicas: int) -> str:
        self._deployment(namespace, deployment)
        return f"deployment {namespace}/{deployment} scaled to {replicas} replicas"


# --------------------------------------------------------------------------- jenkins
class MockJenkins:
    def __init__(self, scenario: dict[str, Any]):
        self.jobs = {j["name"]: j for j in scenario["ci"]["jobs"]}

    def _job(self, job: str) -> dict[str, Any]:
        if job not in self.jobs:
            raise NotFoundError(f"job {job!r} not found (known: {', '.join(self.jobs)})")
        return self.jobs[job]

    def _build(self, job: str, number: int) -> dict[str, Any]:
        for b in self._job(job)["builds"]:
            if b["number"] == number:
                return b
        raise NotFoundError(f"build {job}#{number} not found")

    def list_jobs(self) -> list[dict[str, Any]]:
        out = []
        for j in self.jobs.values():
            last = j["builds"][0]
            out.append(
                {
                    "name": j["name"],
                    "url": j["url"],
                    "last_build": last["number"],
                    "last_result": last["result"],
                }
            )
        return out

    def recent_builds(self, job: str, limit: int = 5) -> list[dict[str, Any]]:
        keys = ("number", "result", "started_at", "duration_s", "parameters")
        return [{k: b[k] for k in keys} for b in self._job(job)["builds"][:limit]]

    def build_details(self, job: str, number: int) -> dict[str, Any]:
        b = self._build(job, number)
        return {k: v for k, v in b.items() if k != "log"}

    def build_log(self, job: str, number: int, tail: int = 80) -> str:
        return "\n".join(self._build(job, number)["log"].strip().splitlines()[-tail:])

    def trigger_build(self, job: str, parameters: dict[str, str] | None = None) -> str:
        j = self._job(job)
        return f"queued {job} build #{j['builds'][0]['number'] + 1} with parameters {parameters or {}}"


# --------------------------------------------------------------------------- prometheus
class MockPrometheus:
    def __init__(self, scenario: dict[str, Any]):
        self.scenario = scenario
        self.m = scenario["metrics"]

    def _recovered(self) -> bool:
        return bool(read_state(self.scenario).get("rolled_back"))

    def _error_rates(self) -> dict[str, float]:
        if self._recovered():
            return self.m["healthy_after_rollback"]["error_rate"]
        return self.m["error_rate"]

    def query(self, promql: str) -> list[dict[str, Any]]:
        q = promql.lower()
        if q.strip() == "up" or q.startswith("up{"):
            return [{"metric": {"job": s}, "value": 1} for s in self.m["error_rate"]]
        if "5.." in q or "error" in q or "status" in q:
            return [{"metric": {"service": s}, "value": v} for s, v in self._error_rates().items()]
        if "duration" in q or "latency" in q:
            return [{"metric": {"service": s}, "value": v} for s, v in self.m["latency_p95_s"].items()]
        return []

    def firing_alerts(self) -> list[dict[str, Any]]:
        if self._recovered():
            return []
        return self.m["alerts"]

    def error_rate(self, service: str, window: str = "5m") -> float | None:
        return self._error_rates().get(service)

    def latency_p95(self, service: str, window: str = "5m") -> float | None:
        if self._recovered() and service == "payments-api":
            return 0.21
        return self.m["latency_p95_s"].get(service)
