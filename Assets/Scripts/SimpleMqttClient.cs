using System;
using System.Collections.Generic;
using System.IO;
using System.Net.Security;
using System.Net.Sockets;
using System.Security.Authentication;
using System.Text;
using System.Threading;
using System.Threading.Tasks;

namespace RobotVoice.Mqtt
{
    public sealed class SimpleMqttClient : IDisposable
    {
        private readonly SemaphoreSlim ioLock = new SemaphoreSlim(1, 1);
        private readonly object stateLock = new object();

        private TcpClient tcpClient;
        private Stream stream;
        private SimpleMqttClientOptions options;
        private CancellationTokenSource keepAliveCts;
        private Task keepAliveTask;
        private ushort packetIdentifier = 1;
        private bool disposed;

        public bool IsConnected { get; private set; }

        public event Action Connected;
        public event Action<string> Disconnected;

        public async Task ConnectAsync(SimpleMqttClientOptions options, CancellationToken cancellationToken)
        {
            if (options == null)
            {
                throw new ArgumentNullException(nameof(options));
            }

            ThrowIfDisposed();

            lock (stateLock)
            {
                if (IsConnected)
                {
                    return;
                }

                this.options = options;
                packetIdentifier = 1;
            }

            tcpClient = new TcpClient();
            try
            {
                await tcpClient.ConnectAsync(options.Host, options.Port).ConfigureAwait(false);
                var networkStream = tcpClient.GetStream();
                stream = await CreateTransportStreamAsync(networkStream, cancellationToken).ConfigureAwait(false);

                await ioLock.WaitAsync(cancellationToken).ConfigureAwait(false);
                try
                {
                    await SendConnectPacketAsync(cancellationToken).ConfigureAwait(false);
                    var packet = await ReadPacketAsync(cancellationToken).ConfigureAwait(false);
                    ValidateConnAck(packet);
                }
                finally
                {
                    ioLock.Release();
                }

                lock (stateLock)
                {
                    IsConnected = true;
                }

                Connected?.Invoke();
                StartKeepAliveLoop();
            }
            catch
            {
                Cleanup("Connection failed");
                throw;
            }
        }

        public async Task PublishAsync(SimpleMqttApplicationMessage message, CancellationToken cancellationToken)
        {
            if (message == null)
            {
                throw new ArgumentNullException(nameof(message));
            }

            ThrowIfDisposed();

            if (!IsConnected)
            {
                throw new InvalidOperationException("MQTT client is not connected.");
            }

            await ioLock.WaitAsync(cancellationToken).ConfigureAwait(false);
            try
            {
                var packetId = await SendPublishPacketAsync(message, cancellationToken).ConfigureAwait(false);
                if (message.QualityOfServiceLevel == SimpleMqttQualityOfServiceLevel.AtLeastOnce)
                {
                    var packet = await ReadPacketAsync(cancellationToken).ConfigureAwait(false);
                    if ((packet.Header & 0xF0) != 0x40)
                    {
                        throw new InvalidOperationException("Expected PUBACK response from broker.");
                    }

                    if (packet.Payload.Length < 2)
                    {
                        throw new InvalidOperationException("PUBACK payload malformed.");
                    }

                    if (packetId.HasValue)
                    {
                        var ackId = (ushort)((packet.Payload[0] << 8) | packet.Payload[1]);
                        if (ackId != packetId.Value)
                        {
                            throw new InvalidOperationException("PUBACK packet identifier mismatch.");
                        }
                    }
                }
                else if (message.QualityOfServiceLevel == SimpleMqttQualityOfServiceLevel.ExactlyOnce)
                {
                    throw new NotSupportedException("QoS 2 is not supported by SimpleMqttClient.");
                }
            }
            catch
            {
                Cleanup("Publish failed");
                throw;
            }
            finally
            {
                ioLock.Release();
            }
        }

        public async Task DisconnectAsync(CancellationToken cancellationToken)
        {
            ThrowIfDisposed();

            if (!IsConnected)
            {
                Cleanup("Disconnected");
                return;
            }

            await ioLock.WaitAsync(cancellationToken).ConfigureAwait(false);
            try
            {
                if (stream != null)
                {
                    var packet = new byte[] { 0xE0, 0x00 };
                    await stream.WriteAsync(packet, 0, packet.Length, cancellationToken).ConfigureAwait(false);
                    await stream.FlushAsync(cancellationToken).ConfigureAwait(false);
                }
            }
            catch
            {
                Cleanup("Disconnect failed");
                throw;
            }
            finally
            {
                ioLock.Release();
            }

            Cleanup("Disconnected");
        }

        public void Dispose()
        {
            if (disposed)
            {
                return;
            }

            disposed = true;
            Cleanup("Disposed");
            ioLock.Dispose();
        }

        private void StartKeepAliveLoop()
        {
            var period = options?.KeepAlivePeriod ?? TimeSpan.Zero;
            if (period <= TimeSpan.Zero)
            {
                return;
            }

            keepAliveCts?.Cancel();
            keepAliveCts?.Dispose();

            keepAliveCts = new CancellationTokenSource();
            var token = keepAliveCts.Token;
            keepAliveTask = Task.Run(() => KeepAliveLoopAsync(period, token), token);
        }

        private async Task KeepAliveLoopAsync(TimeSpan period, CancellationToken token)
        {
            try
            {
                while (!token.IsCancellationRequested)
                {
                    await Task.Delay(period, token).ConfigureAwait(false);
                    if (!IsConnected || token.IsCancellationRequested)
                    {
                        break;
                    }

                    await ioLock.WaitAsync(token).ConfigureAwait(false);
                    try
                    {
                        await SendPingReqAsync(token).ConfigureAwait(false);
                        var packet = await ReadPacketAsync(token).ConfigureAwait(false);
                        if ((packet.Header & 0xF0) != 0xD0)
                        {
                            throw new InvalidOperationException("Expected PINGRESP from broker.");
                        }
                    }
                    finally
                    {
                        ioLock.Release();
                    }
                }
            }
            catch (OperationCanceledException)
            {
            }
            catch
            {
                Cleanup("KeepAlive failed");
            }
        }

        private async Task<Stream> CreateTransportStreamAsync(NetworkStream networkStream, CancellationToken cancellationToken)
        {
            if (networkStream == null)
            {
                throw new ArgumentNullException(nameof(networkStream));
            }

            cancellationToken.ThrowIfCancellationRequested();

            var tlsOptions = options?.TlsOptions;
            if (tlsOptions == null || !tlsOptions.UseTls)
            {
                return networkStream;
            }

            RemoteCertificateValidationCallback validationCallback = null;
            if (tlsOptions.CertificateValidationCallback != null)
            {
                validationCallback = (_, certificate, chain, sslPolicyErrors) => tlsOptions.CertificateValidationCallback(certificate, chain, sslPolicyErrors);
            }
            else if (tlsOptions.AllowUntrustedCertificates)
            {
                validationCallback = (_, __, ___, ____) => true;
            }

            var sslStream = new SslStream(networkStream, false, validationCallback);
            try
            {
                var targetHost = string.IsNullOrEmpty(tlsOptions.TargetHost) ? options.Host : tlsOptions.TargetHost;
                var certificates = tlsOptions.ClientCertificates;
                var protocols = tlsOptions.SslProtocols ?? SslProtocols.None;
                var checkRevocation = tlsOptions.CheckCertificateRevocation;

                await sslStream.AuthenticateAsClientAsync(targetHost, certificates, protocols, checkRevocation).ConfigureAwait(false);
                return sslStream;
            }
            catch
            {
                sslStream.Dispose();
                throw;
            }
        }

        private async Task SendConnectPacketAsync(CancellationToken cancellationToken)
        {
            if (stream == null)
            {
                throw new InvalidOperationException("MQTT stream is not available.");
            }

            var hasUsername = !string.IsNullOrEmpty(options.Username);
            var hasPassword = !string.IsNullOrEmpty(options.Password);
            var variableHeader = new List<byte>();
            variableHeader.AddRange(EncodeString("MQTT"));
            variableHeader.Add(0x04); // Protocol level 3.1.1

            byte connectFlags = 0;
            if (options.CleanSession)
            {
                connectFlags |= 0x02;
            }

            if (hasUsername)
            {
                connectFlags |= 0x80;
            }

            if (hasPassword)
            {
                connectFlags |= 0x40;
            }

            variableHeader.Add(connectFlags);

            var keepAliveSeconds = (ushort)Math.Min(ushort.MaxValue, Math.Max(0, (int)Math.Round(options.KeepAlivePeriod.TotalSeconds)));
            variableHeader.Add((byte)(keepAliveSeconds >> 8));
            variableHeader.Add((byte)(keepAliveSeconds & 0xFF));

            var payload = new List<byte>();
            payload.AddRange(EncodeString(string.IsNullOrEmpty(options.ClientId) ? Guid.NewGuid().ToString("N") : options.ClientId));
            if (hasUsername)
            {
                payload.AddRange(EncodeString(options.Username));
            }

            if (hasPassword)
            {
                payload.AddRange(EncodeString(options.Password ?? string.Empty));
            }

            var remainingLength = variableHeader.Count + payload.Count;
            var packet = new List<byte> { 0x10 };
            packet.AddRange(EncodeRemainingLength(remainingLength));
            packet.AddRange(variableHeader);
            packet.AddRange(payload);

            var buffer = packet.ToArray();
            await stream.WriteAsync(buffer, 0, buffer.Length, cancellationToken).ConfigureAwait(false);
            await stream.FlushAsync(cancellationToken).ConfigureAwait(false);
        }

        private async Task<ushort?> SendPublishPacketAsync(SimpleMqttApplicationMessage message, CancellationToken cancellationToken)
        {
            if (stream == null)
            {
                throw new InvalidOperationException("MQTT stream is not available.");
            }

            var topicBytes = EncodeString(message.Topic);
            var payload = message.Payload ?? Array.Empty<byte>();
            byte header = 0x30; // PUBLISH
            ushort? packetId = null;

            switch (message.QualityOfServiceLevel)
            {
                case SimpleMqttQualityOfServiceLevel.AtMostOnce:
                    break;
                case SimpleMqttQualityOfServiceLevel.AtLeastOnce:
                    header |= 0x02;
                    packetId = GetNextPacketIdentifier();
                    break;
                case SimpleMqttQualityOfServiceLevel.ExactlyOnce:
                    throw new NotSupportedException("QoS 2 is not supported by SimpleMqttClient.");
                default:
                    throw new ArgumentOutOfRangeException();
            }

            var remainingLength = topicBytes.Length + payload.Length;
            if (packetId.HasValue)
            {
                remainingLength += 2;
            }

            var packet = new List<byte> { header };
            packet.AddRange(EncodeRemainingLength(remainingLength));
            packet.AddRange(topicBytes);
            if (packetId.HasValue)
            {
                packet.Add((byte)(packetId.Value >> 8));
                packet.Add((byte)(packetId.Value & 0xFF));
            }

            packet.AddRange(payload);
            var buffer = packet.ToArray();
            await stream.WriteAsync(buffer, 0, buffer.Length, cancellationToken).ConfigureAwait(false);
            await stream.FlushAsync(cancellationToken).ConfigureAwait(false);

            return packetId;
        }

        private async Task SendPingReqAsync(CancellationToken cancellationToken)
        {
            if (stream == null)
            {
                throw new InvalidOperationException("MQTT stream is not available.");
            }

            var packet = new byte[] { 0xC0, 0x00 };
            await stream.WriteAsync(packet, 0, packet.Length, cancellationToken).ConfigureAwait(false);
            await stream.FlushAsync(cancellationToken).ConfigureAwait(false);
        }

        private async Task<MqttPacket> ReadPacketAsync(CancellationToken cancellationToken)
        {
            if (stream == null)
            {
                throw new InvalidOperationException("MQTT stream is not available.");
            }

            var header = await ReadExactAsync(1, cancellationToken).ConfigureAwait(false);
            var remainingLength = await ReadRemainingLengthAsync(cancellationToken).ConfigureAwait(false);
            var payload = remainingLength > 0 ? await ReadExactAsync(remainingLength, cancellationToken).ConfigureAwait(false) : Array.Empty<byte>();
            return new MqttPacket(header[0], payload);
        }

        private async Task<int> ReadRemainingLengthAsync(CancellationToken cancellationToken)
        {
            var multiplier = 1;
            var value = 0;
            byte encodedByte;
            var loops = 0;
            do
            {
                var buffer = await ReadExactAsync(1, cancellationToken).ConfigureAwait(false);
                encodedByte = buffer[0];
                value += (encodedByte & 127) * multiplier;
                multiplier *= 128;
                loops++;
                if (loops > 4)
                {
                    throw new InvalidOperationException("Malformed MQTT remaining length.");
                }
            }
            while ((encodedByte & 128) != 0);

            return value;
        }

        private async Task<byte[]> ReadExactAsync(int count, CancellationToken cancellationToken)
        {
            var buffer = new byte[count];
            var offset = 0;
            while (offset < count)
            {
                var read = await stream.ReadAsync(buffer, offset, count - offset, cancellationToken).ConfigureAwait(false);
                if (read == 0)
                {
                    throw new IOException("MQTT connection closed by remote host.");
                }

                offset += read;
            }

            return buffer;
        }

        private ushort GetNextPacketIdentifier()
        {
            lock (stateLock)
            {
                if (packetIdentifier == 0)
                {
                    packetIdentifier = 1;
                }

                var id = packetIdentifier;
                packetIdentifier++;
                if (packetIdentifier == 0)
                {
                    packetIdentifier = 1;
                }

                return id;
            }
        }

        private static byte[] EncodeString(string value)
        {
            var bytes = Encoding.UTF8.GetBytes(value ?? string.Empty);
            if (bytes.Length > ushort.MaxValue)
            {
                throw new InvalidOperationException("MQTT string exceeds maximum allowed length.");
            }

            var buffer = new byte[bytes.Length + 2];
            buffer[0] = (byte)(bytes.Length >> 8);
            buffer[1] = (byte)(bytes.Length & 0xFF);
            Buffer.BlockCopy(bytes, 0, buffer, 2, bytes.Length);
            return buffer;
        }

        private static byte[] EncodeRemainingLength(int value)
        {
            if (value < 0)
            {
                throw new ArgumentOutOfRangeException(nameof(value));
            }

            var bytes = new List<byte>();
            do
            {
                var digit = value % 128;
                value /= 128;
                if (value > 0)
                {
                    digit |= 0x80;
                }
                bytes.Add((byte)digit);
            }
            while (value > 0);

            return bytes.ToArray();
        }

        private void ValidateConnAck(MqttPacket packet)
        {
            if ((packet.Header & 0xF0) != 0x20)
            {
                throw new InvalidOperationException("Unexpected packet while waiting for CONNACK.");
            }

            if (packet.Payload.Length < 2)
            {
                throw new InvalidOperationException("Invalid CONNACK payload.");
            }

            var returnCode = packet.Payload[1];
            if (returnCode != 0)
            {
                throw new InvalidOperationException($"MQTT broker rejected connection (code {returnCode}).");
            }
        }

        private void Cleanup(string reason)
        {
            lock (stateLock)
            {
                var wasConnected = IsConnected;
                IsConnected = false;

                try
                {
                    keepAliveCts?.Cancel();
                }
                catch
                {
                }

                keepAliveCts?.Dispose();
                keepAliveCts = null;
                keepAliveTask = null;

                if (stream != null)
                {
                    try
                    {
                        stream.Dispose();
                    }
                    catch
                    {
                    }
                    stream = null;
                }

                if (tcpClient != null)
                {
                    try
                    {
                        tcpClient.Close();
                    }
                    catch
                    {
                    }
                    tcpClient = null;
                }

                if (wasConnected)
                {
                    Disconnected?.Invoke(reason ?? "Disconnected");
                }
            }
        }

        private void ThrowIfDisposed()
        {
            if (disposed)
            {
                throw new ObjectDisposedException(nameof(SimpleMqttClient));
            }
        }

        private readonly struct MqttPacket
        {
            public MqttPacket(byte header, byte[] payload)
            {
                Header = header;
                Payload = payload;
            }

            public byte Header { get; }
            public byte[] Payload { get; }
        }
    }
}
