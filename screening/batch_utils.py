"""Shared batch writing for the split_*.py splitters (one per discovery source):
split_greenhouse.py / split_ashby.py.

Each splitter finds its own source file (naming and selection differ per platform) and calls
write_and_verify(); this module chunks the jobs, replaces today's batch files, verifies them
against the source and prints the summary.

Batch file: <output_dir>/<date>_<platform>_batch_NNN.json
Payload:    {platform, source_file, [extra fields], date, batch_index, batch_count, job_count, jobs}
"""
import json
from collections import Counter

BATCH_SIZE = 10


def canonical(job):
    return json.dumps(job, ensure_ascii=False, sort_keys=True)


def verify(source_jobs, batch_files, job_key):
    """Assert the batches hold exactly the source jobs, in order; return duplicate job_key values."""
    batched = []
    for f in batch_files:
        chunk = json.loads(f.read_text(encoding="utf-8"))["jobs"]
        assert 0 < len(chunk) <= BATCH_SIZE, f"{f.name}: bad batch size {len(chunk)}"
        batched.extend(chunk)

    assert len(batched) == len(source_jobs), f"count mismatch {len(batched)} != {len(source_jobs)}"
    assert [canonical(j) for j in batched] == [canonical(j) for j in source_jobs], \
        "job content or order changed"
    src, out = Counter(map(canonical, source_jobs)), Counter(map(canonical, batched))
    assert not (src - out), "jobs lost"
    assert not (out - src), "jobs duplicated"

    dup_keys = [k for k, c in Counter(j.get(job_key) for j in source_jobs).items() if c > 1]
    return dup_keys


def write_and_verify(jobs, platform, today, source, output_dir, job_key, extra=None):
    """Replace today's batch files for `platform` with `jobs` split into BATCH_SIZE chunks.

    `extra` holds platform-specific payload fields, placed right after source_file.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    for old in output_dir.glob(f"{today}_{platform}_batch_*.json"):
        old.unlink()

    chunks = [jobs[i:i + BATCH_SIZE] for i in range(0, len(jobs), BATCH_SIZE)]
    batch_files = []
    for n, chunk in enumerate(chunks, 1):
        out = output_dir / f"{today}_{platform}_batch_{n:03d}.json"
        payload = {
            "platform": platform,
            "source_file": str(source),
            **(extra or {}),
            "date": today,
            "batch_index": n,
            "batch_count": len(chunks),
            "job_count": len(chunk),
            "jobs": chunk,
        }
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        batch_files.append(out)

    dup_keys = verify(jobs, batch_files, job_key)

    print(f"source: {source}")
    print(f"jobs: {len(jobs)}  batches: {len(chunks)}  sizes: {[len(c) for c in chunks]}")
    for f in batch_files:
        print(f"  {f.relative_to(output_dir.parent.parent)}")
    print("verification: PASSED (count, no loss, no duplication, content unchanged)")
    if dup_keys:
        print(f"note: source itself has {len(dup_keys)} duplicate {job_key} value(s)")
