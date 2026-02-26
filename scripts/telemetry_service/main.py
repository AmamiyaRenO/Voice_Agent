#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import random
import sqlite3
import threading
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import paho.mqtt.client as mqtt
except Exception:  # pragma: no cover - optional dependency for mock-only mode
    mqtt = None  # type: ignore[assignment]
import uvicorn
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default


def _env_int(name: str, default: int, floor: int = 1) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return max(floor, default)
    try:
        return max(floor, int(raw))
    except Exception:
        return max(floor, default)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _coerce_iso_utc(value: Any) -> str:
    if value is None:
        return _utc_now_iso()
    if isinstance(value, (int, float)):
        dt = datetime.fromtimestamp(float(value), tz=timezone.utc)
        return dt.replace(microsecond=0).isoformat()
    text = str(value).strip()
    if not text:
        return _utc_now_iso()
    try:
        normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()
    except Exception:
        return _utc_now_iso()


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _new_mqtt_client(client_id: str):
    if mqtt is None:
        raise RuntimeError("paho-mqtt is not installed")
    callback_api = getattr(mqtt, "CallbackAPIVersion", None)
    if callback_api is not None:
        return mqtt.Client(callback_api.VERSION2, client_id=client_id)
    return mqtt.Client(client_id=client_id)


@dataclass
class Settings:
    api_host: str = "0.0.0.0"
    api_port: int = 8101
    db_path: str = ""
    mqtt_enable: bool = True
    mqtt_host: str = "127.0.0.1"
    mqtt_port: int = 1883
    mqtt_topic: str = "voiceagent/telemetry/#"
    auto_seed: bool = True
    seed_user: str = "demo_user"
    seed_days: int = 21

    @classmethod
    def from_env(cls) -> "Settings":
        script_dir = Path(__file__).resolve().parent
        default_db = script_dir / "telemetry.db"
        cfg = cls()
        cfg.api_host = os.environ.get("TELEMETRY_HOST", cfg.api_host)
        cfg.api_port = _env_int("TELEMETRY_PORT", cfg.api_port, floor=1)
        cfg.db_path = os.environ.get("TELEMETRY_DB_PATH", str(default_db))
        cfg.mqtt_enable = _env_bool("TELEMETRY_MQTT_ENABLE", cfg.mqtt_enable)
        cfg.mqtt_host = os.environ.get("MQTT_HOST", cfg.mqtt_host)
        cfg.mqtt_port = _env_int("MQTT_PORT", cfg.mqtt_port, floor=1)
        cfg.mqtt_topic = os.environ.get("TELEMETRY_MQTT_TOPIC", cfg.mqtt_topic)
        cfg.auto_seed = _env_bool("TELEMETRY_AUTO_SEED", cfg.auto_seed)
        cfg.seed_user = os.environ.get("TELEMETRY_SEED_USER", cfg.seed_user).strip() or "demo_user"
        cfg.seed_days = _env_int("TELEMETRY_SEED_DAYS", cfg.seed_days, floor=1)
        return cfg


class TelemetryStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with self._lock:
            self.conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA foreign_keys=ON;

                CREATE TABLE IF NOT EXISTS telemetry_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT UNIQUE,
                    event_name TEXT NOT NULL,
                    user_id TEXT,
                    game_id TEXT,
                    session_id TEXT,
                    stage_id TEXT,
                    ts_utc TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'mqtt',
                    received_at_utc TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_events_user_ts
                ON telemetry_events(user_id, ts_utc);

                CREATE TABLE IF NOT EXISTS exercise_sessions (
                    session_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    game_id TEXT,
                    exercise_program TEXT,
                    age_group TEXT,
                    started_at_utc TEXT,
                    ended_at_utc TEXT,
                    end_reason TEXT,
                    active_seconds REAL DEFAULT 0,
                    completion_rate REAL DEFAULT 0,
                    reps_plant INTEGER DEFAULT 0,
                    reps_water INTEGER DEFAULT 0,
                    reps_weeds INTEGER DEFAULT 0,
                    reps_fruit INTEGER DEFAULT 0,
                    reps_target_plant INTEGER DEFAULT 0,
                    reps_target_water INTEGER DEFAULT 0,
                    reps_target_weeds INTEGER DEFAULT 0,
                    reps_target_fruit INTEGER DEFAULT 0,
                    seconds_plant REAL DEFAULT 0,
                    seconds_water REAL DEFAULT 0,
                    seconds_weeds REAL DEFAULT 0,
                    seconds_fruit REAL DEFAULT 0,
                    stability_score REAL DEFAULT 0,
                    symmetry_score REAL DEFAULT 0,
                    avg_wrist_height_vs_head REAL DEFAULT 0,
                    safety_events INTEGER DEFAULT 0,
                    updated_at_utc TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_sessions_user_end
                ON exercise_sessions(user_id, ended_at_utc);

                CREATE TABLE IF NOT EXISTS exercise_stage_summaries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT UNIQUE,
                    user_id TEXT NOT NULL,
                    game_id TEXT,
                    session_id TEXT NOT NULL,
                    stage_id TEXT NOT NULL,
                    reps_done INTEGER DEFAULT 0,
                    reps_target INTEGER DEFAULT 0,
                    stage_seconds REAL DEFAULT 0,
                    ts_utc TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_stage_session
                ON exercise_stage_summaries(session_id);
                """
            )
            self.conn.commit()

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def ingest_event(self, payload: Dict[str, Any], source: str = "mqtt") -> None:
        event_name = str(payload.get("event_name") or payload.get("type") or "unknown").strip()
        event_id = str(payload.get("event_id") or uuid.uuid4().hex)
        user_id = str(payload.get("user_id") or "unknown_user")
        game_id = str(payload.get("game_id") or "unknown_game")
        session_id = str(payload.get("session_id") or f"session-{uuid.uuid4().hex}")
        stage_id = str(payload.get("stage_id") or "")
        ts_utc = _coerce_iso_utc(payload.get("ts_utc") or payload.get("ts"))
        received_at_utc = _utc_now_iso()
        payload_json = json.dumps(payload, separators=(",", ":"))

        with self._lock:
            self.conn.execute(
                """
                INSERT OR IGNORE INTO telemetry_events (
                    event_id, event_name, user_id, game_id, session_id,
                    stage_id, ts_utc, payload_json, source, received_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    event_name,
                    user_id,
                    game_id,
                    session_id,
                    stage_id,
                    ts_utc,
                    payload_json,
                    source,
                    received_at_utc,
                ),
            )

            if event_name == "exercise_session_start":
                self._upsert_session_start(payload, user_id, game_id, session_id, ts_utc, received_at_utc)
            elif event_name == "exercise_session_end":
                self._upsert_session_end(payload, user_id, game_id, session_id, ts_utc, received_at_utc)
            elif event_name == "exercise_stage_summary":
                self.conn.execute(
                    """
                    INSERT OR REPLACE INTO exercise_stage_summaries (
                        event_id, user_id, game_id, session_id, stage_id,
                        reps_done, reps_target, stage_seconds, ts_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        user_id,
                        game_id,
                        session_id,
                        stage_id or "unknown",
                        _to_int(payload.get("reps_done")),
                        _to_int(payload.get("reps_target")),
                        _to_float(payload.get("stage_seconds")),
                        ts_utc,
                    ),
                )

            self.conn.commit()

    def _upsert_session_start(
        self,
        payload: Dict[str, Any],
        user_id: str,
        game_id: str,
        session_id: str,
        ts_utc: str,
        now_utc: str,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO exercise_sessions (
                session_id, user_id, game_id, exercise_program, age_group,
                started_at_utc, updated_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                user_id = excluded.user_id,
                game_id = excluded.game_id,
                exercise_program = excluded.exercise_program,
                age_group = excluded.age_group,
                started_at_utc = COALESCE(exercise_sessions.started_at_utc, excluded.started_at_utc),
                updated_at_utc = excluded.updated_at_utc
            """,
            (
                session_id,
                user_id,
                game_id,
                str(payload.get("exercise_program") or ""),
                str(payload.get("age_group") or ""),
                ts_utc,
                now_utc,
            ),
        )

    def _upsert_session_end(
        self,
        payload: Dict[str, Any],
        user_id: str,
        game_id: str,
        session_id: str,
        ts_utc: str,
        now_utc: str,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO exercise_sessions (
                session_id, user_id, game_id, exercise_program, age_group,
                ended_at_utc, end_reason, active_seconds, completion_rate,
                reps_plant, reps_water, reps_weeds, reps_fruit,
                reps_target_plant, reps_target_water, reps_target_weeds, reps_target_fruit,
                seconds_plant, seconds_water, seconds_weeds, seconds_fruit,
                stability_score, symmetry_score, avg_wrist_height_vs_head, safety_events,
                updated_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                user_id = excluded.user_id,
                game_id = excluded.game_id,
                exercise_program = excluded.exercise_program,
                age_group = excluded.age_group,
                ended_at_utc = excluded.ended_at_utc,
                end_reason = excluded.end_reason,
                active_seconds = excluded.active_seconds,
                completion_rate = excluded.completion_rate,
                reps_plant = excluded.reps_plant,
                reps_water = excluded.reps_water,
                reps_weeds = excluded.reps_weeds,
                reps_fruit = excluded.reps_fruit,
                reps_target_plant = excluded.reps_target_plant,
                reps_target_water = excluded.reps_target_water,
                reps_target_weeds = excluded.reps_target_weeds,
                reps_target_fruit = excluded.reps_target_fruit,
                seconds_plant = excluded.seconds_plant,
                seconds_water = excluded.seconds_water,
                seconds_weeds = excluded.seconds_weeds,
                seconds_fruit = excluded.seconds_fruit,
                stability_score = excluded.stability_score,
                symmetry_score = excluded.symmetry_score,
                avg_wrist_height_vs_head = excluded.avg_wrist_height_vs_head,
                safety_events = excluded.safety_events,
                updated_at_utc = excluded.updated_at_utc
            """,
            (
                session_id,
                user_id,
                game_id,
                str(payload.get("exercise_program") or ""),
                str(payload.get("age_group") or ""),
                ts_utc,
                str(payload.get("end_reason") or "unknown"),
                _to_float(payload.get("active_seconds")),
                _to_float(payload.get("completion_rate")),
                _to_int(payload.get("reps_plant")),
                _to_int(payload.get("reps_water")),
                _to_int(payload.get("reps_weeds")),
                _to_int(payload.get("reps_fruit")),
                _to_int(payload.get("reps_target_plant")),
                _to_int(payload.get("reps_target_water")),
                _to_int(payload.get("reps_target_weeds")),
                _to_int(payload.get("reps_target_fruit")),
                _to_float(payload.get("seconds_plant")),
                _to_float(payload.get("seconds_water")),
                _to_float(payload.get("seconds_weeds")),
                _to_float(payload.get("seconds_fruit")),
                _to_float(payload.get("stability_score")),
                _to_float(payload.get("symmetry_score")),
                _to_float(payload.get("avg_wrist_height_vs_head")),
                _to_int(payload.get("safety_events")),
                now_utc,
            ),
        )

    def list_users(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT
                    user_id,
                    COUNT(*) AS session_count,
                    MAX(COALESCE(ended_at_utc, started_at_utc, updated_at_utc)) AS last_seen_utc
                FROM exercise_sessions
                GROUP BY user_id
                ORDER BY last_seen_utc DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def session_count(self, user_id: Optional[str] = None) -> int:
        with self._lock:
            if user_id:
                row = self.conn.execute(
                    "SELECT COUNT(*) AS c FROM exercise_sessions WHERE user_id = ?",
                    (user_id,),
                ).fetchone()
            else:
                row = self.conn.execute(
                    "SELECT COUNT(*) AS c FROM exercise_sessions"
                ).fetchone()
        return int(row["c"]) if row else 0

    def event_count(self) -> int:
        with self._lock:
            row = self.conn.execute("SELECT COUNT(*) AS c FROM telemetry_events").fetchone()
        return int(row["c"]) if row else 0

    def get_weekly_metrics(self, user_id: str, days: int) -> Dict[str, Any]:
        days = max(1, min(days, 90))
        end_day = datetime.now(timezone.utc).date()
        start_day = end_day - timedelta(days=days - 1)
        start_text = start_day.isoformat()
        end_text = end_day.isoformat()

        with self._lock:
            rows = self.conn.execute(
                """
                SELECT
                    date(COALESCE(ended_at_utc, started_at_utc, updated_at_utc)) AS day,
                    COUNT(*) AS sessions,
                    COALESCE(SUM(active_seconds), 0) AS active_seconds,
                    COALESCE(AVG(completion_rate), 0) AS completion_rate_avg,
                    COALESCE(AVG(stability_score), 0) AS stability_avg,
                    COALESCE(AVG(symmetry_score), 0) AS symmetry_avg,
                    COALESCE(SUM(safety_events), 0) AS safety_events,
                    COALESCE(SUM(reps_plant + reps_water + reps_weeds + reps_fruit), 0) AS reps_total
                FROM exercise_sessions
                WHERE user_id = ?
                  AND date(COALESCE(ended_at_utc, started_at_utc, updated_at_utc)) BETWEEN ? AND ?
                GROUP BY day
                ORDER BY day
                """,
                (user_id, start_text, end_text),
            ).fetchall()

            totals_row = self.conn.execute(
                """
                SELECT
                    COALESCE(SUM(reps_plant), 0) AS reps_plant,
                    COALESCE(SUM(reps_water), 0) AS reps_water,
                    COALESCE(SUM(reps_weeds), 0) AS reps_weeds,
                    COALESCE(SUM(reps_fruit), 0) AS reps_fruit,
                    COALESCE(SUM(seconds_plant), 0) AS seconds_plant,
                    COALESCE(SUM(seconds_water), 0) AS seconds_water,
                    COALESCE(SUM(seconds_weeds), 0) AS seconds_weeds,
                    COALESCE(SUM(seconds_fruit), 0) AS seconds_fruit
                FROM exercise_sessions
                WHERE user_id = ?
                  AND date(COALESCE(ended_at_utc, started_at_utc, updated_at_utc)) BETWEEN ? AND ?
                """,
                (user_id, start_text, end_text),
            ).fetchone()

        by_day = {str(row["day"]): dict(row) for row in rows}
        daily: List[Dict[str, Any]] = []

        total_active_seconds = 0.0
        total_sessions = 0
        total_safety_events = 0
        total_reps = 0
        completion_values: List[float] = []
        stability_values: List[float] = []
        symmetry_values: List[float] = []

        for offset in range(days):
            day = start_day + timedelta(days=offset)
            key = day.isoformat()
            row = by_day.get(key)
            sessions = int(row["sessions"]) if row else 0
            active_seconds = float(row["active_seconds"]) if row else 0.0
            completion = float(row["completion_rate_avg"]) if row else 0.0
            stability = float(row["stability_avg"]) if row else 0.0
            symmetry = float(row["symmetry_avg"]) if row else 0.0
            safety = int(row["safety_events"]) if row else 0
            reps_total = int(row["reps_total"]) if row else 0

            daily.append(
                {
                    "date": key,
                    "sessions": sessions,
                    "active_minutes": round(active_seconds / 60.0, 2),
                    "completion_rate_avg": round(completion, 4),
                    "stability_avg": round(stability, 4),
                    "symmetry_avg": round(symmetry, 4),
                    "safety_events": safety,
                    "reps_total": reps_total,
                }
            )

            total_sessions += sessions
            total_active_seconds += active_seconds
            total_safety_events += safety
            total_reps += reps_total
            if sessions > 0:
                completion_values.append(completion)
                stability_values.append(stability)
                symmetry_values.append(symmetry)

        adherence_days = sum(1 for item in daily if item["sessions"] > 0)
        completion_avg = sum(completion_values) / len(completion_values) if completion_values else 0.0
        stability_avg = sum(stability_values) / len(stability_values) if stability_values else 0.0
        symmetry_avg = sum(symmetry_values) / len(symmetry_values) if symmetry_values else 0.0

        stage_totals = {
            "reps_plant": int(totals_row["reps_plant"]) if totals_row else 0,
            "reps_water": int(totals_row["reps_water"]) if totals_row else 0,
            "reps_weeds": int(totals_row["reps_weeds"]) if totals_row else 0,
            "reps_fruit": int(totals_row["reps_fruit"]) if totals_row else 0,
            "minutes_plant": round(float(totals_row["seconds_plant"]) / 60.0, 2) if totals_row else 0.0,
            "minutes_water": round(float(totals_row["seconds_water"]) / 60.0, 2) if totals_row else 0.0,
            "minutes_weeds": round(float(totals_row["seconds_weeds"]) / 60.0, 2) if totals_row else 0.0,
            "minutes_fruit": round(float(totals_row["seconds_fruit"]) / 60.0, 2) if totals_row else 0.0,
        }

        return {
            "user_id": user_id,
            "window_days": days,
            "summary": {
                "total_sessions": total_sessions,
                "adherence_days": adherence_days,
                "adherence_rate": round(adherence_days / float(days), 4),
                "total_active_minutes": round(total_active_seconds / 60.0, 2),
                "avg_minutes_per_active_day": round((total_active_seconds / 60.0) / adherence_days, 2)
                if adherence_days > 0
                else 0.0,
                "completion_rate_avg": round(completion_avg, 4),
                "stability_avg": round(stability_avg, 4),
                "symmetry_avg": round(symmetry_avg, 4),
                "safety_events": total_safety_events,
                "reps_total": total_reps,
            },
            "stage_totals": stage_totals,
            "daily": daily,
            "generated_at_utc": _utc_now_iso(),
        }

    def seed_fake_data(self, user_id: str, days: int, force: bool = False) -> int:
        user_id = (user_id or "demo_user").strip()
        days = max(1, min(days, 180))

        if not force and self.session_count(user_id) > 0:
            return 0

        rng = random.Random(f"telemetry-{user_id}-{days}")
        start_day = datetime.now(timezone.utc).date() - timedelta(days=days - 1)
        inserted = 0

        for i in range(days):
            day = start_day + timedelta(days=i)
            if rng.random() < 0.35:
                continue

            sessions_today = 1 if rng.random() < 0.88 else 2
            for serial in range(sessions_today):
                session_id = f"mock-{user_id}-{day.isoformat()}-{serial + 1}"
                active_seconds = round(rng.uniform(420.0, 1800.0), 1)
                completion_rate = round(rng.uniform(0.58, 0.98), 4)
                safety_events = 0 if rng.random() < 0.7 else rng.randint(1, 3)

                reps_plant = rng.randint(4, 10)
                reps_water = rng.randint(6, 14)
                reps_weeds = rng.randint(4, 10)
                reps_fruit = rng.randint(4, 10)

                start_dt = datetime(day.year, day.month, day.day, 8 + serial * 3, rng.randint(0, 45), tzinfo=timezone.utc)
                end_dt = start_dt + timedelta(seconds=active_seconds)

                end_payload = {
                    "schema_version": "elder_exercise_v1",
                    "event_id": uuid.uuid4().hex,
                    "event_name": "exercise_session_end",
                    "ts_utc": end_dt.replace(microsecond=0).isoformat(),
                    "user_id": user_id,
                    "game_id": "mediapipe_test_demo",
                    "session_id": session_id,
                    "exercise_program": "garden_game",
                    "age_group": "60_74",
                    "end_reason": "complete" if completion_rate > 0.8 else "user_stop",
                    "active_seconds": active_seconds,
                    "completion_rate": completion_rate,
                    "reps_plant": reps_plant,
                    "reps_water": reps_water,
                    "reps_weeds": reps_weeds,
                    "reps_fruit": reps_fruit,
                    "reps_target_plant": 6,
                    "reps_target_water": 8,
                    "reps_target_weeds": 6,
                    "reps_target_fruit": 6,
                    "seconds_plant": round(active_seconds * rng.uniform(0.15, 0.3), 1),
                    "seconds_water": round(active_seconds * rng.uniform(0.2, 0.35), 1),
                    "seconds_weeds": round(active_seconds * rng.uniform(0.15, 0.3), 1),
                    "seconds_fruit": round(active_seconds * rng.uniform(0.15, 0.3), 1),
                    "stability_score": round(rng.uniform(0.62, 0.96), 4),
                    "symmetry_score": round(rng.uniform(0.6, 0.95), 4),
                    "avg_wrist_height_vs_head": round(rng.uniform(-0.12, 0.18), 4),
                    "safety_events": safety_events,
                }
                self.ingest_event(end_payload, source="seed")

                start_payload = {
                    "schema_version": "elder_exercise_v1",
                    "event_id": uuid.uuid4().hex,
                    "event_name": "exercise_session_start",
                    "ts_utc": start_dt.replace(microsecond=0).isoformat(),
                    "user_id": user_id,
                    "game_id": "mediapipe_test_demo",
                    "session_id": session_id,
                    "exercise_program": "garden_game",
                    "age_group": "60_74",
                }
                self.ingest_event(start_payload, source="seed")
                inserted += 1

        return inserted


class MqttTelemetryIngestor:
    def __init__(self, settings: Settings, store: TelemetryStore) -> None:
        self.settings = settings
        self.store = store
        self.client: Optional[Any] = None
        self._started = False

    def start(self) -> None:
        if not self.settings.mqtt_enable:
            print("[telemetry] mqtt ingest disabled")
            return
        if mqtt is None:
            print("[telemetry] paho-mqtt not installed; mqtt ingest disabled")
            return

        client = _new_mqtt_client(f"telemetry-svc-{uuid.uuid4().hex[:8]}")
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        self.client = client
        print(f"[telemetry] connecting to mqtt {self.settings.mqtt_host}:{self.settings.mqtt_port}")
        client.connect(self.settings.mqtt_host, self.settings.mqtt_port, keepalive=20)
        client.loop_start()
        self._started = True

    def stop(self) -> None:
        if not self.client or not self._started:
            return
        try:
            self.client.loop_stop()
        finally:
            self.client.disconnect()
            self._started = False

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        print(f"[telemetry] mqtt connected rc={reason_code}")
        client.subscribe(self.settings.mqtt_topic)
        print(f"[telemetry] subscribed {self.settings.mqtt_topic}")

    def _on_message(self, client, userdata, msg) -> None:
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except Exception:
            return
        if not isinstance(payload, dict):
            return
        try:
            self.store.ingest_event(payload, source="mqtt")
        except Exception as exc:
            print(f"[telemetry] ingest error: {exc}")


class SeedFakeRequest(BaseModel):
    user_id: str = "demo_user"
    days: int = 21
    force: bool = False


class IngestEventRequest(BaseModel):
    event: Dict[str, Any]


SETTINGS = Settings.from_env()


@asynccontextmanager
async def _lifespan(app: FastAPI):
    store = TelemetryStore(Path(SETTINGS.db_path))
    ingestor = MqttTelemetryIngestor(SETTINGS, store)

    if SETTINGS.auto_seed:
        inserted = store.seed_fake_data(SETTINGS.seed_user, SETTINGS.seed_days, force=False)
        if inserted > 0:
            print(f"[telemetry] seeded fake sessions: {inserted} (user={SETTINGS.seed_user})")

    ingestor.start()
    app.state.store = store
    app.state.ingestor = ingestor
    try:
        yield
    finally:
        ingestor.stop()
        store.close()


app = FastAPI(
    title="Voice Agent Telemetry Service",
    version="0.1.0",
    lifespan=_lifespan,
)


def _store() -> TelemetryStore:
    return app.state.store  # type: ignore[attr-defined]


def _build_dashboard_html(default_user_id: str, default_days: int) -> str:
    html = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Elder Exercise Dashboard</title>
  <style>
    :root {
      --bg: #0b1220;
      --panel: #111b31;
      --panel-2: #0f1a2e;
      --text: #e8edf7;
      --muted: #9fb0cf;
      --line: #5ea1ff;
      --ok: #2ec27e;
      --warn: #ffb86b;
      --danger: #ff6f6f;
      --card: #17253f;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
      color: var(--text);
      background: radial-gradient(circle at 20% 0%, #182a4c 0%, var(--bg) 45%, #060b15 100%);
      min-height: 100vh;
      padding: 20px;
    }
    .wrap { max-width: 1100px; margin: 0 auto; }
    .top {
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      align-items: center;
      margin-bottom: 16px;
      background: rgba(17, 27, 49, 0.85);
      border: 1px solid rgba(255,255,255,0.08);
      border-radius: 12px;
      padding: 12px;
      backdrop-filter: blur(4px);
    }
    .top input, .top select, .top button {
      height: 36px;
      border-radius: 8px;
      border: 1px solid rgba(255,255,255,0.12);
      background: #111e37;
      color: var(--text);
      padding: 0 12px;
    }
    .top button { cursor: pointer; background: #1f4e94; border-color: #3a71c3; }
    .kpis {
      display: grid;
      grid-template-columns: repeat(4, minmax(160px, 1fr));
      gap: 12px;
      margin-bottom: 12px;
    }
    .kpi {
      background: var(--card);
      border: 1px solid rgba(255,255,255,0.1);
      border-radius: 12px;
      padding: 12px;
    }
    .kpi .label { font-size: 12px; color: var(--muted); margin-bottom: 8px; }
    .kpi .value { font-size: 24px; font-weight: 700; letter-spacing: 0.2px; }
    .grid {
      display: grid;
      grid-template-columns: 1.35fr 1fr;
      gap: 12px;
    }
    .panel {
      background: var(--panel);
      border: 1px solid rgba(255,255,255,0.1);
      border-radius: 12px;
      padding: 12px;
    }
    .panel h3 { margin: 0 0 10px; font-size: 14px; color: #d7e2f7; }
    svg { width: 100%; height: 220px; background: var(--panel-2); border-radius: 10px; }
    .legend { font-size: 12px; color: var(--muted); margin-top: 8px; }
    .bars { display: grid; gap: 8px; margin-top: 8px; }
    .bar-row { display: grid; grid-template-columns: 90px 1fr 50px; gap: 8px; align-items: center; font-size: 12px; }
    .bar-track { height: 10px; border-radius: 999px; background: #1c2e4f; overflow: hidden; }
    .bar-fill { height: 100%; background: linear-gradient(90deg, #5ea1ff, #87c7ff); }
    .status { margin-top: 8px; color: var(--muted); font-size: 12px; min-height: 18px; }
    @media (max-width: 900px) {
      .kpis { grid-template-columns: repeat(2, minmax(140px, 1fr)); }
      .grid { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="top">
      <label>User</label>
      <input id="userId" value="__DEFAULT_USER_ID__" />
      <label>Days</label>
      <select id="days">
        <option>7</option>
        <option selected>14</option>
        <option>21</option>
        <option>30</option>
      </select>
      <button id="loadBtn">Load</button>
      <button id="seedBtn">Seed Mock Data</button>
      <div id="status" class="status"></div>
    </div>

    <div class="kpis">
      <div class="kpi"><div class="label">Active Minutes</div><div id="kpiMinutes" class="value">-</div></div>
      <div class="kpi"><div class="label">Adherence</div><div id="kpiAdherence" class="value">-</div></div>
      <div class="kpi"><div class="label">Avg Completion</div><div id="kpiCompletion" class="value">-</div></div>
      <div class="kpi"><div class="label">Safety Events</div><div id="kpiSafety" class="value">-</div></div>
    </div>

    <div class="grid">
      <div class="panel">
        <h3>Active Minutes Trend</h3>
        <svg id="lineChart" viewBox="0 0 700 220" preserveAspectRatio="none"></svg>
        <div class="legend">Daily active minutes over selected window.</div>
      </div>
      <div class="panel">
        <h3>Stage Totals (Reps)</h3>
        <div id="stageBars" class="bars"></div>
      </div>
      <div class="panel">
        <h3>Safety Events Trend</h3>
        <svg id="safetyChart" viewBox="0 0 700 220" preserveAspectRatio="none"></svg>
        <div class="legend">Daily detected risk events.</div>
      </div>
      <div class="panel">
        <h3>Quality Snapshot</h3>
        <div id="qualityBars" class="bars"></div>
      </div>
    </div>
  </div>

  <script>
    const userInput = document.getElementById('userId');
    const daysInput = document.getElementById('days');
    const statusEl = document.getElementById('status');
    const kpiMinutes = document.getElementById('kpiMinutes');
    const kpiAdherence = document.getElementById('kpiAdherence');
    const kpiCompletion = document.getElementById('kpiCompletion');
    const kpiSafety = document.getElementById('kpiSafety');
    const lineChart = document.getElementById('lineChart');
    const safetyChart = document.getElementById('safetyChart');
    const stageBars = document.getElementById('stageBars');
    const qualityBars = document.getElementById('qualityBars');

    function setStatus(text, bad=false) {
      statusEl.textContent = text || '';
      statusEl.style.color = bad ? '#ff8f8f' : '#9fb0cf';
    }

    function fmtPct(v) {
      return (Number(v || 0) * 100).toFixed(1) + '%';
    }

    function renderBars(container, items) {
      container.innerHTML = '';
      const maxVal = Math.max(1, ...items.map(i => i.value));
      for (const item of items) {
        const row = document.createElement('div');
        row.className = 'bar-row';
        row.innerHTML = `
          <div>${item.label}</div>
          <div class="bar-track"><div class="bar-fill" style="width:${(item.value / maxVal) * 100}%"></div></div>
          <div>${item.text || item.value}</div>
        `;
        container.appendChild(row);
      }
    }

    function drawLine(svgEl, values, color) {
      const width = 700;
      const height = 220;
      const padL = 40, padR = 16, padT = 14, padB = 26;
      const innerW = width - padL - padR;
      const innerH = height - padT - padB;
      const maxVal = Math.max(1, ...values);
      const minVal = 0;
      const n = Math.max(values.length, 1);
      const points = values.map((v, i) => {
        const x = padL + (i / Math.max(n - 1, 1)) * innerW;
        const y = padT + (1 - ((v - minVal) / (maxVal - minVal || 1))) * innerH;
        return `${x},${y}`;
      });
      const areaPoints = points.concat([`${padL + innerW},${padT + innerH}`, `${padL},${padT + innerH}`]).join(' ');
      const polylinePoints = points.join(' ');

      let grid = '';
      for (let i = 0; i <= 4; i++) {
        const y = padT + (i / 4) * innerH;
        grid += `<line x1="${padL}" y1="${y}" x2="${padL + innerW}" y2="${y}" stroke="rgba(255,255,255,0.08)" stroke-width="1"/>`;
      }

      svgEl.innerHTML = `
        <rect x="0" y="0" width="${width}" height="${height}" fill="transparent"></rect>
        ${grid}
        <polygon points="${areaPoints}" fill="${color}" opacity="0.16"></polygon>
        <polyline points="${polylinePoints}" fill="none" stroke="${color}" stroke-width="3" stroke-linecap="round"></polyline>
      `;
    }

    async function loadMetrics() {
      const user = (userInput.value || '').trim() || 'demo_user';
      const days = Number(daysInput.value || 14);
      setStatus('Loading...');
      try {
        const resp = await fetch(`/metrics/user/${encodeURIComponent(user)}/weekly?days=${days}&auto_seed_if_empty=true`);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        const summary = data.summary || {};
        const daily = data.daily || [];
        const stage = data.stage_totals || {};

        kpiMinutes.textContent = Number(summary.total_active_minutes || 0).toFixed(1);
        kpiAdherence.textContent = fmtPct(summary.adherence_rate || 0);
        kpiCompletion.textContent = fmtPct(summary.completion_rate_avg || 0);
        kpiSafety.textContent = String(summary.safety_events || 0);

        drawLine(lineChart, daily.map(d => Number(d.active_minutes || 0)), '#5ea1ff');
        drawLine(safetyChart, daily.map(d => Number(d.safety_events || 0)), '#ffb86b');

        renderBars(stageBars, [
          { label: 'Plant', value: Number(stage.reps_plant || 0) },
          { label: 'Water', value: Number(stage.reps_water || 0) },
          { label: 'Weeds', value: Number(stage.reps_weeds || 0) },
          { label: 'Fruit', value: Number(stage.reps_fruit || 0) },
        ]);

        renderBars(qualityBars, [
          { label: 'Stability', value: Number(summary.stability_avg || 0), text: fmtPct(summary.stability_avg || 0) },
          { label: 'Symmetry', value: Number(summary.symmetry_avg || 0), text: fmtPct(summary.symmetry_avg || 0) },
          { label: 'Adherence', value: Number(summary.adherence_rate || 0), text: fmtPct(summary.adherence_rate || 0) },
          { label: 'Completion', value: Number(summary.completion_rate_avg || 0), text: fmtPct(summary.completion_rate_avg || 0) },
        ]);

        setStatus(`Loaded ${daily.length} points for ${user}`);
      } catch (err) {
        setStatus(`Load failed: ${err.message || err}`, true);
      }
    }

    async function seedMock() {
      const user = (userInput.value || '').trim() || 'demo_user';
      const days = Number(daysInput.value || 14);
      setStatus('Seeding mock data...');
      try {
        const resp = await fetch('/admin/seed-fake', {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ user_id: user, days: days, force: false }),
        });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        await loadMetrics();
      } catch (err) {
        setStatus(`Seed failed: ${err.message || err}`, true);
      }
    }

    document.getElementById('loadBtn').addEventListener('click', loadMetrics);
    document.getElementById('seedBtn').addEventListener('click', seedMock);

    const params = new URLSearchParams(location.search);
    const defaultDays = Number(params.get('days') || "__DEFAULT_DAYS__");
    if (params.get('user_id')) userInput.value = params.get('user_id');
    if ([7, 14, 21, 30].includes(defaultDays)) daysInput.value = String(defaultDays);
    loadMetrics();
  </script>
</body>
</html>
"""
    html = html.replace("__DEFAULT_USER_ID__", default_user_id or "demo_user")
    html = html.replace("__DEFAULT_DAYS__", str(default_days))
    return html


@app.get("/healthz")
def healthz() -> Dict[str, Any]:
    store = _store()
    return {
        "status": "ok",
        "time_utc": _utc_now_iso(),
        "db_path": SETTINGS.db_path,
        "mqtt_ingest_enabled": SETTINGS.mqtt_enable,
        "session_count": store.session_count(),
        "event_count": store.event_count(),
    }


@app.get("/users")
def list_users() -> Dict[str, Any]:
    return {"users": _store().list_users()}


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(
    user_id: str = Query(default="demo_user"),
    days: int = Query(default=14, ge=1, le=90),
) -> HTMLResponse:
    return HTMLResponse(content=_build_dashboard_html(default_user_id=user_id, default_days=days))


@app.get("/metrics/user/{user_id}/weekly")
def metrics_weekly(
    user_id: str,
    days: int = Query(default=14, ge=1, le=90),
    auto_seed_if_empty: bool = Query(default=True),
) -> Dict[str, Any]:
    store = _store()
    if auto_seed_if_empty and store.session_count(user_id) == 0:
        store.seed_fake_data(user_id=user_id, days=max(days, 14), force=False)
    return store.get_weekly_metrics(user_id=user_id, days=days)


@app.post("/admin/seed-fake")
def admin_seed_fake(req: SeedFakeRequest) -> Dict[str, Any]:
    store = _store()
    inserted = store.seed_fake_data(user_id=req.user_id, days=req.days, force=req.force)
    return {
        "ok": True,
        "inserted_sessions": inserted,
        "user_id": req.user_id,
        "session_count": store.session_count(req.user_id),
    }


@app.post("/ingest")
def ingest_event(req: IngestEventRequest) -> Dict[str, Any]:
    _store().ingest_event(req.event, source="http")
    return {"ok": True}


def main() -> int:
    uvicorn.run(
        app,
        host=SETTINGS.api_host,
        port=SETTINGS.api_port,
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
