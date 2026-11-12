"""Coordinator threads over the shared JSON-RPC surface (docs/coordinator-threads.md).

Real dispatch, real SessionDB, the real async-delegation registry, ledger and approval queue; only the
model conversation and the socket are replaced. Covers the per-session coordinator flag, delivery of
a child's message as a new parent turn (held while the parent is mid-turn, never duplicated),
``subagent.reply``, ``subagent.continue`` and child approvals on the parent's channel.
"""

import json
import queue
import threading
from types import SimpleNamespace

import pytest

from agent.prompt_builder import COORDINATOR_MODE_GUIDANCE
from hermes_state import SessionDB
from tests.tools import test_delegate_transcript_readiness as transcript_readiness
from tests.waiting import wait_until
from tools import async_delegation as ad
from tools import async_delegation_messages as adm
from tools import delegate_tool as dt
from tools.delegate_tool_child_run import _register_child
from tools.delegate_tool_registry import _active_subagents, _unregister_subagent
from tools.process_registry import process_registry
from tools.process_registry_notifications import format_process_notification
from tui_gateway import server

offline_children = transcript_readiness.offline_children
SUBAGENT = "sa-0-thread01"


def _drain() -> list:
    events = []
    while not process_registry.completion_queue.empty():
        events.append(process_registry.completion_queue.get_nowait())
    return events


def _take(predicate, what: str) -> dict:
    def take():
        try:
            evt = process_registry.completion_queue.get_nowait()
        except queue.Empty:
            return None
        return evt if predicate(evt) else None
    return wait_until(take, what=what)


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    """One live session ("ui-1") on a stored conversation ("parent") with a client transport attached."""
    ad._reset_for_tests()
    adm._reset_for_tests()
    _drain()
    config: dict = {}
    monkeypatch.setattr(dt, "_load_config", lambda: config)
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("parent", source="desktop")
    monkeypatch.setattr(server, "_get_db", lambda: db)
    frames: list = []
    transport = SimpleNamespace(write=lambda frame: frames.append(frame) or True)
    agent = transcript_readiness._parent(db, "parent", lambda *a, **kw: None)
    agent.ephemeral_system_prompt = "Be brief."
    owner = {"session_key": "parent", "history": [], "history_lock": threading.RLock(), "transport": transport,
             "agent": agent, "running": False}
    monkeypatch.setattr(server, "_sessions", {"ui-1": owner})
    emitted: list = []
    monkeypatch.setattr(server, "_emit", lambda event, sid, payload=None: emitted.append((event, sid, payload)))

    def call(method, *, via=transport, **params):
        return server.dispatch({"id": 1, "method": method, "params": {"session_id": "ui-1", **params}}, transport=via)

    releases: list = []
    yield SimpleNamespace(owner=owner, agent=agent, db=db, call=call, frames=frames, emitted=emitted, config=config,
                          transport=transport, releases=releases)
    for release in releases:
        release.set()
    for subagent_id in list(_active_subagents):
        _unregister_subagent(subagent_id)
    adm._reset_for_tests()
    wait_until(lambda: not ad.active_count(), what="background units to finish")
    ad._reset_for_tests()
    _drain()
    db.close()


def _running_thread(rt, *, interrupted: threading.Event | None = None) -> threading.Event:
    """A registered child of a running background unit of the session; returns the event that ends the unit."""
    release = threading.Event()
    rt.releases.append(release)
    handle = ad.dispatch_async_delegation_batch(
        goals=["Check the licences"], context=None, toolsets=None, role="leaf", model="test", session_key="parent",
        origin_ui_session_id="ui-1", parent_session_id="parent",
        interrupt_fn=interrupted.set if interrupted is not None else None,
        runner=lambda: {"results": []} if release.wait(30) else {})
    child = SimpleNamespace(_subagent_id=SUBAGENT, _delegate_depth=1, _delegation_id=handle["delegation_id"],
                            _async_unit_id=handle["delegation_id"], model="test")
    _register_child(child, rt.agent, "Check the licences", owner_session_id="ui-1", owner_transport=rt.transport,
                    owner_session_record=rt.owner)
    return release


# ── coordinator mode ────────────────────────────────────────────────────────
def test_coordinator_mode_is_set_reported_and_survives_a_resume(runtime):
    rt = runtime
    assert server._session_info(None, rt.owner)["coordinator_mode"] is False

    assert rt.call("session.mode.set", coordinator=True)["result"] == {"coordinator": True, "pending": False}

    info = server._session_info(None, rt.owner)
    assert info["coordinator_mode"] is True
    assert {"threads.roster", "threads.steer", "threads.messages", "threads.continue", "threads.approvals",
            "coordinator_mode"} <= set(info["capabilities"])
    assert rt.agent.ephemeral_system_prompt == f"Be brief.\n\n{COORDINATOR_MODE_GUIDANCE}"

    # A resume builds a new session record and a new agent for the same stored conversation.
    resumed = {"session_key": "parent", "agent": SimpleNamespace(ephemeral_system_prompt="Be brief.")}
    assert server._session_info(None, resumed)["coordinator_mode"] is True
    server._sync_coordinator_mode(resumed)  # every turn start
    assert COORDINATOR_MODE_GUIDANCE in resumed["agent"].ephemeral_system_prompt
    resumed["agent"].ephemeral_system_prompt = "A new persona."  # a personality change replaces the overlay
    server._sync_coordinator_mode(resumed)
    assert resumed["agent"].ephemeral_system_prompt == f"A new persona.\n\n{COORDINATOR_MODE_GUIDANCE}"

    assert rt.call("session.mode.set", coordinator=False)["result"]["coordinator"] is False
    assert rt.agent.ephemeral_system_prompt == "Be brief."
    assert server._session_info(None, {"session_key": "parent"})["coordinator_mode"] is False
    assert "error" in rt.call("session.mode.set", coordinator="yes")
    assert "error" in rt.call("session.mode.set", session_id="missing", coordinator=True)


def test_a_draft_chat_keeps_the_mode_until_its_first_turn_stores_it(runtime):
    rt = runtime
    draft = {"session_key": "draft", "history": [], "history_lock": threading.RLock(), "transport": rt.transport,
             "agent": None, "running": False}
    server._sessions["ui-draft"] = draft

    assert rt.call("session.mode.set", session_id="ui-draft", coordinator=True)["result"] == {
        "coordinator": True, "pending": True}
    assert server._session_info(None, draft)["coordinator_mode"] is True

    rt.db.create_session("draft", source="desktop")  # the first prompt creates the row
    draft["agent"] = SimpleNamespace(ephemeral_system_prompt=None)
    server._sync_coordinator_mode(draft)
    assert draft["agent"].ephemeral_system_prompt == COORDINATOR_MODE_GUIDANCE
    assert server._session_info(None, {"session_key": "draft"})["coordinator_mode"] is True


def test_turning_the_mode_off_leaves_running_threads_alone(runtime):
    rt = runtime
    interrupted = threading.Event()
    release = _running_thread(rt, interrupted=interrupted)
    rt.call("session.mode.set", coordinator=True)

    rt.call("session.mode.set", coordinator=False)

    assert not interrupted.is_set() and ad.active_count() == 1
    assert rt.call("subagent.list")["result"]["subagents"][0]["subagent_id"] == SUBAGENT
    release.set()
    done = _take(lambda evt: not adm.is_thread_message(evt), "the thread's completion")
    assert done["type"] == "async_delegation" and done["session_key"] == "parent", "it still reports back"


def test_the_main_agents_delegations_run_in_the_background():
    from unittest.mock import patch

    from run_agent import AIAgent

    main = object.__new__(AIAgent)
    main._delegate_depth = 0
    with patch("tools.delegate_tool.delegate_task", return_value="{}") as delegate:
        main._dispatch_delegate_task({"goal": "three independent jobs"})
    assert delegate.call_args.kwargs["background"] is True


# ── thread messages as parent turns ─────────────────────────────────────────
def test_a_thread_update_becomes_one_new_parent_turn_only_once_the_parent_is_idle(runtime, monkeypatch):
    rt = runtime
    _running_thread(rt)
    submitted: list = []
    monkeypatch.setattr(server, "_run_prompt_submit", lambda rid, sid, session, text, **kw: submitted.append((sid, text, kw)) or True)
    assert adm.post_update(SUBAGENT, "2 of 5 jobs fail on Android API 35")["status"] == "sent"
    evt = _take(adm.is_thread_message, "the update on the completion queue")
    handle = lambda copy: server._notif_handle_ready(
        "ui-1", rt.owner, [copy], set(), process_registry, format_process_notification, None)

    rt.owner["running"] = True  # the coordinator is answering the user
    handle(evt)
    assert submitted == [] and not any(e[0] == "subagent.message" for e in rt.emitted)
    held = _take(adm.is_thread_message, "the held update back on the queue")

    rt.owner["running"] = False  # that reply ended
    handle(held)
    ((sid, text, kwargs),) = submitted
    assert sid == "ui-1" and "2 of 5 jobs fail on Android API 35" in text and text.startswith("[THREAD UPDATE")
    annotation = kwargs["display_metadata"]
    assert kwargs["display_kind"] == "async_delegation_message"
    assert annotation["type"] == "async_delegation_message" and annotation["kind"] == "update"
    assert annotation["subagent_id"] == SUBAGENT and annotation["goal"] == "Check the licences"
    assert annotation["text"] == "2 of 5 jobs fail on Android API 35"
    assert [e for e in rt.emitted if e[0] == "subagent.message"] == [("subagent.message", "ui-1", annotation)]

    rt.owner["running"] = False
    handle(dict(evt))  # a second copy (restart replay, another poller): the ledger row is already delivered
    assert len(submitted) == 1


def test_a_completion_turn_names_each_thread_that_ran():
    evt = {"type": "async_delegation", "delegation_id": "deleg_1", "goals": ["Draft release notes", "Check licences"],
           "total_duration_seconds": 61.5, "results": [
               {"task_index": 0, "status": "completed", "subagent_id": "sa-0-aaaa", "duration_seconds": 40.2,
                "tool_trace": [{"tool": "read_file"}, {"tool": "terminal"}]},
               {"task_index": 1, "status": "failed", "subagent_id": "sa-1-bbbb", "duration_seconds": 61.0, "tool_trace": []}]}

    meta = server._async_delegation_display_metadata(evt)

    assert meta["delegation_id"] == "deleg_1" and meta["task_count"] == 2 and meta["duration_seconds"] == 61.5
    assert meta["threads"] == [
        {"subagent_id": "sa-0-aaaa", "task_index": 0, "goal": "Draft release notes", "status": "completed",
         "tool_count": 2, "duration_seconds": 40.2},
        {"subagent_id": "sa-1-bbbb", "task_index": 1, "goal": "Check licences", "status": "failed",
         "tool_count": 0, "duration_seconds": 61.0}]


# ── subagent.reply ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("answered_by", ["user", "coordinator"])
def test_the_first_reply_resumes_the_waiting_thread_and_later_ones_are_refused(runtime, answered_by):
    rt = runtime
    _running_thread(rt)
    adm.set_question_notifier(server._emit_thread_question)
    answer: dict = {}
    asking = threading.Thread(target=lambda: answer.update(adm.ask_question(SUBAGENT, "Drop the two AGPL packages?")))
    asking.start()
    try:
        question_id = _take(adm.is_thread_message, "the question")["thread_message"]["question_id"]
        pending = rt.call("subagent.list")["result"]["subagents"][0]["pending_question"]
        assert pending["question_id"] == question_id and pending["text"] == "Drop the two AGPL packages?"

        # The client that answers has often reconnected: a new session record for the same conversation.
        server._sessions["ui-1"] = reattached = {**rt.owner}
        reply = rt.call("subagent.reply", subagent_id=SUBAGENT, question_id=question_id, text="Flag them for legal",
                        answered_by=answered_by)
        assert reply["result"] == {"accepted": True, "subagent_id": SUBAGENT, "question_id": question_id,
                                   "answered_by": answered_by}
        asking.join(10)
        assert answer == {"answered": True, "answer": "Flag them for legal", "answered_by": answered_by}

        late = rt.call("subagent.reply", subagent_id=SUBAGENT, question_id=question_id, text="Drop them")
        assert late["error"]["code"] == 4090 and f"already answered by the {answered_by}" in late["error"]["message"]
        assert rt.call("subagent.list")["result"]["subagents"][0]["pending_question"] is None
        statuses = [(e[1], e[2]["status"]) for e in rt.emitted if e[0] == "subagent.question"]
        assert statuses == [("ui-1", "pending"), ("ui-1", "answered")]
        assert reattached["session_key"] == "parent"
    finally:
        adm.set_question_notifier(None)
        adm._reset_for_tests()
        asking.join(10)


def test_a_reply_needs_this_conversations_thread_and_an_attached_client(runtime):
    rt = runtime
    _running_thread(rt)
    stranger = SimpleNamespace(write=lambda frame: True)
    args = dict(subagent_id=SUBAGENT, question_id="q_none", text="yes")

    assert rt.call("subagent.reply", via=stranger, **args)["error"]["code"] == 4001
    assert rt.call("subagent.reply", **{**args, "subagent_id": "sa-0-other"})["error"]["code"] == 4004
    assert rt.call("subagent.reply", **args)["error"]["code"] == 4090, "no such open question"
    assert "error" in rt.call("subagent.reply", **{**args, "answered_by": "someone"})


# ── subagent.continue ───────────────────────────────────────────────────────
def test_continue_reruns_a_finished_thread_and_its_report_is_a_new_parent_turn(runtime, monkeypatch, offline_children):
    rt = runtime
    first = dt._build_child_agent(task_index=0, goal="Draft the release notes", context=None, toolsets=["file"],
                                  model=None, max_iterations=2, task_count=1, parent_agent=rt.agent)
    first._ensure_db_session()
    rt.db.append_message(first.session_id, "user", "Draft the release notes")
    rt.db.append_message(first.session_id, "assistant", "Here are the release notes: ...")
    subagent_id = first._subagent_id
    first.close()
    runs: list = []

    def run_conversation(self, user_message=None, conversation_history=None, **_kwargs):
        runs.append((self._subagent_id, user_message, len(conversation_history or [])))
        return {"final_response": "Shorter notes: ...", "completed": True, "api_calls": 1, "messages": []}

    monkeypatch.setattr("run_agent.AIAgent.run_conversation", run_conversation)

    assert rt.call("subagent.continue", subagent_id=subagent_id, text="Make the summary shorter") is None  # pooled
    response = wait_until(lambda: next((f for f in rt.frames if f.get("id") == 1), None), what="the RPC response")
    assert response["result"]["status"] == "dispatched" and response["result"]["subagent_id"] == subagent_id

    done = _take(lambda evt: any(r.get("subagent_id") == subagent_id for r in evt.get("results") or []),
                 "the follow-up's completion")
    assert runs == [(subagent_id, "Make the summary shorter", 2)]
    submitted: list = []
    monkeypatch.setattr(server, "_run_prompt_submit", lambda rid, sid, session, text, **kw: submitted.append((sid, kw)) or True)
    server._notif_handle_ready("ui-1", rt.owner, [done], set(), process_registry, format_process_notification, None)
    ((sid, kwargs),) = submitted
    assert sid == "ui-1" and kwargs["display_kind"] == "async_delegation_complete"
    (thread,) = kwargs["display_metadata"]["threads"]
    assert thread["subagent_id"] == subagent_id and thread["status"] == "completed"

    rt.call("subagent.continue", subagent_id=subagent_id, text="  ")
    refused = wait_until(lambda: next((f for f in rt.frames if "error" in f), None), what="the refusal")
    assert refused["error"]["code"] == 4002


# ── child approvals on the parent's channel ─────────────────────────────────
def _child_approval(rt, monkeypatch, client_answer):
    """Run one child approval through the real gateway wait; returns ``(decision, requests sent to the client)``."""
    from tools import approval as approval_mod
    from tools import approval_gateway_wait as wait_mod
    from tools.delegate_tool_config import subagent_approval_scope
    from tui_gateway import server_requests

    sent: list = []

    def send_async(method, sid, params, on_result):
        sent.append((method, sid, params))
        on_result(client_answer)
        return lambda reason: None

    monkeypatch.setattr(server_requests, "send_async", send_async)
    monkeypatch.setattr(wait_mod._ctx, "_get_approval_timeout", lambda: 5)
    monkeypatch.setattr(wait_mod._ctx, "_fire_approval_hook", lambda name, **kw: None)
    approval_mod.register_gateway_notify("parent", lambda data: server._emit_approval_request("ui-1", data))
    try:
        with subagent_approval_scope(SUBAGENT, "Check the licences"):
            decision = wait_mod._await_gateway_decision(
                "parent", approval_mod._gateway_notify_cbs["parent"],
                {"command": "rm -rf build", "description": "recursive delete", "pattern_key": "dangerous",
                 "pattern_keys": ["dangerous"]})
    finally:
        approval_mod.unregister_gateway_notify("parent")
    return decision, sent


def test_a_child_approval_is_asked_on_the_parent_session_naming_the_thread(runtime, monkeypatch):
    decision, sent = _child_approval(runtime, monkeypatch, {"choice": "once"})

    ((method, sid, params),) = sent
    assert method == "approval" and sid == "ui-1"
    assert params["subagent_id"] == SUBAGENT and params["goal"] == "Check the licences"
    assert params["command"] == "rm -rf build" and params["request_id"]
    assert decision["resolved"] is True and decision["choice"] == "once", "the answer reaches the child that asked"


@pytest.mark.parametrize("auto_approve, choice", [(False, "deny"), (True, "once")])
def test_with_no_client_to_ask_the_auto_policy_decides(runtime, monkeypatch, auto_approve, choice):
    runtime.config["subagent_auto_approve"] = auto_approve

    decision, sent = _child_approval(runtime, monkeypatch, None)  # None: no attached client can answer

    assert len(sent) == 1 and decision["resolved"] is True and decision["choice"] == choice


def test_auto_mode_never_asks_a_person(runtime, monkeypatch):
    runtime.config.update(subagent_approval="auto", subagent_auto_approve=True)

    decision, sent = _child_approval(runtime, monkeypatch, {"choice": "deny"})

    assert sent == [] and decision["choice"] == "once"


def test_the_approval_payload_is_unchanged_for_the_main_agent(runtime, monkeypatch):
    from tui_gateway import server_requests

    sent: list = []
    monkeypatch.setattr(server_requests, "send_async",
                        lambda method, sid, params, on_result: sent.append(params) or (lambda reason: None))
    server._emit_approval_request("ui-1", {"command": "rm -rf build", "description": "", "request_id": "r-1"})
    assert "subagent_id" not in sent[0] and "goal" not in sent[0]
    assert json.dumps(sent[0])  # the payload stays plain JSON
