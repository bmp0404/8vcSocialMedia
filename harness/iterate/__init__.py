"""Self-iteration: the harness proposes changes to its own configuration, tests them against the
champion on the same items, and keeps only changes that clear a paired statistical gate.

Read eval/experiments/DESIGN.md for the design. Modules:
- knobs: every tunable, typed and versioned, plus config hashing and diffs.
- stats: paired cluster bootstrap, Benjamini-Hochberg and the promotion rule.
- fitness: frozen measurement instruments (action lineup, qualify AUC, memory ablation, observe recall).
- propose: rule-based and LLM proposers that may only change registered knobs.
- runner: runs one batch of experiments under a call budget and writes the ledger.
- ledger: one JSON file per experiment in eval/experiments/, the champion pointer and CHANGELOG.md.
"""
