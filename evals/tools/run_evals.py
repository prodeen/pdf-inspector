#!/usr/bin/env python3
"""Run the whole Prodeen eval corpus and report per-document scores.

Reads `evals/manifest.jsonl`, extracts each PDF with the release `pdf2md`,
scores it against its golden reference, and prints a table. Candidate markdown
is regenerated every run and never committed — the goldens and the PDFs are the
durable artifacts.

  cargo build --release
  python3 evals/tools/run_evals.py
  python3 evals/tools/run_evals.py --doc eurlex-396-consolidated --keep

Exit code is nonzero if any document scores below its manifest `gate`, so this
can be wired into CI once the numbers stop moving.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EVALS = ROOT / "evals"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf2md", type=Path, default=ROOT / "target/release/pdf2md")
    ap.add_argument("--detect-pdf", type=Path, default=ROOT / "target/release/detect-pdf")
    ap.add_argument("--doc", help="run a single document id")
    ap.add_argument("--keep", action="store_true",
                    help="keep generated candidate markdown for inspection")
    ap.add_argument("--json-output", type=Path)
    args = ap.parse_args()

    if not args.pdf2md.exists():
        print(f"missing {args.pdf2md} — run: cargo build --release", file=sys.stderr)
        return 1

    manifest = EVALS / "manifest.jsonl"
    docs = [json.loads(l) for l in manifest.read_text().splitlines() if l.strip()]
    if args.doc:
        docs = [d for d in docs if d["id"] == args.doc]
        if not docs:
            print(f"no document with id {args.doc}", file=sys.stderr)
            return 1

    out_dir = EVALS / "candidate"
    out_dir.mkdir(exist_ok=True)
    results, failures = [], []

    for doc in docs:
        did = doc["id"]
        pdf = EVALS / doc["pdf"]
        cand = out_dir / f"{did}.md"

        # Never `check=True` here. A document that fails to extract is exactly
        # what this corpus exists to catch — crashing the run with a traceback
        # would hide it and take every later document down with it. pdf2md
        # exits 2 on "this PDF needs OCR", which is a verdict, not a crash.
        with cand.open("w") as fh:
            rc = subprocess.run([str(args.pdf2md), str(pdf)], stdout=fh,
                                stderr=subprocess.DEVNULL, check=False).returncode

        expect = doc.get("expect") or {}
        if expect.get("pdf_type"):
            det = subprocess.run([str(args.detect_pdf), str(pdf), "--analyze", "--json"],
                                 capture_output=True, check=False)
            actual_type = (json.loads(det.stdout).get("pdf_type")
                           if det.returncode == 0 else f"<detect-pdf exit {det.returncode}>")
            if actual_type != expect["pdf_type"]:
                failures.append(f"{did}: pdf_type {actual_type!r} != expected "
                                f"{expect['pdf_type']!r}")
        if "exit_code" in expect:
            if rc != expect["exit_code"]:
                failures.append(f"{did}: pdf2md exit {rc} != expected {expect['exit_code']}")
        elif rc != 0:
            failures.append(f"{did}: pdf2md exited {rc} (extraction failed)")

        # Classification-only documents have no text layer, so there is nothing
        # for a text golden to compare against; their gate is `expect`.
        if doc.get("scoring") == "classification_only":
            results.append({"id": did, "golden_pages": 0, "raw_chars_retained_pct": 0.0,
                            "text_similarity": 0.0, "classification_only": True,
                            "exit_code": rc})
            if not args.keep:
                cand.unlink(missing_ok=True)
            continue

        golden = EVALS / doc["golden"]
        cmd = [sys.executable, str(EVALS / "tools/score.py"),
               "--golden", str(golden), "--candidate", str(cand),
               "--json-output", str(out_dir / f"{did}.score.json")]
        if doc.get("key_pattern"):
            cmd += ["--key-pattern", doc["key_pattern"]]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, check=True)

        score = json.loads((out_dir / f"{did}.score.json").read_text())
        score["id"] = did
        score["exit_code"] = rc
        results.append(score)

        gate = doc.get("gate") or {}
        for metric, floor in gate.items():
            actual = score.get(metric)
            if actual is None:
                failures.append(f"{did}: gate metric {metric!r} not produced")
            elif actual < floor:
                failures.append(f"{did}: {metric} {actual} < gate {floor}")

        if not args.keep:
            cand.unlink(missing_ok=True)

    # `valued%` — not `exact%` — is the column to read. It counts only golden
    # rows that carry at least one value, so a document that lost every number
    # cannot post a respectable score off rows that never had one.
    # `words%` is the only column that sees lost inter-word spacing; chars% and
    # textsim both normalise whitespace away and rate such a document healthy.
    hdr = (f"{'document':<30}{'pages':>6}{'chars%':>8}{'words%':>8}{'textsim':>9}"
           f"{'keys':>6}{'valued%':>8}")
    print(hdr)
    print("-" * len(hdr))
    for r in results:
        if r.get("classification_only"):
            print(f"{r['id']:<30}{'—':>6}{'—':>8}{'—':>8}{'—':>9}{'—':>6}"
                  f"{'  (classification only)':>8}")
            continue
        keys = r.get("valued_keys", 0)
        valued = r.get("valued_keys_exact_pct")
        print(f"{r['id']:<30}{r['golden_pages']:>6}{r['raw_chars_retained_pct']:>8.1f}"
              f"{r['word_boundary_retained_pct']:>8.1f}"
              f"{r['text_similarity']:>9.4f}{keys:>6}"
              f"{('—' if valued is None else f'{valued:.1f}'):>8}")

    if args.json_output:
        args.json_output.write_text(json.dumps(results, indent=2) + "\n")

    if failures:
        print("\nGATE FAILURES:")
        for f in failures:
            print(f"  {f}")
        return 1
    print("\nall gates passed" if any(d.get("gate") for d in docs) else "\nno gates configured")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
