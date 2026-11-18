"""House store: people, reminder state and flags (SQLite, created on first use)."""
import json
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass
class Person:
    id: int
    name: str
    grocy_user_id: int
    ha_person: str
    notify_service: str


@dataclass
class Reminder:
    key: str
    audience: str | None = None
    remind_at: str = "09:00"
    repeat_min: int | None = None
    snooze_until: datetime | None = None
    last_sent: datetime | None = None
    sent_to: list[str] = field(default_factory=list)
    verb: str | None = None


def _dt(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


class HouseStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock, self._db:
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS people (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, grocy_user_id INTEGER,
                    ha_person TEXT, notify_service TEXT);
                CREATE TABLE IF NOT EXISTS reminders (key TEXT PRIMARY KEY, audience TEXT, remind_at TEXT, repeat_min INTEGER,
                    snooze_until TEXT, last_sent TEXT, sent_to TEXT, verb TEXT);
                CREATE TABLE IF NOT EXISTS flags (name TEXT PRIMARY KEY, value TEXT);
            """)

    def _q(self, sql: str, *args) -> list[tuple]:
        with self._lock, self._db:
            return self._db.execute(sql, args).fetchall()

    def people(self) -> list[Person]:
        return [Person(*r) for r in self._q("SELECT id, name, grocy_user_id, ha_person, notify_service FROM people ORDER BY id")]

    def person(self, id: int) -> Person | None:
        return next((p for p in self.people() if p.id == id), None)

    def save_person(self, name: str, grocy_user_id: int, ha_person: str, notify_service: str, id: int | None = None) -> int:
        with self._lock, self._db:
            if id is None:
                return self._db.execute("INSERT INTO people (name, grocy_user_id, ha_person, notify_service) VALUES (?,?,?,?)",
                                        (name, grocy_user_id, ha_person, notify_service)).lastrowid
            self._db.execute("UPDATE people SET name=?, grocy_user_id=?, ha_person=?, notify_service=? WHERE id=?",
                             (name, grocy_user_id, ha_person, notify_service, id))
            return id

    def delete_person(self, id: int) -> None:
        self._q("DELETE FROM people WHERE id=?", id)

    def reminder(self, key: str) -> Reminder:
        rows = self._q("SELECT audience, remind_at, repeat_min, snooze_until, last_sent, sent_to, verb FROM reminders WHERE key=?", key)
        if not rows:
            return Reminder(key)
        a, at, rep, snz, last, sent, verb = rows[0]
        return Reminder(key, a, at, rep, _dt(snz), _dt(last), json.loads(sent or "[]"), verb)

    def save_reminder(self, r: Reminder) -> None:
        self._q("INSERT OR REPLACE INTO reminders VALUES (?,?,?,?,?,?,?,?)", r.key, r.audience, r.remind_at, r.repeat_min,
                r.snooze_until and r.snooze_until.isoformat(), r.last_sent and r.last_sent.isoformat(),
                json.dumps(r.sent_to), r.verb)

    def drop_reminder(self, key: str) -> None:
        self._q("DELETE FROM reminders WHERE key=?", key)

    def keys(self) -> list[str]:
        return [r[0] for r in self._q("SELECT key FROM reminders ORDER BY key")]

    def flag(self, name: str) -> str | None:
        rows = self._q("SELECT value FROM flags WHERE name=?", name)
        return rows[0][0] if rows else None

    def set_flag(self, name: str, value: str | None) -> None:
        if value is None:
            self._q("DELETE FROM flags WHERE name=?", name)
        else:
            self._q("INSERT OR REPLACE INTO flags VALUES (?,?)", name, value)
