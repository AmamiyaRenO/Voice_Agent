import inspect
import json
from pathlib import Path
from unittest import mock

import pytest

from voice_agent_sdk import VoiceAgentClient


ROOT = Path(__file__).resolve().parents[1]
SDK_MANIFEST_PATH = ROOT / "Assets" / "StreamingAssets" / "panel" / "sdk-manifest.json"
SDK_HTML_PATH = ROOT / "Assets" / "StreamingAssets" / "panel" / "sdk.html"
REMOVED_METHODS = {
    "connect_mqtt",
    "disconnect_mqtt",
    "synthesize_wav",
    "set_dialog_style",
    "publish_raw",
    "face_idle",
    "servo_open",
    "servo_close",
    "servo_open_hold",
    "servo_close_hold",
    "servo_center_hold",
    "servo_stop",
    "servo_open_slow",
    "servo_close_slow",
}


def load_manifest():
    return json.loads(SDK_MANIFEST_PATH.read_text(encoding="utf-8"))


def make_client(http_session=None):
    return VoiceAgentClient(host="10.0.0.1", http_session=http_session)


def invoke_manifest_method(client: VoiceAgentClient, method_name: str, payload: dict):
    if method_name in {"get_logs", "get_tts_options", "get_kokoro_options", "get_llm_prompt", "get_runtime_config", "get_asr_status", "start_listening", "pause_listening", "exit_game", "led_off", "flower_open", "flower_close", "flower_stop", "flower_open_slow", "flower_close_slow", "reset_llm_prompt"}:
        return getattr(client, method_name)()
    if method_name == "speak":
        return client.speak(
            payload["text"],
            voice=payload.get("voice"),
            model=payload.get("model"),
            speed=payload.get("speed", 1.0),
            volume=payload.get("volume", 1.0),
            backend=payload.get("backend"),
        )
    if method_name in {"set_voice", "set_kokoro_voice"}:
        return getattr(client, method_name)(payload["voice"])
    if method_name == "set_tts_model":
        return client.set_tts_model(payload["model"])
    if method_name == "set_tts_backend":
        return client.set_tts_backend(payload["backend"])
    if method_name == "kokoro_speak":
        return client.kokoro_speak(payload["text"], voice=payload.get("voice"))
    if method_name == "set_llm_prompt":
        return client.set_llm_prompt(payload["prompt"])
    if method_name == "set_local_model":
        return client.set_local_model(payload["ollama_model"])
    if method_name in {"set_asr_mode", "set_backend_asr_mode"}:
        return getattr(client, method_name)(payload["mode"])
    if method_name == "describe_current_camera":
        return client.describe_current_camera(payload["prompt"], model=payload.get("model"))
    if method_name == "launch_game":
        return client.launch_game(payload["name"])
    if method_name == "face_preset":
        return client.face_preset(payload["mode"], seconds=payload["seconds"])
    if method_name == "face_custom":
        return client.face_custom(payload["value"], seconds=payload["seconds"])
    if method_name in {"face_happy", "face_neutral", "face_sad", "face_very_sad", "face_excited"}:
        return getattr(client, method_name)(seconds=payload["seconds"])
    if method_name == "led_breathe":
        return client.led_breathe(
            color=payload["color"],
            brightness=payload["brightness"],
            period=payload["period"],
            duration=payload["duration"],
        )
    if method_name == "led_solid":
        return client.led_solid(
            color=payload["color"],
            brightness=payload["brightness"],
            duration=payload["duration"],
        )
    if method_name == "led_random":
        return client.led_random(duration=payload["duration"])
    raise AssertionError(f"Unhandled manifest method in test: {method_name}")


def test_sdk_manifest_matches_client_public_methods():
    manifest = load_manifest()
    manifest_methods = {entry["client_method"] for entry in manifest["methods"]}
    public_methods = {
        name
        for name, value in inspect.getmembers(VoiceAgentClient, predicate=callable)
        if not name.startswith("_") and name != "__init__"
    }

    assert public_methods == manifest_methods
    assert not (REMOVED_METHODS & public_methods)


def test_sdk_visualizer_uses_shared_manifest():
    source = SDK_HTML_PATH.read_text(encoding="utf-8")
    assert "/sdk-manifest.json" in source
    assert "const sdkMap = {" not in source
    assert "buildSdkMap(manifest)" in source


@pytest.mark.parametrize(
    "entry",
    load_manifest()["methods"],
    ids=lambda entry: entry["client_method"],
)
def test_every_manifest_method_calls_expected_endpoint(entry):
    session = mock.Mock()
    response = mock.Mock()
    response.raise_for_status = mock.Mock()
    response.json.return_value = {"status": "ok", "method": entry["client_method"]}
    session.request.return_value = response

    client = make_client(http_session=session)
    result = invoke_manifest_method(client, entry["client_method"], entry.get("payload") or {})

    assert result["status"] == "ok"
    session.request.assert_called_once()
    args, kwargs = session.request.call_args
    assert args[0] == entry["http_method"]
    assert args[1] == f"http://10.0.0.1:8787{entry['endpoint']}"
    expected_payload = entry.get("payload") or None
    assert kwargs.get("json") == expected_payload
    assert kwargs["timeout"] > 0


def test_speak_requires_text():
    client = make_client(http_session=mock.Mock())
    with pytest.raises(ValueError, match="text is required"):
        client.speak("   ")


def test_set_voice_requires_value():
    client = make_client(http_session=mock.Mock())
    with pytest.raises(ValueError, match="voice is required"):
        client.set_voice("")
