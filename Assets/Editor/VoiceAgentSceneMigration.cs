#if UNITY_EDITOR
using System;
using System.Collections.Generic;
using System.Linq;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;
using UnityEngine.SceneManagement;

namespace RobotVoice.EditorTools
{
    public static class VoiceAgentSceneMigration
    {
        private const string LocalHost = "127.0.0.1";
        private const int MqttPort = 1883;
        private const string IntentTopic = "robot/intent";
        private const string VoiceTextTopic = "robot/voice/text";
        private const string CaptionTopic = "robot/captions/text";
        private const string DialogAnswerTopic = "robot/dialog/answer";
        private const string AsrBaseUrl = "http://127.0.0.1:8000";
        private const string AsrTranscribeUrl = AsrBaseUrl + "/transcribe";
        private const string PiperSpeakUrl = "http://127.0.0.1:5005/speak";
        private const string PiperSpeakStreamUrl = "http://127.0.0.1:5005/speak_stream";
        private const string QwenSpeakUrl = "http://127.0.0.1:5006/speak";
        private const string OllamaBaseUrl = "http://127.0.0.1:11434";
        private const string TelemetryDashboardUrl = "http://127.0.0.1:8101/dashboard";
        private const string DefaultVisionModel = "gemma3:4b";
        private const string UnityVoiceSource = "unity_voice";

        [MenuItem("Tools/Voice Agent/Migrate Scene Defaults")]
        public static void MigrateAllScenes()
        {
            if (!EditorSceneManager.SaveCurrentModifiedScenesIfUserWantsTo())
            {
                return;
            }

            var originalScenePath = SceneManager.GetActiveScene().path;
            var sceneGuids = AssetDatabase.FindAssets("t:Scene");
            var scenePaths = sceneGuids
                .Select(AssetDatabase.GUIDToAssetPath)
                .Where(path => !string.IsNullOrWhiteSpace(path))
                .Distinct(StringComparer.OrdinalIgnoreCase)
                .OrderBy(path => path, StringComparer.OrdinalIgnoreCase)
                .ToList();

            var totalScenesTouched = 0;
            var totalPropertyUpdates = 0;

            try
            {
                for (var i = 0; i < scenePaths.Count; i++)
                {
                    var scenePath = scenePaths[i];
                    var scene = EditorSceneManager.OpenScene(scenePath, OpenSceneMode.Single);
                    var updates = MigrateScene(scene);
                    if (updates > 0)
                    {
                        EditorSceneManager.SaveScene(scene);
                        totalScenesTouched++;
                        totalPropertyUpdates += updates;
                        Debug.Log($"[VoiceAgentMigration] {scenePath}: updated {updates} properties");
                    }
                }
            }
            finally
            {
                if (!string.IsNullOrWhiteSpace(originalScenePath))
                {
                    EditorSceneManager.OpenScene(originalScenePath, OpenSceneMode.Single);
                }
            }

            EditorUtility.DisplayDialog(
                "Voice Agent Migration",
                $"Scenes touched: {totalScenesTouched}\nProperties updated: {totalPropertyUpdates}",
                "OK");
        }

        private static int MigrateScene(Scene scene)
        {
            var updates = 0;
            var roots = scene.GetRootGameObjects();

            foreach (var root in roots)
            {
                updates += MigrateComponents(root);
            }

            return updates;
        }

        private static int MigrateComponents(GameObject root)
        {
            var updates = 0;

            foreach (var launcher in root.GetComponentsInChildren<VoiceGameLauncher>(true))
            {
                updates += MigrateVoiceGameLauncher(launcher);
            }

            foreach (var speech in root.GetComponentsInChildren<VoskSpeechToText>(true))
            {
                updates += MigrateSpeechToText(speech);
            }

            foreach (var publisher in root.GetComponentsInChildren<MqttIntentPublisher>(true))
            {
                updates += MigrateMqttIntentPublisher(publisher);
            }

            foreach (var caption in root.GetComponentsInChildren<MqttCaptionSubscriber>(true))
            {
                updates += MigrateCaptionSubscriber(caption);
            }

            foreach (var dialog in root.GetComponentsInChildren<MqttDialogAnswerSubscriber>(true))
            {
                updates += MigrateDialogSubscriber(dialog);
            }

            foreach (var panel in root.GetComponentsInChildren<UserTestControlPanel>(true))
            {
                updates += MigrateTestPanel(panel);
            }

            return updates;
        }

        private static int MigrateVoiceGameLauncher(VoiceGameLauncher component)
        {
            if (component == null) return 0;

            var so = new SerializedObject(component);
            var pending = 0;

            pending += SetStringIfEmpty(so, "voiceTextTopic", VoiceTextTopic);
            pending += SetStringIfEmpty(so, "piperSpeakUrl", PiperSpeakUrl);
            pending += SetStringIfEmpty(so, "qwenSpeakUrl", QwenSpeakUrl);
            pending += SetStringIfEmpty(so, "piperSpeakStreamUrl", PiperSpeakStreamUrl);

            return ApplyIfChanged(component, so, pending);
        }

        private static int MigrateSpeechToText(VoskSpeechToText component)
        {
            if (component == null) return 0;

            var so = new SerializedObject(component);
            var pending = 0;
            pending += SetStringIfEmpty(so, "PythonServiceUrl", AsrTranscribeUrl);

            return ApplyIfChanged(component, so, pending);
        }

        private static int MigrateMqttIntentPublisher(MqttIntentPublisher component)
        {
            if (component == null) return 0;

            var so = new SerializedObject(component);
            var pending = 0;

            pending += SetStringIfEmpty(so, "host", LocalHost);
            pending += SetIntIfInvalid(so, "port", MqttPort, minimum: 1);
            pending += SetStringIfEmpty(so, "intentTopic", IntentTopic);
            pending += SetStringIfEmptyOrLegacy(so, "sourceLabel", UnityVoiceSource, "unity_whisper");

            return ApplyIfChanged(component, so, pending);
        }

        private static int MigrateCaptionSubscriber(MqttCaptionSubscriber component)
        {
            if (component == null) return 0;

            var so = new SerializedObject(component);
            var pending = 0;

            pending += SetStringIfEmpty(so, "host", LocalHost);
            pending += SetIntIfInvalid(so, "port", MqttPort, minimum: 1);
            // Prevent self-feedback loops from accidental wrong topic.
            pending += SetStringIfEmptyOrLegacy(so, "topic", CaptionTopic, VoiceTextTopic);

            return ApplyIfChanged(component, so, pending);
        }

        private static int MigrateDialogSubscriber(MqttDialogAnswerSubscriber component)
        {
            if (component == null) return 0;

            var so = new SerializedObject(component);
            var pending = 0;

            pending += SetStringIfEmpty(so, "host", LocalHost);
            pending += SetIntIfInvalid(so, "port", MqttPort, minimum: 1);
            pending += SetStringIfEmpty(so, "topic", DialogAnswerTopic);

            return ApplyIfChanged(component, so, pending);
        }

        private static int MigrateTestPanel(UserTestControlPanel component)
        {
            if (component == null) return 0;

            var so = new SerializedObject(component);
            var pending = 0;

            pending += SetStringIfEmpty(so, "voiceServiceUrl", PiperSpeakUrl);
            pending += SetStringIfEmpty(so, "llmServiceBaseUrl", AsrBaseUrl);
            pending += SetStringIfEmpty(so, "ollamaBaseUrl", OllamaBaseUrl);
            pending += SetStringIfEmpty(so, "telemetryDashboardUrl", TelemetryDashboardUrl);
            pending += SetStringIfEmpty(so, "defaultVisionModel", DefaultVisionModel);

            return ApplyIfChanged(component, so, pending);
        }

        private static int ApplyIfChanged(UnityEngine.Object target, SerializedObject so, int pendingChanges)
        {
            if (pendingChanges <= 0)
            {
                return 0;
            }

            so.ApplyModifiedPropertiesWithoutUndo();
            EditorUtility.SetDirty(target);
            return pendingChanges;
        }

        private static int SetStringIfEmpty(SerializedObject so, string propertyName, string desiredValue)
        {
            return SetStringIfEmptyOrLegacy(so, propertyName, desiredValue);
        }

        private static int SetStringIfEmptyOrLegacy(
            SerializedObject so,
            string propertyName,
            string desiredValue,
            params string[] legacyValues)
        {
            var prop = so.FindProperty(propertyName);
            if (prop == null || prop.propertyType != SerializedPropertyType.String)
            {
                return 0;
            }

            var current = (prop.stringValue ?? string.Empty).Trim();
            if (string.Equals(current, desiredValue, StringComparison.Ordinal))
            {
                return 0;
            }

            var shouldUpdate = string.IsNullOrEmpty(current);
            if (!shouldUpdate && legacyValues != null)
            {
                for (var i = 0; i < legacyValues.Length; i++)
                {
                    var legacy = legacyValues[i] ?? string.Empty;
                    if (string.Equals(current, legacy, StringComparison.OrdinalIgnoreCase))
                    {
                        shouldUpdate = true;
                        break;
                    }
                }
            }

            if (!shouldUpdate)
            {
                return 0;
            }

            prop.stringValue = desiredValue;
            return 1;
        }

        private static int SetIntIfInvalid(SerializedObject so, string propertyName, int desiredValue, int minimum)
        {
            var prop = so.FindProperty(propertyName);
            if (prop == null || prop.propertyType != SerializedPropertyType.Integer)
            {
                return 0;
            }

            if (prop.intValue >= minimum)
            {
                return 0;
            }

            prop.intValue = desiredValue;
            return 1;
        }
    }
}
#endif
