"""
GrantFlow test suite — genlayer-test (Direct Mode).

Direct Mode runs the contract in-memory (no Docker / Studio needed) and
lets us mock LLM and web responses, which makes the AI evaluation and
milestone-verification paths fully testable in CI.

Install & run:
    pip install genlayer-test
    pytest tests/ -v

Docs: https://docs.genlayer.com/developers/intelligent-contracts/testing
"""

import json

CONTRACT = "contracts/grant_flow.py"

DEPLOY_ARGS = [
    "Test DAO",                                  # dao_name
    "Fund public-good tooling for GenLayer.",    # criteria
    30,                                          # min_total_score (of 50)
    70,                                          # min_confidence
    0,                                           # submit_cooldown_secs (off for tests)
]

VALID_MILESTONES = json.dumps(
    [
        {"description": "Ship the MVP contracts to testnet", "percent": 40},
        {"description": "Public beta with docs and a demo video", "percent": 60},
    ]
)

APPROVE_EVALUATION = json.dumps(
    {
        "scores": {
            "impact": 9,
            "feasibility": 8,
            "innovation": 8,
            "budget": 7,
            "credibility": 8,
        },
        "decision": "approve",
        "reasoning": "Strong team, clear plan, reasonable budget.",
    }
)

REJECT_EVALUATION = json.dumps(
    {
        "scores": {
            "impact": 3,
            "feasibility": 2,
            "innovation": 4,
            "budget": 2,
            "credibility": 3,
        },
        "decision": "reject",
        "reasoning": "Vague scope and unjustified budget.",
    }
)

MILESTONE_DONE = json.dumps(
    {
        "completed": True,
        "confidence": 90,
        "summary": "The repository contains the released MVP as described.",
    }
)

MILESTONE_NOT_DONE = json.dumps(
    {
        "completed": False,
        "confidence": 20,
        "summary": "The page shows no deliverable matching the milestone.",
    }
)

SUMMARY_100 = (
    "We are building an open-source analytics dashboard for GenLayer "
    "validators. Team of two senior engineers, budget covers three months "
    "of development, and all code will be MIT licensed."
)


def _fund_treasury(direct_vm, contract, amount):
    """Best-effort treasury funding — the balance-setting helper name has
    varied across genlayer-test releases, so try the known ones."""
    for attr in ("fund", "set_balance", "deal"):
        if hasattr(direct_vm, attr):
            getattr(direct_vm, attr)(contract.address, amount)
            return
    contract.fund(value=amount)  # fallback: call the payable method


def _submit(contract, requested_wei=10**18):
    return contract.submit_proposal(
        "GenLayer Validator Analytics",
        SUMMARY_100,
        "https://github.com/example/validator-analytics",
        requested_wei,
        VALID_MILESTONES,
    )


# ---------------------------------------------------------------------------
# Deployment & configuration
# ---------------------------------------------------------------------------

def test_deploy_and_config(direct_deploy):
    contract = direct_deploy(CONTRACT, *DEPLOY_ARGS)
    config = contract.get_config()
    assert config["dao_name"] == "Test DAO"
    assert config["min_total_score"] == 30
    assert config["min_confidence"] == 70
    assert config["paused"] is False

    summary = contract.get_summary()
    assert summary["proposal_count"] == 0
    assert summary["committed_wei"] == 0


# ---------------------------------------------------------------------------
# Submission validation
# ---------------------------------------------------------------------------

def test_submit_proposal_happy_path(direct_deploy):
    contract = direct_deploy(CONTRACT, *DEPLOY_ARGS)
    pid = _submit(contract)
    assert pid == 0

    p = contract.get_proposal(0)
    assert p["status_name"] == "submitted"
    assert p["milestone_count"] == 2
    assert p["requested_wei"] == 10**18


def test_submit_rejects_bad_milestones(direct_vm, direct_deploy):
    contract = direct_deploy(CONTRACT, *DEPLOY_ARGS)
    bad = json.dumps(
        [
            {"description": "Do half of the work properly", "percent": 40},
            {"description": "Do the other half of the work", "percent": 40},
        ]
    )  # sums to 80, not 100
    with direct_vm.expect_revert("sum to exactly 100"):
        contract.submit_proposal(
            "A valid title here", SUMMARY_100, "", 10**18, bad
        )


def test_submit_rejects_short_summary(direct_vm, direct_deploy):
    contract = direct_deploy(CONTRACT, *DEPLOY_ARGS)
    with direct_vm.expect_revert("at least 100 characters"):
        contract.submit_proposal(
            "A valid title here", "too short", "", 10**18, VALID_MILESTONES
        )


# ---------------------------------------------------------------------------
# AI evaluation (LLM mocked)
# ---------------------------------------------------------------------------

def test_evaluation_approves_good_proposal(direct_vm, direct_deploy):
    contract = direct_deploy(CONTRACT, *DEPLOY_ARGS)
    _fund_treasury(direct_vm, contract, 2 * 10**18)  # treasury must cover request
    _submit(contract)

    direct_vm.mock_llm(r"grant evaluation committee", APPROVE_EVALUATION)
    record = contract.evaluate_proposal(0)

    assert record["approved"] is True
    p = contract.get_proposal(0)
    assert p["status_name"] == "approved"
    assert p["total_score"] == 40
    assert contract.get_summary()["committed_wei"] == 10**18


def test_evaluation_rejects_weak_proposal(direct_vm, direct_deploy):
    contract = direct_deploy(CONTRACT, *DEPLOY_ARGS)
    _fund_treasury(direct_vm, contract, 2 * 10**18)
    _submit(contract)

    direct_vm.mock_llm(r"grant evaluation committee", REJECT_EVALUATION)
    record = contract.evaluate_proposal(0)

    assert record["approved"] is False
    assert contract.get_proposal(0)["status_name"] == "rejected"
    assert contract.get_summary()["committed_wei"] == 0


def test_deterministic_gate_overrides_llm_decision(direct_vm, direct_deploy):
    """LLM says approve but total score is below the DAO threshold —
    the deterministic gate must reject anyway."""
    contract = direct_deploy(CONTRACT, *DEPLOY_ARGS)
    _fund_treasury(direct_vm, contract, 2 * 10**18)
    _submit(contract)

    low_score_approve = json.dumps(
        {
            "scores": {
                "impact": 4, "feasibility": 4, "innovation": 4,
                "budget": 4, "credibility": 4,
            },  # total 20 < 30
            "decision": "approve",
            "reasoning": "Optimistic despite weak scores.",
        }
    )
    direct_vm.mock_llm(r"grant evaluation committee", low_score_approve)
    record = contract.evaluate_proposal(0)

    assert record["approved"] is False
    assert contract.get_proposal(0)["status_name"] == "rejected"


def test_evaluation_requires_treasury_coverage(direct_vm, direct_deploy):
    contract = direct_deploy(CONTRACT, *DEPLOY_ARGS)
    _submit(contract)  # treasury is empty
    with direct_vm.expect_revert("treasury cannot cover"):
        contract.evaluate_proposal(0)


# ---------------------------------------------------------------------------
# Milestone verification & payout (web + LLM mocked)
# ---------------------------------------------------------------------------

def _approved_proposal(direct_vm, direct_deploy):
    contract = direct_deploy(CONTRACT, *DEPLOY_ARGS)
    _fund_treasury(direct_vm, contract, 2 * 10**18)
    _submit(contract)
    direct_vm.mock_llm(r"grant evaluation committee", APPROVE_EVALUATION)
    contract.evaluate_proposal(0)
    return contract

def test_milestone_payout_on_verified_evidence(direct_vm, direct_deploy):
    contract = _approved_proposal(direct_vm, direct_deploy)

    direct_vm.mock_web(
        r"github\.com/example",
        {"status": 200, "body": "<html>Release v1.0 — MVP shipped</html>"},
    )
    direct_vm.mock_llm(r"milestone auditor", MILESTONE_DONE)

    report = contract.claim_milestone(0, "https://github.com/example/repo")
    assert report["paid"] is True
    assert report["amount_wei"] == 4 * 10**17  # 40% of 1 GEN

    p = contract.get_proposal(0)
    assert p["released_count"] == 1
    assert p["paid_wei"] == 4 * 10**17
    assert p["status_name"] == "approved"  # one milestone left


def test_milestone_denied_on_weak_evidence(direct_vm, direct_deploy):
    contract = _approved_proposal(direct_vm, direct_deploy)

    direct_vm.mock_web(
        r"github\.com/example",
        {"status": 200, "body": "<html>Empty repository</html>"},
    )
    direct_vm.mock_llm(r"milestone auditor", MILESTONE_NOT_DONE)

    report = contract.claim_milestone(0, "https://github.com/example/repo")
    assert report["paid"] is False
    assert contract.get_proposal(0)["released_count"] == 0
    assert contract.get_proposal(0)["paid_wei"] == 0


def test_final_milestone_sweeps_remainder_and_completes(direct_vm, direct_deploy):
    contract = _approved_proposal(direct_vm, direct_deploy)
    direct_vm.mock_web(
        r"github\.com/example",
        {"status": 200, "body": "<html>Release v1.0 and beta live</html>"},
    )
    direct_vm.mock_llm(r"milestone auditor", MILESTONE_DONE)

    contract.claim_milestone(0, "https://github.com/example/repo")
    contract.claim_milestone(0, "https://github.com/example/repo")

    p = contract.get_proposal(0)
    assert p["status_name"] == "completed"
    assert p["paid_wei"] == 10**18          # exact total, no rounding dust
    assert contract.get_summary()["committed_wei"] == 0


def test_only_proposer_can_claim(direct_vm, direct_deploy, direct_bob):
    contract = _approved_proposal(direct_vm, direct_deploy)
    with direct_vm.prank(direct_bob):
        with direct_vm.expect_revert("only the proposer"):
            contract.claim_milestone(0, "https://github.com/example/repo")


# ---------------------------------------------------------------------------
# Governance
# ---------------------------------------------------------------------------

def test_owner_controls(direct_vm, direct_deploy, direct_bob):
    contract = direct_deploy(CONTRACT, *DEPLOY_ARGS)

    contract.set_thresholds(35, 80)
    config = contract.get_config()
    assert config["min_total_score"] == 35
    assert config["min_confidence"] == 80

    with direct_vm.prank(direct_bob):
        with direct_vm.expect_revert("only the owner"):
            contract.set_paused(True)

    contract.set_paused(True)
    with direct_vm.expect_revert("paused"):
        _submit(contract)


def test_cancel_frees_commitment(direct_vm, direct_deploy):
    contract = _approved_proposal(direct_vm, direct_deploy)
    assert contract.get_summary()["committed_wei"] == 10**18
    contract.cancel_proposal(0)
    assert contract.get_proposal(0)["status_name"] == "cancelled"
    assert contract.get_summary()["committed_wei"] == 0
