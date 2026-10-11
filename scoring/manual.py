"""Human-in-the-loop provider: the request goes to a folder, a person returns the report.

submit()  writes <inbox>/<key>.request.json (resume text, JD, fingerprint) and returns a receipt.
collect() reads  <inbox>/<key>.report.json once the reviewer has written it, e.g.

    {"score": 74,
     "matched_skills": ["Python", "SQL"],
     "missing_keywords": ["forecasting"],
     "matched_soft_skills": [], "missing_soft_skills": []}

Until then collect() raises ResultNotReady, so the run ends with PENDING_RESULT and the next
run collects without resubmitting.

Privacy: request files contain the full resume and JD in plain text. The inbox must be a
private directory (created 0700; a group- or world-accessible inbox is refused), every file is
written 0600, both files of a job are deleted once its report is committed (release()), and
purge_older_than() removes abandoned requests. Never point the inbox at a synced or shared folder.
"""
from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path

from scoring.journal import atomic_write_json
from scoring.models import QualityReport, ResultNotReady, ResumeDocument, SubmissionReceipt, input_fingerprint


class ManualFileScorer:
    name = "manual"
    max_report_age = 7 * 24 * 3600.0  # a person may answer days later

    def __init__(self, inbox: Path):
        self.inbox = Path(inbox)
        self.inbox.mkdir(mode=0o700, parents=True, exist_ok=True)
        mode = stat.S_IMODE(os.stat(self.inbox).st_mode)
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise PermissionError(f"inbox {self.inbox} is accessible to other users (mode {oct(mode)}); "
                                  "chmod 700 it first")

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

    def release(self, receipt: SubmissionReceipt) -> None:
        """Called by the engine after the report is committed: delete the plain-text copies."""
        for suffix in ("request", "report"):
            (self.inbox / f"{receipt.idempotency_key}.{suffix}.json").unlink(missing_ok=True)

    def purge_older_than(self, seconds: float) -> list[Path]:
        """Delete request / report files older than `seconds` (abandoned reviews)."""
        cutoff, removed = time.time() - seconds, []
        for path in self.inbox.glob("*.json"):
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
                removed.append(path)
        return removed

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
