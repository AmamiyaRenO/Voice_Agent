from __future__ import annotations

try:
    from .api_routes import *  # noqa: F401,F403
    from .api_routes import (
        _ANONYMOUS_SESSION_STORE,
        _AnonymousSessionStore,
        _UnifiedConversationRuntime,
        _environment,
        _generate_structured_spoken_reply,
        _get_unified_conversation_runtime,
        _normalize_final_reply_text,
        _should_clarify_uncertain_turn,
        _structured_template_reply,
        _validate_structured_reply,
        create_app,
        logger,
    )
except Exception:
    from api_routes import *  # noqa: F401,F403
    from api_routes import (
        _ANONYMOUS_SESSION_STORE,
        _AnonymousSessionStore,
        _UnifiedConversationRuntime,
        _environment,
        _generate_structured_spoken_reply,
        _get_unified_conversation_runtime,
        _normalize_final_reply_text,
        _should_clarify_uncertain_turn,
        _structured_template_reply,
        _validate_structured_reply,
        create_app,
        logger,
    )

app = create_app()


async def _spoken_reply_from_payload(
    user_text: str,
    payload: dict,
    *,
    all_game_names: list[str] | None = None,
) -> str:
    fallback_text = _normalize_final_reply_text(
        _structured_template_reply(payload, user_text) or str(payload.get("text") or "")
    )
    if fallback_text:
        payload = dict(payload)
        payload["text"] = fallback_text
    try:
        rendered = await _generate_structured_spoken_reply(user_text, payload)
    except (RuntimeError, Exception) as exc:
        logger.info("structured render fallback: %s", exc)
        return fallback_text
    valid, reason = _validate_structured_reply(rendered, payload, all_game_names=all_game_names)
    if not valid:
        logger.info("structured render validation fallback: %s", reason)
        return fallback_text
    return rendered


if __name__ == "__main__":
    import uvicorn

    port = int(_environment("PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
