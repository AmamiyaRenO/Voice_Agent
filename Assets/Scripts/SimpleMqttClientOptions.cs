using System;
using System.Collections.Generic;
using System.Net.Security;
using System.Security.Authentication;
using System.Security.Cryptography.X509Certificates;

namespace RobotVoice.Mqtt
{
    public sealed class SimpleMqttClientOptions
    {
        public string Host { get; }
        public int Port { get; }
        public string ClientId { get; }
        public bool CleanSession { get; }
        public TimeSpan KeepAlivePeriod { get; }
        public string Username { get; }
        public string Password { get; }
        public SimpleMqttTlsOptions TlsOptions { get; }

        internal SimpleMqttClientOptions(
            string host,
            int port,
            string clientId,
            bool cleanSession,
            TimeSpan keepAlivePeriod,
            string username,
            string password,
            SimpleMqttTlsOptions tlsOptions)
        {
            Host = host;
            Port = port;
            ClientId = clientId;
            CleanSession = cleanSession;
            KeepAlivePeriod = keepAlivePeriod;
            Username = username;
            Password = password;
            TlsOptions = tlsOptions ?? SimpleMqttTlsOptions.Disabled;
        }
    }

    public sealed class SimpleMqttClientOptionsBuilder
    {
        private string host = "127.0.0.1";
        private int port = 1883;
        private string clientId = Guid.NewGuid().ToString("N");
        private bool cleanSession = true;
        private TimeSpan keepAlivePeriod = TimeSpan.FromSeconds(15);
        private string username;
        private string password;
        private SimpleMqttTlsOptions tlsOptions = SimpleMqttTlsOptions.Disabled;

        public SimpleMqttClientOptionsBuilder WithTcpServer(string host, int port = 1883)
        {
            if (string.IsNullOrWhiteSpace(host))
            {
                throw new ArgumentException("Host must be provided", nameof(host));
            }

            if (port <= 0 || port > ushort.MaxValue)
            {
                throw new ArgumentOutOfRangeException(nameof(port), "Port must be between 1 and 65535.");
            }

            this.host = host;
            this.port = port;
            return this;
        }

        public SimpleMqttClientOptionsBuilder WithClientId(string clientId)
        {
            if (!string.IsNullOrWhiteSpace(clientId))
            {
                this.clientId = clientId;
            }
            return this;
        }

        public SimpleMqttClientOptionsBuilder WithCleanSession(bool cleanSession = true)
        {
            this.cleanSession = cleanSession;
            return this;
        }

        public SimpleMqttClientOptionsBuilder WithKeepAlivePeriod(TimeSpan keepAlivePeriod)
        {
            if (keepAlivePeriod < TimeSpan.Zero)
            {
                throw new ArgumentOutOfRangeException(nameof(keepAlivePeriod), "KeepAlive must not be negative.");
            }

            this.keepAlivePeriod = keepAlivePeriod;
            return this;
        }

        public SimpleMqttClientOptionsBuilder WithCredentials(string username, string password)
        {
            this.username = username;
            this.password = password;
            return this;
        }

        public SimpleMqttClientOptionsBuilder WithTls()
        {
            tlsOptions = SimpleMqttTlsOptions.CreateDefault();
            return this;
        }

        public SimpleMqttClientOptionsBuilder WithTls(Action<SimpleMqttTlsOptionsBuilder> configure)
        {
            if (configure == null)
            {
                return WithTls();
            }

            var builder = new SimpleMqttTlsOptionsBuilder();
            configure(builder);
            tlsOptions = builder.Build();
            return this;
        }

        public SimpleMqttClientOptionsBuilder WithoutTls()
        {
            tlsOptions = SimpleMqttTlsOptions.Disabled;
            return this;
        }

        public SimpleMqttClientOptions Build()
        {
            if (string.IsNullOrWhiteSpace(host))
            {
                throw new InvalidOperationException("MQTT host must be configured before building options.");
            }

            if (string.IsNullOrEmpty(clientId))
            {
                clientId = Guid.NewGuid().ToString("N");
            }

            return new SimpleMqttClientOptions(host, port, clientId, cleanSession, keepAlivePeriod, username, password, tlsOptions);
        }
    }

    public sealed class SimpleMqttTlsOptions
    {
        private readonly X509CertificateCollection clientCertificates;

        internal static SimpleMqttTlsOptions Disabled { get; } = new SimpleMqttTlsOptions(false, null, false, true, null, null, null);

        internal static SimpleMqttTlsOptions CreateDefault()
        {
            return new SimpleMqttTlsOptions(true, null, false, true, null, null, null);
        }

        internal SimpleMqttTlsOptions(
            bool useTls,
            string targetHost,
            bool allowUntrustedCertificates,
            bool checkCertificateRevocation,
            X509CertificateCollection clientCertificates,
            Func<X509Certificate, X509Chain, SslPolicyErrors, bool> certificateValidationCallback,
            SslProtocols? sslProtocols)
        {
            UseTls = useTls;
            TargetHost = targetHost;
            AllowUntrustedCertificates = allowUntrustedCertificates;
            CheckCertificateRevocation = checkCertificateRevocation;
            if (clientCertificates != null && clientCertificates.Count > 0)
            {
                this.clientCertificates = new X509CertificateCollection(clientCertificates);
            }

            CertificateValidationCallback = certificateValidationCallback;
            SslProtocols = sslProtocols;
        }

        public bool UseTls { get; }
        public string TargetHost { get; }
        public bool AllowUntrustedCertificates { get; }
        public bool CheckCertificateRevocation { get; }
        public Func<X509Certificate, X509Chain, SslPolicyErrors, bool> CertificateValidationCallback { get; }
        public SslProtocols? SslProtocols { get; }

        public X509CertificateCollection ClientCertificates
        {
            get
            {
                if (clientCertificates == null)
                {
                    return new X509CertificateCollection();
                }

                return new X509CertificateCollection(clientCertificates);
            }
        }
    }

    public sealed class SimpleMqttTlsOptionsBuilder
    {
        private string targetHost;
        private bool allowUntrustedCertificates;
        private bool checkCertificateRevocation = true;
        private SslProtocols? sslProtocols;
        private Func<X509Certificate, X509Chain, SslPolicyErrors, bool> certificateValidationCallback;
        private readonly List<X509Certificate> clientCertificates = new List<X509Certificate>();

        public SimpleMqttTlsOptionsBuilder WithTargetHost(string host)
        {
            if (!string.IsNullOrWhiteSpace(host))
            {
                targetHost = host;
            }

            return this;
        }

        public SimpleMqttTlsOptionsBuilder AllowUntrustedCertificates(bool allow = true)
        {
            allowUntrustedCertificates = allow;
            if (allow)
            {
                checkCertificateRevocation = false;
            }

            return this;
        }

        public SimpleMqttTlsOptionsBuilder CheckCertificateRevocation(bool check = true)
        {
            checkCertificateRevocation = check;
            return this;
        }

        public SimpleMqttTlsOptionsBuilder WithSslProtocols(SslProtocols protocols)
        {
            sslProtocols = protocols;
            return this;
        }

        public SimpleMqttTlsOptionsBuilder WithCertificateValidationCallback(Func<X509Certificate, X509Chain, SslPolicyErrors, bool> callback)
        {
            certificateValidationCallback = callback;
            return this;
        }

        public SimpleMqttTlsOptionsBuilder WithClientCertificate(X509Certificate certificate)
        {
            if (certificate != null)
            {
                clientCertificates.Add(certificate);
            }

            return this;
        }

        public SimpleMqttTlsOptionsBuilder WithClientCertificates(IEnumerable<X509Certificate> certificates)
        {
            if (certificates == null)
            {
                return this;
            }

            foreach (var certificate in certificates)
            {
                if (certificate != null)
                {
                    clientCertificates.Add(certificate);
                }
            }

            return this;
        }

        internal SimpleMqttTlsOptions Build()
        {
            X509CertificateCollection certificateCollection = null;
            if (clientCertificates.Count > 0)
            {
                certificateCollection = new X509CertificateCollection();
                foreach (var certificate in clientCertificates)
                {
                    certificateCollection.Add(certificate);
                }
            }

            return new SimpleMqttTlsOptions(
                true,
                targetHost,
                allowUntrustedCertificates,
                checkCertificateRevocation,
                certificateCollection,
                certificateValidationCallback,
                sslProtocols);
        }
    }
}
