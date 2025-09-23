using System;
using System.Text;

namespace RobotVoice.Mqtt
{
    public enum SimpleMqttQualityOfServiceLevel
    {
        AtMostOnce = 0,
        AtLeastOnce = 1,
        ExactlyOnce = 2
    }

    public sealed class SimpleMqttApplicationMessage
    {
        public string Topic { get; }
        public byte[] Payload { get; }
        public SimpleMqttQualityOfServiceLevel QualityOfServiceLevel { get; }

        internal SimpleMqttApplicationMessage(string topic, byte[] payload, SimpleMqttQualityOfServiceLevel qos)
        {
            Topic = topic;
            Payload = payload;
            QualityOfServiceLevel = qos;
        }
    }

    public sealed class SimpleMqttApplicationMessageBuilder
    {
        private string topic;
        private byte[] payload = Array.Empty<byte>();
        private SimpleMqttQualityOfServiceLevel qos = SimpleMqttQualityOfServiceLevel.AtMostOnce;

        public SimpleMqttApplicationMessageBuilder WithTopic(string topic)
        {
            this.topic = topic;
            return this;
        }

        public SimpleMqttApplicationMessageBuilder WithPayload(string payload)
        {
            if (payload == null)
            {
                this.payload = Array.Empty<byte>();
            }
            else
            {
                this.payload = Encoding.UTF8.GetBytes(payload);
            }
            return this;
        }

        public SimpleMqttApplicationMessageBuilder WithPayload(byte[] payload)
        {
            if (payload == null || payload.Length == 0)
            {
                this.payload = Array.Empty<byte>();
            }
            else
            {
                var copy = new byte[payload.Length];
                Buffer.BlockCopy(payload, 0, copy, 0, payload.Length);
                this.payload = copy;
            }
            return this;
        }

        public SimpleMqttApplicationMessageBuilder WithQualityOfServiceLevel(SimpleMqttQualityOfServiceLevel qos)
        {
            this.qos = qos;
            return this;
        }

        public SimpleMqttApplicationMessage Build()
        {
            if (string.IsNullOrWhiteSpace(topic))
            {
                throw new InvalidOperationException("MQTT messages require a non-empty topic.");
            }

            return new SimpleMqttApplicationMessage(topic, payload, qos);
        }
    }
}
