from __future__ import annotations

try:
    from .api_routes import (
        _AnonymousSessionStore,
        _ReplyChunkAccumulator,
        _UnifiedConversationRuntime,
        _dispatch_command_intent,
        _get_unified_conversation_runtime,
        _is_live_captions_request,
        _json_line,
        _normalize_identity_resolution,
        _normalize_request_source,
        _normalize_transcript_confidence,
        _should_clarify_uncertain_turn,
        _stream_unified_conversation_events,
        _uncertain_turn_reply,
    )
except Exception:
    from api_routes import (
        _AnonymousSessionStore,
        _ReplyChunkAccumulator,
        _UnifiedConversationRuntime,
        _dispatch_command_intent,
        _get_unified_conversation_runtime,
        _is_live_captions_request,
        _json_line,
        _normalize_identity_resolution,
        _normalize_request_source,
        _normalize_transcript_confidence,
        _should_clarify_uncertain_turn,
        _stream_unified_conversation_events,
        _uncertain_turn_reply,
    )
