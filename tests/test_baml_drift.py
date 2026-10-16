"""The committed baml_sdk was generated from the .baml files in eval/baml_src.

Editing a .baml file without regenerating leaves the old prompt running, because Python loads the
generated package, not the source. This catches it without the BAML toolchain; CI's baml job also
regenerates and diffs the rest of the package.
"""

import importlib.util

from harness.config import ROOT

SOURCES = ROOT / "eval" / "baml_src"


def test_generated_sources_match_baml_src():
    spec = importlib.util.spec_from_file_location("generated_sources", ROOT / "baml_sdk" / "_baml_sources.py")
    generated = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generated)
    on_disk = {p.relative_to(SOURCES).as_posix(): p.read_text(encoding="utf-8") for p in SOURCES.rglob("*.baml")}
    stale = sorted(name for name in on_disk.keys() | generated.FILES.keys() if on_disk.get(name) != generated.FILES.get(name))
    assert not stale, f"baml_sdk is stale for {stale}; run BAML_VERSION=0.22.2 baml generate --directory eval"
