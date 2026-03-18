from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from pydantic import BaseModel, Field


class RespondRequest(BaseModel):
    text: str = Field(..., min_length=1, description="User transcript to send to the coach agent")
    system: Optional[str] = Field(default=None, description="Optional system prompt override for the LLM.")
    memory_context: Optional[str] = Field(
        default=None,
        description="Optional short per-user memory summary injected into the prompt.",
    )
    user_id: Optional[str] = Field(
        default=None,
        description="Optional stable user identifier for logging/debugging.",
    )
    dialog_context: Optional[str] = Field(
        default=None,
        description="Optional short-term multi-turn dialogue context (recent turns + summary).",
    )
    dialog_policy: Optional[str] = Field(
        default=None,
        description="Dialogue routing hint: continue_topic, switch_topic, or ask_clarify.",
    )
    current_topic: Optional[str] = Field(
        default=None,
        description="Optional current topic slot from dialogue manager.",
    )
    open_question: Optional[str] = Field(
        default=None,
        description="Optional pending question from previous assistant turn.",
    )
    barge_in: bool = Field(
        default=False,
        description="True when user interrupted ongoing TTS playback (barge-in).",
    )
    interrupted_tts_text: Optional[str] = Field(
        default=None,
        description="Optional interrupted assistant text snippet for context only.",
    )


class RespondResponse(BaseModel):
    text: str
    generation_seconds: Optional[float] = Field(default=None, ge=0.0)


class RespondConfigRequest(BaseModel):
    system_prompt: Optional[str] = Field(default=None, description="Runtime system prompt override.")
    prompt: Optional[str] = Field(default=None, description="Alias of system_prompt.")
    reset: bool = Field(default=False, description="Clear runtime override and use env/default prompt.")


class RespondConfigResponse(BaseModel):
    status: str = "ok"
    system_prompt: str
    runtime_override_active: bool
    source: str


class TranscribeConfigRequest(BaseModel):
    mode: Optional[str] = Field(
        default=None,
        description="ASR mode: whisper-large-v3, moonshine-small, moonshine-medium, or api.",
    )
    reset: bool = Field(default=False, description="Clear runtime override and use env/default mode.")


class TranscribeConfigResponse(BaseModel):
    status: str = "ok"
    mode: str
    source: str
    available_modes: list[str]
    openai_configured: bool
    openai_model: str


class ConversationConfigRequest(BaseModel):
    pipeline_mode: Optional[str] = Field(
        default=None,
        description="Conversation pipeline mode: direct_unified or legacy_mqtt.",
    )
    profile: Optional[str] = Field(
        default=None,
        description="Conversation profile: local or cloud.",
    )
    local_asr_mode: Optional[str] = Field(
        default=None,
        description="Preferred ASR mode when profile=local.",
    )
    cloud_asr_mode: Optional[str] = Field(
        default=None,
        description="Preferred ASR mode when profile=cloud.",
    )
    cloud_response_provider: Optional[str] = Field(
        default=None,
        description="Cloud response provider. Currently only openai is supported.",
    )
    openai_api_key: Optional[str] = Field(
        default=None,
        description="OpenAI API key used for cloud response and API ASR.",
    )
    openai_base_url: Optional[str] = Field(
        default=None,
        description="Optional OpenAI-compatible base URL.",
    )
    openai_transcribe_model: Optional[str] = Field(
        default=None,
        description="OpenAI ASR model used when api transcription is selected.",
    )
    openai_transcribe_prompt: Optional[str] = Field(
        default=None,
        description="Optional OpenAI ASR prompt override.",
    )
    openai_response_model: Optional[str] = Field(
        default=None,
        description="OpenAI chat model used when profile=cloud.",
    )
    local_response_model: Optional[str] = Field(
        default=None,
        description="Ollama model used when profile=local.",
    )
    reset: bool = Field(default=False, description="Reset runtime overrides back to env defaults.")


class ConversationConfigResponse(BaseModel):
    status: str = "ok"
    pipeline_mode: str
    profile: str
    local_asr_mode: str
    cloud_asr_mode: str
    preferred_asr_mode: str
    cloud_response_provider: str
    openai_response_model: str
    local_response_model: str
    openai_configured: bool
    cloud_ready: bool
    effective_response_provider: str


class ConversationTurnRequest(BaseModel):
    text: str = Field(..., min_length=1, description="User transcript to route through the unified conversation pipeline.")
    corr_id: Optional[str] = Field(default=None, description="Stable corr_id from Unity for de-dupe and cancelation.")
    user_id: Optional[str] = Field(default=None, description="Optional stable user identifier.")
    identity_resolution: str = Field(
        default="auto",
        description="Identity policy: auto resolves via speaker tags/profile, none keeps the turn anonymous.",
    )
    source: Optional[str] = Field(default=None, description="Source label for diagnostics.")
    avg_logprob: Optional[float] = Field(default=None)
    rms: Optional[float] = Field(default=None)
    max_amplitude: Optional[float] = Field(default=None)
    speaker_index: Optional[int] = Field(default=None)
    speaker_id: Optional[int] = Field(default=None)
    barge_in: bool = Field(default=False)
    interrupted_tts_text: Optional[str] = Field(default=None)
    interrupted_tts_corr_id: Optional[str] = Field(default=None)
    transcript_source: Optional[str] = Field(default=None)
    transcript_confidence: Optional[str] = Field(default=None)


class OllamaError(RuntimeError):
    pass


class OpenAIResponseError(RuntimeError):
    pass


@dataclass
class CapabilityRouteDecision:
    label: str = "general_chat"
    confidence: float = 0.0
    clarification_text: str = ""
    explicit_reference: bool = False
    routed_text: str = ""
    clarification_kind: str = ""
    merged_from_clarification: bool = False
    structured_payload: Optional[dict] = None
    probe_telemetry: Optional[dict] = None
    fallback_reason: str = ""
