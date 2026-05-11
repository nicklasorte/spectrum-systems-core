"""Phase M.4 ground-truth eval framework.

Modules:
  aligner          -- EvalAligner (two-stage semantic + lexical alignment)
  metrics          -- EvalMetrics (coverage / precision / items_requiring_review)
  regression_gate  -- RegressionGate (per-pair threshold check vs baseline)
  runner           -- EvalRunner (orchestrates load -> align -> metrics -> gate -> summary)
  few_shot         -- FewShotLoader (version-gated hand-authored seed loader)

The M.4 framework lives under ``evals/m4`` to keep it isolated from the
core required-field evals in ``evals/runner.py``. Cores's runtime loop
should never depend on this module; this module reads core artifacts.
"""
from .aligner import EvalAligner
from .metrics import EvalMetrics
from .regression_gate import RegressionGate
from .few_shot import FewShotLoader, load_few_shot_examples
from .runner import EvalRunner

__all__ = [
    "EvalAligner",
    "EvalMetrics",
    "RegressionGate",
    "FewShotLoader",
    "load_few_shot_examples",
    "EvalRunner",
]
