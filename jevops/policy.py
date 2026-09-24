"""Routing policy: code decides what to do with a typed answer.

Three outcomes, as in the TypeSafe confidence guidance: act automatically,
proceed with review, or escalate. The thresholds below are starting points
for shadow mode, not tuned values. Adjust them per case after comparing
model decisions with what the team actually did.
"""
from __future__ import annotations

from dataclasses import dataclass

ACT = "act"
REVIEW = "review"
ESCALATE = "escalate"

# Confidence floor below which no automated action is taken for any case.
CONFIDENCE_FLOOR = 0.5


@dataclass
class Decision:
    outcome: str
    reason: str
    handoff: str  # what the next stage (LLM, human, code) receives


def incident_triage(answers: dict) -> Decision:
    domain = answers["domain"]
    severity = answers["severity"]
    thin = answers["needs_more_evidence"]["noul"]
    if thin > 0.6:
        return Decision(ESCALATE, "model reports evidence is too thin", "collect more evidence, then re-evaluate")
    if domain["confidence"] < CONFIDENCE_FLOOR or domain["choice"] == "unknown":
        return Decision(REVIEW, "no clear owner", "page the on-call coordinator with the full distribution")
    handoff = f"route to {domain['choice']}; LLM investigates with the {domain['choice']}-specific runbook context"
    if severity["score"] >= 1.5:
        return Decision(ACT, f"clear owner ({domain['choice']}) and high severity", handoff + "; page immediately")
    return Decision(ACT, f"clear owner ({domain['choice']})", handoff)


def ci_failure(answers: dict) -> Decision:
    failure = answers["failure_class"]
    retry = answers["retry_may_help"]["noul"]
    if failure["confidence"] < CONFIDENCE_FLOOR or failure["choice"] == "unknown":
        return Decision(REVIEW, "failure class unclear", "LLM reads the full log and proposes a cause")
    if failure["choice"] in ("credential", "runner") and retry > 0.5:
        return Decision(ACT, f"{failure['choice']} failure, retry likely to help", "re-run the job once, then re-evaluate")
    if failure["choice"] == "code":
        return Decision(ACT, "code failure", "LLM explains the failing test against the changed files and proposes a fix")
    return Decision(REVIEW, f"{failure['choice']} failure needs a human", "notify the pipeline owner with the classification")


def action_gate(answers: dict) -> Decision:
    risk = answers["risk"]
    matches = answers["matches_rationale"]["noul"]
    prod = answers["targets_production"]["noul"]
    if matches < 0.7:
        return Decision(ESCALATE, "command does not match the stated rationale", "block; ask the agent to restate or a human to approve")
    if risk["confidence"] < CONFIDENCE_FLOOR:
        return Decision(REVIEW, "risk level unclear", "human approves before execution")
    if risk["score"] < 0.5:
        return Decision(ACT, "read-only command", "execute within existing permissions")
    if risk["score"] < 1.5 and prod < 0.5:
        return Decision(ACT, "reversible change outside production", "execute and record the rollback step")
    return Decision(REVIEW, "mutating change in production or hard to reverse", "human approves; rollback step attached")


POLICIES = {
    "incident-triage": incident_triage,
    "ci-failure": ci_failure,
    "action-gate": action_gate,
}
