"""Phase AB.4 — extraction gap metric eval.

Computes precision / recall / F1 of each extractor (regex, Haiku,
Opus) against an INDEPENDENT gold set, and reports the two gaps the
Phase AB instrument exists to measure:

  - ``gap_1_to_2_f1`` = haiku.f1 - regex.f1   (LLM value over regex)
  - ``gap_2_to_3_f1`` = opus.f1  - haiku.f1   (governed-pipeline cost)

LCS threshold imported from ``config/taxonomy.py`` (same constant the
Phase Z ``extraction_precision`` eval pins), so the two never drift.

Opus-output parsing is the ONE place in the codebase permitted to read
Opus raw text. The parser here is deterministic and *approximate by
design* — it does NOT call an LLM (that would inject non-determinism
into a measurement instrument). When it cannot find a structured
section it falls back to treating section prose as a single item AND
emits a warning so a reader never mistakes "parser found no structure"
for "Opus extracted nothing".
"""
from __future__ import annotations

import difflib
import json
import pathlib

from spectrum_systems_core.config.taxonomy import EXTRACTION_GAP_MIN_LCS

# Categories compared. Each extractor and the gold set carry these
# three lists; matching is per-category (a decision never matches a
# gold question) and the counts are summed for the aggregate metric.
CATEGORIES: tuple[str, ...] = ("decisions", "actions", "questions")

# Heading tokens the approximate Opus parser looks for, mapped to the
# canonical category. Lower-cased substring match on a line.
_OPUS_HEADINGS: dict[str, str] = {
    "decision": "decisions",
    "action item": "actions",
    "action items": "actions",
    "actions": "actions",
    "open question": "questions",
    "open questions": "questions",
    "question": "questions",
}

# Human-readable interpretation of the numbers, embedded in every gap
# result so a new engineer does not have to guess what 0.6 vs 0.9
# means (red-team Pass 1).
_RUBRIC = {
    "lcs_threshold": EXTRACTION_GAP_MIN_LCS,
    "match_rule": (
        "an extracted item matches a gold item when "
        "difflib.SequenceMatcher ratio of the lower-cased texts is "
        f">= {EXTRACTION_GAP_MIN_LCS}; matching is per-category and "
        "each gold item is consumed at most once"
    ),
    "f1_scale": (
        "f1 in [0,1]: ~0.0 no usable extraction, ~0.5 roughly half "
        "of gold recovered at moderate precision, ~0.9+ near-complete "
        "and precise"
    ),
    "gap_sign": (
        "gap_1_to_2_f1 > 0 means Haiku beats regex; "
        "gap_2_to_3_f1 > 0 means unconstrained Opus beats the governed "
        "Haiku path (i.e. the pipeline's structure costs F1)"
    ),
}


class EmptyGoldSetError(ValueError):
    """Raised when the independent gold set has zero items.

    A precision/recall computation on an empty gold set is a silent
    division by zero that would surface as NaN/0.0 and read like a
    real measurement. Fail loud instead (red-team Pass 1).
    """


def _ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _match_against_gold(
    extracted: list[dict], gold: list[dict]
) -> tuple[int, int, int]:
    """Returns ``(true_positives, false_positives, false_negatives)``.

    Two items match if the LCS ratio of their ``text`` values is
    >= ``EXTRACTION_GAP_MIN_LCS``. Each gold item is matched at most
    once (greedy, first extracted item that clears the threshold wins)
    so duplicate extractions cannot inflate recall.
    """
    matched_gold_idx: set[int] = set()
    tp = 0
    for extracted_item in extracted:
        et = (extracted_item or {}).get("text", "") or ""
        for i, gold_item in enumerate(gold):
            if i in matched_gold_idx:
                continue
            gt = (gold_item or {}).get("text", "") or ""
            if _ratio(et, gt) >= EXTRACTION_GAP_MIN_LCS:
                matched_gold_idx.add(i)
                tp += 1
                break
    fp = len(extracted) - tp
    fn = len(gold) - len(matched_gold_idx)
    return tp, fp, fn


def _prf(tp: int, fp: int, fn: int) -> dict:
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if (precision + recall)
        else 0.0
    )
    return {
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "tp": tp,
        "fp": fp,
        "fn": fn,
    }


def _score_extractor(output: dict, gold: dict) -> dict:
    """Per-category match, summed counts → one precision/recall/f1.

    A decision is only ever scored against gold decisions, etc., so a
    high-similarity cross-category coincidence cannot register as a
    true positive.
    """
    tp = fp = fn = 0
    for cat in CATEGORIES:
        extracted_items = output.get(cat) or []
        gold_items = gold.get(cat) or []
        if not isinstance(extracted_items, list):
            extracted_items = []
        if not isinstance(gold_items, list):
            gold_items = []
        c_tp, c_fp, c_fn = _match_against_gold(extracted_items, gold_items)
        tp += c_tp
        fp += c_fp
        fn += c_fn
    return _prf(tp, fp, fn)


def parse_opus_output(raw_output: str) -> tuple[dict, list[str]]:
    """Approximate, deterministic parser for unconstrained Opus text.

    Returns ``(parsed, warnings)`` where ``parsed`` has the same
    ``{decisions, actions, questions}`` shape as the structured
    extractors and ``warnings`` is a list of finding codes.

    Limitations (documented on purpose — this measures the
    structured-vs-unstructured tradeoff, it does not pretend Opus
    emits clean structure):

      - Recognises a section only by a heading line whose lower-cased
        text contains one of the known tokens (e.g. "decisions",
        "action items", "open questions").
      - Within a section, every non-blank line that looks like a
        bullet ("- ", "* ", "• ") or a numbered item ("1. ", "2) ")
        becomes one item; its leading marker is stripped.
      - If a recognised section contains NO bullet/numbered lines, the
        joined section prose becomes a SINGLE item and an
        ``opus_section_prose_fallback:<category>`` warning is emitted.
      - If NO recognised heading is found at all, the entire output
        becomes a single ``decisions`` item and an
        ``opus_no_structure_detected`` warning is emitted — never a
        silent zero (red-team Pass 1).
    """
    warnings: list[str] = []
    parsed: dict[str, list[dict]] = {c: [] for c in CATEGORIES}

    text = raw_output or ""
    if not text.strip():
        warnings.append("opus_output_empty")
        return parsed, warnings

    lines = text.splitlines()
    current: str | None = None
    section_lines: dict[str, list[str]] = {c: [] for c in CATEGORIES}
    bullet_items: dict[str, list[str]] = {c: [] for c in CATEGORIES}
    saw_heading = False

    def _is_bullet(s: str) -> str | None:
        st = s.strip()
        for marker in ("- ", "* ", "• ", "– "):
            if st.startswith(marker):
                return st[len(marker):].strip()
        # numbered: "1. ", "1) ", "12. " etc.
        i = 0
        while i < len(st) and st[i].isdigit():
            i += 1
        if i > 0 and i < len(st) and st[i] in ".)" and st[i + 1:i + 2] == " ":
            return st[i + 1:].strip()
        return None

    for line in lines:
        low = line.strip().lower()
        heading_cat = None
        if low:
            for token, cat in _OPUS_HEADINGS.items():
                # Heading-like: short line that starts with / is the
                # token (optionally numbered like "1. Decisions").
                stripped = low.lstrip("0123456789.) ").rstrip(":").strip()
                if stripped == token or stripped.startswith(token):
                    if len(line.strip()) <= len(token) + 24:
                        heading_cat = cat
                        break
        if heading_cat is not None:
            current = heading_cat
            saw_heading = True
            continue
        if current is None:
            continue
        item = _is_bullet(line)
        if item:
            bullet_items[current].append(item)
        elif line.strip():
            section_lines[current].append(line.strip())

    if not saw_heading:
        warnings.append("opus_no_structure_detected")
        parsed["decisions"].append({"text": text.strip()})
        return parsed, warnings

    for cat in CATEGORIES:
        if bullet_items[cat]:
            parsed[cat] = [{"text": t} for t in bullet_items[cat]]
        elif section_lines[cat]:
            warnings.append(f"opus_section_prose_fallback:{cat}")
            parsed[cat] = [{"text": " ".join(section_lines[cat])}]
        # else: genuinely empty recognised section → 0 items, no warn.
    return parsed, warnings


def compute_gap_metrics(
    comparison_artifact: dict,
    gold_path: pathlib.Path,
    *,
    opus_raw_output: str | None = None,
) -> dict:
    """Compute per-extractor precision/recall/F1 and the two gaps.

    ``comparison_artifact`` is the ``extraction_comparison`` payload
    (``regex_output`` / ``haiku_output`` inline; Opus only by ref).
    ``opus_raw_output`` is the opaque text from the referenced
    ``extraction_unconstrained`` artifact. It is keyword-only and
    optional so the pinned 2-arg call ``compute_gap_metrics(art,
    gold)`` still works (Opus then scores 0 with an
    ``opus_raw_output_not_supplied`` warning rather than crashing); the
    comparison runner always passes it explicitly. This function is the
    ONLY code path permitted to parse Opus output.

    Raises ``EmptyGoldSetError`` if the gold set has zero items across
    all categories — an empty gold set makes precision/recall a silent
    0.0 that reads like a real measurement.
    """
    gold_path = pathlib.Path(gold_path)
    gold = json.loads(gold_path.read_text(encoding="utf-8"))

    total_gold = sum(
        len(gold.get(cat) or []) if isinstance(gold.get(cat), list) else 0
        for cat in CATEGORIES
    )
    if total_gold == 0:
        raise EmptyGoldSetError(
            f"empty_gold_set:{gold_path} carries zero items across "
            f"{CATEGORIES}; cannot compute precision/recall"
        )

    payload = comparison_artifact.get("payload", comparison_artifact)
    regex_output = payload.get("regex_output") or {}
    haiku_output = payload.get("haiku_output") or {}

    opus_warnings: list[str] = []
    if opus_raw_output is None:
        opus_warnings.append("opus_raw_output_not_supplied")
        opus_output: dict = {c: [] for c in CATEGORIES}
    else:
        opus_output, opus_warnings = parse_opus_output(opus_raw_output)

    regex = _score_extractor(regex_output, gold)
    haiku = _score_extractor(haiku_output, gold)
    opus = _score_extractor(opus_output, gold)

    return {
        "regex": regex,
        "haiku": haiku,
        "opus": opus,
        "gap_1_to_2_f1": round(haiku["f1"] - regex["f1"], 4),
        "gap_2_to_3_f1": round(opus["f1"] - haiku["f1"], 4),
        "gold_item_count": total_gold,
        "gold_rubric": gold.get("rubric"),
        "opus_parser_warnings": opus_warnings,
        "rubric": _RUBRIC,
    }


__all__ = [
    "EXTRACTION_GAP_MIN_LCS",
    "CATEGORIES",
    "EmptyGoldSetError",
    "parse_opus_output",
    "compute_gap_metrics",
    "_match_against_gold",
]
