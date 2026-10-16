"""Two parallel lanes over one memory layer. Each `run(ctx, signals)` returns evaluated actions
(vetoed ones flagged, best first) and has already written them to memory."""

from harness.lanes import post, reply
from harness.lanes.common import LaneContext

LANES = {"reply": reply.run, "post": post.run}

__all__ = ["LANES", "LaneContext"]
