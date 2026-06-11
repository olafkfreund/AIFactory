# Testing an authenticated web app with Factory (deploy → log in → find faults → record proof)

> End-to-end runbook for proving Factory can deploy a real authenticated web
> service to AWS, have **TFactory log in as a test user**, browser-test the UI,
> and record **screenshots + findings** as proof — then tear it all down.
>
> Validated 2026-06-11 on AWS account `533267307120` / `eu-west-2`: a FastAPI
> login + contact-form app on App Runner; the browser test logged in, caught a
> deliberate email-validation fault, and recorded 3 screenshots. Reusable command:
> **`/run-aws-webtest`**.

---

## 0. What you need

| Need | Detail |
|---|---|
| **AWS credentials** | An IAM key with App Runner + ECR + IAM-role perms. Stored as **GitHub repo secrets** for the CI path, or read from a local `.envrc` for the CLI path. **Never** put them in an agent's env (scrubbed by `core/auth.py`). |
| **Tools** (CLI path) | `terraform`, `docker`, `aws` CLI, `node`/`npx`. Browser tests run via the official `mcr.microsoft.com/playwright:vX.Y.Z-jammy` image (browsers baked in — avoids host/NixOS browser issues). |
| **The app** | Must expose a **login flow** the test can drive: a login URL, stable selectors for the username/password fields + submit, and a recognisable post-login URL/state. Stable element ids (`#username`, `#password`, `#login-submit`) make this painless. |
| **A test account** | A username/password the test logs in with. For a throwaway demo, app defaults are fine; for a real target, store them as a **test-target credential** (below). |

---

## 1. How Factory logs in as a test user — secrets & `.tfactory.yml`

TFactory keeps **test-target credentials** out of code and injects them at run time.

### 1a. Store the credential
Encrypted at rest (`EncryptedString`, KMS-backed), org-scoped, secret never returned:

```bash
curl -X POST "$TFACTORY_BASE/api/test-credentials" \
  -H "Authorization: Bearer $TFACTORY_TOKEN" -H 'content-type: application/json' \
  -d '{"org_id":"<project>","name":"webform-login","kind":"form",
       "username":"tester","secret":"test-pass-123"}'
```
`kind=form` stores a plaintext `username` + an **encrypted** `secret` (password).
(Other kinds: `api_token`, `basic_auth`, `totp`.)

### 1b. Reference it from `.tfactory.yml`
The target declares its login flow + names the credential. Two ref styles:
- **`env:NAME`** — resolved by the broker from the run environment. **Works today.**
- **`store:<id>`** — the encrypted DB credential. (Runtime hand-off is TFactory
  task #107-4b-final, *pending* — use `env:` until it lands.)

```yaml
targets:
  - name: webform
    type: http
    base_url: https://<your-app>.eu-west-2.awsapprunner.com
    auth:
      type: ref
      ref: login                       # → the test_credentials entry below
      login_url: /login
      username_selector: "#username"
      password_selector: "#password"
      submit_selector: "#login-submit"
      success_url_pattern: "**/app**"   # post-login URL must match

test_credentials:
  login:
    ref: env:TEST_PASSWORD             # or store:<cred-id> once 4b-final lands
    as_username: TEST_USERNAME
    as_secret: TEST_PASSWORD
    kind: form

egress:
  enabled: true                        # login needs network; required when test_credentials is set
```

### 1c. What TFactory generates
When a browser subtask is `requires_auth: true`, gen-functional scaffolds a
Playwright **`auth.setup.ts`** that logs in using the injected env vars and saves
the session (`storageState`), so every test reuses the authenticated session.
Credentials are **never inlined** — read from env at runtime; secrets are
redacted from logs/HAR/verdicts.

```ts
// auth.setup.ts (generated)
setup("authenticate", async ({ page }) => {
  await page.goto(process.env.TFACTORY_TARGET_URL + "/login");
  await page.locator("#username").fill(process.env.TEST_USERNAME ?? "");
  await page.locator("#password").fill(process.env.TEST_PASSWORD ?? "");
  await page.locator("#login-submit").click();
  await page.waitForURL("**/app**");
  await page.context().storageState({ path: ".auth/state.json" });
});
```

---

## 2. Browser-testing + recording proof

TFactory's **browser lane** runs Playwright in a container with
`TFACTORY_TARGET_URL` set to the live endpoint, using the
`visual-inspection.spec.ts` pattern: each verification step captures a
**full-page screenshot** named `NN-<slug>-{pass|fail}.png`.

Results are packaged (`agents/visual_inspection/packager.py`) into:
```
findings/evidence/<test_id>/
automated-test/<run-id>/
  report.md            # human-readable verdict + per-step table + screenshots
  meta.json            # machine-readable: steps, verdict (pass|fail|attention), errors
  screenshots/NN-*.png
  recording/video.webm · trace.zip
```
and surfaced via the portal API:
```
GET /api/visual-inspections                 # list runs
GET /api/visual-inspections/{run_id}         # report + meta + correction plan
GET /api/visual-inspections/{run_id}/download/{report.md|meta.json|report.pdf|...}
```
A failed step → `verdict: fail`, the error message, and the screenshot that
captured the fault = your proof. (P2 also renders a correction plan + GitHub
issue specs.)

---

## 3. Run it (two paths)

### CLI path — `/run-aws-webtest` (fastest to prove)
The bundled conductor: deploys the app to App Runner, runs the Playwright
login + visual-inspection against the live URL, records screenshots + findings,
and **always tears down** (EXIT trap). Cost-guarded: every resource is
`factory-ephemeral`/`spec_id` tagged + named `factory-<spec>-*`.
```
/run-aws-webtest          # deploy → login → test → record proof → teardown
```

### Autonomous (GitHub Actions) path
Push the app + generated `deploy.yml` + App Runner Terraform (S3 backend) to the
repo; the `on: push` deploy runs in Actions (AWS keys = repo secrets), then
TFactory ingests the spec + `deployed_url` and runs the browser lane. (Same
deterministic templates as `/run-aws-demo`; see `deploy_templates.py`.)

---

## 4. Cost guard / teardown (non-negotiable)
- The CLI conductor's EXIT trap runs `terraform destroy` even on failure/Ctrl-C.
- Everything is tagged `factory-ephemeral=true` + `spec_id=<spec>` and named
  `factory-<spec>-*`, so teardown (and any sweeper) can **never** touch other infra.
- Verify after a run:
  ```bash
  aws apprunner list-services --region <r> --query "ServiceSummaryList[?contains(ServiceName,'factory-')].ServiceName" --output json  # → []
  aws ecr describe-repositories --region <r> --query "repositories[?contains(repositoryName,'factory-')].repositoryName" --output json # → []
  ```

---

## 5. The deliberate-fault pattern (so you can prove fault-finding)
Ship a small, visible bug for the test to catch. In the validated demo the
contact form's **email validation was broken** — it accepted `not-an-email` and
showed "saved" instead of an error. The test asserts the invalid input is
rejected; the bug makes that assertion fail → a `fail` step + the
`03-invalid-email-ACCEPTED-fault.png` screenshot. Swap in your own
assertions/faults to prove Factory can test (and record) almost anything.

## 6. Gaps / notes
- `store:<id>` credential runtime injection is pending (TFactory #107-4b-final);
  `env:` refs work now.
- Running the browser lane **inside** a deployed TFactory pod needs docker-for-Playwright
  in the pod; the CLI/CI path runs the same Playwright image directly against the
  live URL (faithful, robust). Productionising the in-pod browser lane against a
  deployed `deployed_url` is tracked alongside the deploy-then-verify wiring.
