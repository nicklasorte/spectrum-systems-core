"""Seed the glossary with 40+ spectrum-domain terms.

Writes two complementary glossary artifact layouts under
``<SDL_ROOT>/glossary/`` (or ``--out`` if provided):

  1. One legacy ``glossary_term`` artifact per term at
     ``<out>/<slug>.json`` (consumed by the legacy GlossaryManager).
  2. The versioned aggregate at ``<out>/spectrum_glossary_v1.json``
     (consumed by ``typed_extraction_runner._resolve_versioned_glossary_artifact``
     for per-chunk injection). The aggregate is the file the wiring
     signal ``glossary_terms_injected_present`` indirectly depends on:
     when this file is missing, the runner loads an empty term list,
     ``total_term_injections == 0``, and the wiring signal flips to
     MISSING. Seeding both shapes from the same source list keeps the
     two consumers in sync by construction.

The output is deterministic: the same term name produces the same
``glossary_term_id`` (a sha1 over the term text, formatted as a UUID).

This is a one-time seed. Re-running it overwrites existing files in
place. Add new terms by appending to ``_TERMS`` below.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List, Tuple


_DETERMINISTIC_CREATED_AT = "1970-01-01T00:00:00+00:00"


def _stable_uuid(term: str) -> str:
    h = hashlib.sha1(term.encode("utf-8")).digest()
    return str(uuid.UUID(bytes=h[:16]))


def _slug(term: str) -> str:
    s = term.lower()
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return s or "term"


# Each entry: (term, definition, authoritative_source, related_terms,
#               is_regulatory_verb, canonical_verb_definition_or_None)
_TERMS: List[Tuple[str, str, str, List[str], bool, Any]] = [
    # --- Technical terms (services / actors) ---
    (
        "FSS",
        "Fixed Satellite Service: satellite communications service in which "
        "Earth stations at fixed points communicate via satellite.",
        "ITU", ["MSS", "FSS protection zone"], False, None,
    ),
    (
        "MSS",
        "Mobile Satellite Service: satellite service for mobile Earth stations.",
        "ITU", ["FSS"], False, None,
    ),
    (
        "P2P",
        "Point-to-Point: fixed terrestrial microwave links between two fixed points.",
        "FCC", [], False, None,
    ),
    (
        "COA",
        "Course of Action: a proposed approach for spectrum transition, with "
        "analysis of feasibility and impact.",
        "NTIA", ["study plan"], False, None,
    ),
    (
        "TIG",
        "Technical Interchange Group: a working group of federal agency "
        "representatives tasked with technical analysis for a specific line of effort.",
        "NTIA", ["WG", "working paper"], False, None,
    ),
    (
        "WG",
        "Working Group: the parent body coordinating multiple TIGs in the 7 GHz study.",
        "NTIA", ["TIG"], False, None,
    ),
    (
        "ITU",
        "International Telecommunication Union: UN agency coordinating global "
        "spectrum use.",
        "ITU", [], False, None,
    ),
    (
        "FCC",
        "Federal Communications Commission: US spectrum regulator for commercial use.",
        "FCC", [], False, None,
    ),
    (
        "NTIA",
        "National Telecommunications and Information Administration: US spectrum "
        "manager for federal government use.",
        "NTIA", [], False, None,
    ),
    (
        "DoD",
        "Department of Defense: incumbent operator of federal spectrum in 7 GHz band.",
        "DoD", [], False, None,
    ),
    (
        "DWCIO",
        "Defense Wireless Communications and Interoperability Office.",
        "DoD", [], False, None,
    ),
    (
        "AFSMO",
        "Air Force Spectrum Management Office.",
        "DoD", [], False, None,
    ),
    (
        "USSPACECOM",
        "United States Space Command.",
        "DoD", [], False, None,
    ),
    # --- Technical terms (protection / propagation) ---
    (
        "FSS protection zone",
        "Geographic area where new spectrum entrants must not cause harmful "
        "interference to FSS Earth stations.",
        "other", ["FSS", "protection criterion"], False, None,
    ),
    (
        "protection criterion",
        "Interference threshold that must not be exceeded. In 7 GHz downlink "
        "study: -10.5 dB at 80th percentile AND -6 dB at 0.03%.",
        "ITU", ["ITU two-point criterion"], False, None,
    ),
    (
        "ITU two-point criterion",
        "Protection standard requiring two separate exceedance thresholds: "
        "-10.5 dB not exceeded more than 20% of time AND -6 dB not exceeded "
        "more than 0.03%.",
        "ITU", ["protection criterion"], False, None,
    ),
    (
        "study plan",
        "Governing document for the 7 GHz spectrum study defining scope, "
        "methodology, and schedule.",
        "NTIA", ["COA", "working paper"], False, None,
    ),
    (
        "working paper",
        "Technical document submitted by TIG participants for group review and "
        "comment. Analogous to a draft technical report.",
        "NTIA", ["TIG", "comment matrix"], False, None,
    ),
    (
        "comment matrix",
        "Structured spreadsheet for submitting line-by-line comments on working "
        "papers.",
        "NTIA", ["working paper"], False, None,
    ),
    (
        "ERP",
        "Effective Radiated Power: total power radiated by a transmitter.",
        "other", [], False, None,
    ),
    (
        "I/N",
        "Interference-to-Noise ratio: key metric for evaluating interference impact.",
        "ITU", ["protection criterion"], False, None,
    ),
    (
        "CDF",
        "Cumulative Distribution Function: statistical function used in ITU "
        "protection criteria.",
        "ITU", ["ITU two-point criterion"], False, None,
    ),
    (
        "ITM",
        "Irregular Terrain Model: propagation model used in interference analysis.",
        "other", [], False, None,
    ),
    (
        "aggregate interference",
        "Total interference from all sources combined, as opposed to single-source "
        "interference.",
        "ITU", ["protection criterion"], False, None,
    ),
    (
        "adjacent channel",
        "Frequency channel immediately next to the channel of interest; subject "
        "to out-of-band emission analysis.",
        "other", ["guard band"], False, None,
    ),
    (
        "guard band",
        "Frequency separation required between different spectrum users.",
        "other", ["adjacent channel"], False, None,
    ),
    (
        "relocation",
        "Movement of a spectrum user from one frequency band to another.",
        "NTIA", ["transition plan"], False, None,
    ),
    (
        "primary status",
        "Highest protection status in spectrum allocation; cannot be required to "
        "accept interference from secondary users.",
        "ITU", ["secondary status"], False, None,
    ),
    (
        "secondary status",
        "Lower protection status; must accept interference from primary users "
        "and cannot cause harmful interference to primary users.",
        "ITU", ["primary status"], False, None,
    ),
    (
        "transition plan",
        "Agency plan for moving operations out of a spectrum band being reallocated.",
        "NTIA", ["relocation"], False, None,
    ),
    (
        "Kiteworks",
        "Secure file sharing system used by NTIA for distributing sensitive "
        "documents.",
        "NTIA", [], False, None,
    ),
    # --- Regulatory verbs (is_regulatory_verb = True) ---
    (
        "approved",
        "Group reached explicit agreement that a proposal is accepted.",
        "CLAUDE.md", ["rejected", "deferred"], True,
        "Explicit affirmative decision by the group with no recorded objections. "
        "Stronger than 'noted' or 'considered'.",
    ),
    (
        "rejected",
        "Group explicitly declined a proposal.",
        "CLAUDE.md", ["approved", "deferred"], True,
        "Explicit negative decision by the group. The proposal as presented is "
        "not accepted; revisiting requires a new proposal.",
    ),
    (
        "deferred",
        "Decision postponed to a future meeting.",
        "CLAUDE.md", ["approved", "rejected"], True,
        "Postponed pending additional information or analysis. Not approved or "
        "rejected.",
    ),
    (
        "noted",
        "Group acknowledged a statement without taking action.",
        "CLAUDE.md", ["considered"], True,
        "Acknowledgment only. No commitment, no approval, no rejection. Do not "
        "use this verb to imply agreement.",
    ),
    (
        "considered",
        "Group discussed a proposal without reaching a decision.",
        "CLAUDE.md", ["noted", "deferred"], True,
        "Discussion occurred. No decision made. Do not conflate with 'approved'.",
    ),
    (
        "action_required",
        "Explicit task assigned to a named owner with implied deadline.",
        "CLAUDE.md", ["open_question"], True,
        "An action item assigned to a specific named owner. Owner is required; "
        "deadline may be implied by meeting cadence if not stated.",
    ),
    (
        "open_question",
        "Question raised but not resolved in the meeting.",
        "CLAUDE.md", ["to_be_determined"], True,
        "An unresolved question. Distinct from action_required because no owner "
        "is named.",
    ),
    (
        "to_be_determined",
        "Specific value or decision explicitly deferred for later resolution.",
        "CLAUDE.md", ["deferred", "open_question"], True,
        "A specific data point or sub-decision marked 'TBD'. Narrower than "
        "'deferred' which applies to whole proposals.",
    ),
    (
        "agreed",
        "Group reached consensus.",
        "CLAUDE.md", ["approved", "consensus"], True,
        "Synonym for 'approved' when used in NTIA/DoD meeting context.",
    ),
    (
        "consensus",
        "All participants accepted the outcome without objection.",
        "CLAUDE.md", ["agreed", "approved"], True,
        "Strongest form of group acceptance. Implies no recorded objections.",
    ),
]


def _build_artifact(
    term: str,
    definition: str,
    authoritative_source: str,
    related_terms: List[str],
    is_regulatory_verb: bool,
    canonical_verb_definition: Any,
    now: str,
) -> Dict[str, Any]:
    return {
        "glossary_term_id": _stable_uuid(term),
        "term": term,
        "definition": definition,
        "authoritative_source": authoritative_source,
        "related_terms": list(related_terms),
        "is_regulatory_verb": is_regulatory_verb,
        "canonical_verb_definition": canonical_verb_definition,
        "artifact_type": "glossary_term",
        "schema_version": "1.0.0",
        "created_at": now,
        "provenance": {"produced_by": "GlossaryManager"},
    }


_VERSIONED_GLOSSARY_FILENAME: str = "spectrum_glossary_v1.json"
_VERSIONED_GLOSSARY_VERSION: str = "1"


def _build_versioned_term(
    term: str,
    definition: str,
    authoritative_source: str,
    related_terms: List[str],
) -> Dict[str, Any]:
    short = definition[:200]
    related_ids = [_stable_uuid(rt) for rt in related_terms]
    return {
        "term_id": _stable_uuid(term),
        "term": term,
        "abbreviation": term if term.isupper() and len(term) <= 8 else None,
        "definition": definition,
        "short_definition": short,
        "authoritative_source": authoritative_source or "unknown",
        "domain_scope": "spectrum",
        "related_term_ids": related_ids,
    }


def _compute_glossary_content_hash(
    glossary_version: str, terms: List[Dict[str, Any]]
) -> str:
    payload = {"glossary_version": glossary_version, "terms": terms}
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _build_versioned_aggregate(now: str) -> Dict[str, Any]:
    """Build the spectrum_glossary_v1 aggregate from the _TERMS table.

    The aggregate must validate against
    ``spectrum_glossary.schema.json``. The runner's
    ``find_matching_terms`` reads ``term`` and ``abbreviation`` for
    lexical match, so both fields must be populated correctly.
    """
    terms_built: List[Dict[str, Any]] = []
    for entry in _TERMS:
        term, definition, source, related, _is_verb, _verb_def = entry
        terms_built.append(
            _build_versioned_term(term, definition, source, related)
        )
    return {
        "artifact_type": "spectrum_glossary",
        "schema_version": "1.0.0",
        "glossary_version": _VERSIONED_GLOSSARY_VERSION,
        "term_count": len(terms_built),
        "content_hash": _compute_glossary_content_hash(
            _VERSIONED_GLOSSARY_VERSION, terms_built,
        ),
        "created_at": now,
        "terms": terms_built,
    }


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description="Seed the spectrum glossary.")
    parser.add_argument(
        "--out",
        default=None,
        help="Output directory. Defaults to <SDL_ROOT>/glossary if SDL_ROOT is set, "
             "else ./glossary.",
    )
    parser.add_argument(
        "--deterministic",
        action="store_true",
        default=True,
        help="Use a fixed created_at (1970-01-01) for byte-identical output. "
             "Default true; pass --no-deterministic to use wall-clock time.",
    )
    parser.add_argument(
        "--no-deterministic", dest="deterministic", action="store_false",
    )
    args = parser.parse_args(argv)

    if args.out:
        out_root = Path(args.out)
    else:
        sdl = os.environ.get("SDL_ROOT", "").strip()
        out_root = Path(sdl) / "glossary" if sdl else Path("glossary")
    out_root.mkdir(parents=True, exist_ok=True)

    if args.deterministic:
        now = _DETERMINISTIC_CREATED_AT
    else:
        now = (
            datetime.datetime.now(datetime.timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%S+00:00")
        )

    written = 0
    for entry in _TERMS:
        term, definition, source, related, is_verb, verb_def = entry
        artifact = _build_artifact(
            term, definition, source, related, is_verb, verb_def, now,
        )
        path = out_root / f"{_slug(term)}.json"
        path.write_text(
            json.dumps(artifact, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        written += 1

    # Versioned aggregate: the file the typed_extraction_runner actually
    # loads. Without it, per-chunk injection produces zero matches and
    # the ``glossary_terms_injected_present`` wiring signal flips to
    # MISSING. Built deterministically from the same _TERMS table so
    # the two outputs cannot drift.
    aggregate = _build_versioned_aggregate(now)
    aggregate_path = out_root / _VERSIONED_GLOSSARY_FILENAME
    aggregate_path.write_text(
        json.dumps(aggregate, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    print(
        f"seed_glossary: wrote {written} per-term files + "
        f"{_VERSIONED_GLOSSARY_FILENAME} ({aggregate['term_count']} terms) "
        f"to {out_root}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
