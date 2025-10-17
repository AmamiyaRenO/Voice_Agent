using System.Text;
using System.Threading.Tasks;
using RobotVoice;
using RobotVoice.Mqtt;
using UnityEngine;

public class PiMessageHub : MonoBehaviour
{
	[SerializeField] private MqttIntentPublisher publisher; // 指向 MessageHubPi 上的 MqttIntentPublisher (host=10.0.0.1)
	[SerializeField] private string faceTopic = "robot/pi/face/cmd";
	[SerializeField] private string servoTopic = "robot/pi/servo/cmd";
	[SerializeField] private string ledTopic = "robot/pi/led/cmd";

	[Header("Defaults")]
	[SerializeField] private float defaultServoSeconds = 2f;
	[SerializeField] private float defaultFaceSeconds = 3f;
	[SerializeField] private float defaultLedBreathSeconds = 2f;
	[SerializeField] private float defaultLedOnSeconds = 1f;
	[SerializeField] private float defaultSlowSeconds = 3f; // 测试用更慢的时长
	[SerializeField] private int defaultSpeedPercent = 30;  // 发送给支持速度的守护进程（若不支持会被忽略）

	void Reset()
	{
		if (publisher == null) publisher = GetComponent<MqttIntentPublisher>();
	}

    public async Task SendFaceAsync(string valueWithSeconds)
    {
        var payload = "{" +
                      "\"action\":\"face\"," +
                      "\"value\":\"" + Escape(valueWithSeconds) + "\"" +
                      "}";
        await PublishRawAsync(faceTopic, payload);
    }

	public async Task SendFaceHappyAsync()
	{
		await SendFaceAsync($"happy:{defaultFaceSeconds}");
	}

	public async Task SendFaceIdleAsync()
	{
		await SendFaceAsync("idle");
	}

    public async Task SendServoAsync(string value)
    {
		var payload = "{" +
		              "\"action\":\"servo\"," +
		              "\"value\":\"" + Escape(value) + "\"" +
		              "}";
        await PublishRawAsync(servoTopic, payload);
    }

	public async Task OpenFlowerAsync()
	{
		await SendServoAsync($"open:{defaultServoSeconds}");
	}

	public async Task CloseFlowerAsync()
	{
		await SendServoAsync($"close:{defaultServoSeconds}");
	}

	// 保持打开：open:0（直到 stop）
	public async Task OpenFlowerHoldAsync()
	{
		await SendServoAsync("open:0");
	}

	// 保持关闭：close:0（直到 stop）
	public async Task CloseFlowerHoldAsync()
	{
		await SendServoAsync("close:0");
	}

	// 保持居中：center:0（直到 stop）
	public async Task CenterFlowerHoldAsync()
	{
		await SendServoAsync("center:0");
	}

	// 停止并释放：stop
	public async Task StopFlowerAsync()
	{
		await SendServoAsync("stop");
	}

	// 仅开（慢速），发送 open:<seconds> 并附加 speed 参数（若 Pi 端支持）
	public async Task OpenFlowerSlowAsync()
	{
		var value = "open:" + defaultSlowSeconds.ToString(System.Globalization.CultureInfo.InvariantCulture);
		var payload = "{" +
		              "\"action\":\"servo\"," +
		              "\"value\":\"" + Escape(value) + "\"," +
		              "\"speed\":" + defaultSpeedPercent.ToString(System.Globalization.CultureInfo.InvariantCulture) +
		              "}";
		await PublishRawAsync(servoTopic, payload);
	}

	// 仅关（慢速）
	public async Task CloseFlowerSlowAsync()
	{
		var value = "close:" + defaultSlowSeconds.ToString(System.Globalization.CultureInfo.InvariantCulture);
		var payload = "{" +
		              "\"action\":\"servo\"," +
		              "\"value\":\"" + Escape(value) + "\"," +
		              "\"speed\":" + defaultSpeedPercent.ToString(System.Globalization.CultureInfo.InvariantCulture) +
		              "}";
		await PublishRawAsync(servoTopic, payload);
	}

    public async Task SendLedBreathAsync(string hexColor = "#00BFFF")
    {
		// 适配 ledScript.py: breathe:#RRGGBB[:duration[:brightness[:period]]]
		var duration = defaultLedBreathSeconds.ToString(System.Globalization.CultureInfo.InvariantCulture);
		var value = "breathe:" + Escape(hexColor) + ":" + duration + ":1.0:1.5"; // brightness=1.0, period=1.5s
		var json = "{" +
				   "\"action\":\"led\"," +
				   "\"value\":\"" + Escape(value) + "\"" +
				   "}";
		await PublishRawAsync(ledTopic, json);
    }

	public async Task SendLedRandomAsync()
	{
		// 适配 ledScript.py: on:#RRGGBB[:duration[:brightness]]
		var rand = new System.Random();
		int r = rand.Next(0, 256), g = rand.Next(0, 256), b = rand.Next(0, 256);
		var hex = "#" + r.ToString("X2") + g.ToString("X2") + b.ToString("X2");
		var duration = defaultLedOnSeconds.ToString(System.Globalization.CultureInfo.InvariantCulture);
		var value = "on:" + hex + ":" + duration + ":1.0";
		var json = "{" +
				   "\"action\":\"led\"," +
				   "\"value\":\"" + Escape(value) + "\"" +
				   "}";
		await PublishRawAsync(ledTopic, json);
	}

	public async Task SendLedOffAsync()
	{
		var json = "{" +
				   "\"action\":\"led\"," +
				   "\"value\":\"off\"" +
				   "}";
		await PublishRawAsync(ledTopic, json);
	}

    // Inspector 右上角 Context Menu 便捷测试
    [ContextMenu("PI/Face Happy (default)")]
    private void CtxFaceHappy() { _ = SendFaceHappyAsync(); }

    [ContextMenu("PI/Face Idle")]
    private void CtxFaceIdle() { _ = SendFaceIdleAsync(); }

    [ContextMenu("PI/Servo Open (default)")]
    private void CtxOpen() { _ = OpenFlowerAsync(); }

    [ContextMenu("PI/Servo Close (default)")]
    private void CtxClose() { _ = CloseFlowerAsync(); }

    [ContextMenu("PI/LED Breathe (default)")]
    private void CtxLed() { _ = SendLedBreathAsync(); }

    private async Task PublishRawAsync(string topic, string payload)
	{
		if (publisher == null)
		{
			Debug.LogWarning("[PiMessageHub] publisher is null");
			return;
		}
        await publisher.PublishRawAsync(topic, payload, SimpleMqttQualityOfServiceLevel.AtLeastOnce);
	}

	private static string Escape(string s)
	{
		return string.IsNullOrEmpty(s) ? string.Empty : s.Replace("\\", "\\\\").Replace("\"", "\\\"");
	}
}

// (Raw publish moved to MqttIntentPublisher.PublishRawAsync)
