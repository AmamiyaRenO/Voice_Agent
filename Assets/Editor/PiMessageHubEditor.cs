using UnityEditor;
using UnityEngine;

[CustomEditor(typeof(PiMessageHub))]
public class PiMessageHubEditor : Editor
{
	public override void OnInspectorGUI()
	{
		base.OnInspectorGUI();
		var hub = (PiMessageHub)target;
		EditorGUILayout.Space();

		// Runtime status (publisher and MQTT) to help debugging
		using (new EditorGUI.DisabledScope(!Application.isPlaying))
		{
			var pubProp = serializedObject.FindProperty("publisher");
			var pub = pubProp != null ? pubProp.objectReferenceValue as RobotVoice.MqttIntentPublisher : null;
			EditorGUILayout.LabelField("Runtime Status", EditorStyles.miniBoldLabel);
			if (pub == null)
			{
				EditorGUILayout.HelpBox("publisher is null (PiMessageHub → MqttIntentPublisher 未绑定)", MessageType.Warning);
			}
			else
			{
				EditorGUILayout.LabelField("Publisher", pub.name);
				EditorGUILayout.LabelField("Disable Publishing", pub.DisablePublishing ? "true" : "false");
				EditorGUILayout.LabelField("MQTT", pub.IsConnected ? $"Connected ({pub.Host}:{pub.Port})" : $"Not Connected ({pub.Host}:{pub.Port})");
			}
			EditorGUILayout.Space();
		}

		EditorGUILayout.LabelField("PiMessageHub Test", EditorStyles.boldLabel);
		GUI.enabled = Application.isPlaying;

		EditorGUILayout.LabelField("Expressions", EditorStyles.miniBoldLabel);
		EditorGUILayout.BeginHorizontal();
		if (GUILayout.Button("Excited")) { Debug.Log("[PiMessageHubEditor] Click: Face Excited"); _ = hub.SendFacePresetAsync("excited", 0f); }
		if (GUILayout.Button("Happy")) { Debug.Log("[PiMessageHubEditor] Click: Face Happy"); _ = hub.SendFacePresetAsync("happy", 0f); }
		if (GUILayout.Button("Neutral")) { Debug.Log("[PiMessageHubEditor] Click: Face Neutral"); _ = hub.SendFacePresetAsync("neutral", 0f); }
		if (GUILayout.Button("Sad")) { Debug.Log("[PiMessageHubEditor] Click: Face Sad"); _ = hub.SendFacePresetAsync("sad", 0f); }
		if (GUILayout.Button("Very Sad")) { Debug.Log("[PiMessageHubEditor] Click: Face VerySad"); _ = hub.SendFacePresetAsync("verySad", 0f); }
		EditorGUILayout.EndHorizontal();

		EditorGUILayout.BeginHorizontal();
		if (GUILayout.Button("Servo Open (default)")) { Debug.Log("[PiMessageHubEditor] Click: Servo Open"); _ = hub.OpenFlowerAsync(); }
		if (GUILayout.Button("Servo Close (default)")) { Debug.Log("[PiMessageHubEditor] Click: Servo Close"); _ = hub.CloseFlowerAsync(); }
		EditorGUILayout.EndHorizontal();
		EditorGUILayout.Space();
		EditorGUILayout.LabelField("LED", EditorStyles.miniBoldLabel);
		EditorGUILayout.BeginHorizontal();
		if (GUILayout.Button("LED Breathe")) { Debug.Log("[PiMessageHubEditor] Click: LED Breathe"); _ = hub.SendLedBreathAsync(); }
		if (GUILayout.Button("LED Random")) { Debug.Log("[PiMessageHubEditor] Click: LED Random"); _ = hub.SendLedRandomAsync(); }
		if (GUILayout.Button("LED Off")) { Debug.Log("[PiMessageHubEditor] Click: LED Off"); _ = hub.SendLedOffAsync(); }
		EditorGUILayout.EndHorizontal();
		GUI.enabled = true;
	}
}


