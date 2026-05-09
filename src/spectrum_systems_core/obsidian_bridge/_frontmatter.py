"""Helpers for parsing and rewriting YAML frontmatter on Markdown notes."""
from __future__ import annotations

import re
from typing import Tuple

import yaml


_FRONTMATTER_RE = re.compile(
    r"\A---\r?\n(.*?)\r?\n---\r?\n?(.*)\Z",
    re.DOTALL,
)


def split(text: str) -> Tuple[dict, str]:
    """Return (frontmatter_dict, body) parsed from a Markdown document.

    Raises ValueError if no frontmatter block is present or YAML is invalid.
    """
    match = _FRONTMATTER_RE.match(text)
    if not match:
        raise ValueError("frontmatter block not found")
    raw_yaml, body = match.group(1), match.group(2)
    loaded = yaml.safe_load(raw_yaml)
    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        raise ValueError("frontmatter must parse to a mapping")
    return loaded, body


def assemble(frontmatter: dict, body: str) -> str:
    """Reassemble a Markdown document from frontmatter dict and body string."""
    dumped = yaml.safe_dump(
        frontmatter,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
    ).rstrip("\n")
    return f"---\n{dumped}\n---\n{body}"


def stamp_file(path: str, fields: dict) -> None:
    """Best-effort: read the file, merge fields into frontmatter, rewrite."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read().decode("utf-8")
        try:
            fm, body = split(raw)
        except ValueError:
            fm, body = {}, raw
        fm.update(fields)
        new_text = assemble(fm, body)
        with open(path, "wb") as fh:
            fh.write(new_text.encode("utf-8"))
    except OSError:
        pass
