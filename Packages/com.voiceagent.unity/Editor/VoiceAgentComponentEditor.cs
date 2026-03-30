using System;
using System.Collections.Generic;
using System.Threading.Tasks;
using UnityEditor;
using UnityEngine;

namespace VoiceAgent.Unity.Editor
{
    [CustomEditor(typeof(VoiceAgentComponent))]
    public sealed class VoiceAgentComponentEditor : UnityEditor.Editor
    {
        private bool isRunning;
        private bool isRefreshingOptions;
        private bool didAttemptAutoRefresh;
        private string optionStatusMessage = "Options not loaded yet.";
        private InspectorOptions inspectorOptions = new InspectorOptions();

        public override void OnInspectorGUI()
        {
            serializedObject.Update();
            var component = (VoiceAgentComponent)target;

            if (!didAttemptAutoRefresh && !isRefreshingOptions)
            {
                didAttemptAutoRefresh = true;
                RefreshInspectorOptions(component);
            }

            DrawSettingsSection(component);
            DrawTtsSection(component);
            DrawRuntimeSection(component);
            DrawAsrSection(component);
            DrawVisionGameSection(component);
            DrawFaceSection(component);
            DrawLedSection(component);
            DrawFlowerSection(component);
            DrawResultSection(component);

            serializedObject.ApplyModifiedProperties();
        }

        private void DrawSettingsSection(VoiceAgentComponent component)
        {
            EditorGUILayout.LabelField("Connection", EditorStyles.boldLabel);
            DrawSettingsFields();

            using (new EditorGUI.DisabledScope(isRunning))
            {
                using (new EditorGUILayout.HorizontalScope())
                {
                    if (GUILayout.Button("Recreate Client"))
                    {
                        ApplyChanges();
                        component.RecreateClient();
                        EditorUtility.SetDirty(component);
                    }

                    if (GUILayout.Button("Check Connection"))
                    {
                        RunTask(component, () => component.CheckConnectionAsync());
                    }

                    using (new EditorGUI.DisabledScope(isRefreshingOptions))
                    {
                        if (GUILayout.Button(isRefreshingOptions ? "Refreshing..." : "Refresh Options"))
                        {
                            RefreshInspectorOptions(component);
                        }
                    }
                }
            }

            if (!string.IsNullOrWhiteSpace(optionStatusMessage))
            {
                EditorGUILayout.HelpBox(optionStatusMessage, MessageType.None);
            }
        }

        private void DrawTtsSection(VoiceAgentComponent component)
        {
            EditorGUILayout.Space();
            EditorGUILayout.LabelField("TTS", EditorStyles.boldLabel);
            DrawProperty("speakText");
            var backendProperty = serializedObject.FindProperty("backend");
            var selectedBackend = backendProperty != null ? backendProperty.stringValue : string.Empty;
            var primaryVoiceChoices = IsKokoroBackend(selectedBackend) ? inspectorOptions.KokoroVoices : inspectorOptions.PiperVoices;
            using (new EditorGUILayout.HorizontalScope())
            {
                DrawStringChoice("voice", "Voice", primaryVoiceChoices);
                DrawStringChoice("backend", "Backend", inspectorOptions.Backends);
                using (new EditorGUI.DisabledScope(IsKokoroBackend(selectedBackend)))
                {
                    DrawStringChoice("ttsModel", "TTS Model", inspectorOptions.TtsModels);
                }
            }
            using (new EditorGUILayout.HorizontalScope())
            {
                DrawProperty("speechSpeed");
                DrawProperty("speechVolume");
            }
            DrawProperty("kokoroText");
            DrawStringChoice("kokoroVoice", "Kokoro Voice", inspectorOptions.KokoroVoices);

            using (new EditorGUI.DisabledScope(isRunning))
            {
                DrawSection(component,
                    ("Speak", () => component.SpeakAsync()),
                    ("Set Voice", () => component.SetVoiceAsync()),
                    ("Set Backend", () => component.SetTtsBackendAsync()),
                    ("Set TTS Model", () => component.SetTtsModelAsync()),
                    ("Get TTS Options", () => component.GetTtsOptionsAsync()),
                    ("Get Kokoro Options", () => component.GetKokoroOptionsAsync()),
                    ("Set Kokoro Voice", () => component.SetKokoroVoiceAsync()),
                    ("Kokoro Speak", () => component.KokoroSpeakAsync()));
            }
        }

        private void DrawRuntimeSection(VoiceAgentComponent component)
        {
            EditorGUILayout.Space();
            EditorGUILayout.LabelField("Runtime", EditorStyles.boldLabel);
            DrawProperty("llmPrompt");
            DrawStringChoice("localModel", "Local Model", inspectorOptions.LocalModels);

            using (new EditorGUI.DisabledScope(isRunning))
            {
                DrawSection(component,
                    ("Get Logs", () => component.GetLogsAsync()),
                    ("Get Runtime Config", () => component.GetRuntimeConfigAsync()),
                    ("Get LLM Prompt", () => component.GetLlmPromptAsync()),
                    ("Set LLM Prompt", () => component.SetLlmPromptAsync()),
                    ("Reset LLM Prompt", () => component.ResetLlmPromptAsync()),
                    ("Set Local Model", () => component.SetLocalModelAsync()));
            }
        }

        private void DrawAsrSection(VoiceAgentComponent component)
        {
            EditorGUILayout.Space();
            EditorGUILayout.LabelField("ASR", EditorStyles.boldLabel);
            using (new EditorGUILayout.HorizontalScope())
            {
                DrawStringChoice("asrMode", "ASR Mode", inspectorOptions.AsrModes);
                DrawStringChoice("backendAsrMode", "Backend ASR Mode", inspectorOptions.BackendAsrModes);
            }

            using (new EditorGUI.DisabledScope(isRunning))
            {
                DrawSection(component,
                    ("Get ASR Status", () => component.GetAsrStatusAsync()),
                    ("Set ASR Mode", () => component.SetAsrModeAsync()),
                    ("Set Backend ASR Mode", () => component.SetBackendAsrModeAsync()),
                    ("Start Listening", () => component.StartListeningAsync()),
                    ("Pause Listening", () => component.PauseListeningAsync()));
            }
        }

        private void DrawVisionGameSection(VoiceAgentComponent component)
        {
            EditorGUILayout.Space();
            EditorGUILayout.LabelField("Vision / Game", EditorStyles.boldLabel);
            DrawProperty("visionPrompt");
            using (new EditorGUILayout.HorizontalScope())
            {
                DrawProperty("visionModel");
                DrawProperty("gameName");
            }

            using (new EditorGUI.DisabledScope(isRunning))
            {
                DrawSection(component,
                    ("Describe Camera", () => component.DescribeCurrentCameraAsync()),
                    ("Launch Game", () => component.LaunchGameAsync()),
                    ("Exit Game", () => component.ExitGameAsync()));
            }
        }

        private void DrawFaceSection(VoiceAgentComponent component)
        {
            EditorGUILayout.Space();
            EditorGUILayout.LabelField("Face", EditorStyles.boldLabel);
            using (new EditorGUILayout.HorizontalScope())
            {
                DrawProperty("facePreset");
                DrawProperty("faceSeconds");
            }
            DrawProperty("faceCustomValue");

            using (new EditorGUI.DisabledScope(isRunning))
            {
                DrawSection(component,
                    ("Face Preset", () => component.FacePresetAsync()),
                    ("Face Custom", () => component.FaceCustomAsync()));
            }
        }

        private void DrawLedSection(VoiceAgentComponent component)
        {
            EditorGUILayout.Space();
            EditorGUILayout.LabelField("LED", EditorStyles.boldLabel);
            DrawProperty("ledColor");
            using (new EditorGUILayout.HorizontalScope())
            {
                DrawProperty("ledBrightness");
                DrawProperty("ledPeriod");
                DrawProperty("ledDuration");
            }

            using (new EditorGUI.DisabledScope(isRunning))
            {
                DrawSection(component,
                    ("LED Breathe", () => component.LedBreatheAsync()),
                    ("LED Solid", () => component.LedSolidAsync()),
                    ("LED Random", () => component.LedRandomAsync()),
                    ("LED Off", () => component.LedOffAsync()));
            }
        }

        private void DrawFlowerSection(VoiceAgentComponent component)
        {
            EditorGUILayout.Space();
            EditorGUILayout.LabelField("Flower", EditorStyles.boldLabel);

            using (new EditorGUI.DisabledScope(isRunning))
            {
                DrawSection(component,
                    ("Flower Open", () => component.FlowerOpenAsync()),
                    ("Flower Close", () => component.FlowerCloseAsync()),
                    ("Flower Stop", () => component.FlowerStopAsync()),
                    ("Flower Open Slow", () => component.FlowerOpenSlowAsync()),
                    ("Flower Close Slow", () => component.FlowerCloseSlowAsync()));
            }
        }

        private void DrawResultSection(VoiceAgentComponent component)
        {
            EditorGUILayout.Space();
            EditorGUILayout.LabelField("Last Result", EditorStyles.boldLabel);

            using (new EditorGUI.DisabledScope(true))
            {
                EditorGUILayout.Toggle("Success", component.LastSuccess);
                EditorGUILayout.IntField("Status Code", component.LastStatusCode);
                EditorGUILayout.TextField("Message", component.LastMessage ?? string.Empty);
                EditorGUILayout.TextArea(component.LastRawBody ?? string.Empty, GUILayout.MinHeight(70f));
            }

            using (new EditorGUI.DisabledScope(isRunning))
            {
                if (GUILayout.Button("Clear Last Result"))
                {
                    ApplyChanges();
                    component.ClearLastResult();
                    EditorUtility.SetDirty(component);
                }
            }

            EditorGUILayout.Space();
            var messageType = component.LastSuccess ? MessageType.Info : MessageType.None;
            EditorGUILayout.HelpBox(
                $"Running: {isRunning}\nStatus: {component.LastStatusCode}\nMessage: {component.LastMessage}",
                messageType);
        }

        private void DrawSettingsFields()
        {
            var settingsProperty = serializedObject.FindProperty("settings");
            if (settingsProperty == null)
            {
                return;
            }

            DrawProperty(settingsProperty, "host");
            using (new EditorGUILayout.HorizontalScope())
            {
                DrawProperty(settingsProperty, "panelPort");
                DrawProperty(settingsProperty, "requestTimeoutSeconds");
            }
            using (new EditorGUILayout.HorizontalScope())
            {
                DrawStringChoice(settingsProperty, "defaultVoice", "Default Voice", inspectorOptions.PiperVoices);
                DrawStringChoice(settingsProperty, "defaultBackend", "Default Backend", inspectorOptions.Backends);
            }
            using (new EditorGUILayout.HorizontalScope())
            {
                DrawStringChoice(settingsProperty, "defaultTtsModel", "Default TTS Model", inspectorOptions.TtsModels);
                DrawStringChoice(settingsProperty, "defaultKokoroVoice", "Default Kokoro Voice", inspectorOptions.KokoroVoices);
            }
        }

        private void DrawSection(VoiceAgentComponent component, params (string label, Func<Task<VoiceAgentApiResult>> action)[] buttons)
        {
            using (new EditorGUILayout.HorizontalScope())
            {
                for (var index = 0; index < buttons.Length; index++)
                {
                    if (GUILayout.Button(buttons[index].label))
                    {
                        RunTask(component, buttons[index].action);
                    }
                }
            }
        }

        private void DrawProperty(string propertyPath)
        {
            var property = serializedObject.FindProperty(propertyPath);
            if (property != null)
            {
                EditorGUILayout.PropertyField(property);
            }
        }

        private void DrawProperty(SerializedProperty parent, string relativePath)
        {
            var property = parent != null ? parent.FindPropertyRelative(relativePath) : null;
            if (property != null)
            {
                EditorGUILayout.PropertyField(property);
            }
        }

        private void DrawStringChoice(string propertyPath, string label, IList<string> options)
        {
            var property = serializedObject.FindProperty(propertyPath);
            DrawStringChoice(property, label, options);
        }

        private void DrawStringChoice(SerializedProperty parent, string relativePath, string label, IList<string> options)
        {
            var property = parent != null ? parent.FindPropertyRelative(relativePath) : null;
            DrawStringChoice(property, label, options);
        }

        private void DrawStringChoice(SerializedProperty property, string label, IList<string> options)
        {
            if (property == null)
            {
                return;
            }

            if (options == null || options.Count == 0)
            {
                EditorGUILayout.PropertyField(property, new GUIContent(label));
                return;
            }

            var values = new List<string>();
            var display = new List<string>();
            var current = property.stringValue ?? string.Empty;

            if (string.IsNullOrWhiteSpace(current))
            {
                values.Add(string.Empty);
                display.Add("<empty>");
            }

            AddChoices(values, display, options);

            if (!string.IsNullOrWhiteSpace(current) && !ContainsValue(values, current))
            {
                values.Insert(0, current);
                display.Insert(0, current + " (current)");
            }

            var selectedIndex = IndexOfValue(values, current);
            if (selectedIndex < 0)
            {
                selectedIndex = 0;
            }

            var nextIndex = EditorGUILayout.Popup(label, selectedIndex, display.ToArray());
            if (nextIndex >= 0 && nextIndex < values.Count)
            {
                property.stringValue = values[nextIndex];
            }
        }

        private void ApplyChanges()
        {
            serializedObject.ApplyModifiedProperties();
        }

        private async void RefreshInspectorOptions(VoiceAgentComponent component)
        {
            if (component == null || isRefreshingOptions)
            {
                return;
            }

            isRefreshingOptions = true;
            optionStatusMessage = "Loading options from runtime...";
            Repaint();

            ApplyChanges();

            try
            {
                using (var client = new VoiceAgentClient(component.Settings))
                {
                    var voiceResult = await client.GetTtsOptionsAsync();
                    var kokoroResult = await client.GetKokoroOptionsAsync();
                    var runtimeResult = await client.GetRuntimeConfigAsync();
                    var asrResult = await client.GetAsrStatusAsync();

                    inspectorOptions = BuildInspectorOptions(voiceResult, kokoroResult, runtimeResult, asrResult);
                    var loadedCount = inspectorOptions.LoadedGroupsCount;
                    optionStatusMessage = loadedCount > 0
                        ? $"Loaded {loadedCount} option group(s) from runtime."
                        : "Could not load runtime options. Text fields are still available as fallback.";
                }
            }
            catch (Exception ex)
            {
                optionStatusMessage = "Failed to load options: " + ex.Message;
            }
            finally
            {
                isRefreshingOptions = false;
                Repaint();
            }
        }

        private static InspectorOptions BuildInspectorOptions(
            VoiceAgentApiResult voiceResult,
            VoiceAgentApiResult kokoroResult,
            VoiceAgentApiResult runtimeResult,
            VoiceAgentApiResult asrResult)
        {
            var options = new InspectorOptions();

            if (voiceResult != null && voiceResult.Success && !string.IsNullOrWhiteSpace(voiceResult.RawBody))
            {
                try
                {
                    var payload = JsonUtility.FromJson<VoiceOptionsPayload>(voiceResult.RawBody);
                    options.PiperVoices = NormalizeChoices(payload != null ? payload.voices : null, payload != null ? payload.current : null);
                    options.TtsModels = NormalizeChoices(payload != null ? payload.models : null, payload != null ? payload.modelCurrent : null);
                    options.Backends = NormalizeChoices(payload != null ? payload.backends : null, payload != null ? payload.backendCurrent : null);
                }
                catch
                {
                }
            }

            if (kokoroResult != null && kokoroResult.Success && !string.IsNullOrWhiteSpace(kokoroResult.RawBody))
            {
                try
                {
                    var payload = JsonUtility.FromJson<KokoroOptionsPayload>(kokoroResult.RawBody);
                    options.KokoroVoices = NormalizeChoices(payload != null ? payload.voices : null, payload != null ? payload.current : null);
                }
                catch
                {
                }
            }

            if (runtimeResult != null && runtimeResult.Success && !string.IsNullOrWhiteSpace(runtimeResult.RawBody))
            {
                try
                {
                    var payload = JsonUtility.FromJson<RuntimeOptionsPayload>(runtimeResult.RawBody);
                    options.LocalModels = NormalizeChoices(payload != null ? payload.ollama_model_options : null, payload != null ? payload.ollama_model : null);
                }
                catch
                {
                }
            }

            if (asrResult != null && asrResult.Success && !string.IsNullOrWhiteSpace(asrResult.RawBody))
            {
                try
                {
                    var payload = JsonUtility.FromJson<AsrStatusPayload>(asrResult.RawBody);
                    options.AsrModes = NormalizeChoices(payload != null ? payload.available_modes : null, payload != null ? payload.mode : null);
                    var backendModes = payload != null && payload.server_transcribe != null ? payload.server_transcribe.available_modes : null;
                    var backendCurrent = payload != null && payload.server_transcribe != null ? payload.server_transcribe.mode : null;
                    options.BackendAsrModes = NormalizeChoices(backendModes, backendCurrent);
                }
                catch
                {
                }
            }

            return options;
        }

        private static List<string> NormalizeChoices(string[] rawValues, string current)
        {
            var values = new List<string>();
            if (rawValues != null)
            {
                for (var index = 0; index < rawValues.Length; index++)
                {
                    var value = rawValues[index];
                    if (string.IsNullOrWhiteSpace(value) || ContainsValue(values, value))
                    {
                        continue;
                    }

                    values.Add(value);
                }
            }

            if (!string.IsNullOrWhiteSpace(current) && !ContainsValue(values, current))
            {
                values.Add(current);
            }

            return values;
        }

        private static void AddChoices(List<string> values, List<string> display, IList<string> options)
        {
            for (var index = 0; index < options.Count; index++)
            {
                var value = options[index];
                if (string.IsNullOrWhiteSpace(value) || ContainsValue(values, value))
                {
                    continue;
                }

                values.Add(value);
                display.Add(value);
            }
        }

        private static int IndexOfValue(IList<string> values, string target)
        {
            var normalizedTarget = target ?? string.Empty;
            for (var index = 0; index < values.Count; index++)
            {
                if (string.Equals(values[index] ?? string.Empty, normalizedTarget, StringComparison.Ordinal))
                {
                    return index;
                }
            }

            return -1;
        }

        private static bool ContainsValue(IList<string> values, string target)
        {
            return IndexOfValue(values, target) >= 0;
        }

        private static bool IsKokoroBackend(string backend)
        {
            return string.Equals((backend ?? string.Empty).Trim(), "kokoro", StringComparison.OrdinalIgnoreCase);
        }

        private async void RunTask(VoiceAgentComponent component, Func<Task> action)
        {
            if (isRunning || action == null)
            {
                return;
            }

            isRunning = true;
            ApplyChanges();
            try
            {
                component.RecreateClient();
                await action();
            }
            catch (Exception ex)
            {
                Debug.LogException(ex, component);
            }
            finally
            {
                isRunning = false;
                EditorUtility.SetDirty(component);
                Repaint();
            }
        }

        [Serializable]
        private sealed class VoiceOptionsPayload
        {
            public string[] voices;
            public string current;
            public string[] models;
            public string modelCurrent;
            public string[] backends;
            public string backendCurrent;
        }

        [Serializable]
        private sealed class KokoroOptionsPayload
        {
            public string[] voices;
            public string current;
        }

        [Serializable]
        private sealed class RuntimeOptionsPayload
        {
            public string[] ollama_model_options;
            public string ollama_model;
        }

        [Serializable]
        private sealed class AsrStatusPayload
        {
            public string[] available_modes;
            public string mode;
            public BackendTranscribePayload server_transcribe;
        }

        [Serializable]
        private sealed class BackendTranscribePayload
        {
            public string[] available_modes;
            public string mode;
        }

        private sealed class InspectorOptions
        {
            public List<string> PiperVoices { get; set; } = new List<string>();
            public List<string> TtsModels { get; set; } = new List<string>();
            public List<string> Backends { get; set; } = new List<string>();
            public List<string> KokoroVoices { get; set; } = new List<string>();
            public List<string> LocalModels { get; set; } = new List<string>();
            public List<string> AsrModes { get; set; } = new List<string>();
            public List<string> BackendAsrModes { get; set; } = new List<string>();

            public int LoadedGroupsCount
            {
                get
                {
                    var count = 0;
                    if (PiperVoices.Count > 0 || TtsModels.Count > 0 || Backends.Count > 0) count++;
                    if (KokoroVoices.Count > 0) count++;
                    if (LocalModels.Count > 0) count++;
                    if (AsrModes.Count > 0 || BackendAsrModes.Count > 0) count++;
                    return count;
                }
            }
        }
    }
}
