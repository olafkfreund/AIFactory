"""Restricted Kubernetes resources for read-only investigation runs."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

from core.job_dispatch import (
    DEFAULT_NIX_IMAGE,
    NONROOT_UID,
    assert_job_policy,
)

from server.services.build_backend import (
    _DEFAULT_DATA_ROOT,
    _DEFAULT_NAMESPACE,
    _ENV_DATA_ROOT,
    _ENV_NAMESPACE,
    _ENV_REPO_PVC,
    _resolve_build_image,
)


def _value(run: Any, key: str) -> Any:
    return run[key] if isinstance(run, dict) else getattr(run, key)


def _names(run: Any) -> tuple[str, str]:
    short = str(_value(run, "id")).replace("-", "")[:8]
    return f"aif-inv-{short}", f"investigations/{_value(run, 'id')}/src"


def _labels(run: Any, kind: str) -> dict[str, str]:
    return {
        "factory.io/kind": "task",
        "factory.io/run-kind": kind,
        "factory.io/investigation": str(_value(run, "id")),
        "factory.io/service": "aifactory",
        "app": "aifactory-investigate",
    }


def _pod(run: Any, containers: list[dict[str, Any]]) -> dict[str, Any]:
    _, subpath = _names(run)
    pvc = os.environ.get(_ENV_REPO_PVC, "").strip()
    root = os.environ.get(_ENV_DATA_ROOT, _DEFAULT_DATA_ROOT)
    if not pvc or not os.path.ismount(root):
        raise ValueError("shared storage unavailable")
    spec = {
        "restartPolicy": "Never",
        "automountServiceAccountToken": False,
        # Service links put every cluster service's address in the env (#2196).
        "enableServiceLinks": False,
        "terminationGracePeriodSeconds": 10,
        "securityContext": {
            "runAsNonRoot": True,
            "runAsUser": NONROOT_UID,
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "volumes": [
            {"name": "src", "persistentVolumeClaim": {"claimName": pvc}},
            {"name": "tmp", "emptyDir": {"sizeLimit": "256Mi"}},
        ],
        "containers": containers,
    }
    for container in containers:
        container["volumeMounts"] = [
            {"name": "src", "mountPath": "/src", "subPath": subpath, "readOnly": True},
            {"name": "tmp", "mountPath": "/tmp"},  # noqa: S108 - Pod scratch volume.
        ]
    return spec


def _hardened() -> dict[str, Any]:
    return {
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"]},
        "readOnlyRootFilesystem": True,
    }


def _job(
    run: Any, kind: str, name: str, pod: dict[str, Any], deadline: int
) -> dict[str, Any]:
    manifest = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": name,
            "namespace": os.environ.get(_ENV_NAMESPACE, _DEFAULT_NAMESPACE),
            "labels": _labels(run, kind),
        },
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": deadline,
            "template": {"metadata": {"labels": _labels(run, kind)}, "spec": pod},
        },
    }
    assert_job_policy(manifest)
    assert_investigation_manifest(manifest)
    return manifest


def agent_manifest(run: Any) -> dict[str, Any]:
    base, _ = _names(run)
    secret = base
    env = [
        {"name": "JOB_ID", "value": f"{base}-agent"},
        {"name": "AIFACTORY_INVESTIGATION_ID", "value": str(_value(run, "id"))},
        {
            "name": "AIFACTORY_API_URL",
            "value": os.environ.get("AIFACTORY_API_URL", "http://aifactory:3101"),
        },
        {"name": "HOME", "value": "/tmp/home"},  # noqa: S108 - Pod scratch volume.
        {"name": "CLAUDE_CONFIG_DIR", "value": "/tmp/claude"},  # noqa: S108 - Pod scratch volume.
        # `-P` keeps the cwd off sys.path, so the backend package root must be named (#2190).
        {"name": "PYTHONPATH", "value": "/home/projects/MagesticAI/apps/backend"},
        *(
            {"name": name, "valueFrom": {"secretKeyRef": {"name": secret, "key": name}}}
            for name in ("CLAUDE_CODE_OAUTH_TOKEN", "AIFACTORY_RUN_TOKEN")
        ),
    ]
    container: dict[str, Any] = {
        "name": "agent",
        "image": _resolve_build_image(DEFAULT_NIX_IMAGE),
        "command": ["python", "-P", "-m", "agents.investigate"],
        "workingDir": "/tmp",  # noqa: S108 - Isolated pod scratch volume.
        "env": env,
        "securityContext": _hardened(),
    }
    pod = _pod(run, [container])
    return _job(
        run,
        "investigate-agent",
        f"{base}-agent",
        pod,
        int(_value(run, "limits")["minutes"]) * 60,
    )


def test_manifest(run: Any, n: int) -> dict[str, Any]:
    base, subpath = _names(run)
    created = _value(run, "created_at")
    minutes = int(_value(run, "limits")["minutes"])
    remaining = minutes * 60
    if created is not None:
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        remaining -= int((datetime.now(UTC) - created).total_seconds())
    seconds = max(1, min(900, remaining))
    proxy = os.environ.get(
        "AIFACTORY_INVESTIGATE_PROXY_URL", "http://aifactory-investigate-proxy:3128"
    )
    env = [
        {"name": "HTTPS_PROXY", "value": proxy},
        {"name": "HTTP_PROXY", "value": proxy},
        {"name": "CI", "value": "1"},
        {"name": "TEST_COMMAND", "value": _value(run, "test_command")},
    ]
    container: dict[str, Any] = {
        "name": "test",
        "image": _resolve_build_image(DEFAULT_NIX_IMAGE),
        "command": ["sh", "-c", f'timeout {seconds} sh -c "$TEST_COMMAND"'],
        "workingDir": "/work",
        "env": env,
        "securityContext": _hardened(),
    }
    pod = _pod(run, [container])
    pod["volumes"].append({"name": "work", "emptyDir": {}})
    container["volumeMounts"].append({"name": "work", "mountPath": "/work"})
    pod["initContainers"] = [
        {
            "name": "copy-source",
            "image": _resolve_build_image(DEFAULT_NIX_IMAGE),
            # Not -a: it sets /work's own times and mode, and /work is root-owned (#2203).
            "command": ["sh", "-c", "cp -R /src/. /work/"],
            "securityContext": _hardened(),
            "volumeMounts": [
                {
                    "name": "src",
                    "mountPath": "/src",
                    "subPath": subpath,
                    "readOnly": True,
                },
                {"name": "work", "mountPath": "/work"},
            ],
        }
    ]
    return _job(run, "investigate-test", f"{base}-test-{n}", pod, seconds + 30)


def secret_manifest(run: Any, job_uid: str) -> dict[str, Any]:
    base, _ = _names(run)
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": base,
            "namespace": os.environ.get(_ENV_NAMESPACE, _DEFAULT_NAMESPACE),
            "ownerReferences": [
                {
                    "apiVersion": "batch/v1",
                    "kind": "Job",
                    "name": f"{base}-agent",
                    "uid": job_uid,
                    "controller": True,
                }
            ],
        },
        "type": "Opaque",
        "stringData": {
            "CLAUDE_CODE_OAUTH_TOKEN": _value(run, "claude_token"),
            "AIFACTORY_RUN_TOKEN": _value(run, "run_token"),
        },
    }


class ManifestRefused(RuntimeError):  # noqa: N818 - Public exception name used by callers.
    """An investigation manifest carries something outside its allowlist."""


def assert_investigation_manifest(m: dict[str, Any]) -> None:
    """Reject an investigation Job that carries credentials or mounts beyond its role."""
    try:
        _check_investigation_manifest(m)
    except (AssertionError, KeyError, StopIteration, TypeError) as exc:
        raise ManifestRefused("investigation manifest refused") from exc


def _check_investigation_manifest(m: dict[str, Any]) -> None:  # noqa: PLR0912, PLR0915 - Keep all manifest refusals in one guard.
    assert_job_policy(m)
    pod = m["spec"]["template"]
    labels = pod["metadata"]["labels"]
    kind = labels.get("factory.io/run-kind")
    if kind not in {"investigate-agent", "investigate-test"}:
        raise ManifestRefused("investigation manifest refused")
    if not (labels.get("factory.io/investigation")):
        raise ManifestRefused("investigation manifest refused")
    if not (labels.get("factory.io/service") == "aifactory"):
        raise ManifestRefused("investigation manifest refused")
    if not (labels.get("app") == "aifactory-investigate"):
        raise ManifestRefused("investigation manifest refused")
    if not ("app.kubernetes.io/instance" not in labels):
        raise ManifestRefused("investigation manifest refused")
    if not ("app.kubernetes.io/instance" not in m["metadata"].get("labels", {})):
        raise ManifestRefused("investigation manifest refused")
    spec = pod["spec"]
    if spec.get("automountServiceAccountToken") is not False:
        raise ManifestRefused("investigation manifest refused")
    if spec.get("enableServiceLinks") is not False:
        raise ManifestRefused("investigation manifest refused")
    if spec.get("terminationGracePeriodSeconds") != 10:  # noqa: PLR2004 - Required pod grace.
        raise ManifestRefused("investigation manifest refused")
    if spec.get("serviceAccountName"):
        raise ManifestRefused("investigation manifest refused")
    if not (
        set(spec)
        <= {
            "restartPolicy",
            "automountServiceAccountToken",
            "enableServiceLinks",
            "terminationGracePeriodSeconds",
            "securityContext",
            "volumes",
            "containers",
            "initContainers",
        }
    ):
        raise ManifestRefused("investigation manifest refused")
    allowed_volumes = {"src", "tmp"} | (
        {"work"} if kind == "investigate-test" else set()
    )
    if not ({v["name"] for v in spec["volumes"]} == allowed_volumes):
        raise ManifestRefused("investigation manifest refused")
    if not (len(spec["volumes"]) == len(allowed_volumes)):
        raise ManifestRefused("investigation manifest refused")
    if not (
        all(
            set(v) == {"name", "persistentVolumeClaim"}
            if v["name"] == "src"
            else set(v) == {"name", "emptyDir"}
            for v in spec["volumes"]
        )
    ):
        raise ManifestRefused("investigation manifest refused")
    if not (
        next(v for v in spec["volumes"] if v["name"] == "src")["persistentVolumeClaim"]
        == {"claimName": os.environ.get(_ENV_REPO_PVC, "").strip()}
    ):
        raise ManifestRefused("investigation manifest refused")
    if not (
        next(v for v in spec["volumes"] if v["name"] == "tmp")["emptyDir"]
        == {"sizeLimit": "256Mi"}
    ):
        raise ManifestRefused("investigation manifest refused")
    if kind == "investigate-test" and not (
        next(v for v in spec["volumes"] if v["name"] == "work")["emptyDir"] == {}
    ):
        raise ManifestRefused("investigation manifest refused")
    if not (len(spec["containers"]) == 1):
        raise ManifestRefused("investigation manifest refused")
    if not (
        len(spec.get("initContainers", [])) == (1 if kind == "investigate-test" else 0)
    ):
        raise ManifestRefused("investigation manifest refused")
    for container in [*spec["containers"], *spec.get("initContainers", [])]:
        if not (
            set(container)
            <= {
                "name",
                "image",
                "command",
                "workingDir",
                "env",
                "securityContext",
                "volumeMounts",
            }
        ):
            raise ManifestRefused("investigation manifest refused")
        if not (container["securityContext"] == _hardened()):
            raise ManifestRefused("investigation manifest refused")
        mounts = container["volumeMounts"]
        if not (len(mounts) == len({mount["name"] for mount in mounts})):
            raise ManifestRefused("investigation manifest refused")
        if not (all(mount["name"] in allowed_volumes for mount in mounts)):
            raise ManifestRefused("investigation manifest refused")
        if not (
            all(
                mount["mountPath"]
                == {"src": "/src", "tmp": "/tmp", "work": "/work"}[mount["name"]]  # noqa: S108 - Pod scratch volume.
                for mount in mounts
            )
        ):
            raise ManifestRefused("investigation manifest refused")
        if not (
            all(
                mount.get("readOnly") is True
                and mount.get("subPath")
                == f"investigations/{labels['factory.io/investigation']}/src"
                and set(mount) == {"name", "mountPath", "subPath", "readOnly"}
                for mount in mounts
                if mount["name"] == "src"
            )
        ):
            raise ManifestRefused("investigation manifest refused")
        if not (
            all(
                set(mount) == {"name", "mountPath"}
                for mount in mounts
                if mount["name"] != "src"
            )
        ):
            raise ManifestRefused("investigation manifest refused")
        if container["name"] == "copy-source":
            if not ("env" not in container):
                raise ManifestRefused("investigation manifest refused")
            if not ({mount["name"] for mount in mounts} == {"src", "work"}):
                raise ManifestRefused("investigation manifest refused")
            continue
        if not ({mount["name"] for mount in mounts} == allowed_volumes):
            raise ManifestRefused("investigation manifest refused")
        expected_env = (
            {
                "JOB_ID",
                "AIFACTORY_INVESTIGATION_ID",
                "AIFACTORY_API_URL",
                "HOME",
                "CLAUDE_CONFIG_DIR",
                "PYTHONPATH",
                "CLAUDE_CODE_OAUTH_TOKEN",
                "AIFACTORY_RUN_TOKEN",
            }
            if kind == "investigate-agent"
            else {"HTTPS_PROXY", "HTTP_PROXY", "CI", "TEST_COMMAND"}
        )
        if not (len(container.get("env", [])) == len(expected_env)):
            raise ManifestRefused("investigation manifest refused")
        if not ({e["name"] for e in container.get("env", [])} == expected_env):
            raise ManifestRefused("investigation manifest refused")
        for entry in container.get("env", []):
            if kind == "investigate-agent" and (
                entry["name"] in {"CLAUDE_CODE_OAUTH_TOKEN", "AIFACTORY_RUN_TOKEN"}
            ) != ("valueFrom" in entry):
                raise ManifestRefused("investigation manifest refused")
            if not (
                set(entry)
                == (
                    {"name", "valueFrom"} if "valueFrom" in entry else {"name", "value"}
                )
            ):
                raise ManifestRefused("investigation manifest refused")
            if "valueFrom" in entry:
                if not (kind == "investigate-agent"):
                    raise ManifestRefused("investigation manifest refused")
                if entry["name"] not in {
                    "CLAUDE_CODE_OAUTH_TOKEN",
                    "AIFACTORY_RUN_TOKEN",
                }:
                    raise ManifestRefused("investigation manifest refused")
                if not (
                    entry["valueFrom"]
                    == {
                        "secretKeyRef": {
                            "name": m["metadata"]["name"].removesuffix("-agent"),
                            "key": entry["name"],
                        }
                    }
                ):
                    raise ManifestRefused("investigation manifest refused")


def _selected(selector: dict[str, Any], labels: dict[str, str]) -> bool:
    if not all(labels.get(k) == v for k, v in selector.get("matchLabels", {}).items()):
        return False
    for expr in selector.get("matchExpressions", []):
        key, op, values = expr["key"], expr["operator"], expr.get("values", [])
        present = key in labels
        if op == "In" and (not present or labels[key] not in values):
            return False
        if op == "NotIn" and present and labels[key] in values:
            return False
        if op == "Exists" and not present:
            return False
        if op == "DoesNotExist" and present:
            return False
        if op not in {"In", "NotIn", "Exists", "DoesNotExist"}:
            raise ValueError(f"unknown selector operator: {op}")
    return True


def network_check(
    policies: list[dict[str, Any]],
    agent_labels: dict[str, str],
    test_labels: dict[str, str],
) -> str | None:
    """Return the first unsafe policy name, or None for exact pod isolation."""
    selected: dict[str, set[str]] = {"agent": set(), "test": set()}
    names: set[str] = set()
    for policy in policies:
        name: str = policy.get("metadata", {}).get("name", "unnamed network policy")
        try:
            names.add(name)
            selector = policy["spec"]["podSelector"]
            if _selected(selector, agent_labels):
                selected["agent"].add(name)
            if _selected(selector, test_labels):
                selected["test"].add(name)
        except (KeyError, TypeError, ValueError):
            return name
    for role in ("agent", "test"):
        expected = {name for name in names if name.endswith(f"-investigate-{role}")}
        if len(expected) != 1:
            return f"missing investigate-{role} policy"
        extra = selected[role] - expected
        if extra:
            return sorted(extra)[0]
        if selected[role] != expected:
            return sorted(expected)[0]
    return None
