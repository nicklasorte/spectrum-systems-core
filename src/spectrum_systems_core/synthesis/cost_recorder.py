"""Cost record append-only logger for synthesis runs (FINDING-F-007).

Each Sonnet call appends a synthesis_run_cost_record line to
synthesis/<run_id>/cost.jsonl. The pricing constants below match
Anthropic's published Sonnet 4 rates: input $3 / Mtok, output $15 / Mtok.
"""
from __future__ import annotations

import datetime
import json
import uuid
from pathlib import Path
from typing import Any

import jsonschema

from ._paths import synthesis_run_dir, synthesis_schema_path

SONNET_INPUT_USD_PER_MTOK = 3.0
SONNET_OUTPUT_USD_PER_MTOK = 15.0
MAX_SYNTHESIS_COST_USD = 0.50


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def estimate_cost_usd(input_tokens: int, output_tokens: int) -> float:
    return (
        input_tokens * SONNET_INPUT_USD_PER_MTOK
        + output_tokens * SONNET_OUTPUT_USD_PER_MTOK
    ) / 1_000_000


def append_cost_record(
    run_id: str,
    repo_root: str,
    *,
    call_purpose: str,
    input_tokens: int,
    output_tokens: int,
    model: str,
) -> dict[str, Any]:
    record = {
        "cost_id": str(uuid.uuid4()),
        "run_id": run_id,
        "call_purpose": call_purpose,
        "input_tokens": int(input_tokens),
        "output_tokens": int(output_tokens),
        "estimated_cost_usd": float(
            estimate_cost_usd(int(input_tokens), int(output_tokens))
        ),
        "model": model,
        "recorded_at": _now_iso(),
    }
    schema = json.loads(
        synthesis_schema_path("synthesis_run_cost_record")
        .read_text(encoding="utf-8")
    )
    jsonschema.Draft202012Validator(schema).validate(record)
    repo_root_path = Path(repo_root).resolve()
    run_dir = synthesis_run_dir(repo_root_path, run_id, create=True)
    target = run_dir / "cost.jsonl"
    with target.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
    return record


def read_cost_records(run_id: str, repo_root: str) -> list:
    target = (
        Path(repo_root).resolve() / "synthesis" / run_id / "cost.jsonl"
    )
    out = []
    if not target.is_file():
        return out
    with target.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def total_cost_usd(run_id: str, repo_root: str) -> float:
    return float(
        sum(r.get("estimated_cost_usd", 0.0) for r in read_cost_records(run_id, repo_root))
    )
