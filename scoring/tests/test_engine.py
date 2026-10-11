"""Offline tests for the Resume Quality Review engine: state machine, journal, crash recovery,
idempotency and fault injection. No network, no browser, no model.

Run: python3 -m unittest discover -s scoring/tests -t .      (from the repository root)
"""
import json
import os
import random
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scoring import states as S
from scoring.engine import (ALREADY_COMMITTED, COMMITTED, FAILED, MANUAL_RECOVERY_REQUIRED,
                            PENDING_RESULT, PROVIDER_UNAVAILABLE, SAFE_TO_RETRY, ReviewEngine,
                            adopt_report, resolve_not_submitted)
from scoring.journal import Journal, atomic_write_json
from scoring.manual import ManualFileScorer
from scoring.mock import MockResumeScorer
from scoring.models import QualityReport, ResumeDocument, ReviewJob
from scoring.testing import Crash, CrashAt, FaultyScorer, MemorySink

RESUME = "Built Python and SQL ETL pipelines and Tableau dashboards; stakeholder management."
JD = "We need Python, SQL, Tableau, Forecasting and Stakeholder Management experience."


class NoLookup:
    """Wraps a provider and hides lookup(): a provider that cannot reconcile."""

    def __init__(self, inner):
        self.inner, self.name = inner, inner.name

    def submit(self, *a, **k):
        return self.inner.submit(*a, **k)

    def collect(self, receipt):
        return self.inner.collect(receipt)


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.provider = MockResumeScorer()
        self.sink = MemorySink()
        self.job = ReviewJob("job-1", ResumeDocument(RESUME), JD)

    def tearDown(self):
        self._tmp.cleanup()

    def engine(self, scorer=None, hook=None, **kw):
        kw.setdefault("sleep", lambda s: None)
        return ReviewEngine(scorer or self.provider, self.dir / "journal", self.sink, hook=hook, **kw)

    def journal(self):
        return Journal.load(self.dir / "journal" / "job-1.json")

    def assert_history_legal(self):
        for step in self.journal().data["history"]:
            self.assertIn(step["to"], S.TRANSITIONS[step["from"]], step)


class TestHappyPath(Base):
    def test_happy_path_walks_every_state_once(self):
        r = self.engine().review(self.job)
        self.assertEqual(r.outcome, COMMITTED)
        self.assertEqual([h["to"] for h in self.journal().data["history"]], list(S.HAPPY_PATH[1:]))
        self.assertEqual(self.provider.submit_calls, 1)
        self.assertEqual(self.sink.reports["job-1"].fingerprint, self.journal().data["report_fingerprint"])
        self.assertEqual(r.report.score, 75)
        self.assertEqual([m.skill for m in r.report.missing_keywords], ["Forecasting"])

    def test_rerun_is_idempotent(self):
        self.engine().review(self.job)
        r = self.engine().review(self.job)
        self.assertEqual(r.outcome, ALREADY_COMMITTED)
        self.assertEqual(sum(r.calls.values()), 0)
        self.assertEqual(self.provider.submit_calls, 1)
        self.assertEqual(self.sink.commits, 1)

    def test_illegal_transition_is_refused(self):
        j = Journal.new(self.dir / "j.json", "x", "k", "fp")
        with self.assertRaises(S.IllegalTransition):
            j.transition(S.SUBMITTED)
        self.assertFalse((self.dir / "j.json").exists())

    def test_feedback_is_the_resume_v2_contract(self):
        r = self.engine().review(self.job)
        fb = r.report.to_feedback()
        self.assertEqual(set(fb), {"hard_skills", "soft_skills"})
        self.assertEqual(fb["hard_skills"]["missing"], [{"skill": "Forecasting", "resume_count": 0, "jd_count": 1}])


class TestCrashRecovery(Base):
    def test_crash_before_intent_resubmits_safely(self):
        with self.assertRaises(Crash):
            self.engine(hook=CrashAt("before_intent")).review(self.job)
        self.assertEqual(self.journal().state, S.INPUT_PREPARED)
        self.assertEqual(self.provider.submit_calls, 0)
        self.assertEqual(self.engine().review(self.job).outcome, COMMITTED)
        self.assertEqual(self.provider.submit_calls, 1)

    def test_crash_after_submission_intent(self):
        """Intent recorded, provider never called: the provider has no record either, so the
        engine cannot prove anything and must not guess: manual recovery, no submit."""
        with self.assertRaises(Crash):
            self.engine(hook=CrashAt("after_intent")).review(self.job)
        r = self.engine().review(self.job)
        self.assertEqual(r.outcome, MANUAL_RECOVERY_REQUIRED)
        self.assertEqual(r.calls["submit"], 0)
        self.assertEqual(self.provider.submit_calls, 0)

    def test_crash_after_submit_reconciles_via_lookup(self):
        with self.assertRaises(Crash):
            self.engine(hook=CrashAt("before_record_submitted")).review(self.job)
        self.assertEqual(self.journal().state, S.SUBMISSION_INTENT_RECORDED)
        r = self.engine().review(self.job)
        self.assertEqual(r.outcome, COMMITTED)
        self.assertTrue(r.reconciled)
        self.assertEqual(r.calls["submit"], 0)
        self.assertEqual(self.provider.submit_calls, 1)

    def test_crash_after_submit_without_lookup_requires_manual_recovery(self):
        scorer = NoLookup(self.provider)
        with self.assertRaises(Crash):
            self.engine(scorer, hook=CrashAt("before_record_submitted")).review(self.job)
        r = self.engine(scorer).review(self.job)
        self.assertEqual(r.outcome, MANUAL_RECOVERY_REQUIRED)
        self.assertEqual(self.provider.submit_calls, 1)

    def test_crash_after_detection_revalidates_without_resubmitting(self):
        with self.assertRaises(Crash):
            self.engine(hook=CrashAt("after_detected")).review(self.job)
        r = self.engine().review(self.job)
        self.assertEqual((r.outcome, r.calls["submit"], r.calls["collect"]), (COMMITTED, 0, 0))

    def test_crash_after_sink_commit_recommits_journaled_report(self):
        with self.assertRaises(Crash):
            self.engine(hook=CrashAt("after_sink_commit")).review(self.job)
        self.assertEqual(self.journal().state, S.RESULT_VALIDATED)
        r = self.engine().review(self.job)
        self.assertEqual((r.outcome, sum(r.calls.values())), (COMMITTED, 0))
        self.assertEqual(self.provider.submit_calls, 1)

    def test_reconcile_never_resubmits(self):
        """Crash at every point after the intent: no rerun ever calls submit() again."""
        for point in ("after_intent", "before_record_submitted", "after_submitted",
                      "after_detected", "after_validated", "after_sink_commit"):
            with self.subTest(point=point):
                self.tearDown()
                self.setUp()
                with self.assertRaises(Crash):
                    self.engine(hook=CrashAt(point)).review(self.job)
                before = self.provider.submit_calls
                for _ in range(3):
                    r = self.engine().review(self.job)
                    self.assertEqual(r.calls["submit"], 0, point)
                self.assertEqual(self.provider.submit_calls, before)
                self.assert_history_legal()


class TestAmbiguousSubmission(Base):
    def test_connection_lost_after_submit_is_reconciled_by_lookup(self):
        r = self.engine(FaultyScorer(self.provider, "lost_after_submit")).review(self.job)
        self.assertEqual(r.outcome, COMMITTED)
        self.assertEqual((r.calls["submit"], r.calls["lookup"]), (1, 1))

    def test_ambiguous_submission_requires_manual_recovery(self):
        r = self.engine(FaultyScorer(NoLookup(self.provider), "lost_after_submit")).review(self.job)
        self.assertEqual(r.outcome, MANUAL_RECOVERY_REQUIRED)
        self.assertIn("outcome unknown", r.detail)

    def test_lost_before_submit_is_still_treated_as_unknown(self):
        """The engine cannot tell 'lost before' from 'lost after'; no record is not proof."""
        r = self.engine(FaultyScorer(self.provider, "lost_before_submit")).review(self.job)
        self.assertEqual(r.outcome, MANUAL_RECOVERY_REQUIRED)

    def test_ambiguous_submission_is_never_retried_automatically(self):
        self.engine(FaultyScorer(NoLookup(self.provider), "lost_after_submit")).review(self.job)
        for _ in range(3):
            r = self.engine().review(self.job)
            self.assertEqual((r.outcome, r.calls["submit"]), (MANUAL_RECOVERY_REQUIRED, 0))
        self.assertEqual(self.provider.submit_calls, 1)


class TestProviderOutcomes(Base):
    def test_rejected_submission_is_safe_to_retry(self):
        r = self.engine(FaultyScorer(self.provider, "reject")).review(self.job)
        self.assertEqual(r.outcome, SAFE_TO_RETRY)
        self.assertEqual(self.provider.submit_calls, 0)
        r = self.engine().review(self.job)
        self.assertEqual(r.outcome, COMMITTED)
        self.assertEqual(self.journal().data["attempt"], 2)

    def test_quota_exhausted_stops_without_submitting(self):
        r = self.engine(FaultyScorer(self.provider, "quota")).review(self.job)
        self.assertEqual(r.outcome, PROVIDER_UNAVAILABLE)
        self.assertIn("PROVIDER_QUOTA_EXHAUSTED", r.detail)
        self.assertEqual(self.provider.submit_calls, 0)
        self.assertEqual(self.engine().review(self.job).outcome, COMMITTED)  # operator restored quota

    def test_report_not_ready_is_collected_later_without_resubmitting(self):
        slow = MockResumeScorer(ready_after_polls=5)
        r = self.engine(slow, max_polls=2).review(self.job)
        self.assertEqual((r.outcome, r.state), (PENDING_RESULT, S.SUBMITTED))
        r = self.engine(slow, max_polls=10).review(self.job)
        self.assertEqual((r.outcome, r.calls["submit"]), (COMMITTED, 0))
        self.assertEqual(slow.submit_calls, 1)


class TestResultValidation(Base):
    def test_stale_report_is_not_committed(self):
        r = self.engine(FaultyScorer(self.provider, "stale_report")).review(self.job)
        self.assertEqual(r.outcome, MANUAL_RECOVERY_REQUIRED)
        self.assertIn("stale report", r.detail)
        self.assertEqual(self.sink.reports, {})

    def test_fingerprint_mismatch_is_not_committed(self):
        r = self.engine(FaultyScorer(self.provider, "wrong_fingerprint")).review(self.job)
        self.assertEqual(r.outcome, MANUAL_RECOVERY_REQUIRED)
        self.assertIn("fingerprint mismatch", r.detail)
        self.assertEqual(self.sink.reports, {})

    def test_input_changed_after_submission_requires_manual_recovery(self):
        with self.assertRaises(Crash):
            self.engine(hook=CrashAt("after_submitted")).review(self.job)
        changed = ReviewJob("job-1", ResumeDocument(RESUME + " edited"), JD)
        r = self.engine().review(changed)
        self.assertEqual((r.outcome, r.calls["submit"]), (MANUAL_RECOVERY_REQUIRED, 0))


class TestPersistence(Base):
    def test_sink_failure_keeps_validated_report_for_recommit(self):
        self.sink.fail_times = 1
        r = self.engine().review(self.job)
        self.assertEqual((r.outcome, r.state), (FAILED, S.RESULT_VALIDATED))
        r = self.engine().review(self.job)
        self.assertEqual((r.outcome, sum(r.calls.values())), (COMMITTED, 0))
        self.assertEqual(self.provider.submit_calls, 1)

    def test_atomic_write_failure_does_not_submit(self):
        real = os.replace
        calls = {"n": 0}

        def flaky(src, dst):
            calls["n"] += 1
            if calls["n"] == 2:  # the SUBMISSION_INTENT_RECORDED write
                raise OSError("disk full")
            return real(src, dst)

        with mock.patch("scoring.journal.os.replace", flaky):
            r = self.engine().review(self.job)
        self.assertEqual(r.outcome, FAILED)
        self.assertEqual(self.journal().state, S.INPUT_PREPARED)  # previous version intact
        self.assertEqual(self.provider.submit_calls, 0)
        self.assertEqual([p.name for p in (self.dir / "journal").iterdir()], ["job-1.json"])  # no tmp left
        self.assertEqual(self.engine().review(self.job).outcome, COMMITTED)

    def test_atomic_write_never_leaves_a_torn_file(self):
        path = self.dir / "x.json"
        atomic_write_json(path, {"v": 1})
        with mock.patch("scoring.journal.os.replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                atomic_write_json(path, {"v": 2})
        self.assertEqual(json.loads(path.read_text()), {"v": 1})
        self.assertEqual([p.name for p in self.dir.iterdir()], ["x.json"])

    def test_corrupt_journal_blocks_the_job_and_is_kept(self):
        path = self.dir / "journal" / "job-1.json"
        path.parent.mkdir(parents=True)
        path.write_text("{not json")
        r = self.engine().review(self.job)
        self.assertEqual((r.outcome, r.calls["submit"]), (MANUAL_RECOVERY_REQUIRED, 0))
        self.assertEqual(path.read_text(), "{not json")


class TestOperatorRecovery(Base):
    def test_operator_confirms_not_submitted_then_retry(self):
        self.engine(FaultyScorer(NoLookup(self.provider), "lost_before_submit")).review(self.job)
        resolve_not_submitted(self.dir / "journal" / "job-1.json", "checked provider history")
        self.assertEqual(self.engine().review(self.job).outcome, COMMITTED)
        self.assert_history_legal()

    def test_operator_adopted_report_is_committed(self):
        self.engine(FaultyScorer(NoLookup(self.provider), "lost_after_submit")).review(self.job)
        report = QualityReport(score=82, matched_skills=["Python", "SQL", "Tableau"],
                               missing_keywords=["forecasting", "stakeholder management"])
        adopt_report(self.dir / "journal" / "job-1.json", report, "copied from provider UI")
        r = self.engine().review(self.job)
        self.assertEqual((r.outcome, r.calls["submit"], r.report.score), (COMMITTED, 0, 82))


class TestManualProvider(Base):
    def test_manual_provider_round_trip(self):
        scorer = ManualFileScorer(self.dir / "inbox")
        r = self.engine(scorer, max_polls=1).review(self.job)
        self.assertEqual(r.outcome, PENDING_RESULT)
        (req,) = (self.dir / "inbox").glob("*.request.json")
        report = req.with_name(req.name.replace(".request.", ".report."))
        report.write_text(json.dumps({"score": 70, "matched_skills": ["Python"], "missing_keywords": ["dbt"]}))
        r = self.engine(scorer).review(self.job)
        self.assertEqual((r.outcome, r.report.score), (COMMITTED, 70))
        self.assertEqual(list((self.dir / "inbox").iterdir()), [])  # plain-text copies released

    def test_manual_inbox_must_be_private(self):
        inbox = self.dir / "shared"
        inbox.mkdir(mode=0o755)
        os.chmod(inbox, 0o755)
        with self.assertRaises(PermissionError):
            ManualFileScorer(inbox)

    def test_manual_files_are_owner_only_and_purged(self):
        scorer = ManualFileScorer(self.dir / "inbox")
        self.engine(scorer, max_polls=1).review(self.job)
        (req,) = (self.dir / "inbox").glob("*.request.json")
        self.assertEqual(os.stat(req).st_mode & 0o077, 0)
        os.utime(req, (0, 0))
        self.assertEqual(scorer.purge_older_than(3600), [req])


class TestFaultSchedules(unittest.TestCase):
    """Property test: random provider faults and crash points, then healthy reruns.

    Invariants: the provider receives at most one submission; every recorded transition is
    legal; a committed report is the journal's validated report for this exact input; the job
    always ends COMMITTED or waiting for an operator.
    """
    FAULTS = ("lost_after_submit", "lost_before_submit", "reject", "quota", "stale_report",
              "wrong_fingerprint")
    POINTS = ("before_intent", "after_intent", "before_record_submitted", "after_submitted",
              "after_detected", "after_validated", "after_sink_commit")

    def test_random_fault_schedules_preserve_invariants(self):
        for seed in range(400):
            rng = random.Random(seed)
            with tempfile.TemporaryDirectory() as tmp, self.subTest(seed=seed):
                provider, sink = MockResumeScorer(), MemorySink(fail_times=rng.choice([0, 0, 1]))
                job = ReviewJob("job", ResumeDocument(RESUME), JD)
                can_lookup = rng.random() < 0.6
                for _ in range(rng.randint(1, 3)):  # faulty runs
                    inner = provider if can_lookup else NoLookup(provider)
                    scorer = FaultyScorer(inner, *rng.sample(self.FAULTS, rng.randint(0, 2)))
                    hook = CrashAt(rng.choice(self.POINTS)) if rng.random() < 0.5 else None
                    try:
                        ReviewEngine(scorer, Path(tmp), sink, hook=hook, sleep=lambda s: None).review(job)
                    except Crash:
                        pass
                final = None
                for _ in range(3):  # healthy reruns
                    inner = provider if can_lookup else NoLookup(provider)
                    final = ReviewEngine(inner, Path(tmp), sink, sleep=lambda s: None).review(job)

                self.assertLessEqual(provider.submit_calls, 1)
                journal = Journal.load(Path(tmp) / "job.json")
                for step in journal.data["history"]:
                    self.assertIn(step["to"], S.TRANSITIONS[step["from"]])
                self.assertIn(final.outcome, (COMMITTED, ALREADY_COMMITTED, MANUAL_RECOVERY_REQUIRED))
                if "job" in sink.reports:
                    committed = sink.reports["job"]
                    self.assertEqual(committed.fingerprint, journal.data["report_fingerprint"])
                    self.assertEqual(committed.input_fingerprint, job.input_fingerprint)
                    self.assertEqual(journal.state, S.COMMITTED)


if __name__ == "__main__":
    unittest.main()
