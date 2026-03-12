import sys
from pathlib import Path


def test_main_app_import_smoke():
    root = Path(__file__).resolve().parents[1]
    service_path = root / "python_voice_service"
    if str(service_path) not in sys.path:
        sys.path.insert(0, str(service_path))

    import main  # type: ignore

    assert getattr(main, "app", None) is not None
