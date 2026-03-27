namespace RobotVoice
{
    internal static class VoiceAgentDefaults
    {
        public const string LocalHost = "127.0.0.1";
        public const int MqttPort = 1883;

        public const string IntentTopic = "robot/intent";
        public const string VoiceTextTopic = "robot/voice/text";
        public const string CaptionTopic = "robot/captions/text";
        public const string DialogAnswerTopic = "robot/dialog/answer";

        public const string AsrBaseUrl = "http://127.0.0.1:8000";
        public const string AsrTranscribeUrl = AsrBaseUrl + "/transcribe";
        public const string ConversationConfigUrl = AsrBaseUrl + "/conversation/config";
        public const string ConversationTurnStreamUrl = AsrBaseUrl + "/conversation/turn/stream";

        public const string PiperBaseUrl = "http://127.0.0.1:5005";
        public const string PiperSpeakUrl = PiperBaseUrl + "/speak";
        public const string PiperSpeakStreamUrl = PiperBaseUrl + "/speak_stream";
        public const string KokoroSpeakUrl = "http://127.0.0.1:5007/speak";

        public const string OllamaBaseUrl = "http://127.0.0.1:11434";
        public const string TelemetryDashboardUrl = "http://127.0.0.1:8101/dashboard";
        public const string DefaultVisionModel = "qwen3.5:0.8b";
    }
}

