"""Phase F: report and keynote synthesis.

Loop: structured retrieval -> context bundle -> Sonnet generation ->
grounding eval -> human review.

Promoted artifacts only enter context bundles. Generated reports must
cite real artifact_ids. Sonnet cost per run is tracked. No autonomous
output: every run requires human review before approval.
"""
from .bundle_assembler import (
    MAX_BUNDLE_TOKENS,
    PROMOTED_STATUSES,
    VALID_AUDIENCES,
    VALID_PURPOSES,
    BundleAssembler,
)
from .bundle_eval import MIN_BUNDLE_ITEMS, BundleEval
from .cost_recorder import (
    MAX_SYNTHESIS_COST_USD,
    append_cost_record,
    estimate_cost_usd,
    read_cost_records,
    total_cost_usd,
)
from .data_lake_check import DataLakeChecker
from .grounding_eval import GroundingEval
from .keynote_eval import KeynoteEval
from .keynote_generator import KeynoteGenerator
from .report_generator import SECTION_TYPES, ReportGenerator
from .retrieval_registry import BUILT_IN_RECIPES, RetrievalRegistry
from .run_manifest import RunManifest
from .story_matrix import AUDIENCE_WEIGHT, StoryMatrix
from .synthesis_review_gateway import SynthesisReviewGateway
from .theme_synthesizer import ThemeSynthesizer

__all__ = [
    "AUDIENCE_WEIGHT",
    "BUILT_IN_RECIPES",
    "BundleAssembler",
    "BundleEval",
    "DataLakeChecker",
    "GroundingEval",
    "KeynoteEval",
    "KeynoteGenerator",
    "MAX_BUNDLE_TOKENS",
    "MAX_SYNTHESIS_COST_USD",
    "MIN_BUNDLE_ITEMS",
    "PROMOTED_STATUSES",
    "ReportGenerator",
    "RetrievalRegistry",
    "RunManifest",
    "SECTION_TYPES",
    "StoryMatrix",
    "SynthesisReviewGateway",
    "ThemeSynthesizer",
    "VALID_AUDIENCES",
    "VALID_PURPOSES",
    "append_cost_record",
    "estimate_cost_usd",
    "read_cost_records",
    "total_cost_usd",
]
