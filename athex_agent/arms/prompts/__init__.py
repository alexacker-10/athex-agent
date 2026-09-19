"""Frozen prompt variants for the LLM arms. Changing text here changes an experiment: bump the
version string and log it in DECISIONS.md; live arms are pinned to a version via their lock."""

from athex_agent.arms.prompts.base import BASE_SYSTEM_PROMPT, PROMPT_VERSIONS

__all__ = ["BASE_SYSTEM_PROMPT", "PROMPT_VERSIONS"]
