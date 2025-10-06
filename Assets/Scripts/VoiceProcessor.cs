/* * * * *
 * A unity voice processor
 * ------------------------------
 * 
 * A Unity script for recording and delivering frames of audio for real-time processing
 * 
 * Written by Picovoice 
 * 2021-02-19
 * 
 * Apache License
 * 
 * Copyright (c) 2021 Picovoice
 * 
 * Licensed under the Apache License, Version 2.0 (the "License");
 *   you may not use this file except in compliance with the License.
 *   You may obtain a copy of the License at
 *   
 *   http://www.apache.org/licenses/LICENSE-2.0
 *   
 *   Unless required by applicable law or agreed to in writing, software
 *   distributed under the License is distributed on an "AS IS" BASIS,
 *   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 *   See the License for the specific language governing permissions and
 *   limitations under the License.
 * 
 * * * * */
using System;
using System.Collections;
using System.Collections.Generic;
using UnityEngine;


/// <summary>
/// Class that records audio and delivers frames for real-time audio processing
/// </summary>
public class VoiceProcessor : MonoBehaviour
{
    /// <summary>
    /// Indicates whether microphone is capturing or not
    /// </summary>
    public bool IsRecording
    {
        get { return _audioClip != null && Microphone.IsRecording(CurrentDeviceName); }
    }

    [SerializeField] private int MicrophoneIndex;

    /// <summary>
    /// Sample rate of recorded audio
    /// </summary>
    public int SampleRate { get; private set; }

    /// <summary>
    /// Size of audio frames that are delivered
    /// </summary>
    public int FrameLength { get; private set; }

    /// <summary>
    /// Event where frames of audio are delivered
    /// </summary>
    public event Action<short[]> OnFrameCaptured;

    /// <summary>
    /// Event when audio capture thread stops
    /// </summary>
    public event Action OnRecordingStop;

    /// <summary>
    /// Event when audio capture thread starts
    /// </summary>
    public event Action OnRecordingStart;

    /// <summary>
    /// Available audio recording devices
    /// </summary>
    public List<string> Devices { get; private set; }

    /// <summary>
    /// Index of selected audio recording device
    /// </summary>
    public int CurrentDeviceIndex { get; private set; }

    /// <summary>
    /// Name of selected audio recording device
    /// </summary>
    public string CurrentDeviceName
    {
        get
        {
            if (CurrentDeviceIndex < 0 || CurrentDeviceIndex >= Microphone.devices.Length)
                return string.Empty;
            return Devices[CurrentDeviceIndex];
        }
    }

    [Header("Voice Detection Settings")]
    [SerializeField, Tooltip("The minimum peak amplitude required before voice activity can start"), Range(0.0f, 1.0f)]
    private float _minimumSpeakingSampleValue = 0.05f;

    [SerializeField, Tooltip("Seconds spent sampling ambient noise to determine an adaptive threshold"), Range(0.1f, 5.0f)]
    private float _noiseCalibrationDuration = 1.5f;

    [SerializeField, Tooltip("Multiplier applied to the ambient-noise standard deviation when computing the speech threshold"), Range(0.0f, 10.0f)]
    private float _noiseStdDevMultiplier = 2.5f;

    [SerializeField, Tooltip("Seconds of sustained speech energy required before voice is considered active"), Range(0.0f, 2.0f)]
    private float _speechActivationHoldDuration = 0.25f;

    [SerializeField, Tooltip("Seconds of sustained silence required before voice is considered inactive"), Range(0.0f, 2.0f)]
    private float _speechDeactivationHoldDuration = 0.45f;

    [SerializeField, Tooltip("Auto detect speech using the adaptive voice activity detector.")]
    private bool _autoDetect;

    private bool _audioDetected;
    private bool _didDetect;
    private bool _transmit;

    private bool _noiseCalibrationCompleted;
    private float _noiseCalibrationStartTime;
    private int _noiseSampleCount;
    private double _noiseMeanRms;
    private double _noiseM2;
    private float _currentVoiceThreshold;
    private float _vadAttackTimer;
    private float _vadReleaseTimer;


    AudioClip _audioClip;
    private event Action RestartRecording;

    void Awake()
    {
        UpdateDevices();
    }
#if UNITY_EDITOR
    void Update()
    {
        if (CurrentDeviceIndex != MicrophoneIndex)
        {
            ChangeDevice(MicrophoneIndex);
        }
    }
#endif

    /// <summary>
    /// Updates list of available audio devices
    /// </summary>
    public void UpdateDevices()
    {
        Devices = new List<string>();
        foreach (var device in Microphone.devices)
            Devices.Add(device);

        if (Devices == null || Devices.Count == 0)
        {
            CurrentDeviceIndex = -1;
            Debug.LogError("There is no valid recording device connected");
            return;
        }

        CurrentDeviceIndex = MicrophoneIndex;
    }

    /// <summary>
    /// Change audio recording device
    /// </summary>
    /// <param name="deviceIndex">Index of the new audio capture device</param>
    public void ChangeDevice(int deviceIndex)
    {
        if (deviceIndex < 0 || deviceIndex >= Devices.Count)
        {
            Debug.LogError(string.Format("Specified device index {0} is not a valid recording device", deviceIndex));
            return;
        }

        if (IsRecording)
        {
            // one time event to restart recording with the new device 
            // the moment the last session has completed
            RestartRecording += () =>
            {
                CurrentDeviceIndex = deviceIndex;
                StartRecording(SampleRate, FrameLength);
                RestartRecording = null;
            };
            StopRecording();
        }
        else
        {
            CurrentDeviceIndex = deviceIndex;
        }
    }

    /// <summary>
    /// Start recording audio
    /// </summary>
    /// <param name="sampleRate">Sample rate to record at</param>
    /// <param name="frameSize">Size of audio frames to be delivered</param>
    /// <param name="autoDetect">Should the audio continuously record based on the volume</param>
    public void StartRecording(int sampleRate = 16000, int frameSize = 512, bool ?autoDetect = null)
    {
        if (autoDetect != null)
        {
            _autoDetect = (bool) autoDetect;
        }

        if (IsRecording)
        {
            // if sample rate or frame size have changed, restart recording
            if (sampleRate != SampleRate || frameSize != FrameLength)
            {
                RestartRecording += () =>
                {
                    StartRecording(SampleRate, FrameLength, autoDetect);
                    RestartRecording = null;
                };
                StopRecording();
            }

            return;
        }

        SampleRate = sampleRate;
        FrameLength = frameSize;

        _audioClip = Microphone.Start(CurrentDeviceName, true, 1, sampleRate);

        _noiseCalibrationCompleted = false;
        _noiseCalibrationStartTime = Time.time;
        _noiseSampleCount = 0;
        _noiseMeanRms = 0.0;
        _noiseM2 = 0.0;
        _currentVoiceThreshold = Mathf.Max(0.0001f, _minimumSpeakingSampleValue);
        _vadAttackTimer = 0f;
        _vadReleaseTimer = 0f;
        _audioDetected = false;
        _didDetect = false;
        _transmit = false;

        StartCoroutine(RecordData());
    }

    /// <summary>
    /// Stops recording audio
    /// </summary>
    public void StopRecording()
    {
        if (!IsRecording)
            return;

        Microphone.End(CurrentDeviceName);
        Destroy(_audioClip);
        _audioClip = null;
        _didDetect = false;
        _audioDetected = false;

        StopCoroutine(RecordData());
    }

    private void UpdateAdaptiveVoiceThreshold(float rms)
    {
        if (!_autoDetect)
        {
            return;
        }

        if (!_noiseCalibrationCompleted)
        {
            UpdateNoiseStatistics(rms);
            if (Time.time - _noiseCalibrationStartTime >= _noiseCalibrationDuration)
            {
                _noiseCalibrationCompleted = true;
            }
        }
        else if (rms < _currentVoiceThreshold)
        {
            UpdateNoiseStatistics(rms);
        }

        _currentVoiceThreshold = Mathf.Clamp(CalculateAdaptiveThreshold(), _minimumSpeakingSampleValue, 1f);
    }

    private void UpdateNoiseStatistics(float rms)
    {
        rms = Mathf.Clamp01(rms);
        _noiseSampleCount++;
        double delta = rms - _noiseMeanRms;
        _noiseMeanRms += delta / _noiseSampleCount;
        double delta2 = rms - _noiseMeanRms;
        _noiseM2 += delta * delta2;
    }

    private float CalculateAdaptiveThreshold()
    {
        if (_noiseSampleCount <= 0)
        {
            return Mathf.Max(_minimumSpeakingSampleValue, _currentVoiceThreshold);
        }

        float mean = Mathf.Clamp01((float)_noiseMeanRms);
        if (_noiseSampleCount == 1)
        {
            float provisional = mean + mean * _noiseStdDevMultiplier;
            return Mathf.Max(_minimumSpeakingSampleValue, provisional);
        }

        float variance = (float)(_noiseM2 / (_noiseSampleCount - 1));
        float stdDev = Mathf.Sqrt(Mathf.Max(0f, variance));
        float threshold = mean + stdDev * _noiseStdDevMultiplier;
        if (!float.IsFinite(threshold))
        {
            threshold = _minimumSpeakingSampleValue;
        }

        return Mathf.Max(_minimumSpeakingSampleValue, threshold);
    }

    private float FrameDurationSeconds()
    {
        if (SampleRate <= 0)
        {
            return 0f;
        }

        return FrameLength / (float)SampleRate;
    }

    /// <summary>
    /// Loop for buffering incoming audio data and delivering frames
    /// </summary>
    IEnumerator RecordData()
    {
        float[] sampleBuffer = new float[FrameLength];
        int startReadPos = 0;

        if (OnRecordingStart != null)
            OnRecordingStart.Invoke();

        while (IsRecording)
        {
            int curClipPos = Microphone.GetPosition(CurrentDeviceName);
            if (curClipPos < startReadPos)
                curClipPos += _audioClip.samples;

            int samplesAvailable = curClipPos - startReadPos;
            if (samplesAvailable < FrameLength)
            {
                yield return null;
                continue;
            }

            int endReadPos = startReadPos + FrameLength;
            if (endReadPos > _audioClip.samples)
            {
                // fragmented read (wraps around to beginning of clip)
                // read bit at end of clip
                int numSamplesClipEnd = _audioClip.samples - startReadPos;
                float[] endClipSamples = new float[numSamplesClipEnd];
                _audioClip.GetData(endClipSamples, startReadPos);

                // read bit at start of clip
                int numSamplesClipStart = endReadPos - _audioClip.samples;
                float[] startClipSamples = new float[numSamplesClipStart];
                _audioClip.GetData(startClipSamples, 0);

                // combine to form full frame
                Buffer.BlockCopy(endClipSamples, 0, sampleBuffer, 0, numSamplesClipEnd);
                Buffer.BlockCopy(startClipSamples, 0, sampleBuffer, numSamplesClipEnd, numSamplesClipStart);
            }
            else
            {
                _audioClip.GetData(sampleBuffer, startReadPos);
            }

            startReadPos = endReadPos % _audioClip.samples;
            if (_autoDetect == false)
            {
                _transmit = _audioDetected = true;
            }
            else
            {
                double sumSquares = 0.0;
                float peakAmplitude = 0.0f;

                for (int i = 0; i < sampleBuffer.Length; i++)
                {
                    float amplitude = Mathf.Abs(sampleBuffer[i]);
                    sumSquares += amplitude * amplitude;
                    if (amplitude > peakAmplitude)
                    {
                        peakAmplitude = amplitude;
                    }
                }

                float rms = sampleBuffer.Length > 0 ? Mathf.Sqrt((float)(sumSquares / sampleBuffer.Length)) : 0f;

                UpdateAdaptiveVoiceThreshold(rms);

                float frameDuration = FrameDurationSeconds();
                float effectiveThreshold = Mathf.Max(_currentVoiceThreshold, _minimumSpeakingSampleValue);
                if (rms >= effectiveThreshold || peakAmplitude >= _minimumSpeakingSampleValue)
                {
                    _vadAttackTimer += frameDuration;
                    _vadReleaseTimer = 0f;
                }
                else
                {
                    _vadReleaseTimer += frameDuration;
                    _vadAttackTimer = 0f;
                }

                if (_audioDetected)
                {
                    if (_vadReleaseTimer >= _speechDeactivationHoldDuration)
                    {
                        _audioDetected = false;
                        _transmit = false;
                    }
                    else
                    {
                        _transmit = true;
                    }
                }
                else
                {
                    bool calibrationComplete = _noiseCalibrationCompleted || Time.time - _noiseCalibrationStartTime >= _noiseCalibrationDuration;
                    if (calibrationComplete)
                    {
                        _noiseCalibrationCompleted = true;
                    }

                    if (calibrationComplete && _vadAttackTimer >= _speechActivationHoldDuration)
                    {
                        _audioDetected = true;
                        _transmit = true;
                        _vadAttackTimer = 0f;
                    }
                    else
                    {
                        _transmit = false;
                    }
                }
            }

            if (_audioDetected)
            {
                _didDetect = true;
                // converts to 16-bit int samples
                short[] pcmBuffer = new short[sampleBuffer.Length];
                for (int i = 0; i < FrameLength; i++)
                {
                    pcmBuffer[i] = (short) Math.Floor(sampleBuffer[i] * short.MaxValue);
                }

                // raise buffer event
                if (OnFrameCaptured != null && _transmit)
                    OnFrameCaptured.Invoke(pcmBuffer);
            }
            else
            {
                if (_didDetect)
                {
                    if (OnRecordingStop != null)
                        OnRecordingStop.Invoke();
                    _didDetect = false;
                }
            }
        }


        if (OnRecordingStop != null)
            OnRecordingStop.Invoke();
        if (RestartRecording != null)
            RestartRecording.Invoke();
    }
}
