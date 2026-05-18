# Harness Mutation Allowlist

Document ID: SSC-CONTRACT-AA-001
Status: Binding for Phase AA onward
Scope: What the Meta-Harness proposer (Phase AA.4) may modify.

This contract is binding. The system constitution
(`docs/architecture/system_constitution.md`) and `CLAUDE.md` take
precedence on any conflict.

---

## 1. Purpose

The Meta-Harness outer loop (Phase AA) lets a proposer agent suggest
*code* changes to the extraction harness, not just prompt changes. A
code-change proposal is only ever applied behind the governed
code-candidate evaluator + auto-PR gate, and only if the diff touches
**exclusively** the files this contract allows.

The allowlist is the machine-readable source of truth. The validator
(`harness/harness_mutation_validator.py`) parses the YAML block below at
runtime — it is **not** hardcoded in Python. If this file is missing or
unparseable the validator fails closed (`allowlist_unavailable`); it
never allows a diff by default.

A diff is valid only when **every** touched path is in `allowed_paths`
and **no** touched path matches any `forbidden_path_patterns`. One
forbidden path rejects the entire diff.

---

## 2. Allowlist (machine-readable)

```yaml
harness_allowlist:
  version: "1.0.0"
  allowed_paths:
    - "src/spectrum_systems_core/extraction/typed_extraction_runner.py"
    - "src/spectrum_systems_core/extraction/chunker.py"
    - "src/spectrum_systems_core/context/bundle_builder.py"
    - "src/spectrum_systems_core/workflows/prompts/"
  forbidden_path_patterns:
    - "docs/contracts/"
    - "docs/architecture/"
    - "CLAUDE.md"
    - "contracts/schemas/"
    - "control/"
    - "evals/runner.py"
    - ".github/"
    - "harness/proposer.py"
    - "harness/pareto_frontier.py"
  rationale: >
    The allowlist governs what the Meta-Harness proposer may modify.
    Governance files, schemas, control logic, and the proposer itself
    are read-only to prevent autonomous self-modification of the
    governed system's rules.
```

---

## 3. Matching rules (binding)

The validator anchors every comparison to a leading `/` so a fragment
only matches on a path-segment boundary (e.g. `control/` matches
`src/spectrum_systems_core/control/decision.py` but not a file literally
named `mycontrol/...`).

- A path is **forbidden** if any `forbidden_path_patterns` fragment
  occurs as a path segment within it. Forbidden is checked first.
- A path is **allowed** only if it is not forbidden AND it is either an
  exact match for an `allowed_paths` file entry, or it lives under an
  `allowed_paths` directory entry (one ending in `/`).
- A path that is neither forbidden nor allowed is **rejected**
  (`not_in_allowlist`). The validator never allows by default.

Any change to `allowed_paths` or `forbidden_path_patterns` is a change
to the governed system's mutation surface and requires an explicit,
human-reviewed PR. The proposer can never edit this file: `docs/`
is forbidden, and the proposer never self-validates (AA.4).
