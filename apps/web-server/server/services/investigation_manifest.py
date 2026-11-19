"""Restricted Kubernetes resources for read-only investigation runs."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

from core.job_dispatch import DEFAULT_NIX_IMAGE, NONROOT_UID, assert_job_policy

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


def _pod(
    run: Any, kind: str, containers: list[dict], *, init: list[dict] | None = None
) -> dict:
    _, subpath = _names(run)
    pvc = os.environ.get(_ENV_REPO_PVC, "").strip()
    root = os.environ.get(_ENV_DATA_ROOT, _DEFAULT_DATA_ROOT)
    if not pvc or not os.path.ismount(root):
        raise ValueError("shared storage unavailable")
    spec = {
        "restartPolicy": "Never",
        "automountServiceAccountToken": False,
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
            {"name": "tmp", "mountPath": "/tmp"},
        ]
    if init:
        spec["initContainers"] = init
    return spec


def _hardened() -> dict:
    return {
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"]},
        "readOnlyRootFilesystem": True,
    }


def _job(run: Any, kind: str, name: str, pod: dict, deadline: int) -> dict:
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


def agent_manifest(run: Any) -> dict:
    base, _ = _names(run)
    secret = base
    env = [
        {"name": "JOB_ID", "value": f"{base}-agent"},
        {"name": "AIFACTORY_INVESTIGATION_ID", "value": str(_value(run, "id"))},
        {
            "name": "AIFACTORY_API_URL",
            "value": os.environ.get("AIFACTORY_API_URL", "http://aifactory:3101"),
        },
        {"name": "HOME", "value": "/tmp/home"},
        {"name": "CLAUDE_CONFIG_DIR", "value": "/tmp/claude"},
        *(
            {"name": name, "valueFrom": {"secretKeyRef": {"name": secret, "key": name}}}
            for name in ("CLAUDE_CODE_OAUTH_TOKEN", "AIFACTORY_RUN_TOKEN")
        ),
    ]
    container = {
        "name": "agent",
        "image": _resolve_build_image(DEFAULT_NIX_IMAGE),
        "command": ["python", "-m", "agents.investigate"],
        "workingDir": "/src",
        "env": env,
        "securityContext": _hardened(),
    }
    pod = _pod(run, "investigate-agent", [container])
    return _job(
        run,
        "investigate-agent",
        f"{base}-agent",
        pod,
        int(_value(run, "limits")["minutes"]) * 60,
    )


def test_manifest(run: Any, n: int) -> dict:
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
    container = {
        "name": "test",
        "image": _resolve_build_image(DEFAULT_NIX_IMAGE),
        "command": ["sh", "-c", f'timeout {seconds} sh -c "$TEST_COMMAND"'],
        "workingDir": "/work",
        "env": env,
        "securityContext": _hardened(),
    }
    pod = _pod(run, "investigate-test", [container])
    pod["volumes"].append({"name": "work", "emptyDir": {}})
    container["volumeMounts"].append({"name": "work", "mountPath": "/work"})
    pod["initContainers"] = [
        {
            "name": "copy-source",
            "image": _resolve_build_image(DEFAULT_NIX_IMAGE),
            "command": ["sh", "-c", "cp -a /src/. /work/"],
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


def secret_manifest(run: Any, job_uid: str) -> dict:
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


if not __debug__:  # the guard below is written as asserts; -O would strip it
    raise ImportError("investigation_manifest must not run with python -O")


class ManifestRefused(RuntimeError):
    """An investigation manifest carries something outside its allowlist."""


def assert_investigation_manifest(m: dict) -> None:
    """Reject an investigation Job that carries credentials or mounts beyond its role."""
    try:
        _check_investigation_manifest(m)
    except (AssertionError, KeyError, StopIteration, TypeError) as exc:
        raise ManifestRefused("investigation manifest refused") from exc


def _check_investigation_manifest(m: dict) -> None:
    assert_job_policy(m)
    pod = m["spec"]["template"]
    labels = pod["metadata"]["labels"]
    kind = labels.get("factory.io/run-kind")
    assert kind in {"investigate-agent", "investigate-test"}
    assert labels.get("factory.io/investigation")
    assert labels.get("factory.io/service") == "aifactory"
    assert labels.get("app") == "aifactory-investigate"
    assert "app.kubernetes.io/instance" not in labels
    assert "app.kubernetes.io/instance" not in m["metadata"].get("labels", {})
    spec = pod["spec"]
    assert spec.get("automountServiceAccountToken") is False
    assert not spec.get("serviceAccountName")
    assert set(spec) <= {
        "restartPolicy",
        "automountServiceAccountToken",
        "securityContext",
        "volumes",
        "containers",
        "initContainers",
    }
    allowed_volumes = {"src", "tmp"} | (
        {"work"} if kind == "investigate-test" else set()
    )
    assert {v["name"] for v in spec["volumes"]} == allowed_volumes
    assert len(spec["volumes"]) == len(allowed_volumes)
    assert all(
        set(v) == {"name", "persistentVolumeClaim"}
        if v["name"] == "src"
        else set(v) == {"name", "emptyDir"}
        for v in spec["volumes"]
    )
    assert next(v for v in spec["volumes"] if v["name"] == "src")[
        "persistentVolumeClaim"
    ] == {"claimName": os.environ.get(_ENV_REPO_PVC, "").strip()}
    assert next(v for v in spec["volumes"] if v["name"] == "tmp")["emptyDir"] == {
        "sizeLimit": "256Mi"
    }
    if kind == "investigate-test":
        assert next(v for v in spec["volumes"] if v["name"] == "work")["emptyDir"] == {}
    assert len(spec["containers"]) == 1
    assert len(spec.get("initContainers", [])) == (
        1 if kind == "investigate-test" else 0
    )
    for container in [*spec["containers"], *spec.get("initContainers", [])]:
        assert set(container) <= {
            "name",
            "image",
            "command",
            "workingDir",
            "env",
            "securityContext",
            "volumeMounts",
        }
        assert container["securityContext"] == _hardened()
        mounts = container["volumeMounts"]
        assert len(mounts) == len({mount["name"] for mount in mounts})
        assert all(mount["name"] in allowed_volumes for mount in mounts)
        assert all(
            mount["mountPath"]
            == {"src": "/src", "tmp": "/tmp", "work": "/work"}[mount["name"]]
            for mount in mounts
        )
        assert all(
            mount.get("readOnly") is True
            and mount.get("subPath")
            == f"investigations/{labels['factory.io/investigation']}/src"
            and set(mount) == {"name", "mountPath", "subPath", "readOnly"}
            for mount in mounts
            if mount["name"] == "src"
        )
        assert all(
            set(mount) == {"name", "mountPath"}
            for mount in mounts
            if mount["name"] != "src"
        )
        if container["name"] == "copy-source":
            assert "env" not in container
            assert {mount["name"] for mount in mounts} == {"src", "work"}
            continue
        assert {mount["name"] for mount in mounts} == allowed_volumes
        expected_env = (
            {
                "JOB_ID",
                "AIFACTORY_INVESTIGATION_ID",
                "AIFACTORY_API_URL",
                "HOME",
                "CLAUDE_CONFIG_DIR",
                "CLAUDE_CODE_OAUTH_TOKEN",
                "AIFACTORY_RUN_TOKEN",
            }
            if kind == "investigate-agent"
            else {"HTTPS_PROXY", "HTTP_PROXY", "CI", "TEST_COMMAND"}
        )
        assert len(container.get("env", [])) == len(expected_env)
        assert {e["name"] for e in container.get("env", [])} == expected_env
        for entry in container.get("env", []):
            assert set(entry) == (
                {"name", "valueFrom"} if "valueFrom" in entry else {"name", "value"}
            )
            if "valueFrom" in entry:
                assert kind == "investigate-agent"
                assert entry["name"] in {
                    "CLAUDE_CODE_OAUTH_TOKEN",
                    "AIFACTORY_RUN_TOKEN",
                }
                assert entry["valueFrom"] == {
                    "secretKeyRef": {
                        "name": m["metadata"]["name"].removesuffix("-agent"),
                        "key": entry["name"],
                    }
                }


def _selected(selector: dict, labels: dict[str, str]) -> bool:
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
    policies: list[dict], agent_labels: dict[str, str], test_labels: dict[str, str]
) -> str | None:
    """Return the first unsafe policy name, or None for exact pod isolation."""
    selected = {"agent": set(), "test": set()}
    names = set()
    for policy in policies:
        name = policy.get("metadata", {}).get("name", "unnamed network policy")
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
    if not any(name.endswith("-investigate-api") for name in names):
        return "missing investigate-api policy"
    return None
