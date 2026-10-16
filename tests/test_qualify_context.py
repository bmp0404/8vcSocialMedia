"""qualify's critic sees the context the action veto judged each draft against; the panel's input is unchanged."""

from harness.llm import LLM, MockLLM
from harness.minds import lanes
from harness.models import Action, Narrative
from harness.qualify import CRITIC_SYSTEM, PANEL_SYSTEM, qualify

FACTS = "SOURCE:\nBoston had knee surgery\n\nFACTS:\n- Clark sent Boston a four-word message"


class Recorder(LLM):
    def __init__(self):
        self.prompts = {}

    def complete_json(self, system, prompt, mock=None):
        self.prompts.setdefault(system.split("\n")[0], []).append(prompt)
        return mock()


def test_critic_gets_the_veto_context_and_the_panel_does_not():
    draft = Action(type="post", text="cc's four words did more than a press conference", mind="caption",
                   narrative_id="n", draft_meta={"veto_context": FACTS})
    bare = Action(type="post", text="a take", mind="caption", narrative_id="n")
    llm = Recorder()
    qualify(llm, "MISSION", [draft, bare], panel={"fan": "a fan"}, ranker="panel")
    (critic,) = llm.prompts[CRITIC_SYSTEM.split("\n")[0]]
    assert "four-word message" in critic
    assert critic.count('"context"') == 1  # only the draft that has one
    panel = llm.prompts[PANEL_SYSTEM.split("\n")[0]]
    assert panel and not any("four-word message" in p for p in panel)


def test_a_mock_caption_carries_the_context_its_veto_saw():
    n = Narrative(id="n", title="Boston surgery", summary="Boston had knee surgery", relevance=0.9, velocity=0.9,
                  signal_urls=["https://www.espn.com/wnba/story/_/id/1/boston"])
    actions, _ = lanes.run(MockLLM(), "MISSION", "", [n], [], takes=[])
    (caption,) = [a for a in actions if a.mind == "caption"]
    context = caption.draft_meta["veto_context"]
    assert context.startswith("SOURCE:\nBoston surgery") and "FACTS:" in context
