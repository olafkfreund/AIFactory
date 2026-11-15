"""Helm chart acceptance tests for the Alembic migration Job."""

from __future__ import annotations

import subprocess

import pytest
import yaml


def _render(chart_dir, set_values: list[str] | None = None) -> list[dict]:
    cmd = ["helm", "template", "test-release", str(chart_dir)]
    for kv in set_values or []:
        cmd.extend(["--set", kv])
    out = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def _migration_jobs(docs: list[dict]) -> list[dict]:
    return [
        doc
        for doc in docs
        if doc.get("kind") == "Job"
        and doc.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/component")
        == "migrations"
    ]


@pytest.mark.helm
class TestMigrationsJob:
    def test_default_job(self, helm_available, chart_dir) -> None:
        jobs = _migration_jobs(_render(chart_dir))
        assert len(jobs) == 1

        job = jobs[0]
        assert (
            job["metadata"]["annotations"]["helm.sh/hook"] == "pre-install,pre-upgrade"
        )
        # Without before-hook-creation the second upgrade fails: Job already exists.
        assert (
            "before-hook-creation"
            in (job["metadata"]["annotations"]["helm.sh/hook-delete-policy"])
        )
        # Job pods must not carry the Service selector labels (AIFactory#1107).
        pod_labels = job["spec"]["template"]["metadata"]["labels"]
        assert pod_labels["app.kubernetes.io/name"] == "aifactory-migrations"
        pod = job["spec"]["template"]["spec"]
        # The chart ServiceAccount does not exist at pre-install.
        assert "serviceAccountName" not in pod
        assert pod["automountServiceAccountToken"] is False
        container = pod["containers"][0]
        assert container["command"][-2:] == ["upgrade", "head"]
        database_url = next(
            env for env in container["env"] if env["name"] == "DATABASE_URL"
        )
        assert database_url["valueFrom"]["secretKeyRef"] == {
            "name": "aifactory-db",
            "key": "database-url",
        }

    def test_auto_apply_suppresses_job(self, helm_available, chart_dir) -> None:
        assert _migration_jobs(_render(chart_dir, ["migrations.autoApply=true"])) == []

    def test_job_image_override(self, helm_available, chart_dir) -> None:
        jobs = _migration_jobs(_render(chart_dir, ["migrations.jobImage=x/y:z"]))
        assert len(jobs) == 1
        assert jobs[0]["spec"]["template"]["spec"]["containers"][0]["image"] == "x/y:z"


def _job_spec(chart_dir, set_values: list[str] | None = None) -> dict:
    return _migration_jobs(_render(chart_dir, set_values))[0]["spec"]


def _raw(chart_dir, template: str, set_values: list[str]) -> str:
    cmd = ["helm", "template", "test-release", str(chart_dir), "-s", template]
    for kv in set_values:
        cmd.extend(["--set", kv])
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout


CA = "global.customCABundle.secretName=corp-ca"


@pytest.mark.helm
class TestMigrationsJobHardening:
    @pytest.mark.parametrize(
        ("sets", "expected"),
        [
            ([], 300),
            (["migrations.jobActiveDeadlineSeconds=900"], 900),
            (["migrations.jobActiveDeadlineSeconds=0"], None),
            (["migrations.jobActiveDeadlineSeconds=null"], None),
        ],
    )
    def test_deadline(self, helm_available, chart_dir, sets, expected) -> None:
        spec = _job_spec(chart_dir, sets)
        assert spec.get("activeDeadlineSeconds") == expected

    def test_negative_deadline_rejected(self, helm_available, chart_dir) -> None:
        with pytest.raises(subprocess.CalledProcessError):
            _render(chart_dir, ["migrations.jobActiveDeadlineSeconds=-1"])

    def test_custom_ca_off_by_default(self, helm_available, chart_dir) -> None:
        pod = _job_spec(chart_dir)["template"]["spec"]
        names = {e["name"] for e in pod["containers"][0]["env"]}
        assert not names & {"SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"}
        assert "custom-ca" not in {v["name"] for v in pod["volumes"]}

    def test_custom_ca_on(self, helm_available, chart_dir) -> None:
        pod = _job_spec(chart_dir, [CA, "global.customCABundle.key=my.pem"])[
            "template"
        ]["spec"]
        container = pod["containers"][0]
        env = {e["name"]: e.get("value") for e in container["env"]}
        assert env["SSL_CERT_FILE"] == "/etc/ssl/custom-ca/my.pem"
        assert env["REQUESTS_CA_BUNDLE"] == "/etc/ssl/custom-ca/my.pem"
        assert {
            "name": "custom-ca",
            "mountPath": "/etc/ssl/custom-ca",
            "readOnly": True,
        } in container["volumeMounts"]
        assert {"name": "custom-ca", "secret": {"secretName": "corp-ca"}} in pod[
            "volumes"
        ]

    def test_resources_default_and_override(self, helm_available, chart_dir) -> None:
        docs = _render(chart_dir)
        web = next(d for d in docs if d.get("kind") == "Deployment")
        web_res = web["spec"]["template"]["spec"]["containers"][0]["resources"]
        job_res = _migration_jobs(docs)[0]["spec"]["template"]["spec"]["containers"][0][
            "resources"
        ]
        assert job_res == web_res
        spec = _job_spec(chart_dir, ["migrations.jobResources.limits.cpu=100m"])
        assert spec["template"]["spec"]["containers"][0]["resources"] == {
            "limits": {"cpu": "100m"}
        }


G = "global.imagePullSecrets[0].name=g"
IMG = "image.pullSecrets[0].name=i"
IMG_DUP = "image.pullSecrets[1].name=g"


@pytest.mark.helm
class TestImagePullSecrets:
    @pytest.mark.parametrize("template", ["job-migrations.yaml", "deployment.yaml"])
    @pytest.mark.parametrize(
        ("sets", "expected"),
        [
            ([G, IMG], ["g", "i"]),
            ([G, IMG, IMG_DUP], ["g", "i"]),
            ([G], ["g"]),
            ([IMG], ["i"]),
            ([], None),
            (["global.imagePullSecrets=null", "image.pullSecrets=null"], None),
            (["global.imagePullSecrets=null", IMG], ["i"]),
            ([G, "image.pullSecrets=null"], ["g"]),
        ],
    )
    def test_merge(self, helm_available, chart_dir, template, sets, expected) -> None:
        out = _raw(chart_dir, f"templates/{template}", sets)
        # Raw count: yaml.safe_load hides a duplicate key.
        assert out.count("imagePullSecrets:") == (0 if expected is None else 1)
        if expected is not None:
            doc = yaml.safe_load(out)
            pod = doc["spec"]["template"]["spec"]
            assert [s["name"] for s in pod["imagePullSecrets"]] == expected
