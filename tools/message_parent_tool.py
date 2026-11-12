"""``message_parent``: a background thread's channel to the chat that started it.

Child-only: the ``thread_messaging`` toolset is added to first-level delegated children
(``delegate_tool._build_child_agent``) and is on no platform default. The handler still refuses a caller
that is not a live child of a running background unit. Mechanics: ``tools/async_delegation_messages.py``.
"""

import json

from tools.registry import registry

MESSAGE_PARENT_SCHEMA = {
    "name": "message_parent",
    "description": (
        "Send a message to the chat that started you, without finishing your task. "
        "kind='update': a short interim finding or status the user would want now; returns at once and you keep "
        "working (updates are rate-limited: extra ones are merged into your next delivery, so send few). "
        "kind='question': you are blocked on a decision only your parent or the user can make; this call waits "
        "for the answer and returns {\"answered\": true, \"answer\": ...}, or {\"answered\": false} when nobody "
        "answered in time — then proceed on your best judgement and say so in your final summary. "
        "Your final result is still your last reply; never send it through this tool."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["update", "question"], "description": "update or question."},
            "text": {"type": "string", "description": "The update or the question, self-contained and brief."},
            "options": {
                "type": "array", "items": {"type": "string"},
                "description": "question only: up to 6 short suggested answers the user can pick from.",
            },
        },
        "required": ["kind", "text"],
    },
}


def _handle(args, task_id=None):
    from tools.async_delegation_messages import message_parent
    return json.dumps(message_parent(args.get("kind"), args.get("text"), args.get("options"), task_id=task_id),
                      ensure_ascii=False)


registry.register(name="message_parent", toolset="thread_messaging", schema=MESSAGE_PARENT_SCHEMA,
                  handler=_handle, emoji="💬")
