from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass(frozen=True)
class PanelOp:
    method: str = "GET"
    body_json: Optional[dict] = None
    body_raw: Optional[str] = None
    timeout_sec: float = 6.0
    expected_statuses: tuple[int, ...] = (200, 400, 405, 502, 503, 504)


# Smoke operations intentionally avoid destructive behavior where possible.
# - For mutable endpoints, we usually send invalid/unknown payloads expecting 400.
# - This still verifies endpoint wiring and handler stability.
USERPANEL_SMOKE_CASES: Dict[str, List[PanelOp]] = {
    "/": [PanelOp(method="GET", expected_statuses=(200,))],
    "/index.html": [PanelOp(method="GET", expected_statuses=(200,))],
    "/panel.html": [PanelOp(method="GET", expected_statuses=(200,))],
    "/games": [PanelOp(method="GET", expected_statuses=(200,))],
    "/games.html": [PanelOp(method="GET", expected_statuses=(200,))],
    "/controls": [PanelOp(method="GET", expected_statuses=(200,))],
    "/controls.html": [PanelOp(method="GET", expected_statuses=(200,))],
    "/runtime": [PanelOp(method="GET", expected_statuses=(200,))],
    "/runtime.html": [PanelOp(method="GET", expected_statuses=(200,))],
    "/memory": [PanelOp(method="GET", expected_statuses=(200,))],
    "/memory.html": [PanelOp(method="GET", expected_statuses=(200,))],
    "/setup": [PanelOp(method="GET", expected_statuses=(200,))],
    "/setup.html": [PanelOp(method="GET", expected_statuses=(200,))],
    "/sdk": [PanelOp(method="GET", expected_statuses=(200,))],
    "/sdk.html": [PanelOp(method="GET", expected_statuses=(200,))],
    "/sdk-manifest": [PanelOp(method="GET", expected_statuses=(200,))],
    "/sdk-manifest.json": [PanelOp(method="GET", expected_statuses=(200,))],
    "/telemetry": [PanelOp(method="GET", expected_statuses=(200,))],
    "/telemetry.html": [PanelOp(method="GET", expected_statuses=(200,))],
    "/camera.mjpg": [PanelOp(method="GET", timeout_sec=2.5, expected_statuses=(200, 503, 0))],
    "/healthz": [PanelOp(method="GET", expected_statuses=(200,))],
    # /api/face may block on MQTT publish when broker/network is unhealthy.
    # Keep the endpoint in live sweep but tolerate client-side timeout status 0.
    "/api/face": [
        PanelOp(
            method="POST",
            body_json={"mode": "idle", "seconds": 0.1},
            expected_statuses=(200, 503, 504, 0),
        )
    ],
    "/api/flower": [PanelOp(method="POST", body_json={"action": "invalid"})],
    "/api/led": [PanelOp(method="POST", body_json={"mode": "invalid"})],
    "/api/voice": [PanelOp(method="POST", body_json={"action": "invalid"})],
    "/api/voice/options": [PanelOp(method="GET", expected_statuses=(200,))],
    "/api/kokoro/options": [PanelOp(method="GET", expected_statuses=(200,))],
    "/api/logs": [PanelOp(method="GET", expected_statuses=(200,))],
    "/api/speak": [PanelOp(method="POST", body_json={"text": ""}, expected_statuses=(400, 503))],
    "/api/kokoro/speak": [PanelOp(method="POST", body_json={"text": ""}, expected_statuses=(400, 503))],
    "/api/llm/prompt": [
        PanelOp(method="GET"),
        PanelOp(method="POST", body_json={"prompt": ""}, expected_statuses=(400, 502, 503)),
    ],
    # Use GET to validate route wiring without triggering heavy vision inference.
    "/api/vision/describe": [PanelOp(method="GET", expected_statuses=(405,))],
    "/api/game": [PanelOp(method="POST", body_json={"action": "invalid"}, expected_statuses=(400, 503))],
    "/api/game/manifest": [
        PanelOp(method="GET", expected_statuses=(200, 500)),
        PanelOp(method="POST", body_json={"games": "invalid"}, expected_statuses=(400, 500)),
    ],
    "/api/file/pick": [PanelOp(method="GET", expected_statuses=(405,))],
    "/api/runtime/config": [
        PanelOp(method="GET", expected_statuses=(200, 500)),
        PanelOp(method="POST", body_raw="[]", expected_statuses=(400,)),
    ],
    "/api/runtime/prereq": [PanelOp(method="GET", expected_statuses=(200,))],
    "/api/runtime/ollama": [PanelOp(method="POST", body_json={"action": "invalid"}, expected_statuses=(400,))],
    "/api/memory": [
        PanelOp(method="GET", expected_statuses=(200, 500)),
        PanelOp(method="POST", body_json={"action": "invalid"}, expected_statuses=(400, 500)),
    ],
    "/api/asr": [
        PanelOp(method="GET"),
        PanelOp(method="POST", body_json={"action": "invalid"}, expected_statuses=(400, 502, 503)),
    ],
    "/api/asr/backend": [
        PanelOp(method="GET"),
        PanelOp(method="POST", body_json={"action": "invalid"}, expected_statuses=(400, 502, 503)),
    ],
    "/camera.jpg": [PanelOp(method="GET", expected_statuses=(200, 503))],
    "/api/camera/status": [PanelOp(method="GET", expected_statuses=(200,))],
    "/api/camera/ping": [PanelOp(method="GET", expected_statuses=(200,))],
    "/favicon.ico": [PanelOp(method="GET", expected_statuses=(204,))],
}
