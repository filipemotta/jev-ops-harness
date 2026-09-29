"""Version the rules, not only the model.

A decision depends on three things the team owns: the thresholds in
`policy.py`, the state builder in `state.py`, and the question text sent with
each call (`instructions` and `criteria`). What counts as "flaky" or
"credential" is written in those criteria, so editing them is a policy change
as much as moving a threshold is.

`policy_version()` hashes all three into a short id. Every logged decision
carries it next to the `model` the API reports, so a shadow report never mixes
decisions made under different rules, and a replay can say which version a
decision came from.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

_RULE_FILES = [Path(__file__).with_name("policy.py"), Path(__file__).with_name("state.py")]


def policy_version(questions: dict) -> str:
    digest = hashlib.sha256()
    for path in _RULE_FILES:
        digest.update(path.read_bytes())
    digest.update(json.dumps(questions, sort_keys=True, separators=(",", ":")).encode())
    return digest.hexdigest()[:12]
