"""已供数登记与变化复核服务。

网页与查询 API 都调用本模块，保证两边看到同一结果。

状态模型（不存"待复核"布尔位，全部由数据推导）：
- 登记时保存当时的雨量（registered_*，此后不再改动）；
- baseline_* 是最近一次确认后的雨量，初始等于登记值；
- 待复核 = 当前小时雨量与 baseline 不同（或当前已不可供数）；
- 确认 = 在 BEGIN IMMEDIATE 事务内重新计算当前值，校验数据版本号与
  "页面所见雨量"仍一致后，把 baseline 推进到当前值。校验失败则整体回滚，
  登记保持待复核，不产生任何部分更新。

并发：
- 上传与确认都以 BEGIN IMMEDIATE 串行化；
- 每次读数真正变化时，同事务递增受影响小时的 hour_versions；
- 因此"页面看到 12.0、点击确认前又上传改成 13.0"会被版本号识破并拒绝。
"""
from __future__ import annotations

import sqlite3

from . import core, db


class ServedError(ValueError):
    """业务拒绝；status 为建议的 HTTP 状态码。"""

    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


def compute_hour_result(conn: sqlite3.Connection, station: str, hour_epoch: int) -> dict:
    """网页与 API 共用的小时计算入口。"""
    counters = db.fetch_hour_counters(conn, station, hour_epoch)
    return core.compute_hour(counters, station, hour_epoch)


def build_state(conn: sqlite3.Connection, row: sqlite3.Row, current: dict | None = None) -> dict:
    """把登记行 + 当前计算值合成对外状态 dict。"""
    if current is None:
        current = compute_hour_result(conn, row["station"], row["hour_epoch"])
    version = db.hour_version(conn, row["station"], row["hour_epoch"])
    changed = (
        current["status"] != "ok"
        or current["total_pulses"] != row["baseline_total_pulses"]
    )
    return {
        "served_id": row["served_id"],
        "station": row["station"],
        "hour": core.iso_utc(row["hour_epoch"]),
        "hour_epoch": row["hour_epoch"],
        # 登记时保留的原值
        "registered_at": row["registered_at"],
        "registered_rainfall_mm": row["registered_rainfall_mm"],
        "registered_total_pulses": row["registered_total_pulses"],
        # 最近一次确认（或登记）的基线，即复核清单中的“原值”
        "baseline_rainfall_mm": row["baseline_rainfall_mm"],
        "baseline_total_pulses": row["baseline_total_pulses"],
        "baseline_version": row["baseline_version"],
        "confirmed_at": row["confirmed_at"],
        # 当前可供数情况，即“现值”
        "current_rainfall_mm": current["rainfall_mm"],
        "current_total_pulses": current["total_pulses"],
        "current_status": current["status"],
        "current_missing_intervals": current["missing_intervals"],
        "current_version": version,
        "needs_review": changed,
        # 只有“有变化且当前可供数”才允许确认
        "confirmable": changed and current["status"] == "ok",
    }


def register_served(conn: sqlite3.Connection, station: str, hour_epoch: int):
    """把当前可供数的小时登记为已供数，保留登记时雨量。

    返回 (state, created)；已登记过的小时幂等返回原登记，不覆盖基线，
    以免绕过复核。
    """
    station = (station or "").strip()
    if not station:
        raise ServedError("缺少站点", 400)
    if hour_epoch % core.HOUR_SECONDS != 0:
        raise ServedError("小时必须整点对齐", 400)

    with db.transaction(conn):
        existing = conn.execute(
            "SELECT * FROM served_hours WHERE station=? AND hour_epoch=?",
            (station, hour_epoch),
        ).fetchone()
        if existing is not None:
            return build_state(conn, existing), False

        current = compute_hour_result(conn, station, hour_epoch)
        if current["status"] != "ok":
            raise ServedError(
                f"{station} @ {core.iso_utc(hour_epoch)} 当前不可供数"
                f"（缺口 {current['missing_intervals']} / {core.INTERVALS_PER_HOUR} 个间隔），不能登记",
                400,
            )
        version = db.hour_version(conn, station, hour_epoch)
        now = db.utcnow_iso()
        cur = conn.execute(
            "INSERT INTO served_hours ("
            " station, hour_epoch, registered_at,"
            " registered_rainfall_mm, registered_total_pulses,"
            " baseline_rainfall_mm, baseline_total_pulses, baseline_version)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                station, hour_epoch, now,
                current["rainfall_mm"], current["total_pulses"],
                current["rainfall_mm"], current["total_pulses"], version,
            ),
        )
        served_id = cur.lastrowid
        conn.execute(
            "INSERT INTO served_events"
            " (served_id, event, rainfall_mm, total_pulses, hour_version, at)"
            " VALUES (?, 'registered', ?, ?, ?, ?)",
            (served_id, current["rainfall_mm"], current["total_pulses"], version, now),
        )
        row = conn.execute(
            "SELECT * FROM served_hours WHERE served_id=?", (served_id,)
        ).fetchone()
        return build_state(conn, row, current), True


def confirm_served(
    conn: sqlite3.Connection,
    served_id: int,
    expected_version: int | None,
    expected_rainfall_mm: float | None,
) -> dict:
    """确认页面所见雨量。

    仅当当前值仍待复核、数据版本号与页面一致、且页面雨量仍等于当前雨量时
    才更新基线；否则整体回滚并保持待复核。
    """
    with db.transaction(conn):
        row = conn.execute(
            "SELECT * FROM served_hours WHERE served_id=?", (served_id,)
        ).fetchone()
        if row is None:
            raise ServedError("已供数登记不存在", 404)

        current = compute_hour_result(conn, row["station"], row["hour_epoch"])
        version = db.hour_version(conn, row["station"], row["hour_epoch"])

        if current["status"] != "ok":
            raise ServedError(
                "该小时当前不可供数，不能确认；清单保持待复核", 409
            )
        if row["baseline_total_pulses"] == current["total_pulses"]:
            raise ServedError("该小时雨量未变化，无需确认", 409)
        if expected_version is None or int(expected_version) != version:
            raise ServedError(
                "核对期间该小时读数已被再次上传改变，本次确认已拒绝；"
                "请按刷新后的数值重新核对", 409
            )
        if expected_rainfall_mm is None or abs(
            round(float(expected_rainfall_mm), 1) - float(current["rainfall_mm"])
        ) > 1e-9:
            raise ServedError(
                "页面所见雨量与当前雨量不一致，本次确认已拒绝；请刷新后重新核对", 409
            )

        now = db.utcnow_iso()
        conn.execute(
            "UPDATE served_hours SET"
            " baseline_rainfall_mm=?, baseline_total_pulses=?, baseline_version=?,"
            " confirmed_at=?"
            " WHERE served_id=?",
            (current["rainfall_mm"], current["total_pulses"], version, now, served_id),
        )
        conn.execute(
            "INSERT INTO served_events"
            " (served_id, event, rainfall_mm, total_pulses, hour_version, at)"
            " VALUES (?, 'confirmed', ?, ?, ?, ?)",
            (served_id, current["rainfall_mm"], current["total_pulses"], version, now),
        )
        row = conn.execute(
            "SELECT * FROM served_hours WHERE served_id=?", (served_id,)
        ).fetchone()
        return build_state(conn, row, current)


def get_served(conn: sqlite3.Connection, served_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM served_hours WHERE served_id=?", (served_id,)
    ).fetchone()
    return build_state(conn, row) if row is not None else None


def list_served(conn: sqlite3.Connection, station: str | None = None) -> list[dict]:
    """全部已供数登记（含 needs_review 标志）。

    整个列表在一个读事务内构建：清单里的每行都基于同一数据快照，
    不会出现一半旧值一半新值的混合结果。
    """
    with db.read_snapshot(conn):
        return _list_served_unlocked(conn, station)


def _list_served_unlocked(
    conn: sqlite3.Connection, station: str | None = None
) -> list[dict]:
    if station:
        rows = conn.execute(
            "SELECT * FROM served_hours WHERE station=? ORDER BY station, hour_epoch",
            (station,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM served_hours ORDER BY station, hour_epoch"
        ).fetchall()
    return [build_state(conn, row) for row in rows]


def list_reviews(conn: sqlite3.Connection, station: str | None = None) -> list[dict]:
    """待复核清单：仅雨量已变化的已供数小时，未变化的不进入。"""
    return [s for s in list_served(conn, station) if s["needs_review"]]


def summarize(conn: sqlite3.Connection, station: str | None = None) -> tuple[list, list]:
    """一次快照内同时取全部登记与待复核清单，供同一页面并列展示。"""
    with db.read_snapshot(conn):
        items = _list_served_unlocked(conn, station)
    return items, [s for s in items if s["needs_review"]]


def list_events(conn: sqlite3.Connection, served_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT event, rainfall_mm, total_pulses, hour_version, at"
        " FROM served_events WHERE served_id=? ORDER BY event_id",
        (served_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def reviews_for_hours(conn: sqlite3.Connection, hours) -> list[dict]:
    """给定 (station, hour_epoch) 集合，返回其中当前处于待复核的已供数小时。

    上传报告用：只列出本批真正改变、且已登记供数的那些小时。
    """
    hours = list(hours)
    if not hours:
        return []
    with db.read_snapshot(conn):
        out = []
        for station, hour_epoch in hours:
            row = conn.execute(
                "SELECT * FROM served_hours WHERE station=? AND hour_epoch=?",
                (station, hour_epoch),
            ).fetchone()
            if row is None:
                continue
            state = build_state(conn, row)
            if state["needs_review"]:
                out.append(state)
    return out
