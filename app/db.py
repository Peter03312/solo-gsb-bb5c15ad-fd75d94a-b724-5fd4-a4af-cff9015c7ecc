"""SQLite 存储层（标准库 sqlite3，无 ORM）。

单连接 + 进程内互斥锁，足以承载单容器服务；更换 Postgres 时只需重写本模块。
时间戳统一以 UTC ISO8601 字符串入库。
"""
import sqlite3
import threading
from contextlib import contextmanager

from . import config

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS calendars (
    calendar_code TEXT NOT NULL,
    date          TEXT NOT NULL,            -- YYYY-MM-DD（当地日历日期）
    kind          TEXT NOT NULL,            -- holiday | workday
    name          TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (calendar_code, date)
);

CREATE TABLE IF NOT EXISTS policies (
    version     TEXT PRIMARY KEY,
    valid_from TEXT NOT NULL,               -- UTC ISO8601，含
    valid_to   TEXT,                        -- UTC ISO8601，不含；NULL 表示至今有效
    body_json  TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    order_id        TEXT PRIMARY KEY,
    created_at      TEXT NOT NULL,          -- UTC ISO8601，决定适用政策版本
    timezone        TEXT NOT NULL,
    calendar_code   TEXT NOT NULL,
    promise_by      TEXT,                   -- UTC ISO8601，可空（缺失时延迟类不可判定）
    weight_kg       REAL,
    created_policy  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS generations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id      TEXT NOT NULL REFERENCES orders(order_id),
    gen_no        INTEGER NOT NULL,
    opened_at     TEXT NOT NULL,            -- UTC，第一代=订单创建；后续=迟到事件到达
    archived      INTEGER NOT NULL DEFAULT 0,
    archived_at   TEXT,
    supersedes_gen INTEGER,                 -- 新一代开启时，被它替代的已归档代号
    determination_json TEXT,                -- 归档时固化的判定快照（不可变）
    UNIQUE (order_id, gen_no)
);

CREATE TABLE IF NOT EXISTS events (
    event_id     TEXT PRIMARY KEY,
    order_id     TEXT NOT NULL REFERENCES orders(order_id),
    gen_id       INTEGER NOT NULL REFERENCES generations(id),
    event_type   TEXT NOT NULL,
    event_time   TEXT NOT NULL,             -- UTC ISO8601，扫描发生时间
    arrived_at   TEXT NOT NULL,             -- UTC ISO8601，服务收到时间（顺序以此为准）
    is_correction INTEGER NOT NULL DEFAULT 0,
    correction_action TEXT,                 -- amend | cancel | insert
    target_event_id TEXT,                   -- 被更正的原事件
    note         TEXT NOT NULL DEFAULT '',
    prev_arrived_at TEXT                    -- 批次内上一事件到达时间，用于报错定位
);
CREATE INDEX IF NOT EXISTS idx_events_order ON events(order_id, arrived_at);
"""


def connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        path = config.db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(SCHEMA)
        _conn = conn
    return _conn


@contextmanager
def tx():
    """串行化所有写事务，保证事件到达顺序校验与插入的原子性。"""
    with _lock:
        conn = connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def q(sql: str, params=()):
    with _lock:
        return connect().execute(sql, params).fetchall()


def q1(sql: str, params=()):
    with _lock:
        return connect().execute(sql, params).fetchone()


def execute(sql: str, params=()):
    with tx() as conn:
        cur = conn.execute(sql, params)
        return cur


def reset_for_tests():
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None
