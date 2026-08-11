# Prodeen eval corpus

Golden-reference validation for `pdf2md` against the regulatory documents we
actually ingest. This is **not** the same thing as `tests/snapshots/` and the
difference is the whole point.

## candidate vs golden vs snapshot

| | Produced by | Answers |
|---|---|---|
| **candidate** | `pdf2md <pdf>` | what the library does today |
| **golden** | a model reading page *images* | what the page actually says |
| **snapshot** | `pdf2md <pdf> > file.md` | did behaviour change? |

`tests/snapshots/*.md` are candidates promoted to fixtures — they encode
current behaviour *including its bugs*. `tests/snapshots/real-estate-pricing.md`
ships with words run together, and that is correct for its purpose: it detects
change, it does not assert quality. Never author a golden by running the
extractor.

Both tiers earn their keep. Snapshots caught a plausible-looking fix that broke
five documents by merging adjacent table header cells. Goldens caught a total
structural failure that all 966 tests pass straight through.

## Running

```bash
cargo build --release
python3 evals/tools/run_evals.py                        # whole corpus
python3 evals/tools/run_evals.py --doc <id> --keep      # one doc, keep output
```

Candidate markdown is regenerated each run into `evals/candidate/` and is
gitignored. The PDFs and goldens are the durable artifacts. Exit code is
nonzero if a document falls below its manifest `gate`.

## Current state

| document | pages | chars retained | text sim | row keys | keys with correct values |
|---|---|---|---|---|---|
| `melatonin-eu-permissibility` | 2 | 91.4% | 0.9502 | — | — |
| `eurlex-396-consolidated` | 15 | 90.0% | 0.3857 | 229 | **0 (0.0%)** |

Read that second row carefully, because it is the reason this corpus exists.
On EU 396/2005 the extractor retains 90% of characters and **100% of the 202
numeric residue values**, while **zero** of 229 commodity codes still carry
their correct values. The data is not missing — it is *dissociated*. Every MRL
number is present; none is attributable to its commodity.

Character-level metrics rate that document healthy. It is unusable, and worse
than useless: an agent reading it pairs the wrong limit with the wrong food and
has no signal that anything went wrong. `score.py` prints a warning whenever it
sees this shape (high character retention, low key integrity).

## Layout

```
evals/
  manifest.jsonl    one JSON object per document — provenance, gates, defects
  pdfs/             excerpt PDFs (the fixtures)
  golden/           <id>/page-NN.md for page-wise, or <id>.md for whole-document
  tools/            the pipeline (named tools/ because .gitignore eats scripts/)
  candidate/        regenerated, gitignored
```

`manifest.jsonl` doubles as the defect inventory: each document carries
`known_defects` with severity, root cause, status and business impact. That is
what makes this repeatable rather than a one-off investigation.

## The candidate pool

Two tracking sheets, rebuilt by `tools/build_inventory.py` from a local mirror
of the datasource buckets:

| sheet | rows | what it is |
|---|---|---|
| `corpus-inventory.csv` | 1042 | studio dataset rows — domain, market, title, `source_url`. Metadata for labelling and selection. |
| `source-documents.csv` | 1140 | retained **originals**, incl. **574 PDFs** (232 MB) — what goldens are built from |

Both carry `in_golden_set` / `golden_doc_id` / `notes`, so promoting a document
into `manifest.jsonl` is recorded in one place.

Mirror the originals with `gcloud storage rsync` (not a web scraper — these are
the exact bytes we ingested, with no dead links):

```bash
ORG=org_2v4ngEeCdoUYaz4tr1GVet3diXV
gcloud storage rsync -r gs://prodeen-datasources-raw/$ORG/ ~/Projects/prodeen/pdf-corpus/raw/
python3 evals/tools/build_inventory.py --csv <studio-export>.csv \
  --mirror ~/Projects/prodeen/pdf-corpus --out-dir evals
```

The mirror lives **outside** the repo (291 MB); only the sheets are committed.

### Why the parsed bucket is not mirrored

`gs://prodeen-datasources-parsed/` holds the current ingestion pipeline's
markdown. That is a *third extractor's candidate*, not a reference — the same
trap as treating a snapshot as truth, one step further removed. The dataset's
`storage_url` column points there, so it describes what we produced, never what
the document says.

It is also useless for this work even as a baseline: parsed objects use
different UUIDs from the originals and carry no back-reference, so they cannot
be tied to any of the 574 PDFs a golden would be built from. 729 MB for no
signal we can act on.

Two limitations of the sheets that remain:

- **They do not join per file.** `corpus-inventory.csv` and
  `source-documents.csv` share only a `datasource_id`, and a datasource spans
  many domains (one covers `aafco.org`, `accessdata.fda.gov`, `congress.gov`
  and more). Per-file linkage, if it exists anywhere, is in the backend DB.
- **498 of the 574 PDFs have no market/domain metadata**, because their
  datasource is not among the 116 sampled in the studio export. Still usable as
  fixtures; they just arrive unlabelled.

## Adding a document

```bash
detect-pdf <pdf> --analyze --json > analysis.json        # check page_count first
evals/tools/sample_excerpt.py <pdf> --analysis analysis.json --out work/ --seed <n>
qpdf work/<name>-excerpt.pdf --remove-unreferenced-resources=yes \
     --object-streams=generate --compress-streams=y evals/pdfs/<id>.pdf
```

Sample contiguous runs, not single pages — table continuation across a page
break is its own defect class. Stratify, or a table-dense document never shows
you its prose. Seed it, or the corpus changes under you.

Shrink before committing. The EU 396 excerpt was 39 MB because `qpdf` preserves
the struct tree for all 4060 source pages; the flags above bring it to 5 MB with
tags intact and byte-identical extraction. Ghostscript reaches 280 KB but strips
the struct tree, which changes what `pdf2md` sees — do not use it.

Then generate the golden:

- **≲20 pages** — read the rendered images and write it yourself. Free, and you
  can cross-check link hrefs against `pdf2md <pdf> --items-json`.
- **larger** — `tools/prepare_batch.py` (prices it, never submits), then
  `tools/submit_batch.py --expect <n>`, which refuses to run unless the uploaded
  request count matches. Needs `gcloud auth login`. ~$0.002/page.

Goldens are **frozen once generated**. Regenerating is a deliberate, reviewed
event — a reference that changes when you rerun it is not a reference.

## Caveat that matters

`human_reviewed` is `false` on every golden here. Gemini hallucinates cells in
dense tables. The EU 396 finding is corroborated by cell-level inspection, but
before anyone acts on an aggregate, spot-check pages 01, 10 and 12 against the
renders (regenerate with `pdftoppm -jpeg -r 200 evals/pdfs/<id>.pdf page`).
