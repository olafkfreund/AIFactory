"""The chart's ServiceAccount must be granted what the build lane calls (#1199).

Factory#550 fixed the half of this that was a lie about a control: the chart
declared `serviceAccount.automountServiceAccountToken: false` while the app
needs the token. But a token with no RoleBinding authorises nothing, so a
self-hoster running `helm install` still got a control plane that reached the
API server and was refused on every call — builds fail at Job creation. The
reference cluster never hit it because factory-gitops declares its own
`aifactory-sandbox` Role; only the published chart was affected.

These tests assert the grant matches what the code actually calls, and that it
is namespace-scoped. The live proof (a real `helm install` on k3d, a Job created
AS the ServiceAccount, its logs read AS the ServiceAccount) is in the PR.
"""

from __future__ import annotations

import subprocess

import pytest
import yaml

# What the four in-cluster call sites need, verb by verb:
#   apps/backend/core/kube_sandbox.py           create/read/delete Job,
#                                               list pods, read pod log
#   apps/web-server/.../build_backend.py        create/read/delete Job
#   apps/web-server/.../build_log_stream.py     list pods, read pod log (follow)
REQUIRED_GRANTS = {
    ("batch", "jobs"): {"create", "get", "delete"},
    ("", "pods"): {"list"},
    ("", "pods/log"): {"get"},
}


def _docs(rendered: str) -> list[dict]:
    return [d for d in yaml.safe_load_all(rendered) if d]


def _sandbox(docs: list[dict], kind: str) -> dict | None:
    for doc in docs:
        if doc.get("kind") == kind and doc["metadata"]["name"].endswith("-sandbox"):
            return doc
    return None


@pytest.mark.helm
def test_default_install_grants_the_build_lane_what_it_calls(helm_template) -> None:
    docs = _docs(helm_template)
    role = _sandbox(docs, "Role")
    assert role is not None, (
        "a default `helm install` ships no Role for the app's ServiceAccount, so "
        "the build lane is refused on every API call"
    )

    granted: dict[tuple[str, str], set[str]] = {}
    for rule in role["rules"]:
        for group in rule["apiGroups"]:
            for resource in rule["resources"]:
                granted.setdefault((group, resource), set()).update(rule["verbs"])

    for key, verbs in REQUIRED_GRANTS.items():
        assert key in granted, f"no rule covers {key}"
        missing = verbs - granted[key]
        assert not missing, f"{key} is missing {sorted(missing)}"


@pytest.mark.helm
def test_the_role_is_bound_to_the_app_service_account(helm_template) -> None:
    """An unbound Role grants nothing. This is the half that is easy to miss."""
    docs = _docs(helm_template)
    binding = _sandbox(docs, "RoleBinding")
    assert binding is not None

    sa = next(
        d
        for d in docs
        if d["kind"] == "ServiceAccount" and "tenant" not in d["metadata"]["name"]
    )
    subjects = [
        s
        for s in binding["subjects"]
        if s["kind"] == "ServiceAccount" and s["name"] == sa["metadata"]["name"]
    ]
    assert subjects, (
        f"the Role is not bound to {sa['metadata']['name']}, the SA the "
        "Deployment actually runs as"
    )
    assert binding["roleRef"]["kind"] == "Role"
    assert binding["roleRef"]["name"] == _sandbox(docs, "Role")["metadata"]["name"]


@pytest.mark.helm
def test_the_grant_is_namespace_scoped_and_least_privilege(helm_template) -> None:
    """A ClusterRole here would be a cluster-wide job-runner. It must not be."""
    docs = _docs(helm_template)
    role = _sandbox(docs, "Role")

    assert role["kind"] == "Role", "the sandbox grant must not be cluster-wide"

    granted = {
        (group, resource)
        for rule in role["rules"]
        for group in rule["apiGroups"]
        for resource in rule["resources"]
    }
    # Nothing the code does not call. secrets in particular: the SA token is
    # mounted, so a secrets read here would widen the blast radius of a
    # compromised control-plane pod well past "runs builds".
    for forbidden in (("", "secrets"), ("", "serviceaccounts"), ("", "namespaces")):
        assert forbidden not in granted, f"{forbidden} is not called by any build path"

    pod_verbs = {
        v
        for rule in role["rules"]
        for v in rule["verbs"]
        if "pods" in rule["resources"] and rule["apiGroups"] == [""]
    }
    assert "delete" not in pod_verbs and "create" not in pod_verbs, (
        "the build lane creates Jobs, never bare pods; the Job controller owns "
        "their lifecycle"
    )


@pytest.mark.helm
def test_disabling_the_flag_renders_no_grant(chart_dir, helm_available) -> None:
    """Mutation check: the flag has to be what produces the Role.

    Without this, a Role hard-coded into the chart would satisfy every
    assertion above while the documented opt-out did nothing.
    """
    result = subprocess.run(
        [
            "helm",
            "template",
            "aifactory",
            str(chart_dir),
            "--set",
            "rbac.jobSandbox.enabled=false",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr[-1500:]

    docs = _docs(result.stdout)
    assert _sandbox(docs, "Role") is None
    assert _sandbox(docs, "RoleBinding") is None


@pytest.mark.helm
def test_rbac_create_is_still_inert_and_says_so(chart_dir, helm_available) -> None:
    """`rbac.create` is referenced by no template; flipping it must render nothing.

    It read as the switch that would have fixed this, which is why the gap
    survived: an operator setting `rbac.create=true` got exactly nothing.
    """
    rendered = {}
    for value in ("true", "false"):
        result = subprocess.run(
            [
                "helm",
                "template",
                "aifactory",
                str(chart_dir),
                "--set",
                f"rbac.create={value}",
                "--set",
                "rbac.jobSandbox.enabled=false",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr[-1500:]
        rendered[value] = result.stdout

    assert rendered["true"] == rendered["false"], (
        "rbac.create now changes the render; either wire it up properly or keep "
        "the values.yaml comment honest about it being a no-op"
    )
