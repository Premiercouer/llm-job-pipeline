"""Offline tests for script/05_enqueue_applications.py (temp jobs root + tracker + list only).

Run: python3 -m unittest discover -s test -p "test_*.py" -v      (from resume/)
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path

# Isolate before any stage (and common.paths) is imported: never touch the repo workspace.
_WS = tempfile.mkdtemp(prefix="ljp_test_ws_")
os.environ["LJP_WORKSPACE"] = _WS
os.environ["LJP_CANDIDATE_DIR"] = str(Path(__file__).resolve().parents[2] / "examples" / "candidate")

SCRIPT = Path(__file__).resolve().parent.parent / "script" / "05_enqueue_applications.py"
spec = importlib.util.spec_from_file_location("enqueue_applications", SCRIPT)
p05 = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = p05
spec.loader.exec_module(p05)

BATCH = "2026-10-07_0008"


class TestEnqueue(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name).resolve()  # macOS: /var -> /private/var, as run() resolves jobs_root
        self.jobs_root = root / "jobs"
        self.app_list = root / "application_list.json"
        self.backup_dir = root / ".runtime"
        self.jobs_root.mkdir()
        self.rows = []
        self.write_list([{"n": 75, "batch": "old", "job_folder": "/x/old/A", "company": "HUMAN",
                          "position": "Data Scientist", "status": "APPLIED"}])
        self.logs = []

    def tearDown(self):
        self.tmp.cleanup()

    def write_list(self, jobs):
        self.app_list.write_text(json.dumps({"_note": "keep me", "jobs": jobs}, ensure_ascii=False, indent=2),
                                 encoding="utf-8")

    def add_job(self, folder, company, position, url, final="DONE", pages="1", pdf=True, batch=BATCH):
        row = dict.fromkeys(p05.p01.TRACKER_COLUMNS, "")
        row.update(batch=batch, job_folder=folder, company=company, position=position, apply_url=url,
                   final_status=final, pdf_pages=pages)
        self.rows.append(row)
        d = self.jobs_root / batch / folder
        d.mkdir(parents=True)
        if pdf:
            (d / "resume.pdf").write_bytes(b"%PDF-1.4")
        p05.p01.write_tracker_atomic(self.jobs_root / p05.p01.TRACKER_NAME, self.rows)

    def run_it(self, dry_run=False, batch=None):
        args = types.SimpleNamespace(jobs_root=str(self.jobs_root), application_list=str(self.app_list),
                                     backup_dir=str(self.backup_dir), dry_run=dry_run, batch=batch)
        code = p05.run(args, log=self.logs.append)
        return code, json.loads(self.app_list.read_text(encoding="utf-8"))

    def test_guess_ats(self):
        cases = {
            "https://www.linkedin.com/jobs/view/4474095520/": "LinkedIn Easy Apply",
            "https://bmo.wd3.myworkdayjobs.com/en-US/External/job/X": "Workday",
            "https://hyundaicapital.taleo.net/careersection/hcca/jobdetail.ftl?job=1": "Taleo",
            "https://grnh.se/j7xvmjjt3us": "Greenhouse",
            "https://yorkvilleuniversity.applytojob.com/apply/K1WMg4OU6A/x": "JazzHR",
            "https://jobs.zs.com/jobs/32755?lang=en-us": None,
            "https://www.jobs.ca/foo/jobs/bar": None,
        }
        self.assertEqual({u: p05.guess_ats(u) for u in cases}, cases)

    def test_adds_done_rows_numbered_after_max(self):
        self.add_job("Python_Developer__Zodiac__d8", "Zodiac Solutions, Inc", "Python Developer",
                     "https://www.linkedin.com/jobs/view/4474095520/")
        self.add_job("Associate_Data_Engineer__BMO__81", "BMO", "Associate, Data Engineer",
                     "https://bmo.wd3.myworkdayjobs.com/x")
        code, data = self.run_it()
        self.assertEqual(code, 0)
        self.assertEqual(data["_note"], "keep me")
        new = data["jobs"][1:]
        self.assertEqual([(j["n"], j["ats"], j["status"]) for j in new],
                         [(76, "LinkedIn Easy Apply", "TODO"), (77, "Workday", "TODO")])
        self.assertEqual(new[0]["resume_pdf"],
                         str(self.jobs_root / BATCH / "Python_Developer__Zodiac__d8" / "resume.pdf"))
        self.assertEqual(len(list(self.backup_dir.glob("application_list.json.bak_*"))), 1)

    def test_second_run_adds_nothing(self):
        self.add_job("A__B__1", "Acme", "Analyst", "https://acme.com/1")
        self.run_it()
        before = self.app_list.read_bytes()
        code, data = self.run_it()
        self.assertEqual((code, len(data["jobs"])), (0, 2))
        self.assertEqual(self.app_list.read_bytes(), before)
        self.assertTrue(any("already listed" in m for m in self.logs))

    def test_not_ready_rows_are_left_out(self):
        self.add_job("F__X__1", "X", "Failed One", "https://x.com/1", final="LAYOUT_FAILED", pages="2")
        self.add_job("P__Y__2", "Y", "Pending One", "https://y.com/2", final="PENDING", pages="")
        self.add_job("M__Z__3", "Z", "No Pdf", "https://z.com/3", pdf=False)
        code, data = self.run_it()
        self.assertEqual((code, len(data["jobs"])), (0, 1))
        self.assertEqual(sum("[NOT_READY]" in m for m in self.logs), 3)

    def test_cross_source_duplicate_is_skipped(self):
        self.add_job("RA__SMH__1", "St. Michael's Hospital", "Research Assistant I, Genomics Research",
                     "https://example.inforcloudsuite.com/x")
        self.write_list([{"n": 57, "batch": "old", "job_folder": "/x/old/SMH",
                          "company": "St. Michael’s Hospital",
                          "position": "Research Assistant I, Genomics Research", "status": "APPLIED"}])
        code, data = self.run_it()
        self.assertEqual((code, len(data["jobs"])), (0, 1))
        self.assertTrue(any("same job as #57 APPLIED" in m for m in self.logs))

    def test_dry_run_writes_nothing(self):
        self.add_job("A__B__1", "Acme", "Analyst", "https://acme.com/1")
        before = self.app_list.read_bytes()
        code, _ = self.run_it(dry_run=True)
        self.assertEqual(code, 0)
        self.assertEqual(self.app_list.read_bytes(), before)
        self.assertFalse(self.backup_dir.exists())

    def test_default_batch_is_newest_and_unknown_batch_fails(self):
        self.add_job("Old__O__1", "Old Co", "Old Role", "https://o.com/1", batch="2026-10-06_2221")
        self.add_job("New__N__1", "New Co", "New Role", "https://n.com/1")
        _, data = self.run_it()
        self.assertEqual([j["company"] for j in data["jobs"][1:]], ["New Co"])
        with self.assertRaises(p05.EnqueueError):
            self.run_it(batch=["2099-01-01_0000"])

    def test_list_changed_during_run_aborts(self):
        self.add_job("A__B__1", "Acme", "Analyst", "https://acme.com/1")
        original, data = p05.load_list(self.app_list)
        self.write_list(data["jobs"] + [{"n": 76, "company": "Other", "position": "Role", "status": "TODO"}])
        with self.assertRaises(p05.EnqueueError):
            p05.write_list(self.app_list, original, data, self.backup_dir, p05.datetime.now())


def tearDownModule():
    shutil.rmtree(_WS, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
