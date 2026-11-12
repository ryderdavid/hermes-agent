"""The coordinator-threads wire (docs/coordinator-threads.md) is declared, and what the gateway
produces for it validates: the capability keys in ``session.info``, the two thread events, the three
RPCs, and the thread attribution on an ``approval`` request."""

import pytest

from tools import async_delegation_messages as adm
from tui_gateway import server
from tui_gateway.contracts.common import SessionLiveInfo
from tui_gateway.contracts.registry import EVENTS, METHODS, SERVER_REQUESTS, ContractViolation, check_payload, check_result

CAPABILITIES = {"threads.roster", "threads.steer", "threads.messages", "threads.continue", "threads.approvals",
                "coordinator_mode"}


def test_session_info_advertises_every_thread_capability_it_serves():
    info = server._session_info(None, {"session_key": ""})

    assert set(info["capabilities"]) >= CAPABILITIES and info["coordinator_mode"] is False
    parsed = SessionLiveInfo.model_validate(info)
    assert parsed.capabilities == info["capabilities"] and parsed.coordinator_mode is False
    # A capability is only advertised with its RPCs registered on this gateway.
    served = {"threads.roster": ["subagent.list", "subagent.tail", "delegation.status"],
              "threads.steer": ["subagent.steer", "subagent.interrupt"], "threads.messages": ["subagent.reply"],
              "threads.continue": ["subagent.continue"], "coordinator_mode": ["session.mode.set"]}
    for capability, methods in served.items():
        assert capability in info["capabilities"]
        assert all(name in METHODS and name in server._methods for name in methods), capability


def test_thread_message_and_question_events_match_their_contracts():
    message = {"type": "async_delegation", "delegation_id": "dmsg_1", "thread_message": {
        "message_id": "dmsg_1", "kind": "question", "text": "Drop them?", "subagent_id": "sa-0-aaaa",
        "delegation_id": "deleg_1", "goal": "Check licences", "sent_at": 1.0, "question_id": "q_1",
        "options": ["Drop", "Flag"], "timeout_seconds": 900.0}}

    check_payload("subagent.message", adm.thread_message_annotation(message))
    check_payload("subagent.question", {"subagent_id": "sa-0-aaaa", "question_id": "q_1", "status": "answered",
                                        "goal": "Check licences", "answered_by": "user", "answer": "Flag"})
    assert {"subagent.message", "subagent.question"} <= set(EVENTS)
    with pytest.raises(ContractViolation):
        check_payload("subagent.question", {"subagent_id": "sa-0-aaaa", "question_id": "q_1", "status": "maybe"})


def test_thread_rpc_results_and_the_approval_attribution_are_declared():
    check_result(METHODS["session.mode.set"], {"coordinator": True, "pending": False})
    check_result(METHODS["subagent.reply"], {"accepted": True, "subagent_id": "sa-0-aaaa", "question_id": "q_1",
                                             "answered_by": "coordinator"})
    check_result(METHODS["subagent.continue"], {"status": "queued", "subagent_id": "sa-0-aaaa", "position": 1})
    check_result(METHODS["subagent.list"], {"subagents": [{
        "subagent_id": "sa-0-aaaa", "pending_question": {"question_id": "q_1", "text": "Drop them?", "asked_at": 1.0,
                                                         "expires_at": 901.0}}],
        "delegations": [], "queued": [{"subagent_id": "sa-1-bbbb", "goal": "Draft notes", "queued_at": 2.0}]})
    approval = SERVER_REQUESTS["approval"].params.model_validate({
        "session_id": "ui-1", "request_id": "r-1", "command": "rm -rf build", "choices": ["once", "deny"],
        "subagent_id": "sa-0-aaaa", "goal": "Check licences"})
    assert (approval.subagent_id, approval.goal) == ("sa-0-aaaa", "Check licences")
