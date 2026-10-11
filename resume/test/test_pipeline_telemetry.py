"""Offline tests for script/pipeline_telemetry.py, telemetry_summary.py and the 02/03/04 hooks.

Only tempfile JSONL, synthetic envelopes and a fake `claude` executable written to a temp dir.
No real Claude worker, browser, network or production telemetry file: the workspace is a temp
dir, and stage 3 runs end to end with the offline mock review provider.

Run: python3 -m unittest discover -s test -p "test_*.py" -v      (from resume/)
"""
import contextlib
import importlib.util
import io
import json
import multiprocessing
import os
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "script"
REPO = SCRIPT.parents[1]
os.environ["LJP_CANDIDATE_DIR"] = str(REPO / "examples" / "candidate")  # fictional demo candidate
PROD_TELEMETRY = REPO / "workspace" / "resume" / ".runtime" / "telemetry" / "worker_usage.jsonl"
SENT = "SENTINEL_PAYLOAD_7f3a"  # planted in prompts / results / tool args; must never be recorded

_TMP = tempfile.TemporaryDirectory()
TMP = Path(_TMP.name)
os.environ["LJP_WORKSPACE"] = str(TMP / "workspace")  # before any stage / common.paths import
TEL = TMP / "usage.jsonl"
os.environ["PIPELINE_TELEMETRY_PATH"] = str(TEL)  # before any stage code runs
os.environ.pop("PIPELINE_TELEMETRY", None)
PROD_EXISTED = PROD_TELEMETRY.exists()

FAKE = TMP / "fake_claude.py"
FAKE.write_text("""#!/usr/bin/env python3
import os, sys, time
prompt = sys.stdin.read()
if os.environ.get("FAKE_MODE") == "sleep":
    time.sleep(5)
fx = os.environ.get("FAKE_FIXTURE")
if "（PREPARE 阶段）" in prompt: fx = os.environ.get("FAKE_PREPARE", fx)
if "（SCAN 阶段）" in prompt: fx = os.environ.get("FAKE_SCAN", fx)
sys.stdout.write(open(fx, encoding="utf-8").read() if fx else "")
sys.exit(int(os.environ.get("FAKE_RC", "0")))
""", encoding="utf-8")
FAKE.chmod(0o755)

if str(SCRIPT) not in sys.path:
    sys.path.insert(0, str(SCRIPT))


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, SCRIPT / file)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


import pipeline_telemetry as T  # noqa: E402
import telemetry_summary as S  # noqa: E402

p02 = _load("generate_v1", "02_generate_v1.py")
p03 = _load("review_resumes", "03_review_resumes.py")
p04 = _load("generate_final", "04_generate_final.py")
pcl = _load("generate_cover_letter", "generate_cover_letter.py")


def tearDownModule():
    _TMP.cleanup()


# ------------------------------------------------------------------ fixtures

USAGE = {"input_tokens": 12, "cache_creation_input_tokens": 3400, "cache_read_input_tokens": 15000,
         "output_tokens": 987, "server_tool_use": {"web_search_requests": 0}, "service_tier": "standard",
         "cache_creation": {"ephemeral_5m_input_tokens": 3400, "ephemeral_1h_input_tokens": 0}}
MU = {"claude-sonnet-5-5": {"inputTokens": 12, "outputTokens": 987, "cacheReadInputTokens": 15000,
                            "cacheCreationInputTokens": 3400, "costUSD": 0.0123},
      "claude-haiku-5-5": {"inputTokens": 300, "outputTokens": 20, "costUSD": 0.0001}}


def envelope(result, **kw):
    e = {"type": "result", "subtype": "success", "is_error": False, "num_turns": 3, "duration_ms": 999,
         "result": result, "session_id": "s", "total_cost_usd": 0.0124, "usage": USAGE, "modelUsage": MU,
         "permission_denials": [{"tool_name": "Write", "tool_input": {"content": SENT}}]}
    e.update(kw)
    return e


POS = "Data Analyst"
RESUME = ("Morgan Ellis\nToronto\nemail\nDATA ANALYST\nSUMMARY OF QUALIFICATIONS\nx " + SENT +
          "\nCORE SKILLS\nPROFESSIONAL EXPERIENCE\nFabrikam Health Analytics\nNorthwind Freight Co.\n"
          "PROJECT EXPERIENCE\nEDUCATION\nLakeshore University\nRiverside Institute of Technology\n")
JOB = {"company": "Acme", "position": POS, "jd": "JD " + SENT}
JR = {"hard_skills": {"matched": [{"skill": "SQL", "resume_count": 1, "jd_count": 2}], "missing": []},
      "soft_skills": {"matched": [], "missing": []}}

_n = 0


def fixture(obj) -> str:
    global _n
    _n += 1
    p = TMP / f"fx_{_n}.txt"
    p.write_text(obj if isinstance(obj, str) else json.dumps(obj), encoding="utf-8")
    return str(p)


def fake_env(**e):
    for k in ("FAKE_FIXTURE", "FAKE_RC", "FAKE_MODE", "FAKE_PREPARE", "FAKE_SCAN"):
        os.environ.pop(k, None)
    os.environ.update({k: str(v) for k, v in e.items()})


def records(path=TEL):
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def stream(result_obj, tools, model="claude-opus-5-5"):
    ev = [{"type": "system", "subtype": "init", "model": model, "tools": ["Read"], "cwd": "/x"}]
    for t in tools:
        ev.append({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": t, "input": {"function": "() => '" + SENT + "'"}}]}})
        ev.append({"type": "user", "message": {"content": [{"type": "tool_result", "content": SENT}]}})
    ev.append(envelope(json.dumps(result_obj), num_turns=len(tools) + 1))
    return "\n".join(json.dumps(e) for e in ev) + "\n"


def event(**kw):
    """A synthetic telemetry record with every field null unless given."""
    r = {"schema_version": 1, "invocation_id": kw.pop("invocation_id", T.new_invocation_id()),
         "run_id": "r1", "stage": "02_v1", "batch": "B", "job_folder": "J", "attempt": 1,
         "cli_status": "SUCCESS", "validation_status": "VALID", "tool_counts": {},
         **{f: None for f in S.FIELDS}}
    r.update(kw)
    return r


def write_events(path, evs):
    path.write_text("".join(json.dumps(e) + "\n" for e in evs), encoding="utf-8")


class Base(unittest.TestCase):
    def setUp(self):
        if TEL.exists():
            TEL.unlink()
        os.environ["PIPELINE_TELEMETRY_PATH"] = str(TEL)
        os.environ.pop("PIPELINE_TELEMETRY", None)

    def tearDown(self):
        text = TEL.read_text(encoding="utf-8") if TEL.exists() else ""
        self.assertNotIn(SENT, text)
        self.assertNotIn("tool_input", text)
        self.assertEqual(PROD_TELEMETRY.exists(), PROD_EXISTED, "production telemetry file touched")


# ------------------------------------------------------------- usage fields

class TestUsageFields(Base):
    def test_snake_case(self):
        f = T.usage_fields(envelope("x"))
        self.assertEqual((f["input_tokens"], f["cache_creation_input_tokens"], f["cache_read_input_tokens"],
                          f["output_tokens"], f["num_turns"], f["total_cost_usd"]), (12, 3400, 15000, 987, 3, 0.0124))
        self.assertEqual(f["model_actual"], "claude-sonnet-5-5")
        self.assertEqual(f["usage_raw"]["cache_creation"]["ephemeral_5m_input_tokens"], 3400)

    def test_camel_case(self):
        env = {"usage": {"inputTokens": 1, "cacheCreationInputTokens": 2, "cacheReadInputTokens": 3,
                         "outputTokens": 4}, "numTurns": 5, "totalCostUSD": 0.5,
               "model_usage": {"m-small": {"output_tokens": 1}, "m-big": {"output_tokens": 9}}}
        f = T.usage_fields(env)
        self.assertEqual([f[k] for k in T.USAGE_FIELDS], [1, 2, 3, 4])
        self.assertEqual((f["num_turns"], f["total_cost_usd"]), (5, 0.5))
        self.assertEqual(f["model_actual"], "m-big")
        self.assertEqual(set(f["model_usage_raw"]), {"m-small", "m-big"})

    def test_snake_preferred_over_camel(self):
        env = {"usage": {"input_tokens": 10, "inputTokens": 99, "output_tokens": None, "outputTokens": 7},
               "num_turns": 2, "numTurns": 8, "model_usage": {"a": {"outputTokens": 1}},
               "modelUsage": {"b": {"outputTokens": 5}}}
        f = T.usage_fields(env)
        self.assertEqual(f["input_tokens"], 10)
        self.assertEqual(f["output_tokens"], 7)       # snake value absent (null) -> camel used
        self.assertEqual(f["num_turns"], 2)
        self.assertEqual(f["model_actual"], "a")      # model_usage (snake) wins over modelUsage

    def test_model_usage_both_spellings_and_output_keys(self):
        for key in ("modelUsage", "model_usage"):
            for out in ("outputTokens", "output_tokens"):
                f = T.usage_fields({key: {"x": {out: 1}, "y": {out: 50}}})
                self.assertEqual(f["model_actual"], "y", (key, out))
        self.assertEqual(T.usage_fields({}, init_model="from-init")["model_actual"], "from-init")

    def test_each_token_field_independently_missing(self):
        full = {"input_tokens": 1, "cache_creation_input_tokens": 2, "cache_read_input_tokens": 3,
                "output_tokens": 4}
        for missing in T.USAGE_FIELDS:
            usage = {k: v for k, v in full.items() if k != missing}
            f = T.usage_fields({"usage": usage})
            self.assertIsNone(f[missing], missing)
            for k in T.USAGE_FIELDS:
                if k != missing:
                    self.assertEqual(f[k], full[k])
        empty = T.usage_fields(None)
        self.assertTrue(all(empty[k] is None for k in T.USAGE_FIELDS + ("num_turns", "total_cost_usd")))
        self.assertEqual(T.usage_fields({"usage": {"input_tokens": True}})["input_tokens"], None)

    def test_raw_counters_drop_free_text(self):
        f = T.usage_fields({"usage": {"input_tokens": 1, "note": SENT * 3, "tier": "standard"}})
        self.assertNotIn("note", f["usage_raw"])
        self.assertEqual(f["usage_raw"]["tier"], "standard")


# --------------------------------------------------------------- summary

class TestSummary(Base):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, "-I", str(SCRIPT / "telemetry_summary.py"), *args],
                              capture_output=True, text=True, timeout=60)

    def test_missing_cache_read_not_counted_as_zero(self):
        p = TMP / "s1.jsonl"
        write_events(p, [event(input_tokens=10, cache_read_input_tokens=1000, output_tokens=5, job_folder="A"),
                         event(input_tokens=20, output_tokens=6, job_folder="B")])
        rep = S.summarize(S.load(p, None, None)[0])
        cr = rep["coverage"]["cache_read_input_tokens"]
        self.assertEqual((cr["sum"], cr["n"], cr["of"], cr["partial"]), (1000, 1, 2, True))
        self.assertEqual(rep["coverage"]["input_tokens"], {"sum": 30, "n": 2, "of": 2, "partial": False})
        cc = rep["coverage"]["cache_creation_input_tokens"]
        self.assertEqual((cc["sum"], cc["n"], cc["partial"]), (None, 0, True))  # no data -> null, not 0
        self.assertEqual(rep["totals"]["per_job"]["cache_read_input_tokens"], {"avg": 500.0, "partial": True})
        text = io.StringIO()
        with contextlib.redirect_stdout(text):
            S.print_text({"path": str(p), "filters": {}, "matched_events": 2, "duplicate_events": 0,
                          "unreadable_lines": 0, **rep})
        self.assertIn("cache_read_input_tokens: 1000 (coverage 1/2) PARTIAL", text.getvalue())
        self.assertIn("cache_creation_input_tokens: null (coverage 0/2) PARTIAL", text.getvalue())

    def test_partial_totals(self):
        p = TMP / "s2.jsonl"
        write_events(p, [event(input_tokens=1, cache_creation_input_tokens=2, cache_read_input_tokens=3,
                               output_tokens=4, duration_ms=100),
                         event(input_tokens=1, cache_creation_input_tokens=2, output_tokens=4, duration_ms=50)])
        t = S.summarize(S.load(p, None, None)[0])["totals"]
        self.assertEqual(t["uncached_input_plus_output"], {"sum": 10, "partial": False})
        self.assertEqual(t["all_input_plus_output"], {"sum": 17, "partial": True})
        self.assertEqual(t["total_duration_ms"], {"sum": 150, "partial": False})
        write_events(p, [event(input_tokens=1, cache_creation_input_tokens=2, cache_read_input_tokens=3,
                               output_tokens=4)])
        t = S.summarize(S.load(p, None, None)[0])["totals"]
        self.assertEqual(t["all_input_plus_output"], {"sum": 10, "partial": False})

    def test_duplicate_invocation_counted_once(self):
        p = TMP / "s3.jsonl"
        e = event(invocation_id="dup-1", output_tokens=100)
        write_events(p, [e, e, event(output_tokens=1)])
        with p.open("a") as f:
            f.write("{not json\n")
        rows, bad, dup = S.load(p, None, None)
        self.assertEqual((len(rows), bad, dup), (2, 1, 1))
        self.assertEqual(S.summarize(rows)["coverage"]["output_tokens"]["sum"], 101)

    def test_json_output_pure_and_complete(self):
        p = TMP / "s4.jsonl"
        write_events(p, [
            event(stage="02_v1", batch="B1", job_folder="J1", run_id="r1", input_tokens=5, output_tokens=5),
            event(stage="02_v1", batch="B1", job_folder="J1", run_id="r1", attempt=2, input_tokens=5),
            event(stage="03_prepare", batch="B1", job_folder="J1", run_id="r2",
                  tool_counts={"Read": 1, "evaluate_script": 1}),
            event(stage="03_scan", batch="B1", job_folder="J1", run_id="r2", tool_counts={"evaluate_script": 2}),
            event(stage="04_v2", batch="B1", job_folder="J1", run_id="r3"),
            event(stage="04_trim", batch="B1", job_folder="J1", run_id="r3", trim_iteration=1),
            event(stage="04_v2", batch="B2", job_folder="J2", run_id="r4"),
        ])
        for args in ([], ["--batch", "B1"], ["--run-id", "r2"]):
            r = self.run_cli("--path", str(p), "--json", *args)
            self.assertEqual(r.returncode, 0, r.stderr)
            rep = json.loads(r.stdout)  # the whole stdout is exactly one JSON object
            self.assertIsInstance(rep, dict)
            for k in ("path", "filters", "matched_events", "unreadable_lines", "unique_jobs", "stages",
                      "models", "totals", "coverage", "trim_distribution", "tool_counts"):
                self.assertIn(k, rep)
            self.assertEqual(rep["stages"]["01_prepare"], {"model_calls": 0, "reason": "pure_python"})
            self.assertEqual(rep["stages"]["05_enqueue"], {"model_calls": 0, "reason": "pure_python"})
        rep = json.loads(self.run_cli("--path", str(p), "--json", "--batch", "B1").stdout)
        self.assertEqual((rep["matched_events"], rep["unique_jobs"]), (6, 1))
        self.assertEqual(rep["totals"]["retries"], 1)
        self.assertEqual(rep["totals"]["invocations_per_job"], 6.0)
        self.assertEqual(rep["trim_distribution"], {"1": 1})
        self.assertEqual(rep["tool_counts"]["total"], {"Read": 1, "evaluate_script": 3})
        self.assertEqual(json.loads(self.run_cli("--path", str(p), "--json", "--run-id", "r2").stdout)
                         ["matched_events"], 2)
        a, b = (self.run_cli("--path", str(p), "--json").stdout for _ in range(2))
        self.assertEqual(a, b)  # stable output
        missing = self.run_cli("--path", str(TMP / "nope.jsonl"), "--json")
        self.assertEqual(json.loads(missing.stdout)["error"], "no telemetry file")
        text = self.run_cli("--path", str(p))
        self.assertIn("[01_prepare] 0 model calls (pure Python)", text.stdout)


# ---------------------------------------------------------- writer / locking

def _append_many(path, n_threads, n):
    os.environ["PIPELINE_TELEMETRY_PATH"] = path
    ts = [threading.Thread(target=lambda: [T.append({"p": os.getpid(), "i": i, "pad": "x" * 3000})
                                            for i in range(n)]) for _ in range(n_threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()


class TestWriter(Base):
    def test_concurrent_append_processes_and_threads(self):
        ctx = multiprocessing.get_context("fork")
        ps = [ctx.Process(target=_append_many, args=(str(TEL), 8, 100)) for _ in range(4)]
        for p in ps:
            p.start()
        for p in ps:
            p.join()
        lines = TEL.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 3200)
        self.assertTrue(all(json.loads(l)["pad"] == "x" * 3000 for l in lines))

    def test_disabled(self):
        os.environ["PIPELINE_TELEMETRY"] = "0"
        T.append({"a": 1})
        self.assertFalse(TEL.exists())

    def test_write_failure_is_a_warning_only(self):
        os.environ["PIPELINE_TELEMETRY_PATH"] = "/dev/null/sub/usage.jsonl"
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            T.append({"x": SENT})
        self.assertTrue(err.getvalue().startswith("warning: telemetry write failed"))
        self.assertNotIn(SENT, err.getvalue())

    def test_emit_once(self):
        inv = T.Invocation("02_v1", ["claude"])
        inv.emit()
        inv.emit()
        rec = records()
        self.assertEqual(len(rec), 1)
        self.assertEqual((rec[0]["cli_status"], rec[0]["error_type"]), ("ERROR", "aborted"))


# ------------------------------------------------------------ stage hooks

class TestStage02(Base):
    job = types.SimpleNamespace(data=JOB, folder="Acme_Data_Analyst", label="Acme — Data Analyst")

    def test_valid(self):
        fake_env(FAKE_FIXTURE=fixture(envelope(json.dumps({"resume_v1": RESUME}))))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            r = p02.generate_with_retry(self.job, p02.worker_command(str(FAKE), "claude-sonnet-5-5"), 30, "B")
        self.assertEqual(r, RESUME)
        self.assertEqual(out.getvalue(), "")
        (rec,) = records()
        self.assertEqual((rec["stage"], rec["batch"], rec["job_folder"], rec["attempt"], rec["cli_status"],
                          rec["validation_status"], rec["model_requested"], rec["output_tokens"]),
                         ("02_v1", "B", "Acme_Data_Analyst", 1, "SUCCESS", "VALID", "claude-sonnet-5-5", 987))

    def test_invalid_retry_stdout_unchanged(self):
        fake_env(FAKE_FIXTURE=fixture(envelope("not json " + SENT)))

        def run():
            o = io.StringIO()
            with contextlib.redirect_stdout(o):
                with self.assertRaises(p02.JobError):
                    p02.generate_with_retry(self.job, p02.worker_command(str(FAKE)), 30, "B")
            return o.getvalue()
        on = run()
        os.environ["PIPELINE_TELEMETRY"] = "0"
        off = run()
        self.assertEqual(on, off)
        rec = records()
        self.assertEqual([(r["attempt"], r["validation_status"], r["error_type"]) for r in rec],
                         [(1, "INVALID", "invalid_result"), (2, "INVALID", "invalid_result")])
        self.assertEqual(len({r["invocation_id"] for r in rec}), 2)

    def test_error_malformed_timeout(self):
        cmd = p02.worker_command(str(FAKE))
        cases = [({"FAKE_FIXTURE": fixture(envelope("x", is_error=True, subtype="error_during_execution")),
                   "FAKE_RC": 1}, 30, ("ERROR", "NOT_REACHED", "nonzero_exit")),
                 ({"FAKE_FIXTURE": fixture("<<not json>>")}, 30, ("MALFORMED_OUTPUT", "NOT_REACHED", "invalid_envelope")),
                 ({"FAKE_FIXTURE": fixture("{}"), "FAKE_MODE": "sleep"}, 1, ("TIMEOUT", "NOT_REACHED", "timeout"))]
        for env, timeout, want in cases:
            if TEL.exists():
                TEL.unlink()
            fake_env(**env)
            inv = T.Invocation("02_v1", cmd)
            with self.assertRaises(p02.JobError):
                p02.generate_once(JOB, cmd, timeout, inv)
            inv.emit()
            (rec,) = records()
            self.assertEqual((rec["cli_status"], rec["validation_status"], rec["error_type"]), want)


class TestStage03(Base):
    """Stage 3 calls a review provider, never a model: no worker, no model telemetry."""

    def _batch(self, root: Path):
        batch, folder = "2026-10-10_1200", "Data_Analyst__Acme__0001"
        job_json = root / batch / folder / "job.json"
        job_json.parent.mkdir(parents=True)
        job_json.write_text(json.dumps({**JOB, "jd": "Python, SQL, Tableau and forecasting.",
                                        "apply_url": "https://x.test/1", "source_key": "https://x.test/1",
                                        "resume_v1": RESUME}), encoding="utf-8")
        row = {c: "" for c in p02.p01.TRACKER_COLUMNS}
        row.update(batch=batch, job_folder=folder, company="Acme", position=POS, v1_status="DONE",
                   review_status="PENDING")
        p02.p01.write_tracker_atomic(root / p02.p01.TRACKER_NAME, [row])
        manifest = root / "v1_ready.json"
        manifest.write_text(json.dumps({"batch": batch, "generated_at": "now", "job_count": 1,
                                        "jobs": [{"job_folder": folder, "job_json": str(job_json)}]}))
        return batch, job_json, manifest

    def test_no_worker_and_no_model_telemetry(self):
        for name in ("worker_command", "run_worker", "process_job"):
            self.assertFalse(hasattr(p03, name), name)
        self.assertFalse(p03.EVENTS_PATH.is_relative_to(REPO), "test must not write into the repository")
        root = TMP / "jobs03"
        batch, job_json, manifest = self._batch(root)
        argv = ["--input", str(manifest), "--jobs-root", str(root), "--provider", "mock", "--poll-interval", "0"]
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(p03.main(argv), 0, out.getvalue())
        data = json.loads(job_json.read_text())
        self.assertIsNone(p03.validate_feedback(data["quality_feedback"]))
        self.assertEqual(data["quality_review"]["provider"], "mock")
        ready = json.loads(p03.feedback_path(batch).read_text())
        self.assertEqual(ready["job_count"], 1)
        self.assertEqual(records(), [])                          # nothing in worker telemetry
        events = [json.loads(l) for l in p03.EVENTS_PATH.read_text().splitlines()]
        self.assertEqual([(e["outcome"], e["submit_calls"]) for e in events], [("COMMITTED", 1)])
        with contextlib.redirect_stdout(io.StringIO()):      # rerun: checkpoint, no provider call
            self.assertEqual(p03.main(argv), 0)
        self.assertEqual(len(p03.EVENTS_PATH.read_text().splitlines()), 1)

    def test_summary_reports_03_as_zero_model(self):
        p = TMP / "s03.jsonl"
        write_events(p, [event(stage="02_v1", batch="B9", job_folder="J1", output_tokens=5),
                         event(stage="03_scan", batch="B9", job_folder="J1", tool_counts={"Read": 1})])
        ev = TMP / "review03.jsonl"
        ev.write_text("\n".join(json.dumps(r) for r in [
            {"batch": "B9", "job_folder": "J1", "provider": "mock", "outcome": "COMMITTED",
             "submit_calls": 1, "collect_calls": 2, "reconciled": False},
            {"batch": "OTHER", "job_folder": "J2", "outcome": "MANUAL_RECOVERY_REQUIRED", "submit_calls": 1}]) + "\n")
        r = subprocess.run([sys.executable, "-I", str(SCRIPT / "telemetry_summary.py"), "--path", str(p),
                            "--review-path", str(ev), "--batch", "B9", "--json"],
                           capture_output=True, text=True, timeout=60, env={**os.environ})
        rep = json.loads(r.stdout)
        st = rep["stages"]["03_review"]
        self.assertEqual((st["model_calls"], st["model_tokens"], st["reason"]), (0, 0, "external_provider"))
        self.assertEqual((st["review"]["job_runs"], st["review"]["submit_calls"], st["review"]["collect_calls"]), (1, 1, 2))
        self.assertTrue(rep["stages"]["03_scan"]["legacy"])     # older model-driven 03 events stay visible
        text = subprocess.run([sys.executable, "-I", str(SCRIPT / "telemetry_summary.py"), "--path", str(p),
                               "--review-path", str(ev)], capture_output=True, text=True, timeout=60).stdout
        self.assertIn("[03_review] 0 model calls, 0 tokens (external review provider)", text)


class TestStage04AndCoverLetter(Base):
    def test_v2_and_trim(self):
        job = types.SimpleNamespace(data={**JOB, "resume_v1": RESUME, "quality_feedback": JR},
                                    folder="Acme_Data_Analyst", label="Acme")
        ctx = {"batch": "B4", "timeout": 30,
               "v2_cmd": p04.worker_command("resume-v2-worker", str(FAKE), None),
               "trim_cmd": p04.worker_command("resume-trim-worker", str(FAKE), "claude-haiku-5-5")}
        saved = p04.validate_resume_v2_initial, p04.validate_resume_v2_trimmed
        p04.validate_resume_v2_initial = lambda r: None if r.get("ok") else "bad"
        p04.validate_resume_v2_trimmed = lambda new, prev: None if new.get("ok") else "bad trim"
        try:
            fake_env(FAKE_FIXTURE=fixture(envelope(json.dumps({"ok": True, "summary": SENT}))))
            with contextlib.redirect_stdout(io.StringIO()):
                p04.generate_v2(job, ctx)
                p04.trim_once(job, ctx, {"ok": True}, {"trim_iteration": 2, "overflow_text": [SENT]})
            fake_env(FAKE_FIXTURE=fixture(envelope(json.dumps({"ok": False}))))
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(p04.JobError):
                p04.trim_once(job, ctx, {"ok": True}, {"trim_iteration": 3})
        finally:
            p04.validate_resume_v2_initial, p04.validate_resume_v2_trimmed = saved
        rec = records()
        self.assertEqual([(r["stage"], r["attempt"], r["trim_iteration"], r["validation_status"]) for r in rec],
                         [("04_v2", 1, None, "VALID"), ("04_trim", 1, 2, "VALID"),
                          ("04_trim", 1, 3, "INVALID"), ("04_trim", 2, 3, "INVALID")])
        self.assertEqual((rec[0]["model_requested"], rec[1]["model_requested"]), ("inherit", "claude-haiku-5-5"))

    def test_cover_letter_helper_compatible(self):
        fake_env(FAKE_FIXTURE=fixture(envelope(json.dumps({"opening": "a"}))))
        self.assertEqual(p04.call_worker([str(FAKE)], "prompt", 30), {"opening": "a"})
        self.assertFalse(TEL.exists())  # the cover-letter path records nothing
        self.assertEqual(pcl.p04.call_worker.__code__.co_varnames[3], "tele")
        self.assertEqual(pcl.MAX_ATTEMPTS, p04.MAX_ATTEMPTS)
        fake_env(FAKE_FIXTURE=fixture("garbage"))
        with self.assertRaises(p04.JobError):
            p04.call_worker([str(FAKE)], "prompt", 30)


if __name__ == "__main__":
    unittest.main()
