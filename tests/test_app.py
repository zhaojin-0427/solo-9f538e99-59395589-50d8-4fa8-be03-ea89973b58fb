"""关键场景自测：核心计算、修订规则、事务原子性、网页/API 一致性、重启持久化。

运行：python3 -m unittest discover -s tests -v
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import core, db, served  # noqa: E402
from app.app import create_app  # noqa: E402


def epoch(y, mo, d, h, mi=0):
    return int(datetime(y, mo, d, h, mi, tzinfo=timezone.utc).timestamp())


# 2026-11-17 09:00Z / 10:00Z / 11:00Z
T09, T10, T11 = epoch(2026, 11, 17, 9), epoch(2026, 11, 17, 10), epoch(2026, 11, 17, 11)


def csv_rows(station, device, start_epoch, counters, revision=1):
    """生成连续分钟的 CSV 行（无表头）。"""
    return [
        f"{station},{device},{core.iso_utc(start_epoch + i * 60)},{c},{revision}"
        for i, c in enumerate(counters)
    ]


class CoreParseTests(unittest.TestCase):
    def test_english_header(self):
        text = "station,device,minute,counter,revision\nS1,primary,2026-11-17T10:00:00Z,123,1\n"
        valid, errors = core.parse_csv_text(text)
        self.assertEqual(errors, [])
        self.assertEqual(len(valid), 1)
        r = valid[0][2]
        self.assertEqual((r.station, r.device, r.counter, r.revision), ("S1", "primary", 123, 1))
        self.assertEqual(r.minute_epoch, T10)

    def test_chinese_header_and_aliases(self):
        text = "站点,主备标记,UTC时间,计数,修订号\n站A,主,2026-11-17 10:00,5,2\n站A,备,2026-11-17 10:00,7,2\n"
        valid, errors = core.parse_csv_text(text)
        self.assertEqual(errors, [])
        self.assertEqual([v[2].device for v in valid], ["primary", "backup"])
        self.assertEqual(valid[0][2].minute_epoch, T10)  # 未带时区按 UTC

    def test_no_header(self):
        text = "S1,backup,2026-11-17T10:00Z,9999,3\n"
        valid, errors = core.parse_csv_text(text)
        self.assertEqual(errors, [])
        self.assertEqual(valid[0][2].counter, 9999)

    def test_timezone_offset_converted_to_utc(self):
        text = "S1,primary,2026-11-17T18:00:00+08:00,1,1\n"
        valid, _ = core.parse_csv_text(text)
        self.assertEqual(valid[0][2].minute_epoch, T10)

    def test_bad_rows_rejected_individually(self):
        text = "\n".join([
            "S1,primary,2026-11-17T10:00Z,100,1",      # ok
            "S1,primary,2026-11-17T10:01Z,10000,1",    # 计数超界
            "S1,primary,2026-11-17T10:02Z,-1,1",       # 计数为负
            "S1,primary,2026-11-17T10:03Z,abc,1",      # 计数非整数
            "S1,primary,2026-11-17T10:04:30Z,1,1",     # 秒非 00
            "S1,third,2026-11-17T10:05Z,1,1",          # 设备标记无法识别
            "S1,primary,2026-11-17T10:06Z,1,-2",       # 修订号为负
            "S1,primary,2026-11-17T10:07Z,1",          # 列数不足
            "S1,primary,2026-11-17T10:08Z,1,1,EXTRA",  # 多余数据列
            ",primary,2026-11-17T10:09Z,1,1",          # 站点为空
        ])
        valid, errors = core.parse_csv_text(text)
        self.assertEqual(len(valid), 1)
        self.assertEqual(len(errors), 9)
        self.assertEqual([e.row_no for e in errors], list(range(2, 11)))

    def test_file_level_errors(self):
        with self.assertRaises(core.CsvFormatError):
            core.parse_csv_text("")
        with self.assertRaises(core.CsvFormatError):
            core.parse_csv_text("station,device,minute,counter\n")  # 表头缺修订号列
        with self.assertRaises(core.CsvFormatError):
            core.parse_csv_text("station,device,minute,counter,revision\n")  # 只有表头

    def test_trailing_comma_tolerated(self):
        valid, errors = core.parse_csv_text("S1,primary,2026-11-17T10:00Z,1,1,\n")
        self.assertEqual(errors, [])
        self.assertEqual(len(valid), 1)

    def test_parse_hour_alignment(self):
        self.assertEqual(core.parse_hour("2026-11-17T10:00Z"), T10)
        self.assertEqual(core.parse_hour("2026-11-17 10:00"), T10)
        with self.assertRaises(ValueError):
            core.parse_hour("2026-11-17T10:30Z")


class CoreComputeTests(unittest.TestCase):
    def test_counter_delta_wraparound(self):
        self.assertEqual(core.counter_delta(9998, 3), 5)   # 下降按一次 10000 回绕
        self.assertEqual(core.counter_delta(100, 250), 150)
        self.assertEqual(core.counter_delta(7, 7), 0)

    def test_full_primary_hour(self):
        counters = {("primary", T10 + i * 60): 1000 + i for i in range(61)}
        r = core.compute_hour(counters, "S1", T10)
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["total_pulses"], 60)
        self.assertEqual(r["rainfall_mm"], 12.0)  # 60 × 0.2
        self.assertEqual(r["missing_intervals"], 0)
        self.assertTrue(all(i["source"] == "primary" for i in r["intervals"]))

    def test_backup_fallback_and_no_cross_device(self):
        counters = {("primary", T10 + i * 60): 100 + i for i in range(61)}
        # 删掉主计 10:30，并放备计 10:30/10:31
        del counters[("primary", T10 + 30 * 60)]
        counters[("backup", T10 + 30 * 60)] = 500
        counters[("backup", T10 + 31 * 60)] = 504
        r = core.compute_hour(counters, "S1", T10)
        self.assertEqual(r["status"], "incomplete")
        self.assertIsNone(r["rainfall_mm"])
        # 第 29 段（10:29~10:30）：主计缺 10:30、备计缺 10:29，跨设备不可减 -> 缺失
        self.assertEqual(r["intervals"][29]["source"], "missing")
        # 第 30 段（10:30~10:31）：备计两端齐全 -> 备计差值
        self.assertEqual(r["intervals"][30]["source"], "backup")
        self.assertEqual(r["intervals"][30]["pulses"], 4)
        # 第 31 段（10:31~10:32）：主计齐全 -> 主计
        self.assertEqual(r["intervals"][31]["source"], "primary")
        self.assertEqual(r["missing_intervals"], 1)
        self.assertEqual(r["source_counts"]["backup"], 1)

    def test_missing_interval_makes_hour_unavailable(self):
        counters = {("primary", T10 + i * 60): i for i in range(61)}
        del counters[("primary", T10 + 45 * 60)]
        r = core.compute_hour(counters, "S1", T10)
        self.assertEqual(r["status"], "incomplete")
        self.assertIsNone(r["rainfall_mm"])
        self.assertIsNone(r["total_pulses"])
        self.assertEqual(r["missing_intervals"], 2)  # 44~45 与 45~46 两段都缺端点

    def test_wraparound_in_interval(self):
        # 计数器在 10:00~10:01 间从 9998 回绕到 3 -> 5 脉冲，其余分钟不变
        counters = {("primary", T10 + i * 60): 3 for i in range(61)}
        counters[("primary", T10)] = 9998
        r = core.compute_hour(counters, "S1", T10)
        self.assertEqual(r["intervals"][0]["pulses"], 5)
        self.assertEqual(r["rainfall_mm"], 1.0)

    def test_hour_must_align(self):
        with self.assertRaises(ValueError):
            core.compute_hour({}, "S1", T10 + 60)


class DbTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "t.db")
        self.conn = db.connect(self.path)
        db.init_db(self.conn)

    def tearDown(self):
        self.conn.close()

    def _reading(self, minute, counter, rev, device="primary", station="S1"):
        return core.Reading(station, device, minute, counter, rev)

    def test_revision_rule(self):
        rows = [(1, "raw", self._reading(T10, 100, 1))]
        rep = db.apply_batch(self.conn, rows, [], "a.csv")
        self.assertEqual(rep["accepted_count"], 1)

        # 同修订号、低修订号都被拒绝
        rep = db.apply_batch(self.conn, [(1, "raw", self._reading(T10, 200, 1))], [], "b.csv")
        self.assertEqual(rep["accepted_count"], 0)
        self.assertEqual(rep["rejected_count"], 1)
        rep = db.apply_batch(self.conn, [(1, "raw", self._reading(T10, 300, 0))], [], "c.csv")
        self.assertEqual(rep["accepted_count"], 0)

        # 更高修订号被接受并覆盖
        rep = db.apply_batch(self.conn, [(1, "raw", self._reading(T10, 400, 2))], [], "d.csv")
        self.assertEqual(rep["accepted_count"], 1)
        self.assertEqual(rep["accepted"][0]["action"], "updated")
        got = db.fetch_hour_counters(self.conn, "S1", T10)
        self.assertEqual(got[("primary", T10)], 400)

    def test_same_key_within_one_batch(self):
        rows = [
            (1, "r1", self._reading(T10, 100, 1)),
            (2, "r2", self._reading(T10, 150, 2)),  # 更高修订，覆盖同批前一行
            (3, "r3", self._reading(T10, 120, 2)),  # 同修订号，拒绝
        ]
        rep = db.apply_batch(self.conn, rows, [], "e.csv")
        self.assertEqual(rep["accepted_count"], 2)
        self.assertEqual(rep["rejected_count"], 1)
        got = db.fetch_hour_counters(self.conn, "S1", T10)
        self.assertEqual(got[("primary", T10)], 150)

    def test_atomic_rollback_no_partial_batch(self):
        # 第二行设备标记非法（绕过解析层直接构造），触发 CHECK 约束失败
        bad = core.Reading("S1", "tertiary", T10 + 60, 5, 1)
        rows = [(1, "ok", self._reading(T10, 100, 1)), (2, "bad", bad)]
        with self.assertRaises(sqlite3.Error):
            db.apply_batch(self.conn, rows, [], "f.csv")
        # 回滚：读数与批次记录都不留
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM batch_rows").fetchone()[0], 0)

    def test_persistence_across_reopen(self):
        rows = [(1, "raw", self._reading(T10, 100, 1))]
        db.apply_batch(self.conn, rows, [], "g.csv")
        self.conn.close()
        conn2 = db.connect(self.path)
        try:
            got = db.fetch_hour_counters(conn2, "S1", T10)
            self.assertEqual(got[("primary", T10)], 100)
        finally:
            conn2.close()
        self.conn = db.connect(self.path)  # tearDown 用


class AppTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "app.db")
        self.app = create_app(self.path)
        self.client = self.app.test_client()

    def _upload(self, text):
        return self.client.post("/api/upload", data={"csv_text": text})

    def _hourly(self, station, hour_text):
        resp = self.client.get(f"/api/hourly?station={station}&hour={hour_text}")
        self.assertEqual(resp.status_code, 200)
        return resp.get_json()

    def test_upload_and_query_flow(self):
        text = "\n".join(csv_rows("S1", "primary", T10, [1000 + i for i in range(61)]))
        resp = self._upload(text)
        self.assertEqual(resp.status_code, 200)
        rep = resp.get_json()
        self.assertEqual(rep["accepted_count"], 61)
        self.assertEqual(rep["rejected_count"], 0)

        r = self._hourly("S1", "2026-11-17T10:00Z")
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["rainfall_mm"], 12.0)

        # 网页与 API 一致
        html = self.client.get("/report?station=S1&hour=2026-11-17T10:00Z").get_data(as_text=True)
        self.assertIn("12.0 mm", html)
        self.assertIn("可供数", html)

    def test_boundary_revision_changes_both_hours_consistently(self):
        # 09:00~11:00 共 121 个主计读数，每分钟 +10 脉冲
        text = "\n".join(csv_rows("S1", "primary", T09, [1000 + 10 * i for i in range(121)]))
        self.assertEqual(self._upload(text).get_json()["accepted_count"], 121)
        self.assertEqual(self._hourly("S1", "2026-11-17T09:00Z")["rainfall_mm"], 120.0)
        self.assertEqual(self._hourly("S1", "2026-11-17T10:00Z")["rainfall_mm"], 120.0)

        # 修订整点 10:00 读数：1600 -> 1595（修订号 2），同时影响前后两小时
        rev = f"S1,primary,{core.iso_utc(T10)},1595,2"
        rep = self._upload(rev).get_json()
        self.assertEqual(rep["accepted_count"], 1)
        self.assertEqual(rep["accepted"][0]["action"], "updated")

        r09 = self._hourly("S1", "2026-11-17T09:00Z")
        r10 = self._hourly("S1", "2026-11-17T10:00Z")
        self.assertEqual(r09["rainfall_mm"], 119.0)  # 末段少 5 脉冲
        self.assertEqual(r10["rainfall_mm"], 121.0)  # 首段多 5 脉冲

        # 网页与 API 对两小时都一致
        h09 = self.client.get("/report?station=S1&hour=2026-11-17T09:00Z").get_data(as_text=True)
        h10 = self.client.get("/report?station=S1&hour=2026-11-17T10:00Z").get_data(as_text=True)
        self.assertIn("119.0 mm", h09)
        self.assertIn("121.0 mm", h10)

    def test_stale_revision_rejected_and_data_unchanged(self):
        text = "\n".join(csv_rows("S1", "primary", T10, [1000 + i for i in range(61)], revision=2))
        self._upload(text)
        before = self._hourly("S1", "2026-11-17T10:00Z")

        stale = f"S1,primary,{core.iso_utc(T10)},5555,1"   # 低修订
        same = f"S1,primary,{core.iso_utc(T10 + 60)},5555,2"  # 同修订
        rep = self._upload("\n".join([stale, same])).get_json()
        self.assertEqual(rep["accepted_count"], 0)
        self.assertEqual(rep["rejected_count"], 2)
        after = self._hourly("S1", "2026-11-17T10:00Z")
        self.assertEqual(before, after)

    def test_incomplete_hour_and_gap_display(self):
        # 缺 10:45 一个端点 -> 两段缺失，整小时不可供数
        counters = [1000 + i for i in range(61)]
        lines = csv_rows("S1", "primary", T10, counters)
        del lines[45]
        self._upload("\n".join(lines))
        r = self._hourly("S1", "2026-11-17T10:00Z")
        self.assertEqual(r["status"], "incomplete")
        self.assertIsNone(r["rainfall_mm"])
        self.assertEqual(r["missing_intervals"], 2)
        html = self.client.get("/report?station=S1&hour=2026-11-17T10:00Z").get_data(as_text=True)
        self.assertIn("不可供数", html)
        self.assertIn("缺口 2 / 60", html)

    def test_bad_file_leaves_nothing(self):
        resp = self._upload("station,device,minute,counter\n")  # 表头缺列
        self.assertEqual(resp.status_code, 400)
        resp = self.client.post("/api/upload", data={})  # 空上传
        self.assertEqual(resp.status_code, 400)
        batches = self.client.get("/api/batches").get_json()
        self.assertEqual(batches, [])

    def test_partial_rows_atomic_report(self):
        good = csv_rows("S1", "primary", T10, [1000 + i for i in range(61)])
        good[10] = "S1,primary,not-a-time,1,1"  # 一行坏行
        rep = self._upload("\n".join(good)).get_json()
        self.assertEqual(rep["accepted_count"], 60)
        self.assertEqual(rep["rejected_count"], 1)
        self.assertIn("时间", rep["rejected"][0]["reason"])
        # 缺 10:10 端点 -> 不可供数
        r = self._hourly("S1", "2026-11-17T10:00Z")
        self.assertEqual(r["status"], "incomplete")

    def test_restart_persistence(self):
        text = "\n".join(csv_rows("S1", "primary", T10, [1000 + i for i in range(61)]))
        self._upload(text)
        # 模拟重启：同一数据库文件新建应用实例
        app2 = create_app(self.path)
        client2 = app2.test_client()
        r = client2.get("/api/hourly?station=S1&hour=2026-11-17T10:00Z").get_json()
        self.assertEqual(r["rainfall_mm"], 12.0)

    def test_health_and_stations(self):
        self.assertEqual(self.client.get("/api/health").get_json()["status"], "ok")
        text = "\n".join(csv_rows("S1", "backup", T10, [i for i in range(61)]))
        self._upload(text)
        stations = self.client.get("/api/stations").get_json()
        self.assertEqual(stations[0]["station"], "S1")
        self.assertEqual(stations[0]["readings"], 61)

    def test_hour_param_validation(self):
        resp = self.client.get("/api/hourly?station=S1&hour=2026-11-17T10:30Z")
        self.assertEqual(resp.status_code, 400)
        resp = self.client.get("/api/hourly?hour=2026-11-17T10:00Z")
        self.assertEqual(resp.status_code, 400)


class ServedFlowTests(unittest.TestCase):
    """已供数登记、变化复核清单、确认的并发校验与一致性。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "served.db")
        self.app = create_app(self.path)
        self.client = self.app.test_client()

    def _upload(self, lines):
        return self.client.post("/api/upload", data={"csv_text": "\n".join(lines)})

    def _full_hour(self, station="S1", start=T10, base=1000, step=1, revision=1):
        return csv_rows(station, "primary", start, [base + step * i for i in range(61)],
                        revision=revision)

    def _register(self, station="S1", hour="2026-11-17T10:00Z"):
        resp = self.client.post("/api/served", json={"station": station, "hour": hour})
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        return resp.get_json()

    def _reviews(self, station=None):
        url = "/api/reviews" + (f"?station={station}" if station else "")
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        return resp.get_json()

    def test_register_keeps_original_rainfall(self):
        self._upload(self._full_hour())  # 60 脉冲 = 12.0 mm
        state = self._register()
        self.assertEqual(state["registered_rainfall_mm"], 12.0)
        self.assertEqual(state["baseline_rainfall_mm"], 12.0)
        self.assertFalse(state["needs_review"])
        self.assertEqual(self._reviews(), [])

        # 修订本小时收口端点 11:00：1055 -> 1060（末段 +5 脉冲），雨量变为 13.0 mm
        self._upload([f"S1,primary,{core.iso_utc(T10 + 3600)},1065,2"])
        got = self.client.get(f"/api/served/{state['served_id']}").get_json()
        # 登记值永久保留，基线仍是 12.0，现值 13.0
        self.assertEqual(got["registered_rainfall_mm"], 12.0)
        self.assertEqual(got["baseline_rainfall_mm"], 12.0)
        self.assertEqual(got["current_rainfall_mm"], 13.0)
        self.assertTrue(got["needs_review"])

        reviews = self._reviews()
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0]["baseline_rainfall_mm"], 12.0)
        self.assertEqual(reviews[0]["current_rainfall_mm"], 13.0)

    def test_register_rejects_incomplete_or_unaligned(self):
        lines = self._full_hour()
        del lines[10]
        self._upload(lines)
        resp = self.client.post(
            "/api/served", json={"station": "S1", "hour": "2026-11-17T10:00Z"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("不可供数", resp.get_json()["error"])

        resp = self.client.post(
            "/api/served", json={"station": "S1", "hour": "2026-11-17T10:30Z"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self._reviews(), [])

    def test_register_is_idempotent_and_does_not_move_baseline(self):
        self._upload(self._full_hour())
        first = self._register()
        # 改变后再登记同一小时：不覆盖原登记、不清除待复核
        self._upload([f"S1,primary,{core.iso_utc(T10 + 3600)},1062,2"])
        resp = self.client.post(
            "/api/served", json={"station": "S1", "hour": "2026-11-17T10:00Z"})
        self.assertEqual(resp.status_code, 200)
        again = resp.get_json()
        self.assertFalse(again["created"])
        self.assertEqual(again["served_id"], first["served_id"])
        self.assertEqual(again["registered_rainfall_mm"], 12.0)
        self.assertTrue(again["needs_review"])
        self.assertEqual(len(self._reviews()), 1)

    def test_confirm_moves_baseline_and_clears_review(self):
        self._upload(self._full_hour())
        state = self._register()
        self._upload([f"S1,primary,{core.iso_utc(T10 + 3600)},1065,2"])

        pending = self.client.get(f"/api/served/{state['served_id']}").get_json()
        self.assertTrue(pending["needs_review"])
        resp = self.client.post(
            f"/api/served/{state['served_id']}/confirm",
            json={
                "expected_version": pending["current_version"],
                "expected_rainfall_mm": pending["current_rainfall_mm"],
            },
        )
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        done = resp.get_json()
        self.assertFalse(done["needs_review"])
        self.assertEqual(done["baseline_rainfall_mm"], 13.0)
        self.assertEqual(done["registered_rainfall_mm"], 12.0)  # 登记原值仍保留
        self.assertEqual(self._reviews(), [])

        # 再次上传改变同一小时 -> 重新进入待复核
        self._upload([f"S1,primary,{core.iso_utc(T10 + 3600)},1066,3"])
        self.assertEqual(len(self._reviews()), 1)
        self.assertEqual(self._reviews()[0]["baseline_rainfall_mm"], 13.0)

    def test_confirm_rejected_when_upload_interleaves(self):
        self._upload(self._full_hour())
        state = self._register()
        self._upload([f"S1,primary,{core.iso_utc(T10 + 3600)},1065,2"])

        # 值班员页面看到 13.0
        page = self.client.get(f"/api/served/{state['served_id']}").get_json()
        self.assertEqual(page["current_rainfall_mm"], 13.0)

        # 点击确认之前又有上传改变该小时（版本 3，现值 13.2）
        self._upload([f"S1,primary,{core.iso_utc(T10 + 3600)},1066,3"])

        resp = self.client.post(
            f"/api/served/{state['served_id']}/confirm",
            json={"expected_version": page["current_version"],
                  "expected_rainfall_mm": page["current_rainfall_mm"]},
        )
        self.assertEqual(resp.status_code, 409)
        still = self.client.get(f"/api/served/{state['served_id']}").get_json()
        self.assertTrue(still["needs_review"])
        self.assertEqual(still["baseline_rainfall_mm"], 12.0)  # 基线未被推进
        self.assertEqual(still["current_rainfall_mm"], 13.2)
        self.assertEqual([r["served_id"] for r in self._reviews()], [state["served_id"]])

        # 按刷新后的值重新核对即可确认
        resp = self.client.post(
            f"/api/served/{state['served_id']}/confirm",
            json={"expected_version": still["current_version"],
                  "expected_rainfall_mm": still["current_rainfall_mm"]},
        )
        self.assertEqual(resp.status_code, 200)

    def test_confirm_rejected_with_wrong_rainfall_or_no_change(self):
        self._upload(self._full_hour())
        state = self._register()
        # 未变化：无需确认
        resp = self.client.post(
            f"/api/served/{state['served_id']}/confirm",
            json={"expected_version": state["current_version"], "expected_rainfall_mm": 12.0})
        self.assertEqual(resp.status_code, 409)

        self._upload([f"S1,primary,{core.iso_utc(T10 + 3600)},1065,2"])
        pending = self.client.get(f"/api/served/{state['served_id']}").get_json()
        # 版本匹配但页面雨量不是当前值 -> 拒绝
        resp = self.client.post(
            f"/api/served/{state['served_id']}/confirm",
            json={"expected_version": pending["current_version"],
                  "expected_rainfall_mm": 12.0})
        self.assertEqual(resp.status_code, 409)
        self.assertTrue(self.client.get(
            f"/api/served/{state['served_id']}").get_json()["needs_review"])

        # 缺参数 -> 400
        resp = self.client.post(f"/api/served/{state['served_id']}/confirm", json={})
        self.assertEqual(resp.status_code, 400)

    def test_incomplete_hour_cannot_be_registered(self):
        lines = self._full_hour()
        del lines[30]
        self._upload(lines)
        resp = self.client.post(
            "/api/served", json={"station": "S1", "hour": "2026-11-17T10:00Z"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("不可供数", resp.get_json()["error"])
        self.assertEqual(self.client.get("/api/served").get_json(), [])

    def test_boundary_revision_marks_both_hours(self):
        # 09:00~11:00，每分钟 +10 脉冲：09、10 两小时各 120.0 mm
        self._upload(csv_rows("S1", "primary", T09, [1000 + 10 * i for i in range(121)]))
        s09 = self._register(hour="2026-11-17T09:00Z")
        s10 = self._register(hour="2026-11-17T10:00Z")
        self.assertEqual((s09["registered_rainfall_mm"], s10["registered_rainfall_mm"]),
                         (120.0, 120.0))
        self.assertEqual(self._reviews(), [])

        # 修订整点 10:00：1600 -> 1595（-5 脉冲）
        self._upload([f"S1,primary,{core.iso_utc(T10)},1595,2"])

        reviews = {r["hour"]: r for r in self._reviews()}
        self.assertEqual(set(reviews),
                         {"2026-11-17T09:00:00Z", "2026-11-17T10:00:00Z"})
        self.assertEqual(reviews["2026-11-17T09:00:00Z"]["current_rainfall_mm"], 119.0)
        self.assertEqual(reviews["2026-11-17T10:00:00Z"]["current_rainfall_mm"], 121.0)
        self.assertEqual(reviews["2026-11-17T09:00:00Z"]["baseline_rainfall_mm"], 120.0)

        # 网页与 API 对两小时给出同一结果
        for hour, mm in (("09:00", "119.0"), ("10:00", "121.0")):
            api = self.client.get(
                f"/api/hourly?station=S1&hour=2026-11-17T{hour}Z").get_json()
            self.assertEqual(api["rainfall_mm"], float(mm))
            html = self.client.get(
                f"/report?station=S1&hour=2026-11-17T{hour}Z").get_data(as_text=True)
            self.assertIn(f"{mm} mm", html)
            self.assertIn("待复核", html)

    def test_failed_upload_leaves_no_half_result(self):
        self._upload(self._full_hour())
        state = self._register()
        version_before = self.client.get(
            f"/api/served/{state['served_id']}").get_json()["current_version"]
        before = self.client.get(
            "/api/hourly?station=S1&hour=2026-11-17T10:00Z").get_json()

        # 文件级错误：整体拒收
        resp = self._upload(["station,device,minute,counter"])  # 表头缺列
        self.assertEqual(resp.status_code, 400)
        # 空上传
        resp = self.client.post("/api/upload", data={})
        self.assertEqual(resp.status_code, 400)

        after = self.client.get(
            "/api/hourly?station=S1&hour=2026-11-17T10:00Z").get_json()
        self.assertEqual(before, after)
        got = self.client.get(f"/api/served/{state['served_id']}").get_json()
        self.assertEqual(got["current_version"], version_before)
        self.assertFalse(got["needs_review"])

    def test_restart_keeps_review_list_and_web_api_agree(self):
        self._upload(self._full_hour())
        state = self._register()
        self._upload([f"S1,primary,{core.iso_utc(T10 + 3600)},1065,2"])

        # 模拟重启：同一数据库文件新应用实例
        app2 = create_app(self.path)
        client2 = app2.test_client()
        api = client2.get("/api/reviews").get_json()
        page = client2.get("/reviews").get_data(as_text=True)
        self.assertEqual(len(api), 1)
        self.assertIn("13.0", page)
        self.assertIn("12.0", page)
        self.assertIn(state["station"], page)

        detail = client2.get(f"/served/{state['served_id']}").get_data(as_text=True)
        self.assertIn("确认页面所见雨量 13.0 mm", detail)

        # 网页确认表单与 API 使用同一版本号，表单提交后清单清空
        resp = client2.post(
            f"/served/{state['served_id']}/confirm",
            data={"expected_version": api[0]["current_version"],
                  "expected_rainfall": api[0]["current_rainfall_mm"]},
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("已确认", resp.get_data(as_text=True))
        self.assertEqual(client2.get("/api/reviews").get_json(), [])

    def test_unknown_served_and_text_registration(self):
        resp = self.client.post(
            "/api/served/999/confirm",
            json={"expected_version": 1, "expected_rainfall_mm": 1.0})
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(self.client.get("/api/served/999").status_code, 404)

        self._upload(self._full_hour())
        # 表单（非 JSON）注册
        resp = self.client.post(
            "/api/served", data={"station": "S1", "hour": "2026-11-17 10:00"})
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        self.assertEqual(resp.get_json()["registered_rainfall_mm"], 12.0)

    def test_confirm_rejected_when_readings_change_even_if_total_unchanged(self):
        """确认前又上传改变了该小时数据（即使雨量合计不变）也必须拒绝。"""
        self._upload(self._full_hour())
        state = self._register()
        self._upload([f"S1,primary,{core.iso_utc(T10 + 3600)},1065,2"])  # 12.0 -> 13.0
        pending = self.client.get(f"/api/served/{state['served_id']}").get_json()
        old_version = pending["current_version"]

        # 内部分钟 10:30 的计数由 1030 改为 1029：脉冲在相邻两段间转移，小时合计仍 13.0
        self._upload([f"S1,primary,{core.iso_utc(T10 + 30 * 60)},1029,2"])
        now = self.client.get(f"/api/served/{state['served_id']}").get_json()
        self.assertEqual(now["current_rainfall_mm"], 13.0)
        self.assertNotEqual(now["current_version"], old_version)
        self.assertTrue(now["needs_review"])

        resp = self.client.post(
            f"/api/served/{state['served_id']}/confirm",
            json={"expected_version": old_version,
                  "expected_rainfall_mm": now["current_rainfall_mm"]})
        self.assertEqual(resp.status_code, 409)
        self.assertTrue(self.client.get(
            f"/api/served/{state['served_id']}").get_json()["needs_review"])

        # 刷新后按新版本确认成功
        resp = self.client.post(
            f"/api/served/{state['served_id']}/confirm",
            json={"expected_version": now["current_version"],
                  "expected_rainfall_mm": now["current_rainfall_mm"]})
        self.assertEqual(resp.status_code, 200)

    def test_inner_revision_with_same_total_does_not_enter_review(self):
        """未改变小时雨量的修订不进入待复核清单。"""
        self._upload(self._full_hour())
        state = self._register()
        # 内部点平移：一段 -1、下一段 +1，合计不变
        self._upload([f"S1,primary,{core.iso_utc(T10 + 30 * 60)},1029,2"])
        got = self.client.get(f"/api/served/{state['served_id']}").get_json()
        self.assertEqual(got["current_rainfall_mm"], 12.0)
        self.assertFalse(got["needs_review"])
        self.assertEqual(self._reviews(), [])
        # 但数据版本已变化，可用于拒绝“确认期间被改动”
        self.assertGreater(got["current_version"], got["baseline_version"])

    def test_upload_response_lists_changed_served_hours(self):
        self._upload(self._full_hour())
        state = self._register()
        rep = self._upload([f"S1,primary,{core.iso_utc(T10 + 3600)},1065,2"]).get_json()
        changed = rep["changed_served_hours"]
        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0]["served_id"], state["served_id"])
        self.assertEqual(changed[0]["baseline_rainfall_mm"], 12.0)
        self.assertEqual(changed[0]["current_rainfall_mm"], 13.0)

        # 未改变雨量的上传不列出（内部分钟平移，合计不变）
        rep = self._upload([f"S1,primary,{core.iso_utc(T10 + 30 * 60)},1029,2"]).get_json()
        self.assertEqual(rep["changed_served_hours"], [])

        # 网页上传报告同样列出（再次改变收口端点）
        html = self.client.post(
            "/upload", data={"csv_text": f"S1,primary,{core.iso_utc(T10 + 3600)},1066,3"}
        ).get_data(as_text=True)
        self.assertIn("本批上传改变了以下已供数小时", html)
        self.assertIn("13.2", html)

    def test_report_page_offers_registration_for_unregistered_hour(self):
        self._upload(self._full_hour())
        html = self.client.get(
            "/report?station=S1&hour=2026-11-17T10:00Z").get_data(as_text=True)
        self.assertIn("把本小时登记为已供数", html)

    def test_web_register_and_review_pages(self):
        self._upload(self._full_hour())
        resp = self.client.post(
            "/served", data={"station": "S1", "hour": "2026-11-17T10:00Z"},
            follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("已登记", html)
        self.assertIn("12.0 mm", html)

        self._upload([f"S1,primary,{core.iso_utc(T10 + 3600)},1065,2"])
        home = self.client.get("/").get_data(as_text=True)
        self.assertIn("待复核的已供数小时（1）", home)
        listing = self.client.get("/served").get_data(as_text=True)
        self.assertIn("待复核", listing)
        reviews = self.client.get("/reviews").get_data(as_text=True)
        self.assertIn("13.0", reviews)


class ServedConcurrencyTests(unittest.TestCase):
    """确认事务与上传事务交错时不得留下不一致结果。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "conc.db")
        conn = db.connect(self.path)
        db.init_db(conn)
        conn.close()

    def _apply(self, conn, minute, counter, revision, station="S1"):
        r = core.Reading(station, "primary", minute, counter, revision)
        return db.apply_batch(conn, [(1, "raw", r)], [], "x.csv")

    def test_confirm_rolls_back_when_readings_change_midway(self):
        conn = db.connect(self.path)
        db.apply_batch(
            conn,
            [(i + 1, "raw", core.Reading("S1", "primary", T10 + i * 60, 1000 + i, 1))
             for i in range(61)],
            [], "full.csv")
        state, _ = served.register_served(conn, "S1", T10)
        self._apply(conn, T10 + 3600, 1065, 2)

        pending = served.get_served(conn, state["served_id"])
        view_version = pending["current_version"]

        # 交错：另一个连接在确认前提交了改变 -> 确认按旧版本号提交
        other = db.connect(self.path)
        try:
            self._apply(other, T10 + 3600, 1066, 3)
        finally:
            other.close()
        with self.assertRaises(served.ServedError):
            served.confirm_served(
                conn, state["served_id"], view_version,
                pending["current_rainfall_mm"])
        after = served.get_served(conn, state["served_id"])
        self.assertTrue(after["needs_review"])
        self.assertEqual(after["baseline_rainfall_mm"], 12.0)
        conn.close()

    def test_version_bumped_only_for_affected_hours(self):
        conn = db.connect(self.path)
        self._apply(conn, T10, 100, 1)          # 整点 10:00 -> 影响 09 与 10 时
        self.assertEqual(db.hour_version(conn, "S1", T09), 1)
        self.assertEqual(db.hour_version(conn, "S1", T10), 1)
        self.assertEqual(db.hour_version(conn, "S1", T11), 0)

        self._apply(conn, T10 + 30 * 60, 130, 1)  # 非整点 -> 仅影响 10 时
        self.assertEqual(db.hour_version(conn, "S1", T09), 1)
        self.assertEqual(db.hour_version(conn, "S1", T10), 2)

        # 同值修订（计数未变）不算数据变化
        self._apply(conn, T10 + 30 * 60, 130, 2)
        self.assertEqual(db.hour_version(conn, "S1", T10), 2)
        conn.close()

    def test_confirm_and_upload_transactions_are_serialized(self):
        """确认事务持写锁期间，另一连接的上传必须等待而不是读到中间态。"""
        conn = db.connect(self.path)
        db.apply_batch(
            conn,
            [(i + 1, "raw", core.Reading("S1", "primary", T10 + i * 60, 1000 + i, 1))
             for i in range(61)],
            [], "full.csv")
        state, _ = served.register_served(conn, "S1", T10)
        self._apply(conn, T10 + 3600, 1065, 2)
        pending = served.get_served(conn, state["served_id"])

        other = db.connect(self.path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            # 模拟确认事务进行中；另一连接写入应被锁阻塞（短 busy_timeout）
            other.execute("PRAGMA busy_timeout=100")
            with self.assertRaises(sqlite3.OperationalError):
                other.execute(
                    "UPDATE readings SET counter=0 WHERE station='S1' AND minute_epoch=?",
                    (T10,),
                )
            conn.execute("ROLLBACK")
        finally:
            other.close()

        # 回滚后数据与待复核状态保持一致
        self.assertEqual(
            served.confirm_served(
                conn, state["served_id"], pending["current_version"],
                pending["current_rainfall_mm"])["baseline_rainfall_mm"],
            13.0,
        )
        conn.close()


if __name__ == "__main__":
    unittest.main()