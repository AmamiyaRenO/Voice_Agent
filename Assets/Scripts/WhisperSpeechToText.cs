using System;
using System.Collections;
using System.Collections.Generic;
using System.Text;
using UnityEngine;
using UnityEngine.Networking;

public class WhisperSpeechToText : MonoBehaviour
{
        [Header("Python Speech Service (Faster-Whisper)")]
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
        public float PythonMaxRecordLength = 1.5f;

        [Tooltip("Frame length for VoiceProcessor when using the Python speech service.")]
        public int PythonFrameLength = 256;

        [Tooltip("Should the recognizer start when the application is launched?")]
        public bool AutoStart = true;

        [Tooltip("The source of the microphone input.")]
        public VoiceProcessor VoiceProcessor;

        // Events
        public Action<string> OnStatusUpdated;
        public Action<string> OnTranscriptionResult;

        // Runtime state
        private bool _running;
        private readonly System.Collections.Concurrent.ConcurrentQueue<string> _threadedResultQueue = new System.Collections.Concurrent.ConcurrentQueue<string>();

        private readonly object _pythonBufferLock = new object();
        private readonly List<short> _pythonBuffer = new List<short>();
        private bool _pythonSegmentActive;
        private float _pythonSegmentStartTime;
        private bool _pythonForceFlushRequested;
        private bool _pythonRequestInFlight;
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
        }

        void Start()
        {
                if (AutoStart)
                {
                        StartWhisperStt();
                }
        }

        public void StartWhisperStt()
        {
                StartCoroutine(StartPythonStt());
        }

        private IEnumerator StartPythonStt()
        {
                yield return WaitForMicrophoneInput();

                OnStatusUpdated?.Invoke("Initialising Python speech service");

                VoiceProcessor.OnFrameCaptured += VoiceProcessorOnOnFrameCaptured;
                VoiceProcessor.OnRecordingStop += VoiceProcessorOnOnRecordingStop;
                VoiceProcessor.OnRecordingStart += VoiceProcessorOnRecordingStart;

                if (PythonMaxRecordLength > 0f)
                {
                        // allow external override before start
                }

                OnStatusUpdated?.Invoke("Python speech service ready");
                ToggleRecording();
        }

        private IEnumerator WaitForMicrophoneInput()
        {
                while (Microphone.devices.Length <= 0)
                        yield return null;
        }

        public void ToggleRecording()
        {
                if (!VoiceProcessor.IsRecording)
                {
                        _running = true;
                        ClearPythonBuffer();
                        var sampleRate = VoiceProcessor.SampleRate > 0 ? VoiceProcessor.SampleRate : 16000;
                        var frameLength = PythonFrameLength > 0 ? PythonFrameLength : (VoiceProcessor.FrameLength > 0 ? VoiceProcessor.FrameLength : 512);
                        VoiceProcessor.StartRecording(sampleRate, frameLength, true);
                }
                else
                {
                        _running = false;
                        VoiceProcessor.StopRecording();
                }
        }

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

        private void VoiceProcessorOnOnFrameCaptured(short[] samples)
        {
                lock (_pythonBufferLock)
                {
                        _pythonBuffer.AddRange(samples);
                }

                if (!_pythonSegmentActive)
                {
                        _pythonSegmentActive = true;
                        _pythonSegmentStartTime = Time.realtimeSinceStartup;
                }

                if (PythonMaxRecordLength > 0 && _pythonSegmentActive)
                {
                        var elapsed = Time.realtimeSinceStartup - _pythonSegmentStartTime;
                        if (elapsed >= PythonMaxRecordLength)
                        {
                                _pythonForceFlushRequested = true;
                        }
                }
        }

        private void VoiceProcessorOnRecordingStart()
        {
                ClearPythonBuffer();
                _pythonSegmentActive = false;
                _pythonSegmentStartTime = 0f;
        }

        private void VoiceProcessorOnOnRecordingStop()
        {
                StartCoroutine(HandlePythonRecordingStop(_running));
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

                if (restartRecording && _running)
                {
                        var sampleRate = VoiceProcessor.SampleRate > 0 ? VoiceProcessor.SampleRate : 16000;
                        var frameLength = PythonFrameLength > 0 ? PythonFrameLength : (VoiceProcessor.FrameLength > 0 ? VoiceProcessor.FrameLength : 512);
                        VoiceProcessor.StartRecording(sampleRate, frameLength, true);
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


