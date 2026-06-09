"""
Data Models for Project Security Profiles
=========================================

Core data structures for representing technology stacks,
custom scripts, and security profiles.
"""

import os
import re
from dataclasses import asdict, dataclass, field

# A well-shaped bare command name: alnum plus . _ - (covers py.test,
# golangci-lint, pip3). No slashes / metacharacters — those can never be a
# legitimate command name.
_EXTRA_CMD_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")

# Hard denylist even for the operator override — a typo/paste must not silently
# grant privilege-escalation or host-control commands.
_EXTRA_CMD_DENY = frozenset(
    {
        "sudo", "su", "doas", "pkexec", "shutdown", "reboot", "halt",
        "poweroff", "mkfs", "fdisk", "dd",
    }
)


def _extra_allowed_commands_from_env() -> set[str]:
    """Operator-supplied extra allowed commands.

    Source: ``AIFACTORY_EXTRA_ALLOWED_COMMANDS`` (comma or whitespace
    separated). Fed by the Settings "additional allowed commands" field via
    the agent subprocess env. Well-shaped names only; hard-denied names dropped.
    """
    raw = os.environ.get("AIFACTORY_EXTRA_ALLOWED_COMMANDS", "")
    if not raw.strip():
        return set()
    out: set[str] = set()
    for tok in re.split(r"[,\s]+", raw.strip()):
        name = tok.strip()
        if name and _EXTRA_CMD_NAME_RE.match(name) and name not in _EXTRA_CMD_DENY:
            out.add(name)
    return out


@dataclass
class TechnologyStack:
    """Detected technologies in a project."""

    languages: list[str] = field(default_factory=list)
    package_managers: list[str] = field(default_factory=list)
    frameworks: list[str] = field(default_factory=list)
    databases: list[str] = field(default_factory=list)
    infrastructure: list[str] = field(default_factory=list)
    cloud_providers: list[str] = field(default_factory=list)
    code_quality_tools: list[str] = field(default_factory=list)
    version_managers: list[str] = field(default_factory=list)


@dataclass
class CustomScripts:
    """Detected custom scripts in the project."""

    npm_scripts: list[str] = field(default_factory=list)
    make_targets: list[str] = field(default_factory=list)
    poetry_scripts: list[str] = field(default_factory=list)
    cargo_aliases: list[str] = field(default_factory=list)
    shell_scripts: list[str] = field(default_factory=list)


@dataclass
class SecurityProfile:
    """Complete security profile for a project."""

    # Command sets
    base_commands: set[str] = field(default_factory=set)
    stack_commands: set[str] = field(default_factory=set)
    script_commands: set[str] = field(default_factory=set)
    custom_commands: set[str] = field(default_factory=set)

    # Detected info
    detected_stack: TechnologyStack = field(default_factory=TechnologyStack)
    custom_scripts: CustomScripts = field(default_factory=CustomScripts)

    # Metadata
    project_dir: str = ""
    created_at: str = ""
    project_hash: str = ""

    def get_all_allowed_commands(self) -> set[str]:
        """Get the complete set of allowed commands.

        Includes operator-supplied extras from the
        ``AIFACTORY_EXTRA_ALLOWED_COMMANDS`` env var (comma/whitespace
        separated) — the backend hook for the Settings "additional allowed
        commands" field. Unlike plan-granted commands (from an untrusted LLM,
        gated by a curated grant-list), these are an explicit operator override,
        so any well-shaped name is honoured. Per-command VALIDATORS (rm path
        checks, git secret scan, …) still apply at enforcement time.
        """
        return (
            self.base_commands
            | self.stack_commands
            | self.script_commands
            | self.custom_commands
            | _extra_allowed_commands_from_env()
        )

    def to_dict(self) -> dict:
        """Convert to JSON-serializable dict."""
        return {
            "base_commands": sorted(self.base_commands),
            "stack_commands": sorted(self.stack_commands),
            "script_commands": sorted(self.script_commands),
            "custom_commands": sorted(self.custom_commands),
            "detected_stack": asdict(self.detected_stack),
            "custom_scripts": asdict(self.custom_scripts),
            "project_dir": self.project_dir,
            "created_at": self.created_at,
            "project_hash": self.project_hash,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SecurityProfile":
        """Load from dict."""
        profile = cls(
            base_commands=set(data.get("base_commands", [])),
            stack_commands=set(data.get("stack_commands", [])),
            script_commands=set(data.get("script_commands", [])),
            custom_commands=set(data.get("custom_commands", [])),
            project_dir=data.get("project_dir", ""),
            created_at=data.get("created_at", ""),
            project_hash=data.get("project_hash", ""),
        )

        if "detected_stack" in data:
            profile.detected_stack = TechnologyStack(**data["detected_stack"])
        if "custom_scripts" in data:
            profile.custom_scripts = CustomScripts(**data["custom_scripts"])

        return profile
