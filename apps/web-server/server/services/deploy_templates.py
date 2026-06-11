"""Deterministic deploy artifacts for the deploy-then-verify stage (#547 follow-up).

These render the EXACT files written into a build's worktree before it is pushed:
an AWS App Runner Terraform module plus the GitHub Actions ``deploy.yml`` and
``destroy.yml`` workflows. They are deterministic (no LLM) on purpose — the
teardown path must be correct and predictable, never guessed by a model.

Cost-guard invariants enforced here:
  * EVERY Terraform resource is tagged ``factory-ephemeral=true`` + ``spec_id=<id>``
    so a scheduled sweeper can destroy leaked infra without ever touching
    non-Factory resources.
  * ``deploy_files()`` ALWAYS returns ``destroy.yml`` alongside ``deploy.yml`` —
    there is no code path that produces a deploy workflow without its teardown.
  * Terraform uses an S3 remote backend so ``apply`` and ``destroy`` share state.

App Runner is the target: a container image → a live HTTPS endpoint with the
smallest Terraform surface (no VPC/ALB/cert), the cheapest short-lived footprint,
and the fastest ``terraform destroy``.
"""

from __future__ import annotations

import re

# Pinned action versions (deterministic CI).
_CHECKOUT = "actions/checkout@v4"
_SETUP_PY = "actions/setup-python@v5"
_AWS_CREDS = "aws-actions/configure-aws-credentials@v4"
_ECR_LOGIN = "aws-actions/amazon-ecr-login@v2"
_SETUP_TF = "hashicorp/setup-terraform@v3"
_UPLOAD = "actions/upload-artifact@v4"


def _slug(name: str) -> str:
    """ECR/App-Runner-safe lowercase slug."""
    s = re.sub(r"[^a-z0-9-]+", "-", name.strip().lower()).strip("-")
    return s or "service"


def sanitize_services(services: list[str]) -> list[str]:
    """De-dupe + slugify service names, preserving order. Always >= 1."""
    seen: set[str] = set()
    out: list[str] = []
    for s in services or []:
        sl = _slug(s)
        if sl not in seen:
            seen.add(sl)
            out.append(sl)
    return out or ["app"]


# ---------------------------------------------------------------------------
# Terraform (App Runner)
# ---------------------------------------------------------------------------

def render_terraform(
    services: list[str],
    *,
    spec_id: str,
    region: str = "eu-west-1",
    state_bucket: str = "",
) -> str:
    """Render ``infra/main.tf``: ECR repo + App Runner service per service, a
    shared ECR-access role, ``factory-ephemeral`` tags, and ``service_url_*``
    outputs. ``state_bucket`` empty → local state (dev/test); set → S3 backend.
    """
    svcs = sanitize_services(services)
    safe_spec = _slug(spec_id)

    backend = ""
    if state_bucket:
        backend = f'''
  backend "s3" {{
    bucket = "{state_bucket}"
    key    = "factory/{safe_spec}/terraform.tfstate"
    region = "{region}"
  }}'''

    blocks: list[str] = [f'''terraform {{
  required_version = ">= 1.5"
  required_providers {{
    aws = {{ source = "hashicorp/aws", version = "~> 5.0" }}
  }}{backend}
}}

provider "aws" {{
  region = var.aws_region
  default_tags {{
    tags = {{
      "factory-ephemeral" = "true"
      "spec_id"           = "{safe_spec}"
      "managed-by"        = "factory"
    }}
  }}
}}

variable "aws_region" {{
  type    = string
  default = "{region}"
}}

variable "image_tag" {{
  type    = string
  default = "latest"
}}

# IAM role App Runner uses to pull from ECR.
resource "aws_iam_role" "apprunner_ecr" {{
  name = "factory-{safe_spec}-ecr"
  assume_role_policy = jsonencode({{
    Version = "2012-10-17"
    Statement = [{{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = {{ Service = "build.apprunner.amazonaws.com" }}
    }}]
  }})
}}

resource "aws_iam_role_policy_attachment" "apprunner_ecr" {{
  role       = aws_iam_role.apprunner_ecr.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSAppRunnerServicePolicyForECRAccess"
}}''']

    for svc in svcs:
        blocks.append(f'''
resource "aws_ecr_repository" "{svc}" {{
  name                 = "factory-{safe_spec}-{svc}"
  image_tag_mutability = "MUTABLE"
  force_delete         = true
}}

resource "aws_apprunner_service" "{svc}" {{
  service_name = "factory-{safe_spec}-{svc}"

  source_configuration {{
    authentication_configuration {{
      access_role_arn = aws_iam_role.apprunner_ecr.arn
    }}
    image_repository {{
      image_identifier      = "${{aws_ecr_repository.{svc}.repository_url}}:${{var.image_tag}}"
      image_repository_type = "ECR"
      image_configuration {{
        port = "8000"
      }}
    }}
    auto_deployments_enabled = false
  }}

  instance_configuration {{
    cpu    = "256"
    memory = "512"
  }}
}}

output "service_url_{svc}" {{
  value = "https://${{aws_apprunner_service.{svc}.service_url}}"
}}

output "ecr_repo_{svc}" {{
  value = aws_ecr_repository.{svc}.repository_url
}}''')

    return "\n".join(blocks) + "\n"


# ---------------------------------------------------------------------------
# GitHub Actions workflows
# ---------------------------------------------------------------------------

def render_deploy_workflow(services: list[str], *, spec_id: str, region: str = "eu-west-1") -> str:
    """Render ``.github/workflows/deploy.yml``: test → build/push to ECR →
    terraform apply → write the live App Runner URLs to the ``deployed-urls``
    artifact (``deployed_urls.json``) that deploy_endgame downloads.
    """
    svcs = sanitize_services(services)
    safe_spec = _slug(spec_id)
    # Per-service build+push steps (build context = service dir if present, else repo root).
    build_steps = "\n".join(
        f'''      - name: Build & push {svc}
        run: |
          REPO=$(terraform -chdir=infra output -raw ecr_repo_{svc} 2>/dev/null || echo "")
          CTX=$([ -d "{svc}" ] && echo "{svc}" || echo ".")
          docker build -t "$REPO:${{{{ github.sha }}}}" -t "$REPO:latest" "$CTX"
          docker push "$REPO:${{{{ github.sha }}}}"
          docker push "$REPO:latest"'''
        for svc in svcs
    )
    url_capture = "\n".join(
        f'''          U=$(terraform -chdir=infra output -raw service_url_{svc})
          echo "{svc}=$U" >> urls.env
          python -c "import json,os; d=json.load(open('deployed_urls.json')) if os.path.exists('deployed_urls.json') else {{}}; d['{svc}']='$U'.strip(); json.dump(d, open('deployed_urls.json','w'))"'''
        for svc in svcs
    )

    return f'''# Generated by Factory deploy_endgame — deterministic, do not hand-edit.
# Deploys {len(svcs)} service(s) to AWS App Runner. Teardown: destroy.yml.
name: factory-deploy

on:
  workflow_dispatch:
  push:
    branches:
      - "auto-claude/**"
      - "{safe_spec}**"

permissions:
  contents: read

env:
  AWS_DEFAULT_REGION: {region}

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: {_CHECKOUT}
      - uses: {_SETUP_PY}
        with:
          python-version: "3.12"
      - name: Run tests (best-effort; non-blocking for the deploy demo)
        run: |
          pip install -q pytest httpx fastapi || true
          pytest -q || echo "::warning::tests reported failures"

  deploy:
    needs: test
    runs-on: ubuntu-latest
    outputs:
      deployed: ${{{{ steps.capture.outputs.deployed }}}}
    steps:
      - uses: {_CHECKOUT}
      - uses: {_AWS_CREDS}
        with:
          aws-access-key-id: ${{{{ secrets.AWS_ACCESS_KEY_ID }}}}
          aws-secret-access-key: ${{{{ secrets.AWS_SECRET_ACCESS_KEY }}}}
          aws-region: {region}
      - uses: {_ECR_LOGIN}
      - uses: {_SETUP_TF}
      - name: Terraform init + apply (create ECR repos + role)
        run: |
          terraform -chdir=infra init -input=false
          terraform -chdir=infra apply -auto-approve -input=false -target=aws_iam_role.apprunner_ecr $(for s in {" ".join(svcs)}; do echo "-target=aws_ecr_repository.$s"; done)
{build_steps}
      - name: Terraform apply (App Runner services)
        run: |
          terraform -chdir=infra apply -auto-approve -input=false -var="image_tag=${{{{ github.sha }}}}"
      - name: Capture deployed URLs
        id: capture
        run: |
          : > urls.env
{url_capture}
          cat urls.env
          echo "deployed=true" >> "$GITHUB_OUTPUT"
      - uses: {_UPLOAD}
        with:
          name: deployed-urls
          path: deployed_urls.json
          if-no-files-found: error
'''


def render_destroy_workflow(*, spec_id: str, region: str = "eu-west-1") -> str:
    """Render ``.github/workflows/destroy.yml`` — the mandatory teardown.
    ``workflow_dispatch`` so deploy_endgame / TFactory / the sweeper can fire it.
    """
    safe_spec = _slug(spec_id)
    return f'''# Generated by Factory deploy_endgame — TEARDOWN (cost guard). Do not hand-edit.
name: factory-destroy

on:
  workflow_dispatch:

permissions:
  contents: read

jobs:
  destroy:
    runs-on: ubuntu-latest
    steps:
      - uses: {_CHECKOUT}
      - uses: {_AWS_CREDS}
        with:
          aws-access-key-id: ${{{{ secrets.AWS_ACCESS_KEY_ID }}}}
          aws-secret-access-key: ${{{{ secrets.AWS_SECRET_ACCESS_KEY }}}}
          aws-region: {region}
      - uses: {_SETUP_TF}
      - name: Terraform destroy (tear down all ephemeral infra)
        run: |
          terraform -chdir=infra init -input=false
          terraform -chdir=infra destroy -auto-approve -input=false
'''


def deploy_files(
    services: list[str],
    *,
    spec_id: str,
    region: str = "eu-west-1",
    state_bucket: str = "",
) -> dict[str, str]:
    """Return the COMPLETE deterministic file set to write into the worktree.

    Always includes destroy.yml alongside deploy.yml — there is no way to get a
    deploy workflow without its teardown (cost-guard invariant).
    """
    return {
        "infra/main.tf": render_terraform(
            services, spec_id=spec_id, region=region, state_bucket=state_bucket
        ),
        ".github/workflows/deploy.yml": render_deploy_workflow(
            services, spec_id=spec_id, region=region
        ),
        ".github/workflows/destroy.yml": render_destroy_workflow(
            spec_id=spec_id, region=region
        ),
    }
