import json
import sqlite3
import time
from pathlib import Path


class Store:
    """Small, transactional local state. No Google credentials are stored here."""

    def __init__(self, directory: Path):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(directory / "workbench.sqlite3")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS records (kind TEXT, id TEXT, body TEXT, PRIMARY KEY(kind,id))")
        self.db.commit()

    def get(self, kind, key, default=None):
        row = self.db.execute("SELECT body FROM records WHERE kind=? AND id=?", (kind, key)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, kind, key, value):
        self.db.execute("INSERT OR REPLACE INTO records VALUES(?,?,?)", (kind, key, json.dumps(value)))
        self.db.commit()
        return value

    def patch(self, kind, key, **changes):
        value = self.get(kind, key, {})
        value.update(changes)
        value["updated_at"] = time.time()
        return self.put(kind, key, value)

    def all(self, kind):
        return [json.loads(row[0]) for row in self.db.execute("SELECT body FROM records WHERE kind=?", (kind,))]

    def put_many(self, kind, values):
        with self.db:
            self.db.executemany("INSERT OR REPLACE INTO records VALUES(?,?,?)",
                                [(kind, value["id"], json.dumps(value)) for value in values])

    def delete(self, kind, key):
        with self.db:
            self.db.execute("DELETE FROM records WHERE kind=? AND id=?", (kind, key))

    def close(self):
        self.db.close()
