"""EvalScoreHistory — append-only per-eval-name history.

harness/evals/<artifact_type>_history.jsonl is SEPARATE from contracts/evals/.
contracts/evals/ defines what to test (the cases). harness/evals/ records what
happened (the results). Append only. Never overwrite. Pipeline-non-blocking.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List

from ._io import append_jsonl, read_jsonl, utcnow_iso
from ._paths import evals_dir


_LOG = logging.getLogger(__name__)


class EvalScoreHistory:
    def record_eval_results(
        self,
        run_id: str,
        eval_results: List[Dict[str, Any]],
        artifact_type: str,
        repo_root: str | Path,
    ) -> None:
        """Append eval results to harness/evals/<artifact_type>_history.jsonl."""
        try:
            target = evals_dir(repo_root) / f"{artifact_type}_history.jsonl"
            target.parent.mkdir(parents=True, exist_ok=True)
            recorded_at = utcnow_iso()
            for result in eval_results or []:
                entry = {
                    "run_id": run_id,
                    "artifact_type": artifact_type,
                    "eval_name": str(result.get("name") or result.get("eval_name") or ""),
                    "status": str(result.get("status") or "unknown"),
                    "score": result.get("score"),
                    "recorded_at": recorded_at,
                }
                append_jsonl(target, entry)
        except OSError as exc:  # pragma: no cover
            _LOG.warning("EvalScoreHistory.record_eval_results failed: %s", exc)

    def get_pass_rate(
        self,
        eval_name: str,
        artifact_type: str,
        repo_root: str | Path,
        last_n_runs: int = 20,
    ) -> Dict[str, Any]:
        target = evals_dir(repo_root) / f"{artifact_type}_history.jsonl"
        records = [
            r for r in read_jsonl(target)
            if r.get("eval_name") == eval_name
        ]
        if not records:
            return {
                "eval_name": eval_name,
                "pass_rate": None,
                "total": 0,
                "pass": 0,
                "fail": 0,
                "warn": 0,
            }
        records = records[-int(last_n_runs):] if last_n_runs > 0 else records
        passed = sum(1 for r in records if r.get("status") == "pass")
        failed = sum(1 for r in records if r.get("status") == "fail")
        warned = sum(1 for r in records if r.get("status") == "warn")
        total = len(records)
        return {
            "eval_name": eval_name,
            "pass_rate": (passed / total) if total else None,
            "total": total,
            "pass": passed,
            "fail": failed,
            "warn": warned,
        }

    def get_degrading_evals(
        self,
        repo_root: str | Path,
        threshold: float = 0.8,
    ) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        directory = evals_dir(repo_root)
        if not directory.is_dir():
            return results
        try:
            files = sorted(directory.glob("*_history.jsonl"))
        except OSError:  # pragma: no cover
            return results
        for path in files:
            artifact_type = path.name[: -len("_history.jsonl")]
            records = read_jsonl(path)
            if not records:
                continue
            eval_names = sorted({r.get("eval_name", "") for r in records if r.get("eval_name")})
            for name in eval_names:
                stats = self.get_pass_rate(
                    name, artifact_type, repo_root, last_n_runs=20
                )
                rate = stats["pass_rate"]
                if rate is None:
                    continue
                if rate < threshold:
                    results.append(
                        {
                            "artifact_type": artifact_type,
                            **stats,
                        }
                    )
        return results

    def write_eval_history_projection(
        self,
        repo_root: str | Path,
        vault_root: str | Path | None = None,
    ) -> str:
        from ..ingestion.obsidian_projection import ObsidianProjection

        return ObsidianProjection().write_eval_history_projection(
            repo_root, vault_root
        )
