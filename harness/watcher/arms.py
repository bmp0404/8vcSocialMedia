"""The `_sgu_` protocol's arms: which kind of reply the operator is offered for a hot target.

The operator posts by hand from `_sgu_`; the harness never posts. For each hot target a seeded coin
picks one arm, so later outcomes can compare arms on targets that were assigned at random rather
than chosen by whoever was at the keyboard:

- continuation: the reply lane's continuation writer, forced on however few replies the thread has.
- human: no draft. The operator is asked for a one-liner of their own, so no draft anchors it.
- styled: the reply lane's styled-prompt fallback, forced on however many replies the thread has.

The coin hashes the seed and the status id, so the same post always gets the same arm, on any
machine and after any restart, and changing the seed starts a new assignment.
"""

import hashlib

ARMS = ("continuation", "human", "styled")
DEFAULT_SEED = "sgu-arms-1"


def arm_for(status_id: str, seed: str = DEFAULT_SEED) -> str:
    """One of ARMS, uniformly, determined by (seed, status_id) alone."""
    if not str(status_id).isdigit():
        raise ValueError(f"arm_for needs an X status id, got {status_id!r}")
    digest = hashlib.sha256(f"{seed}:{status_id}".encode("utf-8")).digest()
    return ARMS[int.from_bytes(digest[:8], "big") % len(ARMS)]
