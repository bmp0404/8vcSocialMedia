"""Which continuation framing keeps drafts on the account's side? 4 threads x 4 samples per framing,
scored by UI artifacts or content-free lines and by the Opus veto. Prints the table in
action/DESIGN.md ("Writing from the account's side").

    HARNESS_PROVIDER=anthropic uv run python -m harness.minds.lab.framing_probe
"""
import json, re, random
from concurrent.futures import ThreadPoolExecutor
from harness import config
from harness.llm import AnthropicLLM
from harness.minds import gate, veto, writer
from harness.minds.lab.reply_experiment import load_threads, page_replies, CONTEXT
from harness.minds.settings import ActionConfig

cfg = ActionConfig(); llm = AnthropicLLM("claude-sonnet-5-5"); mission = config.mission()
test, _ = load_threads(19)
pick = {t["id"][-6:]: t for t in test}
threads = [pick[k] for k in ("339402", "668010", "145977", "024541")]
ART = re.compile(r"\[|\]|draft|reply box|writing area|replying to|write a|post box", re.I)

def page(t, variant):
    reps = [r["text"].replace("\n", " / ") for r in page_replies(t)][:12]
    base = writer.build_page(t["author"], t["text"], reps)
    if variant == "neutral":
        return base, random.Random(t["id"]).choice(writer.PREFILL_HANDLES)
    if variant == "handle":
        return base, writer.load_framing().framing_handle
    if variant == "named":
        return base, f"{writer.load_framing().framing_handle} (Caitlin Clark Fan Account)"

def gen(t, variant):
    p, h = page(t, variant)
    return [writer.continue_page(llm, p, h, cfg, i) for i in range(4)]

rows = {}
with ThreadPoolExecutor(6) as pool:
    futs = {(t["id"], v): pool.submit(gen, t, v) for t in threads for v in ("neutral", "handle", "named")}
    for k, f in futs.items(): rows[k] = f.result()
for v in ("neutral", "handle", "named"):
    texts_all, art, passed, total = [], 0, 0, 0
    for t in threads:
        texts = rows[(t["id"], v)]
        ctx = f"POST by @{t['author']}:\n{t['text']}\n\nTHREAD:\n" + "\n".join("- " + r["text"] for r in page_replies(t)) + f"\n\nFACTS:\n{mission}\n\n{CONTEXT}"
        art += sum(bool(ART.search(x)) or not gate.check(x, "reply", post=t["text"]).passed and any(f.rule == "content_free" for f in gate.check(x, "reply", post=t["text"]).findings) for x in texts)
        ok = [x for x in texts if gate.check(x, "reply", context=ctx, post=t["text"]).passed]
        res = veto.veto(llm, cfg, ok, lane="reply", context=ctx, mission=mission) if ok else []
        passed += sum(r.passed for r in res); total += len(texts)
        texts_all += [(t["author"], x, next((("PASS" if r.passed else r.why) for r in res if r.text == x), "gate")) for x in texts]
    print(f"\n=== {v}: artifacts/content-free {art}/{total}, veto passed {passed}/{total}")
    for a, x, s in texts_all: print(f"  {a:<13} {s[:26]:<26} {x[:95]!r}")
