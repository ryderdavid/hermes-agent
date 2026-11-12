# Coordinator threads — gateway wire contract

Gateway side of the `coordinator-threads` change in `ryderdavid/hermes-android`
(`openspec/changes/coordinator-threads/`, decisions D5–D9). The Android app is
coded against this file: every field name below is the name on the wire.

A **thread** is a background subagent (`delegate_task` child) of one chat. The
chat's main agent is its **parent**; in coordinator mode it is the
**coordinator**. Threads are identified by `subagent_id` (`sa-<n>-<8 hex>`),
which stays the same when a finished thread is continued.

Everything here is served by the legacy JSON-RPC surface (`tui_gateway/`,
reached at `/api/ws` of a standalone `hermes dashboard` / `hermes serve`). See
[Deviations](#deviations) for what that excludes.

Conventions:

- A request is `{"jsonrpc": "2.0", "id": <n>, "method": "<name>", "params": {...}}`.
  A success is `{"jsonrpc": "2.0", "id": <n>, "result": {...}}`, a failure is
  `{"jsonrpc": "2.0", "id": <n>, "error": {"code": <int>, "message": "<text>"}}`.
- An event is `{"jsonrpc": "2.0", "method": "event", "params": {"type": "<name>",
  "session_id": "<live session id>", "payload": {...}}}`.
- `session_id` is the **live** session id (the one `session.create` /
  `session.resume` returned), as for every other `subagent.*` RPC. Unknown
  param keys are rejected with `4000`.
- Fields documented "absent" are omitted, not `null`, unless stated.

## 1. Capabilities

`session.info` (the event, and the `info` object of `session.create` /
`session.resume` / `session.activate` results) carries two new fields:

```json
{
  "coordinator_mode": false,
  "capabilities": [
    "threads.roster", "threads.steer", "threads.messages",
    "threads.continue", "threads.approvals", "coordinator_mode"
  ]
}
```

`capabilities` is a list of strings. A gateway without this change sends
neither field. Both are present in the full `session.info` and in the lazy
`info` (`"lazy": true`) that `session.create` / `session.resume` return before
the agent is built. Two reduced `info` shapes omit them: the `info` of
`session.activate`, and the `session.info` event sent after a working-directory
change on a session whose agent is not built yet. So treat a missing
`capabilities` field as "no news" once a connection has advertised the keys, and
as "unsupported" only if no `session.info` on the connection ever carried it.

Each key means these calls/shapes exist:

| Key | Served by |
|---|---|
| `threads.roster` | `subagent.list`, `subagent.tail`, `delegation.status`, `delegation.pause`, the `subagent.*` progress events |
| `threads.steer` | `subagent.steer`, `subagent.interrupt` |
| `threads.messages` | `message_parent` (child tool), `subagent.reply`, `subagent.message` + `subagent.question` events, the `async_delegation_message` turn annotation, `pending_question` on roster rows |
| `threads.continue` | `subagent.continue`, `queued` in `subagent.list`, `subagent_id` in completion annotations |
| `threads.approvals` | `subagent_id` + `goal` on `approval` server requests raised for a thread |
| `coordinator_mode` | `session.mode.set`, `session.info.coordinator_mode` |

Note on naming: in this gateway the per-chat roster is `subagent.list`
(`{session_id}` → live threads of that chat). `subagent.roster` is an app-wide
count per stored conversation (`{sessions: [{session_id, subagents, background}]}`)
and takes no `session_id`.

## 2. Coordinator mode

### `session.mode.set`

```json
{"method": "session.mode.set", "params": {"session_id": "<live id>", "coordinator": true}}
```

Result:

```json
{"coordinator": true, "pending": false}
```

- `coordinator` (bool, required): the new state. Anything but a JSON boolean
  answers `4000`.
- `pending` is `true` when the chat has no stored row yet (a draft chat that has
  not sent its first prompt); the flag is held in memory and written when the
  first turn creates the row. The mode is already in effect either way.
- Unknown `session_id` answers `4001`.
- A `session.info` event for the session follows, with `coordinator_mode` set
  (not for a chat whose agent has not been built yet; read the RPC result there).
- Idempotent: sending the current value changes nothing (no prompt-cache cost).
  The app may send it on every session bind.

While set, for that session only:

- The coordinator instruction block (`agent/prompt_builder.py::COORDINATOR_MODE_GUIDANCE`)
  is appended to the main agent's system prompt at API-call time. It tells the
  agent to answer the user directly, send multi-step or long-running work to
  background threads with `delegate_task`, and keep the user informed of what
  each thread is doing. User prompt text is never changed.
- The main agent's `delegate_task` runs in the background (see Deviations: this
  is already the gateway default for this surface).

Clearing it removes the block from later turns. It does not stop, interrupt or
re-route threads that are already running; they still report back.

Persistence: stored as `coordinator_mode: true` in the conversation's
`sessions.model_config`, carried across context compression, and read back when
the conversation is resumed (`session.resume` → `info.coordinator_mode`).

## 3. Thread roster additions

### `subagent.list`

`{"method": "subagent.list", "params": {"session_id": "<live id>"}}` result:

```json
{
  "subagents": [
    {
      "subagent_id": "sa-0-1a2b3c4d",
      "parent_id": null,
      "depth": 0,
      "goal": "Check the licences",
      "delegation_id": "deleg_9f8e7d6c",
      "model": "anthropic/claude-sonnet-4.6",
      "started_at": 1767000000.0,
      "status": "running",
      "tool_count": 7,
      "last_tool": "terminal",
      "accepting_steer": true,
      "child_session_id": "20261111_101500_abc123",
      "pending_question": {
        "question_id": "q_0a1b2c3d4e",
        "text": "Drop the two AGPL packages?",
        "options": ["Drop", "Flag for legal"],
        "asked_at": 1767000100.0,
        "expires_at": 1767001000.0
      }
    }
  ],
  "delegations": [],
  "queued": [
    {"subagent_id": "sa-1-5e6f7a8b", "goal": "Draft the release notes", "queued_at": 1767000200.0}
  ]
}
```

New fields:

- `subagents[].pending_question`: always present; `null` unless the thread is
  waiting for an answer — the "needs you" state. `options` is absent when the
  thread gave none. Times are epoch seconds.
- `queued`: this chat's `subagent.continue` follow-ups that are waiting for a
  free background slot, oldest first. An entry leaves the list when the thread
  starts (it then appears in `subagents`) or is dropped.

`delegation.status` rows (`active[]`) carry the same `pending_question` object
(there the key is absent until the thread first asks, then `null` when closed).

## 4. Thread messages (`message_parent`)

### The child tool

First-level threads get one extra tool (toolset `thread_messaging`; the main
agent and deeper subagents never have it):

```
message_parent(kind: "update" | "question", text: string, options?: string[])
```

- `kind: "update"` returns at once: `{"status": "sent", "message_id": "dmsg_…"}`,
  or `{"status": "held", "held_updates": <n>, "note": "…"}` when the thread's
  rate limit is spent (see below).
- `kind: "question"` blocks the thread until answered or the wait limit passes:
  `{"answered": true, "answer": "<text>", "answered_by": "user" | "coordinator"}`
  or `{"answered": false, "reason": "no_answer" | "cancelled"}`
  (`cancelled`: the thread was stopped while waiting).
- `text` is cut at 4000 characters; `options` at 6 entries of 200 characters.
- Called by anything that is not a live child of a running background unit:
  `{"error": "message_parent is only available to a running background thread."}`.

### Delivery rules

- Each message is delivered to the parent chat **exactly once**, as a **new
  parent turn**. If the parent is idle the turn starts immediately. If the
  parent is mid-turn the message is held until that turn ends; it is never
  inserted into the reply in progress.
- Messages are durable: each has its own row in the async-delegation ledger
  (`state.db`, `async_delegations`), so a message queued before a gateway
  restart is delivered once after it and never twice.
- Updates are rate-limited per thread: at most one delivery per
  `delegation.update_interval_seconds` (default 60). Updates sent inside the
  window are held and delivered as **one** merged message when the window ends,
  or earlier when the thread asks a question or finishes (always before its
  completion report). Questions are never held.

### What the client sees for one message

1. **The parent turn's user row.** In `session.history` / transcript rows and
   the `inflight_turn` snapshot the row has

   ```json
   {
     "role": "user",
     "display_kind": "async_delegation_message",
     "display_metadata": { ...annotation, below... }
   }
   ```

   Its `content`/`text` is model-facing scaffolding (it starts with
   `[THREAD UPDATE — <subagent_id>]` or
   `[THREAD QUESTION — <subagent_id>, question <question_id>]`); render the card
   from `display_metadata`, not from the text.

2. **A live event** when that turn starts:

   ```json
   {"type": "subagent.message", "session_id": "<live id>", "payload": { ...annotation... }}
   ```

3. A `status.update` event `{"kind": "process", "text": "<display_text>"}` (the
   existing background-notice line), once per message.

The **annotation** (identical object in `display_metadata` and in the
`subagent.message` payload):

```json
{
  "type": "async_delegation_message",
  "display_text": "Thread Update: Check the licences",
  "message_id": "dmsg_4c5d6e7f80",
  "kind": "update",
  "text": "2 of 5 jobs fail on Android API 35",
  "subagent_id": "sa-0-1a2b3c4d",
  "delegation_id": "deleg_9f8e7d6c",
  "goal": "Check the licences",
  "sent_at": 1767000050.0,
  "merged_count": 3
}
```

A question:

```json
{
  "type": "async_delegation_message",
  "display_text": "Thread Question: Check the licences",
  "message_id": "dmsg_91a2b3c4d5",
  "kind": "question",
  "text": "Drop the two AGPL packages?",
  "subagent_id": "sa-0-1a2b3c4d",
  "delegation_id": "deleg_9f8e7d6c",
  "goal": "Check the licences",
  "sent_at": 1767000100.0,
  "question_id": "q_0a1b2c3d4e",
  "options": ["Drop", "Flag for legal"],
  "timeout_seconds": 900.0
}
```

| Field | Type | Notes |
|---|---|---|
| `type` | string | always `"async_delegation_message"` |
| `display_text` | string | one-line title: `Thread Update: <goal>` / `Thread Question: <goal>` |
| `message_id` | string | unique per delivery; de-duplicate cards on it |
| `kind` | `"update"` \| `"question"` | |
| `text` | string | the update or the question; merged updates are joined with a blank line |
| `subagent_id` | string | the thread |
| `delegation_id` | string | the thread's background unit (matches `subagents[].delegation_id` for a single-unit call) |
| `goal` | string | the thread's task |
| `sent_at` | number | epoch seconds |
| `merged_count` | int | updates only; present only when more than one update was merged |
| `question_id` | string | questions only |
| `options` | string[] | questions only; absent when the thread gave none |
| `timeout_seconds` | number | questions only: the thread's wait limit from `sent_at` |

### `subagent.question` event (question lifecycle)

Emitted to the session that owns the thread, independent of turn delivery:

```json
{"type": "subagent.question", "session_id": "<live id>", "payload": {
  "subagent_id": "sa-0-1a2b3c4d", "question_id": "q_0a1b2c3d4e", "status": "pending",
  "goal": "Check the licences", "text": "Drop the two AGPL packages?",
  "options": ["Drop", "Flag for legal"], "asked_at": 1767000100.0, "expires_at": 1767001000.0}}
```

then exactly one of:

```json
{"subagent_id": "sa-0-1a2b3c4d", "question_id": "q_0a1b2c3d4e", "status": "answered",
 "goal": "Check the licences", "answered_by": "user", "answer": "Flag them for legal"}
{"subagent_id": "sa-0-1a2b3c4d", "question_id": "q_0a1b2c3d4e", "status": "expired", "goal": "Check the licences"}
{"subagent_id": "sa-0-1a2b3c4d", "question_id": "q_0a1b2c3d4e", "status": "cancelled", "goal": "Check the licences"}
```

- `status`: `pending` | `answered` | `expired` | `cancelled`.
- `pending` arrives the moment the thread asks — before its parent turn when
  the parent is busy — so the roster can show "needs you" at once.
- `expired`: nobody answered within `delegation.question_timeout`; the thread
  resumed with `{"answered": false}`. `cancelled`: the thread was stopped.
- `answered_by` / `answer` only on `answered`; `text` / `options` / `asked_at` /
  `expires_at` only on `pending`.
- These events are live-only (not replayed after a gateway restart). After a
  reconnect, `subagent.list` → `pending_question` is the source of truth: a
  question card whose thread no longer has that `question_id` pending is closed.

### `subagent.reply`

```json
{"method": "subagent.reply", "params": {
  "session_id": "<live id>", "subagent_id": "sa-0-1a2b3c4d",
  "question_id": "q_0a1b2c3d4e", "text": "Flag them for legal", "answered_by": "user"}}
```

Result:

```json
{"accepted": true, "subagent_id": "sa-0-1a2b3c4d", "question_id": "q_0a1b2c3d4e", "answered_by": "user"}
```

- `answered_by`: `"user"` (default when omitted) or `"coordinator"`.
- The first reply wins and resumes that thread only. Errors:

| Code | When |
|---|---|
| `4000` | `subagent_id` / `question_id` missing, or `answered_by` not `user`/`coordinator` |
| `4001` | session not found, or this socket is not attached to it |
| `4002` | `text` empty |
| `4004` | no running thread with that id in this chat |
| `4090` | the question is closed. `message` says how: `Question <id> is closed: it was already answered by the user.` / `…by the coordinator.` / `…its wait limit passed.` / `…the thread stopped waiting.`, or `No open question <id> for thread <subagent_id>.` |

- Authority is the conversation (the `subagent.list` rule), not the exact live
  session generation, so a reply still works after the app reconnected.

**"Let coordinator decide".** The app sends an ordinary `prompt.submit` asking
the coordinator to answer (for example: `Answer thread sa-0-1a2b3c4d's question
yourself.`). The coordinator answers with its own tool call,
`delegate_task(action="reply", subagent_id="sa-0-1a2b3c4d", message="<answer>")`,
which resolves the thread's open question with `answered_by: "coordinator"`. The
app learns the outcome from the `subagent.question` `answered` event (or the
roster). The app does not call `subagent.reply` for this path. The question's
parent turn already tells the coordinator not to answer unless the user asks it
to decide.

## 5. Completion reports

A finished thread still reports through the existing `async_delegation`
completion turn. Its user row has `display_kind: "async_delegation_complete"`
and `display_metadata`:

```json
{
  "display_text": "Subagent Task Completed: Check the licences",
  "delegation_id": "deleg_9f8e7d6c",
  "task_count": 1,
  "completed_count": 1,
  "failed_count": 0,
  "duration_seconds": 184.2,
  "threads": [
    {
      "subagent_id": "sa-0-1a2b3c4d",
      "task_index": 0,
      "goal": "Check the licences",
      "status": "completed",
      "tool_count": 12,
      "duration_seconds": 184.2
    }
  ]
}
```

New: `threads`, one entry per subagent that ran in the unit (a batch that
reports as one message has several).

| Field | Type | Notes |
|---|---|---|
| `subagent_id` | string | pass to `subagent.continue` |
| `task_index` | int | position in the `delegate_task` call |
| `goal` | string | that task's goal |
| `status` | string | `completed` \| `failed` \| `interrupted` \| `error` \| `timeout` (the child result's status) |
| `tool_count` | int | tool calls the thread made; for a continued thread, over its whole history |
| `duration_seconds` | number | wall time of that thread |

`threads` is absent on a completion that carries no per-thread result (a unit
that crashed or stalled before any child finished, a replay of a completion
recorded before this change, a `/review` or cron background run). Top-level
`duration_seconds` is the whole unit's; it is absent when unknown. There is no
`subagent.message`-style live event for completions; the existing
`subagent.complete` event (payload `subagent_id`, `status`, `duration_seconds`,
`tool_count`, `summary`, …) still fires when each thread ends.

## 6. Continuing a finished thread

### `subagent.continue`

```json
{"method": "subagent.continue", "params": {
  "session_id": "<live id>", "subagent_id": "sa-0-1a2b3c4d", "text": "Make the summary shorter"}}
```

Result — started:

```json
{"status": "dispatched", "subagent_id": "sa-0-1a2b3c4d",
 "delegation_id": "deleg_5b6c7d8e", "child_session_id": "20261111_104500_def456"}
```

Result — every background slot is in use (`position` is its 1-based place in line):

```json
{"status": "queued", "subagent_id": "sa-0-1a2b3c4d", "position": 1}
```

Result — refused:

```json
{"status": "rejected", "subagent_id": "sa-0-1a2b3c4d",
 "error": "This thread is still running: steer it instead of continuing it."}
```

- `status`: `dispatched` | `queued` | `rejected`. `status` and `subagent_id` are
  always present; `delegation_id` + `child_session_id` only on `dispatched`
  (`child_session_id` may be `null`), `position` only on `queued`, `error` only
  on `rejected`.
- `rejected` reasons (`error` text): the thread is still running; no stored
  history for that id in this chat; a follow-up for it is already queued;
  delegation spawning is paused; the delegation backend refused the route.
- JSON-RPC errors: `4000` (`subagent_id` missing), `4002` (`text` empty), `4001`
  (session not found / not attached / has no agent yet).
- The RPC builds an agent, so its response can take a second or two.

Behaviour:

- The gateway finds the thread's newest stored transcript segment, builds a new
  child from that history, and runs it with `text` as the next user message. The
  thread keeps its `subagent_id`; it gets a new `delegation_id` and a new
  `child_session_id` (the new segment stores the whole thread, so it can be
  continued again).
- It is a normal background unit: it uses one slot of
  `delegation.max_concurrent_children`, emits the usual `subagent.spawn_requested`
  / `subagent.start` / `subagent.tool` / `subagent.complete` events, appears in
  `subagent.list`, can be steered and stopped, can call `message_parent`, and
  reports with a completion turn as in section 5.
- `queued`: the follow-up waits in a first-in-first-out line and starts by itself
  when a background unit finishes. Until then it is listed under
  `subagent.list` → `queued`. The line is in memory: a gateway restart drops it.
  A queued follow-up whose chat was closed is dropped when its turn comes.

## 7. Thread approvals

A thread's dangerous-command approval is raised on its **parent session's**
existing server→client request channel — the same `approval` request the main
agent uses, answered the same way — with two extra params:

```json
{"jsonrpc": "2.0", "id": "srq-1a2b3c4d5e6f", "method": "approval", "params": {
  "session_id": "<live id>",
  "request_id": "f3a9…",
  "command": "rm -rf build",
  "description": "recursive delete",
  "choices": ["once", "session", "always", "deny"],
  "allow_permanent": true,
  "allow_session": true,
  "pattern_key": "…", "pattern_keys": ["…"],
  "subagent_id": "sa-0-1a2b3c4d",
  "goal": "Check the licences"
}}
```

- `subagent_id` and `goal` are present only when a thread needs the approval;
  the main agent's own approvals are unchanged. `goal` is cut at 200 characters.
- The client answers as today: a response frame with the same `id`,
  `{"result": {"choice": "once" | "session" | "always" | "deny"}}`. The answer
  resolves that request only and unblocks the thread that asked.
- `open_requests` (from `session.resume` / `session.events.since`) replays an
  unanswered thread approval with the same params.

Config `delegation.subagent_approval`:

- `ask` (default): as above whenever a client attached to the parent session
  answers server requests (`client.capabilities {server_requests: true}`). With
  no such client, nobody is asked and `delegation.subagent_auto_approve` decides:
  `false` (default) denies, `true` approves once.
- `auto`: never asks a person; `delegation.subagent_auto_approve` decides.

## 8. Config keys

All under `delegation:` in `config.yaml`. The new keys are optional (their
defaults live in code and are listed, commented, in `cli-config.yaml.example`):

| Key | Default | Meaning |
|---|---|---|
| `subagent_approval` | `"ask"` | `ask` \| `auto`, section 7 |
| `question_timeout` | `900` | seconds a thread waits for an answer to a question (minimum 1) |
| `update_interval_seconds` | `60` | least seconds between two update deliveries of one thread; `0` = no limit |
| `subagent_auto_approve` | `false` | existing key; now also the no-client / `auto` decision for thread approvals |
| `max_concurrent_children` | `10` | existing key; the background slot count continued threads share |

## 9. Implementation map

| Piece | Where |
|---|---|
| `message_parent` tool | `tools/message_parent_tool.py`, toolset `thread_messaging` in `toolsets.py` |
| Message events, rate limit, questions | `tools/async_delegation_messages.py` |
| Continue | `tools/delegate_tool_continue.py` |
| RPCs, coordinator flag, capability keys | `tui_gateway/methods_threads.py` |
| Turn delivery + `subagent.message` | `tui_gateway/session_notifications.py::_notif_dispatch_event` |
| Approval attribution | `tools/delegate_tool_config.py::subagent_approval_scope`, `tui_gateway/server.py::_emit_approval_request` |
| Coordinator prompt block | `agent/prompt_builder.py::COORDINATOR_MODE_GUIDANCE` |
| Typed contracts | `tui_gateway/contracts/` (`sessions.py`, `events.py`, `profiles_vault_complete_foreign_subagents.py`, `server_requests.py`, `common.py`); generated `apps/shared/src/gateway-contract.*` |
| Tests | `tests/tools/test_thread_messages.py`, `tests/tools/test_delegate_continue.py`, `tests/tui_gateway/test_coordinator_threads.py`, `tests/tui_gateway/contracts/test_coordinator_threads.py` |

## Deviations

Differences from `design.md` in the app repo, and limits to know before
building on this.

1. **Legacy JSON-RPC surface only.** The RPCs, the capability keys and the
   coordinator flag are implemented in `tui_gateway/` — what a standalone
   `hermes dashboard` / `hermes serve` serves at `/api/ws`. A socket served by
   the gateway's session authority (`hermes gateway run` hosting the listener;
   its `gateway.ready` payload has `session_authority: true`) has its own
   `session.*` / `subagent.*` handlers in `gateway/session_*.py`, which this
   change does not touch: there `session.info` advertises no thread capability,
   `session.mode.set` answers `-32601`, and the app must treat the features as
   absent. Thread messages from children still reach such chats as plain turns.
2. **Queue event type.** D6 names the event `async_delegation_message`. On the
   internal completion queue it is a `type: "async_delegation"` event carrying a
   `thread_message` object, with its own ledger row whose key is the message id.
   Every existing consumer (ownership routing, delivery claim, de-dup, restart
   replay, the CLI and the messaging gateway) keys on that type; a new type
   would have needed a change in each. Clients see the designed name: turn
   `display_kind: "async_delegation_message"` and annotation
   `type: "async_delegation_message"`.
3. **Live event names.** Delivery is announced with `subagent.message`, and the
   question lifecycle with `subagent.question` (not in the design), both in the
   `subagent.*` namespace the app already routes.
4. **`session_id` param.** `subagent.reply` and `subagent.continue` take
   `session_id` in addition to the design's params, like every other
   `subagent.*` RPC.
5. **Background default.** On this surface the main agent's `delegate_task`
   already always runs in the background (`run_agent.py::_dispatch_delegate_task`),
   with or without coordinator mode, so the flag adds no dispatch change. Two
   existing fallbacks still run a call synchronously: no free background slot at
   dispatch time, and finite sessions that cannot receive a later result.
6. **Approvals.** In gateway sessions a child's approval already reached the
   parent session's approval queue before this change (the design assumed
   auto-deny, which is the CLI behaviour). What is new: the `subagent_id` /
   `goal` attribution, the `delegation.subagent_approval` switch, and the
   no-client fallback to `subagent_auto_approve` (previously the request was
   withdrawn, which also denied). CLI sessions are unchanged.
7. **Coordinator's reply path.** "The gateway resolves the reply from that
   turn's tool call" is implemented as a new `delegate_task` control action,
   `action="reply"`; the `delegate_task` schema's `action` enum gained `reply`.
8. **Slot count.** The design text says the background cap defaults to 3; in
   this gateway `delegation.max_concurrent_children` defaults to 10.
9. **Queued follow-ups are not durable** (in memory, lost on restart), and a
   dropped one is only visible as its disappearance from `subagent.list.queued`.
10. **Question events are live-only**; see section 4 for the reconnect rule.
11. **Compute-host workers.** A child running in a compute-host worker process
    has no ledger: its messages are delivered from memory only (no restart replay).
12. **Ledger retention.** Message rows share the `async_delegations` table and
    its retention (50 finished rows, delivered ones pruned first), so a very
    chatty set of threads shortens how long old completion results stay queryable.
13. **Desktop / Ink clients** do not know `display_kind: "async_delegation_message"`
    and show the message turn as an ordinary user bubble with the scaffold text.
