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

from .markdown import MARKDOWN_SUBDIR, markdown_dir

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


CORPUS_COMPARISON_MD_FILENAME = "corpus_comparison.md"

_CORPUS_CATEGORIES = ("decisions", "actions", "questions")


def _f1_cell(f1: Any, n: int) -> str:
    """A per-entity F1 cell that ALWAYS carries how many meetings fed
    the mean. ``n == 0`` renders ``n/a`` not ``0.0000`` so a reader
    never mistakes "no gold-backed successful meeting" for "perfectly
    bad" (red-team Pass 1)."""
    if not n:
        return "n/a (n=0)"
    try:
        return f"{float(f1):.4f} (n={n})"
    except (TypeError, ValueError):
        return f"n/a (n={n})"


def _gap_cell(opus_f1: Any, haiku_f1: Any, n_h: int, n_o: int) -> str:
    if not n_h or not n_o:
        return "n/a"
    try:
        gap = float(opus_f1) - float(haiku_f1)
    except (TypeError, ValueError):
        return "n/a"
    return f"{gap:+.4f}"


def _status_banner(corpus_status: str) -> str:
    if corpus_status == "rejected":
        return (
            "> **REJECTED** — fewer than half the meetings succeeded "
            "for at least one extractor. The aggregate F1 below is NOT "
            "trustworthy as a corpus measurement; treat it as "
            "diagnostic only.\n"
        )
    if corpus_status == "degraded":
        return (
            "> **DEGRADED** — at least one transcript had an extractor "
            "failure or was empty. Failed meetings are EXCLUDED from "
            "the aggregate F1 mean (see the n= count in each cell).\n"
        )
    return ""


def _partial_section(per_meeting: dict[str, Any]) -> str:
    """Render the partial-match diagnostic when ANY meeting recorded a
    partial item. The list (not just the count) must reach a human or
    the partial bucket is useless (red-team Pass 1 / Pass 2 item 5)."""
    rows: list[str] = []
    for mid in sorted(per_meeting):
        pem = (per_meeting[mid] or {}).get("per_entity_metrics")
        if not isinstance(pem, dict):
            continue
        for extractor in ("haiku", "opus"):
            blocks = pem.get(extractor)
            if not isinstance(blocks, dict):
                continue
            for cat in _CORPUS_CATEGORIES:
                cat_block = blocks.get(cat) or {}
                for it in cat_block.get("partial_items", []) or []:
                    rows.append(
                        f"| {mid} | {extractor} | {cat} | "
                        f"{str(it.get('extracted_text', '')).strip()} | "
                        f"{str(it.get('best_gold_text', '')).strip()} | "
                        f"{it.get('lcs')} |"
                    )
    if not rows:
        return ""
    header = (
        "## Partial matches (diagnostic)\n\n"
        "Partial matches (0.4 ≤ LCS < 0.7) are EXCLUDED from F1 (counted "
        "as false positives, never true positives). They are surfaced "
        "here only so a human can judge whether they are hallucinated "
        "paraphrases.\n\n"
        "| Meeting | Extractor | Entity | Extracted | Best gold | LCS |\n"
        "|---------|-----------|--------|-----------|-----------|-----|\n"
    )
    return header + "\n".join(rows) + "\n"


def render_corpus_comparison_markdown(*, corpus_payload: dict[str, Any]) -> str:
    """Pure, deterministic render of a ``corpus_comparison`` payload.

    Same payload → byte-identical bytes (data_lake_contract §6.3). This
    is a VIEW: deleting it loses nothing, the JSON artifact is
    canonical."""
    corpus_id = corpus_payload.get("corpus_id", "")
    corpus_status = corpus_payload.get("corpus_status", "unknown")
    agg = corpus_payload.get("aggregate", {}) or {}
    per_meeting = corpus_payload.get("per_meeting", {}) or {}
    meeting_ids = corpus_payload.get("meeting_ids") or sorted(per_meeting)
    total = len(meeting_ids)
    processed = agg.get("meetings_processed", 0)
    failed = agg.get("meetings_failed", 0)

    pe = agg.get("per_entity_f1", {}) or {}
    pe_n = agg.get("per_entity_f1_n_averaged", {}) or {}
    cost = agg.get("total_cost_usd", {}) or {}
    latency = agg.get("total_latency_ms", {}) or {}

    parts: list[str] = []
    header = (
        f"# Corpus Comparison — {corpus_id}\n"
        f"Status: {corpus_status}\n"
        f"Meetings processed: {processed} / {total} (failed: {failed})\n"
    )
    banner = _status_banner(corpus_status)
    if banner:
        header += "\n" + banner
    parts.append(header)

    # Aggregate per-entity F1.
    agg_rows = []
    for cat in _CORPUS_CATEGORIES:
        c = pe.get(cat, {}) or {}
        n = pe_n.get(cat, {}) or {}
        h_f1, o_f1 = c.get("haiku"), c.get("opus")
        n_h, n_o = int(n.get("haiku", 0) or 0), int(n.get("opus", 0) or 0)
        agg_rows.append(
            f"| {cat.capitalize():<10} | {_f1_cell(h_f1, n_h)} | "
            f"{_f1_cell(o_f1, n_o)} | {_gap_cell(o_f1, h_f1, n_h, n_o)} |"
        )
    parts.append(
        "## Aggregate per-entity F1\n\n"
        "| Entity     | Haiku F1 | Opus F1 | Gap (Opus − Haiku) |\n"
        "|------------|----------|---------|--------------------|\n"
        + "\n".join(agg_rows)
        + "\n\n"
        "_Aggregate F1 is the unweighted mean of gold-backed successful "
        "meetings only; `n=` is how many fed each mean. Partial matches "
        "(0.4 ≤ LCS < 0.7) are listed in the JSON artifact and excluded "
        "from F1._\n"
    )

    # Cost & latency.
    def _mean_cost(name: str) -> float:
        c = float(cost.get(name, 0.0) or 0.0)
        return round(c / processed, 6) if processed else 0.0

    def _lat_s(name: str) -> float:
        return round(int(latency.get(name, 0) or 0) / 1000.0, 3)

    parts.append(
        "## Cost & latency\n\n"
        "| Extractor | Total Cost (USD) | Total Latency (s) | "
        "Mean per Meeting |\n"
        "|-----------|------------------|-------------------|"
        "------------------|\n"
        f"| Haiku     | ${float(cost.get('haiku', 0.0) or 0.0):.6f} | "
        f"{_lat_s('haiku')} | ${_mean_cost('haiku'):.6f} |\n"
        f"| Opus      | ${float(cost.get('opus', 0.0) or 0.0):.6f} | "
        f"{_lat_s('opus')} | ${_mean_cost('opus'):.6f} |\n"
    )

    # Per-meeting breakdown.
    mrows = []
    for mid in meeting_ids:
        m = per_meeting.get(mid, {}) or {}
        st = m.get("extractor_status", {}) or {}
        # Strip the retry hint for the compact table; the full status
        # (with |retry:) lives in the JSON artifact.
        hs = str(st.get("haiku", "?")).split("|")[0]
        os_ = str(st.get("opus", "?")).split("|")[0]
        st_cell = f"H:{hs} O:{os_}"
        f1 = m.get("per_entity_f1")
        if isinstance(f1, dict):
            def _ho(cat: str) -> str:
                cc = f1.get(cat, {}) or {}
                return f"{cc.get('haiku')}/{cc.get('opus')}"

            d_c, a_c, q_c = (
                _ho("decisions"),
                _ho("actions"),
                _ho("questions"),
            )
        else:
            d_c = a_c = q_c = "—/— (no gold)"
        mrows.append(
            f"| {mid} | {st_cell} | {d_c} | {a_c} | {q_c} |"
        )
    parts.append(
        "## Per-meeting breakdown\n\n"
        "| Meeting ID | Status | Decisions F1 (H/O) | "
        "Actions F1 (H/O) | Questions F1 (H/O) |\n"
        "|------------|--------|--------------------|"
        "------------------|--------------------|\n"
        + ("\n".join(mrows) if mrows else "| _(none)_ |  |  |  |  |")
        + "\n"
    )

    disc = corpus_payload.get("discovery_findings") or []
    if disc:
        parts.append(
            "## Skipped inputs\n\n"
            + "\n".join(f"- {d}" for d in disc)
            + "\n"
        )

    partial = _partial_section(per_meeting)
    if partial:
        parts.append(partial)

    return "\n".join(p.rstrip("\n") for p in parts) + "\n"


def write_corpus_comparison_markdown(
    lake_root: Path | str,
    *,
    corpus_id: str,
    corpus_payload: dict[str, Any],
) -> Path:
    """Write the corpus report under
    ``processed/corpus/<corpus_id>/markdown/corpus_comparison.md``.

    Two calls with identical payloads leave a byte-identical file
    (data_lake_contract §6.3 determinism)."""
    from .paths import processed_corpus_dir

    target_dir = processed_corpus_dir(lake_root, corpus_id) / MARKDOWN_SUBDIR
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / CORPUS_COMPARISON_MD_FILENAME
    target_path.write_text(
        render_corpus_comparison_markdown(corpus_payload=corpus_payload),
        encoding="utf-8",
    )
    return target_path


__all__ = [
    "EXTRACTION_COMPARISON_MD_FILENAME",
    "render_extraction_comparison_markdown",
    "write_extraction_comparison_markdown",
    "CORPUS_COMPARISON_MD_FILENAME",
    "render_corpus_comparison_markdown",
    "write_corpus_comparison_markdown",
]
