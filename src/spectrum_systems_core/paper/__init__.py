"""Phase D: working paper + comment intelligence.

Loop: source text -> technical_claim -> evidence_record -> issue_record ->
revision_instruction -> revision_diff -> revised_draft.
"""
from .assumption_extractor import AssumptionExtractor
from .claim_eval import ClaimEval
from .claim_extractor import ClaimExtractor
from .comment_processor import CommentProcessor
from .contradiction_detector import ContradictionDetector
from .evidence_builder import EvidenceBuilder
from .evidence_eval import EvidenceEval
from .issue_eval import IssueEval
from .issue_registry import IssueRegistry
from .publication_formatter import PublicationFormatter
from .revision_eval import RevisionEval
from .revision_generator import RevisionGenerator
from .revision_workflow import RevisionWorkflow

__all__ = [
    "AssumptionExtractor",
    "ClaimEval",
    "ClaimExtractor",
    "CommentProcessor",
    "ContradictionDetector",
    "EvidenceBuilder",
    "EvidenceEval",
    "IssueEval",
    "IssueRegistry",
    "PublicationFormatter",
    "RevisionEval",
    "RevisionGenerator",
    "RevisionWorkflow",
]
