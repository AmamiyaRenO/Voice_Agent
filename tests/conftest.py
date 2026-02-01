import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SDK_PATH = ROOT / "python_sdk"
if str(SDK_PATH) not in sys.path:
    sys.path.insert(0, str(SDK_PATH))

# Guardrail: ensure tests import the in-repo SDK (python_sdk/voice_agent_sdk),
# not an accidentally installed site-packages version with the same name.
try:
    import voice_agent_sdk as _voice_agent_sdk  # noqa: F401

    _sdk_file = Path(getattr(_voice_agent_sdk, "__file__", "")).resolve()
    if SDK_PATH.resolve() not in _sdk_file.parents:
        raise RuntimeError(
            "voice_agent_sdk was imported from an unexpected location.\n"
            f"Expected under: {SDK_PATH}\n"
            f"Actual:        {_sdk_file}\n"
            "Fix: uninstall the external package (pip uninstall voice-agent-sdk / voice_agent_sdk),\n"
            "or ensure you run pytest from the repo root so tests prioritize python_sdk/."
        )
except Exception as e:
    # Re-raise as RuntimeError to make pytest output clearer.
    raise RuntimeError(str(e)) from e