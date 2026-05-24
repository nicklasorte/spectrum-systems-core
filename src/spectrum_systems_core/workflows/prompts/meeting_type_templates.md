---
version: 5.C
description: >-
  Phase 5 Variant C — meeting-type templates injected as a prompt
  preamble. Each section gives the model an expected extraction profile
  derived from the 24 paired-meeting corpus, so it has a numeric prior
  on volume per type and a list of "do NOT expect" types that are
  predictable absences for that meeting category.

  The expected counts are calibrated to keep precision the primary
  beneficiary: each type's max expected count is bounded below 2x the
  Opus baseline for the same type, so a runaway Haiku extraction has a
  numerical ceiling it must justify against. See
  tests/test_meeting_type_templates.py::test_template_expected_counts_are_realistic
  for the regression contract.

  Section markers (TEMPLATE_BEGIN/END:<meeting_type>) bound each
  template body so the loader can slice the file deterministically.
---

<!-- TEMPLATE_BEGIN:downlink_tig -->
## Meeting Type: Downlink TIG Kickoff

Expected extraction profile based on the kickoff meetings in the
paired corpus:

- decisions: 1-3 (scope, geographic coverage, methodology framework)
- action_items: 3-6 (system validation, charter review, data submission)
- open_questions: 1-3 (scope ambiguities, classification handling)
- procedural_ruling: 2-4 (meeting format, pre-decisional disclaimer)
- attendees: 20-35
- topics: 4-6 (NOT 20+; match agenda items only)
- technical_parameters: 1-3 (frequency band references only)
- regulatory_references: 1-2
- named_artifacts: 4-8 (study plan, charters, working papers)
- scheduled_events: 3-6

Do NOT expect: precedent_reference (too early in the study),
issue_registry_entry (issue tracking not yet started),
position_statement (positions not yet contested), dissent_or_objection
(no contested votes at kickoff).
<!-- TEMPLATE_END:downlink_tig -->

<!-- TEMPLATE_BEGIN:uplink_tig -->
## Meeting Type: Uplink TIG Kickoff

Expected extraction profile mirrors downlink_tig kickoffs (the two
streams run in parallel):

- decisions: 1-3 (scope, geographic coverage, methodology framework)
- action_items: 3-6 (system validation, charter review, data submission)
- open_questions: 1-3 (scope ambiguities)
- procedural_ruling: 2-4 (meeting format, pre-decisional disclaimer)
- attendees: 20-35
- topics: 4-6 (NOT 20+; match agenda items only)
- technical_parameters: 1-3
- regulatory_references: 1-2
- named_artifacts: 4-8
- scheduled_events: 3-6

Do NOT expect: precedent_reference, issue_registry_entry,
position_statement, dissent_or_objection.
<!-- TEMPLATE_END:uplink_tig -->

<!-- TEMPLATE_BEGIN:p2p_tig -->
## Meeting Type: P2P TIG

Expected extraction profile for point-to-point TIG meetings — these
are more technical than kickoff meetings because the methodology is
the primary discussion topic:

- decisions: 2-5 (repacking methodology, frequency planning approach)
- action_items: 5-10 (GMF validation, paired link analysis,
  link-by-link review)
- technical_parameters: 3-8 (frequency ranges, link counts, paired
  band widths)
- regulatory_references: 2-4 (IQLink, GMF, ITU references)
- claims: 3-8 (methodology assertions)
- risks: 1-3 (schedule, data fidelity)
- open_questions: 2-5
- attendees: 15-30
- topics: 4-8

Do NOT expect: dissent_or_objection (working-level meetings rarely
record formal dissent), external_stakeholder_input (industry
participants generally absent at this layer).
<!-- TEMPLATE_END:p2p_tig -->

<!-- TEMPLATE_BEGIN:working_group -->
## Meeting Type: Working Group

Expected extraction profile for working-group meetings — broader scope
than TIG meetings, more cross-agency coordination:

- decisions: 3-8 (charter finalization, methodology choices)
- action_items: 5-12 (cross-agency coordination, data submission)
- open_questions: 3-7 (technical methodology disputes)
- risks: 2-5 (schedule compression, data gaps)
- cross_references: 3-6 (prior WG decisions, presidential memo)
- topics: 5-10
- attendees: 25-40
- technical_parameters: 2-6
- regulatory_references: 2-5
- named_artifacts: 5-10
- scheduled_events: 2-5
- position_statement: 1-4 (agencies starting to stake positions)

Expect some issue_registry_entry items (1-3) once the WG is past
kickoff. precedent_reference may appear (0-2) if older WG decisions
are cited.
<!-- TEMPLATE_END:working_group -->

<!-- TEMPLATE_BEGIN:downlink_tig_working -->
## Meeting Type: Downlink TIG Working (post-kickoff)

Expected extraction profile for downlink TIG working sessions — once
kickoff is past, the discussion shifts to analysis-of-the-week:

- decisions: 2-5 (methodology refinements, exclusion rules)
- action_items: 4-10 (analysis runs, data updates)
- claims: 2-6 (interpretation of new data)
- technical_parameters: 3-8
- open_questions: 2-5
- risks: 1-4
- issue_registry_entry: 1-4 (issues now being tracked)
- attendees: 15-25
- topics: 4-8

Do NOT expect: dissent_or_objection unless contested votes are recorded.
<!-- TEMPLATE_END:downlink_tig_working -->

<!-- TEMPLATE_BEGIN:uplink_tig_working -->
## Meeting Type: Uplink TIG Working (post-kickoff)

Mirrors downlink_tig_working:

- decisions: 2-5
- action_items: 4-10
- claims: 2-6
- technical_parameters: 3-8
- open_questions: 2-5
- risks: 1-4
- issue_registry_entry: 1-4
- attendees: 15-25
- topics: 4-8
<!-- TEMPLATE_END:uplink_tig_working -->

<!-- TEMPLATE_BEGIN:adjudication -->
## Meeting Type: Adjudication / Comment Review

Expected extraction profile for comment-adjudication sessions —
heavily document-driven, formal dispositions per filing:

- decisions: 5-15 (one per comment / per filing being adjudicated)
- action_items: 3-8
- cross_references: 5-15 (commenters, prior filings)
- regulatory_references: 3-8
- external_stakeholder_input: 5-15 (the comments themselves)
- topics: 3-6
- attendees: 10-20
- procedural_ruling: 3-8 (adjudication rules, dissent recording)

Expect dissent_or_objection (1-5) when contested adjudications appear.
<!-- TEMPLATE_END:adjudication -->

<!-- TEMPLATE_BEGIN:unknown -->
## Meeting Type: Unknown

The meeting type could not be inferred from the source identifier or
title. No expected-volume prior is supplied; use the standard prompt
rules without a meeting-type-specific ceiling.
<!-- TEMPLATE_END:unknown -->
