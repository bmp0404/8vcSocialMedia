"""The reply box counts X's weighted length in the browser (harness/review/web/src/lib/xtext.ts). These
vectors are its test data; this test keeps them equal to the Python count the server checks with."""

import json
from pathlib import Path

from harness.xtext import weighted_length

VECTORS = Path(__file__).parent.parent / "harness" / "review" / "web" / "src" / "lib" / "xtext.vectors.json"


def test_the_browsers_vectors_are_pythons_counts():
    vectors = json.loads(VECTORS.read_text(encoding="utf-8"))
    assert len(vectors) >= 20
    assert [weighted_length(v["text"]) for v in vectors] == [v["weight"] for v in vectors]
