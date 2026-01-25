import json
from unittest import mock

from voice_agent_sdk import VoiceAgentClient
from voice_agent_sdk.client import MqttConfig


def make_client(mqtt_client=None, http_session=None):
    return VoiceAgentClient(
        host="10.0.0.1",
        mqtt_config=MqttConfig(host="10.0.0.1"),
        mqtt_client=mqtt_client,
        http_session=http_session,
    )


def test_speak_uses_get_by_default():
    session = mock.Mock()
    response = mock.Mock()
    response.content = b"audio"
    response.raise_for_status = mock.Mock()
    session.get.return_value = response

    client = make_client(http_session=session)
    payload = client.speak("hello", voice="en_US", model="piper-en")

    assert payload == b"audio"
    session.get.assert_called_once()
    args, kwargs = session.get.call_args
    assert args[0].endswith("/speak")
    assert kwargs["params"]["text"] == "hello"
    assert kwargs["params"]["voice"] == "en_US"
    assert kwargs["params"]["model"] == "piper-en"


def test_speak_uses_post_when_enabled():
    session = mock.Mock()
    response = mock.Mock()
    response.content = b"audio"
    response.raise_for_status = mock.Mock()
    session.post.return_value = response

    client = make_client(http_session=session)
    client.speak("hello", use_post=True)

    session.post.assert_called_once()


def test_face_preset_publishes_message():
    mqtt_client = mock.Mock()
    client = make_client(mqtt_client=mqtt_client)
    client.face_preset("happy", duration=2.5)

    mqtt_client.publish.assert_called_once()
    topic, payload = mqtt_client.publish.call_args[0][:2]
    data = json.loads(payload)
    assert topic == "robot/pi/face/cmd"
    assert data["action"] == "face"
    assert data["value"] == "happy:2.5"


def test_led_breathe_publishes_message():
    mqtt_client = mock.Mock()
    client = make_client(mqtt_client=mqtt_client)
    client.led_breathe(color="#00BFFF", brightness=0.8, period=2.5, duration=3)

    topic, payload = mqtt_client.publish.call_args[0][:2]
    data = json.loads(payload)
    assert topic == "robot/pi/led/cmd"
    assert data["action"] == "led"
    assert data["value"] == "breathe:#00BFFF:3:0.8:2.5"


def test_set_dialog_style_publishes_message():
    mqtt_client = mock.Mock()
    client = make_client(mqtt_client=mqtt_client)
    client.set_dialog_style("Supportive")

    topic, payload = mqtt_client.publish.call_args[0][:2]
    data = json.loads(payload)
    assert topic == "robot/dialog/style"
    assert data["style"] == "Supportive"
