"""Live backends for real clusters and tools.

Kubernetes uses the official client (`pip install 'mcp-devops-agents[live]'`) with your kubeconfig
or in-cluster service account. Jenkins and Prometheus use their HTTP APIs.
Give the agents a dedicated identity with read-only RBAC; write tools are also gated by policy.
"""

from __future__ import annotations

import datetime as dt
import os
from typing import Any

import httpx


def _env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} must be set for DEVOPS_BACKEND=live")
    return value


# --------------------------------------------------------------------------- kubernetes
class LiveKubernetes:
    def __init__(self) -> None:
        try:
            from kubernetes import client, config
        except ImportError as e:  # pragma: no cover - depends on optional extra
            raise RuntimeError("install the live extra: pip install 'mcp-devops-agents[live]'") from e
        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()
        self.core = client.CoreV1Api()
        self.apps = client.AppsV1Api()

    @staticmethod
    def _pod_status(p: Any) -> str:
        for cs in p.status.container_statuses or []:
            if cs.state and cs.state.waiting and cs.state.waiting.reason:
                return cs.state.waiting.reason
        return p.status.phase

    def list_namespaces(self) -> list[str]:
        return [n.metadata.name for n in self.core.list_namespace().items]

    def list_pods(self, namespace: str, selector: str | None = None) -> list[dict[str, Any]]:
        pods = self.core.list_namespaced_pod(namespace, label_selector=selector or "").items
        return [
            {
                "name": p.metadata.name,
                "status": self._pod_status(p),
                "ready": all(cs.ready for cs in p.status.container_statuses or []),
                "restarts": sum(cs.restart_count for cs in p.status.container_statuses or []),
                "image": p.spec.containers[0].image,
                "node": p.spec.node_name,
            }
            for p in pods
        ]

    def describe_pod(self, namespace: str, name: str) -> dict[str, Any]:
        p = self.core.read_namespaced_pod(name, namespace)
        c = p.spec.containers[0]
        last = None
        for cs in p.status.container_statuses or []:
            if cs.last_state and cs.last_state.terminated:
                t = cs.last_state.terminated
                last = f"Terminated ({t.reason}, exit code {t.exit_code})"
        return {
            "name": p.metadata.name,
            "status": self._pod_status(p),
            "image": c.image,
            "node": p.spec.node_name,
            "last_state": last,
            # Values from secrets are not resolved here; only literal env values are shown.
            "env": {e.name: e.value for e in c.env or [] if e.value is not None},
            "resources": c.resources.to_dict() if c.resources else None,
        }

    def pod_logs(self, namespace: str, name: str, tail: int = 50, previous: bool = False) -> str:
        return self.core.read_namespaced_pod_log(name, namespace, tail_lines=tail, previous=previous)

    def events(self, namespace: str, involved_object: str | None = None) -> list[dict[str, Any]]:
        items = self.core.list_namespaced_event(namespace).items
        out = [
            {
                "type": e.type,
                "reason": e.reason,
                "object": f"{e.involved_object.kind.lower()}/{e.involved_object.name}",
                "message": e.message,
                "count": e.count,
            }
            for e in items
        ]
        if involved_object:
            out = [e for e in out if involved_object in e["object"]]
        return out[-50:]

    def list_deployments(self, namespace: str) -> list[dict[str, Any]]:
        return [
            {
                "name": d.metadata.name,
                "replicas": d.spec.replicas,
                "ready": d.status.ready_replicas or 0,
                "image": d.spec.template.spec.containers[0].image,
                "revision": int(d.metadata.annotations.get("deployment.kubernetes.io/revision", 0)),
            }
            for d in self.apps.list_namespaced_deployment(namespace).items
        ]

    def _replicasets(self, namespace: str, deployment: str) -> list[Any]:
        d = self.apps.read_namespaced_deployment(deployment, namespace)
        sel = ",".join(f"{k}={v}" for k, v in d.spec.selector.match_labels.items())
        rss = self.apps.list_namespaced_replica_set(namespace, label_selector=sel).items
        owned = [r for r in rss if any(o.uid == d.metadata.uid for o in r.metadata.owner_references or [])]
        return sorted(
            owned, key=lambda r: int(r.metadata.annotations.get("deployment.kubernetes.io/revision", 0))
        )

    def rollout_history(self, namespace: str, deployment: str) -> list[dict[str, Any]]:
        return [
            {
                "revision": int(r.metadata.annotations.get("deployment.kubernetes.io/revision", 0)),
                "image": r.spec.template.spec.containers[0].image,
                "change_cause": r.metadata.annotations.get("kubernetes.io/change-cause"),
                "deployed_at": r.metadata.creation_timestamp.isoformat(),
            }
            for r in self._replicasets(namespace, deployment)
        ]

    def restart_deployment(self, namespace: str, deployment: str) -> str:
        now = dt.datetime.now(dt.UTC).isoformat()
        patch = {
            "spec": {"template": {"metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": now}}}}
        }
        self.apps.patch_namespaced_deployment(deployment, namespace, patch)
        return f"deployment {namespace}/{deployment} restarted at {now}"

    def rollback_deployment(self, namespace: str, deployment: str, to_revision: int | None = None) -> str:
        """Equivalent of `kubectl rollout undo`: copy an older ReplicaSet's pod template back."""
        rss = self._replicasets(namespace, deployment)
        if len(rss) < 2 and to_revision is None:
            raise RuntimeError("no previous revision to roll back to")
        rev = lambda r: int(r.metadata.annotations.get("deployment.kubernetes.io/revision", 0))  # noqa: E731
        target = next((r for r in rss if rev(r) == to_revision), None) if to_revision else rss[-2]
        if target is None:
            raise RuntimeError(f"revision {to_revision} not found")
        template = self.apps.api_client.sanitize_for_serialization(target.spec.template)
        template["metadata"].get("labels", {}).pop("pod-template-hash", None)
        self.apps.patch_namespaced_deployment(deployment, namespace, {"spec": {"template": template}})
        return f"deployment {namespace}/{deployment} rolled back to revision {rev(target)}"

    def scale_deployment(self, namespace: str, deployment: str, replicas: int) -> str:
        self.apps.patch_namespaced_deployment_scale(deployment, namespace, {"spec": {"replicas": replicas}})
        return f"deployment {namespace}/{deployment} scaled to {replicas}"


# --------------------------------------------------------------------------- jenkins
class LiveJenkins:
    def __init__(self, url: str, user: str, token: str, transport: httpx.BaseTransport | None = None):
        self.http = httpx.Client(
            base_url=url.rstrip("/"), auth=(user, token), timeout=20, transport=transport
        )

    @classmethod
    def from_env(cls) -> LiveJenkins:
        return cls(_env("JENKINS_URL"), _env("JENKINS_USER"), _env("JENKINS_TOKEN"))

    def _get(self, path: str, **params: Any) -> Any:
        r = self.http.get(path, params=params)
        r.raise_for_status()
        return r.json() if "json" in r.headers.get("content-type", "") else r.text

    @staticmethod
    def _job_path(job: str) -> str:
        return "/job/" + "/job/".join(job.split("/"))

    def list_jobs(self) -> list[dict[str, Any]]:
        data = self._get("/api/json", tree="jobs[name,url,lastBuild[number,result]]")
        return [
            {
                "name": j["name"],
                "url": j["url"],
                "last_build": (j.get("lastBuild") or {}).get("number"),
                "last_result": (j.get("lastBuild") or {}).get("result"),
            }
            for j in data["jobs"]
        ]

    def recent_builds(self, job: str, limit: int = 5) -> list[dict[str, Any]]:
        tree = f"builds[number,result,timestamp,duration,actions[parameters[name,value]]]{{0,{limit}}}"
        data = self._get(f"{self._job_path(job)}/api/json", tree=tree)
        out = []
        for b in data["builds"]:
            params = {
                p["name"]: p.get("value")
                for a in b.get("actions", [])
                if a and "parameters" in a
                for p in a["parameters"]
            }
            out.append(
                {
                    "number": b["number"],
                    "result": b["result"],
                    "started_at": dt.datetime.fromtimestamp(b["timestamp"] / 1000, dt.UTC).isoformat(),
                    "duration_s": round(b["duration"] / 1000),
                    "parameters": params,
                }
            )
        return out

    def build_details(self, job: str, number: int) -> dict[str, Any]:
        tree = (
            "number,result,timestamp,duration,changeSets[items[commitId,author[fullName],msg,affectedPaths]]"
        )
        b = self._get(f"{self._job_path(job)}/{number}/api/json", tree=tree)
        changes = [
            {
                "commit": i["commitId"][:7],
                "author": i["author"]["fullName"],
                "message": i["msg"],
                "files": i.get("affectedPaths", []),
            }
            for cs in b.get("changeSets", [])
            for i in cs.get("items", [])
        ]
        return {
            "number": b["number"],
            "result": b["result"],
            "duration_s": round(b["duration"] / 1000),
            "changes": changes,
        }

    def build_log(self, job: str, number: int, tail: int = 80) -> str:
        text = self._get(f"{self._job_path(job)}/{number}/consoleText")
        return "\n".join(str(text).splitlines()[-tail:])

    def trigger_build(self, job: str, parameters: dict[str, str] | None = None) -> str:
        path = f"{self._job_path(job)}/{'buildWithParameters' if parameters else 'build'}"
        r = self.http.post(path, params=parameters or {})
        r.raise_for_status()
        return f"queued {job}: {r.headers.get('location', 'ok')}"


# --------------------------------------------------------------------------- prometheus
class LivePrometheus:
    def __init__(
        self,
        url: str,
        transport: httpx.BaseTransport | None = None,
        service_label: str = "service",
        status_label: str = "status",
        requests_metric: str = "http_requests_total",
        duration_metric: str = "http_request_duration_seconds_bucket",
    ):
        self.http = httpx.Client(base_url=url.rstrip("/"), timeout=20, transport=transport)
        self.service_label = service_label
        self.status_label = status_label
        self.requests_metric = requests_metric
        self.duration_metric = duration_metric

    @classmethod
    def from_env(cls) -> LivePrometheus:
        return cls(
            _env("PROMETHEUS_URL"),
            service_label=os.environ.get("PROM_SERVICE_LABEL", "service"),
            status_label=os.environ.get("PROM_STATUS_LABEL", "status"),
            requests_metric=os.environ.get("PROM_REQUESTS_METRIC", "http_requests_total"),
            duration_metric=os.environ.get("PROM_DURATION_METRIC", "http_request_duration_seconds_bucket"),
        )

    def query(self, promql: str) -> list[dict[str, Any]]:
        r = self.http.get("/api/v1/query", params={"query": promql})
        r.raise_for_status()
        body = r.json()
        if body.get("status") != "success":
            raise RuntimeError(body.get("error", "query failed"))
        return [{"metric": s["metric"], "value": float(s["value"][1])} for s in body["data"]["result"]]

    def _scalar(self, promql: str) -> float | None:
        res = self.query(promql)
        return res[0]["value"] if res else None

    def firing_alerts(self) -> list[dict[str, Any]]:
        r = self.http.get("/api/v1/alerts")
        r.raise_for_status()
        return [
            {
                "name": a["labels"].get("alertname"),
                "severity": a["labels"].get("severity"),
                "service": a["labels"].get(self.service_label),
                "summary": a.get("annotations", {}).get("summary"),
                "active_since": a.get("activeAt"),
            }
            for a in r.json()["data"]["alerts"]
            if a.get("state") == "firing"
        ]

    def error_rate(self, service: str, window: str = "5m") -> float | None:
        sel = f'{self.service_label}="{service}"'
        m, st = self.requests_metric, self.status_label
        return self._scalar(
            f'sum(rate({m}{{{sel},{st}=~"5.."}}[{window}])) / sum(rate({m}{{{sel}}}[{window}]))'
        )

    def latency_p95(self, service: str, window: str = "5m") -> float | None:
        sel = f'{self.service_label}="{service}"'
        return self._scalar(
            f"histogram_quantile(0.95, sum by (le) (rate({self.duration_metric}{{{sel}}}[{window}])))"
        )
