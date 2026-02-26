#!/usr/bin/env python3
"""Build Windows service executables with PyInstaller."""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Tuple


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
VOICE_DIR = REPO_ROOT / "python_voice_service"


@dataclass
class ServiceSpec:
    name: str
    entry: Path
    python_paths: List[Path] = field(default_factory=list)
    hidden_imports: List[str] = field(default_factory=list)
    collect_all: List[str] = field(default_factory=list)
    collect_submodules: List[str] = field(default_factory=list)
    add_data: List[Tuple[Path, str]] = field(default_factory=list)
    enabled_by_default: bool = True
    required_modules: List[str] = field(default_factory=list)


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


def _unique(values: Iterable[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for value in values:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _uvicorn_hidden_imports() -> List[str]:
    return [
        "uvicorn.logging",
        "uvicorn.loops.auto",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan.on",
    ]


def _service_specs() -> Dict[str, ServiceSpec]:
    specs = [
        ServiceSpec(
            name="voice_service",
            entry=SCRIPTS_DIR / "packaging" / "entrypoints" / "voice_service_entry.py",
            python_paths=[VOICE_DIR],
            hidden_imports=_uvicorn_hidden_imports(),
            collect_all=["faster_whisper", "ctranslate2", "tokenizers", "av"],
            required_modules=["uvicorn", "fastapi", "numpy", "httpx", "faster_whisper"],
        ),
        ServiceSpec(
            name="piper_http",
            entry=SCRIPTS_DIR / "packaging" / "entrypoints" / "piper_http_entry.py",
            python_paths=[VOICE_DIR],
            hidden_imports=_uvicorn_hidden_imports(),
            required_modules=["uvicorn", "fastapi"],
        ),
        ServiceSpec(
            name="qwen_tts_http",
            entry=SCRIPTS_DIR / "packaging" / "entrypoints" / "qwen_tts_http_entry.py",
            python_paths=[VOICE_DIR],
            hidden_imports=_uvicorn_hidden_imports(),
            collect_all=["qwen_tts", "torch", "torchaudio", "transformers"],
            enabled_by_default=False,
            required_modules=["uvicorn", "fastapi", "numpy", "qwen_tts"],
        ),
        ServiceSpec(
            name="intent_service",
            entry=SCRIPTS_DIR / "intent_service" / "main.py",
            python_paths=[SCRIPTS_DIR],
            add_data=[
                (SCRIPTS_DIR / "intent_service" / "config.yaml", "intent_service"),
                (SCRIPTS_DIR / "intent_service" / "manifest.json", "intent_service"),
            ],
            required_modules=["paho.mqtt.client", "yaml"],
        ),
        ServiceSpec(
            name="dialog_service",
            entry=SCRIPTS_DIR / "dialog_service" / "main.py",
            python_paths=[SCRIPTS_DIR],
            required_modules=["paho.mqtt.client", "httpx"],
        ),
        ServiceSpec(
            name="telemetry_service",
            entry=SCRIPTS_DIR / "telemetry_service" / "main.py",
            python_paths=[SCRIPTS_DIR],
            hidden_imports=_uvicorn_hidden_imports(),
            required_modules=["uvicorn", "fastapi", "paho.mqtt.client"],
        ),
        ServiceSpec(
            name="game_launcher",
            entry=SCRIPTS_DIR / "game_launcher" / "main.py",
            python_paths=[SCRIPTS_DIR],
            add_data=[
                (SCRIPTS_DIR / "game_launcher" / "config.yaml", "game_launcher"),
                (SCRIPTS_DIR / "intent_service" / "manifest.json", "intent_service"),
            ],
            required_modules=["paho.mqtt.client", "yaml"],
        ),
        ServiceSpec(
            name="service_launcher",
            entry=SCRIPTS_DIR / "start_local_services.py",
            python_paths=[SCRIPTS_DIR],
            add_data=[
                (SCRIPTS_DIR / "local_services.default.json", "scripts"),
                (SCRIPTS_DIR / "local_services.user.sample.json", "scripts"),
            ],
        ),
    ]
    return {spec.name: spec for spec in specs}


def _default_service_names(specs: Dict[str, ServiceSpec], include_qwen: bool) -> List[str]:
    names: List[str] = []
    for name, spec in specs.items():
        if spec.enabled_by_default:
            names.append(name)
    if include_qwen and "qwen_tts_http" not in names:
        names.append("qwen_tts_http")
    return names


def _parse_services(raw: str, specs: Dict[str, ServiceSpec], include_qwen: bool) -> List[ServiceSpec]:
    if raw.strip().lower() == "default":
        names = _default_service_names(specs, include_qwen)
    else:
        names = [part.strip() for part in raw.split(",") if part.strip()]

    unknown = [name for name in names if name not in specs]
    if unknown:
        known = ", ".join(sorted(specs.keys()))
        raise ValueError(f"Unknown services: {', '.join(unknown)}. Known: {known}")

    return [specs[name] for name in names]


def _build_command(
    spec: ServiceSpec,
    *,
    output_dir: Path,
    work_root: Path,
    spec_root: Path,
    clean: bool,
) -> List[str]:
    if not spec.entry.exists():
        raise FileNotFoundError(f"Entry script not found: {spec.entry}")

    cmd: List[str] = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--onefile",
        "--name",
        spec.name,
        "--distpath",
        str(output_dir),
        "--workpath",
        str(work_root / spec.name),
        "--specpath",
        str(spec_root),
    ]
    if clean:
        cmd.append("--clean")

    for path in spec.python_paths:
        cmd.extend(["--paths", str(path)])
    for module in _unique(spec.hidden_imports):
        cmd.extend(["--hidden-import", module])
    for module in _unique(spec.collect_submodules):
        if _module_available(module):
            cmd.extend(["--collect-submodules", module])
    for module in _unique(spec.collect_all):
        if _module_available(module):
            cmd.extend(["--collect-all", module])
    for source_path, dest_dir in spec.add_data:
        if source_path.exists():
            cmd.extend(["--add-data", f"{source_path}{os.pathsep}{dest_dir}"])

    cmd.append(str(spec.entry))
    return cmd


def _run(cmd: Sequence[str]) -> None:
    print("[build-exe]", " ".join(cmd))
    subprocess.run(list(cmd), check=True)


def _module_importable(name: str) -> bool:
    try:
        importlib.import_module(name)
        return True
    except Exception:
        return False


def _preflight_requirements(selected: Sequence[ServiceSpec]) -> None:
    missing_by_service: Dict[str, List[str]] = {}
    for spec in selected:
        missing: List[str] = []
        for module in spec.required_modules:
            if not _module_importable(module):
                missing.append(module)
        if missing:
            missing_by_service[spec.name] = missing

    if missing_by_service:
        lines = ["Missing required python modules for selected services:"]
        for name in sorted(missing_by_service.keys()):
            lines.append(f"- {name}: {', '.join(missing_by_service[name])}")
        lines.append("Install dependencies first, or run build_services_exe.ps1 without -SkipInstall.")
        raise RuntimeError("\n".join(lines))


def main(argv: Sequence[str] | None = None) -> int:
    specs = _service_specs()
    parser = argparse.ArgumentParser(
        description="Build service executables into dist/services using PyInstaller.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--services", default="default", help="Comma-separated service names, or 'default'.")
    parser.add_argument("--include-qwen", action="store_true", help="Include qwen_tts_http in default selection.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=REPO_ROOT / "dist" / "services",
        help="Directory containing final service executables.",
    )
    parser.add_argument(
        "--work-root",
        type=Path,
        default=REPO_ROOT / "dist" / "pyinstaller-work",
        help="PyInstaller temporary work directory root.",
    )
    parser.add_argument(
        "--spec-root",
        type=Path,
        default=REPO_ROOT / "dist" / "pyinstaller-spec",
        help="Directory for generated .spec files.",
    )
    parser.add_argument("--clean", action="store_true", help="Pass --clean to PyInstaller.")
    parser.add_argument("--list", action="store_true", help="List available service names and exit.")
    parser.add_argument("--dry-run", action="store_true", help="Print PyInstaller commands without executing.")
    args = parser.parse_args(argv)

    if args.list:
        for name in sorted(specs.keys()):
            default_tag = " (default)" if specs[name].enabled_by_default else ""
            print(f"- {name}{default_tag}")
        return 0

    try:
        selected = _parse_services(args.services, specs, include_qwen=args.include_qwen)
    except ValueError as exc:
        parser.error(str(exc))
        return 2

    _preflight_requirements(selected)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.work_root.mkdir(parents=True, exist_ok=True)
    args.spec_root.mkdir(parents=True, exist_ok=True)

    for spec in selected:
        command = _build_command(
            spec,
            output_dir=args.output_dir.resolve(),
            work_root=args.work_root.resolve(),
            spec_root=args.spec_root.resolve(),
            clean=args.clean,
        )
        if args.dry_run:
            print("[build-exe]", " ".join(command))
        else:
            _run(command)

    print()
    print("[build-exe] done. output:", args.output_dir.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
