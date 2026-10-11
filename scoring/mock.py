"""Fully offline provider: deterministic keyword coverage of a JD by a resume.

It behaves like a remote service as far as the engine can tell: submissions get ids, are
de-duplicated by idempotency key, can be looked up after a crash, and every submission
produces a new report. CI uses it to run Resume V1 → scoring → structured feedback → Resume V2
without any network or account.
"""
from __future__ import annotations

import itertools
import re
import threading
import time
from typing import Callable

from scoring.models import QualityReport, ResultNotReady, ResumeDocument, SubmissionReceipt, input_fingerprint

HARD_SKILLS = (
    "Python", "R", "SQL", "Excel", "Power BI", "Tableau", "Looker", "DAX", "BigQuery", "Snowflake",
    "Databricks", "Spark", "PySpark", "Airflow", "dbt", "AWS", "GCP", "Azure", "SageMaker",
    "Docker", "Kubernetes", "Git", "ETL", "Data Pipelines", "Data Modeling", "Data Warehousing",
    "Data Quality", "Data Governance", "Data Visualization", "Dashboards", "KPI", "A/B Testing",
    "Experimentation", "Statistics", "Regression", "Forecasting", "Time Series", "Machine Learning",
    "Classification", "Clustering", "Deep Learning", "NLP", "LLM", "Feature Engineering",
    "scikit-learn", "pandas", "NumPy", "XGBoost", "REST API", "PostgreSQL", "Tableau Prep",
    "Predictive Modeling", "Business Intelligence", "Reporting",
)
SOFT_SKILLS = (
    "Stakeholder Management", "Stakeholder Engagement", "Communication", "Collaboration",
    "Problem Solving", "Requirements Gathering", "Presentation", "Storytelling", "Leadership",
    "Attention to Detail", "Project Management", "Cross-functional",
)


def _count(term: str, text: str) -> int:
    pattern = r"(?<![A-Za-z0-9])" + re.escape(term).replace(r"\ ", r"[\s-]+") + r"(?![A-Za-z0-9])"
    return len(re.findall(pattern, text, flags=re.IGNORECASE))


def keyword_report(resume: str, jd: str) -> dict:
    out = {}
    for name, vocab in (("hard", HARD_SKILLS), ("soft", SOFT_SKILLS)):
        matched, missing = [], []
        for term in vocab:
            jc = _count(term, jd)
            if not jc:
                continue
            rc = _count(term, resume)
            (matched if rc else missing).append({"skill": term, "resume_count": rc, "jd_count": jc})
        out[name] = (matched, missing)
    total = len(out["hard"][0]) + len(out["hard"][1])
    score = round(100 * len(out["hard"][0]) / total) if total else None
    return {"score": score, "hard": out["hard"], "soft": out["soft"]}


class MockResumeScorer:
    name = "mock"

    def __init__(self, clock: Callable[[], float] = time.time, ready_after_polls: int = 0):
        self.clock = clock
        self.ready_after_polls = ready_after_polls
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self.submissions: dict[str, dict] = {}   # idempotency_key -> stored submission
        self.submit_calls = 0
        self.last_report_id: str | None = None

    def submit(self, resume: ResumeDocument, job_description: str, *, idempotency_key: str) -> SubmissionReceipt:
        with self._lock:
            self.submit_calls += 1
            existing = self.submissions.get(idempotency_key)
            if existing:  # idempotent: the same key never creates a second submission
                return existing["receipt"]
            receipt = SubmissionReceipt(
                submission_id=f"mock-sub-{next(self._ids)}", idempotency_key=idempotency_key,
                input_fingerprint=input_fingerprint(resume.text, job_description),
                submitted_at=self.clock(), previous_report_id=self.last_report_id, provider=self.name)
            self.submissions[idempotency_key] = {"receipt": receipt, "resume": resume.text,
                                                 "jd": job_description, "polls": 0, "report": None}
            return receipt

    def lookup(self, idempotency_key: str) -> SubmissionReceipt | None:
        entry = self.submissions.get(idempotency_key)
        return entry["receipt"] if entry else None

    def collect(self, receipt: SubmissionReceipt) -> QualityReport:
        with self._lock:
            entry = self.submissions.get(receipt.idempotency_key)
            if entry is None or entry["receipt"].submission_id != receipt.submission_id:
                raise ResultNotReady(f"unknown submission {receipt.submission_id}")
            entry["polls"] += 1
            if entry["polls"] <= self.ready_after_polls:
                raise ResultNotReady("still scoring")
            if entry["report"] is None:
                k = keyword_report(entry["resume"], entry["jd"])
                entry["report"] = QualityReport(
                    score=k["score"], matched_skills=k["hard"][0], missing_keywords=k["hard"][1],
                    matched_soft_skills=k["soft"][0], missing_soft_skills=k["soft"][1],
                    report_id=f"mock-rep-{receipt.submission_id}", generated_at=self.clock(),
                    input_fingerprint=receipt.input_fingerprint, provider=self.name)
                self.last_report_id = entry["report"].report_id
            return entry["report"]
