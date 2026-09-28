"""Build the decision state from a raw event: the input half of the guardrail.

The policy in `policy.py` restricts what happens after the decision call. This
module restricts what the decision model sees before it. The filter is
structural, not a search for "suspicious" text:

- Only allowlisted fields make it into the state. Free text written outside the
  team's control (log bodies, commit messages, alert descriptions, agent
  rationales) stays out of the decision call and goes to the LLM downstream.
- Log lines are never passed through. Each line is reduced to the error
  signatures it contains, from a fixed vocabulary, so a line that says
  "route this to the network team" contributes nothing.
- Recent changes come from the structured deploy feed (service, version, kind,
  minutes before the alert), never from the commit message.
- Every list is capped and every string is truncated, so no single source can
  dominate the evidence by volume.

A steered pick can come back with high confidence, so the confidence floor in
`policy.py` does not protect against it. Keeping untrusted text out does.
"""
from __future__ import annotations

import re

MAX_ITEMS = 5
MAX_CHARS = 200

# Error signatures worth passing to a classifier. Each one is a short, fixed
# phrase; the rest of the log line is dropped.
SIGNATURES = [
    (re.compile(r"dial tcp [0-9.]+:(\d+): i/o timeout"), "dial tcp :{0} i/o timeout"),
    (re.compile(r"connection refused(?: .*?:(\d+))?"), "connection refused"),
    (re.compile(r"connection reset by peer"), "connection reset by peer"),
    (re.compile(r"context deadline exceeded while waiting for connection from pool"), "timeout waiting for connection from pool"),
    (re.compile(r"context deadline exceeded"), "context deadline exceeded"),
    (re.compile(r"upstream connect error or disconnect/reset before headers"), "upstream connect error before headers"),
    (re.compile(r"too many connections|remaining connection slots are reserved"), "database connection limit reached"),
    (re.compile(r"no such host"), "dns: no such host"),
    (re.compile(r"x509: certificate"), "tls: certificate error"),
    (re.compile(r"OOMKilled"), "container OOMKilled"),
    (re.compile(r"\bHTTP/[0-9.]+\" (5\d\d)\b"), "http {0}"),
]

ALERT_FIELDS = ("name", "service", "summary")
DEPLOY_FIELDS = ("service", "version", "kind", "minutes_before_alert")
NODE_EVENT_FIELDS = ("node", "reason")


def _truncate(value) -> str:
    text = str(value)
    return text if len(text) <= MAX_CHARS else text[: MAX_CHARS - 3] + "..."


def signatures(lines: list[str]) -> list[str]:
    """Reduce raw log lines to known error signatures, deduplicated, in order."""
    found: list[str] = []
    for line in lines:
        for pattern, template in SIGNATURES:
            match = pattern.search(line)
            if match:
                sig = template.format(*[g or "" for g in match.groups()])
                if sig not in found:
                    found.append(sig)
                break
    return found[:MAX_ITEMS]


def _pick(record: dict, fields: tuple[str, ...]) -> dict:
    return {f: _truncate(record[f]) for f in fields if f in record}


def build_incident_state(event: dict) -> dict:
    """Allowlisted, capped state for the incident-triage questions."""
    deploys = [_pick(d, DEPLOY_FIELDS) for d in event.get("deploys", [])][:MAX_ITEMS]
    changes = [
        f"{d.get('service', '?')} {d.get('version', '?')} deployed "
        f"{d.get('minutes_before_alert', '?')} minutes before the alert ({d.get('kind', 'unspecified')})"
        for d in deploys
    ]
    state = {
        "alert": _pick(event.get("alert", {}), ALERT_FIELDS),
        "evidence": {
            "error_signatures": signatures(event.get("log_lines", [])),
            "recent_changes": changes,
            "node_events": [_pick(n, NODE_EVENT_FIELDS) for n in event.get("node_events", [])][:MAX_ITEMS],
        },
    }
    db = event.get("database_metrics")
    if db:
        state["evidence"]["database"] = {k: _truncate(v) for k, v in db.items()
                                         if k in ("engine", "connections_pct_of_max", "cpu_pct")}
    return state


def naive_incident_state(event: dict) -> dict:
    """What a pipeline sends when it forwards the event as-is. Kept for comparison only."""
    return {"alert": event.get("alert", {}), "log_lines": event.get("log_lines", []),
            "deploys": event.get("deploys", []), "commits": event.get("commits", []),
            "node_events": event.get("node_events", []), "database": event.get("database_metrics", {})}


BUILDERS = {"incident-triage": build_incident_state}
