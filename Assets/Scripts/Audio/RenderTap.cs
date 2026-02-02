using System;
using UnityEngine;

namespace RobotVoice.Audio
{
    /// <summary>
    /// Taps the Unity audio render stream (what is being played) via OnAudioFilterRead and stores it
    /// into a thread-safe ring buffer so other components (e.g., AEC) can consume fixed-size frames.
    ///
    /// Attach this component to the GameObject that has the active AudioListener (typically Main Camera).
    /// </summary>
    public sealed class RenderTap : MonoBehaviour
    {
        [Header("Buffer")]
        [Tooltip("How many seconds of render audio to keep buffered.")]
        [Range(0.25f, 5f)]
        public float bufferSeconds = 2.0f;

        [Header("Diagnostics")]
        [Tooltip("If true, logs periodic render stats (RMS/max).")]
        public bool verboseLogging = true;

        private readonly object _lock = new object();
        private float[] _ring;
        private int _ringWrite;
        private int _ringRead;
        private int _ringCount;
        private int _channels;
        private int _sampleRate;
        private float _lastLogTime;
        private bool _loggedFirstCallback;
        private volatile float _lastRms;
        private volatile float _lastPeak;
        private volatile int _lastChannels;
        private volatile int _lastSampleRate;
        private volatile bool _hasStats;

        public int SampleRateHz => _sampleRate;
        public int Channels => _channels;

        public int AvailableSamples
        {
            get
            {
                lock (_lock)
                {
                    return _ringCount;
                }
            }
        }

        private void Awake()
        {
            _sampleRate = AudioSettings.outputSampleRate;
            EnsureBufferAllocated(channels: 2);
        }

        private void EnsureBufferAllocated(int channels)
        {
            var seconds = Mathf.Max(0.25f, bufferSeconds);
            var sr = _sampleRate > 0 ? _sampleRate : 48000;
            var cap = Mathf.CeilToInt(sr * seconds) * Mathf.Max(1, channels);
            cap = Mathf.Max(4096, cap);

            lock (_lock)
            {
                _channels = Mathf.Max(1, channels);
                _ring = new float[cap];
                _ringWrite = 0;
                _ringRead = 0;
                _ringCount = 0;
            }
        }

        /// <summary>
        /// Try to dequeue exactly <paramref name="count"/> samples (interleaved) into <paramref name="dst"/>.
        /// Returns false if there are not enough samples buffered yet.
        /// </summary>
        public bool TryDequeue(int count, float[] dst, int dstOffset = 0)
        {
            if (dst == null) throw new ArgumentNullException(nameof(dst));
            if (count <= 0) return false;
            if (dstOffset < 0 || dstOffset + count > dst.Length) throw new ArgumentOutOfRangeException(nameof(dstOffset));

            lock (_lock)
            {
                if (_ring == null || _ringCount < count)
                {
                    return false;
                }

                var capacity = _ring.Length;
                var remaining = count;
                var src = _ringRead;
                var dstIndex = dstOffset;

                while (remaining > 0)
                {
                    var chunk = Math.Min(remaining, capacity - src);
                    Array.Copy(_ring, src, dst, dstIndex, chunk);
                    src = (src + chunk) % capacity;
                    dstIndex += chunk;
                    remaining -= chunk;
                }

                _ringRead = src;
                _ringCount -= count;
                return true;
            }
        }

        private void OnAudioFilterRead(float[] data, int channels)
        {
            if (data == null || data.Length == 0)
            {
                return;
            }

            // If channel count changes (rare), reset the buffer to avoid misaligned interleaving.
            if (channels != _channels || _ring == null)
            {
                EnsureBufferAllocated(channels);
            }

            if (verboseLogging)
            {
                // Compute simple render energy stats to verify the tap is actually receiving audio.
                double sumSquares = 0.0;
                float peak = 0f;
                for (int i = 0; i < data.Length; i++)
                {
                    var a = Mathf.Abs(data[i]);
                    sumSquares += a * a;
                    if (a > peak) peak = a;
                }
                var rms = data.Length > 0 ? Mathf.Sqrt((float)(sumSquares / data.Length)) : 0f;
                // NOTE: OnAudioFilterRead runs on the audio thread. Do not call Unity time APIs here.
                _lastRms = rms;
                _lastPeak = peak;
                _lastChannels = channels;
                // Audio thread: do not call AudioSettings here.
                _lastSampleRate = _sampleRate;
                _hasStats = true;
            }

            lock (_lock)
            {
                if (_ring == null)
                {
                    return;
                }

                var capacity = _ring.Length;
                for (int i = 0; i < data.Length; i++)
                {
                    _ring[_ringWrite] = data[i];
                    _ringWrite = (_ringWrite + 1) % capacity;

                    if (_ringCount < capacity)
                    {
                        _ringCount++;
                    }
                    else
                    {
                        // Overwrite oldest data when buffer is full.
                        _ringRead = (_ringRead + 1) % capacity;
                    }
                }
            }
        }

        private void Update()
        {
            // Main thread only: keep sample rate in sync if Unity output SR changes.
            var sr = AudioSettings.outputSampleRate;
            if (sr > 0 && sr != _sampleRate)
            {
                _sampleRate = sr;
                EnsureBufferAllocated(_channels > 0 ? _channels : 2);
            }

            if (!verboseLogging || !_hasStats)
            {
                return;
            }

            var now = Time.realtimeSinceStartup;
            if (!_loggedFirstCallback)
            {
                _loggedFirstCallback = true;
                Debug.Log($"[RenderTap] First audio callback on '{gameObject.name}'. sr={_lastSampleRate} ch={_lastChannels} rms={_lastRms:0.0000} max={_lastPeak:0.0000}");
            }

            if (now - _lastLogTime > 2.0f)
            {
                _lastLogTime = now;
                Debug.Log($"[RenderTap] '{gameObject.name}' sr={_lastSampleRate} ch={_lastChannels} rms={_lastRms:0.0000} max={_lastPeak:0.0000} bufferedSamples={AvailableSamples}");
            }
        }
    }
}

