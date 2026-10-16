"""Named experiment batches. Each proposal here is motivated by a measured research finding, cited
in `proposer.source`, so the run spends its small Benjamini-Hochberg family on likely effects."""

from harness.iterate.runner import Proposal

RESEARCH = {"kind": "manual"}

ACTION_CONTINUATION = Proposal(
    "action_lineup", {"action.reply.generation": "continuation"},
    "Continuing the real thread as a document on claude-opus-4-5 reads less like AI than the persona JSON call: "
    "research measured forced-choice detection of 31% against 100%.",
    {**RESEARCH, "source": "research/action.md, finding 2"}, "continuation (claude-opus-4-5, prefill)")

ACTION_STYLED = Proposal(
    "action_lineup", {"action.reply.generation": "styled"},
    "Short style rules on the same model read less like AI than the persona JSON call (research: 81% against 100%).",
    {**RESEARCH, "source": "research/action.md, findings 1 and 2"}, "styled prompt (sonnet)")

ACTION_PIPELINE = Proposal(
    "action_lineup", {"action.reply.generation": "pipeline", "action.samples": 4},
    "sam/action's reply lane (4 continuations, slop gate, two-order Opus veto, random pick) reads less like AI "
    "than the persona JSON call and is no less safe, because its veto removes the invented claims and insults "
    "plain continuation shipped in exp-20261016-action-f691d2.",
    {**RESEARCH, "source": "research/action.md, findings 2-4; exp-20261016-action-f691d2"},
    "sam/action reply lane, 4 samples")

QUALIFY_BATCH1 = Proposal(
    "qualify_auc", {"qualify.batch": 1},
    "Scoring each post in its own call separates viral from Deadweight better than scoring an account's posts "
    "together, because batched scores assimilate toward their neighbours (research: 0.12 AUC from order alone).",
    {**RESEARCH, "source": "research/eval.md, finding 1"}, "one post per call")

QUALIFY_READER = Proposal(
    "qualify_auc", {"qualify.panel": "single_reader"},
    "One no-persona reader separates viral from Deadweight posts within an account at least as well as the persona "
    "panel (Upworthy: tau 0.361 against 0.084).",
    {**RESEARCH, "source": "research/eval.md, finding 8"}, "single reader")

QUALIFY_GPT = Proposal(
    "qualify_auc", {"qualify.model": "gpt-6.1-sol"},
    "A GPT-family panel scores within-account AUC at least as well as Claude (research: 0.857 against 0.786), and "
    "avoids self-preference on Claude-written drafts.",
    {**RESEARCH, "source": "research/eval.md, finding 14"}, "panel on gpt-6.1-sol")

QUALIFY_MEMORY = Proposal(
    "qualify_auc", {"qualify.memory": "on"},
    "Showing the panel the account's own older posts with their outcomes raises within-account AUC.",
    {**RESEARCH, "source": "research/memory.md, findings 8 and 11"}, "memory on (same-account history)")

QUALIFY_MEMORY_SHUFFLED = Proposal(
    "qualify_auc", {"qualify.memory": "shuffled"},
    "Control for memory-on: the same older posts with their outcome labels shuffled. If this wins too, the gain "
    "comes from seeing examples, not from their outcomes.",
    {**RESEARCH, "source": "research/memory.md, finding 11"}, "memory shuffled (control)")

PLANS = {
    "first": [ACTION_CONTINUATION],
    "action": [ACTION_CONTINUATION, ACTION_STYLED],
    "pipeline": [ACTION_PIPELINE],
    "qualify": [QUALIFY_BATCH1, QUALIFY_READER, QUALIFY_MEMORY, QUALIFY_MEMORY_SHUFFLED],
    "qualify_gpt": [QUALIFY_GPT],
}
