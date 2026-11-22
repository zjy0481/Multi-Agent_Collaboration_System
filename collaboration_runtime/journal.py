"""Durable execution checkpoints and tool receipts.

All operations use the supplied SQLite connection. The caller owns transaction
boundaries and must serialize writes to a connection; no method commits.
"""
from __future__ import annotations

import json
import sqlite3


def _encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


class ExecutionJournal:
    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self.db.execute('''CREATE TABLE IF NOT EXISTS execution_checkpoints (
            turn_id TEXT PRIMARY KEY, generation INTEGER NOT NULL,
            step INTEGER NOT NULL, phase TEXT NOT NULL,
            history TEXT NOT NULL, pending TEXT NOT NULL
        )''')
        self.db.execute('''CREATE TABLE IF NOT EXISTS execution_receipts (
            turn_id TEXT NOT NULL, step INTEGER NOT NULL, position INTEGER NOT NULL,
            state TEXT NOT NULL, generation INTEGER NOT NULL,
            call_id TEXT NOT NULL, name TEXT NOT NULL, arguments TEXT NOT NULL,
            result TEXT,
            PRIMARY KEY (turn_id, step, position),
            FOREIGN KEY (turn_id) REFERENCES execution_checkpoints(turn_id)
        )''')

    def checkpoint(self, turn_id: str) -> dict | None:
        row = self.db.execute(
            'SELECT generation, step, phase, history, pending FROM execution_checkpoints WHERE turn_id = ?',
            (turn_id,),
        ).fetchone()
        if row is None:
            return None
        return {'generation': row[0], 'step': row[1], 'phase': row[2],
                'history': json.loads(row[3]), 'pending': json.loads(row[4])}

    def create(self, turn_id: str, history: list) -> None:
        self.db.execute(
            'INSERT INTO execution_checkpoints VALUES (?, 1, 0, ?, ?, ?)',
            (turn_id, 'model', _encode(history), _encode([])),
        )

    def current(self, turn_id: str, generation: int) -> bool:
        return self.db.execute(
            'SELECT 1 FROM execution_checkpoints WHERE turn_id = ? AND generation = ?',
            (turn_id, generation),
        ).fetchone() is not None

    def advance(self, turn_id: str, generation: int, step: int, phase: str, history: list) -> bool:
        if phase not in ('model', 'tools'):
            raise ValueError('invalid checkpoint phase')
        cursor = self.db.execute('''UPDATE execution_checkpoints SET step = ?, phase = ?, history = ?
                                    WHERE turn_id = ? AND generation = ?''',
                                 (step, phase, _encode(history), turn_id, generation))
        return cursor.rowcount == 1

    def next_generation(self, turn_id: str) -> int:
        cursor = self.db.execute(
            'UPDATE execution_checkpoints SET generation = generation + 1 WHERE turn_id = ?', (turn_id,),
        )
        if cursor.rowcount != 1:
            raise KeyError(turn_id)
        return self.db.execute('SELECT generation FROM execution_checkpoints WHERE turn_id = ?',
                               (turn_id,)).fetchone()[0]

    def queue_input(self, turn_id: str, text: str) -> None:
        row = self.db.execute('SELECT pending FROM execution_checkpoints WHERE turn_id = ?', (turn_id,)).fetchone()
        if row is None:
            raise KeyError(turn_id)
        pending = json.loads(row[0])
        pending.append(text)
        self.db.execute('UPDATE execution_checkpoints SET pending = ? WHERE turn_id = ?',
                        (_encode(pending), turn_id))

    def drain_inputs(self, turn_id: str, generation: int) -> list:
        row = self.db.execute('SELECT pending FROM execution_checkpoints WHERE turn_id = ? AND generation = ?',
                              (turn_id, generation)).fetchone()
        if row is None:
            return []
        cursor = self.db.execute('UPDATE execution_checkpoints SET pending = ? WHERE turn_id = ? AND generation = ?',
                                 (_encode([]), turn_id, generation))
        return json.loads(row[0]) if cursor.rowcount == 1 else []

    def receipt(self, turn_id: str, step: int, position: int) -> dict | None:
        row = self.db.execute('''SELECT state, generation, call_id, name, arguments, result
                                 FROM execution_receipts WHERE turn_id = ? AND step = ? AND position = ?''',
                              (turn_id, step, position)).fetchone()
        if row is None:
            return None
        return {'state': row[0], 'generation': row[1], 'call_id': row[2], 'name': row[3],
                'arguments': json.loads(row[4]), 'result': json.loads(row[5]) if row[5] is not None else None}

    def start_tool(self, turn_id: str, generation: int, step: int, position: int,
                   call_id: str, name: str, arguments) -> bool:
        encoded = _encode(arguments)
        cursor = self.db.execute('''INSERT INTO execution_receipts
            (turn_id, step, position, state, generation, call_id, name, arguments, result)
            SELECT ?, ?, ?, 'started', ?, ?, ?, ?, NULL
            WHERE EXISTS (SELECT 1 FROM execution_checkpoints WHERE turn_id = ? AND generation = ?)
            ON CONFLICT(turn_id, step, position) DO UPDATE SET generation = excluded.generation
            WHERE execution_receipts.state = 'started'
              AND execution_receipts.call_id = excluded.call_id
              AND execution_receipts.name = excluded.name
              AND execution_receipts.arguments = excluded.arguments
              AND EXISTS (SELECT 1 FROM execution_checkpoints WHERE turn_id = excluded.turn_id
                          AND generation = excluded.generation)''',
            (turn_id, step, position, generation, call_id, name, encoded, turn_id, generation))
        return cursor.rowcount == 1

    def complete_tool(self, turn_id: str, generation: int, step: int, position: int, result) -> bool:
        cursor = self.db.execute('''UPDATE execution_receipts SET state = 'completed', result = ?
            WHERE turn_id = ? AND step = ? AND position = ? AND state = 'started' AND generation = ?
              AND EXISTS (SELECT 1 FROM execution_checkpoints WHERE turn_id = ? AND generation = ?)''',
            (_encode(result), turn_id, step, position, generation, turn_id, generation))
        return cursor.rowcount == 1
