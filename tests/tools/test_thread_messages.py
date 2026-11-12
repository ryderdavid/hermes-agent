"""``message_parent``: a running background child's interim updates and questions to its parent.

Driven through the tool registry against the real async-delegation registry, completion queue and
ledger (a temp HERMES_HOME): an update is delivered once (also across a restart), a chatty child's
excess updates merge into one delivery, a question blocks the child until the first reply or the
wait limit, and only a child of a running background unit may call the tool.
"""

import json
import queue
import threading
from types import SimpleNamespace

import pytest

from tests.waiting import wait_until
from tools import async_delegation as ad
from tools import async_delegation_messages as adm
from tools import delegate_tool as dt
from tools.delegate_tool_child_run import _register_child
from tools.delegate_tool_registry import _active_subagents, _handle_control_action, _unregister_subagent
from tools.process_registry import process_registry
from tools.process_registry_notifications import async_delegation_display_text, format_process_notification
from tools.registry import registry

import tools.message_parent_tool

SUBAGENT = "sa-0-thread01"


def _drain() -> list:
    events = []
    while not process_registry.completion_queue.empty():
        events.append(process_registry.completion_queue.get_nowait())
    return events


def _next_message(timeout: float = 10.0) -> dict:
    """The next thread message on the shared queue (other events are discarded)."""
    def take():
        try:
            evt = process_registry.completion_queue.get_nowait()
        except queue.Empty:
            return None
        return evt if adm.is_thread_message(evt) else None
    return wait_until(take, timeout=timeout, what="a thread message on the completion queue")


@pytest.fixture
def thread(monkeypatch):
    """A registered child of a RUNNING background unit; yields ``(call, parent, unit_id)``."""
    ad._reset_for_tests()
    adm._reset_for_tests()
    _drain()
    config: dict = {}
    monkeypatch.setattr(dt, "_load_config", lambda: config)
    release = threading.Event()
    handle = ad.dispatch_async_delegation_batch(
        goals=["Check the licences"], context=None, toolsets=None, role="leaf", model="test", session_key="parent",
        origin_ui_session_id="ui-1", parent_session_id="parent", runner=lambda: {"results": []} if release.wait(30) else {})
    unit_id = handle["delegation_id"]
    parent = SimpleNamespace(session_id="parent")
    child = SimpleNamespace(_subagent_id=SUBAGENT, _delegate_depth=1, _delegation_id=unit_id, _async_unit_id=unit_id,
                            model="test")
    _register_child(child, parent, "Check the licences", owner_session_id="ui-1", owner_transport=None,
                    owner_session_record=None)

    def call(**args) -> dict:
        return json.loads(registry.dispatch("message_parent", args, task_id=SUBAGENT))

    yield call, parent, unit_id, config
    _unregister_subagent(SUBAGENT)
    release.set()
    wait_until(lambda: not ad.active_count(), what="the background unit to finish")
    adm._reset_for_tests()
    ad._reset_for_tests()
    _drain()


def test_an_update_is_one_durable_message_delivered_exactly_once(thread):
    call, _parent, unit_id, _config = thread

    assert call(kind="update", text="2 of 5 jobs fail on Android API 35")["status"] == "sent"

    evt = _next_message()
    message = evt["thread_message"]
    assert evt["type"] == "async_delegation" and evt["session_key"] == "parent" and evt["origin_ui_session_id"] == "ui-1"
    assert message["kind"] == "update" and message["text"] == "2 of 5 jobs fail on Android API 35"
    assert (message["subagent_id"], message["delegation_id"], message["goal"]) == (SUBAGENT, unit_id, "Check the licences")
    assert evt["delegation_id"] == message["message_id"] != unit_id, "a message has a ledger row of its own"
    # The delivery claim is the cross-consumer gate: one consumer delivers, a second copy cannot.
    claim = ad.claim_event_delivery(evt, "tui-poller")
    assert claim
    assert ad.claim_event_delivery(dict(evt), "gateway") is None
    ad.complete_event_delivery(evt, claim)
    assert ad.claim_event_delivery(dict(evt), "gateway") is None
    assert ad.active_count() == 1, "the unit keeps running: a message is not its completion"


def test_a_queued_update_survives_a_restart_and_is_not_duplicated(thread):
    call, _parent, _unit_id, _config = thread
    call(kind="update", text="halfway through")
    lost = _next_message()  # the in-memory copy dies with the process

    with ad._orphan_lock:  # a fresh process has offered nothing yet
        ad._offered.clear()
    after_restart: queue.Queue = queue.Queue()
    assert ad.restore_undelivered_completions(after_restart) == 1
    replayed = after_restart.get_nowait()
    assert replayed["restored"] is True
    assert replayed["thread_message"]["text"] == "halfway through"
    assert replayed["thread_message"]["message_id"] == lost["thread_message"]["message_id"]

    claim = ad.claim_event_delivery(replayed, "tui-poller")
    ad.complete_event_delivery(replayed, claim)
    with ad._orphan_lock:
        ad._offered.clear()
    assert ad.restore_undelivered_completions(queue.Queue()) == 0, "a delivered message never replays"


def test_a_chatty_thread_has_its_excess_updates_merged_into_one_delivery(thread):
    call, _parent, _unit_id, config = thread
    config["update_interval_seconds"] = 3600

    assert call(kind="update", text="first")["status"] == "sent"
    assert call(kind="update", text="second")["status"] == "held"
    assert call(kind="update", text="third")["held_updates"] == 2
    assert [e["thread_message"]["text"] for e in _drain() if adm.is_thread_message(e)] == ["first"]

    adm.child_finished(SUBAGENT)  # the held ones go out before the child's completion (or when the window ends)

    merged = _next_message()["thread_message"]
    assert merged["text"] == "second\n\nthird" and merged["merged_count"] == 2
    assert _drain() == []


def test_held_updates_are_delivered_when_the_rate_window_ends(thread):
    call, _parent, _unit_id, config = thread
    config["update_interval_seconds"] = 0.2

    call(kind="update", text="first")
    assert call(kind="update", text="second")["status"] == "held"

    assert [_next_message()["thread_message"]["text"] for _ in range(2)] == ["first", "second"]


@pytest.mark.parametrize("answered_by", ["user", "coordinator"])
def test_a_question_blocks_the_child_until_the_first_reply(thread, answered_by):
    call, parent, _unit_id, _config = thread
    answer: dict = {}
    asking = threading.Thread(
        target=lambda: answer.update(call(kind="question", text="Drop the two AGPL packages?", options=["Drop", "Flag"])))
    asking.start()
    try:
        message = _next_message()["thread_message"]
        question_id = message["question_id"]
        assert message["kind"] == "question" and message["options"] == ["Drop", "Flag"]
        assert _active_subagents[SUBAGENT]["pending_question"]["question_id"] == question_id
        assert asking.is_alive(), "the child waits for its answer"

        if answered_by == "user":
            assert adm.reply_to_question(SUBAGENT, question_id, "Flag them for legal", "user") == {"accepted": True}
        else:  # "Let coordinator decide": the main agent answers with its delegate_task control action
            reply = json.loads(_handle_control_action("reply", SUBAGENT, "Flag them for legal", parent))
            assert reply["status"] == "delivered" and reply["question_id"] == question_id
        asking.join(10)
        assert answer == {"answered": True, "answer": "Flag them for legal", "answered_by": answered_by}

        late = adm.reply_to_question(SUBAGENT, question_id, "Drop them", "user")
        assert late["accepted"] is False and f"already answered by the {answered_by}" in late["error"]
        assert _active_subagents[SUBAGENT]["pending_question"] is None
    finally:
        adm._reset_for_tests()
        asking.join(10)


def test_an_unanswered_question_resumes_the_child_with_no_answer(thread, monkeypatch):
    call, parent, _unit_id, _config = thread
    monkeypatch.setattr(adm, "question_timeout_seconds", lambda: 0.05)
    seen: list = []
    adm.set_question_notifier(lambda route, payload: seen.append((route["origin_ui_session_id"], payload["status"])))
    try:
        assert call(kind="question", text="Which branch?") == {"answered": False, "reason": "no_answer"}
    finally:
        adm.set_question_notifier(None)

    question_id = _next_message()["thread_message"]["question_id"]
    assert seen == [("ui-1", "pending"), ("ui-1", "expired")]
    assert _active_subagents[SUBAGENT]["pending_question"] is None
    late = adm.reply_to_question(SUBAGENT, question_id, "main", "user")
    assert late["accepted"] is False and "wait limit passed" in late["error"]
    assert "no open question" in json.loads(_handle_control_action("reply", SUBAGENT, "main", parent))["error"]


def test_only_a_child_of_a_running_background_unit_may_message_its_parent(thread):
    call, _parent, _unit_id, _config = thread
    outsider = json.loads(registry.dispatch("message_parent", {"kind": "update", "text": "hi"}, task_id="not-a-child"))
    assert "only available to a running background thread" in outsider["error"]
    assert "error" in call(kind="shout", text="hi") and "error" in call(kind="update", text="  ")
    assert _drain() == []


def test_a_message_reads_as_its_own_parent_turn(thread):
    call, _parent, _unit_id, _config = thread
    call(kind="update", text="Build is green again")
    update = _next_message()
    text = format_process_notification(update)
    assert text.startswith(f"[THREAD UPDATE — {SUBAGENT}]") and "Build is green again" in text
    assert "ASYNC DELEGATION COMPLETE" not in text
    assert async_delegation_display_text(update) == "Thread Update: Check the licences"
    assert adm.thread_message_annotation(update)["type"] == "async_delegation_message"


def test_first_level_children_get_the_tool_and_deeper_ones_do_not():
    from unittest.mock import MagicMock, patch

    from toolsets import resolve_toolset

    assert resolve_toolset("thread_messaging") == ["message_parent"]
    assert registry.get_entry("message_parent").toolset == "thread_messaging"

    def child_toolsets(depth: int) -> list:
        parent = MagicMock(_delegate_depth=depth, _session_db=None, _active_children=[], tool_progress_callback=None,
                           _active_children_lock=threading.Lock(), enabled_toolsets=["web", "thread_messaging"],
                           base_url="https://openrouter.ai/api/v1", provider="openrouter", model="m", _print_fn=None)
        with patch("run_agent.AIAgent") as agent, patch.object(dt, "_load_config", return_value={"max_spawn_depth": 3}):
            agent.return_value = MagicMock()
            dt._build_child_agent(task_index=0, goal="g", context=None, toolsets=None, model=None, max_iterations=5,
                                  task_count=1, parent_agent=parent)
        return agent.call_args[1]["enabled_toolsets"]

    assert child_toolsets(0).count("thread_messaging") == 1
    assert "thread_messaging" not in child_toolsets(1), "a grandchild reports to its orchestrator, not to a chat"
