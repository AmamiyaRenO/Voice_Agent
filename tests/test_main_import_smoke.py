import importlib.util
import sys
from pathlib import Path


def test_main_app_import_smoke():
    root = Path(__file__).resolve().parents[1]
    service_path = root / "python_voice_service"
    main_path = service_path / "main.py"
    spec = importlib.util.spec_from_file_location("python_voice_service_main_smoke", str(main_path))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["python_voice_service_main_smoke"] = module
    spec.loader.exec_module(module)

    assert getattr(module, "app", None) is not None
