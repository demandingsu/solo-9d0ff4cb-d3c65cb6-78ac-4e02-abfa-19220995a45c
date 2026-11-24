"""领域服务：批量报送事务 + 走行里程查询。

设计要点：
- 报送在单事务内完成：原始表写入 + 服务层（日表/连续块）同步维护，
  任何校验失败整体回滚，旧供数结果保留。
- 查询只读 wheelset_overhaul / wheelset_mount 和两张服务表，
  绝不扫描原始日里程表 car_daily_mileage。
- 缺报日不建档：连续块之间的空隙即缺报段。
"""

from __future__ import annotations

import datetime as dt
import sqlite3

from .errors import ConflictError, MissingBaselineError
from .schemas import BatchIngestRequest

# ---------------------------------------------------------------------------
# UTC 日工具
# ---------------------------------------------------------------------------


def add_days(day: str, delta: int) -> str:
    return (dt.date.fromisoformat(day) + dt.timedelta(days=delta)).isoformat()


# ---------------------------------------------------------------------------
# 服务层维护：连续已报日块 [start_day, end_day)
# ---------------------------------------------------------------------------


def _block_add_day(conn: sqlite3.Connection, car_id: str, day: str) -> None:
    """把一个已报日并入连续块（调用前保证该日尚未被块覆盖）。"""
    tomorrow = add_days(day, 1)
    left = conn.execute(
        "SELECT start_day, end_day FROM car_mileage_block "
        "WHERE car_id = ? AND end_day = ?",
        (car_id, day),
    ).fetchone()
    right = conn.execute(
        "SELECT start_day, end_day FROM car_mileage_block "
        "WHERE car_id = ? AND start_day = ?",
        (car_id, tomorrow),
    ).fetchone()
    if left and right:
        conn.execute(
            "UPDATE car_mileage_block SET end_day = ? "
            "WHERE car_id = ? AND start_day = ?",
            (right["end_day"], car_id, left["start_day"]),
        )
        conn.execute(
            "DELETE FROM car_mileage_block WHERE car_id = ? AND start_day = ?",
            (car_id, right["start_day"]),
        )
    elif left:
        conn.execute(
            "UPDATE car_mileage_block SET end_day = ? "
            "WHERE car_id = ? AND start_day = ?",
            (tomorrow, car_id, left["start_day"]),
        )
    elif right:
        conn.execute(
            "UPDATE car_mileage_block SET start_day = ? "
            "WHERE car_id = ? AND start_day = ?",
            (day, car_id, right["start_day"]),
        )
    else:
        conn.execute(
            "INSERT INTO car_mileage_block (car_id, start_day, end_day) "
            "VALUES (?, ?, ?)",
            (car_id, day, tomorrow),
        )


def _block_remove_day(conn: sqlite3.Connection, car_id: str, day: str) -> None:
    """从连续块中摘除一个日（调用前保证该日被块覆盖）。"""
    block = conn.execute(
        "SELECT start_day, end_day FROM car_mileage_block "
        "WHERE car_id = ? AND start_day <= ? AND end_day > ?",
        (car_id, day, day),
    ).fetchone()
    if block is None:  # 防御：理论上不会发生
        raise RuntimeError(f"服务层块状态不一致：{car_id} {day} 不在任何连续块中")
    start, end = block["start_day"], block["end_day"]
    tomorrow = add_days(day, 1)
    if start == day and end == tomorrow:
        conn.execute(
            "DELETE FROM car_mileage_block WHERE car_id = ? AND start_day = ?",
            (car_id, start),
        )
    elif start == day:
        conn.execute(
            "UPDATE car_mileage_block SET start_day = ? "
            "WHERE car_id = ? AND start_day = ?",
            (tomorrow, car_id, start),
        )
    elif end == tomorrow:
        conn.execute(
            "UPDATE car_mileage_block SET end_day = ? "
            "WHERE car_id = ? AND start_day = ?",
            (day, car_id, start),
        )
    else:
        conn.execute(
            "UPDATE car_mileage_block SET end_day = ? "
            "WHERE car_id = ? AND start_day = ?",
            (day, car_id, start),
        )
        conn.execute(
            "INSERT INTO car_mileage_block (car_id, start_day, end_day) "
            "VALUES (?, ?, ?)",
            (car_id, tomorrow, end),
        )


def _serving_upsert(
    conn: sqlite3.Connection,
    old_car: str | None,
    old_day: str | None,
    new_car: str,
    new_day: str,
    meters: int,
) -> None:
    """同步服务层日表与连续块，支持更正（含换车厢、换日）。"""
    if old_car is not None and (old_car, old_day) != (new_car, new_day):
        conn.execute(
            "DELETE FROM car_mileage_day_serving WHERE car_id = ? AND day = ?",
            (old_car, old_day),
        )
        _block_remove_day(conn, old_car, old_day)

    conn.execute(
        "INSERT INTO car_mileage_day_serving (car_id, day, meters) VALUES (?, ?, ?) "
        "ON CONFLICT(car_id, day) DO UPDATE SET meters = excluded.meters",
        (new_car, new_day, meters),
    )
    if old_car is None or (old_car, old_day) != (new_car, new_day):
        # 同日同车厢更正只改米数，块不动；否则新日入块
        covered = conn.execute(
            "SELECT 1 FROM car_mileage_block "
            "WHERE car_id = ? AND start_day <= ? AND end_day > ?",
            (new_car, new_day, new_day),
        ).fetchone()
        if not covered:
            _block_add_day(conn, new_car, new_day)


# ---------------------------------------------------------------------------
# 批量报送（单事务，失败整体回滚）
# ---------------------------------------------------------------------------


def _empty_counter() -> dict[str, int]:
    return {"mileage": 0, "mounts": 0, "overhauls": 0}


def _empty_record_lists() -> dict[str, list[str]]:
    return {"mileage": [], "mounts": [], "overhauls": []}


def ingest_batch(conn: sqlite3.Connection, req: BatchIngestRequest) -> dict:
    inserted = _empty_counter()
    corrected = _empty_counter()
    inserted_nos = _empty_record_lists()
    corrected_nos = _empty_record_lists()
    received = {
        "mileage": len(req.mileage),
        "mounts": len(req.mounts),
        "overhauls": len(req.overhauls),
    }

    try:
        # 1) 车厢日里程：原始表 upsert + 服务层同步
        for rec in req.mileage:
            old = conn.execute(
                "SELECT car_id, day FROM car_daily_mileage WHERE record_no = ?",
                (rec.record_no,),
            ).fetchone()
            existed = old is not None
            conn.execute(
                "INSERT INTO car_daily_mileage (record_no, car_id, day, meters) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(record_no) DO UPDATE SET "
                "car_id = excluded.car_id, day = excluded.day, meters = excluded.meters",
                (rec.record_no, rec.car_id, rec.day, rec.meters),
            )
            _serving_upsert(
                conn,
                old["car_id"] if old else None,
                old["day"] if old else None,
                rec.car_id,
                rec.day,
                rec.meters,
            )
            if existed:
                corrected["mileage"] += 1
                corrected_nos["mileage"].append(rec.record_no)
            else:
                inserted["mileage"] += 1
                inserted_nos["mileage"].append(rec.record_no)

        # 2) 装车段
        for rec in req.mounts:
            old = conn.execute(
                "SELECT record_no FROM wheelset_mount WHERE record_no = ?",
                (rec.record_no,),
            ).fetchone()
            conn.execute(
                "INSERT INTO wheelset_mount "
                "(record_no, wheelset_id, car_id, start_day, end_day) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(record_no) DO UPDATE SET "
                "wheelset_id = excluded.wheelset_id, car_id = excluded.car_id, "
                "start_day = excluded.start_day, end_day = excluded.end_day",
                (
                    rec.record_no,
                    rec.wheelset_id,
                    rec.car_id,
                    rec.start_day,
                    rec.end_day,
                ),
            )
            if old:
                corrected["mounts"] += 1
                corrected_nos["mounts"].append(rec.record_no)
            else:
                inserted["mounts"] += 1
                inserted_nos["mounts"].append(rec.record_no)

        # 同一轮对装车段重叠（半开区间）：退回整批
        overlap = conn.execute(
            "SELECT m1.record_no AS a, m2.record_no AS b, m1.wheelset_id "
            "FROM wheelset_mount m1 "
            "JOIN wheelset_mount m2 "
            "  ON m1.wheelset_id = m2.wheelset_id "
            " AND m1.record_no < m2.record_no "
            " AND m1.start_day < m2.end_day "
            " AND m2.start_day < m1.end_day "
            "LIMIT 1"
        ).fetchone()
        if overlap:
            raise ConflictError(
                f"轮对 {overlap['wheelset_id']} 的装车段重叠："
                f"记录 {overlap['a']} 与 {overlap['b']}，整批退回"
            )

        # 3) 大修日
        for rec in req.overhauls:
            old = conn.execute(
                "SELECT record_no FROM wheelset_overhaul WHERE record_no = ?",
                (rec.record_no,),
            ).fetchone()
            conn.execute(
                "INSERT INTO wheelset_overhaul (record_no, wheelset_id, overhaul_day) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(record_no) DO UPDATE SET "
                "wheelset_id = excluded.wheelset_id, "
                "overhaul_day = excluded.overhaul_day",
                (rec.record_no, rec.wheelset_id, rec.overhaul_day),
            )
            if old:
                corrected["overhauls"] += 1
                corrected_nos["overhauls"].append(rec.record_no)
            else:
                inserted["overhauls"] += 1
                inserted_nos["overhauls"].append(rec.record_no)

        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise ConflictError(f"数据约束冲突，整批退回：{exc}") from exc
    except Exception:
        conn.rollback()
        raise

    return {
        "received": received,
        "inserted": inserted,
        "corrected": corrected,
        "inserted_record_nos": inserted_nos,
        "corrected_record_nos": corrected_nos,
    }


# ---------------------------------------------------------------------------
# 查询：按轮对 + 截止日，从最近一次大修起跨车厢累加
# ---------------------------------------------------------------------------


def _missing_ranges_for_segment(
    conn: sqlite3.Connection, car_id: str, seg_start: str, seg_end: str
) -> list[tuple[str, str]]:
    """利用连续块空隙推导 [seg_start, seg_end) 内的缺报段，缺报日不建档。"""
    blocks = conn.execute(
        "SELECT start_day, end_day FROM car_mileage_block "
        "WHERE car_id = ? AND start_day < ? AND end_day > ? "
        "ORDER BY start_day",
        (car_id, seg_end, seg_start),
    ).fetchall()
    gaps: list[tuple[str, str]] = []
    cursor = seg_start
    for b in blocks:
        b_start = max(b["start_day"], seg_start)
        b_end = min(b["end_day"], seg_end)
        if b_start > cursor:
            gaps.append((cursor, b_start))
        if b_end > cursor:
            cursor = b_end
    if cursor < seg_end:
        gaps.append((cursor, seg_end))
    return gaps


def _merge_adjacent(ranges: list[tuple[str, str]]) -> list[tuple[str, str]]:
    if not ranges:
        return []
    merged = [ranges[0]]
    for start, end in ranges[1:]:
        last_start, last_end = merged[-1]
        if start == last_end:  # 相邻段（同一车的多个装车段）合并
            merged[-1] = (last_start, end)
        else:
            merged.append((start, end))
    return merged


def query_mileage(conn: sqlite3.Connection, wheelset_id: str, cutoff_day: str) -> dict:
    baseline = conn.execute(
        "SELECT MAX(overhaul_day) AS d FROM wheelset_overhaul "
        "WHERE wheelset_id = ? AND overhaul_day <= ?",
        (wheelset_id, cutoff_day),
    ).fetchone()["d"]
    if baseline is None:
        raise MissingBaselineError(
            f"轮对 {wheelset_id} 在 {cutoff_day}（含）之前没有大修记录，缺少起算日"
        )

    range_end_excl = add_days(cutoff_day, 1)  # 含截止日
    mounts = conn.execute(
        "SELECT car_id, start_day, end_day FROM wheelset_mount "
        "WHERE wheelset_id = ? AND start_day < ? AND end_day > ? "
        "ORDER BY start_day, car_id",
        (wheelset_id, range_end_excl, baseline),
    ).fetchall()

    cars_out: list[dict] = []
    missing_by_car: dict[str, list[dict]] = {}
    total_known = 0
    all_complete = True

    for m in mounts:
        seg_start = max(m["start_day"], baseline)
        seg_end = min(m["end_day"], range_end_excl)
        if seg_start >= seg_end:
            continue
        row = conn.execute(
            "SELECT COALESCE(SUM(meters), 0) AS total "
            "FROM car_mileage_day_serving "
            "WHERE car_id = ? AND day >= ? AND day < ?",
            (m["car_id"], seg_start, seg_end),
        ).fetchone()
        known = int(row["total"])
        total_known += known

        gaps = _missing_ranges_for_segment(conn, m["car_id"], seg_start, seg_end)
        ranges = _merge_adjacent(gaps)
        missing = [
            {
                "start_day": s,
                "end_day": e,
                "days": (dt.date.fromisoformat(e) - dt.date.fromisoformat(s)).days,
            }
            for s, e in ranges
        ]
        if missing:
            all_complete = False
            missing_by_car.setdefault(m["car_id"], [])
            missing_by_car[m["car_id"]].extend(missing)
        cars_out.append(
            {
                "car_id": m["car_id"],
                "mount_start_day": seg_start,
                "mount_end_day": seg_end,
                "known_meters": known,
                "missing_ranges": missing,
            }
        )

    return {
        "wheelset_id": wheelset_id,
        "cutoff_day": cutoff_day,
        "baseline_overhaul_day": baseline,
        "range_start_day": baseline,
        "range_end_exclusive_day": range_end_excl,
        "known_meters": total_known,
        "mileage_complete": all_complete,
        "is_lower_bound": not all_complete,
        "cars": cars_out,
        "missing_ranges": missing_by_car,
    }
