"""Phase AB.5 — deterministic Markdown view for extraction comparisons.

Renders an ``extraction_comparison`` instrument artifact (+ its
``extraction_telemetry`` sibling) to a single human-readable report.

Contract notes:
  - This is a VIEW (data_lake_contract §6.3): regenerated from the
    canonical JSON, never canonical itself. Deleting it loses nothing.
  - It is a Phase-AB measurement-instrument view, not a product-artifact
    view, so the §6.3 per-artifact frontmatter table does not bind it;
    the §6.3 determinism rule does — identical inputs MUST produce
    byte-identical Markdown.
  - The Opus column is the OPAQUE raw text passed through verbatim.
    This renderer NEVER parses it (the only Opus parser is
    ``evals.extraction_gap``). Quoting opaque text in a view is not
    "parsing" — markdown is not an eval and not the control gate.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .markdown import markdown_dir

EXTRACTION_COMPARISON_MD_FILENAME = "extraction_comparison.md"


def _bullets(items: list[Any]) -> str:
    if not items:
        return "_(none)_"
    out: list[str] = []
    for it in items:
        if isinstance(it, dict):
            text = str(it.get("text", "")).strip()
            owner = it.get("owner")
            verb = it.get("verb")
            suffix = ""
            if verb:
                suffix += f" _(verb: {verb})_"
            if owner:
                suffix += f" _(owner: {owner})_"
            out.append(f"- {text}{suffix}")
        else:
            out.append(f"- {str(it).strip()}")
    return "\n".join(out)


def _category_block(
    title: str,
    regex_items: list[Any],
    haiku_items: list[Any],
    opus_raw_text: str,
) -> str:
    return (
        f"## {title}\n\n"
        f"### Regex\n{_bullets(regex_items)}\n\n"
        f"### Haiku\n{_bullets(haiku_items)}\n\n"
        f"### Opus (raw)\n{_opus_quoted(opus_raw_text)}\n"
    )


def _opus_quoted(opus_raw_text: str) -> str:
    """Verbatim opaque Opus text inside a fenced block. Never parsed."""
    if not opus_raw_text.strip():
        return "_(no Opus output)_"
    # Fence so the opaque text renders literally and a stray "##" in
    # Opus output cannot break the report structure.
    return "```text\n" + opus_raw_text.rstrip("\n") + "\n```"


def _cost_row(name: str, tel: dict[str, Any]) -> str:
    cost = float(tel.get("cost_usd", 0.0) or 0.0)
    latency = int(tel.get("latency_ms", 0) or 0)
    return f"| {name:<9} | ${cost:.4f} | {latency} |"


def _gap_block(gap_metrics: dict[str, Any] | None) -> str:
    if not gap_metrics:
        return (
            "## Gap Metrics\n\n"
            "_Not computed — no independent gold set is associated "
            "with this meeting. Gap metrics are produced by "
            "`evals.extraction_gap.compute_gap_metrics` against a "
            "`tests/fixtures/comparison_gold/` fixture._\n"
        )
    regex_f1 = gap_metrics.get("regex", {}).get("f1")
    haiku_f1 = gap_metrics.get("haiku", {}).get("f1")
    opus_f1 = gap_metrics.get("opus", {}).get("f1")
    g12 = gap_metrics.get("gap_1_to_2_f1")
    g23 = gap_metrics.get("gap_2_to_3_f1")
    warns = gap_metrics.get("opus_parser_warnings") or []
    lines = [
        "## Gap Metrics",
        "",
        f"- Regex F1: {regex_f1}",
        f"- Haiku F1: {haiku_f1}",
        f"- Opus F1:  {opus_f1}",
        f"- Gap 1→2 (regex → Haiku): {g12}",
        f"- Gap 2→3 (Haiku → Opus):  {g23}",
        f"- Gold items: {gap_metrics.get('gold_item_count')}",
    ]
    if warns:
        lines.append(f"- Opus parser warnings: {', '.join(warns)}")
    return "\n".join(lines) + "\n"


def render_extraction_comparison_markdown(
    *,
    comparison_payload: dict[str, Any],
    telemetry_payload: dict[str, Any],
    opus_raw_text: str = "",
    gap_metrics: dict[str, Any] | None = None,
) -> str:
    """Pure, deterministic render. Same inputs → identical bytes."""
    meeting_id = comparison_payload.get("meeting_id", "")
    status = comparison_payload.get("extractor_status", {})
    regex_out = comparison_payload.get("regex_output", {}) or {}
    haiku_out = comparison_payload.get("haiku_output", {}) or {}

    parts: list[str] = []
    parts.append(f"# Extraction Comparison — {meeting_id}\n")
    parts.append(
        "## Status\n"
        f"- Regex: {status.get('regex', 'unknown')}\n"
        f"- Haiku: {status.get('haiku', 'unknown')}\n"
        f"- Opus:  {status.get('opus', 'unknown')}\n"
    )
    parts.append(
        "## Cost\n\n"
        "| Extractor | Cost (USD) | Latency (ms) |\n"
        "|-----------|------------|--------------|\n"
        + _cost_row("Regex", telemetry_payload.get("regex", {})) + "\n"
        + _cost_row("Haiku", telemetry_payload.get("haiku", {})) + "\n"
        + _cost_row("Opus", telemetry_payload.get("opus", {})) + "\n"
    )
    parts.append(_gap_block(gap_metrics))
    parts.append(
        _category_block(
            "Decisions",
            regex_out.get("decisions", []),
            haiku_out.get("decisions", []),
            opus_raw_text,
        )
    )
    parts.append(
        _category_block(
            "Actions",
            regex_out.get("actions", []),
            haiku_out.get("actions", []),
            opus_raw_text,
        )
    )
    parts.append(
        _category_block(
            "Questions",
            regex_out.get("questions", []),
            haiku_out.get("questions", []),
            opus_raw_text,
        )
    )
    # Single trailing newline; join with blank lines between blocks.
    return "\n".join(p.rstrip("\n") for p in parts) + "\n"


def write_extraction_comparison_markdown(
    lake_root: Path | str,
    *,
    meeting_id: str,
    comparison_payload: dict[str, Any],
    telemetry_payload: dict[str, Any],
    opus_raw_text: str = "",
    gap_metrics: dict[str, Any] | None = None,
) -> Path:
    """Write the report under ``markdown/extraction_comparison.md``.

    Returns the path. Two calls with identical inputs leave a
    byte-identical file (data_lake_contract §6.3 determinism)."""
    target_dir = markdown_dir(lake_root, meeting_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / EXTRACTION_COMPARISON_MD_FILENAME
    target_path.write_text(
        render_extraction_comparison_markdown(
            comparison_payload=comparison_payload,
            telemetry_payload=telemetry_payload,
            opus_raw_text=opus_raw_text,
            gap_metrics=gap_metrics,
        ),
        encoding="utf-8",
    )
    return target_path


__all__ = [
    "EXTRACTION_COMPARISON_MD_FILENAME",
    "render_extraction_comparison_markdown",
    "write_extraction_comparison_markdown",
]
