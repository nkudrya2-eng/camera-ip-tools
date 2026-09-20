import datetime as dt
import json
import pathlib
import re
import sqlite3


def camera_identity(row: dict) -> str:
    mac = re.sub(r"[^0-9a-f]", "", str(row.get("mac", "")).lower())
    if len(mac) == 12:
        return f"mac:{mac}"
    for field in ("device_id", "serial_number"):
        value = str(row.get(field, "")).strip().lower()
        if value:
            return f"{field}:{value}"
    return f"ip:{row.get('ip', row.get('current_ip', ''))}"


class CameraStore:
    def __init__(self, path: pathlib.Path):
        self.path = path
        self.connection = sqlite3.connect(path)
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                settings_json TEXT NOT NULL DEFAULT '{}',
                last_used INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS cameras (
                project_id INTEGER NOT NULL,
                identity TEXT NOT NULL,
                data_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (project_id, identity),
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
            );
            """
        )
        self.connection.commit()

    @staticmethod
    def _now() -> str:
        return dt.datetime.now().isoformat(timespec="seconds")

    def list_projects(self) -> list[str]:
        rows = self.connection.execute(
            "SELECT name FROM projects ORDER BY last_used DESC, name"
        ).fetchall()
        return [row[0] for row in rows]

    def ensure_project(self, name: str) -> None:
        name = name.strip()
        if not name:
            raise ValueError("Название объекта не может быть пустым.")
        self.connection.execute(
            "INSERT OR IGNORE INTO projects(name, updated_at) VALUES (?, ?)",
            (name, self._now()),
        )
        self.connection.commit()

    def load_project(self, name: str) -> tuple[dict, list[dict]]:
        row = self.connection.execute(
            "SELECT id, settings_json FROM projects WHERE name = ?", (name,)
        ).fetchone()
        if row is None:
            return {}, []
        project_id, settings_json = row
        cameras = [
            json.loads(item[0])
            for item in self.connection.execute(
                "SELECT data_json FROM cameras WHERE project_id = ? ORDER BY identity",
                (project_id,),
            ).fetchall()
        ]
        return json.loads(settings_json or "{}"), cameras

    def save_project(self, name: str, settings: dict, cameras: list[dict]) -> None:
        self.ensure_project(name)
        now = self._now()
        self.connection.execute("UPDATE projects SET last_used = 0")
        self.connection.execute(
            "UPDATE projects SET settings_json = ?, last_used = 1, updated_at = ? WHERE name = ?",
            (json.dumps(settings, ensure_ascii=False), now, name),
        )
        project_id = self.connection.execute(
            "SELECT id FROM projects WHERE name = ?", (name,)
        ).fetchone()[0]
        for camera in cameras:
            camera = json.loads(json.dumps(camera, ensure_ascii=False))
            camera.get("camera", {}).pop("password", None)
            identity = camera_identity(camera)
            self.connection.execute(
                """
                INSERT INTO cameras(project_id, identity, data_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(project_id, identity) DO UPDATE SET
                    data_json = excluded.data_json,
                    updated_at = excluded.updated_at
                """,
                (project_id, identity, json.dumps(camera, ensure_ascii=False), now),
            )
        self.connection.commit()

    def remove_cameras(self, name: str, cameras: list[dict]) -> None:
        identities = [(camera_identity(row), name) for row in cameras]
        with self.connection:
            self.connection.executemany(
                "DELETE FROM cameras WHERE identity = ? AND project_id = (SELECT id FROM projects WHERE name = ?)",
                identities,
            )

    def delete_project(self, name: str) -> None:
        self.connection.execute("DELETE FROM projects WHERE name = ?", (name,))
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()
