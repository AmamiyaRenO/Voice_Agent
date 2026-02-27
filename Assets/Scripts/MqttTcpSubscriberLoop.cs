using System;
using System.IO;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using UnityEngine;

namespace RobotVoice
{
    internal sealed class MqttTcpSubscriberLoop
    {
        private readonly string logTag;
        private readonly bool verboseLogging;
        private readonly TimeSpan reconnectDelay;
        private readonly int packetBufferSize;

        public MqttTcpSubscriberLoop(string logTag, bool verboseLogging, TimeSpan reconnectDelay, int packetBufferSize = 8192)
        {
            this.logTag = string.IsNullOrWhiteSpace(logTag) ? "MqttSubscriber" : logTag.Trim();
            this.verboseLogging = verboseLogging;
            this.reconnectDelay = reconnectDelay < TimeSpan.Zero ? TimeSpan.Zero : reconnectDelay;
            this.packetBufferSize = Math.Max(1024, packetBufferSize);
        }

        public async Task RunAsync(string host, int port, string topic, string clientId, Action<string, string> onPublish, CancellationToken token)
        {
            if (onPublish == null)
            {
                throw new ArgumentNullException(nameof(onPublish));
            }

            var brokerHost = string.IsNullOrWhiteSpace(host) ? VoiceAgentDefaults.LocalHost : host.Trim();
            var brokerPort = port > 0 ? port : VoiceAgentDefaults.MqttPort;
            var subscribeTopic = string.IsNullOrWhiteSpace(topic) ? string.Empty : topic.Trim();
            var resolvedClientId = string.IsNullOrWhiteSpace(clientId) ? $"unity-sub-{Guid.NewGuid():N}" : clientId.Trim();

            if (string.IsNullOrEmpty(subscribeTopic))
            {
                Debug.LogError($"[{logTag}] subscription topic is empty.");
                return;
            }

            while (!token.IsCancellationRequested)
            {
                try
                {
                    if (verboseLogging)
                    {
                        Debug.Log($"[{logTag}] connecting {brokerHost}:{brokerPort}");
                    }

                    using var tcpClient = new TcpClient();
                    await tcpClient.ConnectAsync(brokerHost, brokerPort).ConfigureAwait(false);
                    using var stream = tcpClient.GetStream();

                    await SendConnectAsync(stream, resolvedClientId, token).ConfigureAwait(false);
                    await ReadConnAckAsync(stream, token).ConfigureAwait(false);

                    if (verboseLogging)
                    {
                        Debug.Log($"[{logTag}] connected and acknowledged");
                    }

                    await SendSubscribeAsync(stream, subscribeTopic, packetId: 1, token).ConfigureAwait(false);
                    if (verboseLogging)
                    {
                        Debug.Log($"[{logTag}] subscribed '{subscribeTopic}'");
                    }

                    var packetBuffer = new byte[packetBufferSize];
                    while (!token.IsCancellationRequested)
                    {
                        var header = await ReadByteAsync(stream, token).ConfigureAwait(false);
                        if (header < 0)
                        {
                            break;
                        }

                        var remaining = await ReadRemainingLengthAsync(stream, token).ConfigureAwait(false);
                        if (remaining <= 0 || remaining > packetBuffer.Length)
                        {
                            await SkipBytesAsync(stream, remaining, token).ConfigureAwait(false);
                            continue;
                        }

                        var read = await ReadExactAsync(stream, packetBuffer, remaining, token).ConfigureAwait(false);
                        if (read != remaining)
                        {
                            break;
                        }

                        var packetType = (byte)(header >> 4);
                        if (packetType != 3) // PUBLISH
                        {
                            continue;
                        }

                        var qos = (header & 0x06) >> 1;
                        var index = 0;
                        if (remaining < 2)
                        {
                            continue;
                        }

                        var topicLength = (packetBuffer[index] << 8) | packetBuffer[index + 1];
                        index += 2;
                        if (topicLength <= 0 || index + topicLength > remaining)
                        {
                            continue;
                        }

                        var messageTopic = Encoding.UTF8.GetString(packetBuffer, index, topicLength);
                        index += topicLength;

                        if (qos > 0)
                        {
                            if (index + 2 > remaining)
                            {
                                continue;
                            }

                            // QoS1/2 include packet identifier after topic.
                            index += 2;
                        }

                        var payloadLength = remaining - index;
                        if (payloadLength <= 0)
                        {
                            continue;
                        }

                        var payload = Encoding.UTF8.GetString(packetBuffer, index, payloadLength);
                        onPublish(messageTopic, payload);
                    }
                }
                catch (OperationCanceledException) when (token.IsCancellationRequested)
                {
                    break;
                }
                catch (Exception ex)
                {
                    if (verboseLogging)
                    {
                        Debug.LogWarning($"[{logTag}] connection error, retrying... ({ex.GetType().Name}: {ex.Message})");
                    }
                }

                if (reconnectDelay > TimeSpan.Zero && !token.IsCancellationRequested)
                {
                    try
                    {
                        await Task.Delay(reconnectDelay, token).ConfigureAwait(false);
                    }
                    catch (OperationCanceledException)
                    {
                        break;
                    }
                }
            }
        }

        private static async Task SendConnectAsync(NetworkStream stream, string clientId, CancellationToken token)
        {
            using var payloadStream = new MemoryStream();
            payloadStream.WriteByte(0x00);
            payloadStream.WriteByte(0x04);
            payloadStream.WriteByte((byte)'M');
            payloadStream.WriteByte((byte)'Q');
            payloadStream.WriteByte((byte)'T');
            payloadStream.WriteByte((byte)'T');
            payloadStream.WriteByte(0x04); // MQTT 3.1.1
            payloadStream.WriteByte(0x02); // clean session
            payloadStream.WriteByte(0x00);
            payloadStream.WriteByte(0x00); // keepalive disabled

            var clientIdBytes = Encoding.UTF8.GetBytes(clientId ?? string.Empty);
            payloadStream.WriteByte((byte)((clientIdBytes.Length >> 8) & 0xFF));
            payloadStream.WriteByte((byte)(clientIdBytes.Length & 0xFF));
            payloadStream.Write(clientIdBytes, 0, clientIdBytes.Length);

            var payload = payloadStream.ToArray();
            await WriteFixedHeaderAsync(stream, 0x10, payload.Length, token).ConfigureAwait(false);
            await stream.WriteAsync(payload, 0, payload.Length, token).ConfigureAwait(false);
            await stream.FlushAsync(token).ConfigureAwait(false);
        }

        private static async Task ReadConnAckAsync(NetworkStream stream, CancellationToken token)
        {
            var buffer = new byte[4];
            var read = await ReadExactAsync(stream, buffer, 4, token).ConfigureAwait(false);
            if (read != 4 || buffer[0] != 0x20 || buffer[3] != 0x00)
            {
                throw new IOException("MQTT broker rejected connection.");
            }
        }

        private static async Task SendSubscribeAsync(NetworkStream stream, string topic, ushort packetId, CancellationToken token)
        {
            var topicBytes = Encoding.UTF8.GetBytes(topic ?? string.Empty);
            using var payloadStream = new MemoryStream();
            payloadStream.WriteByte((byte)((packetId >> 8) & 0xFF));
            payloadStream.WriteByte((byte)(packetId & 0xFF));
            payloadStream.WriteByte((byte)((topicBytes.Length >> 8) & 0xFF));
            payloadStream.WriteByte((byte)(topicBytes.Length & 0xFF));
            payloadStream.Write(topicBytes, 0, topicBytes.Length);
            payloadStream.WriteByte(0x00); // QoS 0

            var payload = payloadStream.ToArray();
            await WriteFixedHeaderAsync(stream, 0x82, payload.Length, token).ConfigureAwait(false);
            await stream.WriteAsync(payload, 0, payload.Length, token).ConfigureAwait(false);
            await stream.FlushAsync(token).ConfigureAwait(false);
        }

        private static async Task WriteFixedHeaderAsync(NetworkStream stream, byte type, int length, CancellationToken token)
        {
            using var headerStream = new MemoryStream();
            headerStream.WriteByte(type);
            do
            {
                var encoded = (byte)(length % 128);
                length /= 128;
                if (length > 0)
                {
                    encoded |= 0x80;
                }
                headerStream.WriteByte(encoded);
            }
            while (length > 0);

            var header = headerStream.ToArray();
            await stream.WriteAsync(header, 0, header.Length, token).ConfigureAwait(false);
        }

        private static async Task<int> ReadByteAsync(Stream stream, CancellationToken token)
        {
            var one = new byte[1];
            var read = await stream.ReadAsync(one, 0, 1, token).ConfigureAwait(false);
            return read <= 0 ? -1 : one[0];
        }

        private static async Task<int> ReadRemainingLengthAsync(Stream stream, CancellationToken token)
        {
            var multiplier = 1;
            var value = 0;
            var loops = 0;
            while (true)
            {
                var digit = await ReadByteAsync(stream, token).ConfigureAwait(false);
                if (digit < 0)
                {
                    return -1;
                }

                value += (digit & 127) * multiplier;
                if ((digit & 128) == 0)
                {
                    return value;
                }

                multiplier *= 128;
                loops++;
                if (loops > 3)
                {
                    return -1;
                }
            }
        }

        private static async Task<int> ReadExactAsync(Stream stream, byte[] buffer, int length, CancellationToken token)
        {
            var total = 0;
            while (total < length)
            {
                var read = await stream.ReadAsync(buffer, total, length - total, token).ConfigureAwait(false);
                if (read <= 0)
                {
                    break;
                }
                total += read;
            }
            return total;
        }

        private static async Task SkipBytesAsync(Stream stream, int length, CancellationToken token)
        {
            if (length <= 0)
            {
                return;
            }

            var temp = new byte[1024];
            var remaining = length;
            while (remaining > 0)
            {
                var toRead = Math.Min(remaining, temp.Length);
                var read = await stream.ReadAsync(temp, 0, toRead, token).ConfigureAwait(false);
                if (read <= 0)
                {
                    break;
                }
                remaining -= read;
            }
        }
    }
}
