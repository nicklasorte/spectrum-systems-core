# Red Team Review #2 — Index and Query

Document ID: SSC-REDTEAM-002
Scope: SSC-011 cross-meeting index, SSC-012 deterministic query.
Reviewer stance: skeptical reader hunting for false positives, hidden
semantic creep, and silent failure modes.

---

## Method

Walk the index → query path with adversarial inputs. Look for cases where
matches are returned that should not be, cases where a match is missed, and
cases where the layer secretly grew a model dependency.

---

## Findings

### must_fix

**KW1. Keyword search over `payload_text` matches JSON syntax, not just text.**
`query._keyword_matches` falls back to `json.dumps(payload, ...)` and
substrings against that. The dumped string contains JSON syntax: field
names, brackets, quotes, escape sequences. Keyword `true` matches any
boolean field. Keyword `grounding` matches every grounded artifact via
the field name. Keyword `meeting_id` matches every record. These are not
real text matches — they are structural matches against the encoding. The
query layer is silently lying about what it found.
*Fix*: collect string leaves from the payload and search those, not the
JSON envelope.

### should_fix

**DT1. Date filter inputs are not validated.**
`query` accepts any string for `date_from`/`date_to` and lexicographically
compares against the index `date` field (which is YYYY-MM-DD). A caller
passing `"2026/05/09"` or `"May 9 2026"` gets silently-wrong results
because string ordering disagrees with calendar ordering for those
formats. Fail closed on bad input rather than return wrong answers.
*Fix*: validate `date_from` and `date_to` match the contract format
`YYYY-MM-DD`; raise `QueryError` otherwise.

**IDX1. Index can be silently stale.**
`query` reads `indexes/meetings/artifact_index.jsonl` directly. If the
file is missing or out of date relative to `processed/`, the query
returns an answer that reflects an old world. A new engineer running
`query()` after `run_transcript_pipeline()` and seeing zero results
cannot tell that the cause is "you forgot to rebuild the index".
*Fix*: when the index file does not exist, build it on demand inside
`query`. Document that mutations to `processed/` require a manual rebuild
because directory state isn't watched.

### defer_with_reason

**KW2. Substring keyword has no word boundary.**
`fcc` matches `fcco`. Word-boundary matching adds Unicode tokenization
rules that the constitution would call ceremony before need. The current
behavior is documented and predictable. Reason: simple beats clever
until the first real failure that boundary matching would have caught.

**SEM. Semantic search creep.**
None observed. No vector store, no embeddings, no model client. The
`payload_text` fallback is plain `lower()` substring. Reason: the
constitution's "no semantic search until deterministic index/query
works" rule is currently respected; flagged so it stays that way.

**ECP. Grounding eval does not require excerpt identity to a transcript line.**
The eval checks substring containment. A pathological excerpt of one or
two characters would pass. The current extractors only produce full
transcript lines, so this is not a real failure today. Reason: tightening
the eval would defend against a producer no one is building. Re-examine
if a future workflow synthesizes excerpts.

---

## Loop integrity check

- Produce → Evaluate → Decide → Promote: untouched by index/query work.
- Index rebuilds deterministically; query is pure function of (index file,
  filters). After fixes, query also fails-closed on malformed date inputs.
- No path through index/query produces an unsupported claim that could be
  attributed to a transcript that does not contain it (grounding eval
  remains the gate).

---

## Verdict

KW1 is a correctness bug pretending to be a feature; fix it. DT1 and IDX1
are explainability fixes that prevent a confused new engineer. KW2/SEM/ECP
are noted boundaries, not bugs. No model or vector dependency has crept
in.
