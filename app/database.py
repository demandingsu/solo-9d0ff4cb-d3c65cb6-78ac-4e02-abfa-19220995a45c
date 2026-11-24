"""SQLite 连接管理与表结构。

表分两层：

1. 原始供数层（报送/更正留痕）：
   - car_daily_mileage   车厢 UTC 日走行米数（按记录号报送、可更正）
   - wheelset_mount      轮对在车厢上的装车日期段（含首日、不含末日）
   - wheelset_overhaul   轮对大修日

2. 供数服务层（随报送事务同步维护，查询只走这一层，不扫原始日里程表）：
   - car_mileage_day_serving  已报日的车厢-日里程（零米也在行），主键区间求和
   - car_mileage_block        连续已报日压缩成的日期块，缺报段由块间空隙推导，
                              缺报日不逐日建档
"""

from __future__ import annotations

import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS car_daily_mileage (
    record_no TEXT PRIMARY KEY,
    car_id    TEXT NOT NULL,
    day       TEXT NOT NULL,              -- UTC 日，YYYY-MM-DD
    meters    INTEGER NOT NULL CHECK (meters >= 0),
    UNIQUE (car_id, day)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS wheelset_mount (
    record_no   TEXT PRIMARY KEY,
    wheelset_id TEXT NOT NULL,
    car_id      TEXT NOT NULL,
    start_day   TEXT NOT NULL,            -- 含首日
    end_day     TEXT NOT NULL,            -- 不含末日
    CHECK (end_day > start_day)
);
CREATE INDEX IF NOT EXISTS idx_mount_wheelset
    ON wheelset_mount (wheelset_id, start_day, end_day);

CREATE TABLE IF NOT EXISTS wheelset_overhaul (
    record_no    TEXT PRIMARY KEY,
    wheelset_id  TEXT NOT NULL,
    overhaul_day TEXT NOT NULL,
    UNIQUE (wheelset_id, overhaul_day)
) WITHOUT ROWID;

-- 服务层：每个“已报”车厢-日一行（含零米），缺报日没有行
CREATE TABLE IF NOT EXISTS car_mileage_day_serving (
    car_id TEXT NOT NULL,
    day    TEXT NOT NULL,
    meters INTEGER NOT NULL,
    PRIMARY KEY (car_id, day)
) WITHOUT ROWID;

-- 服务层：连续已报日压缩块 [start_day, end_day)
CREATE TABLE IF NOT EXISTS car_mileage_block (
    car_id    TEXT NOT NULL,
    start_day TEXT NOT NULL,
    end_day   TEXT NOT NULL,              -- 不含末日
    PRIMARY KEY (car_id, start_day)
) WITHOUT ROWID;
"""


def get_db_path() -> str:
    return os.environ.get("DB_PATH", os.path.join(os.getcwd(), "data", "warehouse.db"))


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or get_db_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
