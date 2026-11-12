"""``subagent.continue``: a finished background thread runs again on its own stored history.

Real SessionDB, real child construction and the real async-delegation registry in a temp
HERMES_HOME; only the model conversation is replaced. The follow-up keeps the thread's
``subagent_id``, takes a background slot (waiting in line when none is free) and reports to the
parent with an ordinary completion event.
"""

import threading

import pytest

from hermes_state import SessionDB
from tests.tools import test_delegate_transcript_readiness as transcript_readiness
from tests.waiting import wait_until
from tools import async_delegation as ad
from tools import delegate_tool as dt
from tools import delegate_tool_continue as dc
from tools.delegate_tool_sessions import stored_child_session
from tools.process_registry import process_registry

offline_children = transcript_readiness.offline_children


def _completion_for(subagent_id: str) -> dict:
    def take():
        while not process_registry.completion_queue.empty():
            evt = process_registry.completion_queue.get_nowait()
            if any(r.get("subagent_id") == subagent_id for r in evt.get("results") or []):
                return evt
        return None
    return wait_until(take, what=f"the completion of thread {subagent_id}")


@pytest.fixture
def finished_thread(tmp_path, monkeypatch, offline_children):
    """A parent conversation with one FINISHED child whose transcript is stored; yields
    ``(parent, db, subagent_id, runs, config)`` where ``runs`` records each follow-up conversation."""
    ad._reset_for_tests()
    with dc._pending_lock:
        dc._pending.clear()
    config: dict = {}
    monkeypatch.setattr(dt, "_load_config", lambda: config)
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("parent", source="desktop")
    parent = transcript_readiness._parent(db, "parent", lambda *a, **kw: None)
    first = dt._build_child_agent(task_index=0, goal="Draft the release notes", context=None, toolsets=["file"],
                                  model=None, max_iterations=2, task_count=1, parent_agent=parent)
    first._ensure_db_session()
    db.append_message(first.session_id, "user", "Draft the release notes")
    db.append_message(first.session_id, "assistant", "Here are the release notes: ...")
    subagent_id = first._subagent_id
    first.close()

    runs: list = []

    def run_conversation(self, user_message=None, conversation_history=None, **_kwargs):
        history = list(conversation_history or [])
        messages = [*history, {"role": "user", "content": user_message},
                    {"role": "assistant", "content": "Shorter notes: ..."}]
        self._flush_messages_to_session_db(messages, history)  # what the real turn loop does at its end
        runs.append({"subagent_id": self._subagent_id, "session_id": self.session_id, "user_message": user_message,
                     "history": [(m["role"], m["content"]) for m in history]})
        return {"final_response": "Shorter notes: ...", "completed": True, "api_calls": 1, "messages": messages}

    monkeypatch.setattr("run_agent.AIAgent.run_conversation", run_conversation)
    yield parent, db, subagent_id, runs, config
    wait_until(lambda: not ad.active_count(), what="background units to finish")
    ad._reset_for_tests()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()
    db.close()


def _continue(parent, subagent_id, text="Make the summary shorter"):
    return dc.continue_subagent(parent, subagent_id, text, ui_session_id="ui-1", owner_transport=None,
                                owner_session_record=None)


def test_a_finished_thread_runs_again_with_its_history_and_reports_back(finished_thread):
    parent, db, subagent_id, runs, _config = finished_thread

    outcome = _continue(parent, subagent_id)

    assert outcome["status"] == "dispatched" and outcome["subagent_id"] == subagent_id
    evt = _completion_for(subagent_id)
    assert runs == [{
        "subagent_id": subagent_id, "session_id": outcome["child_session_id"],
        "user_message": "Make the summary shorter",
        "history": [("user", "Draft the release notes"), ("assistant", "Here are the release notes: ...")]}]
    # Reported the way a first run is: an async_delegation completion addressed to the parent conversation.
    assert evt["type"] == "async_delegation" and evt["delegation_id"] == outcome["delegation_id"]
    assert (evt["session_key"], evt["parent_session_id"], evt["origin_ui_session_id"]) == ("parent", "parent", "ui-1")
    (result,) = evt["results"]
    assert result["status"] == "completed" and result["summary"] == "Shorter notes: ..."
    # The new segment is the thread's newest and holds the whole thread, so the next follow-up resumes from it.
    assert stored_child_session(db, subagent_id, ["parent"]) == outcome["child_session_id"]
    stored = [(m["role"], m["content"]) for m in db.get_messages_as_conversation(outcome["child_session_id"])]
    assert stored == [("user", "Draft the release notes"), ("assistant", "Here are the release notes: ..."),
                      ("user", "Make the summary shorter"), ("assistant", "Shorter notes: ...")]


def test_a_follow_up_waits_in_line_when_every_slot_is_in_use(finished_thread):
    parent, _db, subagent_id, runs, config = finished_thread
    config["max_concurrent_children"] = 1
    release = threading.Event()
    blocker = ad.dispatch_async_delegation_batch(
        goals=["a long job"], context=None, toolsets=None, role="leaf", model="test", session_key="parent",
        parent_session_id="parent", max_async_children=1, runner=lambda: {"results": []} if release.wait(30) else {})
    assert blocker["status"] == "dispatched"
    try:
        queued = _continue(parent, subagent_id)
        assert queued == {"status": "queued", "subagent_id": subagent_id, "position": 1}
        assert [q["subagent_id"] for q in dc.queued_continuations(parent)] == [subagent_id]
        assert runs == [], "a queued follow-up has not started"
        again = _continue(parent, subagent_id)
        assert again["status"] == "rejected" and "already queued" in again["error"]
    finally:
        release.set()

    evt = _completion_for(subagent_id)  # the slot freed: the follow-up started on its own and reported back
    assert evt["results"][0]["status"] == "completed"
    assert [r["user_message"] for r in runs] == ["Make the summary shorter"]
    assert dc.queued_continuations(parent) == []


def test_only_a_finished_thread_of_this_conversation_can_be_continued(finished_thread):
    parent, db, subagent_id, runs, _config = finished_thread
    from tools.delegate_tool_registry import _register_subagent, _unregister_subagent

    unknown = _continue(parent, "sa-0-00000000")
    assert unknown["status"] == "rejected" and "No stored history" in unknown["error"]

    db.create_session("elsewhere", source="desktop")
    stranger = transcript_readiness._parent(db, "elsewhere", lambda *a, **kw: None)
    assert _continue(stranger, subagent_id)["status"] == "rejected", "another conversation's thread is not continuable"

    _register_subagent({"subagent_id": subagent_id, "goal": "Draft the release notes"})
    try:
        running = _continue(parent, subagent_id)
        assert running["status"] == "rejected" and "still running" in running["error"]
    finally:
        _unregister_subagent(subagent_id)
    assert runs == []
