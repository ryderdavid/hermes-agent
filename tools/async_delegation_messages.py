"""Interim child-to-parent messages (the ``message_parent`` tool) on the async-delegation completion path.

A background child can tell its parent something before it finishes: an ``update`` (fire and
forget) or a ``question`` (the child blocks until it is answered or the wait limit passes). Both
ride the SHARED ``process_registry.completion_queue`` as a ``type="async_delegation"`` event that
carries a ``thread_message`` dict, with a ledger row of its own (``delegation_id`` = the message
id, ``state='message'``). So every drain, ownership, claim, de-dup and restart-replay path treats a
message exactly like a completion: it is delivered once, as a NEW parent turn, never mid-turn.

Updates are rate-limited per child (``delegation.update_interval_seconds``); excess updates are
held and merged into the next allowed delivery. Questions are never held.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import threading
import time
import uuid
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

THREAD_MESSAGE_KEY = "thread_message"
THREAD_MESSAGE_DISPLAY_KIND = "async_delegation_message"
DEFAULT_UPDATE_INTERVAL_S = 60.0
DEFAULT_QUESTION_TIMEOUT_S = 900.0
_MAX_TEXT_CHARS = 4000
_MAX_OPTIONS = 6
_MAX_RESOLVED = 200
_ROUTE_FIELDS = ("session_key", "origin_ui_session_id", "origin_session_id", "parent_session_id")


class _Question:
    __slots__ = ("answer", "answered_by", "event", "question_id", "route", "subagent_id")

    def __init__(self, question_id: str, subagent_id: str, route: dict[str, Any]) -> None:
        self.question_id, self.subagent_id, self.route = question_id, subagent_id, route
        self.event = threading.Event()
        self.answer: Optional[str] = None
        self.answered_by: Optional[str] = None


_lock = threading.Lock()
# subagent_id -> monotonic time of its last delivered update.
_last_update: dict[str, float] = {}
# subagent_id -> {"texts", "timer", "route"}: updates held for the next allowed delivery.
_held: dict[str, dict[str, Any]] = {}
_questions: dict[str, _Question] = {}
# question_id -> how it ended, so a late reply gets a reason instead of "unknown question" (bounded FIFO).
_resolved: dict[str, str] = {}
# ``notify(route, payload)``: the TUI gateway's ``subagent.question`` emitter (route names the owning session).
_question_notifier: Optional[Callable[[dict[str, Any], dict[str, Any]], None]] = None


def set_question_notifier(notify: Optional[Callable[[dict[str, Any], dict[str, Any]], None]]) -> None:
    global _question_notifier
    _question_notifier = notify


def is_thread_message(evt: Any) -> bool:
    return isinstance(evt, dict) and isinstance(evt.get(THREAD_MESSAGE_KEY), dict)


def update_interval_seconds() -> float:
    """delegation.update_interval_seconds: least time between two ``update`` deliveries of one child (0 = no limit)."""
    from tools.delegate_tool_config import _knob
    return _knob("update_interval_seconds", None, lambda v: max(0.0, float(v)), DEFAULT_UPDATE_INTERVAL_S,
                 "delegation.update_interval_seconds=%r is not a number; using the default of 60s")


def question_timeout_seconds() -> float:
    """delegation.question_timeout: how long a child waits for an answer before it resumes without one."""
    from tools.delegate_tool_config import _knob
    return _knob("question_timeout", None, lambda v: max(1.0, float(v)), DEFAULT_QUESTION_TIMEOUT_S,
                 "delegation.question_timeout=%r is not a number; using the default of 900s")


def _thread_route(subagent_id: str) -> Optional[dict[str, Any]]:
    """Attribution and routing for a live child of a RUNNING background unit, else None. The unit's record
    holds the routing its completion will use, so a message reaches the same parent session."""
    from tools import async_delegation as ad
    from tools.delegate_tool_registry import _active_subagents, _active_subagents_lock
    with _active_subagents_lock:
        record = _active_subagents.get(subagent_id)
        if record is None:
            return None
        goal, unit_id = record.get("goal"), getattr(record.get("agent"), "_async_unit_id", None)
    if not unit_id:
        return None
    with ad._records_lock:
        unit = ad._records.get(unit_id)
        if unit is None or unit.get("status") not in ad._ACTIVE_STATES:
            return None
        return {"subagent_id": subagent_id, "goal": str(goal or ""), "delegation_id": unit_id,
                **{k: unit.get(k) for k in (*_ROUTE_FIELDS, *ad._ROUTING_KEYS) if unit.get(k)}}


def _persist_message(evt: dict[str, Any]) -> None:
    """Durable row for one message, shaped so ``restore_undelivered_completions`` and the orphan sweep replay it
    and the delivery claim settles it. A compute-host worker has no ledger of its own: its messages are in-memory."""
    from tools import async_delegation as ad
    from tools.delegation_redaction import redact_completion_event
    if ad.worker_ledger():
        return
    from gateway.status import get_process_start_time
    owner_started_at = get_process_start_time(os.getpid())
    now = time.time()
    with ad._DB_LOCK, ad._transaction() as conn:
        conn.execute("""INSERT OR REPLACE INTO async_delegations
               (delegation_id, origin_session, origin_ui_session_id, parent_session_id, state, dispatched_at,
                completed_at, updated_at, event_json, delivery_state, delivery_attempts, owner_pid,
                owner_started_at, task_json, origin_session_id)
               VALUES (?, ?, ?, ?, 'message', ?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?)""",
            (evt["delegation_id"], evt.get("session_key", ""), evt.get("origin_ui_session_id", ""),
             evt.get("parent_session_id"), now, now, now, json.dumps(redact_completion_event(evt)), os.getpid(),
             owner_started_at, json.dumps({"goal": evt.get("goal", "")}), evt.get("origin_session_id", "")))


def _enqueue(route: dict[str, Any], kind: str, texts: list[str], **question: Any) -> str:
    """Persist and queue one message event; returns its message id."""
    from tools.process_registry import process_registry
    message_id = f"dmsg_{uuid.uuid4().hex[:10]}"
    now = time.time()
    evt = {
        "type": "async_delegation", "delegation_id": message_id, "status": "message", "goal": route["goal"],
        THREAD_MESSAGE_KEY: {
            "message_id": message_id, "kind": kind, "text": "\n\n".join(texts), "subagent_id": route["subagent_id"],
            "delegation_id": route["delegation_id"], "goal": route["goal"], "sent_at": now,
            **({"merged_count": len(texts)} if len(texts) > 1 else {}), **question},
        "session_key": route.get("session_key", ""), "origin_ui_session_id": route.get("origin_ui_session_id", ""),
        "origin_session_id": route.get("origin_session_id", ""), "parent_session_id": route.get("parent_session_id"),
        "dispatched_at": now, "completed_at": now,
        **{k: v for k, v in route.items() if k not in (*_ROUTE_FIELDS, "subagent_id", "goal", "delegation_id")}}
    try:
        _persist_message(evt)
    except Exception:  # the ledger is busy or unreadable: the message still reaches the live parent
        logger.exception("Thread message %s: durable write failed; delivering in-memory only", message_id)
    process_registry.completion_queue.put(evt)
    return message_id


def post_update(subagent_id: str, text: str) -> dict[str, Any]:
    """Send (or hold, when the child's rate limit is spent) one interim update."""
    route = _thread_route(subagent_id)
    if route is None:
        return {"error": "message_parent is only available to a running background thread."}
    interval, now = update_interval_seconds(), time.monotonic()
    with _lock:
        held, last = _held.get(subagent_id), _last_update.get(subagent_id)
        if held is None and (last is None or now - last >= interval):
            _last_update[subagent_id] = now
        else:
            if held is None:
                # The timer thread starts with an empty Context: flush under this child's (profile home, ledger).
                timer = threading.Timer(max(0.0, interval - (now - (last or now))), contextvars.copy_context().run,
                                        args=(flush_held_updates, subagent_id))
                timer.daemon = True
                held = _held[subagent_id] = {"texts": [], "timer": timer, "route": route}
                timer.start()
            held["texts"].append(text)
            return {"status": "held", "held_updates": len(held["texts"]),
                    "note": "Rate limit reached: this update is merged into the next delivery to your parent."}
    return {"status": "sent", "message_id": _enqueue(route, "update", [text])}


def flush_held_updates(subagent_id: str) -> Optional[str]:
    """Deliver a child's held updates now as ONE message; returns its id, or None when nothing was held."""
    with _lock:
        held = _held.pop(subagent_id, None)
        if held is None or not held["texts"]:
            return None
        _last_update[subagent_id] = time.monotonic()
    held["timer"].cancel()
    return _enqueue(held["route"], "update", held["texts"])


def child_finished(subagent_id: Optional[str]) -> None:
    """A child's run ended: deliver what it still holds (before its completion event) and forget its rate limit."""
    if not subagent_id:
        return
    try:
        flush_held_updates(subagent_id)
    except Exception:
        logger.warning("Thread %s: could not deliver its held updates", subagent_id, exc_info=True)
    with _lock:
        _last_update.pop(subagent_id, None)


def _set_pending_question(subagent_id: str, pending: Optional[dict[str, Any]]) -> None:
    """Mirror the open question onto the live registry record (``subagent.list`` / ``delegation.status``)."""
    from tools.delegate_tool_registry import _active_subagents, _active_subagents_lock
    with _active_subagents_lock:
        if (record := _active_subagents.get(subagent_id)) is not None:
            record["pending_question"] = pending


def _notify_question(route: dict[str, Any], payload: dict[str, Any]) -> None:
    notify = _question_notifier
    if notify is None:
        return
    try:
        notify(route, payload)
    except Exception:
        logger.debug("subagent.question notifier failed", exc_info=True)


def _wait_for_answer(question: _Question, timeout: float) -> str:
    """Block until answered, interrupted or timed out. Polled in ~1s slices so the child's inactivity
    trackers (heartbeat, stale monitor) see a live wait, like an approval prompt."""
    from tools.approval_human_wait import activity_heartbeat
    from tools.interrupt import is_interrupted
    deadline, heartbeat = time.monotonic() + timeout, activity_heartbeat("waiting for the parent's answer")
    while True:
        remaining = deadline - time.monotonic()
        if question.event.wait(timeout=max(0.0, min(1.0, remaining))):
            return "answered" if question.answer is not None else "cancelled"
        if is_interrupted():
            return "cancelled"
        if remaining <= 0:
            return "expired"
        heartbeat()


def ask_question(subagent_id: str, text: str, options: Optional[list[str]] = None) -> dict[str, Any]:
    """Ask the parent and wait: ``{"answered": True, "answer", "answered_by"}`` or ``{"answered": False}``."""
    route = _thread_route(subagent_id)
    if route is None:
        return {"error": "message_parent is only available to a running background thread."}
    question_id, timeout = f"q_{uuid.uuid4().hex[:10]}", question_timeout_seconds()
    question = _Question(question_id, subagent_id, route)
    asked = {"question_id": question_id, "text": text, **({"options": options} if options else {}),
             "asked_at": time.time(), "expires_at": time.time() + timeout}
    with _lock:
        _questions[question_id] = question
    _set_pending_question(subagent_id, asked)
    state = "cancelled"
    try:
        flush_held_updates(subagent_id)  # what the child said before asking is read first
        _enqueue(route, "question", [text], question_id=question_id, timeout_seconds=timeout,
                 **({"options": options} if options else {}))
        _notify_question(route, {"subagent_id": subagent_id, "status": "pending", "goal": route["goal"], **asked})
        state = _wait_for_answer(question, timeout)
    finally:
        with _lock:
            _questions.pop(question_id, None)
            # A reply can land between the wait ending and this removal: it was accepted, so it counts.
            state = "answered" if question.answer is not None else state
            _resolved[question_id] = (f"it was already answered by the {question.answered_by}" if state == "answered"
                                      else "its wait limit passed" if state == "expired" else "the thread stopped waiting")
            while len(_resolved) > _MAX_RESOLVED:
                _resolved.pop(next(iter(_resolved)))
        _set_pending_question(subagent_id, None)
    _notify_question(route, {
        "subagent_id": subagent_id, "question_id": question_id, "status": state, "goal": route["goal"],
        **({"answered_by": question.answered_by, "answer": question.answer} if state == "answered" else {})})
    if state == "answered":
        return {"answered": True, "answer": question.answer, "answered_by": question.answered_by}
    return {"answered": False, "reason": "no_answer" if state == "expired" else "cancelled"}


def pending_question_id(subagent_id: str) -> Optional[str]:
    with _lock:
        return next((q.question_id for q in _questions.values() if q.subagent_id == subagent_id), None)


def reply_to_question(subagent_id: str, question_id: str, text: str, answered_by: str) -> dict[str, Any]:
    """Answer an open question. The first reply wins: ``{"accepted": True}``, else ``{"accepted": False, "error"}``."""
    with _lock:
        question = _questions.get(question_id)
        if question is None or question.subagent_id != subagent_id:
            ended = _resolved.get(question_id)
            return {"accepted": False, "error": (f"Question {question_id} is closed: {ended}." if ended else
                                                 f"No open question {question_id} for thread {subagent_id}.")}
        if question.answer is not None:
            return {"accepted": False,
                    "error": f"Question {question_id} is closed: it was already answered by the {question.answered_by}."}
        question.answer, question.answered_by = text, answered_by
        question.event.set()
    return {"accepted": True}


def message_parent(kind: Any, text: Any, options: Any = None, *, task_id: Optional[str] = None) -> dict[str, Any]:
    """Tool body. A child runs its tools under ``task_id == subagent_id``, which is how the caller is known."""
    kind, text = str(kind or "").strip().lower(), str(text or "").strip()
    if kind not in ("update", "question"):
        return {"error": "kind must be 'update' or 'question'."}
    if not text:
        return {"error": "text is required."}
    text = text[:_MAX_TEXT_CHARS]
    subagent_id = str(task_id or "")
    if kind == "update":
        return post_update(subagent_id, text)
    choices = [str(o).strip()[:200] for o in options if str(o).strip()][:_MAX_OPTIONS] if isinstance(options, list) else []
    return ask_question(subagent_id, text, choices or None)


# ── Presentation (model-facing turn text, UI title, client annotation) ──────
def thread_message_annotation(evt: dict[str, Any]) -> dict[str, Any]:
    """What a client sees: the ``display_metadata`` of the parent turn and the ``subagent.message`` payload."""
    message = evt.get(THREAD_MESSAGE_KEY) or {}
    out = {"type": THREAD_MESSAGE_DISPLAY_KIND, "display_text": thread_message_display_text(evt)}
    for key in ("message_id", "kind", "text", "subagent_id", "delegation_id", "goal", "sent_at", "merged_count",
                "question_id", "options", "timeout_seconds"):
        if message.get(key) is not None:
            out[key] = message[key]
    return out


def thread_message_display_text(evt: dict[str, Any]) -> str:
    message = evt.get(THREAD_MESSAGE_KEY) or {}
    goal = " ".join(str(message.get("goal") or "Background thread").split())
    goal = goal[:117] + "..." if len(goal) > 120 else goal
    return f"Thread {'Question' if message.get('kind') == 'question' else 'Update'}: {goal}"


def format_thread_message(evt: dict[str, Any]) -> str:
    """The parent agent's turn text for one message."""
    message = evt.get(THREAD_MESSAGE_KEY) or {}
    subagent_id, goal = message.get("subagent_id", "unknown"), message.get("goal") or ""
    if message.get("kind") != "question":
        merged = message.get("merged_count")
        return "\n".join(line for line in (
            f"[THREAD UPDATE — {subagent_id}]",
            "A background thread you started sent an interim update. It is still running; its final result "
            "arrives later as its own message. Tell the user briefly if this matters to them. Do not reply to the thread.",
            f"Thread task: {goal}" if goal else "",
            f"({merged} updates merged into this one)" if merged else "",
            "--- UPDATE ---", str(message.get("text") or "")) if line)
    options = message.get("options") or []
    minutes = round(float(message.get("timeout_seconds") or DEFAULT_QUESTION_TIMEOUT_S) / 60)
    return "\n".join(line for line in (
        f"[THREAD QUESTION — {subagent_id}, question {message.get('question_id')}]",
        "A background thread you started is paused, waiting for an answer to the question below. Relay the "
        "question to the user and end your turn: the user normally answers the thread directly. Answer it "
        "yourself ONLY when the user asks you to decide, with delegate_task(action='reply', "
        f"subagent_id='{subagent_id}', message='<your answer>'). Unanswered, the thread resumes without an "
        f"answer after about {minutes} minute(s).",
        f"Thread task: {goal}" if goal else "",
        "--- QUESTION ---", str(message.get("text") or ""),
        ("Options: " + " | ".join(str(o) for o in options)) if options else "") if line)


def _reset_for_tests() -> None:
    with _lock:
        for held in _held.values():
            held["timer"].cancel()
        _held.clear()
        _last_update.clear()
        for question in _questions.values():
            question.event.set()
        _questions.clear()
        _resolved.clear()
