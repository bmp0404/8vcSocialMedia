"""Write reply and caption text without asking a chat model to "write a tweet".

Replies are written by continuation. The target post and its real replies become a saved web
page, and `claude-opus-4-5-20251101` (the one gateway model that accepts assistant prefill)
continues the page at the next `@handle:` line, stopping at the next reply. Context lines carry
neutral handles and no like counts, so the model copies the thread's register instead of writing
a "top reply". The research report (8vc-context/research/action.md, finding 2) measured this at
detection AUC 0.44 under both judges, against 1.00 for the persona prompt this replaced.

When the page would be too thin, or the provider can't prefill, the styled prompt on Opus 4.5
is the fallback (AUC 0.79). Captions for the post lane use the same styled prompt.
"""

import json
import os
import random
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from harness import config
from harness.llm import LLM
from harness.minds import routing
from harness.minds.settings import ActionConfig

# Handles for the reply the model writes. Plain, not themed, so the code stays brand-agnostic.
MOCK_WORDS = ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel")
PREFILL_HANDLES = ("realone_j", "kaymarie", "tuesdayfan", "dmo_44", "lo_gx", "brennnn", "itsnotkev", "marisolxo")

STYLED_SYSTEM = """You run a small account on X. The mission below sets its subjects and tone. Write replies the way a real person types them on a phone.
Most good replies are under 12 words. Lowercase is fine. One idea per reply. No hashtags, no em dashes, no emoji strings.
Don't summarize the post, don't explain the joke, don't sound like a brand. Punch at takes, not people. Never invent facts, names or stats.

MISSION:
{mission}"""

CAPTION_SYSTEM = """You caption posts for a small account on X that shares other people's media and news. The mission below sets its subjects and tone.
A caption reads like a text to a friend: under {max_chars} characters, lowercase is fine. It has to make sense on its own, even if the link preview never loads, so give your reaction together with the one fact it reacts to ("aliyah had knee surgery and still made all-star. absurd"). Don't credit the source or add a link; that gets added for you. No hashtags, no em dashes, no emoji strings, don't sound like a brand. Never add a fact, name or number the source doesn't contain.

MISSION:
{mission}"""


ACCOUNT_PATH = config.ROOT / "action" / "account.json"

BANTER_ANGLE = ("This post is about a rival of the account. Reply from the account's side with banter at the rival's "
                "take or team: a jab, never cheering them, never insulting a person.")


class WriterUnavailable(RuntimeError):
    """Continuation needs an Anthropic-gateway provider; callers fall back to the styled writer."""


@dataclass(frozen=True)
class Account:
    """The account the reply lane writes as (action/account.json)."""

    handle: str


def load_account(path: Path | None = None) -> Account | None:
    """The account file at HARNESS_ACCOUNT or action/account.json, or None when there is none."""
    path = path or Path(os.getenv("HARNESS_ACCOUNT") or ACCOUNT_PATH)
    if not path.exists():
        return None
    raw = {k: v for k, v in json.loads(path.read_text(encoding="utf-8")).items() if not k.startswith("_")}
    if set(raw) != {"handle"} or not isinstance(raw["handle"], str) or not raw["handle"].strip("@ "):
        raise ValueError(f"{path}: needs exactly one non-empty 'handle' string (keys starting with _ are notes); "
                         f"got keys {sorted(raw)}")
    return Account(raw["handle"].strip().lstrip("@"))


def build_page(author: str, post_text: str, replies: list[str]) -> str:
    lines = "\n".join(f"@user{i + 1:02d}: {r}" for i, r in enumerate(replies))
    return (f'x.com: {author} on X: "{post_text}"\n\n{author} @{author}\n{post_text}\n\n'
            f"Replies (sorted by relevance):\n\n{lines}")


def _first_line(text: str) -> str:
    t = text.strip().split("\n")[0].strip()
    if len(t) > 1 and t[0] in "\"“'" and t[-1] in "\"”'":
        t = t[1:-1].strip()
    return t


def continue_page(llm: LLM, page: str, handle: str, cfg: ActionConfig, i: int = 0) -> str:
    if routing.is_mock(llm):
        return f"mock continuation {MOCK_WORDS[i % len(MOCK_WORDS)]}"
    client = routing.messages_client(llm)
    if client is None:
        raise WriterUnavailable(f"{type(llm).__name__} can't prefill; continuation needs the Anthropic gateway")
    r = client.messages.create(
        model=cfg.continuation_model, max_tokens=80,
        messages=[{"role": "user", "content": "Saved page text:"}, {"role": "assistant", "content": f"{page}\n@{handle}:"}],
        stop_sequences=["\n@"],
    )
    return _first_line("".join(b.text for b in r.content if b.type == "text"))


def continuations(llm: LLM, author: str, post_text: str, replies: list[str], cfg: ActionConfig, seed: str,
                  account: Account | None = None) -> list[str]:
    """cfg.reply_samples continuations of the page: replying as `account` when given, else one
    neutral handle each. Replying as the account's handle is what puts the draft on the account's
    side; Iterate's retest found neutral handles on a rival's thread wrote as the rival's fans."""
    page = build_page(author, post_text, replies[: cfg.page_replies])
    if account:
        handles = [account.handle] * cfg.reply_samples
    else:
        handles = random.Random(seed).sample(PREFILL_HANDLES, min(cfg.reply_samples, len(PREFILL_HANDLES)))
    with ThreadPoolExecutor(len(handles) or 1) as pool:
        return list(pool.map(lambda ih: continue_page(llm, page, ih[1], cfg, ih[0]), enumerate(handles)))


def styled_replies(llm: LLM, mission: str, author: str, post_text: str, replies: list[str], cfg: ActionConfig,
                   angle: str = "") -> list[str]:
    others = "\n".join(f"- {r}" for r in replies[: cfg.page_replies]) or "(none yet)"
    user = (f'POST by @{author}: "{post_text}"\n\nOTHER REPLIES ALREADY IN THE THREAD:\n{others}\n\n'
            + (f"{angle}\n\n" if angle else "")
            + f'Write {cfg.reply_samples} different replies to the post. Return JSON: {{"replies": ["..."]}}')
    out = routing.on_model(llm, cfg.styled_model, "the styled reply writer").complete_json(
        STYLED_SYSTEM.format(mission=mission), user,
        mock=lambda: {"replies": [f"mock styled reply {MOCK_WORDS[i % len(MOCK_WORDS)]}" for i in range(cfg.reply_samples)]},
    )
    return [_first_line(x) for x in out.get("replies", []) if isinstance(x, str)][: cfg.reply_samples]


def captions(llm: LLM, mission: str, source: str, cfg: ActionConfig) -> list[str]:
    user = (f"SOURCE:\n{source}\n\nWrite {cfg.caption_samples} different captions. "
            'Return JSON: {"captions": ["..."]}')
    out = routing.on_model(llm, cfg.styled_model, "the caption writer").complete_json(
        CAPTION_SYSTEM.format(mission=mission, max_chars=cfg.caption_max_chars), user,
        mock=lambda: {"captions": [f"mock caption {MOCK_WORDS[i % len(MOCK_WORDS)]}" for i in range(cfg.caption_samples)]},
    )
    return [_first_line(x) for x in out.get("captions", []) if isinstance(x, str)][: cfg.caption_samples]
