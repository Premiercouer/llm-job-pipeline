"""Human-in-the-loop provider: the request goes to a folder, a person returns the report.

submit()  writes <inbox>/<key>.request.json (resume text, JD, fingerprint) and returns a receipt.
collect() reads  <inbox>/<key>.report.json once the reviewer has written it, e.g.

    {"score": 74,
     "matched_skills": ["Python", "SQL"],
     "missing_keywords": ["forecasting"],
     "matched_soft_skills": [], "missing_soft_skills": []}

Until then collect() raises ResultNotReady, so the run ends with PENDING_RESULT and the next
run collects without resubmitting.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from scoring.journal import atomic_write_json
from scoring.models import QualityReport, ResultNotReady, ResumeDocument, SubmissionReceipt, input_fingerprint


class ManualFileScorer:
    name = "manual"
    max_report_age = 7 * 24 * 3600.0  # a person may answer days later

    def __init__(self, inbox: Path):
        self.inbox = Path(inbox)

    def _req(self, key: str) -> Path:
        return self.inbox / f"{key}.request.json"

    def submit(self, resume: ResumeDocument, job_description: str, *, idempotency_key: str) -> SubmissionReceipt:
        path = self._req(idempotency_key)
        if path.exists():
            return SubmissionReceipt(**json.loads(path.read_text(encoding="utf-8"))["receipt"])
        receipt = SubmissionReceipt(
            submission_id=f"manual-{idempotency_key[:12]}", idempotency_key=idempotency_key,
            input_fingerprint=input_fingerprint(resume.text, job_description),
            submitted_at=time.time(), provider=self.name)
        atomic_write_json(path, {"receipt": receipt.to_dict(), "resume": resume.text, "job_description": job_description})
        return receipt

    def lookup(self, idempotency_key: str) -> SubmissionReceipt | None:
        path = self._req(idempotency_key)
        if not path.exists():
            return None
        return SubmissionReceipt(**json.loads(path.read_text(encoding="utf-8"))["receipt"])

    def collect(self, receipt: SubmissionReceipt) -> QualityReport:
        path = self.inbox / f"{receipt.idempotency_key}.report.json"
        if not path.exists():
            raise ResultNotReady(f"waiting for {path.name}")
        data = json.loads(path.read_text(encoding="utf-8"))
        return QualityReport(
            score=data.get("score"), matched_skills=data.get("matched_skills", ()),
            missing_keywords=data.get("missing_keywords", ()),
            matched_soft_skills=data.get("matched_soft_skills", ()),
            missing_soft_skills=data.get("missing_soft_skills", ()),
            report_id=f"manual-report-{receipt.submission_id}", generated_at=path.stat().st_mtime,
            input_fingerprint=receipt.input_fingerprint, provider=self.name)
