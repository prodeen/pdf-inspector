#!/usr/bin/env python3
"""Reduce the mirror to the documents still worth goldening.

The mirror is much smaller than its file count suggests: 574 PDF rows are 354
distinct files, and most of what remains after deduplication is instances of a
handful of templates. Selecting by hand means re-discovering that every session,
and re-reading documents a previous session already covered.

Three things count as covered, in increasing order of judgement:

  duplicate    byte-identical to another file (same sha256)
  goldened     this exact file is already in manifest.jsonl
  template     it shares a document template with a goldened file

The template test is the one that earns its keep, and it deliberately does not
compare words. Four PyFPDF product data sheets in this mirror share one template
and score 0.12-0.16 Jaccard on their text, because they describe different
products: text similarity says "unrelated" about documents that are, for our
purposes, the same document. What decides whether a file can teach the extractor
something new is the shape it presents — the producer that wrote it and the
sequence of block types it contains — not what it says.

So the fingerprint is the distribution of block types in the extracted markdown:
headings by level, table rows by column count, list items, paragraphs, with every
word discarded. Those same four sheets score 0.90-0.93 on it, and 0.32 against
the other PyFPDF template in the mirror.

Clustering is confined to files sharing a producer, a size class and a dominant
script. The script guard matters: two all-prose documents have near-identical
block profiles whatever language they are in, and a Cyrillic regulation exercises
code that a Latin one does not.

Output is `corpus-roadmap.csv`: one row per remaining cluster, largest first, so
"pick the next N documents" is reading off the top rather than a fresh survey.
Every member sha256 is kept in the row, so nothing is lost by collapsing.

  ./build_roadmap.py --mirror ~/Projects/prodeen/pdf-corpus
  ./build_roadmap.py --refresh-profiles      # re-profile the mirror from scratch
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EVALS = ROOT / "evals"

# Content rules for families that are chunks of one source document rather than
# instances of a template. Page slices of a 4000-page regulation share almost no
# vocabulary with each other, so no similarity threshold will group them; the
# thing they have in common is which document they were cut from.
REPRESENTED_BY = [
    {"golden_doc_id": "eurlex-396-consolidated",
     # Matched anywhere in the text, not just the first line: these are page
     # slices, and a slice can open mid-table with an amendment marker rather
     # than the running header.
     "match_text": r"02005R0396",
     "why": "13-page pdf-lib slices of EU 396/2005, chunked by the ingestion "
            "pipeline at consolidation 065.001"},
]

# Block-profile similarity above which two files of the same producer, size
# class and script are treated as one template. Measured separation in this
# mirror: 0.90-0.93 within a template, 0.32 across templates.
SHAPE_THRESHOLD = 0.85


def profile(pdf2md: Path, detect: Path, path: str) -> dict:
    rec = {"path": path}
    try:
        a = subprocess.run([str(detect), path, "--analyze", "--json"],
                           capture_output=True, timeout=180)
        rec["analysis"] = json.loads(a.stdout) if a.returncode == 0 else None
    except Exception:
        rec["analysis"] = None
    try:
        i = subprocess.run(["pdfinfo", path], capture_output=True, timeout=60)
        meta = {}
        for line in i.stdout.decode("utf-8", "replace").splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip()
        rec["pdfinfo"] = meta
    except Exception:
        rec["pdfinfo"] = {}
    try:
        o = subprocess.run([str(pdf2md), path], capture_output=True, timeout=180)
        md = o.stdout.decode("utf-8", "replace").split("--- Markdown Output ---")[-1]
    except Exception:
        md = ""
    rec["first_line"] = next((l.strip() for l in md.splitlines() if l.strip()), "")[:200]
    rec["text"] = md
    return rec


def shape(md: str) -> Counter:
    """The document's block profile, with every word discarded.

    A heading is an H<level>, a table row is a TR<columns>, and that is all the
    detail retained. Two renderings of one template produce the same profile
    even when they share almost no vocabulary.
    """
    toks: list[str] = []
    for line in md.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("|") and s.endswith("|") and len(s) > 1:
            cells = s[1:-1].split("|")
            is_sep = all(c.strip() and set(c.strip()) <= set("-: ") for c in cells)
            toks.append("SEP" if is_sep else f"TR{len(cells)}")
        elif re.match(r"^#{1,6} ", s):
            toks.append(f"H{len(s) - len(s.lstrip('#'))}")
        elif re.match(r"^([-*+] |\d+[.)] )", s):
            toks.append("LI")
        else:
            toks.append("P")
    return Counter(toks)


def shape_similarity(a: Counter, b: Counter) -> float:
    """1 - total variation distance between two block profiles."""
    ta, tb = sum(a.values()), sum(b.values())
    if not ta or not tb:
        return 0.0
    return 1 - 0.5 * sum(abs(a[k] / ta - b[k] / tb) for k in set(a) | set(b))


def script_of(md: str) -> str:
    """Dominant writing system. Latin and Cyrillic prose look identical as block
    profiles but are not interchangeable for an extractor."""
    counts = Counter()
    for ch in md:
        o = ord(ch)
        if 0x0400 <= o <= 0x04FF:
            counts["cyrillic"] += 1
        elif 0x4E00 <= o <= 0x9FFF or 0x3040 <= o <= 0x30FF:
            counts["cjk"] += 1
        elif 0x0590 <= o <= 0x06FF:
            counts["rtl"] += 1
        elif ch.isalpha() and o < 0x0250:
            counts["latin"] += 1
    return counts.most_common(1)[0][0] if counts else "none"


def cluster(records: list[dict]) -> list[list[int]]:
    fps = [shape(r["text"]) for r in records]
    parent = list(range(len(records)))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    buckets = defaultdict(list)
    for i, r in enumerate(records):
        pc = (r["analysis"] or {}).get("page_count", 0)
        size_class = 0 if pc <= 2 else 1 if pc <= 20 else 2
        buckets[(r["pdfinfo"].get("Producer", "?")[:30], size_class,
                 script_of(r["text"]))].append(i)

    for idxs in buckets.values():
        for a in range(len(idxs)):
            for b in range(a + 1, len(idxs)):
                i, j = idxs[a], idxs[b]
                if not fps[i] or not fps[j]:
                    continue
                if shape_similarity(fps[i], fps[j]) >= SHAPE_THRESHOLD:
                    union(i, j)

    groups = defaultdict(list)
    for i in range(len(records)):
        groups[find(i)].append(i)
    return list(groups.values())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mirror", type=Path,
                    default=Path.home() / "Projects/prodeen/pdf-corpus")
    ap.add_argument("--pdf2md", type=Path, default=ROOT / "target/release/pdf2md")
    ap.add_argument("--detect-pdf", type=Path, default=ROOT / "target/release/detect-pdf")
    ap.add_argument("--cache", type=Path, default=EVALS / ".roadmap-cache.json",
                    help="profiles are expensive; reuse them unless --refresh-profiles")
    ap.add_argument("--refresh-profiles", action="store_true")
    ap.add_argument("--out", type=Path, default=EVALS / "corpus-roadmap.csv")
    args = ap.parse_args()

    rows = [r for r in csv.DictReader((EVALS / "source-documents.csv").open())
            if r["extension"] == "pdf" and Path(r["local_path"]).exists()]
    if not rows:
        print("no mirrored PDFs found — check --mirror and source-documents.csv",
              file=sys.stderr)
        return 1

    # Deduplicate by content hash. Keeps the first row; the rest are recorded as
    # duplicates so the count stays auditable.
    first, dup_count = {}, 0
    for r in rows:
        if r["sha256"] in first:
            dup_count += 1
        else:
            first[r["sha256"]] = r

    cache = {}
    if args.cache.exists() and not args.refresh_profiles:
        cache = json.loads(args.cache.read_text())
    todo = [r for sha, r in first.items() if sha not in cache]
    if todo:
        print(f"profiling {len(todo)} file(s)…", file=sys.stderr)
        with ThreadPoolExecutor(8) as ex:
            done = list(ex.map(
                lambda r: (r["sha256"], profile(args.pdf2md, args.detect_pdf,
                                                r["local_path"])), todo))
        cache |= dict(done)
        args.cache.write_text(json.dumps(cache))

    records, shas = [], []
    for sha, r in first.items():
        rec = dict(cache[sha])
        rec["row"] = r
        records.append(rec)
        shas.append(sha)

    manifest = [json.loads(l) for l in
                (EVALS / "manifest.jsonl").read_text().splitlines() if l.strip()]
    goldened = {d["source"].get("source_sha256"): d["id"] for d in manifest
                if d.get("source", {}).get("source_sha256")}

    groups = cluster(records)

    out_rows, covered, remaining = [], defaultdict(int), 0
    for members in groups:
        members.sort(key=lambda i: -((records[i]["analysis"] or {}).get("page_count", 0)))
        member_shas = [shas[i] for i in members]

        hit = next((goldened[s] for s in member_shas if s in goldened), None)
        rule = None
        if not hit:
            for r in REPRESENTED_BY:
                if any(re.search(r["match_text"], records[i]["text"])
                       for i in members):
                    rule = r
                    break

        if hit or rule:
            reason = "goldened" if hit and member_shas[0] in goldened else \
                     "template" if hit else "represented"
            did = hit or rule["golden_doc_id"]
            covered[reason] += len(members)
            for s in member_shas:
                first[s]["in_golden_set"] = "yes" if s in goldened else "covered"
                first[s]["golden_doc_id"] = did
                if s not in goldened:
                    first[s]["notes"] = (rule or {}).get(
                        "why", f"same document template as {did}")
            continue

        remaining += 1
        rep = records[members[0]]
        a = rep["analysis"] or {}
        out_rows.append({
            "cluster_size": len(members),
            "representative_sha256": member_shas[0],
            "representative_path": rep["path"],
            "pages": a.get("page_count", ""),
            "pdf_type": a.get("pdf_type", "unparseable"),
            "pages_with_tables": len(a.get("pages_with_tables", [])),
            "pages_needing_ocr": len(a.get("pages_needing_ocr", [])),
            "producer": rep["pdfinfo"].get("Producer", "")[:60],
            "title": (rep["pdfinfo"].get("Title") or "")[:60],
            "market_code": rep["row"]["market_code"],
            "domain": ";".join(rep.get("domains", [])[:2]) if rep.get("domains") else "",
            "first_line": rep["first_line"][:120],
            "member_sha256s": ";".join(member_shas),
        })

    # Biggest clusters first: one golden there retires the most files.
    out_rows.sort(key=lambda r: (-r["cluster_size"], -(r["pages"] or 0)))
    with args.out.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(out_rows[0]) if out_rows else ["cluster_size"])
        w.writeheader()
        w.writerows(out_rows)

    src = EVALS / "source-documents.csv"
    all_rows = list(csv.DictReader(src.open()))
    patch = {r["sha256"]: r for r in first.values()}
    for r in all_rows:
        p = patch.get(r["sha256"])
        if p and p.get("golden_doc_id"):
            r["in_golden_set"] = p["in_golden_set"]
            r["golden_doc_id"] = p["golden_doc_id"]
            r["notes"] = p.get("notes", r.get("notes", ""))
    with src.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(all_rows[0]))
        w.writeheader()
        w.writerows(all_rows)

    total = len(rows)
    print(f"mirrored PDF rows        {total}")
    print(f"  duplicate files        {dup_count}")
    print(f"  unique files           {len(first)}")
    for k in ("goldened", "template", "represented"):
        if covered[k]:
            print(f"  covered ({k:<11}) {covered[k]}")
    print(f"  REMAINING clusters     {remaining}"
          f"  ({sum(r['cluster_size'] for r in out_rows)} files)")
    print(f"\nwrote {args.out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
