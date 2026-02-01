using System;
using System.Collections;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Text;
using UnityEngine;
using UnityEngine.Networking;

public class VoskSpeechToText : MonoBehaviour
{
        [Header("Python Speech Service")]
        [Tooltip("If enabled, audio is sent to an external Python service that performs speech recognition using Faster-Whisper.")]
        public bool UsePythonService;

        [Tooltip("HTTP endpoint for the Python speech service transcribe API.")]
        public string PythonServiceUrl = "http://127.0.0.1:8000/transcribe";

        [Tooltip("Optional language hint passed to the Python speech service (e.g. 'zh').")]
        public string PythonServiceLanguage = string.Empty;

        [Tooltip("Beam size used by the Python speech service.")]
        public int PythonServiceBeamSize = 5;

        [Tooltip("Minimum normalized amplitude required before audio is sent to the Python speech service (0-1).")]
        [Range(0f, 1f)]
        public float PythonServiceSilenceThreshold = 0.02f;

        [Tooltip("Max record length per segment when using the Python speech service (seconds).")]
        [Range(0.1f, 10f)]
        public float PythonMaxRecordLength = 3.0f;

        [Tooltip("Frame length for VoiceProcessor when using the Python speech service.")]
        public int PythonFrameLength = 256;

        [Tooltip("The source of the microphone input.")]
        public VoiceProcessor VoiceProcessor;
	[Tooltip("The Max number of alternatives that will be processed.")]
	public int MaxAlternatives = 3;

	[Tooltip("How long should we record before restarting?")]
	public float MaxRecordLength = 5;

	[Tooltip("Should the recognizer start when the application is launched?")]
	public bool AutoStart = true;

	[Tooltip("The phrases that will be detected. If left empty, all words will be detected.")]
	public List<string> KeyPhrases = new List<string>();

	//Holds all of the audio data until the user stops talking.
	private readonly List<short> _buffer = new List<short>();

	//Called when the the state of the controller changes.
	public Action<string> OnStatusUpdated;

	//Called after a transcription is ready.
	public Action<string> OnTranscriptionResult;

        [Header("Diagnostics")]
        [Tooltip("If true, log record stop events (can be noisy because segmentation stops recording frequently).")]
        public bool VerboseStopLogging = false;

	private bool _isInitializing;

	private bool _didInit;

	//Threading Logic

	// Flag to signal we are ending
	private bool _running;

	//Thread safe queue of microphone data.
	private readonly ConcurrentQueue<short[]> _threadedBufferQueue = new ConcurrentQueue<short[]>();

        //Thread safe queue of resuts
        private readonly ConcurrentQueue<string> _threadedResultQueue = new ConcurrentQueue<string>();

        // Python speech service state
        private bool _usePythonService;
        private bool _playbackMute; // when true, drop captured frames to avoid TTS feedback
        private readonly object _pythonBufferLock = new object();
        private readonly List<short> _pythonBuffer = new List<short>();
        private bool _pythonSegmentActive;
        private float _pythonSegmentStartTime;
        private bool _pythonForceFlushRequested;
        private bool _pythonRequestInFlight;
        private float _defaultMaxRecordLength;
        private bool _defaultMaxRecordLengthCaptured;
        private bool _wakeWordOverrideActive;
        private bool _wakeWordPrimingStopPending;
        private float _pythonLastSegmentMaxAmplitude;
        private float _pythonLastSegmentRms;
        void Awake()
        {
                if (VoiceProcessor == null)
                {
                        VoiceProcessor = GetComponent<VoiceProcessor>();
                        if (VoiceProcessor == null)
                        {
                                VoiceProcessor = gameObject.AddComponent<VoiceProcessor>();
                        }
                }

                if (!_defaultMaxRecordLengthCaptured)
                {
                        _defaultMaxRecordLength = MaxRecordLength;
                        _defaultMaxRecordLengthCaptured = true;
                }
        }

	//If Auto start is enabled, starts vosk speech to text.
	void Start()
	{
		//KeyPhrases = new List<string> { "forward", "backward", "left", "right" };
		if (AutoStart)
		{
			StartVoskStt();
		}
	}

	/// <summary>
	/// Start speech to text (Python service only; Vosk implementation removed)
	/// </summary>
	/// <param name="keyPhrases">Unused in Python mode.</param>
	/// <param name="modelPath">Unused in Python mode.</param>
	/// <param name="startMicrophone">Should the microphone start after initialization?</param>
	/// <param name="maxAlternatives">The maximum number of alternative phrases detected</param>
        public void StartVoskStt(List<string> keyPhrases = null, string modelPath = default, bool startMicrophone = false, int maxAlternatives = 3)
        {
                if (_isInitializing)
                {
                        Debug.LogError("Initializing in progress!");
                        return;
		}
		if (_didInit)
		{
			Debug.LogError("Speech has already been initialized!");
			return;
		}

                // Force Python service usage; Vosk implementation removed.
                _usePythonService = true;

                if (keyPhrases != null)
		{
			KeyPhrases = keyPhrases;
		}

                MaxAlternatives = maxAlternatives;

                StartCoroutine(StartPythonStt(startMicrophone));
        }

        private IEnumerator StartPythonStt(bool startMicrophone)
        {
                _isInitializing = true;

                yield return WaitForMicrophoneInput();

                OnStatusUpdated?.Invoke("Initialising Python speech service");

                VoiceProcessor.OnFrameCaptured += VoiceProcessorOnOnFrameCaptured;
                VoiceProcessor.OnRecordingStop += VoiceProcessorOnOnRecordingStop;
                VoiceProcessor.OnRecordingStart += VoiceProcessorOnRecordingStart;

                _isInitializing = false;
                _didInit = true;

                OnStatusUpdated?.Invoke("Python speech service ready");

                if (PythonMaxRecordLength > 0f)
                {
                        MaxRecordLength = PythonMaxRecordLength;
                }

                ToggleRecording();
        }

	//Wait until microphones are initialized
	private IEnumerator WaitForMicrophoneInput()
	{
		while (Microphone.devices.Length <= 0)
			yield return null;
	}

	//Can be called from a script or a GUI button to start detection.
        public void ToggleRecording()
        {
                Debug.Log("Toogle Recording");
                if (!VoiceProcessor.IsRecording)
                {
                        Debug.Log("Start Recording");
                        _running = true;

                        ClearPythonBuffer();
                        var sampleRate = VoiceProcessor.SampleRate > 0 ? VoiceProcessor.SampleRate : 16000;
                        var frameLength = PythonFrameLength > 0 ? PythonFrameLength : (VoiceProcessor.FrameLength > 0 ? VoiceProcessor.FrameLength : 512);
                        VoiceProcessor.StartRecording(sampleRate, frameLength, false);
                }
                else
                {
                        Debug.Log("Stop Recording");
                        _running = false;
                        VoiceProcessor.StopRecording();
                }
        }

	//Calls the On Phrase Recognized event on the Unity Thread
        void Update()
        {
                if (_pythonForceFlushRequested)
                {
                        _pythonForceFlushRequested = false;
                        if (_running)
                        {
                                VoiceProcessor.StopRecording();
                        }
                }

                if (_threadedResultQueue.TryDequeue(out string voiceResult))
                {
                    OnTranscriptionResult?.Invoke(voiceResult);
                }
        }

	//Callback from the voice processor when new audio is detected
        private void VoiceProcessorOnOnFrameCaptured(short[] samples)
        {
                if (_playbackMute)
                {
                        // Drop frames while TTS is playing to avoid feedback
                        return;
                }
                lock (_pythonBufferLock)
                {
                        _pythonBuffer.AddRange(samples);
                }

                if (!_pythonSegmentActive)
                {
                        _pythonSegmentActive = true;
                        _pythonSegmentStartTime = Time.realtimeSinceStartup;
                }

                if (MaxRecordLength > 0 && _pythonSegmentActive)
                {
                        var elapsed = Time.realtimeSinceStartup - _pythonSegmentStartTime;
                        if (elapsed >= MaxRecordLength)
                        {
                                _pythonForceFlushRequested = true;
                        }
                }
        }

	//Callback from the voice processor when recording stops
        private void VoiceProcessorOnRecordingStart()
        {
                ClearPythonBuffer();
                _pythonSegmentActive = false;
                _pythonSegmentStartTime = 0f;
        }

        private void VoiceProcessorOnOnRecordingStop()
        {
                StartCoroutine(HandlePythonRecordingStop(_running));
                if (VerboseStopLogging)
                {
                        Debug.Log("[VoskSpeechToText] Recording stopped");
                }
        }

        public void StartWakeWordWindow(float durationSeconds)
        {
                if (durationSeconds <= 0f)
                {
                        return;
                }

                if (!_defaultMaxRecordLengthCaptured)
                {
                        _defaultMaxRecordLength = MaxRecordLength;
                        _defaultMaxRecordLengthCaptured = true;
                }

                var clamped = Mathf.Max(0.1f, durationSeconds);
                MaxRecordLength = clamped;

                _running = true;
                _wakeWordOverrideActive = true;
                _wakeWordPrimingStopPending = VoiceProcessor != null && VoiceProcessor.IsRecording;

                if (VoiceProcessor == null)
                {
                        return;
                }

                if (!VoiceProcessor.IsRecording)
                {
                        ClearPythonBuffer();
                        var sampleRate = VoiceProcessor.SampleRate > 0 ? VoiceProcessor.SampleRate : 16000;
                        var frameLength = PythonFrameLength > 0 ? PythonFrameLength : (VoiceProcessor.FrameLength > 0 ? VoiceProcessor.FrameLength : 512);
                        VoiceProcessor.StartRecording(sampleRate, frameLength, false);
                }
                else
                {
                        _pythonForceFlushRequested = true;
                }

                return;
        }

        private void ClearPythonBuffer()
        {
                lock (_pythonBufferLock)
                {
                        _pythonBuffer.Clear();
                }

                _pythonSegmentActive = false;
                _pythonSegmentStartTime = 0f;
        }

        private IEnumerator HandlePythonRecordingStop(bool restartRecording)
        {
                short[] samples = null;

                lock (_pythonBufferLock)
                {
                        if (_pythonBuffer.Count > 0)
                        {
                                samples = _pythonBuffer.ToArray();
                                _pythonBuffer.Clear();
                        }

                        _pythonSegmentActive = false;
                        _pythonSegmentStartTime = 0f;
                }

                if (samples != null && samples.Length > 0)
                {
                        yield return SendAudioToPython(samples);
                }

                if (_wakeWordOverrideActive)
                {
                        if (_wakeWordPrimingStopPending)
                        {
                                _wakeWordPrimingStopPending = false;
                        }
                        else
                        {
                                if (_defaultMaxRecordLengthCaptured)
                                {
                                        MaxRecordLength = _defaultMaxRecordLength;
                                }

                                _wakeWordOverrideActive = false;
                        }
                }

                if (restartRecording && _running)
                {
                        var sampleRate = VoiceProcessor.SampleRate > 0 ? VoiceProcessor.SampleRate : 16000;
                        var frameLength = PythonFrameLength > 0 ? PythonFrameLength : (VoiceProcessor.FrameLength > 0 ? VoiceProcessor.FrameLength : 512);
                        VoiceProcessor.StartRecording(sampleRate, frameLength, false);
                }
        }

        private IEnumerator SendAudioToPython(short[] samples)
        {
                if (samples == null || samples.Length == 0)
                {
                        yield break;
                }

                while (_pythonRequestInFlight)
                {
                        yield return null;
                }

                _pythonRequestInFlight = true;

                float maxAmplitude;
                float rms;
                if (IsPythonAudioSegmentSilent(samples, out maxAmplitude, out rms))
                {
                        _pythonRequestInFlight = false;
                        OnStatusUpdated?.Invoke("Python speech service skipped silent audio");
                        _pythonLastSegmentMaxAmplitude = 0f;
                        _pythonLastSegmentRms = 0f;
                        yield break;
                }

                _pythonLastSegmentMaxAmplitude = maxAmplitude;
                _pythonLastSegmentRms = rms;

                var payload = new byte[samples.Length * sizeof(short)];
                Buffer.BlockCopy(samples, 0, payload, 0, payload.Length);

                var sampleRate = VoiceProcessor.SampleRate > 0 ? VoiceProcessor.SampleRate : 16000;
                var url = BuildPythonServiceUrl(sampleRate);
                if (string.IsNullOrEmpty(url))
                {
                        Debug.LogError("Python speech service URL is not configured");
                        _pythonRequestInFlight = false;
                        yield break;
                }

                OnStatusUpdated?.Invoke("Sending audio to Python speech service...");

                using (var request = new UnityWebRequest(url, UnityWebRequest.kHttpVerbPOST))
                {
                        request.uploadHandler = new UploadHandlerRaw(payload)
                        {
                                contentType = "application/octet-stream"
                        };
                        request.downloadHandler = new DownloadHandlerBuffer();

                        yield return request.SendWebRequest();

                        if (request.result == UnityWebRequest.Result.ConnectionError || request.result == UnityWebRequest.Result.ProtocolError)
                        {
                                var error = string.IsNullOrEmpty(request.error) ? request.result.ToString() : request.error;
                                Debug.LogError($"Python speech service request failed: {error}");
                                OnStatusUpdated?.Invoke($"Python speech service error: {error}");
                        }
                        else
                        {
                                var response = request.downloadHandler.text;
                                if (!string.IsNullOrEmpty(response))
                                {
                                        OnStatusUpdated?.Invoke("Python speech service transcription ready");
                                        var enriched = InjectPythonSegmentMetrics(response, _pythonLastSegmentMaxAmplitude, _pythonLastSegmentRms);
                                        _threadedResultQueue.Enqueue(enriched);
                                }
                                else
                                {
                                        OnStatusUpdated?.Invoke("Python speech service returned empty result");
                                }
                        }
                }

                _pythonRequestInFlight = false;
                _pythonLastSegmentMaxAmplitude = 0f;
                _pythonLastSegmentRms = 0f;
        }

        private bool IsPythonAudioSegmentSilent(short[] samples, out float maxAmplitude, out float rms)
        {
                maxAmplitude = 0f;
                rms = 0f;

                if (samples == null || samples.Length == 0)
                {
                        return true;
                }

                double sumSquares = 0.0;

                for (int i = 0; i < samples.Length; i++)
                {
                        float amplitude = Mathf.Abs(samples[i]) / 32768f;
                        sumSquares += amplitude * amplitude;
                        if (amplitude > maxAmplitude)
                        {
                                maxAmplitude = amplitude;
                        }
                }

                if (samples.Length > 0)
                {
                        rms = Mathf.Sqrt((float)(sumSquares / samples.Length));
                }

                return maxAmplitude < PythonServiceSilenceThreshold;
        }

        // Allow external components to temporarily mute microphone capture while local audio is playing
        private void SetPlaybackMute(bool value)
        {
                _playbackMute = value;
        }

        private string InjectPythonSegmentMetrics(string json, float maxAmplitude, float rms)
        {
                if (string.IsNullOrEmpty(json) || !json.TrimStart().StartsWith("{"))
                {
                        return json;
                }

                try
                {
                        var node = JSONNode.Parse(json);
                        var obj = node?.AsObject;
                        if (obj == null)
                        {
                                return json;
                        }

                        obj["max_amplitude"] = Mathf.Clamp01(maxAmplitude);
                        obj["rms"] = Mathf.Clamp01(rms);

                        return obj.ToString();
                }
                catch
                {
                        return json;
                }
        }

        private string BuildPythonServiceUrl(int sampleRate)
        {
                if (string.IsNullOrWhiteSpace(PythonServiceUrl))
                {
                        return string.Empty;
                }

                var builder = new StringBuilder(PythonServiceUrl);
                builder.Append(PythonServiceUrl.Contains("?") ? "&" : "?");
                builder.Append("sample_rate=");
                builder.Append(sampleRate);

                if (!string.IsNullOrWhiteSpace(PythonServiceLanguage))
                {
                        builder.Append("&language=");
                        builder.Append(UnityWebRequest.EscapeURL(PythonServiceLanguage.Trim()));
                }

                if (PythonServiceBeamSize > 0)
                {
                        builder.Append("&beam_size=");
                        builder.Append(PythonServiceBeamSize);
                }

                return builder.ToString();
        }



}
