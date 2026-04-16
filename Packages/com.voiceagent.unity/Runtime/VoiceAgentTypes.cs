using System;
using UnityEngine;
using UnityEngine.Events;
using UnityEngine.Serialization;

namespace VoiceAgent.Unity
{
    [Serializable]
    public sealed class VoiceAgentSettings
    {
        public string host = "127.0.0.1";
        [Min(1)] public int panelPort = 8787;
        public string defaultVoice = "en_US";
        public string defaultBackend = "piper";
        public string defaultTtsModel;
        public string defaultKokoroVoice = "af_heart";
        [Min(0.5f)] public float requestTimeoutSeconds = 15f;
    }

    [Serializable]
    public sealed class VoiceAgentSpeechRequest
    {
        [TextArea(2, 6)] public string text;
        public string voice;
        public string backend;
        public string model;
        [Min(0.1f)] public float speed = 1f;
        [Min(0f)] public float volume = 1f;
    }

    public enum VoiceAgentFacePreset
    {
        Neutral,
        Happy,
        Sad,
        VerySad,
        Excited,
    }

    public sealed class VoiceAgentApiResult
    {
        public bool Success { get; set; }
        public int StatusCode { get; set; }
        public string Message { get; set; }
        public string RawBody { get; set; }

        public static VoiceAgentApiResult Ok(string message = "", int statusCode = 200, string rawBody = "")
        {
            return new VoiceAgentApiResult
            {
                Success = true,
                StatusCode = statusCode,
                Message = message ?? string.Empty,
                RawBody = rawBody ?? string.Empty,
            };
        }

        public static VoiceAgentApiResult Fail(string message, int statusCode = 500, string rawBody = "")
        {
            return new VoiceAgentApiResult
            {
                Success = false,
                StatusCode = statusCode,
                Message = message ?? string.Empty,
                RawBody = rawBody ?? string.Empty,
            };
        }
    }

    public sealed class VoiceAgentConnectionHealth
    {
        public bool IsReachable { get; set; }
        public bool HealthEndpointOk { get; set; }
        public bool VoiceOptionsOk { get; set; }
        public bool AsrStatusOk { get; set; }
        public string Summary { get; set; }
    }

    public sealed class VoiceAgentTypedResult<T> where T : class
    {
        public VoiceAgentApiResult ApiResult { get; set; }
        public T Payload { get; set; }
    }

    [Serializable]
    public sealed class VoiceAgentBackendAsrStatus
    {
        public string[] available_modes;
        public string mode;

        public string[] AvailableModes => available_modes ?? Array.Empty<string>();
        public string Mode => mode ?? string.Empty;
    }

    [Serializable]
    public sealed class VoiceAgentAsrStatus
    {
        public string status;
        public string message;
        public string event_type;
        public string mode;
        public string streaming_backend;
        public string current_partial;
        public string stable_partial;
        public string final_transcript;
        public int final_transcript_seq;
        public bool assistant_speaking;
        public bool conversation_dispatch_enabled;
        public bool supports_hotwords;
        public int hotwords_count;
        public string hotword_strategy;
        public bool listening;
        public VoiceAgentBackendAsrStatus server_transcribe;
        public string[] available_modes;

        public string Status => status ?? string.Empty;
        public string Message => message ?? string.Empty;
        public string EventType => event_type ?? string.Empty;
        public string Mode => mode ?? string.Empty;
        public string StreamingBackend => streaming_backend ?? string.Empty;
        public string CurrentPartial => current_partial ?? string.Empty;
        public string StablePartial => stable_partial ?? string.Empty;
        public string FinalTranscript => final_transcript ?? string.Empty;
        public int FinalTranscriptSequence => final_transcript_seq;
        public bool AssistantSpeaking => assistant_speaking;
        public bool ConversationDispatchEnabled => conversation_dispatch_enabled;
        public bool SupportsHotwords => supports_hotwords;
        public int HotwordsCount => hotwords_count;
        public string HotwordStrategy => hotword_strategy ?? string.Empty;
        public bool Listening => listening;
        public VoiceAgentBackendAsrStatus Backend => server_transcribe;
        public string[] AvailableModes => available_modes ?? Array.Empty<string>();
    }

    [Serializable]
    public sealed class VoiceAgentReplyRule
    {
        [FormerlySerializedAs("keyword")]
        public string listenFor;
        [FormerlySerializedAs("response")]
        public string replyWith;
    }

    [Serializable]
    public sealed class VoiceAgentReplyMatchedEvent : UnityEvent<string>
    {
    }
}
