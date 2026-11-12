"""Coordinator threads for shared clients (docs/coordinator-threads.md): the per-session coordinator
flag (``session.mode.set``), answering a child's question (``subagent.reply``), following up a finished
child (``subagent.continue``) and the capability keys ``session.info`` advertises."""

from .method_ctx import HandlerRegistry, bind_module

_registry = HandlerRegistry()
method = _registry.method

# Feature keys of this gateway, reported as ``session.info.capabilities``.
THREAD_CAPABILITIES = (
    "threads.roster", "threads.steer", "threads.messages", "threads.continue", "threads.approvals", "coordinator_mode",
)
# ``sessions.model_config`` key: conversation-wide, copied onto compression continuations.
_COORDINATOR_CONFIG_KEY = "coordinator_mode"


def _coordinator_mode(session: dict) -> bool:
    """The session's coordinator flag. A record that never had it set (resume, reattach) reads its stored row once."""
    if "coordinator_mode" not in session:
        stored, key = False, str(session.get("session_key") or "")
        if key:
            with contextlib.suppress(Exception), _session_db(session) as db:
                if db is not None:
                    stored = bool(db.get_session_model_config_value(
                        db.get_compression_tip(key) or key, _COORDINATOR_CONFIG_KEY, False))
        session["coordinator_mode"] = stored
    return bool(session["coordinator_mode"])


def _persist_coordinator_mode(session: dict) -> bool:
    """Write the flag onto the conversation's row; False while the row does not exist yet (a draft chat), in
    which case the next turn start writes it."""
    enabled, key, written = bool(session.get("coordinator_mode")), str(session.get("session_key") or ""), False
    with contextlib.suppress(Exception), _session_db(session) as db:
        written = db is not None and db.update_conversation_model_config_value(
            key, _COORDINATOR_CONFIG_KEY, lambda _old: True if enabled else None) is not None
    session["_coordinator_mode_unsaved"] = not written
    return written


def _sync_coordinator_mode(session: dict) -> None:
    """Make the agent's ephemeral system prompt carry the coordinator block iff the flag is set. Run at
    ``session.mode.set`` and at every turn start, so a rebuilt agent, a resumed session and a personality
    change (which replaces the ephemeral prompt) all converge. Appended at API-call time, like a
    personality overlay: the cached prefix changes only when the flag itself changes."""
    from agent.prompt_builder import COORDINATOR_MODE_GUIDANCE
    enabled = _coordinator_mode(session)
    if session.get("_coordinator_mode_unsaved"):
        _persist_coordinator_mode(session)
    current = str(getattr(session.get("agent"), "ephemeral_system_prompt", None) or "")
    if session.get("agent") is None or (COORDINATOR_MODE_GUIDANCE in current) == enabled:
        return  # the usual case: nothing to add and nothing to strip, so the agent is not touched
    base = current.replace(COORDINATOR_MODE_GUIDANCE, "").strip()
    session["agent"].ephemeral_system_prompt = "\n\n".join(
        part for part in (base, COORDINATOR_MODE_GUIDANCE if enabled else "") if part) or None


def _with_thread_attribution(data: dict | None) -> dict:
    """An approval's data plus ``{subagent_id, goal}`` when a delegated child's run raised it: a child of a
    gateway session waits on its parent session's approval queue, so the request names the thread."""
    from tools.delegate_tool_config import subagent_approval_attribution
    return {**(data or {}), **subagent_approval_attribution()}


def _thread_approval_without_a_person(payload: dict, session_key: str, *, asked: bool) -> bool:
    """Settle a THREAD's approval by ``delegation.subagent_auto_approve`` (approve once when true, else deny)
    when no person will answer it: always under ``delegation.subagent_approval: auto``, else once the ask
    found no attached client that can answer (``asked``). False: not a thread's, or a person is being asked."""
    from tools import approval
    from tools.delegate_tool_config import _get_subagent_approval_callback, subagent_approval_mode
    if not payload.get("subagent_id") or not (asked or subagent_approval_mode() == "auto"):
        return False
    choice = _get_subagent_approval_callback()(str(payload.get("command") or ""), str(payload.get("description") or ""))
    approval.resolve_gateway_approval(session_key, choice, request_id=str(payload.get("request_id") or "") or None)
    return True


@method("session.mode.set")
def _(rid, params: dict) -> dict:
    """Set or clear coordinator mode for one session. Clearing changes later turns only: children already
    running are not touched and still report back."""
    session, err = _sess_nowait(params, rid)
    if err is not None:
        return err
    if not isinstance(coordinator := params.get("coordinator"), bool):
        return _err(rid, 4000, "coordinator must be true or false")
    session["coordinator_mode"] = coordinator
    pending = not _persist_coordinator_mode(session)
    _sync_coordinator_mode(session)
    _emit_session_info_for_session(params.get("session_id", ""), session)
    return _ok(rid, {"coordinator": coordinator, "pending": pending})


def _thread_authority(rid, params: dict):
    """``(session_id, transport, owner, None)`` for a subagent RPC, or an error response in the last slot."""
    session_id = _str_param(params, "session_id")
    transport, owner = _current_session_steer_authority(session_id)
    if transport is None or owner is None:
        return session_id, None, None, _err(rid, 4001, "session not found or not owned by this transport")
    return session_id, transport, owner, None


@method("subagent.reply")
def _(rid, params: dict) -> dict:
    """Answer the question a child is blocked on. Authority is the conversation (the ``subagent.list``
    rule), not the exact session generation: a question can wait many minutes and the client that
    answers has often reconnected since the child was spawned."""
    from tools.async_delegation_messages import reply_to_question

    subagent_id, question_id = _str_param(params, "subagent_id"), _str_param(params, "question_id")
    if not subagent_id or not question_id:
        return _err(rid, 4000, "subagent_id and question_id required")
    if not (text := (params.get("text") or "").strip()):
        return _err(rid, 4002, "text is required")
    answered_by = _str_param(params, "answered_by", "user")
    if answered_by not in ("user", "coordinator"):
        return _err(rid, 4000, "answered_by must be 'user' or 'coordinator'")
    session_id, transport, owner, err = _thread_authority(rid, params)
    if err is not None:
        return err
    if not any(r.get("subagent_id") == subagent_id for r in _visible_subagent_records(session_id, transport, owner)):
        return _err(rid, 4004, "no running thread with that id in this chat")
    verdict = reply_to_question(subagent_id, question_id, text, answered_by)
    if not verdict["accepted"]:
        return _err(rid, 4090, verdict["error"])
    return _ok(rid, {"accepted": True, "subagent_id": subagent_id, "question_id": question_id,
                     "answered_by": answered_by})


@method("subagent.continue")
def _(rid, params: dict) -> dict:
    """Follow up a FINISHED child of this session; it re-runs on its stored history as a background unit."""
    from tools.approval_context import reset_current_session_key, set_current_session_key
    from tools.delegate_tool_continue import continue_subagent

    if not (subagent_id := _str_param(params, "subagent_id")):
        return _err(rid, 4000, "subagent_id required")
    if not (text := (params.get("text") or "").strip()):
        return _err(rid, 4002, "text is required")
    session_id, transport, owner, err = _thread_authority(rid, params)
    if err is not None:
        return err
    if (agent := owner.get("agent")) is None:
        return _err(rid, 4001, "session has no agent yet")
    # The child is built and dispatched as inside a turn of this session: its profile, its session identity
    # (completion routing) and its approval queue (a child's approval is asked on this session's channel).
    approval = set_current_session_key(owner["session_key"])
    tokens = _set_session_context(owner["session_key"], ui_session_id=session_id)
    try:
        with _session_profile_runtime_scope(owner):
            result = continue_subagent(agent, subagent_id, text, ui_session_id=session_id,
                                       owner_transport=transport, owner_session_record=owner)
    finally:
        _clear_session_context(tokens)
        reset_current_session_key(approval)
    return _ok(rid, result)


def register(server) -> None:
    bind_module(globals(), server, skip=("_",))
