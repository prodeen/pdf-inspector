#!/usr/bin/env python3
"""Prepare — but do NOT submit — a Gemini batch job that goldens page images.

Writes the JSONL, prices the job, and prints the two commands that would run
it. Submission stays a human decision: the batch bills real money and the
whole point of a golden corpus is that references are generated once,
deliberately, and then frozen.

Differs from `eu396-processing/images_to_markdown.py` in the prompt only: this
asks for GitHub-Flavored Markdown, because the reference is scored against
pdf2md output and a dialect mismatch shows up as extraction error that isn't
there. That script's HTML-table prompt is right for its own downstream
consumer, and wrong here.

  ./prepare_batch.py images/ --name eurlex-396 --out ./batch
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

MODEL = "gemini-3-flash-preview"
PROJECT = "upbeat-object-453314-n3"
GCS_BUCKET = "gs://prodeen-tmp/_processing"

# Vertex Batch API: 50% of interactive pricing. Update if the rate card moves.
INPUT_PRICE_PER_M = 0.25
OUTPUT_PRICE_PER_M = 1.50

# Gemini tiles large images into 768x768 crops at 258 tokens each.
TILE_PX = 768
TOKENS_PER_TILE = 258
# A dense regulatory table page. Prose pages land far below this; the estimate
# is deliberately pessimistic so the printed cost is a ceiling, not a guess.
ASSUMED_OUTPUT_TOKENS_PER_PAGE = 6000

PROMPT = """\
Transcribe this page image into GitHub-Flavored Markdown. This is a reference \
transcription used to evaluate an automated PDF extractor, so fidelity to what \
is actually printed matters more than readability.

Rules:
1. Tables use GFM pipe syntax with a `|---|` separator after the header row. \
Every row must have the same number of cells as the header. Use an empty cell \
for a blank. Never use rowspan/colspan; repeat a spanned value on each row it \
covers.
2. Preserve the reading order of the page. Do not reorder columns or rows.
3. Mark headings with ATX `#`..`######` matching their visual hierarchy.
4. Preserve bold as **text** and italic as *text*. Do not add emphasis that is \
not visually present.
5. Transcribe symbols, footnote markers, amendment markers (e.g. ►M246 ◄) and \
numeric formatting (including comma decimal separators) exactly as printed.
6. Do not add commentary, headers, or code fences around the output. Emit only \
the transcription.
7. If the page is blank, emit nothing."""

GENERATION_CONFIG = {
    "generationConfig": {
        "temperature": 0.0,  # goldens must be reproducible; eu396 uses 0.2
        "topP": 0.95,
        "maxOutputTokens": 65535,
        "thinkingConfig": {"thinkingLevel": "MINIMAL"},
    },
    "safetySettings": [
        {"category": c, "threshold": "OFF"}
        for c in ("HARM_CATEGORY_HATE_SPEECH", "HARM_CATEGORY_DANGEROUS_CONTENT",
                  "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_HARASSMENT")
    ],
}


def _image_tokens(path: Path) -> int:
    """Token cost of one image under Gemini's 768px tiling."""
    try:
        with path.open("rb") as fh:
            data = fh.read(65536)
        # Minimal JPEG SOF parse — avoids a Pillow dependency for two integers.
        i, w, h = 2, None, None
        while i < len(data) - 9:
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xC0, 0xC1, 0xC2):
                h = (data[i + 5] << 8) | data[i + 6]
                w = (data[i + 7] << 8) | data[i + 8]
                break
            if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            i += 2 + ((data[i + 2] << 8) | data[i + 3])
        if not w or not h:
            return TOKENS_PER_TILE * 12
    except OSError:
        return TOKENS_PER_TILE * 12
    tiles = math.ceil(w / TILE_PX) * math.ceil(h / TILE_PX)
    return tiles * TOKENS_PER_TILE


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("images", type=Path)
    ap.add_argument("--name", required=True, help="corpus document id")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--stamp", required=True,
                    help="job timestamp; passed in so reruns are reproducible")
    args = ap.parse_args()

    files = sorted(args.images.glob("*.jpg"))
    if not files:
        print(f"no .jpg under {args.images}", file=sys.stderr)
        return 1

    args.out.mkdir(parents=True, exist_ok=True)
    job_id = f"{args.name}-{args.stamp}"
    gcs_images = f"{GCS_BUCKET}/{job_id}/images"

    lines, in_tokens = [], 0
    for f in files:
        in_tokens += _image_tokens(f) + len(PROMPT) // 4
        lines.append(json.dumps({
            "key": f.stem,
            "request": {
                "contents": [{"role": "user", "parts": [
                    {"text": PROMPT},
                    {"fileData": {"fileUri": f"{gcs_images}/{f.name}",
                                  "mimeType": "image/jpeg"}},
                ]}],
                **GENERATION_CONFIG,
            },
        }))

    jsonl = args.out / "batch_input.jsonl"
    jsonl.write_text("\n".join(lines) + "\n")

    out_tokens = len(files) * ASSUMED_OUTPUT_TOKENS_PER_PAGE
    cost = in_tokens / 1e6 * INPUT_PRICE_PER_M + out_tokens / 1e6 * OUTPUT_PRICE_PER_M

    print(f"pages          : {len(files)}")
    print(f"jsonl          : {jsonl}  ({jsonl.stat().st_size/1024:.0f} KB)")
    print(f"model          : {MODEL}   project {PROJECT}")
    print(f"input tokens   : ~{in_tokens:,} (image tiles + prompt)")
    print(f"output tokens  : ~{out_tokens:,} (assumes {ASSUMED_OUTPUT_TOKENS_PER_PAGE:,}/page, pessimistic)")
    print(f"ESTIMATED COST : ~${cost:.2f}")
    print()
    print("NOT SUBMITTED. To run this job:")
    print(f"  1. gcloud storage cp {args.images}/*.jpg {gcs_images}/")
    print(f"  2. gcloud storage cp {jsonl} {GCS_BUCKET}/{job_id}/input.jsonl")
    print(f"  3. submit with src={GCS_BUCKET}/{job_id}/input.jsonl "
          f"dest={GCS_BUCKET}/{job_id}/output")
    return 0


if __name__ == "__main__":
    sys.exit(main())
