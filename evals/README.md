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
python3 -m unittest discover -s evals/tools/tests       # tests for the tools
```

Candidate markdown is regenerated each run into `evals/candidate/` and is
gitignored. The PDFs and goldens are the durable artifacts. Exit code is
nonzero if a document falls below its manifest `gate`.

The tools' own tests live in `evals/tools/tests/`, not `scripts/tests/`,
because `.gitignore` drops any directory named `scripts/` at any depth — a new
file added there is invisible to git and silently never committed. After adding
anything under `evals/`, confirm with `git status --untracked-files=all`.

## Current state

12 documents, 55 pages of golden reference, 11 PDF producers.

| document | pages | chars | text sim | valued keys | correct |
|---|---|---|---|---|---|
| `codex-gsfa-cxs192` | 3 | 100.4% | 0.9944 | 105 | 97.1% |
| `sg-food-additives-permitted` | 3 | 99.9% | 0.9973 | 51 | 76.5% |
| `pyfpdf-sea-salt-spec` | 2 | 100.6% | 0.9970 | — | — |
| `anvisa-rdc-tables` | 3 | 103.3% | 0.9655 | — | — |
| `tt-legal-notice-192-1999` | 4 | 100.8% | 0.9631 | — | — |
| `melatonin-eu-permissibility` | 2 | 91.4% | 0.9530 | — | — |
| `citric-acid-coa-sds` | 4 | 97.4% | 0.9069 | — | — |
| `tw-food-additive-standards` | 3 | 101.5% | 0.8719 | 8 | 12.5% |
| `ara-oil-supply-chain-dashboard` | 6 | 101.2% | 0.8663 | — | — |
| `eurlex-396-consolidated` | 15 | 90.0% | 0.5522 | 116 | **0.0%** |
| `jp-additive-use-categories` | 3 | **47.3%** | 0.3081 | — | — |
| `coconut-oil-spec-scanned` | 7 | classification only | | | |

Read the `eurlex` row carefully, because it is the reason this corpus exists.
The extractor retains 90% of characters and **100% of the numeric residue
values**, while **zero** of the 116 commodity codes that actually carry values
still carry the right ones. The data is not missing — it is *dissociated*.
Every MRL number is present; none is attributable to its commodity.

Character-level metrics rate that document healthy. It is unusable, and worse
than useless: an agent reading it pairs the wrong limit with the wrong food and
has no signal that anything went wrong. `score.py` prints a warning whenever it
sees this shape (high character retention, low key integrity).

`jp-additive-use-categories` is the same failure one step worse — it loses more
than half the characters outright — and `ara-oil-supply-chain-dashboard` is the
same failure at its most deceptive: page 1 is perfect, pages 2 onward merge
every table on the page into one grid and pull the headings between them into
cells.

`codex` and `sg` are the load-bearing counter-examples. Both are ruled
multi-page tables and both come out nearly intact, which is what makes the
other three diagnosable as specific defects rather than "tables are hard".

### Read `valued%`, not `exact%`

`keys_exact_pct` counts every keyed row, including rows whose only cell is
their own label. Reproducing one of those proves nothing, and on EU 396 they
were enough to turn a total loss into a 32.8% score. `valued_keys_exact_pct`
counts only rows that had something to lose. That is the number the gate and
the table above use.

Two related traps this corpus has already sprung on its own tooling:

- `difflib.SequenceMatcher` defaults to `autojunk=True`, which treats any
  element in more than 1% of a >200-element sequence as junk. On *character*
  sequences that is most of the alphabet. The SDS excerpt scored 0.2317 with it
  on and 0.9069 with it off, for the same two files.
- Exact-match on cell tuples counted a phantom trailing empty column as
  dissociation, reporting `tw-food-additive-standards` at 0% when every value
  was in fact attributed to the right key. Trailing blanks are now stripped
  before comparison; padding is a structure defect, not a dissociation one.

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

Two manifest fields beyond scoring:

- `expect` — `{"pdf_type": …, "exit_code": …}`. Checked on every run. This is
  how a document with no text layer earns a gate, and how a document that used
  to fail extraction outright stays fixed.
- `scoring: "classification_only"` — no text golden; the `expect` block is the
  whole gate. Used where a text reference could only ever score 0% and would
  read as a permanent regression rather than a correct verdict.

`run_evals.py` never runs `pdf2md` with `check=True`. A document that fails to
extract is precisely what this corpus is for; crashing the run would hide it
and take every later document down with it.

## The candidate pool

Two tracking sheets, rebuilt by `tools/build_inventory.py` from a local mirror
of the datasource buckets:

| sheet | rows | what it is |
|---|---|---|
| `corpus-inventory.csv` | 1042 | studio dataset rows — domain, market, title, `source_url`. Metadata for labelling and selection. |
| `source-documents.csv` | 1140 | retained **originals**, incl. **574 PDFs** (232 MB) — what goldens are built from |

Both carry `in_golden_set` / `golden_doc_id` / `notes`, so promoting a document
into `manifest.jsonl` is recorded in one place.

### The pool is much smaller than 574

Deduplicated by sha256 the 574 PDFs are **354** distinct files, and **185 of
those 354 are 13-page `pdf-lib` slices of one document** — EU 396/2005 at
consolidation 065.001, chunked by the ingestion pipeline. They are already
represented by `eurlex-396-consolidated` (067.001) and are not worth further
corpus slots; they do independently reproduce its table-collapse defect.

That leaves ~169 genuinely distinct documents. Profiling all 574 with
`detect-pdf --analyze` takes about 9 seconds:

| | count |
|---|---|
| text-based | 549 |
| image-based / scanned / mixed | 23 |
| failed to parse at all | 2 (same file twice — now fixed) |
| ≤20 pages | 462 |

The ≤20-page majority is what makes self-authored goldens practical; see
"Adding a document".

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

Write the golden *before* looking at the candidate. It is the one rule that
makes the reference independent, and it is cheap to break by accident.

Size the excerpt to what you will actually transcribe. Ten of these documents
started as 6-page samples and five were re-cut to a single 3-page run once it
was clear a 6-page hand-transcription of a dense table would be rushed. A
smaller golden you trust beats a larger one you don't — and the golden must
cover the *whole* excerpt, or `raw_chars_retained_pct` becomes meaningless.

Set a `gate` from the observed score, a little below it, so the document
catches regressions without failing on noise. Add `expect` for anything the
score cannot see: `pdf_type`, and `exit_code` for documents that must keep
extracting at all.

## Caveat that matters

`human_reviewed` is `false` on every golden here — the batch-generated ones
because Gemini hallucinates cells in dense tables, the agent-authored ones
because a model transcribing 50 pages of page images will drop a cell
somewhere. Both need a human before anyone acts on an aggregate.

Spot-check against the renders before trusting a number
(`pdftoppm -jpeg -r 150 evals/pdfs/<id>.pdf page`). The EU 396 finding is
corroborated by cell-level inspection; for the new documents, the defects with
quoted `evidence` in `manifest.jsonl` were each read off the output directly,
but the aggregate percentages have not been audited.

Where a score needs interpretation, the manifest says so in `notes` —
`tw-food-additive-standards` posts 12.5% valued-key accuracy and trips the
dissociation warning, but its values are correctly attributed and the misses
are a hyphen-rejoin defect. Read the diff before believing the metric.
