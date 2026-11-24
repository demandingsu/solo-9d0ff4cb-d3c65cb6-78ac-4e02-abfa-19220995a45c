"""端到端接口测试：走 /ingest/batch 与里程查询接口。"""

from __future__ import annotations

import os
import sqlite3

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("DB_PATH", str(db_file))
    # 延迟导入，确保 DB_PATH 生效
    from app.main import app
    from app.database import connect, init_db

    with TestClient(app) as c:
        conn = connect(str(db_file))
        init_db(conn)
        conn.close()
        yield c, str(db_file)


def _batch(client, mileage=None, mounts=None, overhauls=None):
    return client.post(
        "/ingest/batch",
        json={
            "mileage": mileage or [],
            "mounts": mounts or [],
            "overhauls": overhauls or [],
        },
    )


def _query(client, wheelset_id: str, cutoff_day: str):
    return client.get(
        f"/wheelsets/{wheelset_id}/mileage",
        params={"cutoff_day": cutoff_day},
    )


def test_single_car_full_cycle_with_zero_day(client):
    c, _ = client
    r = _batch(
        c,
        overhauls=[{"record_no": "OH-1", "wheelset_id": "W1", "overhaul_day": "2026-01-10"}],
        mounts=[{"record_no": "M-1", "wheelset_id": "W1", "car_id": "C1",
                 "start_day": "2026-01-10", "end_day": "2026-01-13"}],
        mileage=[
            {"record_no": "D-1", "car_id": "C1", "day": "2026-01-10", "meters": 100},
            {"record_no": "D-2", "car_id": "C1", "day": "2026-01-11", "meters": 0},
            {"record_no": "D-3", "car_id": "C1", "day": "2026-01-12", "meters": 200},
        ],
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["inserted"] == {"mileage": 3, "mounts": 1, "overhauls": 1}
    assert body["corrected"] == {"mileage": 0, "mounts": 0, "overhauls": 0}

    r = _query(c, "W1", "2026-01-12")
    assert r.status_code == 200, r.text
    data = r.json()
    # 含大修日与截止日：100 + 0 + 200，零米算已报，里程完整
    assert data["known_meters"] == 300
    assert data["mileage_complete"] is True
    assert data["is_lower_bound"] is False
    assert data["baseline_overhaul_day"] == "2026-01-10"
    assert data["range_start_day"] == "2026-01-10"
    assert data["range_end_exclusive_day"] == "2026-01-13"
    assert data["cars"][0]["car_id"] == "C1"
    assert data["missing_ranges"] == {}


def test_transfer_between_cars_half_open_segments(client):
    c, _ = client
    r = _batch(
        c,
        overhauls=[{"record_no": "OH-1", "wheelset_id": "W1", "overhaul_day": "2026-02-01"}],
        mounts=[
            # 相邻段：1 日在 C1，2 日起在 C2，半开区间不重叠
            {"record_no": "M-1", "wheelset_id": "W1", "car_id": "C1",
             "start_day": "2026-02-01", "end_day": "2026-02-02"},
            {"record_no": "M-2", "wheelset_id": "W1", "car_id": "C2",
             "start_day": "2026-02-02", "end_day": "2026-02-04"},
        ],
        mileage=[
            {"record_no": "D-1", "car_id": "C1", "day": "2026-02-01", "meters": 111},
            {"record_no": "D-2", "car_id": "C2", "day": "2026-02-02", "meters": 222},
            {"record_no": "D-3", "car_id": "C2", "day": "2026-02-03", "meters": 333},
        ],
    )
    assert r.status_code == 200, r.text

    r = _query(c, "W1", "2026-02-03")
    data = r.json()
    assert r.status_code == 200
    assert data["known_meters"] == 666  # 跨车厢累加
    assert data["mileage_complete"] is True
    cars = {x["car_id"]: x for x in data["cars"]}
    assert cars["C1"]["known_meters"] == 111
    assert cars["C2"]["known_meters"] == 555


def test_missing_reports_give_lower_bound_and_ranges(client):
    c, _ = client
    r = _batch(
        c,
        overhauls=[{"record_no": "OH-1", "wheelset_id": "W1", "overhaul_day": "2026-03-01"}],
        mounts=[{"record_no": "M-1", "wheelset_id": "W1", "car_id": "C1",
                 "start_day": "2026-03-01", "end_day": "2026-03-06"}],
        mileage=[
            # 缺 03-02、03-03；03-04 零米已报；缺 03-05
            {"record_no": "D-1", "car_id": "C1", "day": "2026-03-01", "meters": 10},
            {"record_no": "D-2", "car_id": "C1", "day": "2026-03-04", "meters": 0},
        ],
    )
    assert r.status_code == 200, r.text

    r = _query(c, "W1", "2026-03-05")
    data = r.json()
    assert data["known_meters"] == 10  # 只是下限
    assert data["mileage_complete"] is False
    assert data["is_lower_bound"] is True
    ranges = data["missing_ranges"]["C1"]
    assert ranges == [
        {"start_day": "2026-03-02", "end_day": "2026-03-04", "days": 2},
        {"start_day": "2026-03-05", "end_day": "2026-03-06", "days": 1},
    ]
    # 零米日（03-04）不能被缺报段覆盖：它只能作为前段的 end_day 边界，
    # 任何缺报段的 start_day 都不会落在 03-04
    assert all(x["start_day"] != "2026-03-04" for x in ranges)


def test_backfill_correction_completes_mileage(client):
    c, _ = client
    _batch(
        c,
        overhauls=[{"record_no": "OH-1", "wheelset_id": "W1", "overhaul_day": "2026-03-01"}],
        mounts=[{"record_no": "M-1", "wheelset_id": "W1", "car_id": "C1",
                 "start_day": "2026-03-01", "end_day": "2026-03-03"}],
        mileage=[{"record_no": "D-1", "car_id": "C1", "day": "2026-03-01", "meters": 10}],
    )
    # 先缺 03-02
    data = _query(c, "W1", "2026-03-02").json()
    assert data["is_lower_bound"] is True

    # 新记录补报缺报日
    r = _batch(c, mileage=[{"record_no": "D-2", "car_id": "C1",
                            "day": "2026-03-02", "meters": 40}])
    assert r.status_code == 200
    data = _query(c, "W1", "2026-03-02").json()
    assert data["known_meters"] == 50
    assert data["mileage_complete"] is True


def test_correction_by_record_no_updates_serving(client):
    c, _ = client
    _batch(
        c,
        overhauls=[{"record_no": "OH-1", "wheelset_id": "W1", "overhaul_day": "2026-04-01"}],
        mounts=[{"record_no": "M-1", "wheelset_id": "W1", "car_id": "C1",
                 "start_day": "2026-04-01", "end_day": "2026-04-03"}],
        mileage=[
            {"record_no": "D-1", "car_id": "C1", "day": "2026-04-01", "meters": 100},
            {"record_no": "D-2", "car_id": "C1", "day": "2026-04-02", "meters": 200},
        ],
    )
    assert _query(c, "W1", "2026-04-02").json()["known_meters"] == 300

    # 同记录号更正米数
    r = _batch(c, mileage=[{"record_no": "D-1", "car_id": "C1",
                            "day": "2026-04-01", "meters": 150}])
    body = r.json()
    assert body["corrected"]["mileage"] == 1
    assert body["corrected_record_nos"]["mileage"] == ["D-1"]
    assert _query(c, "W1", "2026-04-02").json()["known_meters"] == 350


def test_correction_moves_record_to_another_day_and_car(client):
    c, db_path = client
    _batch(
        c,
        overhauls=[{"record_no": "OH-1", "wheelset_id": "W1", "overhaul_day": "2026-05-01"}],
        mounts=[
            {"record_no": "M-1", "wheelset_id": "W1", "car_id": "C1",
             "start_day": "2026-05-01", "end_day": "2026-05-03"},
            {"record_no": "M-2", "wheelset_id": "W1", "car_id": "C2",
             "start_day": "2026-05-03", "end_day": "2026-05-05"},
        ],
        mileage=[{"record_no": "D-1", "car_id": "C1", "day": "2026-05-01", "meters": 90}],
    )
    # 更正到另一车厢、另一日
    r = _batch(c, mileage=[{"record_no": "D-1", "car_id": "C2",
                            "day": "2026-05-03", "meters": 80}])
    assert r.status_code == 200

    with sqlite3.connect(db_path) as raw:
        # 服务层日表与原始表一致，旧位置已摘除
        rows = raw.execute(
            "SELECT car_id, day, meters FROM car_mileage_day_serving ORDER BY car_id, day"
        ).fetchall()
        assert rows == [("C2", "2026-05-03", 80)]
        # 连续块随之迁移
        blocks = raw.execute(
            "SELECT car_id, start_day, end_day FROM car_mileage_block ORDER BY car_id"
        ).fetchall()
        assert blocks == [("C2", "2026-05-03", "2026-05-04")]
        # 缺报日不建档：没有 C1 的服务行
        assert raw.execute(
            "SELECT COUNT(*) FROM car_mileage_day_serving WHERE car_id = 'C1'"
        ).fetchone()[0] == 0


def test_overlapping_mounts_reject_whole_batch_and_keep_old_state(client):
    c, _ = client
    ok = _batch(
        c,
        mounts=[{"record_no": "M-1", "wheelset_id": "W1", "car_id": "C1",
                 "start_day": "2026-06-01", "end_day": "2026-06-05"}],
        mileage=[{"record_no": "D-1", "car_id": "C1", "day": "2026-06-01", "meters": 5}],
    )
    assert ok.status_code == 200

    # 同批含重叠装车段与一条新里程：整批退回
    r = _batch(
        c,
        mounts=[
            {"record_no": "M-2", "wheelset_id": "W1", "car_id": "C2",
             "start_day": "2026-06-04", "end_day": "2026-06-08"},
        ],
        mileage=[{"record_no": "D-2", "car_id": "C1", "day": "2026-06-02", "meters": 7}],
    )
    assert r.status_code == 409
    assert "重叠" in r.json()["detail"]

    # 旧供数结果保留：D-2 未生效
    r = _batch(c)  # 空批也能正常提交
    assert r.status_code == 200
    # 通过一次更正同号验证旧状态，并确认 06-02 仍缺报
    _batch(
        c,
        overhauls=[{"record_no": "OH-1", "wheelset_id": "W1", "overhaul_day": "2026-06-01"}],
    )
    data = _query(c, "W1", "2026-06-04").json()
    assert data["known_meters"] == 5
    flat = [x for rs in data["missing_ranges"].values() for x in rs]
    assert any(x["start_day"] == "2026-06-02" for x in flat)


def test_missing_baseline_overhaul_returns_422(client):
    c, _ = client
    _batch(
        c,
        mounts=[{"record_no": "M-1", "wheelset_id": "W9", "car_id": "C1",
                 "start_day": "2026-01-01", "end_day": "2026-01-05"}],
        mileage=[{"record_no": "D-1", "car_id": "C1", "day": "2026-01-01", "meters": 1}],
    )
    r = _query(c, "W9", "2026-01-04")
    assert r.status_code == 422
    assert "起算日" in r.json()["detail"]

    # 截止日之后的大修不能作为起算日
    _batch(c, overhauls=[{"record_no": "OH-9", "wheelset_id": "W9",
                          "overhaul_day": "2026-01-10"}])
    r = _query(c, "W9", "2026-01-04")
    assert r.status_code == 422


def test_second_overhaul_resets_baseline(client):
    c, _ = client
    _batch(
        c,
        overhauls=[
            {"record_no": "OH-1", "wheelset_id": "W1", "overhaul_day": "2026-07-01"},
            {"record_no": "OH-2", "wheelset_id": "W1", "overhaul_day": "2026-07-10"},
        ],
        mounts=[{"record_no": "M-1", "wheelset_id": "W1", "car_id": "C1",
                 "start_day": "2026-07-01", "end_day": "2026-07-12"}],
        mileage=[
            {"record_no": "D-1", "car_id": "C1", "day": "2026-07-01", "meters": 999},
            {"record_no": "D-2", "car_id": "C1", "day": "2026-07-10", "meters": 10},
            {"record_no": "D-3", "car_id": "C1", "day": "2026-07-11", "meters": 20},
        ],
    )
    data = _query(c, "W1", "2026-07-11").json()
    # 从最近一次大修 07-10 起算，07-01 的 999 不计入
    assert data["baseline_overhaul_day"] == "2026-07-10"
    assert data["known_meters"] == 30


def test_query_never_reads_raw_mileage_table(client, monkeypatch):
    c, db_path = client
    _batch(
        c,
        overhauls=[{"record_no": "OH-1", "wheelset_id": "W1", "overhaul_day": "2026-08-01"}],
        mounts=[{"record_no": "M-1", "wheelset_id": "W1", "car_id": "C1",
                 "start_day": "2026-08-01", "end_day": "2026-08-03"}],
        mileage=[
            {"record_no": "D-1", "car_id": "C1", "day": "2026-08-01", "meters": 10},
            {"record_no": "D-2", "car_id": "C1", "day": "2026-08-02", "meters": 20},
        ],
    )
    # 在当前进程注册 SQLite 授权回调：任何对原始日里程表的读取都直接拒绝，
    # 查询必须只走服务层（car_mileage_day_serving / car_mileage_block）
    def _authorizer(action, arg1, arg2, db_name, trigger):
        if action == sqlite3.SQLITE_READ and arg1 == "car_daily_mileage":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    # 授权回调是连接级的，TestClient 每次请求新建连接，因此用 monkeypatch 包裹 connect
    import app.database as database

    real_connect = database.connect
    _authorizer._keepalive = True  # 保持引用，防止回调被回收

    def _patched_connect(db_path=None):
        conn = real_connect(db_path)
        conn.set_authorizer(_authorizer)
        return conn

    monkeypatch.setattr(database, "connect", _patched_connect)
    monkeypatch.setattr("app.main.connect", _patched_connect)

    r = _query(c, "W1", "2026-08-02")
    assert r.status_code == 200, r.text
    assert r.json()["known_meters"] == 30


def test_invalid_payloads_are_rejected(client):
    c, _ = client
    # 装车段末日 <= 首日
    r = _batch(
        c,
        mounts=[{"record_no": "M-1", "wheelset_id": "W1", "car_id": "C1",
                 "start_day": "2026-01-05", "end_day": "2026-01-05"}],
    )
    assert r.status_code == 422

    # 负米数
    r = _batch(c, mileage=[{"record_no": "D-1", "car_id": "C1",
                            "day": "2026-01-05", "meters": -1}])
    assert r.status_code == 422

    # 同批重复记录号
    r = _batch(
        c,
        mileage=[
            {"record_no": "DUP", "car_id": "C1", "day": "2026-01-05", "meters": 1},
            {"record_no": "DUP", "car_id": "C1", "day": "2026-01-06", "meters": 2},
        ],
    )
    assert r.status_code == 422

    # 非法日期
    r = _query(c, "W1", "not-a-date")
    assert r.status_code == 422


def test_cutoff_before_overhaul_day_is_excluded_baseline(client):
    c, _ = client
    _batch(
        c,
        overhauls=[{"record_no": "OH-1", "wheelset_id": "W1", "overhaul_day": "2026-10-10"}],
        mounts=[{"record_no": "M-1", "wheelset_id": "W1", "car_id": "C1",
                 "start_day": "2026-10-01", "end_day": "2026-10-20"}],
        mileage=[{"record_no": "D-1", "car_id": "C1", "day": "2026-10-05", "meters": 100}],
    )
    # 截止日早于唯一一次大修：该大修不可用作起算日
    r = _query(c, "W1", "2026-10-09")
    assert r.status_code == 422


def test_missing_gap_across_two_mount_segments_same_car(client):
    c, _ = client
    _batch(
        c,
        overhauls=[{"record_no": "OH-1", "wheelset_id": "W1", "overhaul_day": "2026-11-01"}],
        mounts=[
            # 同一车厢两段，中间 11-03 轮对不在车上（不计缺报）
            {"record_no": "M-1", "wheelset_id": "W1", "car_id": "C1",
             "start_day": "2026-11-01", "end_day": "2026-11-03"},
            {"record_no": "M-2", "wheelset_id": "W1", "car_id": "C1",
             "start_day": "2026-11-04", "end_day": "2026-11-06"},
        ],
        mileage=[
            {"record_no": "D-1", "car_id": "C1", "day": "2026-11-01", "meters": 10},
            # 11-02 缺报；11-04 缺报；11-05 已报
            {"record_no": "D-2", "car_id": "C1", "day": "2026-11-05", "meters": 20},
        ],
    )
    data = _query(c, "W1", "2026-11-05").json()
    assert data["known_meters"] == 30
    assert data["is_lower_bound"] is True
    ranges = data["missing_ranges"]["C1"]
    # 两个缺报段被 11-03~11-04 的非装车空档隔开，不能合并
    assert ranges == [
        {"start_day": "2026-11-02", "end_day": "2026-11-03", "days": 1},
        {"start_day": "2026-11-04", "end_day": "2026-11-05", "days": 1},
    ]


def test_duplicate_car_day_conflict_rolls_back(client):
    c, _ = client
    _batch(
        c,
        mileage=[
            {"record_no": "D-1", "car_id": "C1", "day": "2026-09-01", "meters": 1},
            {"record_no": "D-2", "car_id": "C1", "day": "2026-09-02", "meters": 2},
        ],
    )
    # 不同记录号指向同一车厢-日：唯一约束冲突，整批回滚
    r = _batch(
        c,
        mileage=[{"record_no": "D-3", "car_id": "C1",
                  "day": "2026-09-02", "meters": 3}],
    )
    assert r.status_code == 409

    # 旧结果保留：D-2 仍是 2 米
    _batch(
        c,
        overhauls=[{"record_no": "OH-1", "wheelset_id": "W1", "overhaul_day": "2026-09-01"}],
        mounts=[{"record_no": "M-1", "wheelset_id": "W1", "car_id": "C1",
                 "start_day": "2026-09-01", "end_day": "2026-09-03"}],
    )
    data = _query(c, "W1", "2026-09-02").json()
    assert data["known_meters"] == 3  # 1 + 2
    assert data["mileage_complete"] is True
