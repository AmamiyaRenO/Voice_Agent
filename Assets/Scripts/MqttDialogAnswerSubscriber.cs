using System;
using System.IO;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using UnityEngine;

namespace RobotVoice
{
    // Subscribe to dialog_service answers and record them into ConversationLog as Coach.
    public sealed class MqttDialogAnswerSubscriber : MonoBehaviour
    {
        [Header("MQTT")]
        [SerializeField] private string host = "127.0.0.1";
        [SerializeField] private int port = 1883;
        [SerializeField] private string topic = "robot/dialog/answer";
        [SerializeField] private bool autoStart = true;

        [Header("Playback (Unity TTS)")]
        [SerializeField, Tooltip("If set, plays dialog answers through Unity (recommended for AEC).")]
        private bool playAnswerInUnity = true;
        [SerializeField] private VoiceGameLauncher launcher;

        [Header("Diagnostics")]
        [SerializeField] private bool verboseLogging = true;

        private CancellationTokenSource loopCts;
        private Task loopTask;
        private SynchronizationContext mainThreadContext;
        private string mqttClientId;

        private void Awake()
        {
            mainThreadContext = SynchronizationContext.Current;
            if (launcher == null)
            {
                launcher = FindObjectOfType<VoiceGameLauncher>();
            }
            try
            {
                var unique = SystemInfo.deviceUniqueIdentifier;
                if (string.IsNullOrEmpty(unique)) unique = Environment.MachineName ?? "host";
                var shortId = unique.Length > 6 ? unique.Substring(0, 6) : unique;
                mqttClientId = $"unity-dialog-{shortId}";
            }
            catch
            {
                mqttClientId = $"unity-dialog-{Guid.NewGuid().ToString("N").Substring(0, 6)}";
            }
        }

        private void OnEnable()
        {
            if (autoStart) StartSubscriber();
        }

        private void OnDisable()
        {
            StopSubscriber();
        }

        public void StartSubscriber()
        {
            if (loopTask != null) return;
            loopCts = new CancellationTokenSource();
            loopTask = Task.Run(() => RunLoopAsync(loopCts.Token));
            if (verboseLogging) Debug.Log("[DialogAnswerSubscriber] start requested");
        }

        public void StopSubscriber()
        {
            try { loopCts?.Cancel(); } catch { }
            loopTask = null;
            loopCts?.Dispose();
            loopCts = null;
        }

        private async Task RunLoopAsync(CancellationToken token)
        {
            while (!token.IsCancellationRequested)
            {
                try
                {
                    if (verboseLogging) Debug.Log($"[DialogAnswerSubscriber] connecting {host}:{port}");
                    using var client = new TcpClient();
                    await client.ConnectAsync(host, port);
                    using var stream = client.GetStream();

                    await SendConnectAsync(stream, mqttClientId ?? "unity-dialog", token);
                    await ReadConnAckAsync(stream, token);
                    if (verboseLogging) Debug.Log("[DialogAnswerSubscriber] connected and acknowledged");

                    await SendSubscribeAsync(stream, topic, packetId: 2, token: token);
                    if (verboseLogging) Debug.Log($"[DialogAnswerSubscriber] subscribed '{topic}'");

                    var buffer = new byte[8192];
                    while (!token.IsCancellationRequested)
                    {
                        int header = stream.ReadByte();
                        if (header < 0) break;
                        int remaining = ReadRemainingLength(stream);
                        if (remaining <= 0 || remaining > buffer.Length)
                        {
                            SkipBytes(stream, remaining);
                            continue;
                        }
                        int read = ReadExact(stream, buffer, remaining);
                        if (read != remaining) break;

                        byte packetType = (byte)(header >> 4);
                        if (packetType == 3) // PUBLISH
                        {
                            int idx = 0;
                            int topicLen = (buffer[idx] << 8) | buffer[idx + 1]; idx += 2;
                            string msgTopic = Encoding.UTF8.GetString(buffer, idx, topicLen); idx += topicLen;
                            int payloadLen = remaining - idx;
                            if (payloadLen > 0)
                            {
                                string json = Encoding.UTF8.GetString(buffer, idx, payloadLen);
                                if (verboseLogging) Debug.Log($"[DialogAnswerSubscriber] msg on '{msgTopic}': {json}");
                                HandleAnswerJson(json);
                            }
                        }
                    }
                }
                catch (Exception ex)
                {
                    if (verboseLogging) Debug.LogWarning($"[DialogAnswerSubscriber] connection error, retrying... ({ex.GetType().Name}: {ex.Message})");
                    await Task.Delay(600, token);
                }
            }
        }

        private void HandleAnswerJson(string json)
        {
            if (string.IsNullOrWhiteSpace(json)) return;
            try
            {
                var node = JSONNode.Parse(json);
                var text = node?["text"]?.Value ?? string.Empty;
                if (string.IsNullOrWhiteSpace(text)) return;
                var corrId = node?["corr_id"]?.Value ?? string.Empty;
                var ttsInstruct = node?["tts_instruct"]?.Value ?? string.Empty;
                var ttsSpeaker = node?["tts_speaker"]?.Value ?? string.Empty;

                PostToMainThread(() =>
                {
                    ConversationLog.AddEntry(ConversationRole.Coach, text, "dialog_service");
                    if (playAnswerInUnity && launcher != null)
                    {
                        // Play via Unity so RenderTap has a reference signal for AEC.
                        launcher.PlayDialogAnswerFromService(text, corrId, ttsInstruct, ttsSpeaker);
                    }
                });
            }
            catch { }
        }

        private void PostToMainThread(Action action)
        {
            if (action == null) return;
            var ctx = mainThreadContext;
            if (ctx != null && SynchronizationContext.Current != ctx)
            {
                ctx.Post(_ => action(), null);
            }
            else
            {
                action();
            }
        }

        // --- Minimal MQTT helpers (QoS 0) ---
        private static async Task SendConnectAsync(NetworkStream s, string clientId, CancellationToken token)
        {
            using var ms = new MemoryStream();
            ms.WriteByte(0x00); ms.WriteByte(0x04);
            ms.WriteByte((byte)'M'); ms.WriteByte((byte)'Q'); ms.WriteByte((byte)'T'); ms.WriteByte((byte)'T');
            ms.WriteByte(0x04);
            ms.WriteByte(0x02); // clean session
            ms.WriteByte(0x00); ms.WriteByte(0x3C); // 60s
            var cid = Encoding.UTF8.GetBytes(clientId);
            ms.WriteByte((byte)((cid.Length >> 8) & 0xFF));
            ms.WriteByte((byte)(cid.Length & 0xFF));
            ms.Write(cid, 0, cid.Length);
            var payload = ms.ToArray();
            await WriteFixedHeaderAsync(s, 0x10, payload.Length, token);
            await s.WriteAsync(payload, 0, payload.Length, token);
            await s.FlushAsync(token);
        }

        private static async Task ReadConnAckAsync(NetworkStream s, CancellationToken token)
        {
            var b = new byte[4];
            int read = 0;
            while (read < 4)
            {
                int n = await s.ReadAsync(b, read, 4 - read, token);
                if (n <= 0) throw new IOException("MQTT connack read failed");
                read += n;
            }
            if (b[0] != 0x20 || b[3] != 0x00)
                throw new IOException("MQTT broker rejected connection");
        }

        private static async Task SendSubscribeAsync(NetworkStream s, string topic, ushort packetId, CancellationToken token)
        {
            var topicBytes = Encoding.UTF8.GetBytes(topic);
            using var ms = new MemoryStream();
            ms.WriteByte((byte)((packetId >> 8) & 0xFF));
            ms.WriteByte((byte)(packetId & 0xFF));
            ms.WriteByte((byte)((topicBytes.Length >> 8) & 0xFF));
            ms.WriteByte((byte)(topicBytes.Length & 0xFF));
            ms.Write(topicBytes, 0, topicBytes.Length);
            ms.WriteByte(0x00); // QoS 0
            var payload = ms.ToArray();
            await WriteFixedHeaderAsync(s, 0x82, payload.Length, token);
            await s.WriteAsync(payload, 0, payload.Length, token);
            await s.FlushAsync(token);
        }

        private static async Task WriteFixedHeaderAsync(NetworkStream s, byte type, int len, CancellationToken token)
        {
            using var ms = new MemoryStream();
            ms.WriteByte(type);
            do
            {
                byte encoded = (byte)(len % 128);
                len /= 128;
                if (len > 0) encoded |= 0x80;
                ms.WriteByte(encoded);
            } while (len > 0);
            var fixedHeader = ms.ToArray();
            await s.WriteAsync(fixedHeader, 0, fixedHeader.Length, token);
        }

        private static int ReadRemainingLength(Stream s)
        {
            int multiplier = 1, value = 0;
            while (true)
            {
                int digit = s.ReadByte(); if (digit < 0) return -1;
                value += (digit & 127) * multiplier;
                if ((digit & 128) == 0) break;
                multiplier *= 128; if (multiplier > 128 * 128 * 128) return -1;
            }
            return value;
        }

        private static int ReadExact(Stream s, byte[] buffer, int len)
        {
            int total = 0;
            while (total < len)
            {
                int n = s.Read(buffer, total, len - total);
                if (n <= 0) break;
                total += n;
            }
            return total;
        }

        private static void SkipBytes(Stream s, int len)
        {
            var tmp = new byte[1024]; int remaining = len;
            while (remaining > 0)
            {
                int n = s.Read(tmp, 0, Math.Min(remaining, tmp.Length));
                if (n <= 0) break;
                remaining -= n;
            }
        }
    }
}

