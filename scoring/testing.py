"""Fault injection for the review engine (used by scoring/tests, usable by any provider's tests).

`Crash` simulates the process dying at a named point; `CrashAt` is an engine hook raising it.
`FaultyScorer` wraps a real provider and injects provider-side failures:

    lost_after_submit   the provider accepts the submission, then the connection drops
    lost_before_submit  the connection drops before the provider receives anything
    reject              the provider definitely refuses the submission
    quota               the provider has no quota left (raised before submitting)
    stale_report        collect() returns the report that existed before this submission
    wrong_fingerprint   collect() returns a report for a different input
"""
from __future__ import annotations

import dataclasses

from scoring.models import ProviderUnavailable, SubmissionRejected


class Crash(BaseException):
    """Process death. BaseException so no `except Exception` in the engine can swallow it."""


class CrashAt:
    def __init__(self, *points: str, times: int = 1):
        self.points, self.remaining = set(points), times

    def __call__(self, point: str, job_id: str) -> None:
        if point in self.points and self.remaining > 0:
            self.remaining -= 1
            raise Crash(point)


class FaultyScorer:
    def __init__(self, inner, *faults: str):
        self.inner, self.faults = inner, set(faults)
        self.name = f"faulty({inner.name})"
        if hasattr(inner, "lookup"):
            self.lookup = inner.lookup

    def submit(self, resume, job_description, *, idempotency_key):
        if "quota" in self.faults:
            raise ProviderUnavailable("no scans left", code="PROVIDER_QUOTA_EXHAUSTED")
        if "reject" in self.faults:
            raise SubmissionRejected("resume text rejected")
        if "lost_before_submit" in self.faults:
            self.faults.discard("lost_before_submit")
            raise ConnectionError("connection reset before request was sent")
        receipt = self.inner.submit(resume, job_description, idempotency_key=idempotency_key)
        if "lost_after_submit" in self.faults:
            self.faults.discard("lost_after_submit")
            raise ConnectionError("connection reset after request was sent")
        return receipt

    def collect(self, receipt):
        report = self.inner.collect(receipt)
        if "stale_report" in self.faults:
            report = dataclasses.replace(report, report_id=receipt.previous_report_id or "",
                                         generated_at=receipt.submitted_at - 3600)
        if "wrong_fingerprint" in self.faults:
            report = dataclasses.replace(report, input_fingerprint="0" * 64)
        return report


class MemorySink:
    """In-memory ResultSink with an optional injected write failure."""

    def __init__(self, fail_times: int = 0):
        self.reports: dict = {}
        self.commits = 0
        self.fail_times = fail_times

    def committed_fingerprint(self, job_id):
        r = self.reports.get(job_id)
        return r.fingerprint if r else None

    def commit(self, job_id, report):
        if self.fail_times > 0:
            self.fail_times -= 1
            raise OSError("disk full")
        self.commits += 1
        self.reports[job_id] = report
