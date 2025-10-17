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
		EditorGUILayout.LabelField("PiMessageHub Test", EditorStyles.boldLabel);
		GUI.enabled = Application.isPlaying;
		if (GUILayout.Button("Face Happy (default)")) { _ = hub.SendFaceHappyAsync(); }
		if (GUILayout.Button("Face Idle")) { _ = hub.SendFaceIdleAsync(); }
		EditorGUILayout.BeginHorizontal();
		if (GUILayout.Button("Servo Open (default)")) { _ = hub.OpenFlowerAsync(); }
		if (GUILayout.Button("Servo Close (default)")) { _ = hub.CloseFlowerAsync(); }
		EditorGUILayout.EndHorizontal();
		EditorGUILayout.Space();
		EditorGUILayout.LabelField("LED", EditorStyles.miniBoldLabel);
		EditorGUILayout.BeginHorizontal();
		if (GUILayout.Button("LED Breathe")) { _ = hub.SendLedBreathAsync(); }
		if (GUILayout.Button("LED Random")) { _ = hub.SendLedRandomAsync(); }
		if (GUILayout.Button("LED Off")) { _ = hub.SendLedOffAsync(); }
		EditorGUILayout.EndHorizontal();
		GUI.enabled = true;
	}
}


