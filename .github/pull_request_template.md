## What changed

<!-- The problem and what the harness does now. Name the stage (observe, orient,
     decide, qualify, learn, memory or review) and the lane (reply, post or both). -->

## Why

<!-- The reason or the trade-off. Don't repeat the section above. -->

## Shared contracts

<!-- List any change to harness/models.py, ACTION_TYPES, the SQLite schema in
     harness/memory.py, a prompt's JSON shape, or out/approved-run<id>.json.
     Say who has to update their branch. Write "None" if nothing changed. -->

## Sample output

<!-- Paste 2 to 5 recommendations or eval numbers this branch produced, with the
     provider, model and sources you ran. We compare branches on this output.
     Remove this section for changes with no output, such as docs. -->

## UI changes

<!-- Before and after screenshots of harness/review/ at the same state.
     Remove this section when nothing visible changes. -->

## Verification

<!-- Commands you ran and what they returned. Say what you didn't test. -->

## Checklist

- [ ] `python -m harness run --provider mock` finishes on this branch
- [ ] Nothing in this PR posts to X or calls an X write endpoint
- [ ] No keys, cookies or session tokens are committed
- [ ] Brand-specific text lives in `mission/` or a prompt file, not in Python
- [ ] I told the team about any shared-contract change, or there is none
