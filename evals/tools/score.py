#!/usr/bin/env python3
"""Score pdf2md output against per-page golden references.

Two layers, because they answer different questions.

Structure (always): how much of the table topology survived. This is what
TEDS measures and what character-level diffing is blind to — an extractor can
retain 90% of a page's characters while losing every row/column binding on it.

Key integrity (opt-in, --key-pattern): whether a row's identifier still carries
its own values. For documents where rows are keyed — a commodity code, a CAS
number, an article number — this is the metric that decides whether the output
is usable. A dissociated value is worse than a missing one: it still reads as
data, so a downstream agent pairs it with confidence and is wrong.

  ./score.py --golden golden/ --candidate cand.md
  ./score.py --golden golden/ --candidate cand.md --key-pattern '^\\d{7}$'
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from pathlib import Path


def tables(md: str) -> list[list[list[str]]]:
    """Extract GFM pipe tables as lists of rows of cells, separators dropped."""
    out: list[list[list[str]]] = []
    cur: list[list[str]] = []
    for line in md.split("\n"):
        s = line.strip()
        if s.startswith("|") and s.endswith("|") and len(s) > 1:
            cells = [c.strip() for c in s[1:-1].split("|")]
            if not all(c and set(c) <= set("-: ") for c in cells):
                cur.append(cells)
        elif cur:
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


def keyed_rows(md: str, key_re: re.Pattern) -> dict[str, tuple[str, ...]]:
    """Map row key -> trailing cells, for rows whose first cell matches the key.

    Trailing empty cells are dropped. An extractor that emits a phantom empty
    column pads every row with `""`, which would fail an exact tuple comparison
    on rows whose values are all correct and correctly attributed — reporting
    it as the dissociation failure this metric exists to detect. Padding is a
    formatting defect; it belongs in the structure metrics, not this one.
    """
    found: dict[str, tuple[str, ...]] = {}
    for t in tables(md):
        for row in t:
            if row and key_re.match(row[0]):
                values = list(row[1:])
                while values and not values[-1]:
                    values.pop()
                found[row[0]] = tuple(values)
    return found


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--golden", type=Path, required=True,
                    help="directory of page-NN.md references, or a single whole-document .md")
    ap.add_argument("--candidate", type=Path, required=True,
                    help="pdf2md output for the whole excerpt")
    ap.add_argument("--key-pattern",
                    help="regex matching a row-key cell, e.g. '^\\d{7}$' for EU commodity codes")
    ap.add_argument("--json-output", type=Path)
    args = ap.parse_args()

    # Page-wise goldens come from the batch pipeline; small documents are
    # goldened as one file by an agent reading the renders. Both are valid.
    if args.golden.is_dir():
        golden_files = sorted(args.golden.glob("page-*.md"))
    elif args.golden.is_file():
        golden_files = [args.golden]
    else:
        golden_files = []
    if not golden_files:
        print(f"no golden references at {args.golden}", file=sys.stderr)
        return 1

    cand_text = args.candidate.read_text()
    cand_tables = tables(cand_text)
    key_re = re.compile(args.key_pattern) if args.key_pattern else None
    cand_keys = keyed_rows(cand_text, key_re) if key_re else {}

    cols = ["page", "rows", "cols"] + (["keys", "found", "exact"] if key_re else [])
    widths = [9, 6, 6] + ([7, 7, 7] if key_re else [])
    print("".join(c.rjust(w) if i else c.ljust(w) for i, (c, w) in enumerate(zip(cols, widths))))
    print("-" * sum(widths))

    per_page = []
    tot_keys = tot_found = tot_exact = tot_rows = 0
    # A golden row whose only cell is its own label carries no payload, so
    # reproducing it proves nothing. Counting those rows as successes is how a
    # document that lost every residue value can still post a respectable
    # exact-match rate. `valued` counts only rows that have something to lose.
    tot_valued = tot_valued_exact = 0
    for gf in golden_files:
        g = gf.read_text()
        gt = tables(g)
        rows = sum(len(t) for t in gt)
        ncols = max((len(r) for t in gt for r in t), default=0)
        tot_rows += rows
        rec = {"page": gf.stem, "golden_rows": rows, "golden_cols": ncols}
        cells = [gf.stem.rjust(0).ljust(widths[0]), str(rows).rjust(widths[1]),
                 str(ncols).rjust(widths[2])]
        if key_re:
            gk = keyed_rows(g, key_re)
            found = sum(1 for k in gk if k in cand_keys)
            exact = sum(1 for k, v in gk.items() if cand_keys.get(k) == v)
            valued = {k: v for k, v in gk.items() if any(c for c in v[1:])}
            valued_exact = sum(1 for k, v in valued.items() if cand_keys.get(k) == v)
            tot_keys += len(gk); tot_found += found; tot_exact += exact
            tot_valued += len(valued); tot_valued_exact += valued_exact
            rec |= {"keys": len(gk), "found": found, "exact": exact,
                    "valued": len(valued), "valued_exact": valued_exact}
            cells += [str(len(gk)).rjust(widths[3]), str(found).rjust(widths[4]),
                      str(exact).rjust(widths[5])]
        per_page.append(rec)
        print("".join(cells))

    gold_text = "\n".join(f.read_text() for f in golden_files)
    # autojunk=False is load-bearing. difflib's autojunk heuristic treats any
    # element occurring in more than 1% of a sequence longer than 200 as junk —
    # on a *character* sequence that is most of the alphabet, so it silently
    # collapses the ratio on longer documents. The SDS excerpt scores 0.2317
    # with it on and 0.9069 with it off, for the same pair of files.
    sim = difflib.SequenceMatcher(
        None, _norm(cand_text), _norm(gold_text), autojunk=False
    ).ratio()
    gn, cn = re.sub(r"\s+", "", gold_text), re.sub(r"\s+", "", cand_text)
    chars_pct = 100 * len(cn) / max(len(gn), 1)

    print("-" * sum(widths))
    summary = {
        "golden_pages": len(golden_files),
        "golden_table_rows": tot_rows,
        "candidate_table_blocks": len(cand_tables),
        "text_similarity": round(sim, 4),
        "raw_chars_retained_pct": round(chars_pct, 1),
        "per_page": per_page,
    }
    print(f"golden pages              : {len(golden_files)}")
    print(f"golden table rows         : {tot_rows}")
    print(f"candidate table blocks    : {len(cand_tables)}")
    print(f"raw chars retained        : {chars_pct:.1f}%")
    print(f"text similarity           : {sim:.4f}")

    if key_re:
        pct_found = 100 * tot_found / max(tot_keys, 1)
        pct_exact = 100 * tot_exact / max(tot_keys, 1)
        pct_valued = 100 * tot_valued_exact / max(tot_valued, 1)
        summary |= {"keys_in_golden": tot_keys, "keys_found": tot_found,
                    "keys_exact": tot_exact, "keys_found_pct": round(pct_found, 1),
                    "keys_exact_pct": round(pct_exact, 1),
                    "valued_keys": tot_valued, "valued_keys_exact": tot_valued_exact,
                    "valued_keys_exact_pct": round(pct_valued, 1)}
        print()
        print(f"row keys in golden        : {tot_keys}")
        print(f"  present in candidate    : {tot_found}  ({pct_found:.1f}%)")
        print(f"  with ALL values correct : {tot_exact}  ({pct_exact:.1f}%)")
        print(f"row keys carrying values  : {tot_valued}")
        print(f"  with ALL values correct : {tot_valued_exact}  ({pct_valued:.1f}%)"
              f"  <- decides usability")
        if pct_valued < 50 and chars_pct > 80:
            print()
            print("  WARNING: high character retention with low key integrity means values")
            print("  are DISSOCIATED, not missing. Text metrics will look healthy and the")
            print("  output is still unusable. Do not judge this document by NID alone.")

    if args.json_output:
        args.json_output.write_text(json.dumps(summary, indent=2) + "\n")
        print(f"\nwrote {args.json_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
