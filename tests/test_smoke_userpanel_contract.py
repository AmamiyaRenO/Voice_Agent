import re
from pathlib import Path

from userpanel_smoke_cases import USERPANEL_SMOKE_CASES


ROOT = Path(__file__).resolve().parents[1]
PANEL_CS = ROOT / "Assets" / "Scripts" / "UserTestControlPanel.cs"


def _extract_panel_routes(source: str) -> set[str]:
    # Restrict to the main switch(path) block in HandleRequestAsync.
    match = re.search(r"switch\s*\(\s*path\s*\)\s*\{(?P<body>.*?)default\s*:", source, re.S)
    if not match:
        raise RuntimeError("Failed to locate switch(path) block in UserTestControlPanel.cs")
    body = match.group("body")
    routes = set(re.findall(r'case\s+"([^"]+)"\s*:', body))
    return routes


def test_smoke_userpanel_route_coverage_map_is_complete():
    source = PANEL_CS.read_text(encoding="utf-8", errors="ignore")
    routes = _extract_panel_routes(source)
    covered = set(USERPANEL_SMOKE_CASES.keys())

    missing = sorted(routes - covered)
    extra = sorted(covered - routes)

    assert not missing, f"User panel routes missing smoke cases: {missing}"
    assert not extra, f"Smoke cases reference non-existent user panel routes: {extra}"

