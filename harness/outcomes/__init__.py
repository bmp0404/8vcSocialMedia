"""Outcomes: did 8VC's bot post what we approved, and how did it do?

`harness outcomes sync` reads the approved hand-off files and the account's own X timeline, matches
each approved action to the post the bot made from it, snapshots that post's counts at +1h, +6h,
+24h and +48h, labels each snapshot against the account's own earlier posts at the same age, and
writes the result to memory's `outcomes` table with `memory.record_outcomes`. `harness outcomes
status` prints where every approved action stands. Nothing here writes to X. Design notes are in
DESIGN.md next to this file.
"""
