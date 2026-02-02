using System;
using UnityEngine;

namespace RobotVoice.Audio
{
    /// <summary>
    /// Unity-side AEC engine using a native WebRTC AudioProcessing (APM/AEC3) plugin.
    /// Consumes render reference audio from <see cref="RenderTap"/> and processes microphone frames.
    /// </summary>
    public sealed class AecEngine : MonoBehaviour
    {
        [Header("Enable")]
        [Tooltip("If false, AEC is bypassed.")]
        public bool enableAec = true;

        [Header("Signal routing")]
        [Tooltip("Render reference tap. If null, the engine will try to attach one to the active AudioListener.")]
        public RenderTap renderTap;

        [Header("Format")]
        [Tooltip("Capture channels. Microphone capture in this project is mono.")]
        [Range(1, 2)]
        public int captureChannels = 1;

        [Tooltip("If 0, uses AudioSettings.outputSampleRate.")]
        public int targetSampleRateHz = 0;

        [Tooltip("AEC block size in milliseconds. WebRTC APM is happiest with 10ms blocks.")]
        [Range(5, 20)]
        public int blockMs = 10;

        [Header("Alignment")]
        [Tooltip("Stream delay (ms) used by APM to align render reference with capture.")]
        [Range(0, 300)]
        public int streamDelayMs = 120;

        [Tooltip("If true, automatically estimates streamDelayMs by correlating render vs mic.")]
        public bool autoTuneDelay = true;

        [Tooltip("How often to auto-tune delay (seconds).")]
        [Range(1, 10)]
        public int autoTuneEverySeconds = 2;

        [Tooltip("Max delay (ms) to search when auto-tuning.")]
        [Range(50, 300)]
        public int autoTuneMaxDelayMs = 200;

        [Header("Diagnostics")]
        [Tooltip("If true, dumps short WAV files (mic/render/aec) under Application.persistentDataPath.")]
        public bool dumpWav = false;

        [Tooltip("Maximum duration (seconds) to dump per session.")]
        [Range(1, 60)]
        public int dumpMaxSeconds = 10;

        public bool IsAvailable => _available;
        public string LastInitError => _lastInitError;
        public int SampleRateHz => targetSampleRateHz > 0 ? targetSampleRateHz : AudioSettings.outputSampleRate;
        public int BlockSamplesPerChannel => Mathf.Max(1, (SampleRateHz * Mathf.Max(1, blockMs)) / 1000);

        private IntPtr _apm;
        private bool _available;
        private string _lastInitError = string.Empty;
        private int _renderChannels;

        // Render reference as required by APM (target sample rate, interleaved).
        private float[] _renderFrame;
        // Raw render samples dequeued from RenderTap (output sample rate, interleaved).
        private float[] _renderTapFrame;
        private float[] _captureFrame;
        private float[] _captureOut;
        private short[] _renderTmpPcm16;

        private WavFileWriter _dumpMic;
        private WavFileWriter _dumpRender;
        private WavFileWriter _dumpAec;
        private int _dumpSamplesWrittenPerChannel;
        private int _failReverseCount;
        private int _failProcessCount;
        private int _failSizeCount;
        private float _lastErrorLogTime;
        private float _lastAutoTuneTime;

        // History buffers for delay auto-tuning (mono at SampleRateHz).
        private float[] _histRenderMono;
        private float[] _histMicMono;
        private int _histWrite;
        private int _histCount;
        private float[] _renderMonoBlock;

        private void Awake()
        {
            // Always show where dumps would go (helps users find the folder even if AEC fails).
            if (dumpWav)
            {
                Debug.Log($"[AEC] dumpWav=true. persistentDataPath={Application.persistentDataPath}");
            }

            // Diagnostics: show all listeners and which one we attach to.
            try
            {
                var listeners = FindObjectsOfType<AudioListener>(true);
                if (listeners != null)
                {
                    for (int i = 0; i < listeners.Length; i++)
                    {
                        var l = listeners[i];
                        if (l == null) continue;
                        Debug.Log($"[AEC] AudioListener[{i}] name='{l.gameObject.name}' enabled={l.enabled} activeInHierarchy={l.gameObject.activeInHierarchy}");
                    }
                }
            }
            catch { }

            if (renderTap == null)
            {
                var listener = FindObjectOfType<AudioListener>();
                if (listener != null)
                {
                    renderTap = listener.GetComponent<RenderTap>();
                    if (renderTap == null)
                    {
                        renderTap = listener.gameObject.AddComponent<RenderTap>();
                    }
                    Debug.Log($"[AEC] Using AudioListener '{listener.gameObject.name}' for RenderTap. RenderTapOn='{renderTap.gameObject.name}'");
                }
                else
                {
                    Debug.LogWarning("[AEC] No AudioListener found in scene; RenderTap cannot capture render reference.");
                }
            }

            TryInit();
        }

        private void OnDestroy()
        {
            Shutdown();
        }

        public void Shutdown()
        {
            StopDump();
            if (_apm != IntPtr.Zero)
            {
                try { ApmNative.apm_destroy(_apm); }
                catch { /* ignored */ }
                _apm = IntPtr.Zero;
            }
            _available = false;
        }

        public bool TryInit()
        {
            Shutdown();
            _lastInitError = string.Empty;

            if (!enableAec)
            {
                _available = false;
                return false;
            }

            // Render channels will be known after the first audio callback; assume stereo by default.
            _renderChannels = renderTap != null && renderTap.Channels > 0 ? renderTap.Channels : 2;

            try
            {
                _apm = ApmNative.apm_create(SampleRateHz, _renderChannels, captureChannels);
                if (_apm == IntPtr.Zero)
                {
                    _available = false;
                    _lastInitError = "apm_create returned NULL (native init failed)";
                    return false;
                }
                ApmNative.apm_set_stream_delay_ms(_apm, Mathf.Max(0, streamDelayMs));
                _available = true;
                EnsureBuffers();
                return true;
            }
            catch (DllNotFoundException)
            {
                _available = false;
                _apm = IntPtr.Zero;
                _lastInitError = "DllNotFoundException: webrtc_apm_unity.dll not found or dependency DLLs missing";
                return false;
            }
            catch (EntryPointNotFoundException)
            {
                _available = false;
                _apm = IntPtr.Zero;
                _lastInitError = "EntryPointNotFoundException: DLL exports do not match (wrong DLL build/name)";
                return false;
            }
            catch
            {
                _available = false;
                _apm = IntPtr.Zero;
                _lastInitError = "Unknown exception during AEC init";
                return false;
            }
        }

        private void EnsureBuffers()
        {
            var targetSpc = BlockSamplesPerChannel;
            _renderChannels = renderTap != null && renderTap.Channels > 0 ? renderTap.Channels : _renderChannels;
            _renderChannels = Mathf.Max(1, _renderChannels);

            var renderLen = targetSpc * _renderChannels;
            var capLen = targetSpc * Mathf.Max(1, captureChannels);

            if (_renderFrame == null || _renderFrame.Length != renderLen) _renderFrame = new float[renderLen];
            var tapSr = renderTap != null ? renderTap.SampleRateHz : AudioSettings.outputSampleRate;
            var tapSpc = Mathf.Max(1, (tapSr * Mathf.Max(1, blockMs)) / 1000);
            var tapLen = tapSpc * _renderChannels;
            if (_renderTapFrame == null || _renderTapFrame.Length != tapLen) _renderTapFrame = new float[tapLen];
            if (_captureFrame == null || _captureFrame.Length != capLen) _captureFrame = new float[capLen];
            if (_captureOut == null || _captureOut.Length != capLen) _captureOut = new float[capLen];
            if (_renderTmpPcm16 == null || _renderTmpPcm16.Length != renderLen) _renderTmpPcm16 = new short[renderLen];
            if (_renderMonoBlock == null || _renderMonoBlock.Length != targetSpc) _renderMonoBlock = new float[targetSpc];

            // 1 second history for auto-tune (mono)
            var histLen = Mathf.Max(8000, SampleRateHz); // at least 1s at current SR
            if (_histRenderMono == null || _histRenderMono.Length != histLen)
            {
                _histRenderMono = new float[histLen];
                _histMicMono = new float[histLen];
                _histWrite = 0;
                _histCount = 0;
            }
        }

        private void PushHistoryMono(float[] renderInterleaved, int renderChannels, float[] micMono, int samplesPerChannel)
        {
            if (_histRenderMono == null || _histMicMono == null) return;
            if (samplesPerChannel <= 0) return;

            // Mix render to mono for correlation
            if (_renderMonoBlock == null || _renderMonoBlock.Length != samplesPerChannel)
            {
                _renderMonoBlock = new float[samplesPerChannel];
            }
            if (renderInterleaved != null && renderInterleaved.Length >= samplesPerChannel * Mathf.Max(1, renderChannels))
            {
                var ch = Mathf.Max(1, renderChannels);
                for (int i = 0; i < samplesPerChannel; i++)
                {
                    float sum = 0f;
                    int baseIdx = i * ch;
                    for (int c = 0; c < ch; c++)
                    {
                        sum += renderInterleaved[baseIdx + c];
                    }
                    _renderMonoBlock[i] = sum / ch;
                }
            }
            else
            {
                Array.Clear(_renderMonoBlock, 0, _renderMonoBlock.Length);
            }

            // Push into ring
            var cap = _histRenderMono.Length;
            for (int i = 0; i < samplesPerChannel; i++)
            {
                _histRenderMono[_histWrite] = _renderMonoBlock[i];
                _histMicMono[_histWrite] = micMono[i];
                _histWrite = (_histWrite + 1) % cap;
                if (_histCount < cap) _histCount++;
            }
        }

        private void Update()
        {
            if (!autoTuneDelay || !enableAec || !_available || _apm == IntPtr.Zero)
            {
                return;
            }

            var now = Time.realtimeSinceStartup;
            if (now - _lastAutoTuneTime < Mathf.Max(1, autoTuneEverySeconds))
            {
                return;
            }

            if (_histRenderMono == null || _histMicMono == null || _histCount < SampleRateHz / 2)
            {
                return;
            }

            _lastAutoTuneTime = now;

            // Copy history in chronological order
            var cap = _histRenderMono.Length;
            var count = Mathf.Min(_histCount, cap);
            var render = new float[count];
            var mic = new float[count];
            var start = (_histWrite - count + cap) % cap;
            for (int i = 0; i < count; i++)
            {
                var idx = (start + i) % cap;
                render[i] = _histRenderMono[idx];
                mic[i] = _histMicMono[idx];
            }

            // Downsample factor for speed
            const int ds = 4;
            var dsCount = count / ds;
            if (dsCount < 2000) return;

            // Precompute energies (avoid tuning on silence)
            double eRender = 0, eMic = 0;
            for (int i = 0; i < dsCount; i++)
            {
                var r = render[i * ds];
                var m = mic[i * ds];
                eRender += r * r;
                eMic += m * m;
            }
            if (eRender < 1e-3 || eMic < 1e-3)
            {
                return;
            }

            var maxLagSamples = Mathf.Clamp((autoTuneMaxDelayMs * SampleRateHz) / 1000, 0, count - 1);
            var maxLagDs = Math.Max(0, maxLagSamples / ds);
            if (maxLagDs <= 0) return;

            // Correlate mic against render with positive lag (mic delayed vs render)
            double bestCorr = double.NegativeInfinity;
            int bestLagDs = 0;

            // Step in ~2.5ms increments at ds=4, sr=32k => 80 samples ~ 20 ds steps.
            var stepDs = Math.Max(1, (int)((SampleRateHz * 0.0025f) / ds));
            for (int lag = 0; lag <= maxLagDs; lag += stepDs)
            {
                double dot = 0;
                int n = dsCount - lag;
                for (int i = 0; i < n; i++)
                {
                    dot += render[i * ds] * mic[(i + lag) * ds];
                }
                // Normalize by energy (rough)
                var corr = dot / Math.Sqrt(eRender * eMic);
                if (corr > bestCorr)
                {
                    bestCorr = corr;
                    bestLagDs = lag;
                }
            }

            // Only update when correlation is meaningful
            if (bestCorr > 0.05)
            {
                var bestLagSamples = bestLagDs * ds;
                var newDelay = Mathf.Clamp((bestLagSamples * 1000) / SampleRateHz, 0, 300);
                if (Mathf.Abs(newDelay - streamDelayMs) >= 5)
                {
                    streamDelayMs = newDelay;
                    Debug.Log($"[AEC] Auto-tuned streamDelayMs={streamDelayMs} (corr={bestCorr:0.000}, sr={SampleRateHz})");
                }
            }
        }

        private static void ResampleInterleavedLinear(
            float[] src,
            int srcSamplesPerChannel,
            float[] dst,
            int dstSamplesPerChannel,
            int channels)
        {
            if (src == null || dst == null) return;
            if (srcSamplesPerChannel <= 0 || dstSamplesPerChannel <= 0 || channels <= 0) return;
            if (channels == 1 && srcSamplesPerChannel == dstSamplesPerChannel)
            {
                Array.Copy(src, 0, dst, 0, Math.Min(src.Length, dst.Length));
                return;
            }

            var ratio = (float)srcSamplesPerChannel / dstSamplesPerChannel;
            for (int i = 0; i < dstSamplesPerChannel; i++)
            {
                var srcPos = i * ratio;
                var idx = (int)srcPos;
                var frac = srcPos - idx;
                var idx2 = idx + 1;
                if (idx < 0) idx = 0;
                if (idx >= srcSamplesPerChannel) idx = srcSamplesPerChannel - 1;
                if (idx2 >= srcSamplesPerChannel) idx2 = srcSamplesPerChannel - 1;

                for (int ch = 0; ch < channels; ch++)
                {
                    var a = src[idx * channels + ch];
                    var b = src[idx2 * channels + ch];
                    dst[i * channels + ch] = a + (b - a) * frac;
                }
            }
        }

        private void StartDumpIfNeeded()
        {
            if (!dumpWav)
            {
                StopDump();
                return;
            }

            if (_dumpMic != null && _dumpRender != null && _dumpAec != null)
            {
                return;
            }

            StopDump();

            var dir = System.IO.Path.Combine(Application.persistentDataPath, "aec_dumps");
            Debug.Log($"[AEC] Dump WAV enabled. Output dir: {dir}");
            var tag = DateTime.Now.ToString("yyyyMMdd_HHmmss");
            _dumpSamplesWrittenPerChannel = 0;

            _dumpMic = new WavFileWriter(System.IO.Path.Combine(dir, $"mic_{tag}.wav"), SampleRateHz, captureChannels);
            _dumpRender = new WavFileWriter(System.IO.Path.Combine(dir, $"render_{tag}.wav"), SampleRateHz, _renderChannels);
            _dumpAec = new WavFileWriter(System.IO.Path.Combine(dir, $"aec_{tag}.wav"), SampleRateHz, captureChannels);
        }

        private void StopDump()
        {
            try { _dumpMic?.Dispose(); } catch { }
            try { _dumpRender?.Dispose(); } catch { }
            try { _dumpAec?.Dispose(); } catch { }
            _dumpMic = null;
            _dumpRender = null;
            _dumpAec = null;
            _dumpSamplesWrittenPerChannel = 0;
        }

        /// <summary>
        /// Process a single capture block through AEC. Input is PCM16 (mono). Output is PCM16 (mono).
        /// If AEC is not available, returns false.
        /// </summary>
        public bool TryProcessCapturePcm16(short[] capturePcm16, out short[] outPcm16)
        {
            outPcm16 = null;

            if (!enableAec || !_available || _apm == IntPtr.Zero)
            {
                return false;
            }

            if (capturePcm16 == null || capturePcm16.Length == 0)
            {
                return false;
            }

            EnsureBuffers();

            var spc = BlockSamplesPerChannel;
            var expected = spc * captureChannels;
            if (capturePcm16.Length != expected)
            {
                // Caller must feed exactly one block.
                _failSizeCount++;
                if (Time.realtimeSinceStartup - _lastErrorLogTime > 1.0f)
                {
                    _lastErrorLogTime = Time.realtimeSinceStartup;
                    Debug.LogWarning($"[AEC] Block size mismatch. got={capturePcm16.Length} expected={expected} spc={spc} sr={SampleRateHz}");
                }
                return false;
            }

            // Pull render reference. If not enough buffered yet, use zeros.
            Array.Clear(_renderFrame, 0, _renderFrame.Length);
            var tapSr = renderTap != null ? renderTap.SampleRateHz : AudioSettings.outputSampleRate;
            var tapSpc = Mathf.Max(1, (tapSr * Mathf.Max(1, blockMs)) / 1000);
            if (renderTap != null)
            {
                _renderChannels = Mathf.Max(1, renderTap.Channels);
                EnsureBuffers();
                if (!renderTap.TryDequeue(_renderTapFrame.Length, _renderTapFrame, 0))
                {
                    // Not enough render samples buffered yet; keep the previous renderTapFrame
                    // to reduce gaps at the start of playback.
                }
            }

            // Resample render reference to match APM sample rate if needed.
            if (tapSr == SampleRateHz)
            {
                Array.Copy(_renderTapFrame, 0, _renderFrame, 0, Math.Min(_renderTapFrame.Length, _renderFrame.Length));
            }
            else
            {
                ResampleInterleavedLinear(_renderTapFrame, tapSpc, _renderFrame, spc, _renderChannels);
            }

            // Start dump early so we get mic/render even if AEC processing fails.
            StartDumpIfNeeded();
            if (_dumpMic != null && _dumpRender != null)
            {
                var maxSamples = Mathf.Max(1, dumpMaxSeconds) * SampleRateHz;
                if (_dumpSamplesWrittenPerChannel < maxSamples)
                {
                    _dumpMic.WritePcm16(capturePcm16);
                    for (int i = 0; i < _renderFrame.Length; i++)
                    {
                        var v = Mathf.Clamp(_renderFrame[i], -1f, 1f);
                        _renderTmpPcm16[i] = (short)Mathf.RoundToInt(v * 32767f);
                    }
                    _dumpRender.WritePcm16(_renderTmpPcm16);
                }
            }

            // Some APM builds expect stream delay to be set before each ProcessStream.
            ApmNative.apm_set_stream_delay_ms(_apm, Mathf.Max(0, streamDelayMs));

            // Feed reverse stream (render reference)
            var rcReverse = ApmNative.apm_process_reverse_stream(_apm, _renderFrame, spc);
            if (rcReverse != 0)
            {
                _failReverseCount++;
                if (Time.realtimeSinceStartup - _lastErrorLogTime > 1.0f)
                {
                    _lastErrorLogTime = Time.realtimeSinceStartup;
                    Debug.LogWarning($"[AEC] ProcessReverse failed rc={rcReverse} (sr={SampleRateHz}, spc={spc}, tapSr={tapSr})");
                }
                return false;
            }

            // Convert capture PCM16 -> float
            for (int i = 0; i < capturePcm16.Length; i++)
            {
                _captureFrame[i] = Mathf.Clamp(capturePcm16[i] / 32768f, -1f, 1f);
            }

            // Feed history for auto-tune (use pre-AEC mic + current render reference)
            if (autoTuneDelay)
            {
                PushHistoryMono(_renderFrame, _renderChannels, _captureFrame, spc);
            }

            var rc = ApmNative.apm_process_stream(_apm, _captureFrame, spc, _captureOut);
            if (rc != 0)
            {
                _failProcessCount++;
                if (Time.realtimeSinceStartup - _lastErrorLogTime > 1.0f)
                {
                    _lastErrorLogTime = Time.realtimeSinceStartup;
                    Debug.LogWarning($"[AEC] ProcessStream failed rc={rc} (sr={SampleRateHz}, spc={spc}, delayMs={streamDelayMs})");
                }
                return false;
            }

            // Convert float -> PCM16
            var processed = new short[capturePcm16.Length];
            for (int i = 0; i < processed.Length; i++)
            {
                var v = Mathf.Clamp(_captureOut[i], -1f, 1f);
                processed[i] = (short)Mathf.RoundToInt(v * 32767f);
            }

            // Optional WAV dumps (short sessions)
            if (_dumpAec != null)
            {
                var maxSamples = Mathf.Max(1, dumpMaxSeconds) * SampleRateHz;
                if (_dumpSamplesWrittenPerChannel < maxSamples)
                {
                    _dumpAec.WritePcm16(processed);
                    _dumpSamplesWrittenPerChannel += spc;
                }
                else
                {
                    StopDump();
                }
            }

            outPcm16 = processed;
            return true;
        }
    }
}

