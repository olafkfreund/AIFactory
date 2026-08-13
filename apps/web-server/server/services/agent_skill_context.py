"""Skill-context file writing — SkillContextMixin (#703).

_write_skill_context (reads a task's selected/suggested skills from
task_metadata.json and writes skill_context.md into the spec dir for the agent
to pick up), lifted out of the AgentService god-class into a mixin. The method
uses no instance state, so the mixin needs no host-dependency stubs; AgentService
inherits it and the method runs unchanged via the MRO.
"""

from __future__ import annotations

import contextlib
import json
import logging
from pathlib import Path


class SkillContextMixin:
    """Writes skill_context.md from a task's selected skills (for AgentService)."""

    def _write_skill_context(self, spec_dir: Path) -> None:
        """Write skill_context.md to spec_dir based on selectedSkills in task_metadata.json.

        If selectedSkills is non-empty, loads up to 5 skill files and writes them
        as a structured markdown file that the agent system will auto-include as
        context (the agent reads all .md files in spec_dir).

        If no skills are selected, removes any existing skill_context.md.
        """

        logger = logging.getLogger(__name__)

        skill_context_file = spec_dir / "skill_context.md"
        task_metadata_file = spec_dir / "task_metadata.json"

        # Load task metadata to get selected skills
        selected_skill_ids: list[str] = []
        if task_metadata_file.exists():
            try:
                task_metadata = json.loads(task_metadata_file.read_text())
                # Hybrid skill selection (#394): prefer the planner-confirmed
                # selectedSkills; fall back to the auto-proposed suggestedSkills
                # so relevant skills are always applied even if the planner
                # didn't refine them.
                raw_skills = task_metadata.get("selectedSkills") or task_metadata.get(
                    "suggestedSkills", []
                )
                # skills are stored as list[dict] with {id, name, category, source}
                # Also handle plain string IDs for backward compatibility
                for item in raw_skills:
                    if isinstance(item, dict):
                        sid = item.get("id", "")
                    else:
                        sid = str(item)
                    if sid:
                        selected_skill_ids.append(sid)
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(
                    f"[AgentService] Could not read task_metadata.json for skills: {e}"
                )

        # If no skills selected, remove any existing skill_context.md
        if not selected_skill_ids:
            if skill_context_file.exists():
                try:
                    skill_context_file.unlink()
                    logger.info(
                        "[AgentService] Removed skill_context.md (no skills selected)"
                    )
                except OSError as e:
                    logger.warning(
                        f"[AgentService] Could not remove skill_context.md: {e}"
                    )
            return

        # Load skill contents (max 5 skills to stay within token budget)
        from .skills_service import get_skills_service

        skills_service = get_skills_service()

        sections: list[str] = []
        loaded_count = 0

        for skill_id in selected_skill_ids[:5]:
            # Parse skill_id format: "{category}/{skill_name}"
            if "/" not in skill_id:
                logger.warning(
                    f"[AgentService] Invalid skill_id format (missing '/'): {skill_id}"
                )
                continue

            category, name = skill_id.split("/", 1)
            skill_summary = skills_service.get_skill(category, name)
            skill_content = skills_service.get_skill_content(category, name)

            if skill_content is None:
                logger.warning(f"[AgentService] Skill not found in index: {skill_id}")
                continue

            # Truncate each skill to 2500 chars to manage token budget
            skill_content_truncated = skill_content[:2500]
            if len(skill_content) > 2500:
                skill_content_truncated += "\n\n*[Content truncated for token budget]*"

            display_name = skill_summary.name if skill_summary else name
            sections.append(
                f"## {display_name} ({category})\n\n{skill_content_truncated}\n\n---"
            )
            loaded_count += 1

        if not sections:
            # No skills could be loaded — clean up stale file if present
            if skill_context_file.exists():
                with contextlib.suppress(OSError):
                    skill_context_file.unlink()
            return

        # Format as structured markdown
        header = (
            "# Selected Skills Context\n\n"
            "The following skill documentation has been included to assist with this task.\n"
            "Reference these skills when implementing the solution.\n\n"
            "---"
        )
        skill_context_content = header + "\n\n" + "\n\n".join(sections) + "\n"

        try:
            spec_dir.mkdir(parents=True, exist_ok=True)
            skill_context_file.write_text(skill_context_content, encoding="utf-8")
            logger.info(
                f"[AgentService] Wrote skill_context.md with {loaded_count} skill(s)"
            )
        except OSError as e:
            logger.error(f"[AgentService] Failed to write skill_context.md: {e}")
