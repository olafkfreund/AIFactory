# Deploy-then-verify demo (real AWS App Runner)

A CLI conductor that deploys 2 FastAPI services to real AWS App Runner via the
Factory's deterministic Terraform (`deploy_templates.py`), proves the live API,
runs the AC-mapped tests against the live endpoints, and tears everything down.

    bash conductor.sh   # needs AWS creds (see ENVRC in the script) + docker + terraform

Cost-guarded: every resource is `factory-ephemeral`/`spec_id` tagged; the
teardown trap runs `terraform destroy` even on failure. Screencast:
https://factory.freundcloud.com/blog/2026/06/11/deploy-then-verify-on-real-aws/
