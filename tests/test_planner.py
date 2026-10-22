import anyio

from arp.agent.planners import LLMPlanner, missing_args
from arp.security.guards import evaluate_action


class ScriptedLLM:
    """Returns one scripted propose_action call per chat turn and records the messages."""

    def __init__(self, proposals):
        self.proposals = list(proposals)
        self.seen = []

    async def chat(self, ctx, messages, tools=None, tool_choice="auto"):
        self.seen.append([dict(m) for m in messages])
        args = self.proposals.pop(0)
        return {"content": "", "tool_calls": [{"id": f"c{len(self.seen)}", "name": "propose_action", "arguments": args}]}


class Ctx:
    requester = "u100"

    class tools:  # noqa: N801 - minimal stand-in for the gateway
        @staticmethod
        async def schemas():
            return {}


INCOMPLETE = {"action": "grant_folder_access", "rationale": "needs access"}
COMPLETE = {**INCOMPLETE, "user_id": "u100", "folder": "finance-reports"}


def test_missing_args():
    assert missing_args(INCOMPLETE) == ["user_id", "folder"]
    assert missing_args(COMPLETE) == []
    assert missing_args({"action": "create_ticket", "queue": "security"}) == ["summary"]


def test_v3_returns_incomplete_proposal_to_the_model_and_accepts_the_repair():
    llm = ScriptedLLM([INCOMPLETE, COMPLETE])
    planner = LLMPlanner(llm, prompt_version="v2", validate=True, version="v3-llm")
    plan = anyio.run(planner.plan, Ctx(), "I need access to finance-reports", "access")
    assert plan.action["folder"] == "finance-reports" and planner.repairs == 1
    assert "requires user_id, folder" in llm.seen[1][-1]["content"]


def test_v2_passes_incomplete_proposal_through_and_policy_rejects_it_with_the_real_reason():
    plan = anyio.run(LLMPlanner(ScriptedLLM([INCOMPLETE]), prompt_version="v2").plan, Ctx(), "x", "access")
    assert plan.action == INCOMPLETE
    d = evaluate_action(plan.action, requester="u100", requester_groups=[], folder_info=None, flags=[])
    assert not d.allowed and d.reason == "invalid action: missing user_id"
