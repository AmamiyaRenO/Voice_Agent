using RobotVoice;
using UnityEditor;
using UnityEngine;

[CustomEditor(typeof(UserTestControlPanel))]
public sealed class UserTestControlPanelEditor : Editor
{
    public override void OnInspectorGUI()
    {
        DrawDefaultInspector();

        EditorGUILayout.Space();
        EditorGUILayout.LabelField("Streaming ASR", EditorStyles.boldLabel);
        EditorGUILayout.HelpBox(
            "These buttons call the desktop runtime /api/asr operator mode. Use Backend ASR only for /transcribe config.",
            MessageType.Info);

        var panel = (UserTestControlPanel)target;

        using (new EditorGUILayout.HorizontalScope())
        {
            if (GUILayout.Button("Apply Live Captions"))
            {
                panel.ApplyLiveCaptionsStreamingAsrModeForTester();
            }

            if (GUILayout.Button("Apply API"))
            {
                panel.ApplyApiStreamingAsrModeForTester();
            }

            if (GUILayout.Button("Apply Gemini Live"))
            {
                panel.ApplyGeminiLiveStreamingAsrModeForTester();
            }
        }

        if (GUILayout.Button("Apply Configured Default Streaming ASR"))
        {
            panel.ApplyConfiguredStreamingAsrModeForTester();
        }
    }
}
