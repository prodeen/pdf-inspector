#!/usr/bin/env python3
"""Sample contiguous page sequences from a PDF, build an excerpt, render page images.

Stratifies by the page classes `detect-pdf --analyze --json` reports, so a
golden reference covers prose, tables and multi-column layout rather than
whatever happens to sit at the front of the document. Seeded: the same seed
and the same analysis always select the same pages, so a golden stays
reproducible and reviewable.

  ./sample_excerpt.py doc.pdf --analysis a.json --out ./work --seed 20260805
"""

from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
from pathlib import Path


def _stratify(analysis: dict) -> dict[str, list[int]]:
    """Bucket 1-indexed pages by layout class, richest class first.

    A page belongs to exactly one bucket so the sampler cannot draw the same
    page twice through two different strata.
    """
    n = analysis["page_count"]
    tables = set(analysis.get("pages_with_tables", []))
    columns = set(analysis.get("pages_with_columns", []))
    strata: dict[str, list[int]] = {
        "table_and_columns": [],
        "table_only": [],
        "columns_only": [],
        "prose": [],
    }
    for page in range(1, n + 1):
        t, c = page in tables, page in columns
        key = (
            "table_and_columns"
            if t and c
            else "table_only" if t else "columns_only" if c else "prose"
        )
        strata[key].append(page)
    return {k: v for k, v in strata.items() if v}


def _draw(strata: dict[str, list[int]], runs: int, run_len: int, rng: random.Random,
          total_pages: int) -> list[tuple[int, int]]:
    """Pick `runs` contiguous sequences, spreading them across strata.

    Anchors are drawn from a stratum but the run extends past it — a table
    that starts on a sampled page usually continues onto the next, and
    continuation is exactly what we need the reference to cover.
    """
    names = list(strata)
    chosen: list[tuple[int, int]] = []
    used: set[int] = set()
    for i in range(runs):
        # Round-robin the strata so every layout class is represented before
        # any class is sampled twice.
        pool = strata[names[i % len(names)]]
        candidates = [p for p in pool if not any(p - run_len < u < p + run_len for u in used)]
        if not candidates:
            candidates = pool
        anchor = rng.choice(candidates)
        start = max(1, min(anchor, total_pages - run_len + 1))
        end = min(start + run_len - 1, total_pages)
        chosen.append((start, end))
        used.update(range(start, end + 1))
    return sorted(chosen)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf", type=Path)
    ap.add_argument("--analysis", type=Path, required=True,
                    help="detect-pdf --analyze --json output for this PDF")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--seed", type=int, default=20260805)
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--run-len", type=int, default=3)
    ap.add_argument("--dpi", type=int, default=200)
    args = ap.parse_args()

    analysis = json.loads(args.analysis.read_text())
    total = analysis["page_count"]
    strata = _stratify(analysis)
    ranges = _draw(strata, args.runs, args.run_len, random.Random(args.seed), total)

    print(f"source        : {args.pdf.name}  ({total} pages)")
    for name, pages in strata.items():
        print(f"  stratum {name:<18} {len(pages):>5} pages")
    spec = ",".join(f"{a}-{b}" for a, b in ranges)
    sampled = sum(b - a + 1 for a, b in ranges)
    print(f"sampled       : {spec}  ({sampled} pages, {100*sampled/total:.2f}% of document)")

    args.out.mkdir(parents=True, exist_ok=True)
    images = args.out / "images"
    images.mkdir(exist_ok=True)
    excerpt = args.out / f"{args.pdf.stem}-excerpt.pdf"

    subprocess.run(["qpdf", str(args.pdf), "--pages", ".", spec, "--", str(excerpt)], check=True)
    subprocess.run(["pdftoppm", "-jpeg", "-r", str(args.dpi), str(excerpt), str(images / "page")],
                   check=True)

    # Map excerpt page N back to its page in the source, so a reviewer looking
    # at page-07.jpg can find the same page in the original document.
    mapping = [p for a, b in ranges for p in range(a, b + 1)]
    (args.out / "page_map.json").write_text(json.dumps(
        {"seed": args.seed, "source_pages": total, "ranges": [list(r) for r in ranges],
         "excerpt_to_source": {str(i + 1): p for i, p in enumerate(mapping)}}, indent=2) + "\n")

    print(f"excerpt       : {excerpt}  ({excerpt.stat().st_size/1e6:.1f} MB)")
    print(f"images        : {len(list(images.glob('*.jpg')))} @ {args.dpi} DPI")
    return 0


if __name__ == "__main__":
    sys.exit(main())
