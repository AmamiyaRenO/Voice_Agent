using System;
using System.Collections;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Text;
using UnityEngine;
using UnityEngine.Networking;
using RobotVoice;
using RobotVoice.Audio;

public class UnitySpeechInputFallback : MonoBehaviour
{
        [Header("Python Speech Service")]

        [Tooltip("HTTP endpoint for the Python speech service transcribe API.")]
        public string PythonServiceUrl = VoiceAgentDefaults.AsrTranscribeUrl;

        [Tooltip("Optional language hint passed to the Python speech service (e.g. 'zh').")]
        public string PythonServiceLanguage = string.Empty;

        [Tooltip("Beam size used by the Python speech service.")]
        public int PythonServiceBeamSize = 5;

        [Tooltip("Minimum normalized amplitude required before audio is sent to the Python speech service (0-1).")]
        [Range(0f, 1f)]
        public float PythonServiceSilenceThreshold = 0.015f;

        [Tooltip("Max record length per segment when using the Python speech service (seconds).")]
        [Range(0.1f, 10f)]
        public float PythonMaxRecordLength = 4.0f;

        [Header("Segmentation (VAD + Max Duration)")]
        [Tooltip("Use voice activity detection to cut segments instead of only using fixed time windows.")]
        public bool UseVadSegmentation = true;

        [Tooltip("Sustained speech duration required to start a segment (seconds).")]
        [Range(0.01f, 1.5f)]
        public float VadActivationSeconds = 0.14f;

        [Tooltip("Sustained silence duration required to end a segment (seconds).")]
        [Range(0.05f, 2f)]
        public float VadSilenceSeconds = 0.45f;

        [Tooltip("Audio kept before speech start so the first syllable is not cut (seconds).")]
        [Range(0f, 0.8f)]
        public float VadPreRollSeconds = 0.2f;

        [Header("Long Utterance Stability")]
        [Tooltip("If enabled, auto-raise endpointing minima so long sentences are less likely to be cut and sent too early.")]
        public bool PreferLongUtteranceStability = true;

        [Tooltip("Minimum sustained silence required before ending a segment when stability mode is enabled.")]
        [Range(0.3f, 2f)]
        public float MinStableVadSilenceSeconds = 0.65f;

        [Tooltip("Minimum max segment duration used as a safety cap when stability mode is enabled.")]
        [Range(3f, 15f)]
        public float MinStableSegmentSeconds = 6.5f;

        [Tooltip("Once soft max length is reached, require at least this much silence before flushing the segment.")]
        [Range(0.05f, 0.8f)]
        public float StableFlushMinSilenceSeconds = 0.18f;

        [Tooltip("Absolute hard cap for one segment when the speaker keeps talking without silence.")]
        [Range(4f, 20f)]
        public float StableHardSegmentSeconds = 11f;

        [Tooltip("Start threshold multiplier relative to PythonServiceSilenceThreshold.")]
        [Range(1f, 2f)]
        public float VadStartThresholdMultiplier = 1.2f;

        [Tooltip("RMS gate scaling factor relative to the peak threshold.")]
        [Range(0.2f, 1f)]
        public float VadRmsThresholdScale = 0.6f;

        [Tooltip("Frame length for VoiceProcessor when using the Python speech service.")]
        public int PythonFrameLength = 256;

        [Tooltip("The source of the microphone input.")]
        public VoiceProcessor VoiceProcessor;

	[Tooltip("How long should we record before restarting?")]
	public float MaxRecordLength = 5;

	[Tooltip("Should the recognizer start when the application is launched?")]
	public bool AutoStart = false;

	//Called when the the state of the controller changes.
	public Action<string> OnStatusUpdated;

	//Called after a transcription is ready.
	public Action<string> OnTranscriptionResult;

        [Header("Diagnostics")]
        [Tooltip("If true, log record stop events (can be noisy because segmentation stops recording frequently).")]
        public bool VerboseStopLogging = false;
        [Tooltip("If true, emit status messages for silent/empty ASR segments (can be noisy).")]
        public bool LogNoSpeechStatusMessages = false;

        [Header("AEC (WebRTC APM/AEC3)")]
        [Tooltip("If enabled, microphone frames are processed through AEC before buffering/sending to ASR.")]
        public bool EnableAec = true;

        [Tooltip("Optional reference to the AEC engine. If null, the first AecEngine found in the scene will be used.")]
        public AecEngine AecEngine;

        [Tooltip("If true, dump mic/render/aec WAV files under Application.persistentDataPath\\aec_dumps.")]
        public bool AecDumpWav = false;

        [Tooltip("Maximum duration (seconds) to dump per session.")]
        [Range(1, 60)]
        public int AecDumpMaxSeconds = 10;

	private bool _isInitializing;

	private bool _didInit;

	//Threading Logic

	// Flag to signal we are ending
	private bool _running;

        //Thread safe queue of resuts
        private readonly ConcurrentQueue<string> _threadedResultQueue = new ConcurrentQueue<string>();

        // Python speech service state
        private bool _playbackMute; // when true, drop captured frames to avoid TTS feedback
        private readonly object _pythonBufferLock = new object();
        private readonly List<short> _pythonBuffer = new List<short>();
        private bool _pythonSegmentActive;
        private float _pythonSegmentStartTime;
        private bool _pythonForceFlushRequested;
        private bool _pythonRequestInFlight;
        private string _pythonPendingFlushReason = "unknown";
        private int _activeRecordingSampleRate = 16000;
        private float _vadSpeechTimerSec;
        private float _vadSilenceTimerSec;
        private readonly List<short> _vadPreRollBuffer = new List<short>();
        private float _defaultMaxRecordLength;
        private bool _defaultMaxRecordLengthCaptured;
        private bool _wakeWordOverrideActive;
        private bool _wakeWordPrimingStopPending;
        private float _pythonLastSegmentMaxAmplitude;
        private float _pythonLastSegmentRms;
        private bool _startListeningAfterInitialization = true;

        public bool IsListening => VoiceProcessor != null && VoiceProcessor.IsRecording;
        public bool IsInitialized => _didInit;
        public bool IsInitializing => _isInitializing;
        public bool IsSpeechSegmentActiveForDispatch
        {
                get
                {
                        lock (_pythonBufferLock)
                        {
                                return _pythonSegmentActive || _vadSpeechTimerSec > 0.02f;
                        }
                }
        }
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

                ApplyEndpointingStabilityDefaults();

                if (AecEngine == null)
                {
                        AecEngine = FindObjectOfType<AecEngine>();
                }
                if (EnableAec && AecEngine == null)
                {
                        var go = new GameObject("AecEngine");
                        AecEngine = go.AddComponent<AecEngine>();
                        DontDestroyOnLoad(go);
                }
        }

        private int PickBestNativeAecSampleRateHz()
        {
                // WebRTC APM supports native rates: 8k/16k/32k/48k. Prefer highest supported by mic device caps.
                try
                {
                        var device = VoiceProcessor != null ? VoiceProcessor.CurrentDeviceName : string.Empty;
                        Microphone.GetDeviceCaps(device, out int minFreq, out int maxFreq);
                        if (minFreq <= 0 && maxFreq <= 0)
                        {
                                // unknown caps: use Unity output sample rate if it's a native rate, else fallback 48k
                                var outSr = AudioSettings.outputSampleRate;
                                if (outSr == 48000 || outSr == 32000 || outSr == 16000 || outSr == 8000) return outSr;
                                return 48000;
                        }

                        int[] native = { 48000, 32000, 16000, 8000 };
                        foreach (var sr in native)
                        {
                                if (maxFreq > 0 && sr > maxFreq) continue;
                                if (minFreq > 0 && sr < minFreq) continue;
                                return sr;
                        }
                }
                catch { }
                return 32000;
        }

        private void GetRecordingFormat(out int sampleRate, out int frameLength)
        {
                sampleRate = VoiceProcessor != null && VoiceProcessor.SampleRate > 0 ? VoiceProcessor.SampleRate : 16000;
                frameLength = PythonFrameLength > 0 ? PythonFrameLength : (VoiceProcessor != null && VoiceProcessor.FrameLength > 0 ? VoiceProcessor.FrameLength : 512);

                if (EnableAec && AecEngine != null && AecEngine.enableAec)
                {
                        var sr = PickBestNativeAecSampleRateHz();
                        sampleRate = sr;
                        frameLength = Mathf.Max(80, sr / 100); // 10ms
                }
        }

        public bool HasActiveAec()
        {
                return EnableAec && AecEngine != null && AecEngine.enableAec && AecEngine.IsAvailable;
        }

	// If AutoStart is enabled, begin speech recognition on startup.
	void Start()
	{
		if (AutoStart)
		{
			StartSpeechRecognition();
		}
	}

	/// <summary>
	/// Start speech recognition (Python speech service).
	/// </summary>
	/// <param name="startMicrophone">Should the microphone start after initialization?</param>
        public void StartSpeechRecognition(bool startMicrophone = true)
        {
                if (_isInitializing)
                {
                        Debug.LogError("Initializing in progress!");
                        return;
		}
		if (_didInit)
		{
			Debug.LogError("Unity speech input has already been initialized!");
			return;
		}

                _startListeningAfterInitialization = startMicrophone;

                StartCoroutine(StartPythonStt(startMicrophone));
        }

        private IEnumerator StartPythonStt(bool startMicrophone)
        {
                _isInitializing = true;

                ApplyEndpointingStabilityDefaults();

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

                if (_startListeningAfterInitialization)
                {
                        ToggleRecording();
                }
        }

        private void ApplyEndpointingStabilityDefaults()
        {
                if (!PreferLongUtteranceStability)
                {
                        return;
                }

                var minSilence = Mathf.Clamp(MinStableVadSilenceSeconds, 0.3f, 2f);
                if (UseVadSegmentation && VadSilenceSeconds < minSilence)
                {
                        VadSilenceSeconds = minSilence;
                }

                var minSegment = Mathf.Clamp(MinStableSegmentSeconds, 3f, 15f);
                if (PythonMaxRecordLength > 0f && PythonMaxRecordLength < minSegment)
                {
                        PythonMaxRecordLength = minSegment;
                }

                if (MaxRecordLength > 0f && MaxRecordLength < minSegment)
                {
                        MaxRecordLength = minSegment;
                }

                var hardSegment = Mathf.Clamp(StableHardSegmentSeconds, 4f, 20f);
                StableHardSegmentSeconds = Mathf.Max(hardSegment, minSegment + 0.8f);
                StableFlushMinSilenceSeconds = Mathf.Clamp(StableFlushMinSilenceSeconds, 0.05f, 0.8f);
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
                        var aec = EnableAec ? AecEngine : null;
                        if (aec != null && aec.enableAec)
                        {
                                // Run AEC in 10ms native blocks at a rate supported by the mic device (commonly 32kHz).
                                var sr = PickBestNativeAecSampleRateHz();
                                var fl = Mathf.Max(80, sr / 100); // 10ms
                                aec.targetSampleRateHz = sr;
                                aec.blockMs = 10;
                                aec.captureChannels = 1;
                                aec.dumpWav = AecDumpWav;
                                aec.dumpMaxSeconds = Mathf.Clamp(AecDumpMaxSeconds, 1, 60);
                                var ok = aec.TryInit();
                                if (!ok)
                                {
                                        Debug.LogWarning($"[RobotVoice] AEC init failed. sr={sr} err={aec.LastInitError}");
                                        Debug.LogWarning($"[RobotVoice] WAV dumps (if enabled) would be under: {Application.persistentDataPath}\\aec_dumps");
                                }
                                else
                                {
                                        Debug.Log($"[RobotVoice] AEC init OK. sr={sr} delayMs={aec.streamDelayMs} renderTapSr={(aec.renderTap != null ? aec.renderTap.SampleRateHz : AudioSettings.outputSampleRate)}");
                                        if (AecDumpWav)
                                        {
                                                Debug.Log($"[RobotVoice] AEC WAV dumps enabled -> {Application.persistentDataPath}\\aec_dumps");
                                        }
                                }
                                VoiceProcessor.StartRecording(sr, fl, false);
                                _activeRecordingSampleRate = sr > 0 ? sr : 16000;
                                return;
                        }

                        GetRecordingFormat(out var sampleRate, out var frameLength);
                        VoiceProcessor.StartRecording(sampleRate, frameLength, false);
                        _activeRecordingSampleRate = sampleRate > 0 ? sampleRate : 16000;
                }
                else
                {
                        Debug.Log("Stop Recording");
                        _running = false;
                        VoiceProcessor.StopRecording();
                }
        }

        public void SetListeningEnabled(bool enabled)
        {
                if (enabled)
                {
                        _startListeningAfterInitialization = true;
                        if (_isInitializing)
                        {
                                return;
                        }

                        if (!_didInit)
                        {
                                StartSpeechRecognition(startMicrophone: true);
                                return;
                        }

                        if (!IsListening)
                        {
                                ToggleRecording();
                        }
                        return;
                }

                _startListeningAfterInitialization = false;
                _running = false;
                if (VoiceProcessor != null && VoiceProcessor.IsRecording)
                {
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
                                FlushCurrentSegment();
                        }
                }

                if (_threadedResultQueue.TryDequeue(out string voiceResult))
                {
                    OnTranscriptionResult?.Invoke(voiceResult);
                }
        }

        private int GetCurrentRecordingSampleRate()
        {
                if (_activeRecordingSampleRate > 0)
                {
                        return _activeRecordingSampleRate;
                }
                if (VoiceProcessor != null && VoiceProcessor.SampleRate > 0)
                {
                        return VoiceProcessor.SampleRate;
                }
                return 16000;
        }

        private static void AnalyzeAudioFrame(short[] samples, out float maxAmplitude, out float rms)
        {
                maxAmplitude = 0f;
                rms = 0f;
                if (samples == null || samples.Length == 0)
                {
                        return;
                }

                double sumSquares = 0.0;
                for (int i = 0; i < samples.Length; i++)
                {
                        float amplitude = Mathf.Abs(samples[i]) / 32768f;
                        if (amplitude > maxAmplitude)
                        {
                                maxAmplitude = amplitude;
                        }
                        sumSquares += amplitude * amplitude;
                }

                rms = Mathf.Sqrt((float)(sumSquares / samples.Length));
        }

        private void AppendPreRollLocked(short[] samples, int sampleRate)
        {
                if (!UseVadSegmentation || VadPreRollSeconds <= 0f || samples == null || samples.Length == 0)
                {
                        return;
                }

                _vadPreRollBuffer.AddRange(samples);
                int maxSamples = Mathf.Max(0, Mathf.RoundToInt(Mathf.Max(0f, VadPreRollSeconds) * sampleRate));
                if (maxSamples <= 0)
                {
                        _vadPreRollBuffer.Clear();
                        return;
                }

                int overflow = _vadPreRollBuffer.Count - maxSamples;
                if (overflow > 0)
                {
                        _vadPreRollBuffer.RemoveRange(0, overflow);
                }
        }

        private void ApplyWakeWordWindowBoundary()
        {
                if (!_wakeWordOverrideActive)
                {
                        return;
                }

                if (_wakeWordPrimingStopPending)
                {
                        _wakeWordPrimingStopPending = false;
                        return;
                }

                if (_defaultMaxRecordLengthCaptured)
                {
                        MaxRecordLength = _defaultMaxRecordLength;
                }
                _wakeWordOverrideActive = false;
        }

        private void FlushCurrentSegment()
        {
                short[] samples = null;
                var endpointReason = "unknown";
                lock (_pythonBufferLock)
                {
                        if (_pythonBuffer.Count > 0)
                        {
                                samples = _pythonBuffer.ToArray();
                                _pythonBuffer.Clear();
                        }
                        _pythonSegmentActive = false;
                        _pythonSegmentStartTime = 0f;
                        _vadSpeechTimerSec = 0f;
                        _vadSilenceTimerSec = 0f;
                        endpointReason = string.IsNullOrWhiteSpace(_pythonPendingFlushReason)
                                ? "unknown"
                                : _pythonPendingFlushReason;
                        _pythonPendingFlushReason = "unknown";
                }

                ApplyWakeWordWindowBoundary();

                if (samples != null && samples.Length > 0)
                {
                        StartCoroutine(SendAudioToPython(samples, endpointReason));
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

                if (samples == null || samples.Length == 0)
                {
                        return;
                }

                // If AEC is active, process the frame before buffering it for ASR.
                if (EnableAec && AecEngine != null && AecEngine.enableAec && AecEngine.IsAvailable)
                {
                        if (AecEngine.TryProcessCapturePcm16(samples, out var processed) && processed != null)
                        {
                                samples = processed;
                        }
                }

                int sampleRate = GetCurrentRecordingSampleRate();
                float frameDurationSec = sampleRate > 0 ? (samples.Length / (float)sampleRate) : 0f;
                AnalyzeAudioFrame(samples, out var framePeak, out var frameRms);

                float endThreshold = Mathf.Max(0.001f, PythonServiceSilenceThreshold);
                float startThreshold = Mathf.Max(endThreshold, endThreshold * Mathf.Max(1f, VadStartThresholdMultiplier));
                float rmsScale = Mathf.Clamp(VadRmsThresholdScale, 0.2f, 1f);

                bool isSpeechFrame = framePeak >= startThreshold || frameRms >= (startThreshold * rmsScale);
                bool isSilenceFrame = framePeak < endThreshold && frameRms < (endThreshold * rmsScale);

                bool shouldFlush = false;
                var flushReason = string.Empty;

                lock (_pythonBufferLock)
                {
                        if (UseVadSegmentation)
                        {
                                if (!_pythonSegmentActive)
                                {
                                        if (isSpeechFrame)
                                        {
                                                _vadSpeechTimerSec += frameDurationSec;
                                        }
                                        else
                                        {
                                                _vadSpeechTimerSec = 0f;
                                        }

                                        if (_vadSpeechTimerSec >= Mathf.Max(0.01f, VadActivationSeconds))
                                        {
                                                _pythonSegmentActive = true;
                                                _pythonSegmentStartTime = Time.realtimeSinceStartup;
                                                _pythonBuffer.Clear();
                                                if (_vadPreRollBuffer.Count > 0)
                                                {
                                                        _pythonBuffer.AddRange(_vadPreRollBuffer);
                                                }
                                                _vadSpeechTimerSec = 0f;
                                                _vadSilenceTimerSec = 0f;
                                        }
                                }
                        }
                        else if (!_pythonSegmentActive)
                        {
                                _pythonSegmentActive = true;
                                _pythonSegmentStartTime = Time.realtimeSinceStartup;
                        }

                        if (_pythonSegmentActive)
                        {
                                _pythonBuffer.AddRange(samples);

                                if (UseVadSegmentation)
                                {
                                        if (isSilenceFrame)
                                        {
                                                _vadSilenceTimerSec += frameDurationSec;
                                        }
                                        else
                                        {
                                                _vadSilenceTimerSec = 0f;
                                        }

                                        if (VadSilenceSeconds > 0f && _vadSilenceTimerSec >= VadSilenceSeconds)
                                        {
                                                shouldFlush = true;
                                                flushReason = "vad_silence";
                                        }
                                }

                                if (MaxRecordLength > 0f)
                                {
                                        var elapsed = Time.realtimeSinceStartup - _pythonSegmentStartTime;
                                        if (UseVadSegmentation && PreferLongUtteranceStability)
                                        {
                                                var softLimit = Mathf.Max(
                                                        MaxRecordLength,
                                                        Mathf.Clamp(MinStableSegmentSeconds, 3f, 15f));
                                                var hardLimit = Mathf.Max(
                                                        softLimit + 0.8f,
                                                        Mathf.Clamp(StableHardSegmentSeconds, 4f, 20f));
                                                if (elapsed >= softLimit)
                                                {
                                                        var minTailSilence = Mathf.Clamp(StableFlushMinSilenceSeconds, 0.05f, 0.8f);
                                                        if (_vadSilenceTimerSec >= minTailSilence || elapsed >= hardLimit)
                                                        {
                                                                shouldFlush = true;
                                                                flushReason = elapsed >= hardLimit
                                                                        ? "max_length_hard"
                                                                        : "max_length_soft";
                                                        }
                                                }
                                        }
                                        else if (elapsed >= MaxRecordLength)
                                        {
                                                shouldFlush = true;
                                                flushReason = "max_length";
                                        }
                                }
                        }

                        AppendPreRollLocked(samples, sampleRate);
                }

                if (shouldFlush)
                {
                        if (string.IsNullOrWhiteSpace(flushReason))
                        {
                                flushReason = "unknown";
                        }
                        _pythonPendingFlushReason = flushReason;
                        _pythonForceFlushRequested = true;
                }
        }

	//Callback from the voice processor when recording stops
        private void VoiceProcessorOnRecordingStart()
        {
                ClearPythonBuffer();
        }

        private void VoiceProcessorOnOnRecordingStop()
        {
                StartCoroutine(HandlePythonRecordingStop(_running));
                if (VerboseStopLogging)
                {
                        Debug.Log("[UnitySpeechInput] Recording stopped");
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
                        GetRecordingFormat(out var sampleRate, out var frameLength);
                        VoiceProcessor.StartRecording(sampleRate, frameLength, false);
                        _activeRecordingSampleRate = sampleRate > 0 ? sampleRate : 16000;
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
                        _vadPreRollBuffer.Clear();
                        _vadSpeechTimerSec = 0f;
                        _vadSilenceTimerSec = 0f;
                        _pythonPendingFlushReason = "unknown";
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

                        _vadPreRollBuffer.Clear();
                        _vadSpeechTimerSec = 0f;
                        _vadSilenceTimerSec = 0f;
                        _pythonSegmentActive = false;
                        _pythonSegmentStartTime = 0f;
                }

                if (restartRecording && _running)
                {
                        GetRecordingFormat(out var sampleRate, out var frameLength);
                        VoiceProcessor.StartRecording(sampleRate, frameLength, false);
                        _activeRecordingSampleRate = sampleRate > 0 ? sampleRate : 16000;
                }

                ApplyWakeWordWindowBoundary();

                // Do not block restart on HTTP round-trip; otherwise we create
                // dead-air gaps and lose words between chunks.
                if (samples != null && samples.Length > 0)
                {
                        StartCoroutine(SendAudioToPython(samples, "recording_stop"));
                }

                yield break;
        }

        private IEnumerator SendAudioToPython(short[] samples, string endpointReason = "unknown")
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
                        if (LogNoSpeechStatusMessages)
                        {
                                OnStatusUpdated?.Invoke("Python speech service skipped silent audio");
                        }
                        _pythonLastSegmentMaxAmplitude = 0f;
                        _pythonLastSegmentRms = 0f;
                        yield break;
                }

                _pythonLastSegmentMaxAmplitude = maxAmplitude;
                _pythonLastSegmentRms = rms;

                var payload = new byte[samples.Length * sizeof(short)];
                Buffer.BlockCopy(samples, 0, payload, 0, payload.Length);

                var sampleRate = _activeRecordingSampleRate > 0 ? _activeRecordingSampleRate : 16000;
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
                                        var enriched = InjectPythonSegmentMetrics(
                                                response,
                                                _pythonLastSegmentMaxAmplitude,
                                                _pythonLastSegmentRms,
                                                endpointReason);
                                        _threadedResultQueue.Enqueue(enriched);
                                }
                                else
                                {
                                        if (LogNoSpeechStatusMessages)
                                        {
                                                OnStatusUpdated?.Invoke("Python speech service returned empty result");
                                        }
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

        // Allow external components to temporarily mute microphone capture while local audio is playing.
        public void SetPlaybackMute(bool value)
        {
                _playbackMute = value;
        }

        private string InjectPythonSegmentMetrics(string json, float maxAmplitude, float rms, string endpointReason)
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
                        if (!string.IsNullOrWhiteSpace(endpointReason))
                        {
                                obj["endpoint_reason"] = endpointReason.Trim().ToLowerInvariant();
                        }

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

                // Stable session id so the Python service can apply streaming overlap/context.
                try
                {
                        var sid = SystemInfo.deviceUniqueIdentifier;
                        if (!string.IsNullOrWhiteSpace(sid))
                        {
                                builder.Append("&session_id=");
                                builder.Append(UnityWebRequest.EscapeURL(sid));
                        }
                }
                catch { }

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

