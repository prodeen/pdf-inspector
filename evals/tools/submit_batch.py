#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["google-genai"]
# ///
"""Submit a prepared golden-sample batch, poll it, and write per-page markdown.

Takes the GCS prefix that `prepare_batch.py` printed. Refuses to submit if the
uploaded JSONL request count doesn't match `--expect`, so a job can never
silently run over more pages than were reviewed and priced.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from google import genai
from google.genai import types

PROJECT = "upbeat-object-453314-n3"
MODEL = "gemini-3-flash-preview"
INPUT_PRICE_PER_M = 0.25
OUTPUT_PRICE_PER_M = 1.50


def gcs_cat(uri: str) -> str:
    return subprocess.run(["gcloud", "storage", "cat", uri],
                          check=True, capture_output=True, text=True).stdout


def gcs_ls(uri: str) -> list[str]:
    r = subprocess.run(["gcloud", "storage", "ls", uri], capture_output=True, text=True)
    return [l.strip() for l in r.stdout.strip().split("\n") if l.strip()] if r.returncode == 0 else []


def strip_fences(text: str) -> str:
    if not text.startswith("```"):
        return text
    lines = text.split("\n")[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True, help="gs://.../<job-id> prefix")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--expect", type=int, required=True,
                    help="exact request count the uploaded JSONL must contain")
    args = ap.parse_args()

    base = args.base.rstrip("/")
    gcs_input, gcs_output = f"{base}/input.jsonl", f"{base}/output"

    n = len([l for l in gcs_cat(gcs_input).strip().split("\n") if l.strip()])
    if n != args.expect:
        print(f"REFUSING: {gcs_input} has {n} requests, expected {args.expect}", file=sys.stderr)
        return 1
    print(f"input verified: {n} requests")

    client = genai.Client(vertexai=True, project=PROJECT, location="global")
    job = client.batches.create(
        model=MODEL,
        src=types.BatchJobSource(gcs_uri=[gcs_input], format="jsonl"),
        config=types.CreateBatchJobConfig(
            display_name=base.rsplit("/", 1)[-1],
            dest=types.BatchJobDestination(gcs_uri=gcs_output, format="jsonl"),
        ),
    )
    print(f"submitted: {job.name}\nstate: {job.state}")

    start = time.time()
    while not any(s in str(job.state) for s in ("SUCCEEDED", "FAILED", "CANCELLED")):
        time.sleep(15)
        job = client.batches.get(name=job.name)
        m, s = divmod(int(time.time() - start), 60)
        st = job.completion_stats
        done = f" | {(st.successful_count or 0) + (st.failed_count or 0)}/{n}" if st else ""
        print(f"  [{m}m{s:02d}s] {job.state}{done}", flush=True)

    print(f"\nfinished: {job.state}")
    if "SUCCEEDED" not in str(job.state):
        print(f"error: {job.error}", file=sys.stderr)
        return 1

    args.out.mkdir(parents=True, exist_ok=True)
    in_tok = out_tok = ok = err = 0
    dest = job.dest.gcs_uri if job.dest else gcs_output
    for f in [f for f in gcs_ls(f"{dest}/**") if f.endswith(".jsonl")]:
        for line in [l for l in gcs_cat(f).strip().split("\n") if l.strip()]:
            d = json.loads(line)
            key = d.get("key", "")
            if d.get("error") or not key:
                print(f"  {key or '?'}: ERROR {str(d.get('error'))[:160]}")
                err += 1
                continue
            resp = d.get("response", {})
            cands = resp.get("candidates", [])
            if not cands:
                print(f"  {key}: no candidates")
                err += 1
                continue
            text = "".join(p["text"] for p in cands[0].get("content", {}).get("parts", [])
                           if "text" in p)
            (args.out / f"{key}.md").write_text(strip_fences(text).strip() + "\n")
            u = resp.get("usageMetadata", {})
            in_tok += u.get("promptTokenCount", 0)
            out_tok += u.get("candidatesTokenCount", 0)
            ok += 1

    cost = in_tok / 1e6 * INPUT_PRICE_PER_M + out_tok / 1e6 * OUTPUT_PRICE_PER_M
    print(f"\npages written : {ok}   errors: {err}   -> {args.out}")
    print(f"tokens        : in {in_tok:,}  out {out_tok:,}")
    print(f"ACTUAL COST   : ${cost:.4f}")
    return 0 if err == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
