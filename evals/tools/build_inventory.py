#!/usr/bin/env python3
"""Build the corpus tracking sheets from the local GCS mirror. Inventory only.

Does no parsing — file type comes from magic bytes, not from opening documents.

Two sheets:

  corpus-inventory.csv   the studio dataset rows — domain, market, title,
                         source_url. Metadata for labelling and selection.
  source-documents.csv   the retained original files — the PDFs a golden is
                         actually built from.

The parsed `.md` bucket is deliberately NOT mirrored or referenced. It holds
the current ingestion pipeline's output: a third extractor's candidate, not a
reference. It also cannot be joined to the originals — raw and parsed objects
use different UUIDs and the markdown carries no back-reference — so it says
nothing about the documents we actually build goldens from.

The two sheets share only a datasource id, and a datasource spans many domains,
so they do not join per-file either. Read the first as "what we ingested" and
the second as "what we can build goldens from".

  ./build_inventory.py --csv studio_results.csv --mirror ~/Projects/prodeen/pdf-corpus \\
      --out-dir evals
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from pathlib import Path

GOLDEN_COLS = ["in_golden_set", "golden_doc_id", "notes"]


def sniff(path: Path) -> str:
    """Magic-byte identification. Not parsing — just what the bytes claim to be."""
    try:
        head = path.open("rb").read(8)
    except OSError:
        return "unreadable"
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"PK\x03\x04"):
        return "zip/ooxml"
    if head[:5].lower() in (b"<!doc", b"<html"):
        return "html"
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        return "ole"
    return "text/other"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_golden_ids(manifest: Path) -> dict[str, str]:
    """Map a source_url already in the golden set -> its manifest document id."""
    if not manifest.exists():
        return {}
    out = {}
    for line in manifest.read_text().splitlines():
        if not line.strip():
            continue
        d = json.loads(line)
        url = (d.get("source") or {}).get("source_url")
        if url:
            out[url] = d["id"]
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", type=Path, required=True, help="studio results export")
    ap.add_argument("--mirror", type=Path, required=True, help="local gcloud rsync mirror")
    ap.add_argument("--out-dir", type=Path, required=True)
    args = ap.parse_args()

    mirror = args.mirror.expanduser()
    raw_root = mirror / "raw"
    golden = load_golden_ids(args.out_dir / "manifest.jsonl")

    # --- sheet 1: dataset rows, metadata only --------------------------------
    rows = list(csv.DictReader(args.csv.open(encoding="utf-8")))
    ds_meta: dict[str, tuple[str, str, str]] = {}
    inv_path = args.out_dir / "corpus-inventory.csv"
    fields = ["doc_id", "datasource_id", "domain", "datasource_name", "market_code",
              "industry", "title", "source_url", "source_is_pdf_url",
              "upstream_content_type"] + GOLDEN_COLS
    with inv_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for r in rows:
            m = re.match(r"gs://[^/]+/[^/]+/([^/]+)/([^/.]+)\.md$", r.get("storage_url") or "")
            ds_id, doc_id = (m.group(1), m.group(2)) if m else ("", "")
            if ds_id:
                ds_meta.setdefault(ds_id, (r.get("datasource_name", ""),
                                           r.get("market_code", ""), r.get("industry", "")))
            src = r.get("source_url") or ""
            w.writerow({
                "doc_id": doc_id, "datasource_id": ds_id, "domain": r.get("domain", ""),
                "datasource_name": r.get("datasource_name", ""),
                "market_code": r.get("market_code", ""), "industry": r.get("industry", ""),
                "title": (r.get("title") or "").replace("\n", " ")[:200],
                "source_url": src,
                "source_is_pdf_url": "yes" if src.lower().split("?")[0].endswith(".pdf") else "no",
                # Describes the ORIGINAL upstream document, not the stored object
                # (the stored object is always parsed markdown).
                "upstream_content_type": r.get("content_type", ""),
                "in_golden_set": "yes" if src in golden else "no",
                "golden_doc_id": golden.get(src, ""), "notes": "",
            })

    # --- sheet 2: retained originals, the golden-set candidates --------------
    src_path = args.out_dir / "source-documents.csv"
    sfields = ["file_id", "datasource_id", "datasource_name", "market_code", "industry",
               "file_type", "extension", "bytes", "sha256", "local_path"] + GOLDEN_COLS
    counts: dict[str, int] = {}
    n = 0
    with src_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=sfields)
        w.writeheader()
        for p in sorted(raw_root.rglob("*")):
            if not p.is_file():
                continue
            ds_id = p.parent.name
            name, market, industry = ds_meta.get(ds_id, ("", "", ""))
            kind = sniff(p)
            counts[kind] = counts.get(kind, 0) + 1
            n += 1
            w.writerow({
                "file_id": p.stem, "datasource_id": ds_id, "datasource_name": name,
                "market_code": market, "industry": industry, "file_type": kind,
                "extension": p.suffix.lower().lstrip("."), "bytes": p.stat().st_size,
                "sha256": sha256(p), "local_path": str(p),
                "in_golden_set": "no", "golden_doc_id": "", "notes": "",
            })

    print(f"{inv_path}  {len(rows)} rows (metadata only)")
    print(f"{src_path}  {n} originals: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    if golden:
        marked = sum(1 for r in rows if (r.get('source_url') or '') in golden)
        print(f"golden set: {len(golden)} documents in manifest, {marked} matched in dataset")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
