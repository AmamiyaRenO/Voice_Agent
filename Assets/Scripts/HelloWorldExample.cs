using UnityEngine;

public class HelloWorldExample : MonoBehaviour
{
    public VoskSpeechToText speech;

    void Start()
    {
        if (speech == null)
        {
            speech = GetComponent<VoskSpeechToText>();
        }

        speech.OnTranscriptionResult += OnResult;

        // Start the recogniser and microphone if AutoStart is disabled
        if (!speech.AutoStart)
        {
            speech.StartVoskStt(startMicrophone: true);
        }
    }

    private void OnResult(string json)
    {
        var result = new RecognitionResult(json);
        foreach (var phrase in result.Phrases)
        {
            if (phrase.Text.ToLower().Contains("hello"))
            {
                Debug.Log("hello world");
                break;
            }
        }
    }

    void OnDestroy()
    {
        speech.OnTranscriptionResult -= OnResult;
    }
}
