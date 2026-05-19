"""Verbatim span grounding for promoted artifacts.

The grounding gate verifies that every extracted item is byte-anchored
to the source transcript before an artifact is promoted. See
``promotion/gate.py`` for the gate logic and ``normalize.py`` for the
deterministic normalization function used by the byte-match comparison.
"""
from .normalize import normalize_transcript, NormalizedTranscript

__all__ = ["normalize_transcript", "NormalizedTranscript"]
