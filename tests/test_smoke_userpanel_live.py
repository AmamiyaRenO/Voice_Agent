import json
import os
import urllib.error
import urllib.request

import pytest

from userpanel_smoke_cases import USERPANEL_SMOKE_CASES


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default


LIVE_ENABLED = _env_bool("VOICE_AGENT_PANEL_LIVE", False)
PANEL_BASE_URL = (os.environ.get("VOICE_AGENT_PANEL_URL") or "http://127.0.0.1:8787").rstrip("/")


def _call_panel(path: str, method: str, body_json, body_raw, timeout_sec: float):
    data = None
    headers = {}
    if body_json is not None:
        data = json.dumps(body_json).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif body_raw is not None:
        data = body_raw.encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(
        url=f"{PANEL_BASE_URL}{path}",
        data=data,
        headers=headers,
        method=method.upper(),
    )
    try:
        with urllib.request.urlopen(request, timeout=max(0.5, float(timeout_sec))) as resp:
            status = int(resp.getcode() or 0)
            payload = resp.read(1024)
            return status, payload
    except urllib.error.HTTPError as exc:
        payload = b""
        try:
            payload = exc.read(1024)
        except Exception:
            pass
        return int(exc.code or 0), payload
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        detail = str(reason) if reason is not None else str(exc)
        return 0, detail.encode("utf-8", errors="ignore")
    except TimeoutError as exc:
        return 0, str(exc).encode("utf-8", errors="ignore")


@pytest.fixture(scope="session", autouse=True)
def _panel_live_guard():
    if not LIVE_ENABLED:
        pytest.skip("Set VOICE_AGENT_PANEL_LIVE=1 to run live userpanel smoke checks.")
    status, payload = _call_panel("/healthz", "GET", None, None, 2.0)
    if status != 200:
        detail = payload.decode("utf-8", errors="ignore")
        pytest.skip(f"User panel not reachable at {PANEL_BASE_URL} (status={status}, body={detail!r})")


@pytest.mark.parametrize("path", sorted(USERPANEL_SMOKE_CASES.keys()))
def test_smoke_userpanel_live(path: str):
    ops = USERPANEL_SMOKE_CASES[path]
    for op in ops:
        status, payload = _call_panel(
            path=path,
            method=op.method,
            body_json=op.body_json,
            body_raw=op.body_raw,
            timeout_sec=op.timeout_sec,
        )
        assert status in op.expected_statuses, (
            f"path={path} method={op.method} status={status} "
            f"expected={op.expected_statuses} body={payload.decode('utf-8', errors='ignore')!r}"
        )
