"""Data persistence for the Telegram Business Assistant Bot."""

from __future__ import annotations

import json
import os
import tempfile
from collections import OrderedDict
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any


def ensure_private_dir(path: Path) -> None:
    """Create directory with 0700 permissions."""
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


class JsonFile:
    """Atomic JSON file read/write with private permissions."""

    def __init__(self, path: Path, default: Any) -> None:
        self.path = path
        self.default = default
        ensure_private_dir(path.parent)

    def read(self) -> Any:
        """Read JSON file, return default on error."""
        try:
            with self.path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return self.default

    def write(self, data: Any) -> None:
        """Write JSON atomically with 0600 permissions."""
        ensure_private_dir(self.path.parent)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=self.path.parent,
            delete=False,
            suffix=".tmp",
        ) as tmp:
            json.dump(data, tmp, ensure_ascii=False, separators=(",", ":"))
            tmp_path = Path(tmp.name)
        try:
            os.chmod(tmp_path, 0o600)
        except OSError:
            pass
        os.replace(tmp_path, self.path)


@dataclass
class HistoryItem:
    """Single history entry."""
    role: str  # "owner" or "other"
    name: str
    text: str
    lang: str | None
    ts: float


class HistoryStore:
    """Conversation history per business connection + chat."""

    def __init__(self, path: Path, limit: int) -> None:
        self.file = JsonFile(path, default={})
        self.limit = limit

    def _key(self, connection_id: str, chat_id: int) -> str:
        return f"{connection_id}:{chat_id}"

    def get(self, connection_id: str, chat_id: int) -> list[HistoryItem]:
        """Get history for a connection+chat."""
        data = self.file.read()
        key = self._key(connection_id, chat_id)
        items = data.get(key, [])
        return [HistoryItem(**item) for item in items]

    def add(self, connection_id: str, chat_id: int, item: HistoryItem) -> None:
        """Add item to history, enforce limit."""
        data = self.file.read()
        key = self._key(connection_id, chat_id)
        items = data.get(key, [])
        items.append(asdict(item))
        if len(items) > self.limit:
            items = items[-self.limit:]
        data[key] = items
        self.file.write(data)

    def last_language(self, connection_id: str, chat_id: int) -> str | None:
        """Get language of last message in chat."""
        items = self.get(connection_id, chat_id)
        for item in reversed(items):
            if item.lang:
                return item.lang
        return None

    def chat_count(self) -> int:
        """Count unique chats in history."""
        data = self.file.read()
        return len(data)


class SeenMessages:
    """Deduplication store with persistence."""

    def __init__(self, path: Path, capacity: int = 5000) -> None:
        self.path = path
        self.capacity = capacity
        self._data: OrderedDict[str, None] = OrderedDict()
        self._load()

    def _load(self) -> None:
        try:
            with self.path.open("r", encoding="utf-8") as f:
                items = json.load(f)
                for item in items:
                    self._data[item] = None
        except (OSError, json.JSONDecodeError):
            pass

    def _save(self) -> None:
        ensure_private_dir(self.path.parent)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=self.path.parent,
            delete=False,
            suffix=".tmp",
        ) as tmp:
            json.dump(list(self._data.keys()), tmp, ensure_ascii=False)
            tmp_path = Path(tmp.name)
        try:
            os.chmod(tmp_path, 0o600)
        except OSError:
            pass
        os.replace(tmp_path, self.path)

    def first_time(self, key: str) -> bool:
        """Check if key is new, mark as seen."""
        if key in self._data:
            # Move to end (most recent)
            self._data.move_to_end(key)
            return False
        self._data[key] = None
        if len(self._data) > self.capacity:
            self._data.popitem(last=False)
        self._save()
        return True


@dataclass
class RuntimeState:
    """Runtime state persisted across restarts."""

    paused: bool = False
    limit_until: float = 0.0

    @classmethod
    def load(cls, path: Path) -> "RuntimeState":
        try:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
                return cls(**data)
        except (OSError, json.JSONDecodeError):
            return cls()

    def save(self, path: Path) -> None:
        ensure_private_dir(path.parent)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            delete=False,
            suffix=".tmp",
        ) as tmp:
            json.dump(asdict(self), tmp)
            tmp_path = Path(tmp.name)
        try:
            os.chmod(tmp_path, 0o600)
        except OSError:
            pass
        os.replace(tmp_path, path)


class PreviewStore:
    """Store for preview logs (JSONL format)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        ensure_private_dir(path.parent)

    def append(self, data: dict[str, Any]) -> None:
        """Append a preview record."""
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False) + "\n")
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass