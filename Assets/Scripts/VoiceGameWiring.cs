using UnityEngine;

namespace RobotVoice
{
    public class VoiceGameWiring : MonoBehaviour
    {
        [Header("References")]
        [SerializeField] private VoskSpeechToText speech;
        [SerializeField] private VoiceGameLauncher launcher;

        private void Awake()
        {
            if (speech == null)
            {
                speech = GetComponent<VoskSpeechToText>();
            }
            if (launcher == null)
            {
                launcher = GetComponent<VoiceGameLauncher>();
            }
        }

        private void OnEnable()
        {
            if (speech != null)
            {
                speech.OnTranscriptionResult += OnResult;
            }
        }

        private void OnDisable()
        {
            if (speech != null)
            {
                speech.OnTranscriptionResult -= OnResult;
            }
        }

        private void OnResult(string json)
        {
            if (launcher != null)
            {
                launcher.HandleVoskResult(json);
            }
        }
    }
}


