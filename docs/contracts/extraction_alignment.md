# Extraction Alignment Contract

Document ID: SSC-CONTRACT-002
Status: Binding for Phase Y onward
Scope: How a Haiku-extracted item is judged to "match" an Opus
ceiling item when computing recall / precision / F1.

`alignment_contract_version: "1.0.0"`

This contract is binding. The system constitution
(`docs/architecture/system_constitution.md`) takes precedence on any
conflict. It is consumed by `evals/extraction_comparison.py`, which
refuses to run if the version it is handed does not equal the version
declared on this line above (fail-closed; no silent version drift).

---

## 1. The alignment predicate

A Haiku item **aligns** to a ceiling item if and only if **all three**
of the following hold:

1. They share the same `schema_type` (exact string equality), AND
2. Source-span IoU on `source_turn_ids` ≥ **0.50**, AND
3. Text cosine similarity on `source_text` (TF-IDF, scikit-learn
   `TfidfVectorizer` defaults, both texts lowercased and stripped) ≥
   **0.70**.

IoU is `|A ∩ B| / |A ∪ B|` over the *sets* of `source_turn_ids`. If
both sides have empty `source_turn_ids` the IoU is defined as `0.0`
(an ungrounded pair cannot be a confident span match — fail-closed,
never `1.0`).

The TF-IDF vector space is fit on the corpus of every ceiling and
Haiku `source_text` in the comparison so the vocabulary is fixed and
the computation is deterministic for identical inputs. Cosine is
rounded to 6 decimal places before the threshold test and before it is
written into `aligned_pairs`, so two runs over identical inputs
produce a byte-identical artifact.

Matching is **one-to-one and greedy within a `schema_type`**:
candidate pairs that satisfy the predicate are ordered by
`(ceiling_item_id, haiku_item_id)` and assigned greedily; once a
ceiling item or a Haiku item is consumed it cannot align again. The
deterministic ordering is what makes the pairing replay-stable.

---

## 2. Metrics

For one `schema_type`:

- `true_positives` — count of aligned pairs.
- `false_negatives` — ceiling items with no aligned Haiku item.
- `false_positives` — Haiku items with no aligned ceiling item.
- `recall = true_positives / ceiling_count` (0.0 if `ceiling_count == 0`).
- `precision = true_positives / haiku_count` (0.0 if `haiku_count == 0`).
- `f1 = 2·P·R / (P + R)` (0.0 if `P + R == 0`).

`recall`, `precision`, and `f1` are exact rationals over integer
counts and are written without rounding (a Python float division of
two ints is itself deterministic). Only `iou` and `cosine` inside
`aligned_pairs` are rounded, because they are the only floats that
flow from the vectorizer.

`total_metrics` aggregates true/false positives/negatives across every
`schema_type` and recomputes recall / precision / f1 from those totals
(micro-average, not a mean of per-type F1s — a mean would let a
high-volume type hide a collapsed rare type).

---

## 3. Rejected alternatives (rationale)

- **Looser: IoU ≥ 0.30 / cosine ≥ 0.50.** Rejected: at IoU 0.30 a
  Haiku item citing one of three ceiling turns counts as a hit, which
  inflates recall and lets the Y.3 gate pass output that is mostly
  ungrounded. The point of the ceiling is to be honest about misses.
- **Stricter: IoU ≥ 0.80 / cosine ≥ 0.90 / exact turn-set equality.**
  Rejected: Opus and Haiku legitimately cite overlapping-but-not-equal
  turn windows for the same decision; an over-strict predicate
  manufactures false negatives and would make the miner chase
  phantom patterns.
- **Embedding / semantic cosine.** Rejected by the constitution
  (no embeddings / vector / semantic search). TF-IDF is a
  deterministic bag-of-words computation, not a learned embedding,
  and is already an approved dependency (`scikit-learn`).
- **Bipartite optimal (Hungarian) matching.** Rejected: greedy
  deterministic ordering is simpler, replay-stable, and the
  difference versus optimal is immaterial at meeting scale; optimal
  matching would add a non-obvious dependency on tie-break order.

---

## 4. Versioning

Any change to the predicate thresholds, the IoU definition, the
tie-break ordering, or the vectorizer configuration is a **breaking**
change and MUST bump `alignment_contract_version` (semver) on the
line in section header. An `extraction_alignment_comparison` artifact
records the version it was computed under so a later reader can tell
whether two comparisons are commensurable.
