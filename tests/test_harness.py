"""Offline tests: answer shapes match the documented API and policies route as intended."""
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
