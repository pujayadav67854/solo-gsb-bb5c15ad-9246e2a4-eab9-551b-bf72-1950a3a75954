"""SQLite 存储层：连接管理、建表、初始化与演示数据。

写入全部串行化（进程内写锁 + 立即事务），保证“更正一次提交、
失败回滚不留部分改动”；开启外键约束与 WAL。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from . import allergens as al

SCHEMA = """
CREATE TABLE IF NOT EXISTS allergen (
    code        TEXT PRIMARY KEY,
    name_zh     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS material_lot (
    lot_no      TEXT PRIMARY KEY,
    material    TEXT NOT NULL,
    allergens   TEXT NOT NULL DEFAULT '[]',  -- JSON 数组，过敏原代码
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS line (
    line_id     TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS batch (
    batch_no         TEXT PRIMARY KEY,
    line_id          TEXT NOT NULL REFERENCES line(line_id),
    seq              INTEGER NOT NULL,
    material_lots    TEXT NOT NULL DEFAULT '[]',   -- JSON 数组，原料批号
    declared         TEXT NOT NULL DEFAULT '[]',   -- JSON 数组，声明过敏原
    cleaned          TEXT NOT NULL DEFAULT '[]',   -- JSON 数组，本次清洁确认清除的过敏原
    created_at       TEXT NOT NULL,
    UNIQUE(line_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_batch_line_seq ON batch(line_id, seq);

CREATE TABLE IF NOT EXISTS batch_material (
    batch_no  TEXT NOT NULL REFERENCES batch(batch_no),
    lot_no    TEXT NOT NULL REFERENCES material_lot(lot_no),
    PRIMARY KEY (batch_no, lot_no)
);

CREATE INDEX IF NOT EXISTS idx_batch_material_lot ON batch_material(lot_no);

-- 每个批次当前裁决快照
CREATE TABLE IF NOT EXISTS adjudication (
    batch_no      TEXT PRIMARY KEY REFERENCES batch(batch_no),
    version       INTEGER NOT NULL DEFAULT 0,
    actual        TEXT NOT NULL DEFAULT '[]',   -- JSON：实际可能含有的过敏原
    undeclared    TEXT NOT NULL DEFAULT '[]',   -- JSON：实际含但未声明
    disposition   TEXT NOT NULL,                -- RELEASE / HOLD
    reasons       TEXT NOT NULL DEFAULT '[]',   -- JSON：原因条目
    sources       TEXT NOT NULL DEFAULT '{}',   -- JSON：过敏原 -> [来源说明]
    decided_at    TEXT NOT NULL
);

-- 裁决历史版本（只追加，用于审计与查询）
CREATE TABLE IF NOT EXISTS adjudication_version (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_no      TEXT NOT NULL,
    line_id       TEXT NOT NULL,
    seq           INTEGER NOT NULL,
    version       INTEGER NOT NULL,
    actual        TEXT NOT NULL DEFAULT '[]',
    undeclared    TEXT NOT NULL DEFAULT '[]',
    disposition   TEXT NOT NULL,
    reasons       TEXT NOT NULL DEFAULT '[]',
    sources       TEXT NOT NULL DEFAULT '{}',
    trigger       TEXT NOT NULL,                -- REGISTER / CORRECT_BATCH / CORRECT_LOT
    decided_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_version_batch
    ON adjudication_version(batch_no, version);
"""

# 初始化空库时写入的参考原料批号（不影响裁决的演示样例）
SEED_MATERIAL_LOTS = [
    ("RM-MILK-001", "全脂奶粉", ["milk"]),
    ("RM-FLOUR-001", "小麦粉", ["gluten"]),
    ("RM-NUT-001", "花生酱", ["peanut"]),
    ("RM-CACAO-001", "可可块", []),
]


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def loads(value: str) -> Any:
    return json.loads(value)


class Store:
    """线程安全的 SQLite 封装。"""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._write_lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None

    def connect(self) -> None:
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            self.path, check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA busy_timeout = 5000")

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """立即开启写事务；异常回滚，正常提交。进程内串行。

        注意：SQLite 的 executescript 会隐式提交事务，因此本上下文内
        只允许 execute/executemany；DDL 建表请在 autocommit 状态下执行。
        """
        assert self._conn is not None
        conn = self._conn
        with self._write_lock:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except Exception:
                # 事务可能已被隐式提交（理论上不应发生），忽略重复回滚
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    pass
                raise

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        assert self._conn is not None
        with self._write_lock:
            yield self._conn

    def is_fresh(self) -> bool:
        """库中是否尚无过敏原字典（即是否需要初始化）。"""
        cur = self._conn.execute(
            "SELECT COUNT(*) AS n FROM sqlite_master WHERE type='table' AND name='allergen'"
        )
        if cur.fetchone()["n"] == 0:
            return True
        cur = self._conn.execute("SELECT COUNT(*) AS n FROM allergen")
        return cur.fetchone()["n"] == 0


def initialize(store: Store, seed_demo: bool = False) -> None:
    """幂等初始化：建表、写入过敏原字典；全新库可附带演示原料批号。

    建表 DDL 在 autocommit 状态下执行（executescript 会隐式提交，
    不能放在写事务内）；字典与演示数据写入放在单个事务中。
    """
    from datetime import UTC, datetime

    assert store._conn is not None
    conn = store._conn
    fresh = store.is_fresh()
    # DDL：autocommit 状态执行，IF NOT EXISTS 保证幂等
    conn.executescript(SCHEMA)
    with store.write():
        conn.executemany(
            "INSERT OR IGNORE INTO allergen(code, name_zh) VALUES (?, ?)",
            list(al.ALLERGENS.items()),
        )
        if seed_demo and fresh:
            now = datetime.now(UTC).isoformat()
            conn.execute(
                "INSERT OR IGNORE INTO line(line_id, created_at) VALUES (?, ?)",
                ("L1", now),
            )
            conn.executemany(
                "INSERT OR IGNORE INTO material_lot"
                "(lot_no, material, allergens, updated_at) VALUES (?, ?, ?, ?)",
                [
                    (lot_no, material, dumps(allergen_codes), now)
                    for lot_no, material, allergen_codes in SEED_MATERIAL_LOTS
                ],
            )
