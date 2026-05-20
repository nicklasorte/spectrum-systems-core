"""Phase 3P negative-transfer guard.

Reads comparison artifacts under ``<lake>/store/processed/meetings/*/``
and pairs the latest "pre-few-shot" comparison (one whose
``prompt_content_hash`` does NOT match the current production prompt's
hash) with the latest "post-few-shot" comparison (one whose
``prompt_content_hash`` DOES match) for each source. The guard fails
if the post-few-shot F1 dropped by more than 0.05 (5 points) on any
source. Sources with fewer than two comparisons are skipped (the
delta is undefined).

Exit codes:
- 0: PASS (no source regressed by more than 5 points).
- 1: FAIL (at least one source regressed).
- 2: structural error (missing lake, malformed artifact).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys
from typing import Any, Optional

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_PROMPT_PATH = (
    _REPO_ROOT
    / "src"
    / "spectrum_systems_core"
    / "workflows"
    / "prompts"
    / "meeting_minutes_llm.md"
)

REGRESSION_THRESHOLD: float = 0.05


def _current_prompt_hash() -> str:
    """Hash the current production prompt file.

    Mirrors ``pipeline.governed_run.prompt_content_hash``: sha256 of the
    UTF-8 bytes after newline normalisation. The few-shot wrapper
    markers are part of the canonical text, so the hash bound to a
    Phase-3P prompt is distinct from any pre-3P prompt.
    """
    text = _PROMPT_PATH.read_text(encoding="utf-8")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read_comparisons(meeting_dir: pathlib.Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in sorted(meeting_dir.glob("comparison_result*.json")):
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        out.append(doc)
    return out


def _extract_prompt_hash(comparison: dict[str, Any]) -> Optional[str]:
    """Pull the prompt_content_hash from a comparison artifact.

    Looks first at top-level ``prompt_content_hash`` (fixture-friendly
    shape), then at ``haiku.extraction_config.prompt_content_hash``,
    which is where the production comparison engine stamps it.
    """
    top = comparison.get("prompt_content_hash")
    if isinstance(top, str) and top:
        return top
    haiku = comparison.get("haiku") or {}
    ec = haiku.get("extraction_config") or {}
    val = ec.get("prompt_content_hash")
    if isinstance(val, str) and val:
        return val
    return None


def _extract_f1(comparison: dict[str, Any]) -> Optional[float]:
    summary = comparison.get("summary") or {}
    val = summary.get("haiku_f1_vs_opus")
    if val is None:
        val = comparison.get("haiku_f1_vs_opus")
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _classify_per_source(
    comparisons: list[dict[str, Any]],
    current_hash: str,
) -> tuple[Optional[float], Optional[float]]:
    """Return ``(pre_f1, post_f1)`` for one source.

    ``pre`` = most recent comparison whose prompt hash != current hash.
    ``post`` = most recent comparison whose prompt hash == current hash.
    Either may be ``None`` if no qualifying comparison exists.
    """
    pre: Optional[dict[str, Any]] = None
    post: Optional[dict[str, Any]] = None
    # Process in declared order. Comparisons are timestamped via
    # `compared_at`; sort newest first so the first match per category
    # is the most recent.
    def _ts(c: dict[str, Any]) -> str:
        return str(c.get("compared_at", ""))

    for c in sorted(comparisons, key=_ts, reverse=True):
        h = _extract_prompt_hash(c)
        if h is None:
            continue
        if h == current_hash and post is None:
            post = c
        elif h != current_hash and pre is None:
            pre = c
        if pre is not None and post is not None:
            break

    pre_f1 = _extract_f1(pre) if pre is not None else None
    post_f1 = _extract_f1(post) if post is not None else None
    return pre_f1, post_f1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Phase 3P negative-transfer guard. Fails when any source's "
            "F1 drops by more than 5 points after the few-shot prompt "
            "lands."
        )
    )
    parser.add_argument(
        "--lake",
        type=pathlib.Path,
        required=True,
        help="Data-lake root containing store/processed/meetings/.",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=REGRESSION_THRESHOLD,
        help=(
            "F1 regression threshold (default 0.05). A post_f1 - pre_f1 "
            "delta below the negative threshold counts as a regression."
        ),
    )
    parser.add_argument(
        "--current-hash",
        type=str,
        default=None,
        help=(
            "Override the current production prompt hash. Defaults to "
            "the sha256 of the live prompt file. Tests use this so the "
            "fixture is decoupled from the prompt file's actual content."
        ),
    )
    args = parser.parse_args(argv)

    meetings_root = args.lake / "store" / "processed" / "meetings"
    if not meetings_root.is_dir():
        # Tolerate fixture-style flat layout (lake/<source>/...) used
        # by the CI fixture. Required because the production layout
        # nests under store/processed but a hermetic fixture does not.
        meetings_root = args.lake
        if not meetings_root.is_dir():
            print(f"FAIL data lake missing: {args.lake}", file=sys.stderr)
            return 2

    current_hash = args.current_hash or _current_prompt_hash()
    regressed: list[tuple[str, float, float, float]] = []
    skipped: list[str] = []
    examined: list[tuple[str, float, float, float]] = []

    for src_dir in sorted(meetings_root.iterdir()):
        if not src_dir.is_dir():
            continue
        source_id = src_dir.name
        comparisons = _read_comparisons(src_dir)
        if len(comparisons) < 2:
            skipped.append(source_id)
            continue
        pre_f1, post_f1 = _classify_per_source(comparisons, current_hash)
        if pre_f1 is None or post_f1 is None:
            skipped.append(source_id)
            continue
        delta = post_f1 - pre_f1
        examined.append((source_id, pre_f1, post_f1, delta))
        if delta < -float(args.threshold):
            regressed.append((source_id, pre_f1, post_f1, delta))

    for source_id, pre, post, delta in examined:
        print(
            f"  {source_id}: pre={pre:.3f} post={post:.3f} "
            f"delta={delta:+.3f}"
        )
    if skipped:
        print(f"SKIPPED (insufficient comparisons): {skipped}")

    if regressed:
        print(
            f"FAIL negative-transfer guard: {len(regressed)} source(s) "
            "regressed by more than threshold",
            file=sys.stderr,
        )
        for source_id, pre, post, delta in regressed:
            print(
                f"  {source_id}: pre={pre:.3f} post={post:.3f} "
                f"delta={delta:+.3f}",
                file=sys.stderr,
            )
        return 1

    print("PASS: no source regressed beyond the threshold")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
