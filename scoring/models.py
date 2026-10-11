"""Provider-facing types of the Resume Quality Review module.

A provider (an ATS keyword scanner, a manual reviewer, a mock) scores one resume against one
job description. The engine never needs to know how: it only calls `submit()` once and then
`collect()` until the report is ready, and it treats every outcome it cannot prove as
ambiguous (see scoring/engine.py).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Protocol, runtime_checkable


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def input_fingerprint(resume_text: str, job_description: str) -> str:
    """Identity of what is being scored. A provider echoes it in its report; the engine refuses
    a report whose fingerprint differs (it would score some other resume / JD)."""
    return sha256_text(json.dumps([resume_text, job_description], ensure_ascii=False))


@dataclass(frozen=True)
class ResumeDocument:
    text: str
    format: str = "text"   # plain-text resume (Resume V1)

    @property
    def sha256(self) -> str:
        return sha256_text(self.text)


@dataclass(frozen=True)
class SubmissionReceipt:
    """Proof that the provider accepted exactly one submission."""
    submission_id: str
    idempotency_key: str
    input_fingerprint: str
    submitted_at: float                    # epoch seconds, provider or engine clock
    previous_report_id: str | None = None  # newest report that existed before this submission
    provider: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "SubmissionReceipt":
        return cls(**data)


@dataclass(frozen=True)
class SkillMatch:
    skill: str
    resume_count: int
    jd_count: int


def _as_match(item, matched: bool) -> SkillMatch:
    if isinstance(item, SkillMatch):
        return item
    if isinstance(item, dict):
        return SkillMatch(item["skill"], int(item["resume_count"]), int(item["jd_count"]))
    return SkillMatch(str(item), 1 if matched else 0, 1)


@dataclass(frozen=True)
class QualityReport:
    """Structured feedback for one (resume, JD) pair.

    `matched_skills` / `missing_keywords` are hard skills; the soft-skill lists are optional.
    Plain strings are accepted and become counts (1, 1) for matched and (0, 1) for missing.
    """
    score: int | None
    matched_skills: tuple = ()
    missing_keywords: tuple = ()
    matched_soft_skills: tuple = ()
    missing_soft_skills: tuple = ()
    report_id: str = ""
    generated_at: float = 0.0
    input_fingerprint: str = ""
    provider: str = ""

    def __post_init__(self):
        for name, matched in (("matched_skills", True), ("missing_keywords", False),
                              ("matched_soft_skills", True), ("missing_soft_skills", False)):
            object.__setattr__(self, name, tuple(_as_match(x, matched) for x in getattr(self, name)))

    def to_feedback(self) -> dict:
        """The `quality_feedback` block Resume V2 consumes (validated by validate_feedback)."""
        block = lambda items: [asdict(m) for m in items]
        return {"hard_skills": {"matched": block(self.matched_skills), "missing": block(self.missing_keywords)},
                "soft_skills": {"matched": block(self.matched_soft_skills),
                                "missing": block(self.missing_soft_skills)}}

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in ("matched_skills", "missing_keywords", "matched_soft_skills", "missing_soft_skills"):
            d[k] = [asdict(m) for m in getattr(self, k)]
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "QualityReport":
        return cls(**data)

    @property
    def fingerprint(self) -> str:
        """Content hash of the report (what gets committed); stable across runs."""
        return sha256_text(json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False))


# ---------------------------------------------------------------- provider errors

class ProviderError(Exception):
    """Base class. Anything else a provider raises is treated as an unknown outcome."""


class ProviderUnavailable(ProviderError):
    """Raised BEFORE anything was submitted: quota exhausted, outage, not signed in.
    The whole review run stops and waits for an operator."""

    def __init__(self, message: str, code: str = "PROVIDER_UNAVAILABLE"):
        super().__init__(message)
        self.code = code


class SubmissionRejected(ProviderError):
    """The provider definitely did not accept the submission (validation error, 4xx).
    Proven not submitted, so a later run may retry."""


class ResultNotReady(ProviderError):
    """collect(): the submission exists but its report is not ready yet."""


# ---------------------------------------------------------------- provider protocol

@runtime_checkable
class ResumeScorer(Protocol):
    name: str

    def submit(self, resume: ResumeDocument, job_description: str, *,
               idempotency_key: str) -> SubmissionReceipt: ...

    def collect(self, receipt: SubmissionReceipt) -> QualityReport: ...


@runtime_checkable
class ReconcilableScorer(ResumeScorer, Protocol):
    """Optional: a provider that can look a submission up by idempotency key. Lets the engine
    resolve 'submitted or not?' after a crash or lost connection without resubmitting."""

    def lookup(self, idempotency_key: str) -> SubmissionReceipt | None: ...


@dataclass
class ReviewJob:
    job_id: str
    resume: ResumeDocument
    job_description: str
    label: str = ""
    meta: dict = field(default_factory=dict)

    @property
    def input_fingerprint(self) -> str:
        return input_fingerprint(self.resume.text, self.job_description)
