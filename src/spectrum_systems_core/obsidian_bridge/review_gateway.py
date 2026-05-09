"""Module 2a: Obsidian review gateway.

Emits a Markdown review form for a reviewer to fill out, polls for completion,
and hands off parsing to ``ObsidianReviewParser``.
"""
from __future__ import annotations

import datetime
import os
import shutil
from typing import Any, Dict

from . import _frontmatter
from .review_parser import ObsidianReviewParser


class ObsidianReviewGateway:

    REVIEW_FORM_TEMPLATE = """---
review_for_artifact_id: "{{artifact_id}}"
review_for_artifact_type: "{{artifact_type}}"
pipeline_run_id: "{{pipeline_run_id}}"
reviewer_id: ""
decision: ""
reviewed_at: ""
review_status: pending
---

# Review: {{artifact_type}} — {{artifact_id}}

**Eval Summary**
{{eval_summary_markdown}}

---

## Your Decision

Set `decision` in the frontmatter to one of:

- `approve` — artifact is publication-ready, no changes needed
- `revise` — changes needed (list findings below)
- `block` — artifact must be rejected outright (list reason below)

---

## Findings

> Add one finding per section. Delete this section if decision = approve.
> Severity: S0 (cosmetic) | S1 (minor) | S2 (requires fix) | S3 (major) | S4 (critical/block)

### Finding 1

- **severity**: <!-- S0 | S1 | S2 | S3 | S4 -->
- **section**: <!-- which section or field this applies to -->
- **description**: <!-- what is wrong -->
- **required_action**: <!-- what must change -->

---

## Reviewer Notes

<!-- Optional: any additional context -->

---

> When complete: set `review_status` to `submitted` in the frontmatter.
> The pipeline resumes automatically once this field is set.
"""

    def emit_review_form(
        self,
        artifact_id: str,
        artifact_type: str,
        pipeline_run_id: str,
        eval_summary_markdown: str,
        vault_root: str,
    ) -> str:
        rendered = (
            self.REVIEW_FORM_TEMPLATE
            .replace("{{artifact_id}}", artifact_id)
            .replace("{{artifact_type}}", artifact_type)
            .replace("{{pipeline_run_id}}", pipeline_run_id)
            .replace("{{eval_summary_markdown}}", eval_summary_markdown)
        )
        pending_dir = os.path.join(vault_root, "Reviews", "Pending")
        os.makedirs(pending_dir, exist_ok=True)
        dest = os.path.join(pending_dir, f"{artifact_id}_review.md")
        with open(dest, "wb") as fh:
            fh.write(rendered.encode("utf-8"))
        return os.path.abspath(dest)

    def poll_for_completion(
        self,
        artifact_id: str,
        vault_root: str,
        timeout_hours: int = 72,
    ) -> Dict[str, Any]:
        review_note_path = os.path.join(
            vault_root, "Reviews", "Pending", f"{artifact_id}_review.md"
        )
        if not os.path.exists(review_note_path):
            return {"status": "not_found"}
        try:
            with open(review_note_path, "rb") as fh:
                raw = fh.read().decode("utf-8")
            frontmatter, _body = _frontmatter.split(raw)
        except (OSError, UnicodeDecodeError, ValueError):
            return {"status": "awaiting"}

        if frontmatter.get("review_status") == "submitted":
            parsed = ObsidianReviewParser().parse(review_note_path, vault_root)
            completed_dir = os.path.join(vault_root, "Reviews", "Completed")
            os.makedirs(completed_dir, exist_ok=True)
            completed_path = os.path.join(
                completed_dir, f"{artifact_id}_review.md"
            )
            shutil.move(review_note_path, completed_path)
            return {"status": "complete", "artifact": parsed.get("artifact")}

        awaiting_since = frontmatter.get("ingestion_at")
        if awaiting_since:
            try:
                started = datetime.datetime.strptime(
                    awaiting_since, "%Y-%m-%dT%H:%M:%SZ"
                )
            except (TypeError, ValueError):
                started = None
            if started is not None:
                elapsed = datetime.datetime.utcnow() - started
                if elapsed >= datetime.timedelta(hours=timeout_hours):
                    return {"status": "timeout"}
        return {"status": "awaiting"}
