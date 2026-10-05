"""Prompt templates shipped with the package (seed versions; the registry versions them)."""

from __future__ import annotations

from importlib import resources


def load(name: str) -> str:
    return resources.files(__name__).joinpath(f"{name}.md").read_text(encoding="utf-8")
