import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException


def _runtime_module():
    import sys

    root = Path(__file__).resolve().parents[1]
    service_dir = root / "python_voice_service"
    if str(service_dir) not in sys.path:
        sys.path.insert(0, str(service_dir))
    import desktop_runtime

    return desktop_runtime


@pytest.mark.parametrize(
    ("name", "content_type"),
    [
        ("app.css", "text/css; charset=utf-8"),
        ("shell.js", "application/javascript; charset=utf-8"),
        ("theme.js", "application/javascript; charset=utf-8"),
        ("rachel-device.png", "image/png"),
        ("sdk-manifest.json", "application/json; charset=utf-8"),
        ("lucide.LICENSE.txt", "text/plain; charset=utf-8"),
    ],
)
def test_panel_asset_resolver_serves_known_types(tmp_path: Path, name: str, content_type: str):
    runtime = _runtime_module()
    asset = tmp_path / name
    asset.write_bytes(b"asset")

    resolved, media_type = runtime._resolve_panel_asset_from_candidates(name, [tmp_path])

    assert resolved == asset.resolve()
    assert media_type == content_type


@pytest.mark.parametrize(
    "name",
    [
        "../secret.txt",
        "..\\secret.txt",
        "/absolute.css",
        "nested/app.css",
        "nested\\app.css",
        "payload.exe",
        "missing.js",
    ],
)
def test_panel_asset_resolver_rejects_traversal_unknown_types_and_missing_files(tmp_path: Path, name: str):
    runtime = _runtime_module()

    with pytest.raises(HTTPException) as error:
        runtime._resolve_panel_asset_from_candidates(name, [tmp_path])

    assert error.value.status_code == 404


def test_python_panel_routes_serve_all_pages_and_shared_assets():
    runtime = _runtime_module()

    async def exercise_routes():
        transport = httpx.ASGITransport(app=runtime.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://panel") as client:
            for route in (
                "/index.html",
                "/controls.html",
                "/games.html",
                "/memory.html",
                "/runtime.html",
                "/setup.html",
                "/sdk.html",
                "/telemetry.html",
            ):
                response = await client.get(route)
                assert response.status_code == 200, route
                assert response.headers["content-type"].startswith("text/html"), route

            expected_types = {
                "/panel-assets/app.css": "text/css",
                "/panel-assets/shell.js": "application/javascript",
                "/panel-assets/theme.js": "application/javascript",
                "/panel-assets/rachel-device.png": "image/png",
                "/panel-assets/lucide.LICENSE.txt": "text/plain",
            }
            for route, expected_type in expected_types.items():
                response = await client.get(route)
                assert response.status_code == 200, route
                assert response.headers["content-type"].startswith(expected_type), route

            for route in (
                "/panel-assets/%2e%2e%2fsecret.txt",
                "/panel-assets/nested%2fapp.css",
                "/panel-assets/payload.exe",
                "/panel-assets/missing.js",
            ):
                assert (await client.get(route)).status_code == 404, route

    asyncio.run(exercise_routes())


def test_runtime_payload_exposes_voice_identification_flags(tmp_path: Path):
    runtime = _runtime_module()
    payload = runtime._build_runtime_payload(
        {
            "env": {
                "VOICE_SPEAKER_ID_ENABLED": "1",
                "VOICE_SPEAKER_ID_AUTO_GUEST_LEARNING": "0",
            }
        },
        user_path=tmp_path / "user.json",
        default_path=tmp_path / "default.json",
        message="loaded",
    )

    assert payload["speaker_id_enabled"] is True
    assert payload["speaker_auto_learning_enabled"] is False


def test_manifest_status_reports_launch_readiness_and_path_error(tmp_path: Path):
    runtime = _runtime_module()
    executable = tmp_path / "ready.exe"
    executable.write_bytes(b"")
    manifest = tmp_path / "games.json"
    manifest.write_text(
        '{"games": ['
        f'{{"id": "ready", "name": "Ready", "exec": "{str(executable).replace(chr(92), chr(92) * 2)}"}},'
        '{"id": "missing", "name": "Missing", "exec": "missing.exe"}'
        '] }',
        encoding="utf-8",
    )

    payload = runtime._manifest_status_payload(manifest)
    ready, missing = payload["games"]

    assert ready["launch_ready"] is True
    assert ready["path_error"] == ""
    assert missing["launch_ready"] is False
    assert "Executable not found" in missing["path_error"]
