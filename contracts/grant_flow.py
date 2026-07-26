# v0.2.16
# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
from genlayer import *

from dataclasses import dataclass
import json
import time
import typing

# ==========================================================================
# GrantFlow - AI-evaluated, milestone-based grant treasury primitive.
#
# Lifecycle:
#   1. SUBMIT   - applicant posts a proposal with a milestone budget split
#                 (percents must sum to 100; validated deterministically).
#   2. EVALUATE - an LLM scores the proposal against the DAO's rubric on 5
#                 dimensions; validators reach consensus on the MEANING of
#                 the verdict via the comparative Equivalence Principle.
#   3. VERIFY   - the grantee claims each milestone with a public evidence
#                 URL; the contract fetches the page itself with native web
#                 access and the LLM judges whether the milestone described
#                 at submission time was actually delivered.
#   4. PAY      - passing verdicts release the milestone tranche of GEN
#                 from the contract treasury to the grantee wallet.
#
# Core design rule: "LLM proposes, code disposes". The LLM never moves
# money. Every state change is gated by deterministic Python checks
# (score threshold, confidence threshold, treasury accounting).
#
# Prompt-injection hardening: applicant text is fenced inside explicit
# untrusted-data markers, marker strings are stripped from user input, and
# the committee prompt instructs the model to ignore embedded instructions
# and to penalize the credibility score of manipulation attempts.
# ==========================================================================

# -------------------------- constants -------------------------------------

STATUS_SUBMITTED = 0
STATUS_APPROVED = 1
STATUS_REJECTED = 2
STATUS_COMPLETED = 3
STATUS_CANCELLED = 4

STATUS_NAMES = ["submitted", "approved", "rejected", "completed", "cancelled"]

SCORE_DIMENSIONS = ["impact", "feasibility", "innovation", "budget", "credibility"]
MAX_SCORE_PER_DIMENSION = 10
MAX_TOTAL_SCORE = len(SCORE_DIMENSIONS) * MAX_SCORE_PER_DIMENSION  # 50

MAX_MILESTONES = 6
MAX_EVIDENCE_CHARS = 6000     # web evidence budget fed to the LLM
MAX_REASONING_CHARS = 800

UNTRUSTED_OPEN = "<untrusted_applicant_data>"
UNTRUSTED_CLOSE = "</untrusted_applicant_data>"


def _clean_user_text(text: str, max_len: int) -> str:
    """Strip injection markers from applicant-provided text and cap length."""
    text = text.replace(UNTRUSTED_OPEN, "").replace(UNTRUSTED_CLOSE, "")
    return text.strip()[:max_len]


def _clamp(value: typing.Any, lo: int, hi: int, default: int) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


# ------------- ghost-contract interface (EOA value transfers) --------------

@gl.evm.contract_interface
class _Recipient:
    class View:
        pass

    class Write:
        pass


# -------------------------- storage types ----------------------------------

@allow_storage
@dataclass
class Proposal:
    proposer: str             # lowercase hex address of the applicant wallet
    title: str
    summary: str
    link: str                 # optional supporting URL (repo, doc, site)
    requested_wei: bigint
    approved_wei: bigint
    paid_wei: bigint
    status: bigint            # index into STATUS_NAMES
    total_score: bigint       # 0..50, set at evaluation time
    milestones_json: str      # canonical [{"description": str, "percent": int}]
    milestone_count: bigint
    released_count: bigint    # milestones are claimed strictly in order
    submitted_at: bigint      # unix seconds (deterministic tx time)


# ----------------------------- contract ------------------------------------

class Contract(gl.Contract):
    # governance / configuration
    owner: str                # lowercase hex address
    dao_name: str
    criteria: str             # the DAO's grant rubric, in natural language
    min_total_score: bigint   # 0..50, deterministic approval gate
    min_confidence: bigint    # 0..100, deterministic milestone-payout gate
    submit_cooldown_secs: bigint
    paused: bool

    # grant state
    next_id: bigint
    proposals: TreeMap[str, Proposal]        # str(proposal id) -> Proposal
    evaluations: TreeMap[str, str]           # str(proposal id) -> JSON record
    milestone_reports: TreeMap[str, str]     # "<id>:<idx>"      -> JSON report
    last_submission: TreeMap[str, bigint]    # sender hex        -> unix secs
    total_committed: bigint                  # approved but not yet paid out

    def __init__(
        self,
        dao_name: str,
        criteria: str,
        min_total_score: int,
        min_confidence: int,
        submit_cooldown_secs: int,
    ):
        """
        dao_name:             display name of the grant program.
        criteria:             natural-language rubric the LLM evaluates
                              proposals against.
        min_total_score:      minimum total score (0-50) required for
                              approval. 30 is a sensible default.
        min_confidence:       minimum verdict confidence (0-100) required
                              to release a milestone payment. 70 recommended.
        submit_cooldown_secs: anti-spam delay between submissions from the
                              same wallet. 3600 recommended, 0 disables.
        """
        self.owner = gl.message.sender_address.as_hex.lower()
        self.dao_name = dao_name.strip()[:120]
        self.criteria = criteria.strip()[:4000]
        self.min_total_score = _clamp(min_total_score, 0, MAX_TOTAL_SCORE, 30)
        self.min_confidence = _clamp(min_confidence, 0, 100, 70)
        self.submit_cooldown_secs = max(0, int(submit_cooldown_secs))
        self.paused = False
        self.next_id = 0
        self.total_committed = 0

    # ------------------------- internal helpers ---------------------------

    def _sender_hex(self) -> str:
        return gl.message.sender_address.as_hex.lower()

    def _only_owner(self) -> None:
        if self._sender_hex() != self.owner:
            raise gl.vm.UserError("only the owner can call this method")

    def _not_paused(self) -> None:
        if self.paused:
            raise gl.vm.UserError("the grant program is paused")

    def _get_proposal(self, proposal_id: int) -> Proposal:
        proposal = self.proposals.get(str(int(proposal_id)), None)
        if proposal is None:
            raise gl.vm.UserError(f"proposal {int(proposal_id)} does not exist")
        return proposal

    def _available_wei(self) -> int:
        balance = int(self.balance)
        committed = int(self.total_committed)
        return balance - committed if balance > committed else 0

    @staticmethod
    def _parse_milestones(milestones_json: str) -> str:
        """Validate applicant milestones and return the canonical JSON."""
        try:
            raw = json.loads(milestones_json)
        except Exception:
            raise gl.vm.UserError(
                'milestones must be JSON like '
                '[{"description": "...", "percent": 50}, ...]'
            )
        if not isinstance(raw, list) or not (1 <= len(raw) <= MAX_MILESTONES):
            raise gl.vm.UserError(
                f"between 1 and {MAX_MILESTONES} milestones are required"
            )
        clean: list = []
        percent_sum = 0
        for item in raw:
            if not isinstance(item, dict):
                raise gl.vm.UserError("each milestone must be an object")
            description = _clean_user_text(str(item.get("description", "")), 500)
            percent = item.get("percent")
            if len(description) < 10:
                raise gl.vm.UserError(
                    "each milestone needs a description of at least 10 characters"
                )
            if not isinstance(percent, int) or isinstance(percent, bool) or not (
                1 <= percent <= 100
            ):
                raise gl.vm.UserError(
                    "each milestone needs an integer percent between 1 and 100"
                )
            percent_sum += percent
            clean.append({"description": description, "percent": percent})
        if percent_sum != 100:
            raise gl.vm.UserError(
                f"milestone percents must sum to exactly 100 (got {percent_sum})"
            )
        return json.dumps(clean, sort_keys=True)

    # ----------------------------- treasury --------------------------------

    @gl.public.write.payable
    def fund(self) -> None:
        """Anyone can top up the grant treasury with GEN."""
        if int(gl.message.value) == 0:
            raise gl.vm.UserError("send a non-zero amount of GEN to fund")

    # ----------------------------- 1. submit --------------------------------

    @gl.public.write
    def submit_proposal(
        self,
        title: str,
        summary: str,
        link: str,
        requested_wei: int,
        milestones_json: str,
    ) -> int:
        """Submit a grant proposal. Returns the new proposal id.

        milestones_json example:
            [{"description": "Ship MVP contracts on testnet", "percent": 40},
             {"description": "Public beta with docs + report", "percent": 60}]
        """
        self._not_paused()
        sender = self._sender_hex()
        now = int(time.time())

        last = int(self.last_submission.get(sender, 0))
        if last > 0 and now < last + int(self.submit_cooldown_secs):
            raise gl.vm.UserError(
                "cooldown active: please wait before submitting again"
            )

        title = _clean_user_text(title, 120)
        summary = _clean_user_text(summary, 5000)
        link = _clean_user_text(link, 300)
        if len(title) < 8:
            raise gl.vm.UserError("title must be at least 8 characters")
        if len(summary) < 100:
            raise gl.vm.UserError(
                "summary must be at least 100 characters - describe the project, "
                "the team, the budget and the expected impact"
            )
        amount = int(requested_wei)
        if amount <= 0:
            raise gl.vm.UserError("requested_wei must be positive")

        canonical_milestones = self._parse_milestones(milestones_json)
        milestone_count = len(json.loads(canonical_milestones))

        pid = int(self.next_id)
        self.proposals[str(pid)] = Proposal(
            proposer=sender,
            title=title,
            summary=summary,
            link=link,
            requested_wei=amount,
            approved_wei=0,
            paid_wei=0,
            status=STATUS_SUBMITTED,
            total_score=0,
            milestones_json=canonical_milestones,
            milestone_count=milestone_count,
            released_count=0,
            submitted_at=now,
        )
        self.next_id = pid + 1
        self.last_submission[sender] = now
        return pid

    # ------------------------------ views -----------------------------------

    @gl.public.view
    def get_config(self) -> typing.Any:
        return {
            "dao_name": self.dao_name,
            "criteria": self.criteria,
            "owner": self.owner,
            "min_total_score": int(self.min_total_score),
            "max_total_score": MAX_TOTAL_SCORE,
            "min_confidence": int(self.min_confidence),
            "submit_cooldown_secs": int(self.submit_cooldown_secs),
            "paused": self.paused,
        }

    @gl.public.view
    def get_summary(self) -> typing.Any:
        counts = {name: 0 for name in STATUS_NAMES}
        for _, p in self.proposals.items():
            counts[STATUS_NAMES[int(p.status)]] += 1
        return {
            "dao_name": self.dao_name,
            "treasury_wei": int(self.balance),
            "committed_wei": int(self.total_committed),
            "available_wei": self._available_wei(),
            "proposal_count": int(self.next_id),
            "status_counts": counts,
        }

    @gl.public.view
    def get_proposal(self, proposal_id: int) -> typing.Any:
        p = self._get_proposal(proposal_id)
        return {
            "id": int(proposal_id),
            "proposer": p.proposer,
            "title": p.title,
            "summary": p.summary,
            "link": p.link,
            "requested_wei": int(p.requested_wei),
            "approved_wei": int(p.approved_wei),
            "paid_wei": int(p.paid_wei),
            "status": int(p.status),
            "status_name": STATUS_NAMES[int(p.status)],
            "total_score": int(p.total_score),
            "milestones": json.loads(p.milestones_json),
            "milestone_count": int(p.milestone_count),
            "released_count": int(p.released_count),
            "submitted_at": int(p.submitted_at),
        }

    @gl.public.view
    def list_proposals(self) -> typing.Any:
        """Compact listing for tooling (newest first)."""
        out: list = []
        for pid_str, p in self.proposals.items():
            out.append(
                {
                    "id": int(pid_str),
                    "title": p.title,
                    "proposer": p.proposer,
                    "requested_wei": int(p.requested_wei),
                    "paid_wei": int(p.paid_wei),
                    "status_name": STATUS_NAMES[int(p.status)],
                    "total_score": int(p.total_score),
                    "released_count": int(p.released_count),
                    "milestone_count": int(p.milestone_count),
                }
            )
        out.sort(key=lambda item: item["id"], reverse=True)
        return out
