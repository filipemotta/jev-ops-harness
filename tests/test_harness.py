"""Offline tests: answer shapes match the documented API and policies route as intended."""
import contextlib
import io
import json
import unittest
from pathlib import Path

from jevops.client import StubClient, confidence_from
from jevops.policy import ACT, ESCALATE, REVIEW, POLICIES

CASES = Path(__file__).resolve().parent.parent / "cases"


def load(name):
    return json.loads((CASES / f"{name}.json").read_text())


class AnswerShapeTest(unittest.TestCase):
    def test_every_case_returns_documented_shape(self):
        client = StubClient()
        for path in sorted(CASES.glob("*.json")):
            case = json.loads(path.read_text())
            response = client.evaluate(case["state"], case["questions"])
            self.assertEqual(set(response), {"model", "answers", "usage"})
            for key, question in case["questions"].items():
                answer = response["answers"][key]
                self.assertEqual(answer["type"], question["type"])
                if question["type"] == "noul":
                    self.assertTrue(0.0 <= answer["noul"] <= 1.0)
                elif question["type"] == "choice":
                    self.assertIn(answer["choice"], question["criteria"])
                    self.assertAlmostEqual(sum(answer["probabilities"].values()), 1.0, places=2)
                    self.assertTrue(0.0 <= answer["confidence"] <= 1.0)
                else:
                    self.assertEqual(len(answer["legend"]), len(question["criteria"]))
                    self.assertAlmostEqual(sum(answer["probabilities"].values()), 1.0, places=2)
                    self.assertTrue(0.0 <= answer["score"] <= len(question["criteria"]) - 1)

    def test_confidence_bounds(self):
        self.assertEqual(confidence_from({"a": 1.0, "b": 0.0, "c": 0.0}), 1.0)
        self.assertAlmostEqual(confidence_from({"a": 1 / 3, "b": 1 / 3, "c": 1 / 3}), 0.0, places=6)


class PolicyTest(unittest.TestCase):
    def test_incident_with_clear_owner_acts(self):
        answers = {
            "domain": {"type": "choice", "choice": "database", "confidence": 0.9,
                       "probabilities": {"database": 0.93, "application": 0.05, "network": 0.01, "platform": 0.01, "unknown": 0.0}},
            "severity": {"type": "score", "score": 1.8, "confidence": 0.8, "legend": {}, "probabilities": {}},
            "deploy_correlated": {"type": "noul", "noul": 0.9},
            "needs_more_evidence": {"type": "noul", "noul": 0.1},
        }
        decision = POLICIES["incident-triage"](answers)
        self.assertEqual(decision.outcome, ACT)
        self.assertIn("database", decision.handoff)

    def test_incident_with_thin_evidence_escalates(self):
        answers = {
            "domain": {"type": "choice", "choice": "network", "confidence": 0.9, "probabilities": {}},
            "severity": {"type": "score", "score": 1.0, "confidence": 0.9, "legend": {}, "probabilities": {}},
            "deploy_correlated": {"type": "noul", "noul": 0.2},
            "needs_more_evidence": {"type": "noul", "noul": 0.8},
        }
        self.assertEqual(POLICIES["incident-triage"](answers).outcome, ESCALATE)

    def test_low_confidence_never_acts(self):
        answers = {
            "failure_class": {"type": "choice", "choice": "credential", "confidence": 0.3, "probabilities": {}},
            "retry_may_help": {"type": "noul", "noul": 0.9},
            "touches_pipeline": {"type": "noul", "noul": 0.1},
        }
        self.assertEqual(POLICIES["ci-failure"](answers).outcome, REVIEW)

    def test_gate_blocks_command_that_exceeds_rationale(self):
        answers = {
            "risk": {"type": "score", "score": 0.1, "confidence": 0.95, "legend": {}, "probabilities": {}},
            "matches_rationale": {"type": "noul", "noul": 0.2},
            "targets_production": {"type": "noul", "noul": 0.9},
        }
        self.assertEqual(POLICIES["action-gate"](answers).outcome, ESCALATE)

    def test_gate_reviews_mutating_change_in_production(self):
        answers = {
            "risk": {"type": "score", "score": 1.1, "confidence": 0.9, "legend": {}, "probabilities": {}},
            "matches_rationale": {"type": "noul", "noul": 0.95},
            "targets_production": {"type": "noul", "noul": 0.95},
        }
        self.assertEqual(POLICIES["action-gate"](answers).outcome, REVIEW)


if __name__ == "__main__":
    unittest.main()


class StateFilterTest(unittest.TestCase):
    """The input half of the guardrail: untrusted text never reaches the decision call."""

    def setUp(self):
        from jevops.state import build_incident_state, naive_incident_state
        self.case = json.loads((CASES / "raw" / "incident-injected-log.json").read_text())
        self.built = build_incident_state(self.case["raw_event"])
        self.naive = naive_incident_state(self.case["raw_event"])

    def test_injected_text_is_absent_from_built_state(self):
        text = json.dumps(self.built).lower()
        for phrase in ("automated triage", "network team", "packet loss", "commit", "runbook"):
            self.assertNotIn(phrase, text)

    def test_evidence_is_reduced_to_signatures(self):
        sigs = self.built["evidence"]["error_signatures"]
        self.assertIn("dial tcp :5432 i/o timeout", sigs)
        self.assertIn("database connection limit reached", sigs)
        self.assertLessEqual(len(sigs), 5)

    def test_unfiltered_state_is_steered_past_the_confidence_floor(self):
        # The point of the test: a steered pick clears the policy's confidence
        # floor, so the output gate alone does not stop it.
        answers = StubClient().evaluate(self.naive, self.case["questions"])["answers"]
        self.assertEqual(answers["domain"]["choice"], "network")
        self.assertEqual(POLICIES["incident-triage"](answers).outcome, ACT)

    def test_built_state_routes_to_the_real_owner(self):
        answers = StubClient().evaluate(self.built, self.case["questions"])["answers"]
        self.assertEqual(answers["domain"]["choice"], "database")
        self.assertEqual(answers["domain"]["probabilities"]["network"], 0.0)

    def test_caps_hold_under_volume(self):
        from jevops.state import MAX_CHARS, MAX_ITEMS, build_incident_state
        event = {"alert": {"name": "x", "summary": "s" * 1000},
                 "log_lines": [f"dial tcp 10.0.0.{i}:{5000 + i}: i/o timeout" for i in range(50)],
                 "deploys": [{"service": "svc", "version": f"v{i}"} for i in range(50)]}
        state = build_incident_state(event)
        self.assertLessEqual(len(state["alert"]["summary"]), MAX_CHARS)
        self.assertLessEqual(len(state["evidence"]["error_signatures"]), MAX_ITEMS)
        self.assertLessEqual(len(state["evidence"]["recent_changes"]), MAX_ITEMS)


class VersioningTest(unittest.TestCase):
    """Rules are versioned with the model, and a replay shows what a change flips."""

    CORPUS = CASES.parent / "shadow" / "replay-corpus.jsonl"

    def test_model_is_pinned_not_an_alias(self):
        from jevops.client import DEFAULT_MODEL
        models = {DEFAULT_MODEL} | {json.loads(p.read_text())["model"] for p in CASES.rglob("*.json")}
        self.assertEqual(models, {"jev-1.13.0"})

    def test_policy_version_is_stable_and_tracks_criteria(self):
        import copy
        from jevops.versioning import policy_version
        questions = load("ci-failure")["questions"]
        self.assertEqual(policy_version(questions), policy_version(copy.deepcopy(questions)))
        # Redefining what "runner" means is a policy change, even with the same thresholds.
        edited = copy.deepcopy(questions)
        edited["failure_class"]["criteria"]["runner"] += " or a flaky test that passes on retry"
        self.assertNotEqual(policy_version(questions), policy_version(edited))

    def test_logged_record_carries_model_and_policy_version(self):
        import tempfile
        import harness
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "decisions.jsonl"
            with contextlib.redirect_stdout(io.StringIO()):
                harness.run(str(CASES / "ci-failure.json"), live=False, log=str(log))
            record = json.loads(log.read_text())
        self.assertEqual(record["model"], "stub-0.1")
        self.assertRegex(record["policy_version"], r"^[0-9a-f]{12}$")
        self.assertEqual(record["outcome"], ACT)
        self.assertIn("state", record)
        self.assertIsNone(record["human_choice"])

    def test_replay_of_corpus_has_no_flips(self):
        # Fails when a change to thresholds, state builder or criteria flips a
        # recorded decision. Re-record the corpus on purpose, in review.
        import harness
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = harness.replay(str(self.CORPUS), live=False, fail_on_flip=True)
        self.assertEqual(code, 0, out.getvalue())

    def test_replay_catches_a_threshold_change(self):
        import harness
        from unittest import mock
        out = io.StringIO()
        with mock.patch("jevops.policy.CONFIDENCE_FLOOR", 0.7), contextlib.redirect_stdout(out):
            code = harness.replay(str(self.CORPUS), live=False, fail_on_flip=True)
        self.assertEqual(code, 1)
        self.assertIn("ci-001", out.getvalue())
        self.assertIn("outcome: act -> review", out.getvalue())
