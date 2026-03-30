using System;
using System.Threading.Tasks;
using UnityEngine;

namespace VoiceAgent.Unity
{
    [DisallowMultipleComponent]
    public sealed class VoiceAgentComponent : MonoBehaviour
    {
        [SerializeField] private VoiceAgentSettings settings = new VoiceAgentSettings();

        [Header("TTS")]
        [SerializeField, TextArea(2, 5)] private string speakText = "Hello from VoiceAgent.";
        [SerializeField] private string voice;
        [SerializeField] private string backend = "piper";
        [SerializeField] private string ttsModel;
        [SerializeField] private float speechSpeed = 1f;
        [SerializeField] private float speechVolume = 1f;
        [SerializeField, TextArea(2, 5)] private string kokoroText = "Hello from Kokoro.";
        [SerializeField] private string kokoroVoice = "af_heart";

        [Header("LLM / Runtime")]
        [SerializeField, TextArea(2, 5)] private string llmPrompt = "You are a helpful voice assistant.";
        [SerializeField] private string localModel = "qwen3.5:latest";

        [Header("ASR")]
        [SerializeField] private string asrMode = "manual";
        [SerializeField] private string backendAsrMode = "manual";

        [Header("Vision / Game")]
        [SerializeField, TextArea(2, 5)] private string visionPrompt = "Describe the current camera view.";
        [SerializeField] private string visionModel;
        [SerializeField] private string gameName = "demo";

        [Header("Face")]
        [SerializeField] private VoiceAgentFacePreset facePreset = VoiceAgentFacePreset.Neutral;
        [SerializeField, TextArea(2, 4)] private string faceCustomValue = "^-^";
        [SerializeField] private float faceSeconds = 3f;

        [Header("LED")]
        [SerializeField] private Color ledColor = Color.cyan;
        [SerializeField, Range(0f, 1f)] private float ledBrightness = 0.8f;
        [SerializeField] private float ledPeriod = 2f;
        [SerializeField] private float ledDuration;

        [Header("Last Result")]
        [SerializeField] private bool lastSuccess;
        [SerializeField] private int lastStatusCode;
        [SerializeField, TextArea(2, 4)] private string lastMessage;
        [SerializeField, TextArea(3, 10)] private string lastRawBody;

        private VoiceAgentClient client;

        public VoiceAgentSettings Settings => settings ?? (settings = new VoiceAgentSettings());
        public VoiceAgentClient Client => client ?? (client = new VoiceAgentClient(Settings));
        public bool LastSuccess => lastSuccess;
        public int LastStatusCode => lastStatusCode;
        public string LastMessage => lastMessage;
        public string LastRawBody => lastRawBody;

        private void OnDestroy()
        {
            DisposeClient();
        }

        public void RecreateClient()
        {
            DisposeClient();
            client = new VoiceAgentClient(Settings);
        }

        public void DisposeClient()
        {
            client?.Dispose();
            client = null;
        }

        public void ClearLastResult()
        {
            lastSuccess = false;
            lastStatusCode = 0;
            lastMessage = string.Empty;
            lastRawBody = string.Empty;
        }

        public VoiceAgentSpeechRequest CreateSpeechRequest()
        {
            return new VoiceAgentSpeechRequest
            {
                text = speakText,
                voice = voice,
                backend = backend,
                model = ttsModel,
                speed = speechSpeed,
                volume = speechVolume,
            };
        }

        public Task<VoiceAgentConnectionHealth> CheckConnectionAsync() => RunConnectionAsync(current => current.CheckConnectionHealthAsync());
        public Task<VoiceAgentApiResult> GetLogsAsync() => RunAsync(current => current.GetLogsAsync());
        public Task<VoiceAgentApiResult> GetTtsOptionsAsync() => RunAsync(current => current.GetTtsOptionsAsync());
        public Task<VoiceAgentApiResult> GetKokoroOptionsAsync() => RunAsync(current => current.GetKokoroOptionsAsync());
        public Task<VoiceAgentApiResult> SpeakAsync() => RunAsync(current => current.SpeakAsync(CreateSpeechRequest()));
        public Task<VoiceAgentApiResult> SetVoiceAsync() => RunAsync(current => current.SetVoiceAsync(voice));
        public Task<VoiceAgentApiResult> SetTtsModelAsync() => RunAsync(current => current.SetTtsModelAsync(ttsModel));
        public Task<VoiceAgentApiResult> SetTtsBackendAsync() => RunAsync(current => current.SetTtsBackendAsync(backend));
        public Task<VoiceAgentApiResult> SetKokoroVoiceAsync() => RunAsync(current => current.SetKokoroVoiceAsync(kokoroVoice));
        public Task<VoiceAgentApiResult> KokoroSpeakAsync() => RunAsync(current => current.KokoroSpeakAsync(kokoroText, kokoroVoice));
        public Task<VoiceAgentApiResult> GetLlmPromptAsync() => RunAsync(current => current.GetLlmPromptAsync());
        public Task<VoiceAgentApiResult> SetLlmPromptAsync() => RunAsync(current => current.SetLlmPromptAsync(llmPrompt));
        public Task<VoiceAgentApiResult> ResetLlmPromptAsync() => RunAsync(current => current.ResetLlmPromptAsync());
        public Task<VoiceAgentApiResult> GetRuntimeConfigAsync() => RunAsync(current => current.GetRuntimeConfigAsync());
        public Task<VoiceAgentApiResult> SetLocalModelAsync() => RunAsync(current => current.SetLocalModelAsync(localModel));
        public Task<VoiceAgentApiResult> GetAsrStatusAsync() => RunAsync(current => current.GetAsrStatusAsync());
        public Task<VoiceAgentApiResult> SetAsrModeAsync() => RunAsync(current => current.SetAsrModeAsync(asrMode));
        public Task<VoiceAgentApiResult> SetBackendAsrModeAsync() => RunAsync(current => current.SetBackendAsrModeAsync(backendAsrMode));
        public Task<VoiceAgentApiResult> StartListeningAsync() => RunAsync(current => current.StartListeningAsync());
        public Task<VoiceAgentApiResult> PauseListeningAsync() => RunAsync(current => current.PauseListeningAsync());
        public Task<VoiceAgentApiResult> DescribeCurrentCameraAsync() => RunAsync(current => current.DescribeCurrentCameraAsync(visionPrompt, visionModel));
        public Task<VoiceAgentApiResult> LaunchGameAsync() => RunAsync(current => current.LaunchGameAsync(gameName));
        public Task<VoiceAgentApiResult> ExitGameAsync() => RunAsync(current => current.ExitGameAsync());
        public Task<VoiceAgentApiResult> FacePresetAsync() => RunAsync(current => current.FacePresetAsync(facePreset, faceSeconds));
        public Task<VoiceAgentApiResult> FaceCustomAsync() => RunAsync(current => current.FaceCustomAsync(faceCustomValue, faceSeconds));
        public Task<VoiceAgentApiResult> LedBreatheAsync() => RunAsync(current => current.LedBreatheAsync(ledColor, ledBrightness, ledPeriod, ledDuration));
        public Task<VoiceAgentApiResult> LedSolidAsync() => RunAsync(current => current.LedSolidAsync(ledColor, ledBrightness, ledDuration));
        public Task<VoiceAgentApiResult> LedRandomAsync() => RunAsync(current => current.LedRandomAsync(ledDuration));
        public Task<VoiceAgentApiResult> LedOffAsync() => RunAsync(current => current.LedOffAsync());
        public Task<VoiceAgentApiResult> FlowerOpenAsync() => RunAsync(current => current.FlowerOpenAsync());
        public Task<VoiceAgentApiResult> FlowerCloseAsync() => RunAsync(current => current.FlowerCloseAsync());
        public Task<VoiceAgentApiResult> FlowerStopAsync() => RunAsync(current => current.FlowerStopAsync());
        public Task<VoiceAgentApiResult> FlowerOpenSlowAsync() => RunAsync(current => current.FlowerOpenSlowAsync());
        public Task<VoiceAgentApiResult> FlowerCloseSlowAsync() => RunAsync(current => current.FlowerCloseSlowAsync());

        private async Task<VoiceAgentApiResult> RunAsync(Func<VoiceAgentClient, Task<VoiceAgentApiResult>> action)
        {
            var result = await action(Client).ConfigureAwait(false);
            ApplyResult(result);
            return result;
        }

        private async Task<VoiceAgentConnectionHealth> RunConnectionAsync(Func<VoiceAgentClient, Task<VoiceAgentConnectionHealth>> action)
        {
            var result = await action(Client).ConfigureAwait(false);
            lastSuccess = result != null && result.IsReachable;
            lastStatusCode = result != null && result.IsReachable ? 200 : 0;
            lastMessage = result != null ? result.Summary ?? string.Empty : string.Empty;
            lastRawBody = string.Empty;
            Debug.Log(lastMessage, this);
            return result;
        }

        private void ApplyResult(VoiceAgentApiResult result)
        {
            lastSuccess = result != null && result.Success;
            lastStatusCode = result != null ? result.StatusCode : 0;
            lastMessage = result != null ? result.Message ?? string.Empty : string.Empty;
            lastRawBody = result != null ? result.RawBody ?? string.Empty : string.Empty;

            if (result == null)
            {
                Debug.LogWarning("VoiceAgent returned no result.", this);
                return;
            }

            if (result.Success)
            {
                Debug.Log($"{result.StatusCode}: {result.Message}", this);
            }
            else
            {
                Debug.LogWarning($"{result.StatusCode}: {result.Message}", this);
            }
        }
    }
}
