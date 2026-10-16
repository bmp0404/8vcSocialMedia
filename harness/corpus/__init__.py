"""Corpus of dev-tool posts and replies, and the benchmarks their own engagement labels.

`load()` reads the frozen snapshot in `data/corpus/raw/`. See DESIGN.md in this folder.
"""

from harness.corpus.load import Corpus, Item, created_at, load

__all__ = ["Corpus", "Item", "created_at", "load"]
