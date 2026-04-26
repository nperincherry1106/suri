"""Long-term memory: facts about Namrita that persist across conversations.

The agent's system prompt always includes the current set of facts (see
agent._system_prompt), so there's no `recall` tool — facts are always in
context. We only expose write tools."""

from app import db


def remember_fact(key: str, value: str):
    key = key.strip().lower().replace(" ", "_")
    value = value.strip()
    if not key or not value:
        return {"ok": False, "error": "key and value must both be non-empty"}
    db.set_fact(key, value)
    return {"ok": True, "key": key, "value": value}


def forget_fact(key: str):
    key = key.strip().lower().replace(" ", "_")
    db.forget_fact(key)
    return {"ok": True, "key": key}
