using System;
using System.IO;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using UnityEngine;

namespace RobotVoice
{
    // Minimal MQTT subscriber for caption sentences published by LiveCaptionsListener.
    // It connects over TCP, subscribes to a topic and forwards received "text" fields
    // to VoiceGameLauncher by wrapping them into a Vosk-compatible JSON object.
    public sealed class MqttCaptionSubscriber : MonoBehaviour
    {
        [Header("MQTT")]
        [SerializeField] private string host = "127.0.0.1";
        [SerializeField] private int port = 1883;
        [SerializeField] private string topic = "robot/voice/text";
        [SerializeField] private bool autoStart = true;
        [SerializeField, Tooltip("ClientId suffix to distinguish multiple Unity instances")]
        private string clientIdSuffix = "unity-caption";

        [Header("Routing")]
        [SerializeField] private VoiceGameLauncher launcher;
        [SerializeField, Tooltip("Auto trigger wake flow on first incoming sentence to pass gating")]
        private bool autoWakeOnFirstSentence = true;

        [Header("Diagnostics")]
        [SerializeField] private bool verboseLogging = true;

        private CancellationTokenSource loopCts;
        private Task loopTask;
        private SynchronizationContext mainThreadContext;
        private bool wakePrimed;
        private string mqttClientId;

        private void Awake()
        {
            mainThreadContext = SynchronizationContext.Current;
            if (launcher == null)
            {
                launcher = FindObjectOfType<VoiceGameLauncher>();
            }

            // Build a stable client id on the main thread to avoid Unity API access off-thread
            try
            {
                var unique = SystemInfo.deviceUniqueIdentifier;
                if (string.IsNullOrEmpty(unique)) unique = Environment.MachineName ?? "host";
                var shortId = unique.Length > 6 ? unique.Substring(0, 6) : unique;
                mqttClientId = $"{clientIdSuffix}-{shortId}";
            }
            catch
            {
                mqttClientId = $"{clientIdSuffix}-{Guid.NewGuid().ToString("N").Substring(0, 6)}";
            }
        }

        private void OnEnable()
        {
            if (autoStart)
            {
                StartSubscriber();
            }
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
            if (verboseLogging) Debug.Log("[CaptionSubscriber] start requested");
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
                    if (verboseLogging) Debug.Log($"[CaptionSubscriber] connecting {host}:{port}");
                    using var client = new TcpClient();
                    await client.ConnectAsync(host, port);
                    using var stream = client.GetStream();

                    // CONNECT
                    await SendConnectAsync(stream, mqttClientId ?? $"{clientIdSuffix}-client", token);
                    await ReadConnAckAsync(stream, token);
                    if (verboseLogging) Debug.Log("[CaptionSubscriber] connected and acknowledged");

                    // SUBSCRIBE
                    await SendSubscribeAsync(stream, topic, packetId: 1, token: token);
                    if (verboseLogging) Debug.Log($"[CaptionSubscriber] subscribed '{topic}'");

                    // Loop
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
                            // QoS 0 only (no packet id)
                            int payloadLen = remaining - idx;
                            if (payloadLen > 0)
                            {
                                string json = Encoding.UTF8.GetString(buffer, idx, payloadLen);
                                if (verboseLogging) Debug.Log($"[CaptionSubscriber] msg on '{msgTopic}': {json}");
                                HandleIncomingJson(json);
                            }
                        }
                    }
                }
                catch (Exception ex)
                {
                    if (verboseLogging) Debug.LogWarning($"[CaptionSubscriber] connection error, retrying... ({ex.GetType().Name}: {ex.Message})");
                    await Task.Delay(500, token);
                }
            }
        }

        private void HandleIncomingJson(string json)
        {
            if (string.IsNullOrWhiteSpace(json)) return;
            try
            {
                var node = JSONNode.Parse(json);
                var text = node?["text"]?.Value ?? string.Empty;
                if (string.IsNullOrWhiteSpace(text)) return;

                // Wrap into Vosk-compatible minimal payload for the existing handler.
                var wrapped = $"{{\"text\":\"{Escape(text)}\"}}";
                if (launcher == null) return;

                PostToMainThread(() =>
                {
                    if (autoWakeOnFirstSentence && !wakePrimed)
                    {
                        wakePrimed = true;
                        try { launcher.TriggerWakeWordForTester(); } catch { }
                        if (verboseLogging) Debug.Log("[CaptionSubscriber] auto wake triggered");
                    }
                    try
                    {
                        if (verboseLogging) Debug.Log($"[CaptionSubscriber] forwarding transcript: {text}");
                        launcher.HandleVoskResult(wrapped);
                    }
                    catch { }
                });
            }
            catch
            {
                // ignore parse errors
            }
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

        private static string Escape(string s)
        {
            if (string.IsNullOrEmpty(s)) return string.Empty;
            return s.Replace("\\", "\\\\").Replace("\"", "\\\"");
        }

        // --- Minimal MQTT helpers (QoS 0) ---
        private static async Task SendConnectAsync(NetworkStream s, string clientId, CancellationToken token)
        {
            using var ms = new MemoryStream();
            // Variable header
            ms.WriteByte(0x00); ms.WriteByte(0x04);
            ms.WriteByte((byte)'M'); ms.WriteByte((byte)'Q'); ms.WriteByte((byte)'T'); ms.WriteByte((byte)'T');
            ms.WriteByte(0x04);      // protocol level
            ms.WriteByte(0x02);      // clean session
            ms.WriteByte(0x00); ms.WriteByte(0x3C); // keepalive 60s
            // Payload
            var cid = Encoding.UTF8.GetBytes(clientId);
            ms.WriteByte((byte)((cid.Length >> 8) & 0xFF));
            ms.WriteByte((byte)(cid.Length & 0xFF));
            ms.Write(cid, 0, cid.Length);
            var vhAndPayload = ms.ToArray();
            await WriteFixedHeaderAsync(s, 0x10, vhAndPayload.Length, token);
            await s.WriteAsync(vhAndPayload, 0, vhAndPayload.Length, token);
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

