# jev-ops-harness

Companion repository for the article *Your LLM Is Overqualified for Half Its Job: Jev for Cloud, DevOps and SRE Pipelines*. It is a small Python
harness that shows the division of labor the article argues for: a decision
model selects and classifies, an LLM investigates and writes, code validates
and executes.

Nothing here needs an account to run. A deterministic stub returns answers in
the same shape as the TypeSafe API, so you can read the routing code, run the
three cases and tune thresholds offline. When you have an API key, the same
harness calls the real endpoint with one flag.

## Layout

| Path | What it is |
|---|---|
| `cases/*.json` | Three requests in the exact API shape (`state` + `questions`): incident triage, CI failure, action gate |
| `jevops/client.py` | `HttpClient` (stdlib only, `POST /v1/systemone`) and `StubClient` (offline stand-in) |
| `jevops/policy.py` | Routing policy per case: act, review or escalate, with the confidence thresholds in one place |
| `harness.py` | CLI: run a case, or produce a shadow-mode agreement report |
| `shadow/sample-decisions.jsonl` | Synthetic log of model decisions next to what a human did, to show the report format |
| `tests/` | Offline tests: answer shapes match the documented API, policies route as intended |

## Run it

Python 3.10 or newer, no third-party packages.

```bash
python3 harness.py run cases/incident-triage.json      # stub, offline
python3 harness.py run cases/ci-failure.json
python3 harness.py run cases/action-gate.json
python3 harness.py shadow shadow/sample-decisions.jsonl
python3 -m unittest discover -s tests -t .
```

With an API key (`TYPESAFE_API_KEY`), add `--live` to call `jev-latest`:

```bash
export TYPESAFE_API_KEY=...
python3 harness.py run cases/incident-triage.json --live
```

`TYPESAFE_BASE_URL` overrides the endpoint base (default `https://api.typesafe.ai`)
if you reach the model through a gateway that keeps the same request shape.

## What the stub is and is not

`StubClient` counts keyword hits from each option's criteria, plus a short
synonym list, against the serialized state and normalizes them into a distribution. It exists so the
harness, the policy and the tests run without network access. It is not a
model, it is not calibrated, and its numbers should not be quoted as Jev's.
The real answers come from `--live`.

## Shadow mode

`harness.py shadow` reads a JSONL log where each line holds the model's choice,
its confidence and what the human actually did, and prints agreement overall
and per confidence bucket. Run your pipeline with the decision model in the
loop but not acting, log both columns for a few weeks, and read that report
before moving any threshold in `jevops/policy.py`.

## References

- API reference: https://docs.typesafe.ai/api
- Confidence: https://docs.typesafe.ai/confidence
- Known failure modes of `jev-1.13`: https://docs.typesafe.ai/model-jaggedness/jev-1.13

## License

MIT, see `LICENSE`.
