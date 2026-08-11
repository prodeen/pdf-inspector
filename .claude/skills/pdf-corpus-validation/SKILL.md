---
name: PDF Corpus Validation
description: >
  Build and run a golden-reference validation corpus for pdf-inspector (the
  pdf2md extractor) against real Prodeen documents. Use when the user says
  "validate pdf-inspector", "add a document to the validation set", "build a
  golden sample", "test pdf2md on <document>", "why is this PDF extracted
  wrong", "score the extractor", or wants to find, verify and fix PDF
  extraction defects on regulatory documents. Covers page sampling, Gemini
  batch golden generation, scoring, and the regression gate that fixes must
  pass.
---

# PDF Corpus Validation

Find out what pdf-inspector actually does to *our* documents, prove it with a
reference the extractor had no hand in producing, then fix defects without
regressing anything.

## The three artifacts — get this right or nothing else works

| | Produced by | Job |
|---|---|---|
| **candidate** | `pdf2md <pdf>` | what the library does today |
| **golden** | a model reading page *images* | what the page actually says |
| **snapshot** | `pdf2md <pdf> > file.md` | change detector, committed to the repo |

A snapshot is **not** a golden. Upstream's `tests/snapshots/*.md` are candidates
promoted to fixtures — they encode current behaviour including its bugs, which
is why `tests/snapshots/real-estate-pricing.md` ships with words run together.
Never treat a snapshot as truth, and never write a golden by running the
extractor. A golden must come from something that can see the page.

Both tiers matter and neither substitutes for the other:
- snapshots catch *changes* (they have caught a bad fix that all unit tests passed)
- goldens catch *defects* (they have caught a total failure that 966 tests missed)

## Prerequisites

- `pdf-inspector` checked out and built: `cargo build --release`
- `qpdf` and `pdftoppm` (poppler) — `brew install qpdf poppler`
- `uv`, and `gcloud` authenticated for the batch step (`gcloud auth login`)
- GCP project `upbeat-object-453314-n3`, staging `gs://prodeen-tmp/_processing/`

Everything lives in this repo: the corpus and pipeline in `evals/` (see
`evals/README.md`), this skill in `.claude/skills/`. Run the whole suite with
`python3 evals/tools/run_evals.py`.

Do intermediate work in a scratch directory. `.gitignore` drops `*.pdf` outside
`tests/fixtures/` and `evals/pdfs/`, and drops any directory named `scripts/` at
any depth — which is why the helpers are in `evals/tools/`, not `evals/scripts/`.
After adding corpus files, confirm with `git status --untracked-files=all` that
git can actually see them.

## Workflow

### 1. Profile the document

```bash
detect-pdf <pdf> --analyze --json > analysis.json
```

Read `page_count` before anything else. `file` lies about large PDFs — it
reported 69 pages for a 4060-page document. Note `pdf_type`, `is_complex`, and
how many pages carry tables/columns; this decides whether the document is worth
corpus space.

### 2. Sample page sequences

Never golden a whole large document — 4060 pages is neither affordable to
review nor necessary. Sample contiguous runs, stratified by layout class:

```bash
evals/tools/sample_excerpt.py <pdf> --analysis analysis.json --out work/ \
  --seed 20260805 --runs 5 --run-len 3
```

Contiguous runs (not single pages) because table continuation across a page
break is a defect class of its own. Stratified because a naive sample of a
table-dense document never sees its prose. Seeded because a golden that
resamples on rerun is not a golden. Writes `page_map.json` mapping excerpt
pages back to source pages so a reviewer can find the original.

### 3. Extract the candidate

```bash
pdf2md work/<name>-excerpt.pdf > work/candidate.md
```

Flags go **after** the path — the CLI hardcodes `args[1]` as the input file.
`--items-json` exposes per-item `is_bold`/`is_italic`/font/position and link
annotations; it is the fastest way to root-cause a formatting defect.

### 4. Generate the golden

**Small documents (≲20 pages): do it yourself.** Read the rendered page images
directly and write the reference. Free, immediate, and you can cross-check link
hrefs against `--items-json`. This found 6 defects on a 2-page document.

**Large documents: Gemini batch.**

```bash
evals/tools/prepare_batch.py work/images --name <doc-id> --out work/batch --stamp $(date +%Y%m%d)
# prints cost and the upload commands, then STOPS
gcloud storage cp work/images/*.jpg gs://prodeen-tmp/_processing/<job-id>/images/
gcloud storage cp work/batch/batch_input.jsonl gs://prodeen-tmp/_processing/<job-id>/input.jsonl
evals/tools/submit_batch.py --base gs://prodeen-tmp/_processing/<job-id> --out work/golden --expect 15
```

`prepare_batch.py` never submits. `submit_batch.py` refuses to run unless the
uploaded request count equals `--expect`, so a wrong prefix cannot bill a whole
document. **Confirm with the user before submitting** — it is billable and
goldens are meant to be generated once and frozen.

Cost is ~$0.002/page (15 pages = $0.03, 3.5 min). Spend is not the constraint;
human review is. Do not let cheapness talk you into goldening more pages than
anyone will verify.

The prompt asks for GFM, not HTML tables. `eu396-processing` asks for HTML
because its consumer wants HTML — here a dialect mismatch would score as
extraction error that isn't there. Temperature is 0.0 for reproducibility.

### 5. Score

```bash
evals/tools/score.py --golden work/golden --candidate work/candidate.md \
  --key-pattern '^\d{7}$' --json-output work/score.json
```

`--key-pattern` matches whatever identifies a row — a 7-digit EU commodity
code, a CAS number, an article number. Omit it for prose documents.

**Read key integrity, not text similarity.** On EU 396/2005 the extractor
retained 100% of numeric values and 90% of characters while **0 of 229**
commodity codes kept their correct values. The numbers were all present and
none were attributable. Character metrics rated that document healthy; it is
unusable. `score.py` prints a warning when it detects this shape.

### 6. Record it

Append one JSON object per document to `manifest.jsonl`: source URL + sha256,
sampled page ranges + seed, PDF producer/creator profile, golden provenance
(model, prompt version, cost, `human_reviewed`), and `known_defects` with
severity, evidence and which metric each affects. The manifest doubles as the
defect inventory — it is the thing that makes this repeatable rather than a
one-off investigation.

Goldens are unreviewed by default. Gemini hallucinates cells in dense tables.
Spot-check a few against the page images before trusting an aggregate, and set
`human_reviewed` honestly.

## Fixing a defect

Root-cause from `--items-json` first; the answer is usually in the PDF, not the
heuristics. Two worked examples: bold was lost because Chrome/Skia emits Type3
fonts with no `FontFile` and a BaseFont name reading `.SFNS-Regular` *for the
bold face* — `/FontWeight 600` was the only honest signal. Link hrefs are
extracted into a vector at `markdown/mod.rs:1166` and then never read.

Every change must pass the full gate before it counts:

```bash
cargo test                                    # 816 unit + 148 integration + 2 doc
python3 evals/tools/run_evals.py               # eval corpus gates
cargo fmt --all -- --check
cargo fmt --manifest-path wasm/Cargo.toml -- --check
cargo clippy -- -D warnings                   # NOT --all-targets; test-code lints are upstream debt
python3 -m unittest discover -s scripts/tests
```

If snapshots fail, **assume you are wrong before assuming they are stale.** A
plausible post-processing fix that merged emphasis runs across a line wrap
passed six new unit tests and broke five snapshots — it was silently merging
adjacent *table header cells* and corrupting a `* * * * *` separator. Diff every
failing snapshot and understand it before regenerating any.

Be wary of threshold-shaped heuristics (table detection cascade, heading
promotion scores). Upstream tuned those against ~200 private `pdf-evals`
documents we cannot see, so a local improvement can regress cases invisible to
us. Build corpus coverage for a defect class before touching its thresholds.

## Adding to the permanent validation set

Prefer **assertion tests over snapshots** for our documents — `assert_eq!(t.columns.len(), 7)`
survives unrelated whitespace churn where byte-exact snapshots don't. See
`src/lib.rs:1610` for the house pattern: a docstring naming the failure mode,
then structural assertions.

Keep the scored corpus in a sibling repo, not in the fork. `tests/fixtures/` is
already 9.8 MB against a 10 MiB crates.io cap, and a tree close to upstream is
what keeps `git rebase upstream/main` cheap.
