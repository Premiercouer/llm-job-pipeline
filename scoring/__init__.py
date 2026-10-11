"""Resume Quality Review: provider-agnostic, crash-safe external scoring of resumes.

    from scoring import ReviewEngine, MockResumeScorer, ReviewJob, ResumeDocument

See scoring/engine.py for the safety rules and scoring/states.py for the state machine.
"""
from scoring.engine import ReviewEngine, ReviewResult, ResultSink, validate_feedback
from scoring.mock import MockResumeScorer
from scoring.models import (ProviderUnavailable, QualityReport, ReconcilableScorer, ResultNotReady,
                           ResumeDocument, ResumeScorer, ReviewJob, SubmissionReceipt,
                           SubmissionRejected)

__all__ = ["ReviewEngine", "ReviewResult", "ResultSink", "validate_feedback", "MockResumeScorer",
           "ProviderUnavailable", "QualityReport", "ReconcilableScorer", "ResultNotReady",
           "ResumeDocument", "ResumeScorer", "ReviewJob", "SubmissionReceipt", "SubmissionRejected"]
