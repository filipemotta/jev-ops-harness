"""Decision clients with one interface: evaluate(state, questions) -> answers.

Two implementations:
- HttpClient: calls the real TypeSafe endpoint (POST /v1/systemone). Needs
  TYPESAFE_API_KEY. TYPESAFE_BASE_URL lets you point it at a gateway.
- StubClient: deterministic keyword heuristics that return the same answer
  shape as the API, so the harness runs offline with no account.

The answer shape follows https://docs.typesafe.ai/api (checked 2026-09-24):
  noul   -> {"type": "noul", "noul": 0.0..1.0}
  choice -> {"type": "choice", "choice": str, "probabilities": {opt: p}, "confidence": c}
  score  -> {"type": "score", "score": float, "legend": {"0": ...}, "probabilities": {"0": p}, "confidence": c}
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

DEFAULT_BASE_URL = "https://api.typesafe.ai"
DEFAULT_MODEL = "jev-latest"


class HttpClient:
    """Thin client for the System One endpoint. No SDK, stdlib only."""

    def __init__(self, api_key: str | None = None, base_url: str | None = None,
                 model: str = DEFAULT_MODEL, timeout: float = 10.0) -> None:
        self.api_key = api_key or os.environ.get("TYPESAFE_API_KEY")
        if not self.api_key:
            raise RuntimeError("TYPESAFE_API_KEY is not set")
        self.base_url = (base_url or os.environ.get("TYPESAFE_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.model = model
        self.timeout = timeout

    def evaluate(self, state, questions: dict, model: str | None = None) -> dict:
        body = json.dumps({"state": state, "model": model or self.model, "questions": questions}).encode()
        req = urllib.request.Request(
            f"{self.base_url}/v1/systemone",
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as err:
            # 429 and 529 are documented as retry-with-backoff; the harness surfaces them.
            detail = err.read().decode(errors="replace")
            raise RuntimeError(f"TypeSafe API returned {err.code}: {detail}") from err


def confidence_from(probabilities: dict) -> float:
    """Collapse a distribution into one number between 0 and 1.

    The docs describe confidence as derived from how concentrated the
    distribution is and use (n * peak - 1) / (n - 1) as the illustrative
    formula. The real service computes its own value; this is only for the stub.
    """
    values = list(probabilities.values())
    n = len(values)
    if n < 2:
        return 1.0
    peak = max(values)
    return max(0.0, min(1.0, (n * peak - 1) / (n - 1)))


def _normalize(weights: dict) -> dict:
    total = sum(weights.values())
    if total <= 0:
        share = 1.0 / len(weights)
        return {k: round(share, 3) for k in weights}
    return {k: round(v / total, 3) for k, v in weights.items()}


class StubClient:
    """Deterministic stand-in for offline runs.

    It scores each option by counting keyword hits from the option's own
    criteria text (plus a small list of synonyms) against the serialized
    state. That is enough to exercise the routing code; it is not a model
    and it does not pretend to be calibrated.
    """

    SYNONYMS = {
        "database": ["5432", "postgres", "max_connections", "connection", "pool", "rds", "sql"],
        "network": ["dns", "packet", "loss", "nlb", "alb", "cni", "reset by peer"],
        "platform": ["node", "evict", "scheduler", "autoscaler", "notready", "control plane"],
        "application": ["deployed", "config", "release", "code change", "exception", "nullpointer"],
        "credential": ["denied", "token", "expired", "unauthorized", "403", "permission", "secret"],
        "dependency": ["could not resolve", "404", "not found", "checksum", "registry", "npm err", "pip"],
        "runner": ["no space left", "oom", "killed", "timeout", "cancelled", "disk"],
        "code": ["assert", "failed test", "syntaxerror", "compile", "lint"],
    }

    def evaluate(self, state, questions: dict, model: str | None = None) -> dict:
        text = json.dumps(state).lower()
        answers = {}
        for key, question in questions.items():
            qtype = question["type"]
            if qtype == "choice":
                answers[key] = self._choice(text, question)
            elif qtype == "score":
                answers[key] = self._score(text, question)
            elif qtype == "noul":
                answers[key] = self._noul(text, question)
            else:
                raise ValueError(f"unknown question type: {qtype}")
        return {"model": "stub-0.1", "answers": answers,
                "usage": {"input_tokens": len(text) // 4, "output_tokens": 0}}

    def _hits(self, text: str, option: str, description: str | None) -> int:
        words = set()
        if description:
            cleaned = re.sub(r"[^a-z0-9_ ]", " ", description.lower())
            words.update(w for w in cleaned.split() if len(w) > 4)
        score = sum(text.count(w) for w in words)
        # Domain synonyms count double: they are the signal an operator would look for.
        score += 2 * sum(text.count(w) for w in self.SYNONYMS.get(option, []))
        return score

    def _choice(self, text: str, question: dict) -> dict:
        weights = {}
        for option, description in question["criteria"].items():
            hits = self._hits(text, option, description if isinstance(description, str) else None)
            weights[option] = 0.05 if option == "unknown" else hits + 0.01
        probabilities = _normalize(weights)
        choice = max(probabilities, key=probabilities.get)
        return {"type": "choice", "choice": choice, "probabilities": probabilities,
                "confidence": round(confidence_from(probabilities), 3)}

    def _score(self, text: str, question: dict) -> dict:
        levels = question["criteria"]
        weights = {}
        for index, description in enumerate(levels):
            weights[str(index)] = self._hits(text, f"level{index}", description) + 0.01
        # Bias toward the middle level when nothing matches, so the stub never
        # returns extreme scores on empty evidence.
        if all(v <= 0.02 for v in weights.values()):
            weights[str(len(levels) // 2)] += 1
        probabilities = _normalize(weights)
        score = sum(int(k) * p for k, p in probabilities.items())
        legend = {str(i): d for i, d in enumerate(levels)}
        return {"type": "score", "score": round(score, 2), "legend": legend,
                "probabilities": probabilities, "confidence": round(confidence_from(probabilities), 3)}

    def _noul(self, text: str, question: dict) -> dict:
        criteria = question.get("criteria")
        if criteria:
            # Compare hits for the "yes" description against hits for the "no" description.
            yes = self._hits(text, "yes", str(criteria.get("true", "")))
            no = self._hits(text, "no", str(criteria.get("false", "")))
            value = (0.5 + yes) / (1.0 + yes + no)
        else:
            # Crude fallback: count content words from the question in the state.
            words = [w.strip("`?.,") for w in question["instructions"].lower().split() if len(w) > 5]
            value = min(0.95, 0.15 + 0.2 * sum(1 for w in words if w in text))
        return {"type": "noul", "noul": round(min(0.95, max(0.05, value)), 2)}


def make_client(live: bool):
    """Pick the real client when asked and configured, otherwise the stub."""
    if live:
        return HttpClient()
    return StubClient()
