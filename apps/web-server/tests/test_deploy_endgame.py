"""Tests for the deploy-then-verify stage (deploy_templates + deploy_endgame).

All git/gh/network goes through an injected runner — no real cloud, no network.
The load-bearing assertions are the COST GUARDS: destroy always ships with
deploy, and teardown fires on any failed/timed-out deploy.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile

import pytest
import yaml

from server.services import deploy_endgame as de
from server.services import deploy_templates as dt


# --- deploy_templates: deterministic + cost-guard invariants ----------------

def test_deploy_files_always_includes_destroy():
    files = dt.deploy_files(["frontend", "scoreboard"], spec_id="abc")
    assert ".github/workflows/deploy.yml" in files
    assert ".github/workflows/destroy.yml" in files  # never one without the other


def test_terraform_tags_every_resource_ephemeral():
    tf = dt.render_terraform(["a", "b"], spec_id="demo")
    assert "factory-ephemeral" in tf
    assert "spec_id" in tf
    assert tf.count('resource "aws_apprunner_service"') == 2
    assert tf.count('resource "aws_ecr_repository"') == 2
    assert "force_delete         = true" in tf  # ECR delete never blocks destroy


def test_terraform_s3_backend_only_when_bucket_set():
    assert 'backend "s3"' not in dt.render_terraform(["a"], spec_id="x")
    assert 'backend "s3"' in dt.render_terraform(["a"], spec_id="x", state_bucket="b")


def test_workflows_are_valid_yaml():
    files = dt.deploy_files(["frontend"], spec_id="x")
    yaml.safe_load(files[".github/workflows/deploy.yml"])
    yaml.safe_load(files[".github/workflows/destroy.yml"])


def test_destroy_workflow_runs_terraform_destroy():
    wf = dt.render_destroy_workflow(spec_id="x")
    assert "destroy -auto-approve" in wf


def test_sanitize_services_dedupes_and_slugs():
    assert dt.sanitize_services(["My Svc", "my-svc", "Other!"]) == ["my-svc", "other"]
    assert dt.sanitize_services([]) == ["app"]


# --- deploy_endgame: flag gate + orchestration ------------------------------

def test_flag_default_off(tmp_path, monkeypatch):
    monkeypatch.delenv("AIFACTORY_AUTO_DEPLOY", raising=False)
    assert de.is_deploy_enabled(tmp_path) is False


def test_parse_repo():
    assert de.parse_repo("owner/name") == ("owner", "name")
    assert de.parse_repo("https://github.com/owner/name.git") == ("owner", "name")
    assert de.parse_repo("") is None


def test_set_repo_secrets_passes_values_by_argv():
    calls = []
    def runner(argv, cwd=None):
        calls.append(argv)
        return de.CmdResult(0, "", "")
    ok = de.set_repo_secrets("o", "r", {"AWS_ACCESS_KEY_ID": "AKIA", "AWS_SECRET_ACCESS_KEY": "sek"}, runner=runner)
    assert ok
    assert ["gh", "secret", "set", "AWS_ACCESS_KEY_ID", "--repo", "o/r", "--body", "AKIA"] in calls


def test_set_repo_secrets_fails_on_missing_cred():
    ok = de.set_repo_secrets("o", "r", {"AWS_ACCESS_KEY_ID": "x"}, runner=lambda a, c=None: de.CmdResult(0, "", ""))
    assert ok is False


def _happy_runner(calls):
    def runner(argv, cwd=None):
        calls.append(argv)
        if argv[:3] == ["gh", "run", "list"]:
            return de.CmdResult(0, json.dumps([{"databaseId": 42, "headSha": "abcdef1234567890"}]), "")
        if argv[:3] == ["gh", "run", "view"]:
            return de.CmdResult(0, json.dumps({"status": "completed", "conclusion": "success"}), "")
        if argv[:2] == ["git", "rev-parse"]:
            return de.CmdResult(0, "abcdef1234567890", "")
        return de.CmdResult(0, "", "")
    return runner


def test_run_deploy_endgame_happy_path(monkeypatch):
    calls = []
    wt = tempfile.mkdtemp()
    os.makedirs(os.path.join(wt, "frontend"))
    open(os.path.join(wt, "frontend", "main.py"), "w").close()
    spec = tempfile.mkdtemp()

    def fake_capture(o, r, rid, dest, *, runner):
        json.dump({"frontend": "https://x.awsapprunner.com"}, open(os.path.join(dest, "deployed_urls.json"), "w"))
        return {"frontend": "https://x.awsapprunner.com"}
    monkeypatch.setattr(de, "capture_deployed_url", fake_capture)

    res = asyncio.run(de.run_deploy_endgame(
        spec_dir=spec, spec_id="demo", worktree=wt, branch="auto-claude/x",
        repo="o/r", creds={"AWS_ACCESS_KEY_ID": "a", "AWS_SECRET_ACCESS_KEY": "b"},
        runner=_happy_runner(calls),
    ))
    assert res["deployed"] is True
    assert res["deployed_url"] == "https://x.awsapprunner.com"
    assert os.path.exists(os.path.join(wt, ".github/workflows/destroy.yml"))
    assert os.path.exists(os.path.join(spec, "deploy_result.json"))


def test_run_deploy_endgame_teardown_on_failure():
    calls = []
    wt = tempfile.mkdtemp()
    def runner(argv, cwd=None):
        calls.append(argv)
        if argv[:3] == ["gh", "run", "view"]:
            return de.CmdResult(0, json.dumps({"status": "completed", "conclusion": "failure"}), "")
        if argv[:3] == ["gh", "run", "list"]:
            return de.CmdResult(0, json.dumps([{"databaseId": 9, "headSha": "deadbeef"}]), "")
        if argv[:2] == ["git", "rev-parse"]:
            return de.CmdResult(0, "deadbeef", "")
        return de.CmdResult(0, "", "")
    res = asyncio.run(de.run_deploy_endgame(
        spec_dir=tempfile.mkdtemp(), spec_id="x", worktree=wt, branch="b",
        repo="o/r", creds={"AWS_ACCESS_KEY_ID": "a", "AWS_SECRET_ACCESS_KEY": "b"},
        runner=runner,
    ))
    assert res["deployed"] is False
    assert any(c[:3] == ["gh", "workflow", "run"] and "destroy.yml" in c for c in calls), "teardown must fire"


def test_run_deploy_endgame_no_creds_skips():
    res = asyncio.run(de.run_deploy_endgame(
        spec_dir=tempfile.mkdtemp(), spec_id="x", worktree=tempfile.mkdtemp(), branch="b",
        repo="o/r", creds={}, runner=lambda a, c=None: de.CmdResult(0, "", ""),
    ))
    assert res["deployed"] is False
    assert res["reason"] == "no_aws_creds"
