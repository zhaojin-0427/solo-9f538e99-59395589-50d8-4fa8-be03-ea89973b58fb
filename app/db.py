"""SQLite 持久化层：模式、事务性批量入库、已供数登记与确认、查询。

设计要点：
- readings 以 (station, device, minute_epoch) 为主键，同键只接受更高修订号；
- 每批上传在单个事务中写入（读数 + 批次记录 + 行级明细 + 小时版本号），
  任何异常整体回滚，不会留下半批新结果；
- 已供数登记保存登记时的雨量；待复核清单由“当前雨量 vs 确认基线”实时推导，
  不额外存状态位，因此读数与清单不会出现半更新；
- 每个小时的 data version 在读数被接受时同事务递增，确认时校验版本，
  防止“页面看到后、点击确认前”又被上传改变；
- 数据库文件落在磁盘（Docker 中挂载卷），重启后登记与清单仍可查询。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from . import core

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    station      TEXT    NOT NULL,
    device       TEXT    NOT NULL CHECK (device IN ('primary', 'backup')),
    minute_epoch INTEGER NOT NULL,
    counter      INTEGER NOT NULL CHECK (counter BETWEEN 0 AND 9999),
    revision     INTEGER NOT NULL CHECK (revision >= 0),
    updated_at   TEXT    NOT NULL,
    PRIMARY KEY (station, device, minute_epoch)
);
CREATE INDEX IF NOT EXISTS idx_readings_station_minute
    ON readings (station, minute_epoch);

CREATE TABLE IF NOT EXISTS batches (
    batch_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    uploaded_at   TEXT NOT NULL,
    filename      TEXT,
    total_rows    INTEGER NOT NULL,
    accepted_rows INTEGER NOT NULL,
    rejected_rows INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS batch_rows (
    batch_id INTEGER NOT NULL REFERENCES batches (batch_id) ON DELETE CASCADE,
    row_no   INTEGER NOT NULL,
    outcome  TEXT NOT NULL CHECK (outcome IN ('accepted', 'rejected')),
    detail   TEXT,
    raw      TEXT
);
CREATE INDEX IF NOT EXISTS idx_batch_rows_batch ON batch_rows (batch_id);

-- 每小时数据版本：任一读数被接受时同事务递增；用于确认时的并发校验
CREATE TABLE IF NOT EXISTS hour_versions (
    station     TEXT    NOT NULL,
    hour_epoch  INTEGER NOT NULL,
    version     INTEGER NOT NULL,
    PRIMARY KEY (station, hour_epoch)
);

-- 已供数登记：登记时雨量不可变；baseline 为最近一次确认（或登记）的雨量
CREATE TABLE IF NOT EXISTS served_hours (
    served_id                INTEGER PRIMARY KEY AUTOINCREMENT,
    station                  TEXT    NOT NULL,
    hour_epoch               INTEGER NOT NULL,
    registered_at            TEXT    NOT NULL,
    registered_rainfall_mm   REAL    NOT NULL,
    registered_total_pulses  INTEGER NOT NULL,
    baseline_rainfall_mm     REAL    NOT NULL,
    baseline_total_pulses    INTEGER NOT NULL,
    baseline_version         INTEGER NOT NULL,
    confirmed_at             TEXT,
    UNIQUE (station, hour_epoch)
);

-- 登记/确认审计轨迹（保留每次确认的雨量与版本）
CREATE TABLE IF NOT EXISTS served_events (
    event_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    served_id  INTEGER NOT NULL REFERENCES served_hours (served_id) ON DELETE CASCADE,
    event      TEXT    NOT NULL CHECK (event IN ('registered', 'confirmed')),
    rainfall_mm REAL   NOT NULL,
    total_pulses INTEGER NOT NULL,
    hour_version INTEGER NOT NULL,
    at         TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_served_events_served ON served_events (served_id);
"""



def _utcnow_iso() -> str:
    return utcnow_iso()


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(db_path: str) -> sqlite3.Connection:
    """新建连接。autocommit 模式，写操作由 apply_batch 显式开事务。"""
    conn = sqlite3.connect(db_path, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


@contextmanager
def transaction(conn: sqlite3.Connection):
    """BEGIN IMMEDIATE 事务：失败整体回滚，不留半批结果。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise


@contextmanager
def read_snapshot(conn: sqlite3.Connection):
    """只读一致性快照：多条查询看到同一提交时刻，避免列表内新旧值混杂。"""
    conn.execute("BEGIN")
    try:
        yield
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise


def hour_version(conn: sqlite3.Connection, station: str, hour_epoch: int) -> int:
    """某站某小时的读数版本号；从未有读数时为 0。"""
    row = conn.execute(
        "SELECT version FROM hour_versions WHERE station=? AND hour_epoch=?",
        (station, hour_epoch),
    ).fetchone()
    return row["version"] if row is not None else 0


def _bump_hour_version(conn: sqlite3.Connection, station: str, hour_epoch: int) -> None:
    conn.execute(
        "INSERT INTO hour_versions (station, hour_epoch, version) VALUES (?, ?, 1)"
        " ON CONFLICT (station, hour_epoch) DO UPDATE SET version = version + 1",
        (station, hour_epoch),
    )


def _affected_hours(minute_epoch: int):
    """该分钟读数会影响的整点小时：所在小时；整点分钟还影响前一小时。"""
    hour = minute_epoch - minute_epoch % core.HOUR_SECONDS
    hours = [hour]
    if minute_epoch % core.HOUR_SECONDS == 0:
        hours.append(hour - core.HOUR_SECONDS)
    return hours


def _hour_snapshot(conn: sqlite3.Connection, station: str, hour_epoch: int):
    """小时的可比较快照：(status, total_pulses)。"""
    counters = fetch_hour_counters(conn, station, hour_epoch)
    result = core.compute_hour(counters, station, hour_epoch)
    return result["status"], result["total_pulses"]


def _served_candidates(conn: sqlite3.Connection, valid_rows):
    """本批读数可能影响、且已登记为已供数的小时集合。"""
    pairs = {(r.station, hour) for _, _, r in valid_rows for hour in _affected_hours(r.minute_epoch)}
    if not pairs:
        return set()
    return {
        (station, hour) for station, hour in pairs
        if conn.execute(
            "SELECT 1 FROM served_hours WHERE station=? AND hour_epoch=?",
            (station, hour),
        ).fetchone() is not None
    }


def apply_batch(conn: sqlite3.Connection, valid_rows, row_errors, filename=None) -> dict:
    """在同一事务中应用一批读数；任何异常整体回滚，不留半批结果。

    valid_rows: [(row_no, raw, Reading)]，按文件顺序逐行处理；
    row_errors: 解析阶段已拒绝的 RowError 列表。
    同键（站点,设备,分钟）只接受更高修订号，否则记为拒绝。
    数值确实变化时，同事务内递增受影响小时的版本号（整点读数影响前后两小时），
    供已供数确认做并发校验。
    返回批次报告 dict，其中 changed_served_hours 是本批真正改变了已供数小时
    雨量（合计）的那些小时。
    """
    now = _utcnow_iso()
    accepted = []
    rejected = [{"row_no": e.row_no, "reason": e.reason, "raw": e.raw} for e in row_errors]
    total = len(valid_rows) + len(row_errors)

    with transaction(conn):
        # 写入前先记录可能受影响且已登记供数的小时，用于判断本批是否改变其雨量
        candidates = _served_candidates(conn, valid_rows)
        before = {
            (s, h): _hour_snapshot(conn, s, h) for s, h in sorted(candidates)
        }
        changed_served = set()

        cur = conn.execute(
            "INSERT INTO batches (uploaded_at, filename, total_rows, accepted_rows, rejected_rows)"
            " VALUES (?, ?, ?, 0, 0)",
            (now, filename, total),
        )
        batch_id = cur.lastrowid

        for row_no, raw, r in valid_rows:
            existing = conn.execute(
                "SELECT counter, revision FROM readings"
                " WHERE station=? AND device=? AND minute_epoch=?",
                (r.station, r.device, r.minute_epoch),
            ).fetchone()
            if existing is not None and r.revision <= existing["revision"]:
                rejected.append({
                    "row_no": row_no,
                    "reason": f"修订号 {r.revision} 不高于已存的 {existing['revision']}，同键只接受更高修订",
                    "raw": raw,
                })
                continue
            if existing is None:
                conn.execute(
                    "INSERT INTO readings (station, device, minute_epoch, counter, revision, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (r.station, r.device, r.minute_epoch, r.counter, r.revision, now),
                )
                action, changed = "inserted", True
            else:
                conn.execute(
                    "UPDATE readings SET counter=?, revision=?, updated_at=?"
                    " WHERE station=? AND device=? AND minute_epoch=?",
                    (r.counter, r.revision, now, r.station, r.device, r.minute_epoch),
                )
                action, changed = "updated", r.counter != existing["counter"]
            if changed:
                # 与读数写入同事务：不会出现读数已变、版本未变的半批结果
                for hour in _affected_hours(r.minute_epoch):
                    _bump_hour_version(conn, r.station, hour)
            accepted.append({"row_no": row_no, "action": action, "reading": r})
            conn.execute(
                "INSERT INTO batch_rows (batch_id, row_no, outcome, detail, raw)"
                " VALUES (?, ?, 'accepted', ?, ?)",
                (batch_id, row_no, action, raw),
            )

        for rej in rejected:
            conn.execute(
                "INSERT INTO batch_rows (batch_id, row_no, outcome, detail, raw)"
                " VALUES (?, ?, 'rejected', ?, ?)",
                (batch_id, rej["row_no"], rej["reason"], rej["raw"]),
            )

        conn.execute(
            "UPDATE batches SET accepted_rows=?, rejected_rows=? WHERE batch_id=?",
            (len(accepted), len(rejected), batch_id),
        )

        # 写入后对比小时雨量：只有合计真正变化的小时才算“本批改变”
        for key, old in before.items():
            if _hour_snapshot(conn, *key) != old:
                changed_served.add(key)

    rejected.sort(key=lambda x: x["row_no"])
    return {
        "batch_id": batch_id,
        "filename": filename,
        "uploaded_at": now,
        "total_rows": total,
        "accepted_count": len(accepted),
        "rejected_count": len(rejected),
        "accepted": accepted,
        "rejected": rejected,
        "changed_served_hours": sorted(changed_served),
    }


def fetch_hour_counters(conn: sqlite3.Connection, station: str, hour_start_epoch: int) -> dict:
    """取某站某小时 61 个端点（含下一整点）的全部读数。"""
    rows = conn.execute(
        "SELECT device, minute_epoch, counter FROM readings"
        " WHERE station=? AND minute_epoch BETWEEN ? AND ?",
        (station, hour_start_epoch, hour_start_epoch + 3600),
    ).fetchall()
    return {(r["device"], r["minute_epoch"]): r["counter"] for r in rows}


def list_stations(conn: sqlite3.Connection):
    return conn.execute(
        "SELECT station, COUNT(*) AS n, MIN(minute_epoch) AS first_minute,"
        " MAX(minute_epoch) AS last_minute"
        " FROM readings GROUP BY station ORDER BY station"
    ).fetchall()


def list_batches(conn: sqlite3.Connection, limit: int = 20):
    return conn.execute(
        "SELECT * FROM batches ORDER BY batch_id DESC LIMIT ?", (limit,)
    ).fetchall()


def get_batch(conn: sqlite3.Connection, batch_id: int):
    batch = conn.execute(
        "SELECT * FROM batches WHERE batch_id=?", (batch_id,)
    ).fetchone()
    if batch is None:
        return None, []
    rows = conn.execute(
        "SELECT row_no, outcome, detail, raw FROM batch_rows"
        " WHERE batch_id=? ORDER BY row_no",
        (batch_id,),
    ).fetchall()
    return batch, rows
