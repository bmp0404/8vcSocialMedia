"""Integration tooling: record a live `harness run`, measure each gate, and format the hand-off.

`live` wraps the real CLI run and keeps the signals, narratives and decision logs that `cli.run`
holds only in memory. `measure` audits that record. `handoff` writes the files for 8VC's Grok bot.
"""
