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
import random
from concurrent.futures import ThreadPoolExecutor

from harness.llm import LLM, AnthropicLLM, MockLLM
from harness.minds.settings import ActionConfig

# Handles for the reply the model writes. Plain, not themed, so the code stays brand-agnostic.
PREFILL_HANDLES = ("realone_j", "kaymarie", "tuesdayfan", "dmo_44", "lo_gx", "brennnn", "itsnotkev", "marisolxo")

STYLED_SYSTEM = """You run a small account on X. The mission below sets its subjects and tone. Write replies the way a real person types them on a phone.
Most good replies are under 12 words. Lowercase is fine. One idea per reply. No hashtags, no em dashes, no emoji strings.
Don't summarize the post, don't explain the joke, don't sound like a brand. Punch at takes, not people. Never invent facts, names or stats.

MISSION:
{mission}"""

CAPTION_SYSTEM = """You caption posts for a small account on X that shares other people's media and news. The mission below sets its subjects and tone.
A caption reads like a text to a friend: under {max_chars} characters, one reaction or one detail, lowercase is fine. No hashtags, no em dashes, no emoji strings. Don't summarize the source, don't sound like a brand. Never add a fact, name or number the source doesn't contain.

MISSION:
{mission}"""


class WriterUnavailable(RuntimeError):
    """Continuation needs an Anthropic-gateway provider; callers fall back to the styled writer."""


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
    if isinstance(llm, MockLLM):
        return f"mock continuation {i + 1}"
    if not isinstance(llm, AnthropicLLM):
        raise WriterUnavailable(f"{type(llm).__name__} can't prefill; continuation needs the Anthropic gateway")
    r = llm.client.messages.create(
        model=cfg.continuation_model, max_tokens=80,
        messages=[{"role": "user", "content": "Saved page text:"}, {"role": "assistant", "content": f"{page}\n@{handle}:"}],
        stop_sequences=["\n@"],
    )
    return _first_line("".join(b.text for b in r.content if b.type == "text"))


def continuations(llm: LLM, author: str, post_text: str, replies: list[str], cfg: ActionConfig, seed: str) -> list[str]:
    """cfg.reply_samples continuations of the page, one per prefill handle."""
    page = build_page(author, post_text, replies[: cfg.page_replies])
    handles = random.Random(seed).sample(PREFILL_HANDLES, min(cfg.reply_samples, len(PREFILL_HANDLES)))
    with ThreadPoolExecutor(len(handles) or 1) as pool:
        return list(pool.map(lambda ih: continue_page(llm, page, ih[1], cfg, ih[0]), enumerate(handles)))


def _styled_llm(llm: LLM, model: str) -> LLM:
    return AnthropicLLM(model) if isinstance(llm, AnthropicLLM) else llm


def styled_replies(llm: LLM, mission: str, author: str, post_text: str, replies: list[str], cfg: ActionConfig) -> list[str]:
    others = "\n".join(f"- {r}" for r in replies[: cfg.page_replies]) or "(none yet)"
    user = (f'POST by @{author}: "{post_text}"\n\nOTHER REPLIES ALREADY IN THE THREAD:\n{others}\n\n'
            f'Write {cfg.reply_samples} different replies to the post. Return JSON: {{"replies": ["..."]}}')
    out = _styled_llm(llm, cfg.styled_model).complete_json(
        STYLED_SYSTEM.format(mission=mission), user,
        mock=lambda: {"replies": [f"mock styled reply {i + 1}" for i in range(cfg.reply_samples)]},
    )
    return [_first_line(x) for x in out.get("replies", []) if isinstance(x, str)][: cfg.reply_samples]


def captions(llm: LLM, mission: str, source: str, cfg: ActionConfig) -> list[str]:
    user = (f"SOURCE:\n{source}\n\nWrite {cfg.caption_samples} different captions. "
            'Return JSON: {"captions": ["..."]}')
    out = _styled_llm(llm, cfg.styled_model).complete_json(
        CAPTION_SYSTEM.format(mission=mission, max_chars=cfg.caption_max_chars), user,
        mock=lambda: {"captions": [f"mock caption {i + 1}" for i in range(cfg.caption_samples)]},
    )
    return [_first_line(x) for x in out.get("captions", []) if isinstance(x, str)][: cfg.caption_samples]


def page_json(author: str, post_text: str, replies: list[str]) -> str:
    """The page as JSON, for the decision log."""
    return json.dumps({"author": author, "post": post_text, "replies": replies}, ensure_ascii=False)
