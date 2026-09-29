#!/usr/bin/env python3
"""Run a case through a decision client and apply the routing policy.

Usage:
  python3 harness.py run cases/incident-triage.json            # offline stub
  python3 harness.py run cases/incident-triage.json --live     # real API (TYPESAFE_API_KEY)
  python3 harness.py run cases/raw/incident-injected-log.json  # raw event, state built by jevops/state.py
  python3 harness.py run cases/raw/incident-injected-log.json --unfiltered   # same event forwarded as-is
  python3 harness.py run cases/ci-failure.json --log decisions.jsonl   # also append the decision record
  python3 harness.py shadow shadow/sample-decisions.jsonl      # agreement report
  python3 harness.py replay shadow/replay-corpus.jsonl         # re-run labeled decisions, show what flips

`run` prints the raw answers and the policy decision. A case with a `raw_event`
instead of a `state` goes through the state builder first; `--unfiltered`
forwards the raw event instead, to show what the filter prevents. `shadow` reads a JSONL log
of past decisions (model choice + confidence + what the human did) and reports
agreement overall and per confidence bucket, which is how thresholds get tuned.
`replay` re-runs a labeled corpus against the current model and rules and lists
every decision that changed; `--fail-on-flip` makes it a CI gate.

Every logged decision carries the `model` the API reported and the
`policy_version` of the rules (thresholds, state builder, question criteria), so
decisions made under different versions are never mixed in one report.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from jevops.client import make_client
from jevops.policy import POLICIES, PRIMARY_CHOICE
from jevops.state import BUILDERS, naive_incident_state
from jevops.versioning import policy_version

CASES = Path(__file__).resolve().parent / "cases"


def decide(name: str, state, questions: dict, client, model: str | None) -> tuple[dict, object, dict]:
    """One decision call plus the policy, returned with the record that gets logged."""
    response = client.evaluate(state, questions, model)
    decision = POLICIES[name](response["answers"])
    primary = response["answers"].get(PRIMARY_CHOICE.get(name, ""), {})
    record = {
        "case": name,
        "model": response["model"],
        "policy_version": policy_version(questions),
        "model_choice": primary.get("choice"),
        "model_confidence": primary.get("confidence"),
        "outcome": decision.outcome,
        "state": state,
    }
    return response, decision, record


def run(path: str, live: bool, unfiltered: bool = False, log: str | None = None) -> int:
    case = json.loads(Path(path).read_text())
    name = case.get("policy", Path(path).stem)
    if "raw_event" in case:
        if unfiltered:
            state = naive_incident_state(case["raw_event"])
        else:
            state = BUILDERS[name](case["raw_event"])
        print(f"# state ({'unfiltered' if unfiltered else 'built by jevops/state.py'}):")
        print(json.dumps(state, indent=2))
    else:
        state = case["state"]
    if name not in POLICIES:
        print(f"no policy registered for {name}", file=sys.stderr)
        return 1
    client = make_client(live)
    response, decision, record = decide(name, state, case["questions"], client, case.get("model"))
    print(f"# case: {Path(path).stem}  policy: {name}  model: {response['model']}  policy_version: {record['policy_version']}")
    print(json.dumps(response["answers"], indent=2))
    print(f"# decision: {decision.outcome}  ({decision.reason})")
    print(f"# handoff: {decision.handoff}")
    if log:
        # human_choice is filled in later, from what the on-call engineer or owner did.
        with open(log, "a") as fh:
            fh.write(json.dumps({**record, "human_choice": None}) + "\n")
        print(f"# logged to {log}")
    return 0


def read_jsonl(path: str) -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def shadow(path: str) -> int:
    rows = read_jsonl(path)
    if not rows:
        print("empty log", file=sys.stderr)
        return 1
    groups: dict[tuple, list] = {}
    for row in rows:
        key = (row.get("model", "unrecorded"), row.get("policy_version", "unrecorded"))
        groups.setdefault(key, []).append(row)
    # Agreement measured under one set of rules says nothing about another,
    # so each (model, policy_version) pair gets its own report.
    for (model, version), group in groups.items():
        print(f"# model: {model}  policy_version: {version}")
        agreement_report(group)
    return 0


def agreement_report(rows: list[dict]) -> None:
    buckets = {"<0.5": [], "0.5-0.8": [], ">=0.8": []}
    for row in rows:
        c = row["model_confidence"]
        key = "<0.5" if c < 0.5 else "0.5-0.8" if c < 0.8 else ">=0.8"
        buckets[key].append(row["model_choice"] == row["human_choice"])
    total = sum(len(v) for v in buckets.values())
    agreed = sum(sum(v) for v in buckets.values())
    print(f"# shadow report: {total} decisions, {agreed} agreed ({100 * agreed / total:.0f}%)")
    for key, values in buckets.items():
        if values:
            print(f"  confidence {key:>7}: {len(values):3d} decisions, {100 * sum(values) / len(values):3.0f}% agreement")
        else:
            print(f"  confidence {key:>7}:   0 decisions")


def replay(path: str, live: bool, fail_on_flip: bool) -> int:
    rows = read_jsonl(path)
    if not rows:
        print("empty corpus", file=sys.stderr)
        return 1
    client = make_client(live)
    flips = 0
    before_agreed = after_agreed = labeled = 0
    versions = set()
    for index, row in enumerate(rows, 1):
        name = row["case"]
        case = json.loads((CASES / f"{name}.json").read_text())
        _, _, now = decide(name, row["state"], case["questions"], client, case.get("model"))
        versions.add((row.get("model"), row.get("policy_version"), now["model"], now["policy_version"]))
        changed = [f for f in ("model_choice", "outcome") if row.get(f) != now[f]]
        if changed:
            flips += 1
            detail = ", ".join(f"{f}: {row.get(f)} -> {now[f]}" for f in changed)
            print(f"  FLIP #{index} {name} ({row.get('id', '-')}): {detail}  [human: {row.get('human_choice')}]")
        if row.get("human_choice") is not None:
            labeled += 1
            before_agreed += row.get("model_choice") == row["human_choice"]
            after_agreed += now["model_choice"] == row["human_choice"]
    print(f"# replay: {len(rows)} decisions, {flips} changed")
    for rec_model, rec_version, cur_model, cur_version in sorted(versions, key=str):
        print(f"  recorded {rec_model}/{rec_version} -> now {cur_model}/{cur_version}")
    if labeled:
        print(f"  agreement with humans: {100 * before_agreed / labeled:.0f}% recorded, "
              f"{100 * after_agreed / labeled:.0f}% now ({labeled} labeled)")
    return 1 if fail_on_flip and flips else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_run = sub.add_parser("run")
    p_run.add_argument("case")
    p_run.add_argument("--live", action="store_true", help="call the real API instead of the stub")
    p_run.add_argument("--unfiltered", action="store_true", help="forward a raw_event as-is (comparison only)")
    p_shadow = sub.add_parser("shadow")
    p_run.add_argument("--log", help="append the decision record (model, policy_version, state) to a JSONL file")
    p_shadow.add_argument("log")
    p_replay = sub.add_parser("replay")
    p_replay.add_argument("corpus")
    p_replay.add_argument("--live", action="store_true", help="replay against the real API instead of the stub")
    p_replay.add_argument("--fail-on-flip", action="store_true", help="exit 1 if any decision changed (CI gate)")
    args = parser.parse_args()
    if args.cmd == "run":
        return run(args.case, args.live, args.unfiltered, args.log)
    if args.cmd == "replay":
        return replay(args.corpus, args.live, args.fail_on_flip)
    return shadow(args.log)


if __name__ == "__main__":
    sys.exit(main())
