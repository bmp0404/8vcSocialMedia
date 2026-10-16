import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from harness import config, memory
from harness.llm import MockLLM
from harness.minds import lanes
from harness.models import Action, Narrative, Signal
from engine.runner import Components, Options, run_loop
from engine.runtime import runtime


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.llm = MockLLM()
        self.action = Action(id="draft-1", type="post", text="A draft", mind="test", narrative_id="story")
        self.components = Components(
            read_memory=Mock(return_value="Prior lesson"),
            observe=Mock(return_value=[Signal(source="fixture", text="A release", url="https://example.com/1")]),
            orient=Mock(return_value=[Narrative(id="story", title="Release", summary="A release")]),
            decide=Mock(return_value=[self.action]),
            evaluate=Mock(side_effect=lambda llm, mission, actions, narratives, signals: (actions, {"test": True})),
            save_run=Mock(return_value=7),
            end_round=Mock(return_value=[]),
            finish_memory=Mock(),
        )

    def run_engine(self, **options):
        return run_loop(self.llm, "Mission", ["sample"], Options(**options), self.components, self.root)

    def test_round_wires_memory_observation_action_eval_and_persistence(self):
        result = self.run_engine()[0]
        self.assertEqual(result["status"], "completed")
        self.assertEqual([s["name"] for s in result["stages"]],
                         ["memory_read", "observe", "orient", "action", "eval", "persist", "memory_write"])
        self.assertIn("Prior lesson", self.components.observe.call_args.args[1])
        self.assertEqual(self.components.decide.call_args.args[2], "Prior lesson")
        self.components.save_run.assert_called_once_with([self.action], account="_sgu_")
        self.components.finish_memory.assert_called_once_with(self.llm)
        self.components.end_round.assert_called_once_with(7)
        artifact = json.loads(Path(result["recommendations_path"]).read_text())
        self.assertEqual(artifact["run_id"], 7)
        self.assertEqual(artifact["recommendations"][0]["id"], "draft-1")

    def queue_abstention(self, *args, **kwargs):
        lanes.PENDING.append(lanes.Decision(lanes.REPLY_LANE, "https://x.com/a/status/1", "abstain", "stale_target",
                                            "posted 95 minutes ago"))
        return []

    def test_round_writes_the_decision_log_with_its_abstentions(self):
        self.addCleanup(lanes.PENDING.clear)
        self.components.decide.side_effect = self.queue_abstention
        result = self.run_engine()[0]
        self.assertEqual(result["decisions_path"], str(self.root / "decisions-run7.json"))
        log = json.loads((self.root / "decisions-run7.json").read_text())
        self.assertEqual(log["run_id"], 7)
        self.assertEqual([(d["tool"], d["reason"], d["detail"]) for d in log["decisions"]],
                         [("abstain", "stale_target", "posted 95 minutes ago")])
        self.assertEqual(lanes.PENDING, [])

    def test_failed_round_drops_its_queued_decisions(self):
        self.addCleanup(lanes.PENDING.clear)
        self.components.decide.side_effect = lambda *a, **k: self.queue_abstention() or [self.action]
        self.components.evaluate.side_effect = RuntimeError("judge unavailable")
        with self.assertRaises(RuntimeError):
            self.run_engine()
        self.assertEqual(lanes.PENDING, [])
        self.assertFalse(list(self.root.glob("decisions-run*.json")))

    def test_recommendations_name_the_account(self):
        artifact = json.loads(Path(self.run_engine()[0]["recommendations_path"]).read_text())
        self.assertEqual(artifact["account"], "_sgu_")

    def test_retry_receives_critique_and_preserves_vetoed_history(self):
        revised = Action(id="draft-2", type="post", text="Revised", mind="test", narrative_id="story")
        self.components.decide.side_effect = [[self.action], [revised]]
        def evaluate(llm, mission, actions, narratives, signals):
            actions[0].vetoed = actions[0].id == "draft-1"
            actions[0].critique = "Needs support" if actions[0].vetoed else ""
            return ([a for a in actions if not a.vetoed], {})
        self.components.evaluate.side_effect = evaluate
        result = self.run_engine(retries=2)[0]
        self.assertEqual(result["actions"], 2)
        self.assertEqual(self.components.decide.call_args.kwargs["vetoed"][0].critique, "Needs support")
        self.components.save_run.assert_called_once_with([self.action, revised], account="_sgu_")

    def test_no_signals_is_abstention_and_still_finishes_memory(self):
        self.components.observe.return_value = []
        result = self.run_engine()[0]
        self.assertEqual(result["reason"], "no_signals")
        self.assertEqual(result["status"], "abstained")
        self.components.decide.assert_not_called()
        self.components.save_run.assert_called_once_with([], account="_sgu_")
        self.components.finish_memory.assert_called_once()

    def test_empty_action_is_not_retried(self):
        self.components.decide.return_value = []
        self.assertEqual(self.run_engine(retries=3)[0]["status"], "abstained")
        self.assertEqual(self.components.decide.call_count, 1)
        self.components.evaluate.assert_not_called()

    def test_eval_failure_records_stage_without_publishing(self):
        self.components.evaluate.side_effect = RuntimeError("judge unavailable")
        with self.assertRaisesRegex(RuntimeError, "judge unavailable"):
            self.run_engine()
        journal = json.loads(next(self.root.glob("engine/*/round.json")).read_text())
        self.assertEqual((journal["status"], journal["stage"]), ("failed", "eval"))
        self.components.save_run.assert_not_called()
        self.components.finish_memory.assert_not_called()
        self.assertFalse(list(self.root.glob("recommendations*.json")))
        self.assertTrue(list(self.root.glob("engine/*/actions.json")))

    def test_memory_failure_preserves_saved_run_and_records_failure(self):
        self.components.finish_memory.side_effect = RuntimeError("memory unavailable")
        with self.assertRaises(RuntimeError):
            self.run_engine()
        journal = json.loads(next(self.root.glob("engine/*/round.json")).read_text())
        self.assertEqual((journal["status"], journal["stage"], journal["run_id"]), ("failed", "memory_write", 7))
        self.assertTrue(Path(journal["recommendations_path"]).exists())

    def test_next_round_rereads_memory(self):
        self.components.read_memory.side_effect = ["Before", "After"]
        self.run_engine(rounds=2)
        self.assertEqual([c.args[2] for c in self.components.decide.call_args_list], ["Before", "After"])
        self.assertEqual(self.components.finish_memory.call_count, 2)

    def test_invalid_evaluator_output_cannot_publish(self):
        unknown = Action(id="unknown", type="post", text="Oops", mind="test", narrative_id="story")
        self.components.evaluate.side_effect = lambda *args: ([unknown], {})
        with self.assertRaisesRegex(ValueError, "unknown"):
            self.run_engine()
        self.components.save_run.assert_not_called()

    def test_options_reject_invalid_loop_controls(self):
        for option in ({"rounds": 0}, {"interval": -1}, {"retries": -1}, {"top": 0}):
            with self.assertRaises(ValueError):
                Options(**option)

    def test_runtime_isolates_and_restores_config(self):
        original_db, original_provider = config.DB_PATH, config.PROVIDER
        with patch.dict(os.environ, {"MEMSTORE_PATH": "previous-store"}):
            with runtime("mock", ["sample"], self.root) as out:
                self.assertEqual(config.DB_PATH, self.root / "harness.db")
                self.assertEqual(out, self.root / "out")
                self.assertEqual(os.environ["MEMSTORE_PATH"], str(self.root / "store"))
                run_id = memory.save_run([])
                self.assertEqual(memory.list_runs()[0]["provider"], "mock")
                self.assertEqual(run_id, 1)
            self.assertEqual(os.environ["MEMSTORE_PATH"], "previous-store")
        self.assertEqual((config.DB_PATH, config.PROVIDER), (original_db, original_provider))

    def test_real_mock_pipeline_keeps_rounds_in_same_isolated_database(self):
        with runtime("mock", ["sample"], self.root) as output:
            results = run_loop(self.llm, "Test mission", ["sample"], Options(rounds=2), Components(), output)
            self.assertEqual([r["run_id"] for r in results], [1, 2])
            self.assertEqual(len(memory.list_runs()), 2)
            self.assertFalse(config.LESSONS_PATH.exists())


if __name__ == "__main__":
    unittest.main()
