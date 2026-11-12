"""Continue a finished background thread: ``subagent.continue {subagent_id, text}``.

The finished child's newest stored segment (``delegate_tool_sessions.stored_child_session``) seeds a new
child that keeps the thread's ``subagent_id`` and runs the follow-up with that history. It is dispatched
as an ordinary async unit, so it takes a background slot and reports back like a first run. With no free
slot the follow-up waits in a FIFO and starts when a unit finishes (``async_delegation.add_slot_listener``).
"""

from __future__ import annotations

import collections
import contextvars
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("tools.delegate_tool")

_MESSAGE_KEYS = ("role", "content", "tool_calls", "tool_call_id", "name")
_pending_lock = threading.Lock()
_pending: collections.deque[_Continuation] = collections.deque()


@dataclass
class _Continuation:
    parent_agent: Any
    subagent_id: str
    text: str
    goal: str
    history: list
    ui_session_id: str
    owner_transport: Any
    owner_session_record: Any
    # The requesting RPC's Context (profile home, secrets, approval session key): a queued follow-up
    # starts on a finalizing worker thread that has none of it.
    context: contextvars.Context = field(default_factory=contextvars.copy_context)
    queued_at: float = field(default_factory=time.time)
    waited: bool = False  # already queued once: it keeps its place at the head when it must wait again


def queued_continuations(parent_agent: Any) -> list[dict[str, Any]]:
    """Follow-ups of *parent_agent*'s threads still waiting for a slot, oldest first."""
    with _pending_lock:
        return [{"subagent_id": c.subagent_id, "goal": c.goal, "queued_at": c.queued_at}
                for c in _pending if c.parent_agent is parent_agent]


def _stored_thread(parent_agent: Any, subagent_id: str) -> tuple[Optional[str], list, str]:
    """``(child session id, its history, its goal)`` for a finished child of this conversation."""
    from agent.message_content import flatten_message_text
    from gateway.session_ancillary import _compression_lineage
    from tools.delegate_tool_sessions import stored_child_session
    db, parent_sid = getattr(parent_agent, "_session_db", None), str(getattr(parent_agent, "session_id", "") or "")
    if db is None or not parent_sid:
        return None, [], ""
    child_sid = stored_child_session(db, subagent_id, _compression_lineage(db, parent_sid))
    if child_sid is None:
        return None, [], ""
    # Plain OpenAI-shaped copies: the stored rows' ids and markers belong to the finished segment.
    history = [{k: m[k] for k in _MESSAGE_KEYS if m.get(k) is not None}
               for m in db.get_messages_as_conversation(child_sid) if isinstance(m, dict)]
    goal = next((flatten_message_text(m.get("content")).strip() for m in history if m.get("role") == "user"), "")
    return child_sid, history, " ".join(goal.split())[:500]


def continue_subagent(parent_agent: Any, subagent_id: str, text: str, *, ui_session_id: str,
                      owner_transport: Any, owner_session_record: Any) -> dict[str, Any]:
    """Run finished thread *subagent_id* again with *text*: ``{"status": "dispatched" | "queued" | "rejected"}``."""
    from tools.delegate_tool_registry import _active_subagents, _active_subagents_lock, is_spawn_paused
    with _active_subagents_lock:
        running = subagent_id in _active_subagents
    if running:
        return _rejected(subagent_id, "This thread is still running: steer it instead of continuing it.")
    if is_spawn_paused():
        return _rejected(subagent_id, "Delegation spawning is paused.")
    with _pending_lock:
        if any(c.subagent_id == subagent_id for c in _pending):
            return _rejected(subagent_id, "A follow-up for this thread is already queued.")
    child_sid, history, goal = _stored_thread(parent_agent, subagent_id)
    if child_sid is None or not history:
        return _rejected(subagent_id, "No stored history for this thread in this chat.")
    return _start(_Continuation(parent_agent, subagent_id, text, goal or text, history, ui_session_id,
                                owner_transport, owner_session_record))


def _rejected(subagent_id: str, error: str) -> dict[str, Any]:
    return {"status": "rejected", "subagent_id": subagent_id, "error": error}


def _queue(request: _Continuation) -> dict[str, Any]:
    with _pending_lock:
        (_pending.appendleft if request.waited else _pending.append)(request)
        request.waited = True
        position = next(i for i, c in enumerate(_pending) if c is request) + 1
    return {"status": "queued", "subagent_id": request.subagent_id, "position": position}


def _start(request: _Continuation) -> dict[str, Any]:
    """Build the child and hand it to the async registry, or queue the follow-up when no slot is free."""
    from tools import async_delegation as ad
    from tools.delegate_tool import (
        DEFAULT_MAX_ITERATIONS, _build_children, _get_max_async_children, _get_max_concurrent_children,
        _load_config, _parent_live_home, _routes_for,
    )
    from tools.delegate_tool_child_run import _close_child, _detach_child
    from tools.delegate_tool_dispatch import _Batch, _dispatch_unit
    from tools.delegation_live_log import create_live_transcripts
    parent, max_async = request.parent_agent, _get_max_async_children()
    if not ad.has_free_slot(max_async):
        return _queue(request)
    cfg = _load_config()
    task_list = [{"goal": request.goal}]
    routes, err = _routes_for(task_list, cfg, None, parent)
    if err or routes[0].error:
        return _rejected(request.subagent_id, err or routes[0].error)
    creds, live_home = routes[0].creds, _parent_live_home(parent)
    live_id, writers, paths = create_live_transcripts(
        task_list, None, model=creds.get("model"), provider=creds.get("provider"), home=live_home)
    children, err = _build_children(
        task_list, [None], routes, top_role="leaf", max_iterations=cfg.get("max_iterations", DEFAULT_MAX_ITERATIONS),
        parent_agent=parent, live_deleg_id=live_id, live_writers=writers, subagent_ids=[request.subagent_id])
    if err:
        return _rejected(request.subagent_id, err)
    child = children[0][2]
    child._delegate_continue = {"history": request.history, "message": request.text}
    # The new segment stores the whole thread, so a later follow-up resumes from it alone.
    child._session_row_replay_pending = child.session_id
    unit_id = live_id or ad._new_delegation_id()
    batch = _Batch(task_list, children, parent, creds, None, "leaf", _get_max_concurrent_children(), live_id, writers,
                   paths, "", request.ui_session_id, request.owner_transport, request.owner_session_record, False,
                   time.monotonic(), unit_id=unit_id, live_home=live_home)
    _detach_child(parent, child)  # a background unit outlives the parent's turn stops
    child._async_unit_id = unit_id
    parent_sid = str(getattr(parent, "session_id", "") or "")
    dispatch = _dispatch_unit(batch, unit_id, None, dict(
        session_key=parent_sid, origin_ui_session_id=request.ui_session_id, origin_session_id="",
        parent_session_id=parent_sid, max_async_children=max_async))
    if dispatch.get("status") == "dispatched":
        logger.info("Continuing thread %s as %s", request.subagent_id, unit_id)
        return {"status": "dispatched", "subagent_id": request.subagent_id, "delegation_id": unit_id,
                "child_session_id": str(getattr(child, "session_id", "") or "") or None}
    _close_child(child, "Failed to close a continued subagent that was not admitted")
    if not ad.has_free_slot(max_async):  # lost the slot between the check and the dispatch
        request.waited = True
        return _queue(request)
    return _rejected(request.subagent_id, str(dispatch.get("error") or "not admitted"))


def _drain() -> None:
    """A slot freed: start queued follow-ups, oldest first, until one has to wait again."""
    while True:
        with _pending_lock:
            if not _pending:
                return
            request = _pending.popleft()
        try:
            outcome = request.context.run(_start, request)
        except Exception:
            logger.warning("Queued follow-up for thread %s could not start", request.subagent_id, exc_info=True)
            continue
        if outcome["status"] == "queued":
            return
        if outcome["status"] == "rejected":
            logger.warning("Queued follow-up for thread %s was dropped: %s", request.subagent_id, outcome.get("error"))


def _install() -> None:
    from tools.async_delegation import add_slot_listener
    add_slot_listener(_drain)


_install()
