using System;
using UnityEngine;

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
}
