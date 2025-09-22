using UnityEditor;
using UnityEngine;

public class VoskKeyphraseToolbar : EditorWindow
{
    private VoskSpeechToText _speech;
    private SerializedObject _serializedSpeech;
    private SerializedProperty _keyPhrasesProp;
    private string _newPhrase = "";

    [MenuItem("Window/Vosk/Keyphrase Toolbar")] 
    public static void ShowWindow()
    {
        GetWindow<VoskKeyphraseToolbar>("Keyphrase Toolbar");
    }

    private void OnFocus()
    {
        UpdateSelection();
    }

    private void OnSelectionChange()
    {
        UpdateSelection();
        Repaint();
    }

    private void UpdateSelection()
    {
        _speech = null;
        _serializedSpeech = null;
        _keyPhrasesProp = null;
        if (Selection.activeGameObject != null)
        {
            _speech = Selection.activeGameObject.GetComponent<VoskSpeechToText>();
            if (_speech != null)
            {
                _serializedSpeech = new SerializedObject(_speech);
                _keyPhrasesProp = _serializedSpeech.FindProperty("KeyPhrases");
            }
        }
    }

    private void OnGUI()
    {
        GUILayout.Label("Keyphrases", EditorStyles.boldLabel);
        if (_keyPhrasesProp == null)
        {
            EditorGUILayout.HelpBox("Select a GameObject with VoskSpeechToText to edit its keyphrases.", MessageType.Info);
            return;
        }

        _serializedSpeech.Update();
        EditorGUILayout.PropertyField(_keyPhrasesProp, true);

        EditorGUILayout.BeginHorizontal();
        _newPhrase = EditorGUILayout.TextField(_newPhrase);
        if (GUILayout.Button("Add", GUILayout.Width(50)))
        {
            if (!string.IsNullOrEmpty(_newPhrase))
            {
                _keyPhrasesProp.arraySize++;
                _keyPhrasesProp.GetArrayElementAtIndex(_keyPhrasesProp.arraySize - 1).stringValue = _newPhrase;
                _newPhrase = "";
            }
        }
        EditorGUILayout.EndHorizontal();

        if (GUILayout.Button("Apply"))
        {
            _serializedSpeech.ApplyModifiedProperties();
            EditorUtility.SetDirty(_speech);
        }
    }
}
