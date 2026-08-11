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
        golden = EVALS / doc["golden"]
        cand = out_dir / f"{did}.md"

        with cand.open("w") as fh:
            subprocess.run([str(args.pdf2md), str(pdf)], stdout=fh,
                           stderr=subprocess.DEVNULL, check=True)

        cmd = [sys.executable, str(EVALS / "tools/score.py"),
               "--golden", str(golden), "--candidate", str(cand),
               "--json-output", str(out_dir / f"{did}.score.json")]
        if doc.get("key_pattern"):
            cmd += ["--key-pattern", doc["key_pattern"]]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, check=True)

        score = json.loads((out_dir / f"{did}.score.json").read_text())
        score["id"] = did
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

    hdr = f"{'document':<30}{'pages':>6}{'chars%':>8}{'textsim':>9}{'keys':>6}{'exact%':>8}"
    print(hdr)
    print("-" * len(hdr))
    for r in results:
        keys = r.get("keys_in_golden", 0)
        exact = r.get("keys_exact_pct")
        print(f"{r['id']:<30}{r['golden_pages']:>6}{r['raw_chars_retained_pct']:>8.1f}"
              f"{r['text_similarity']:>9.4f}{keys:>6}"
              f"{('—' if exact is None else f'{exact:.1f}'):>8}")

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
