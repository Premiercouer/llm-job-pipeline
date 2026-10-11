"""Resume Quality Review engine: drives one job through the state machine with any provider.

Safety rules (each one is covered by scoring/tests):

1. Intent before action: SUBMISSION_INTENT_RECORDED is fsynced before `submit()` is called.
2. No blind resubmission: once an intent exists, the engine never calls `submit()` again for
   that job. A lost connection, timeout or unknown error during submit is resolved through
   `lookup()` (if the provider has it) or becomes MANUAL_RECOVERY_REQUIRED.
3. Fresh, matching results only: a report is committed only if it is well-formed, was
   generated after this submission, is not the report that existed before it, and its input
   fingerprint matches what was submitted.
4. Commit, then checkpoint: the report is persisted to the sink first, the journal moves to
   COMMITTED second. A crash in between is repaired by re-committing the journaled report.
5. Reconcile before retry: a run that finds an existing journal continues from the recorded
   state (collect again, validate again, commit again) instead of starting over.
6. Quota / outage stops the run: PROVIDER_UNAVAILABLE ends the job and the caller stops
   starting new jobs; an operator decides what happens next.
"""
from __future__ import annotations

import re
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from scoring import states as S
from scoring.journal import Journal, JournalCorrupt, JournalError
from scoring.models import (ProviderError, ProviderUnavailable, QualityReport, ResultNotReady,
                           ReviewJob, SubmissionReceipt, SubmissionRejected, sha256_text)

# Outcomes reported to the caller (a job outcome, not a journal state).
COMMITTED = "COMMITTED"
ALREADY_COMMITTED = "ALREADY_COMMITTED"
PENDING_RESULT = "PENDING_RESULT"          # submitted, report not ready yet: rerun to collect
SAFE_TO_RETRY = S.SAFE_TO_RETRY
MANUAL_RECOVERY_REQUIRED = S.MANUAL_RECOVERY_REQUIRED
PROVIDER_UNAVAILABLE = S.PROVIDER_UNAVAILABLE
FAILED = "FAILED"                          # local failure (journal / sink write); safe to rerun
OUTCOMES = (COMMITTED, ALREADY_COMMITTED, PENDING_RESULT, SAFE_TO_RETRY,
            MANUAL_RECOVERY_REQUIRED, PROVIDER_UNAVAILABLE, FAILED)


class ResultSink(Protocol):
    """Where validated reports go (e.g. job.json). `commit` must be atomic and idempotent."""

    def committed_fingerprint(self, job_id: str) -> str | None: ...

    def commit(self, job_id: str, report: QualityReport) -> None: ...


@dataclass
class ReviewResult:
    job_id: str
    outcome: str
    state: str | None
    detail: str = ""
    report: QualityReport | None = None
    reconciled: bool = False
    calls: Counter = field(default_factory=Counter)


def validate_feedback(feedback) -> str | None:
    """Schema of the `quality_feedback` block handed to Resume V2. None if valid."""
    if not isinstance(feedback, dict) or set(feedback) != {"hard_skills", "soft_skills"}:
        return "feedback keys must be hard_skills, soft_skills"
    for cat in ("hard_skills", "soft_skills"):
        block = feedback[cat]
        if not isinstance(block, dict) or set(block) != {"matched", "missing"}:
            return f"{cat} must have exactly matched, missing"
        seen: dict[str, str] = {}
        for kind in ("matched", "missing"):
            items = block[kind]
            if not isinstance(items, list):
                return f"{cat}.{kind} must be a list"
            for i, item in enumerate(items):
                where = f"{cat}.{kind}[{i}]"
                if not isinstance(item, dict) or set(item) != {"skill", "resume_count", "jd_count"}:
                    return f"{where} must have exactly skill, resume_count, jd_count"
                if not isinstance(item["skill"], str) or not item["skill"].strip():
                    return f"{where}.skill must be a non-empty string"
                for c in ("resume_count", "jd_count"):
                    v = item[c]
                    if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                        return f"{where}.{c} must be an integer >= 0"
                rc, jc = item["resume_count"], item["jd_count"]
                if kind == "matched" and (rc < 1 or jc < 1):
                    return f"{where} matched needs resume_count >= 1 and jd_count >= 1"
                if kind == "missing" and (rc != 0 or jc < 1):
                    return f"{where} missing needs resume_count == 0 and jd_count >= 1"
                key = " ".join(item["skill"].split()).casefold()
                if key in seen:
                    return f"{cat}: duplicate skill {item['skill']!r} ({seen[key]} and {kind})"
                seen[key] = kind
    return None


def idempotency_key(job_id: str, input_fp: str) -> str:
    """Stable per (job, exact input): the same submission always carries the same key."""
    return sha256_text(f"{job_id}\n{input_fp}")[:32]


def _safe_name(job_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", job_id)[:180] or "job"


class ReviewEngine:
    def __init__(self, scorer, journal_dir: Path, sink: ResultSink, *,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
                 poll_interval: float = 2.0, max_polls: int = 60,
                 max_report_age: float = 3600.0, clock_skew: float = 120.0,
                 hook: Callable[[str, str], None] | None = None):
        self.scorer = scorer
        self.journal_dir = Path(journal_dir)
        self.sink = sink
        self.clock, self.sleep = clock, sleep
        self.poll_interval, self.max_polls = poll_interval, max_polls
        self.max_report_age, self.clock_skew = max_report_age, clock_skew
        self.hook = hook or (lambda point, job_id: None)

    # ------------------------------------------------------------------ public
    def journal_path(self, job_id: str) -> Path:
        return self.journal_dir / f"{_safe_name(job_id)}.json"

    def review(self, job: ReviewJob) -> ReviewResult:
        calls: Counter = Counter()
        res = lambda outcome, j, detail="", report=None, reconciled=False: ReviewResult(
            job.job_id, outcome, j.state if j else None, detail, report, reconciled, calls)
        path = self.journal_path(job.job_id)
        try:
            journal = Journal.load(path)
        except JournalCorrupt as e:
            return ReviewResult(job.job_id, MANUAL_RECOVERY_REQUIRED, None, f"journal corrupt: {e}", calls=calls)

        fp = job.input_fingerprint
        if journal is not None and journal.data["input_fingerprint"] != fp:
            if journal.state in S.POST_INTENT:
                return res(MANUAL_RECOVERY_REQUIRED, journal,
                           f"input changed since {journal.state}; archive the journal to review again")
            journal = None  # nothing was submitted for the old input: start clean
        if journal is None:
            journal = Journal.new(path, job.job_id, idempotency_key(job.job_id, fp), fp)

        try:
            return self._run(job, journal, calls, res)
        except JournalError as e:
            return res(FAILED, journal, str(e))

    # ------------------------------------------------------------------- steps
    def _run(self, job, journal: Journal, calls, res) -> ReviewResult:
        state = journal.state
        reconciled = state not in (S.READY,) and bool(journal.data["history"])

        if state == S.COMMITTED:
            return self._reconcile_committed(job, journal, res)
        if state == S.MANUAL_RECOVERY_REQUIRED:
            return res(MANUAL_RECOVERY_REQUIRED, journal, journal.detail or "blocked")
        if state == S.SUBMISSION_INTENT_RECORDED:
            receipt = self._lookup(journal, calls)
            if receipt is None:
                journal.transition(S.MANUAL_RECOVERY_REQUIRED,
                                   "found an unresolved submission intent; the provider cannot "
                                   "confirm whether it was submitted")
                return res(MANUAL_RECOVERY_REQUIRED, journal, journal.detail, reconciled=True)
            journal.transition(S.SUBMITTED, "reconciled via lookup", receipt=receipt.to_dict())
            state = S.SUBMITTED
        if state in S.STARTABLE:
            outcome = self._submit(job, journal, calls)
            if outcome is not None:
                return res(outcome, journal, journal.detail)
            state = journal.state
        if state == S.SUBMITTED:
            report = self._collect(job, journal, calls)
            if report is None:
                return res(PENDING_RESULT, journal, "report not ready; rerun to collect", reconciled=reconciled)
            state = journal.state
        if state == S.RESULT_DETECTED:
            err = self._validate(journal)
            if err:
                journal.transition(S.MANUAL_RECOVERY_REQUIRED, f"report rejected: {err}")
                return res(MANUAL_RECOVERY_REQUIRED, journal, journal.detail, reconciled=reconciled)
            report = QualityReport.from_dict(journal.data["report"])
            journal.transition(S.RESULT_VALIDATED, "", report_fingerprint=report.fingerprint)
            self.hook("after_validated", job.job_id)
            state = S.RESULT_VALIDATED
        if state == S.RESULT_VALIDATED:
            return self._commit(job, journal, res, reconciled)
        raise AssertionError(f"unhandled state {state}")  # pragma: no cover

    def _submit(self, job: ReviewJob, journal: Journal, calls) -> str | None:
        """Fresh submission. Returns an outcome to stop with, or None to continue (SUBMITTED)."""
        if journal.state != S.INPUT_PREPARED:
            journal.transition(S.INPUT_PREPARED, "", attempt=journal.data["attempt"] + 1)
        self.hook("before_intent", job.job_id)
        journal.transition(S.SUBMISSION_INTENT_RECORDED, "")
        self.hook("after_intent", job.job_id)
        key = journal.data["idempotency_key"]
        try:
            calls["submit"] += 1
            receipt = self.scorer.submit(job.resume, job.job_description, idempotency_key=key)
        except ProviderUnavailable as e:
            journal.transition(S.PROVIDER_UNAVAILABLE, f"{e.code}: {e}")
            return PROVIDER_UNAVAILABLE
        except SubmissionRejected as e:
            journal.transition(S.SAFE_TO_RETRY, f"rejected by provider: {e}")
            return SAFE_TO_RETRY
        except Exception as e:  # noqa: BLE001 - unknown outcome: maybe submitted
            receipt = self._lookup(journal, calls)
            if receipt is None:
                journal.transition(S.MANUAL_RECOVERY_REQUIRED,
                                   f"submission outcome unknown ({type(e).__name__}: {e})")
                return MANUAL_RECOVERY_REQUIRED
            journal.transition(S.SUBMITTED, f"confirmed via lookup after {type(e).__name__}",
                               receipt=receipt.to_dict())
            self.hook("after_submitted", job.job_id)
            return None
        if receipt.idempotency_key != key or receipt.input_fingerprint != journal.data["input_fingerprint"]:
            journal.transition(S.MANUAL_RECOVERY_REQUIRED, "receipt does not match this submission")
            return MANUAL_RECOVERY_REQUIRED
        self.hook("before_record_submitted", job.job_id)
        journal.transition(S.SUBMITTED, "", receipt=receipt.to_dict())
        self.hook("after_submitted", job.job_id)
        return None

    def _lookup(self, journal: Journal, calls) -> SubmissionReceipt | None:
        lookup = getattr(self.scorer, "lookup", None)
        if lookup is None:
            return None
        try:
            calls["lookup"] += 1
            receipt = lookup(journal.data["idempotency_key"])
        except Exception:  # noqa: BLE001 - a failed lookup proves nothing
            return None
        if receipt is None or receipt.input_fingerprint != journal.data["input_fingerprint"]:
            return None
        return receipt

    def _collect(self, job: ReviewJob, journal: Journal, calls) -> QualityReport | None:
        receipt = SubmissionReceipt.from_dict(journal.data["receipt"])
        for i in range(self.max_polls):
            try:
                calls["collect"] += 1
                report = self.scorer.collect(receipt)
            except (ResultNotReady, ProviderError, OSError, TimeoutError):
                if i + 1 < self.max_polls:
                    self.sleep(self.poll_interval)
                continue
            journal.transition(S.RESULT_DETECTED, "", report=report.to_dict())
            self.hook("after_detected", job.job_id)
            return report
        return None

    def _validate(self, journal: Journal) -> str | None:
        try:
            report = QualityReport.from_dict(journal.data["report"])
        except (TypeError, KeyError, ValueError) as e:
            return f"malformed report ({e})"
        err = validate_feedback(report.to_feedback())
        if err:
            return err
        if report.score is not None and not (isinstance(report.score, int) and 0 <= report.score <= 100):
            return f"score out of range: {report.score!r}"
        if journal.data.get("adopted"):
            return None  # operator-supplied report: schema only
        receipt = SubmissionReceipt.from_dict(journal.data["receipt"])
        if report.input_fingerprint != journal.data["input_fingerprint"]:
            return "fingerprint mismatch: the report scored a different resume / JD"
        if not report.report_id or report.report_id == receipt.previous_report_id:
            return "stale report: same report as before this submission"
        if report.generated_at < receipt.submitted_at - self.clock_skew:
            return "stale report: generated before this submission"
        max_age = getattr(self.scorer, "max_report_age", None) or self.max_report_age
        if report.generated_at - receipt.submitted_at > max_age:
            return "stale report: generated too long after the submission"
        if report.generated_at > self.clock() + self.clock_skew:
            return "report timestamp is in the future"
        return None

    def _commit(self, job, journal: Journal, res, reconciled: bool) -> ReviewResult:
        report = QualityReport.from_dict(journal.data["report"])
        try:
            self.sink.commit(job.job_id, report)
        except Exception as e:  # noqa: BLE001 - journal keeps RESULT_VALIDATED; rerun re-commits
            return res(FAILED, journal, f"commit failed ({type(e).__name__}: {e}); rerun to re-commit",
                       report, reconciled)
        self.hook("after_sink_commit", job.job_id)
        journal.transition(S.COMMITTED, "")
        self._release(journal)
        return res(COMMITTED, journal, "", report, reconciled)

    def _release(self, journal: Journal) -> None:
        """Optional provider hook after COMMITTED: drop provider-side copies of the inputs.
        Best effort; a failure never undoes the commit."""
        release = getattr(self.scorer, "release", None)
        if release is not None and journal.data.get("receipt"):
            try:
                release(SubmissionReceipt.from_dict(journal.data["receipt"]))
            except Exception:  # noqa: BLE001
                pass

    def _reconcile_committed(self, job, journal: Journal, res) -> ReviewResult:
        report = QualityReport.from_dict(journal.data["report"])
        current = self.sink.committed_fingerprint(job.job_id)
        if current == journal.data["report_fingerprint"]:
            return res(ALREADY_COMMITTED, journal, "", report, True)
        if current is None:  # journal says committed but the sink lost it: re-commit, never resubmit
            try:
                self.sink.commit(job.job_id, report)
            except Exception as e:  # noqa: BLE001
                return res(FAILED, journal, f"re-commit failed ({type(e).__name__}: {e})", report, True)
            return res(ALREADY_COMMITTED, journal, "re-committed journaled report", report, True)
        return res(MANUAL_RECOVERY_REQUIRED, journal,
                   "sink holds a different report than the journal; resolve by hand", report, True)


# ------------------------------------------------------------- operator actions

def resolve_not_submitted(journal_path: Path, note: str) -> None:
    """Operator confirmed with the provider that nothing was submitted: allow a retry."""
    journal = Journal.load(journal_path)
    if journal is None:
        raise FileNotFoundError(journal_path)
    journal.transition(S.SAFE_TO_RETRY, f"operator: {note}")


def adopt_report(journal_path: Path, report: QualityReport, note: str) -> None:
    """Operator supplies the report of a submission that cannot be collected automatically.
    It is schema-checked and committed by the next run; freshness checks do not apply."""
    journal = Journal.load(journal_path)
    if journal is None:
        raise FileNotFoundError(journal_path)
    journal.transition(S.RESULT_DETECTED, f"operator adopted report: {note}",
                       report=report.to_dict(), adopted=True)
