# github-actions

> Source: curated best practices | 2026

---

# GitHub Actions - Secure, cached, least-privilege CI/CD

GitHub Actions runs CI/CD workflows on events. A production workflow pins its actions, sets least-privilege `permissions`, caches dependencies, runs jobs concurrently where possible, and uses OIDC for cloud auth instead of long-lived secrets. The main risks are over-permissioned `GITHUB_TOKEN`, unpinned third-party actions (supply-chain), and secrets leaking into logs or untrusted PR contexts. This skill covers workflow structure, caching, matrix builds, least-privilege tokens, OIDC, and safe secret handling.

## When to Activate

Use when the task involves GitHub Actions / CI:
- Writing or reviewing `.github/workflows/*.yml`
- Build/test/lint pipelines, matrix builds, caching
- Deployment jobs, environments, approvals
- Secrets, `GITHUB_TOKEN` permissions, or OIDC cloud auth
- Reusable workflows / composite actions

## Patterns and Best Practices

### CI workflow — least privilege, pinned, cached, concurrent

```yaml
name: ci
on:
  push: { branches: [main] }
  pull_request:

permissions:
  contents: read              # default to read-only; grant more per-job as needed

concurrency:                  # cancel superseded runs on the same ref
  group: ci-${{ github.ref }}
  cancel-in-progress: true

jobs:
  test:
    runs-on: ubuntu-24.04
    strategy:
      fail-fast: false
      matrix:
        python: ["3.11", "3.12", "3.13"]
    steps:
      - uses: actions/checkout@v4      # pin to SHA for third-party actions (see below)
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python }}
          cache: pip                    # built-in dependency caching
      - run: pip install -r requirements.txt
      - run: ruff check . && pytest -q
```

### Pin third-party actions by SHA (supply-chain)

```yaml
# A moved tag can be repointed to malicious code. Pin external actions to a full SHA:
- uses: some-org/some-action@3f0e...c9a  # v2.1.0
```

First-party `actions/*` are commonly pinned by major tag; pin anything else to an immutable commit SHA and let Dependabot bump them.

### Least-privilege token, per-job escalation

```yaml
jobs:
  release:
    permissions:
      contents: write          # only this job can write (tags/releases)
      id-token: write          # for OIDC
    runs-on: ubuntu-24.04
    steps: [...]
```

Start the workflow at `contents: read` and grant additional scopes only to the jobs that need them. Never blanket `permissions: write-all`.

### OIDC cloud auth — no long-lived cloud keys

```yaml
    permissions:
      id-token: write
      contents: read
    steps:
      - uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: arn:aws:iam::123456789012:role/gha-deploy
          aws-region: eu-west-1
      - run: aws s3 sync ./dist s3://bucket
```

OIDC exchanges a short-lived GitHub-signed token for cloud credentials — no static `AWS_ACCESS_KEY_ID` secret to leak or rotate. Scope the assumed IAM role's trust policy to the specific repo and branch.

### Caching custom dependencies

```yaml
      - uses: actions/cache@v4
        with:
          path: ~/.cache/my-tool
          key: mytool-${{ runner.os }}-${{ hashFiles('lockfile') }}
          restore-keys: mytool-${{ runner.os }}-
```

Key the cache on a lockfile hash so it invalidates precisely when deps change.

### Deployment with environment protection

```yaml
  deploy:
    needs: test
    if: github.ref == 'refs/heads/main'
    environment: production      # required reviewers / wait timer enforced by GitHub
    runs-on: ubuntu-24.04
    steps: [...]
```

`environment: production` gates the job behind configured protection rules (manual approval, branch restrictions) and scopes environment secrets.

### Secret handling

- Reference secrets via `${{ secrets.NAME }}`; never `echo` them or pass into logs.
- Do **not** expose secrets to `pull_request` workflows from forks (untrusted code). Use `pull_request_target` only with extreme care and never check out + run untrusted PR code with secrets present.
- Prefer OIDC over stored cloud keys; rotate any static secret that must exist.

## Anti-patterns

- `permissions: write-all` or default broad token — over-privileged; scope per job.
- Unpinned third-party actions (`@main`/moving tag) — supply-chain compromise.
- Long-lived cloud access keys in secrets instead of OIDC.
- Running untrusted fork PR code with secrets available (`pull_request_target` misuse).
- `echo ${{ secrets.X }}` or writing secrets to logs/artifacts.
- No dependency caching — slow, expensive builds.
- No `concurrency` cancel — stale runs pile up and waste minutes.
- One mega-job doing everything serially instead of parallel matrix/jobs with `needs`.
