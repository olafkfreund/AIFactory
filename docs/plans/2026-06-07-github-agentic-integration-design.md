# GitHub Agentic Integration Design

> Created: 2026-06-07
> Status: Approved for implementation
> Scope: 4 components — GitHub Models provider, Copilot cloud agent dispatch, AIFactory MCP server, GitHub Actions workflows

## Summary

AIFactory gains a full bidirectional integration with GitHub's agentic surface:

1. **GitHub Models provider** — free OpenAI-compatible inference via `github-models/*` model strings; zero extra billing in Actions
2. **Copilot cloud agent dispatch** — label-triggered delegation of tasks to `copilot-swe-agent[bot]`; AIFactory monitors for the resulting PR and triggers its review engine
3. **AIFactory as MCP server** — FastAPI router at `/mcp` exposes AIFactory's spec, plan, context, and memory tools so the Copilot cloud agent can call home during its coding session
4. **GitHub Actions workflows** — three workflows wire issue creation, Copilot PRs, and general PRs into the AIFactory pipeline; supplemented by GitHub's new Copilot automations (GA 2026-06-02)

All four components are additive and opt-in. Existing tasks and providers are unaffected when new config is absent.

---

## Background

### What GitHub has shipped (as of 2026-06)

| Feature | Surface | Trigger |
|---|---|---|
| Copilot Cloud Agent (coding agent) | GitHub Issues + cloud infra | Assign `copilot-swe-agent[bot]` via UI or `PATCH /repos/{owner}/{repo}/issues/{n}` |
| Copilot Automations | Repo Settings → Copilot → Automations | `issue.created`, `pr.created/updated`, schedule |
| GitHub Models API | `POST https://models.github.ai/inference/chat/completions` | OpenAI-compatible, auth via `models:read` PAT or `GITHUB_TOKEN` in Actions |
| MCP for cloud agent | Repo Settings → Copilot → MCP servers | JSON config, `http`/`sse`/`stdio`, secrets via `COPILOT_MCP_*` |
| Copilot CLI in Actions | `npm i -g @github/copilot` | `copilot -p "..." --allow-all-tools`, auth via `COPILOT_GITHUB_TOKEN` |

### What AIFactory already has

- `CopilotAgenticProvider` — wraps `copilot` CLI for coding phases; selected via `copilot:claude-sonnet-4.5` / `copilot:gpt-5`
- `openai-compatible` provider — accepts `base_url`, `api_key`, `model`; handles any OpenAI-format endpoint
- `runners/github/` suite — PR review engine, issue triage, batch processor, confidence scoring, multi-repo orchestrator
- `gh` CLI integration throughout for GitHub API calls
- Phase-aware provider factory in `providers/factory.py`

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                     GITHUB AGENTIC SURFACE                       │
│                                                                   │
│  GitHub Models API          Copilot Cloud Agent                  │
│  models.github.ai           copilot-swe-agent[bot]               │
│  (OpenAI-compatible)        (autonomous coder)                   │
│       │ POST /chat/completions    │ PATCH issue assignee         │
└───────┼───────────────────────────┼─────────────────────────────┘
        │                           │ webhook / Actions
        ▼                           ▼
┌──────────────────────────────────────────────────────────────────┐
│                         AIFACTORY                                 │
│                                                                   │
│  providers/factory.py          services/copilot_dispatch.py      │
│  github-models/* ─────────▶    dispatch(issue, label)            │
│  (routes to openai-compat)     watch_for_pr()                    │
│  GITHUB_TOKEN auth             task status: copilot_running      │
│                                          copilot_pr_opened       │
│  routes/mcp.py (NEW)           runners/github/services/          │
│  POST /mcp                     pr_review_engine.py (existing)    │
│  ◀── Copilot calls ──          triggered on PR open              │
│      aifactory_get_spec                                          │
│      aifactory_get_plan                                          │
│      aifactory_record_discovery                                  │
└──────────────────────────────────────────────────────────────────┘
        ▲
        │ POST /api/tasks/from-github-issue
        │ POST /api/github/{pr}/review
┌───────┴──────────────────────────────────────────────────────────┐
│                    GITHUB ACTIONS                                  │
│                                                                   │
│  aifactory-task.yml           copilot-pr-review.yml              │
│  on: issues.labeled           on: pull_request.opened            │
│  [aifactory:run]              if: actor == copilot-swe-agent     │
│                                                                   │
│  pr-review.yml                                                    │
│  on: pull_request.labeled                                        │
│  [aifactory:review]                                              │
└──────────────────────────────────────────────────────────────────┘
```

---

## Component 1: GitHub Models Provider

### Goal
Add `github-models` as a first-class provider alias that routes through the existing `openai-compatible` backend with GitHub-specific defaults pre-configured. Zero new provider class needed.

### Changes

**`apps/backend/providers/factory.py`**

Add canonical entry and aliases:
```python
# In _AGENTIC_REGISTRY and _TEXT_REGISTRY — routes to openai-compatible
"github-models": ("providers.openai_compatible_agentic", "OpenAICompatibleAgenticProvider"),

# In _PROVIDER_ALIASES
"github-models": "github-models",
"gh-models":     "github-models",
# Note: do NOT add "github" as an alias — it would shadow the existing gh/GitHub
# API integration used throughout runners/github/ and the gh CLI wrapper paths.
```

In `get_provider()`, when `canonical == "github-models"`, inject defaults before instantiation:
```python
if canonical == "github-models":
    kwargs.setdefault("base_url", "https://models.github.ai/inference")
    kwargs.setdefault("api_key",
        os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN", ""))
    # Strip "github-models/" prefix from model string
    raw_model = kwargs.get("model", "openai/gpt-4.1")
    if raw_model.startswith("github-models/"):
        kwargs["model"] = raw_model[len("github-models/"):]
    canonical = "openai-compatible"
```

**`apps/backend/phase_config.py`**

Extend `infer_provider_from_model()`:
```python
if model.startswith("github-models/"):
    return "github-models"
```

Add model shorthand entries:
```python
MODEL_ID_MAP.update({
    "gpt-4.1":         "github-models/openai/gpt-4.1",
    "o4-mini":         "github-models/openai/o4-mini",
    "llama-3.3-70b":   "github-models/meta/llama-3.3-70b",
    "deepseek-r1":     "github-models/deepseek/deepseek-r1",
})
```

**`apps/web-server/server/routes/github.py`**

New endpoint `GET /api/github/models` — fetches the GitHub Models catalog:
```python
@router.get("/models")
async def list_github_models():
    result = subprocess.run(
        ["gh", "api", "https://models.github.ai/catalog/models"],
        capture_output=True, text=True
    )
    return JSONResponse(json.loads(result.stdout))
```

**Frontend `TaskCreationWizard`**

Provider dropdown includes a "GitHub Models" group populated from `GET /api/github/models`. Model strings formatted as `github-models/{publisher}/{model}`.

**GitHub Actions**

In all three new workflows, add:
```yaml
permissions:
  models: read
```
This grants `GITHUB_TOKEN` the `models:read` scope so Actions can call the Models API at zero cost with no extra secrets.

---

## Component 2: Copilot Cloud Agent Dispatch

### Goal
When a task is created with the `copilot:delegate` label, AIFactory assigns the linked GitHub issue to `copilot-swe-agent[bot]` instead of running its own coder. It then monitors for the resulting PR and triggers its existing PR review engine.

### Configuration

```bash
# apps/web-server/.env
AIFACTORY_COPILOT_DISPATCH_ENABLED=false   # opt-in
# No new PAT needed — reuses gh CLI token (must have repo + issues + pull_requests scopes)
```

### New file: `apps/web-server/server/services/copilot_dispatch_service.py`

```python
class CopilotDispatchService:
    AGENT_HANDLE = "copilot-swe-agent[bot]"
    DISPATCH_LABEL = "copilot:delegate"

    async def dispatch(self, repo_full_name: str, issue_number: int) -> dict:
        """Assign issue to Copilot cloud agent via gh CLI."""
        result = subprocess.run([
            "gh", "api", "--method", "PATCH",
            f"/repos/{repo_full_name}/issues/{issue_number}",
            "-f", f"assignees[]={self.AGENT_HANDLE}"
        ], capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"Dispatch failed: {result.stderr}")
        return {"dispatched_at": datetime.utcnow().isoformat(), "handle": self.AGENT_HANDLE}

    async def find_copilot_pr(self, repo_full_name: str, issue_number: int) -> int | None:
        """Poll for a PR opened by the Copilot agent that references the issue.

        NOTE: GitHub API returns user.login as "copilot-swe-agent" (no [bot] suffix)
        with user.type == "Bot". The "[bot]" suffix only appears in the display name.
        Implementer MUST verify the exact login string against a real Copilot-opened PR
        (gh api /repos/{owner}/{repo}/pulls | jq '.[0].user') before finalising this filter.
        """
        result = subprocess.run([
            "gh", "api", f"/repos/{repo_full_name}/pulls",
            "--jq",
            f'[.[] | select(.user.type == "Bot" and (.user.login | startswith("copilot-swe-agent"))'
            f' and (.body // "" | contains("#{issue_number}")))] | first | .number'
        ], capture_output=True, text=True)
        number = result.stdout.strip()
        return int(number) if number and number != "null" else None
```

### Task status extensions

```python
# apps/web-server/server/models.py
class TaskStatus(str, Enum):
    ...
    COPILOT_RUNNING = "copilot_running"
    COPILOT_PR_OPENED = "copilot_pr_opened"
```

### `task_metadata.json` extension

```json
{
  "copilot_dispatch": {
    "enabled": true,
    "dispatched_at": "2026-06-07T12:00:00Z",
    "issue_number": 42,
    "pr_number": null,
    "pr_url": null,
    "agent_handle": "copilot-swe-agent[bot]",
    "reviewed": false
  }
}
```

### Integration in `agent_service.py`

In `start_task()`, after issue creation:
```python
if dispatch_enabled and "copilot:delegate" in task_labels:
    dispatch_result = await copilot_dispatch_service.dispatch(repo, issue_number)
    await update_task_metadata(task_id, {"copilot_dispatch": dispatch_result})
    await set_task_status(task_id, TaskStatus.COPILOT_RUNNING)
    asyncio.create_task(_watch_copilot_pr(task_id, repo, issue_number))
    return  # Don't start normal coder pipeline
```

Background watcher `_watch_copilot_pr` polls every 60s for up to **59 minutes** — matching GitHub's hard limit on Copilot cloud agent sessions (the agent always terminates within 59 minutes, so there is no point waiting beyond that). If no PR appears by the deadline, the task is marked `failed`.

### Fallback

If `AIFACTORY_COPILOT_DISPATCH_ENABLED` is false, or if `gh api` fails (missing token scope), the dispatch falls back to the normal AIFactory coder pipeline. The task log records a warning line: `[copilot-dispatch] disabled or dispatch failed — falling back to AIFactory coder pipeline`. This warning is surfaced in the task's log panel in the UI so the user is not silently surprised by the label being ignored.

### UI

Task detail panel shows:
- Yellow badge: "Delegated to GitHub Copilot agent" when status is `copilot_running`
- Link to the GitHub issue
- Blue badge: "Copilot PR opened" with PR link when status is `copilot_pr_opened`
- Review results appear in the existing review panel once the PR review completes

---

## Component 3: AIFactory MCP Server

### Goal
Expose AIFactory's spec/plan/context/memory tools as an HTTP MCP server so the Copilot cloud agent can call home during its coding session. The Copilot agent uses these tools to get implementation context and record discoveries without needing to reinvent AIFactory's context graph.

### New file: `apps/web-server/server/routes/mcp.py`

Implements a **minimal POST-only MCP HTTP transport** (JSON-RPC 2.0 over `POST /mcp`). The Copilot cloud agent's MCP client supports POST-only mode without requiring the `GET /mcp` server-push channel defined in MCP spec 2025-11-05. Full Streamable HTTP compliance (server-initiated messages via the GET channel, `Mcp-Session-Id` lifecycle management) is out of scope for this implementation. If the Copilot client requires full compliance in future, the `mcp` Python SDK can replace the hand-rolled router with minimal interface change.

**Methods handled:**

- `initialize` — return server capabilities
- `tools/list` — return the tool catalog
- `tools/call` — dispatch to the appropriate tool handler

**Tool implementations:**

```python
MCP_TOOLS = [
    {
        "name": "aifactory_get_spec",
        "description": "Get the spec.md for a task (implementation requirements)",
        "inputSchema": {"type": "object", "properties": {
            "issue_number": {"type": "integer"},
            "task_id": {"type": "string"}
        }}
    },
    {
        "name": "aifactory_get_plan",
        "description": "Get the implementation_plan.json subtask breakdown",
        "inputSchema": {"type": "object", "properties": {
            "issue_number": {"type": "integer"},
            "task_id": {"type": "string"}
        }}
    },
    {
        "name": "aifactory_get_context",
        "description": "Get discovered codebase context (files, patterns, dependencies)",
        "inputSchema": {"type": "object", "properties": {
            "issue_number": {"type": "integer"}
        }}
    },
    {
        "name": "aifactory_record_discovery",
        "description": "Record a code discovery into AIFactory memory for future sessions",
        "inputSchema": {"type": "object", "required": ["content"], "properties": {
            "issue_number": {"type": "integer"},
            "content": {"type": "string"},
            "category": {"type": "string", "enum": ["pattern", "dependency", "architecture", "other"]}
        }}
    },
    {
        "name": "aifactory_record_gotcha",
        "description": "Record a gotcha or issue found during implementation",
        "inputSchema": {"type": "object", "required": ["content"], "properties": {
            "issue_number": {"type": "integer"},
            "content": {"type": "string"}
        }}
    },
    {
        "name": "aifactory_get_build_progress",
        "description": "Get current task status and subtask completion progress",
        "inputSchema": {"type": "object", "properties": {
            "issue_number": {"type": "integer"},
            "task_id": {"type": "string"}
        }}
    }
]
```

**Auth middleware:**
```python
async def verify_mcp_token(request: Request):
    token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    expected = os.environ.get("AIFACTORY_MCP_SECRET", "")
    if expected and not secrets.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="Invalid MCP token")
```

**Task lookup by issue number:**

Tasks are identified by scanning the filesystem sidecar files, not a SQL query (AIFactory stores task metadata as `task_metadata.json` files on disk, not in a database column):

```python
async def resolve_task_by_issue(
    issue_number: int,
    projects_data_dir: Path,
) -> dict | None:
    """Scan all task specs for one whose copilot_dispatch.issue_number matches."""
    for spec_dir in sorted(projects_data_dir.glob("**/specs/*")):
        meta_file = spec_dir / "task_metadata.json"
        if not meta_file.exists():
            continue
        try:
            meta = json.loads(meta_file.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        dispatch = meta.get("copilot_dispatch", {})
        if dispatch.get("issue_number") == issue_number:
            return {"task_id": spec_dir.name, "spec_dir": str(spec_dir), "metadata": meta}
    return None
```

`projects_data_dir` = `APP_PROJECTS_DATA_DIR` env var (default `~/.aifactory`). This scan is linear over open tasks; acceptable given the low frequency of MCP tool calls.

### Configuration

```bash
# apps/web-server/.env
AIFACTORY_MCP_SECRET=<random-32-char-hex>   # bearer token for MCP endpoint
AIFACTORY_MCP_URL=https://your-domain:3101  # public URL (for Copilot to reach)
```

### Repository MCP config (GitHub Settings → Copilot → MCP servers)

```json
{
  "mcpServers": {
    "aifactory": {
      "type": "http",
      "url": "${COPILOT_MCP_AIFACTORY_URL}/mcp",
      "headers": {
        "Authorization": "Bearer ${COPILOT_MCP_AIFACTORY_TOKEN}"
      },
      "tools": [
        "aifactory_get_spec",
        "aifactory_get_plan",
        "aifactory_get_context",
        "aifactory_record_discovery",
        "aifactory_record_gotcha",
        "aifactory_get_build_progress"
      ]
    }
  }
}
```

Secrets needed in GitHub (Settings → Copilot → Agents secrets):
- `COPILOT_MCP_AIFACTORY_URL` = your AIFactory public URL
- `COPILOT_MCP_AIFACTORY_TOKEN` = value of `AIFACTORY_MCP_SECRET`

### Dev tunnel setup

For local development:
```bash
cloudflared tunnel --url http://localhost:3101
# → https://abc123.trycloudflare.com
# Set COPILOT_MCP_AIFACTORY_URL = https://abc123.trycloudflare.com
```

---

## Component 4: GitHub Actions Workflows

### `aifactory-task.yml` — Issue labeled → create AIFactory task

```yaml
name: AIFactory Task Creation
on:
  issues:
    types: [labeled]

jobs:
  create-task:
    if: github.event.label.name == 'aifactory:run'
    runs-on: ubuntu-latest
    permissions:
      issues: write
      models: read
    steps:
      - name: Create AIFactory task
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          AIFACTORY_URL: ${{ secrets.AIFACTORY_URL }}
          AIFACTORY_TOKEN: ${{ secrets.AIFACTORY_TOKEN }}
        run: |
          ISSUE_NUMBER="${{ github.event.issue.number }}"
          ISSUE_TITLE="${{ github.event.issue.title }}"
          if [ -n "$AIFACTORY_URL" ]; then
            curl -sf -X POST "$AIFACTORY_URL/api/tasks/from-github-issue" \
              -H "Authorization: Bearer $AIFACTORY_TOKEN" \
              -H "Content-Type: application/json" \
              -d "{\"issue_number\": $ISSUE_NUMBER, \"repo\": \"${{ github.repository }}\"}"
          else
            gh issue comment "$ISSUE_NUMBER" \
              --body "🏭 **AIFactory**: Issue queued for import. Set \`AIFACTORY_URL\` secret to automate."
          fi
```

### `copilot-pr-review.yml` — Copilot agent PR → AIFactory review

```yaml
name: Review Copilot Agent PRs
on:
  pull_request:
    types: [opened, synchronize]

jobs:
  aifactory-review:
    if: github.actor == 'copilot-swe-agent[bot]'
    runs-on: ubuntu-latest
    permissions:
      contents: read
      pull-requests: write
      models: read
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - name: Notify AIFactory of PR
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          AIFACTORY_URL: ${{ secrets.AIFACTORY_URL }}
          AIFACTORY_TOKEN: ${{ secrets.AIFACTORY_TOKEN }}
        run: |
          PR_NUMBER="${{ github.event.pull_request.number }}"
          if [ -n "$AIFACTORY_URL" ]; then
            # Notify AIFactory to run its review engine
            curl -sf -X POST "$AIFACTORY_URL/api/github/prs/$PR_NUMBER/review" \
              -H "Authorization: Bearer $AIFACTORY_TOKEN" \
              -H "Content-Type: application/json" \
              -d "{\"repo\": \"${{ github.repository }}\", \"pr_number\": $PR_NUMBER}"
          else
            # Fallback: inline Copilot CLI review
            npm install -g @github/copilot 2>/dev/null
            copilot -p "Review this PR for correctness, security, and code quality.
              Post a summary review comment." \
              --allow-all-tools --no-color \
              --add-dir "${{ github.workspace }}"
          fi
```

### `pr-review.yml` — Label-triggered general PR review

```yaml
name: AIFactory PR Review
on:
  pull_request:
    types: [labeled]

jobs:
  review:
    if: github.event.label.name == 'aifactory:review'
    runs-on: ubuntu-latest
    permissions:
      contents: read
      pull-requests: write
      models: read
    steps:
      - uses: actions/checkout@v4
        with: { fetch-depth: 0 }
      - name: Run AIFactory review
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          AIFACTORY_URL: ${{ secrets.AIFACTORY_URL }}
          AIFACTORY_TOKEN: ${{ secrets.AIFACTORY_TOKEN }}
        run: |
          PR_NUMBER="${{ github.event.pull_request.number }}"
          if [ -n "$AIFACTORY_URL" ]; then
            curl -sf -X POST "$AIFACTORY_URL/api/github/prs/$PR_NUMBER/review" \
              -H "Authorization: Bearer $AIFACTORY_TOKEN" \
              -H "Content-Type: application/json" \
              -d "{\"repo\": \"${{ github.repository }}\", \"pr_number\": $PR_NUMBER}"
          fi
```

### Copilot Automations (Settings → Copilot → Automations)

Configure these directly in GitHub — no YAML needed:

**Automation 1: Issue triage**
- Name: "Auto-triage new issues"
- Trigger: New issue created
- Prompt: "Triage this issue. Add label `bug`, `enhancement`, or `question` based on content. If it's a well-defined, implementable task that an AI coding agent could handle, also add label `copilot:delegate`."
- Tools: Update issue labels

**Automation 2: Release notes**
- Name: "Weekly release notes draft"
- Trigger: Weekly (Monday 09:00)
- Prompt: "Create a draft PR with updated CHANGELOG.md summarizing merged PRs since the last release tag."
- Tools: Create pull request

---

## Data Model Changes

### `apps/web-server/server/models.py`

```python
class TaskStatus(str, Enum):
    # ... existing values ...
    COPILOT_RUNNING = "copilot_running"
    COPILOT_PR_OPENED = "copilot_pr_opened"
```

### `task_metadata.json` (schema extension)

```json
{
  "copilot_dispatch": {
    "enabled": false,
    "dispatched_at": null,
    "issue_number": null,
    "pr_number": null,
    "pr_url": null,
    "agent_handle": "copilot-swe-agent[bot]",
    "reviewed": false,
    "review_pr_url": null
  }
}
```

---

## Configuration Reference

```bash
# apps/web-server/.env — new keys

# Component 2: Copilot cloud agent dispatch
AIFACTORY_COPILOT_DISPATCH_ENABLED=false    # Default off; set true to enable dispatch
# No new PAT — reuses gh CLI token (needs: repo, issues, pull_requests, actions scopes)

# Component 3: MCP server
AIFACTORY_MCP_SECRET=                       # Random 32-char hex; bearer token for /mcp endpoint
AIFACTORY_MCP_URL=                          # Public URL of AIFactory (for docs/setup guides)

# GitHub Actions secrets (set in repo Settings → Secrets → Actions)
# AIFACTORY_URL = https://your-aifactory-instance.com
# AIFACTORY_TOKEN = web-server API token

# GitHub Copilot Agents secrets (Settings → Copilot → Agents secrets)
# COPILOT_MCP_AIFACTORY_URL = https://your-aifactory-instance.com
# COPILOT_MCP_AIFACTORY_TOKEN = value of AIFACTORY_MCP_SECRET
```

---

## Error Handling

| Scenario | Handling |
|---|---|
| Dispatch fails (gh token missing Copilot scope) | Graceful fallback to normal AIFactory coder pipeline; warning logged |
| Copilot agent times out (>59 min) | Watcher detects stale status, marks task as `failed`, records in `self_heal_events` |
| MCP endpoint unreachable from Copilot cloud | Copilot agent continues without AIFactory context (not a hard failure) |
| GITHUB_TOKEN lacks `models:read` | GitHub Models API returns 403; AIFactory surfaces as provider config error |
| PR watcher finds no PR after 60 minutes | Task marked `failed` with note "Copilot cloud agent did not produce a PR within timeout" |
| MCP auth fails (wrong token) | 401 returned; Copilot logs tool failure; agent continues without tool |

---

## Testing Strategy

### Component 1 (GitHub Models)
- Unit: mock `openai-compatible` provider, verify `github-models/openai/gpt-4.1` is resolved to correct `base_url` + stripped model string
- Integration: call `GET /api/github/models` in CI with `GITHUB_TOKEN` — verify response contains model list
- E2E: create a spec task with `provider=github-models`, model=`github-models/openai/gpt-4.1`, verify QA review phase completes

### Component 2 (Dispatch)
- Unit: mock `gh api` calls, verify dispatch sets correct assignee, status transitions fire, fallback works
- Integration: in aifactory-demo project, create issue with `copilot:delegate` label, verify `copilot-swe-agent[bot]` appears as assignee
- Watcher: mock PR response, verify status transitions to `copilot_pr_opened` and review is triggered

### Component 3 (MCP server)
- Unit: test each tool handler returns correct data shape; test auth middleware rejects wrong tokens
- Integration: `curl -X POST localhost:3101/mcp` with valid token, `tools/list` returns all 6 tools
- E2E: configure MCP in a test repo, verify Copilot cloud agent calls appear in AIFactory logs

### Component 4 (Actions)
- Dry-run: use `act` (local GitHub Actions runner) to verify workflows parse and step names are correct
- Integration: create issue with `aifactory:run` label in aifactory-demo, verify workflow fires, comment is posted
- Review flow: open PR with `aifactory:review` label, verify review workflow fires

---

## Implementation Sequence

Priority order (each is independently deployable):

1. **GitHub Models provider** — highest impact, lowest effort. Factory alias + phase_config update + `GET /api/github/models` endpoint + UI dropdown. No new infra.
2. **MCP server router** — new FastAPI router, ~200 lines. Unblocks the Copilot cloud agent bidirectional path.
3. **GitHub Actions workflows** — three YAML files. Immediately useful even without 1-2.
4. **Copilot cloud agent dispatch** — new service + task status extensions + watcher background task. Depends on having issues set up (runners/github already handles this).
5. **Copilot automations** — config-only, no code. Set up in repo settings after other components are live.
6. **UI changes** — provider dropdown update + copilot dispatch toggle + badge in task detail.

---

## Out of Scope

- OAuth-based remote MCP servers (not supported by GitHub Copilot cloud agent currently)
- Replacing AIFactory's Claude SDK coder pipeline with Copilot cloud agent for complex tasks (level 2+ BMad complexity stays on Claude)
- GitHub Copilot code review as a replacement for AIFactory's PR review engine (complementary, not a replacement)
- GitHub Spark (unrelated micro-app builder)
