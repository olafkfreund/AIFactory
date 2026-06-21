"""
Prompt Loading Utilities
========================

Functions for loading agent prompts from markdown files.
Supports dynamic prompt assembly based on project type for context optimization.
Supports Quick Mode for simplified prompts (~70% fewer tokens).
"""

import json
import os
import re
from pathlib import Path

from .project_context import (
    detect_project_capabilities,
    get_mcp_tools_for_project,
    load_project_index,
)

# Directory containing prompt files
# prompts/ is a sibling directory of prompts_pkg/, so go up one level first
PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


def get_planner_prompt(spec_dir: Path) -> str:
    """
    Load the planner agent prompt with spec path injected.
    The planner creates subtask-based implementation plans.

    Args:
        spec_dir: Directory containing the spec.md file

    Returns:
        The planner prompt content with spec path
    """
    # Quick Mode: Use simplified prompt (~70% fewer tokens)
    if os.environ.get("QUICK_MODE") == "true":
        quick_prompt_file = PROMPTS_DIR / "planner_quick.md"
        if quick_prompt_file.exists():
            prompt_file = quick_prompt_file
        else:
            prompt_file = PROMPTS_DIR / "planner.md"
    else:
        prompt_file = PROMPTS_DIR / "planner.md"

    if not prompt_file.exists():
        raise FileNotFoundError(
            f"Planner prompt not found at {prompt_file}\n"
            "Make sure the aifactory/prompts/planner.md file exists."
        )

    prompt = prompt_file.read_text()

    # Inject spec directory information at the beginning
    spec_context = f"""## SPEC LOCATION

Your spec file is located at: `{spec_dir}/spec.md`

🚨 CRITICAL FILE CREATION INSTRUCTIONS 🚨

You MUST use the Write tool to create these files in the spec directory:
- `{spec_dir}/implementation_plan.json` - Subtask-based implementation plan (USE WRITE TOOL!)
- `{spec_dir}/build-progress.txt` - Progress notes (USE WRITE TOOL!)
- `{spec_dir}/init.sh` - Environment setup script (USE WRITE TOOL!)

DO NOT just describe what these files should contain. You MUST actually call the Write tool
with the file path and complete content to create them.

The project root is the parent of aifactory/. Implement code in the project root, not in the spec directory.

---

"""
    return spec_context + prompt


def get_solo_prompt(spec_dir: Path) -> str:
    """
    Load the solo-mode agent prompt with spec path injected (issue #276).

    The solo agent self-directs the whole job in a single streamlined flow:
    it writes its own ``implementation_plan.json``, implements the subtasks,
    and verifies its own work — there is no separate planner or QA agent.

    Args:
        spec_dir: Directory containing the spec.md file

    Returns:
        The solo prompt content with spec path injected
    """
    prompt_file = PROMPTS_DIR / "solo.md"
    if not prompt_file.exists():
        raise FileNotFoundError(
            f"Solo prompt not found at {prompt_file}\n"
            "Make sure the aifactory/prompts/solo.md file exists."
        )

    prompt = prompt_file.read_text()

    # Inject spec directory information at the beginning.
    spec_context = f"""## SPEC LOCATION

Your spec file is located at: `{spec_dir}/spec.md`

🚨 CRITICAL FILE CREATION INSTRUCTIONS 🚨

You MUST use the Write tool to create these files in the spec directory:
- `{spec_dir}/implementation_plan.json` - Your self-authored subtask plan (USE WRITE TOOL!)
- `{spec_dir}/build-progress.txt` - Progress notes (USE WRITE TOOL!)

You MUST keep subtask statuses current via the `update_subtask_status` tool as
you work. The orchestrator reads `implementation_plan.json` to know when the
build is complete.

The project root is the parent of aifactory/. Implement code in the project
root, not in the spec directory.

---

"""
    # RFC-0012 / RFC-0013: the solo agent is coder + QA in one flow, so surface
    # both the team's house standards and the deployment context to it.
    spec_context += get_house_standards_context(spec_dir)
    # RFC-0015: honour the per-project constitution (enforceable clauses = hard).
    spec_context += get_constitution_context(spec_dir)
    spec_context += get_deployment_context(spec_dir)
    return spec_context + prompt


def get_coding_prompt(spec_dir: Path) -> str:
    """
    Load the coding agent prompt with spec path injected.

    Args:
        spec_dir: Directory containing the spec.md and implementation_plan.json

    Returns:
        The coding agent prompt content with spec path
    """
    # Quick Mode: Use simplified prompt (~70% fewer tokens)
    if os.environ.get("QUICK_MODE") == "true":
        quick_prompt_file = PROMPTS_DIR / "coder_quick.md"
        if quick_prompt_file.exists():
            prompt_file = quick_prompt_file
        else:
            prompt_file = PROMPTS_DIR / "coder.md"
    else:
        prompt_file = PROMPTS_DIR / "coder.md"

    if not prompt_file.exists():
        raise FileNotFoundError(
            f"Coding prompt not found at {prompt_file}\n"
            "Make sure the aifactory/prompts/coder.md file exists."
        )

    prompt = prompt_file.read_text()

    spec_context = f"""## SPEC LOCATION

Your spec and progress files are located at:
- Spec: `{spec_dir}/spec.md`
- Implementation plan: `{spec_dir}/implementation_plan.json`
- Progress notes: `{spec_dir}/build-progress.txt`
- Recovery context: `{spec_dir}/memory/attempt_history.json`

The project root is the parent of aifactory/. All code goes in the project root, not in the spec directory.

---

"""

    # RFC-0012: surface the team's house standards so the coder follows them.
    spec_context += get_house_standards_context(spec_dir)

    # RFC-0015: honour the per-project constitution (enforceable clauses = hard).
    spec_context += get_constitution_context(spec_dir)

    # RFC-0013: surface the deployment context (scans, gates, dry-run policy).
    spec_context += get_deployment_context(spec_dir)

    # Check for recovery context (stuck subtasks, retry hints)
    recovery_context = _get_recovery_context(spec_dir)
    if recovery_context:
        spec_context += recovery_context

    # Check for human input file
    human_input_file = spec_dir / "HUMAN_INPUT.md"
    if human_input_file.exists():
        human_input = human_input_file.read_text().strip()
        if human_input:
            spec_context += f"""## HUMAN INPUT (READ THIS FIRST!)

The human has left you instructions. READ AND FOLLOW THESE CAREFULLY:

{human_input}

After addressing this input, you may delete or clear the HUMAN_INPUT.md file.

---

"""

    return spec_context + prompt


def _get_recovery_context(spec_dir: Path) -> str:
    """
    Get recovery context if there are failed attempts or stuck subtasks.

    Args:
        spec_dir: Spec directory containing memory/

    Returns:
        Recovery context string or empty string
    """
    import json

    attempt_history_file = spec_dir / "memory" / "attempt_history.json"

    if not attempt_history_file.exists():
        return ""

    try:
        with open(attempt_history_file) as f:
            history = json.load(f)

        # Check for stuck subtasks
        stuck_subtasks = history.get("stuck_subtasks", [])
        if stuck_subtasks:
            context = """## ⚠️ RECOVERY ALERT - STUCK SUBTASKS DETECTED

Some subtasks have been attempted multiple times without success. These subtasks need:
- A COMPLETELY DIFFERENT approach
- Possibly simpler implementation
- Or escalation to human if infeasible

Stuck subtasks:
"""
            for stuck in stuck_subtasks:
                context += f"- {stuck['subtask_id']}: {stuck['reason']} ({stuck['attempt_count']} attempts)\n"

            context += "\nBefore working on any subtask, check memory/attempt_history.json for previous attempts!\n\n---\n\n"
            return context

        # Check for subtasks with multiple attempts
        subtasks_with_retries = []
        for subtask_id, subtask_data in history.get("subtasks", {}).items():
            attempts = subtask_data.get("attempts", [])
            if len(attempts) > 1 and subtask_data.get("status") != "completed":
                subtasks_with_retries.append((subtask_id, len(attempts)))

        if subtasks_with_retries:
            context = """## ⚠️ RECOVERY CONTEXT - RETRY AWARENESS

Some subtasks have been attempted before. When working on these:
1. READ memory/attempt_history.json for the specific subtask
2. See what approaches were tried
3. Use a DIFFERENT approach

Subtasks with previous attempts:
"""
            for subtask_id, attempt_count in subtasks_with_retries:
                context += f"- {subtask_id}: {attempt_count} attempts\n"

            context += "\n---\n\n"
            return context

        return ""

    except (OSError, json.JSONDecodeError):
        return ""


def get_house_standards_context(spec_dir: Path) -> str:
    """Render the RFC-0012 house standards as a prompt block.

    Reads the signed Task Contract (``context/task_contract.json``, falling back
    to ``implementation_plan.json``) and surfaces
    ``epic_context.house_standards`` so the coder / QA agents FOLLOW the team's
    own conventions (linters, build managers, golden-path guides) instead of
    generic defaults. Degrades silently to "" when no standards were retrieved —
    RFC-0012 retrieval is best-effort, but the standards_conformance gate fails
    the build if a *retrieved* standard is then ignored.
    """
    contract = None
    for rel in ("context/task_contract.json", "implementation_plan.json"):
        path = spec_dir / rel
        if path.exists():
            try:
                contract = json.loads(path.read_text())
                break
            except (OSError, json.JSONDecodeError):
                continue
    if not isinstance(contract, dict):
        return ""

    epic_context = contract.get("epic_context")
    house = (
        epic_context.get("house_standards") if isinstance(epic_context, dict) else None
    )
    if not isinstance(house, dict) or not house.get("available"):
        return ""
    sources = [s for s in house.get("sources", []) if isinstance(s, dict)]
    if not sources:
        return ""

    tools: list[str] = []
    version_managers: list[str] = []
    techdocs: list[str] = []
    lifecycle = None
    for src in sources:
        conv = src.get("conventions")
        if isinstance(conv, dict):
            tools += [str(t) for t in conv.get("code_quality_tools", []) or []]
            version_managers += [str(t) for t in conv.get("version_managers", []) or []]
        techdocs += [str(r) for r in src.get("techdocs_refs", []) or []]
        lifecycle = lifecycle or src.get("lifecycle")

    lines = [
        "## HOUSE STANDARDS (RFC-0012 — FOLLOW THESE)",
        "",
        "This team has its own standards. Follow them over generic defaults. The "
        "`standards_conformance` gate fails the build when a retrieved standard is "
        "ignored (e.g. a declared lint/type tool that is never run).",
        "",
    ]
    if tools:
        lines.append(
            f"- **Code-quality tools (use and run these):** {', '.join(dict.fromkeys(tools))}"
        )
    if version_managers:
        lines.append(
            f"- **Version / build managers:** {', '.join(dict.fromkeys(version_managers))}"
        )
    if lifecycle:
        lines.append(f"- **Component lifecycle:** {lifecycle}")
    if techdocs:
        lines.append("- **Consult these team guides (TechDocs) before coding:**")
        for ref in dict.fromkeys(techdocs):
            lines.append(f"  - `{ref}`")
    lines += ["", "---", "", ""]
    return "\n".join(lines)


def get_constitution_context(spec_dir: Path) -> str:
    """Render the RFC-0015 §3.1 constitution as a prompt block.

    Reads the signed Task Contract (``context/task_contract.json``, falling back
    to ``implementation_plan.json``) and surfaces
    ``epic_context.constitution`` so the coder / QA agents HONOUR the project's
    human-authored governing principles. Mirrors the RFC-0012 house-standards
    injection pattern (``get_house_standards_context``).

    Principles tagged ``enforceable: true`` are rendered as HARD requirements
    (must-follow, not advisory); the ``standards_conformance`` gate enforces the
    same clauses, closing spec-kit's soft-enforcement gap. Advisory principles
    are still injected so the agent has the full context.

    Degrades silently to "" when no constitution was retrieved (``available``
    false, no principles, or the block is absent) — RFC-0015 retrieval degrades,
    never fakes, and the absent case leaves today's behaviour unchanged.
    """
    contract = None
    for rel in ("context/task_contract.json", "implementation_plan.json"):
        path = spec_dir / rel
        if path.exists():
            try:
                contract = json.loads(path.read_text())
                break
            except (OSError, json.JSONDecodeError):
                continue
    if not isinstance(contract, dict):
        return ""

    epic_context = contract.get("epic_context")
    constitution = (
        epic_context.get("constitution") if isinstance(epic_context, dict) else None
    )
    if not isinstance(constitution, dict) or not constitution.get("available"):
        return ""

    principles = [
        p
        for p in constitution.get("principles", [])
        if isinstance(p, dict) and str(p.get("text", "")).strip()
    ]
    if not principles:
        return ""

    # ids tagged enforceable; honour the per-principle flag and the top-level
    # enforceable_ids list (a principle is hard if either marks it so).
    enforceable_ids = {
        str(i) for i in constitution.get("enforceable_ids", []) if str(i).strip()
    }

    def _is_hard(principle: dict[str, object]) -> bool:
        return bool(principle.get("enforceable")) or (
            str(principle.get("id", "")) in enforceable_ids
        )

    hard = [p for p in principles if _is_hard(p)]
    advisory = [p for p in principles if not _is_hard(p)]

    source = str(constitution.get("source", "")).strip()
    lines = [
        "## PROJECT CONSTITUTION (RFC-0015 — GOVERNING PRINCIPLES)",
        "",
        "This project has a human-authored constitution. Honour every principle. "
        "Clauses marked HARD REQUIREMENT are non-negotiable must-follows (not "
        "advisory): the `standards_conformance` gate fails the build when a hard "
        "clause is ignored.",
        "",
    ]
    if source:
        lines += [f"_Source: `{source}`_", ""]

    if hard:
        lines.append("### HARD REQUIREMENTS (must follow)")
        for p in hard:
            pid = str(p.get("id", "")).strip()
            prefix = f"[{pid}] " if pid else ""
            lines.append(
                f"- 🚨 **HARD REQUIREMENT** — {prefix}{str(p['text']).strip()}"
            )
        lines.append("")

    if advisory:
        lines.append("### Advisory principles (follow where applicable)")
        for p in advisory:
            pid = str(p.get("id", "")).strip()
            prefix = f"[{pid}] " if pid else ""
            lines.append(f"- {prefix}{str(p['text']).strip()}")
        lines.append("")

    lines += ["---", "", ""]
    return "\n".join(lines)


def _load_contract(spec_dir: Path) -> dict | None:
    """Read the signed Task Contract for ``spec_dir``.

    Tries ``context/task_contract.json`` first, then ``implementation_plan.json``
    (same resolution order as ``get_house_standards_context``). Returns the
    parsed dict, or None when absent / unreadable. Never raises.
    """
    for rel in ("context/task_contract.json", "implementation_plan.json"):
        path = spec_dir / rel
        if path.exists():
            try:
                data = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, dict):
                return data
    return None


def _deployment_names(deployment: dict, key: str) -> list[str]:
    """Return the de-duplicated, stripped string list at ``deployment[key]``."""
    raw = deployment.get(key)
    if not isinstance(raw, list):
        return []
    return list(dict.fromkeys(str(v).strip() for v in raw if str(v).strip()))


def _deployment_summary_lines(deployment: dict) -> list[str]:
    """Render the one-line-per-field summary of the deployment block."""
    fields = [
        ("Risk class", str(deployment.get("risk_class", "")).strip()),
        ("CI system", str(deployment.get("ci_system", "")).strip()),
        ("Deploy system", str(deployment.get("deploy_system", "")).strip()),
        (
            "Target environments",
            ", ".join(_deployment_names(deployment, "deploy_target_environments")),
        ),
        (
            "Highest blast-radius class",
            str(deployment.get("production_classification", "")).strip(),
        ),
    ]
    lines = [f"- **{label}:** {value}" for label, value in fields if value]

    required_scans = _deployment_names(deployment, "required_scans")
    if required_scans:
        lines.append(
            "- **Required scans (these MUST run before deploy — add them to the "
            f"pipeline):** {', '.join(required_scans)}"
        )
    system_gates = _deployment_names(deployment, "system_gates")
    if system_gates:
        lines.append(
            "- **System gates (must pass before deploy; do not bypass):** "
            f"{', '.join(system_gates)}"
        )
    return lines


def _deployment_pipeline_lines(deployment: dict) -> list[str]:
    """Render the needs_pipeline scaffolding instruction, or [] when not needed."""
    if not deployment.get("needs_pipeline"):
        return []
    ci_system = str(deployment.get("ci_system", "")).strip()
    lines = [
        "",
        "### Pipeline required (`needs_pipeline=true`)",
        "No usable CI/CD pipeline exists for this change. Scaffold one from "
        "the team's golden-path template via the Backstage scaffolder MCP "
        "(`scaffolder_execute-template`), then extend it. The pipeline MUST "
        "include build, the required scans above, and the deploy stage(s).",
    ]
    suggestion = _golden_path_template_for(ci_system)
    if suggestion:
        lines.append(
            f"- **Suggested golden-path template:** `{suggestion}` "
            f"(matched to ci_system=`{ci_system or 'unknown'}`)."
        )
    return lines


def _deployment_verification_lines(deployment: dict) -> list[str]:
    """Render the dry-run deploy-verification policy, or [] when absent."""
    dv = deployment.get("deploy_verification")
    if not isinstance(dv, dict) or not dv:
        return []
    bits = [
        b
        for b in (
            str(dv.get("target_level", "")).strip(),
            str(dv.get("mode", "")).strip(),
        )
        if b
    ]
    if not bits:
        return []
    return [
        "",
        "### Deploy verification policy (DRY-RUN by design)",
        f"- Target level / mode: {' / '.join(bits)}.",
        "- Prove the deploy WOULD work (lint / `helm template` / "
        "`terraform plan` / `--dry-run`). Do NOT run a production apply: "
        "that is VAL-4 and is held behind a human-approval gate.",
    ]


def get_deployment_context(spec_dir: Path) -> str:
    """Render the RFC-0013 ``deployment`` block as a prompt block (#643).

    Reads the signed Task Contract and surfaces ``deployment`` so the coder / QA
    agents PLAN FOR HOW THE CHANGE SHIPS, not just what it does: the required
    pre-deploy scans they must wire in, the system gates that must pass, and the
    DRY-RUN deploy-verification policy (production is VAL-4 and NEVER applied
    autonomously). Degrades silently to "" when there is no deployment block —
    a library change or docs PR keeps the prior behavior.
    """
    contract = _load_contract(spec_dir)
    if contract is None:
        return ""

    deployment = contract.get("deployment")
    if not isinstance(deployment, dict) or not deployment:
        return ""

    lines = [
        "## DEPLOYMENT CONTEXT (RFC-0013 — PLAN FOR HOW IT SHIPS)",
        "",
        "This change has a delivery dimension. Honor it: wire in the required "
        "scans and respect the system gates below. Deploy verification is "
        "DRY-RUN by policy and **production is never applied autonomously**.",
        "",
    ]
    lines += _deployment_summary_lines(deployment)
    lines += _deployment_pipeline_lines(deployment)
    lines += _deployment_verification_lines(deployment)

    if str(deployment.get("production_classification", "")).strip() == "production":
        lines += [
            "",
            "**Production path:** this change reaches production. It is VAL-4 and "
            "NEVER deployed autonomously — a human authorizes the real apply.",
        ]

    lines += ["", "---", "", ""]
    return "\n".join(lines)


def _golden_path_template_for(ci_system: str) -> str:
    """Map a discovered ``ci_system`` to a golden-path scaffolder template ref.

    Returns the Backstage template reference the coder should pass to
    ``scaffolder_execute-template``, or "" when the CI system is unknown (then
    the coder picks from the catalog). These names mirror the fleet golden-path
    templates; an operator override is out of scope for this safe core.
    """
    mapping = {
        "github-actions": "template:default/ci-cd-github-actions",
        "gitlab-ci": "template:default/ci-cd-gitlab-ci",
        "azure-pipelines": "template:default/ci-cd-azure-pipelines",
    }
    return mapping.get((ci_system or "").strip().lower(), "")


def get_followup_planner_prompt(spec_dir: Path) -> str:
    """
    Load the follow-up planner agent prompt with spec path and key files injected.
    The follow-up planner adds new subtasks to an existing completed implementation plan.

    Args:
        spec_dir: Directory containing the completed spec and implementation_plan.json

    Returns:
        The follow-up planner prompt content with paths injected
    """
    prompt_file = PROMPTS_DIR / "followup_planner.md"

    if not prompt_file.exists():
        raise FileNotFoundError(
            f"Follow-up planner prompt not found at {prompt_file}\n"
            "Make sure the aifactory/prompts/followup_planner.md file exists."
        )

    prompt = prompt_file.read_text()

    # Inject spec directory information at the beginning
    spec_context = f"""## SPEC LOCATION (FOLLOW-UP MODE)

You are adding follow-up work to a **completed** spec.

**Key files in this spec directory:**
- Spec: `{spec_dir}/spec.md`
- Follow-up request: `{spec_dir}/FOLLOWUP_REQUEST.md` (READ THIS FIRST!)
- Implementation plan: `{spec_dir}/implementation_plan.json` (APPEND to this, don't replace)
- Progress notes: `{spec_dir}/build-progress.txt`
- Context: `{spec_dir}/context.json`
- Memory: `{spec_dir}/memory/`

**Important paths:**
- Spec directory: `{spec_dir}`
- Project root: Parent of aifactory/ (where code should be implemented)

**Your task:**
1. Read `{spec_dir}/FOLLOWUP_REQUEST.md` to understand what to add
2. Read `{spec_dir}/implementation_plan.json` to see existing phases/subtasks
3. ADD new phase(s) with pending subtasks to the existing plan
4. PRESERVE all existing subtasks and their statuses

---

"""
    return spec_context + prompt


def is_first_run(spec_dir: Path) -> bool:
    """
    Check if this is the first run (no valid implementation plan with subtasks exists yet).

    The spec runner may create a skeleton implementation_plan.json with empty phases.
    This function checks for actual phases with subtasks, not just file existence.

    Args:
        spec_dir: Directory containing spec files

    Returns:
        True if implementation_plan.json doesn't exist or has no subtasks
    """
    plan_file = spec_dir / "implementation_plan.json"

    if not plan_file.exists():
        return True

    try:
        with open(plan_file) as f:
            plan = json.load(f)

        # Check if there are any phases with subtasks
        phases = plan.get("phases", [])
        if not phases:
            return True

        # Check if any phase has subtasks
        total_subtasks = sum(len(phase.get("subtasks", [])) for phase in phases)
        return total_subtasks == 0
    except (OSError, json.JSONDecodeError):
        # If we can't read the file, treat as first run
        return True


def _load_prompt_file(filename: str) -> str:
    """
    Load a prompt file from the prompts directory.

    Args:
        filename: Relative path to prompt file (e.g., "qa_reviewer.md" or "mcp_tools/playwright_browser.md")

    Returns:
        Content of the prompt file

    Raises:
        FileNotFoundError: If prompt file doesn't exist
    """
    prompt_file = PROMPTS_DIR / filename
    if not prompt_file.exists():
        raise FileNotFoundError(f"Prompt file not found: {prompt_file}")
    return prompt_file.read_text()


def get_qa_reviewer_prompt(spec_dir: Path, project_dir: Path) -> str:
    """
    Load the QA reviewer prompt with project-specific MCP tools dynamically injected.

    This function:
    1. Loads the base QA reviewer prompt
    2. Detects project capabilities from project_index.json
    3. Injects only relevant MCP tool documentation (Electron, Puppeteer, DB, API)

    This saves context window by excluding irrelevant tool docs.
    For example, a CLI Python project won't get Electron validation docs.

    Args:
        spec_dir: Directory containing the spec files
        project_dir: Root directory of the project

    Returns:
        The QA reviewer prompt with project-specific tools injected
    """
    # Quick Mode: Use simplified prompt (~70% fewer tokens)
    # Note: Quick mode uses simplified prompt without MCP tool injection
    if os.environ.get("QUICK_MODE") == "true":
        quick_prompt_file = PROMPTS_DIR / "qa_reviewer_quick.md"
        if quick_prompt_file.exists():
            base_prompt = quick_prompt_file.read_text()
            # Add basic spec context and return (skip MCP tool injection for speed)
            spec_context = f"""## SPEC LOCATION

Your spec and progress files are located at:
- Spec: `{spec_dir}/spec.md`
- Implementation plan: `{spec_dir}/implementation_plan.json`
- QA report output: `{spec_dir}/qa_report.md`

The project root is: `{project_dir}`

---

"""
            spec_context += get_house_standards_context(spec_dir)
            # RFC-0015: honour the per-project constitution in QA review.
            spec_context += get_constitution_context(spec_dir)
            spec_context += get_deployment_context(spec_dir)
            return spec_context + base_prompt

    # Load base QA reviewer prompt (full mode with MCP tools)
    base_prompt = _load_prompt_file("qa_reviewer.md")

    # Load project index and detect capabilities
    project_index = load_project_index(project_dir)
    capabilities = detect_project_capabilities(project_index)

    # Get list of MCP tool doc files to include
    mcp_tool_files = get_mcp_tools_for_project(capabilities)

    # Load and assemble MCP tool sections
    mcp_sections = []
    for tool_file in mcp_tool_files:
        try:
            section = _load_prompt_file(tool_file)
            mcp_sections.append(section)
        except FileNotFoundError:
            # Skip missing files gracefully
            pass

    # Inject spec context at the beginning
    spec_context = f"""## SPEC LOCATION

Your spec and progress files are located at:
- Spec: `{spec_dir}/spec.md`
- Implementation plan: `{spec_dir}/implementation_plan.json`
- Progress notes: `{spec_dir}/build-progress.txt`
- QA report output: `{spec_dir}/qa_report.md`
- Fix request output: `{spec_dir}/QA_FIX_REQUEST.md`

The project root is: `{project_dir}`

---

## PROJECT CAPABILITIES DETECTED

"""

    # Add capability summary for transparency
    active_caps = [k for k, v in capabilities.items() if v]
    if active_caps:
        spec_context += (
            "Based on project analysis, the following capabilities were detected:\n"
        )
        for cap in active_caps:
            cap_name = (
                cap.replace("is_", "").replace("has_", "").replace("_", " ").title()
            )
            spec_context += f"- {cap_name}\n"
        spec_context += "\nRelevant validation tools have been included below.\n\n"
    else:
        spec_context += (
            "No special project capabilities detected. Using standard validation.\n\n"
        )

    spec_context += "---\n\n"

    # RFC-0012: surface the team's house standards so QA checks against them.
    spec_context += get_house_standards_context(spec_dir)

    # RFC-0015: honour the per-project constitution so QA verifies enforceable
    # (hard) clauses, not just advisory ones.
    spec_context += get_constitution_context(spec_dir)

    # RFC-0013: surface the deployment context so QA verifies scans/gates and
    # the dry-run deploy policy.
    spec_context += get_deployment_context(spec_dir)

    # Find injection point in base prompt (after PHASE 4, before PHASE 5)
    injection_marker = (
        "<!-- PROJECT-SPECIFIC VALIDATION TOOLS WILL BE INJECTED HERE -->"
    )

    if mcp_sections and injection_marker in base_prompt:
        # Replace marker with actual MCP tool sections
        mcp_content = "\n\n---\n\n## PROJECT-SPECIFIC VALIDATION TOOLS\n\n"
        mcp_content += "The following validation tools are available based on your project type:\n\n"
        mcp_content += "\n\n---\n\n".join(mcp_sections)
        mcp_content += "\n\n---\n"

        # Replace the multi-line marker comment block
        marker_pattern = r"<!-- PROJECT-SPECIFIC VALIDATION TOOLS WILL BE INJECTED HERE -->.*?<!-- - API validation \(for projects with API endpoints\) -->"
        base_prompt = re.sub(marker_pattern, mcp_content, base_prompt, flags=re.DOTALL)
    elif mcp_sections:
        # Fallback: append at the end if marker not found
        base_prompt += "\n\n---\n\n## PROJECT-SPECIFIC VALIDATION TOOLS\n\n"
        base_prompt += "\n\n---\n\n".join(mcp_sections)

    return spec_context + base_prompt


def get_qa_fixer_prompt(spec_dir: Path, project_dir: Path) -> str:
    """
    Load the QA fixer prompt with spec paths injected.

    Args:
        spec_dir: Directory containing the spec files
        project_dir: Root directory of the project

    Returns:
        The QA fixer prompt content with paths injected
    """
    base_prompt = _load_prompt_file("qa_fixer.md")

    spec_context = f"""## SPEC LOCATION

Your spec and progress files are located at:
- Spec: `{spec_dir}/spec.md`
- Implementation plan: `{spec_dir}/implementation_plan.json`
- QA fix request: `{spec_dir}/QA_FIX_REQUEST.md` (READ THIS FIRST!)
- QA report: `{spec_dir}/qa_report.md`

The project root is: `{project_dir}`

---

"""
    return spec_context + base_prompt
