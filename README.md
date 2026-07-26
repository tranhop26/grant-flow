# GrantFlow

**An AI-evaluated, milestone-based grant treasury primitive for DAOs — a standalone GenLayer Intelligent Contract.**

GrantFlow replaces the slowest part of every DAO — deciding who gets funded and whether they actually delivered — with an on-chain AI committee. It scores proposals against the DAO's own natural-language rubric, verifies milestone evidence by reading the live web itself, and releases treasury funds tranche by tranche. No multisig bottleneck, no oracle, no trusted committee.

> submit proposal → LLM committee scores it (validator consensus) → budget locked → grantee ships → the contract fetches the evidence URL and judges delivery → tranche paid.

This repository is a **contract primitive**: a single Python Intelligent Contract plus tests and deploy tooling. There is intentionally **no frontend** — any UI, bot, or other contract can drive it through its public API.

## Why this needs GenLayer

| Need | Classic smart contract | GrantFlow on GenLayer |
|---|---|---|
| Judge proposal quality | impossible | `gl.nondet.exec_prompt` scores 5 rubric dimensions |
| Verify a milestone was delivered | trusted humans / oracles | `gl.nondet.web.get` fetches the public evidence natively |
| Trust the AI's judgment | single-model output, unverifiable | validators re-run the judgment; consensus via the Equivalence Principle |
| Pay out | yes | native GEN transfers from the contract treasury |

## How consensus works here (meaning, not format)

Every AI judgment runs inside a non-deterministic closure and is accepted only through `gl.eq_principle.prompt_comparative`, whose *principle* tells validators what semantic agreement means:

* **Proposal evaluation** — principle: *"The `decision` field must be identical. Each individual score may differ by at most 2 points and `total` by at most 5. `reasoning` must identify the same main strengths and weaknesses."* Two validators that would decide differently about funding can never both pass — agreement is on the **decision and its substance**, never on JSON shape. Formatting noise is eliminated *before* comparison: outputs are parsed, clamped to the rubric ranges, and normalized inside the nondet closure, so what validators compare is the meaning-bearing core `{scores, total, decision, reasoning}`.
* **Milestone verification** — principle: *"The `completed` field must be identical. `confidence` may differ by at most 20. `summary` must describe the same evidence and reach the same conclusion."* The evidence page is truncated to a fixed budget and reduced to a derived verdict, following GenLayer guidance to compare **derived outcomes, not raw web data** (timestamps and dynamic content would break consensus).

**LLM proposes, code disposes.** Consensus produces a judgment; deterministic Python decides what happens with it:

```python
approved = evaluation["decision"] == "approve" and total >= min_score
passed   = verdict["completed"] and verdict["confidence"] >= min_confidence
```

The LLM never moves money, never sets status directly, and cannot exceed the treasury: approvals are blocked unless the *uncommitted* treasury covers the request (`total_committed` accounting).

## Security hardening

* **Prompt-injection defense** — applicant text is fenced in `<untrusted_applicant_data>` markers, the marker strings are stripped from user input, and the committee prompt instructs the model to ignore embedded instructions *and penalize the credibility score of manipulation attempts*. An injection attempt lowers the attacker's score.
* **Fail-closed AI outputs** — malformed LLM output, a missing score, or a missing `decision` **aborts the transaction** instead of being silently derived or defaulted. Nothing about adjudication is ever substituted by a fallback; the proposal stays in `submitted` state and can simply be re-evaluated. Milestone verdicts default to *deny* (`completed=false, confidence=0`) on missing fields — safe for the treasury, retryable for the grantee.
* **SSRF-hardened evidence URLs** — validators fetch the evidence URL themselves, so the contract enforces: `https://` only, public domain names only (no IP literals, no `localhost`, no `.internal`/`.local`-style suffixes, no cloud-metadata hosts), no embedded credentials, no explicit ports. *Residual risk:* DNS rebinding cannot be fully prevented at the contract layer — validator operators should enforce private-range egress blocking in their web-fetch infrastructure.
* **Nondet purity** — non-deterministic closures never touch `self` or storage; all needed values are captured as locals first.
* **Sequential milestones** — tranches release strictly in order; each claim stores an immutable JSON audit report on-chain.
* **No rounding dust** — the final milestone sweeps the exact remainder.
* **Anti-spam** — per-wallet submission cooldown and minimum proposal length.
* **Safety valves** — owner can pause, cancel abandoned grants (freeing committed funds), tune thresholds, and withdraw only *unallocated* funds; ownership is transferable to a DAO multisig.

## Public API

| Method | Access | Description |
|---|---|---|
| `fund()` | payable, anyone | Top up the grant treasury with GEN |
| `submit_proposal(title, summary, link, requested_wei, milestones_json)` | anyone | Submit a proposal; milestone percents must sum to 100 |
| `evaluate_proposal(id)` | anyone (caller pays gas) | Run the AI committee; deterministic approve/reject gate |
| `claim_milestone(id, evidence_url)` | proposer | Web-verified milestone payout |
| `get_config() / get_summary() / list_proposals() / get_proposal(id)` | view | State and treasury accounting |
| `get_evaluation(id) / get_milestone_report(id, idx)` | view | Full on-chain audit trail (JSON) |
| `set_criteria / set_thresholds / set_paused / transfer_ownership / cancel_proposal / withdraw_unallocated` | owner | Governance controls |

Storage layout: `TreeMap[str, Proposal]` with an `@allow_storage @dataclass` struct, all persisted numbers as `bigint`, explicit lifecycle `submitted → approved/rejected → completed` (plus `cancelled`).

## Deployment

| | |
|---|---|
| **CONTRACT_ADDRESS** | `0x2127A79c646B041bf66D8858297dD70449e2257e` |
| **NETWORK** | `studionet` (GenLayer Studio network) |

This instance was configured **on-chain by the owner through the governance setters** after deployment — `set_thresholds(30, 70)` and `set_criteria(<public-goods rubric>)` — because the Studio deploy form did not pass constructor arguments. Anyone can verify the active configuration via `get_config()`: `min_total_score=30`, `min_confidence=70`, the full rubric in `criteria`, `submit_cooldown_secs=0`. Note: `dao_name` is empty on this instance (it is constructor-only by design, with no setter); clients display a fallback label. The deterministic gates behave identically regardless of whether values arrive via constructor or setters.

### Worked example — *illustrative*

The exact scores below are an **expected-output example** (marked as such because LLM scores legitimately vary within the consensus tolerance; the decision itself is what consensus fixes). The same flow passes end-to-end in the test-suite with mocked LLM/web responses.

Call:

```json
submit_proposal(
  "GenLayer Validator Analytics",
  "We are building an open-source analytics dashboard for GenLayer validators. Team of two senior engineers, budget covers three months of development, and all code will be MIT licensed.",
  "https://github.com/example/validator-analytics",
  1000000000000000000,
  "[{\"description\": \"Ship the MVP contracts to testnet\", \"percent\": 40}, {\"description\": \"Public beta with docs and a demo video\", \"percent\": 60}]"
)   →  returns proposal id 0
```

Then `evaluate_proposal(0)` → expected record (also stored via `get_evaluation(0)`):

```json
{
  "proposal_id": 0,
  "evaluation": {
    "scores": {"impact": 8, "feasibility": 8, "innovation": 7, "budget": 7, "credibility": 8},
    "total": 38,
    "decision": "approve",
    "reasoning": "Clear scope with a working-prototype orientation, realistic three-month budget, verifiable public repo. Innovation is moderate but ecosystem value is direct."
  },
  "approved": true,
  "min_total_score": 30
}
```

Then `claim_milestone(0, "https://github.com/example/validator-analytics/releases")` → the contract fetches the page; on a verdict like `{"completed": true, "confidence": 88, ...}` it transfers 40% of 1 GEN (`400000000000000000` wei) to the proposer and stores the audit report at `get_milestone_report(0, 0)`.

## Project structure

```
.
├── contracts/grant_flow.py         # the Intelligent Contract (single file, pure ASCII)
├── tests/test_grant_flow.py        # 18 tests: rubric gate, payouts, fail-closed negative paths
├── tests/conftest.py               # uses genlayer-test fixtures when installed
├── tests/_emulator.py              # fallback fixtures for sandboxes without PyPI access
├── tests/run_tests.py              # zero-dependency runner (python3 tests/run_tests.py)
├── deploy/001_deploy_grant_flow.ts # `genlayer deploy` script
└── requirements-dev.txt            # genlayer-test + pytest
```

## Running the tests

```bash
pip install -r requirements-dev.txt   # Python 3.12+
pytest tests/ -v                      # official genlayer-test Direct Mode
# or, with zero dependencies (fallback emulator, same test file):
python3 tests/run_tests.py
```

The suite covers: deploy/config, submission validation (milestone percents, minimum lengths), AI approval and rejection, **the deterministic gate overriding an over-optimistic LLM**, treasury-coverage enforcement, milestone payout on verified evidence, denial on weak evidence, exact remainder sweeping, proposer-only claims, owner access control, pause, cancellation accounting, and the fail-closed negative paths: malformed LLM output, missing decision, unavailable (HTTP 404) evidence, and SSRF-blocked evidence URLs.

*Known limitation:* direct mode and the emulator execute leader-only; genuine multi-validator disagreement is exercised on-chain by the equivalence-principle strings (identical decision, bounded drift), not in unit tests.

## Reuse beyond grants

The primitive is a general **"AI-judged escrow with web-verified release"**. Point the rubric and milestone semantics elsewhere and it becomes: a bounty program with delivery verification, freelance escrow with dispute-free payouts, hackathon prize distribution judged against submission repos, or public-goods matching rounds with proof-of-impact gates.

## License

MIT — see [LICENSE](LICENSE).
