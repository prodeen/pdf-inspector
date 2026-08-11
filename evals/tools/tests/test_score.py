"""Tests for evals/tools/score.py.

The metric that matters here is key integrity, and the trap that matters is
`text_similarity` reading plausibly while being wrong — see the autojunk test.
"""

import importlib.util
import re
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location("eval_score", ROOT / "evals/tools/score.py")
score = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(score)


class TestTables(unittest.TestCase):
    def test_parses_pipe_tables_and_drops_separators(self):
        md = "intro\n\n|a|b|\n|---|---|\n|1|2|\n|3|4|\n\ntail\n"
        self.assertEqual(score.tables(md), [[["a", "b"], ["1", "2"], ["3", "4"]]])

    def test_separate_blocks_stay_separate(self):
        md = "|a|b|\n|---|---|\n|1|2|\n\n|c|d|\n|---|---|\n|3|4|\n"
        self.assertEqual(len(score.tables(md)), 2)

    def test_colon_alignment_row_is_a_separator_not_data(self):
        md = "|a|b|\n|:---|---:|\n|1|2|\n"
        self.assertEqual(score.tables(md), [[["a", "b"], ["1", "2"]]])


class TestKeyedRows(unittest.TestCase):
    def test_maps_key_to_trailing_cells(self):
        md = "|0110010|Grapefruit|0,05|\n|---|---|---|\n|0110020|Oranges|0,1|\n"
        got = score.keyed_rows(md, re.compile(r"^\d{7}$"))
        self.assertEqual(got, {"0110010": ("Grapefruit", "0,05"),
                               "0110020": ("Oranges", "0,1")})

    def test_rows_whose_first_cell_is_not_a_key_are_ignored(self):
        md = "|Commodity|Name|MRL|\n|---|---|---|\n|0110010|Grapefruit|0,05|\n"
        got = score.keyed_rows(md, re.compile(r"^\d{7}$"))
        self.assertEqual(list(got), ["0110010"])


class TestTextSimilarity(unittest.TestCase):
    def test_autojunk_is_disabled_for_long_documents(self):
        """difflib's autojunk heuristic treats common characters as junk once a
        sequence exceeds 200 elements. On character sequences that is most of
        the alphabet, which collapses the ratio for near-identical documents and
        makes a healthy extraction look broken."""
        base = ("The Diretoria Colegiada da Agencia Nacional de Vigilancia "
                "Sanitaria resolve adotar a seguinte Resolucao. ") * 40
        candidate = base + "One extra sentence at the end.\n"
        with tempfile.TemporaryDirectory() as td:
            g = Path(td) / "golden.md"
            c = Path(td) / "cand.md"
            g.write_text(base)
            c.write_text(candidate)
            out = Path(td) / "score.json"
            rc = score_main(["--golden", str(g), "--candidate", str(c),
                             "--json-output", str(out)])
            self.assertEqual(rc, 0)
            import json
            sim = json.loads(out.read_text())["text_similarity"]
        self.assertGreater(sim, 0.95, f"near-identical documents scored {sim}")


def score_main(argv):
    import sys
    old = sys.argv
    sys.argv = ["score.py", *argv]
    try:
        return score.main()
    finally:
        sys.argv = old


if __name__ == "__main__":
    unittest.main()
