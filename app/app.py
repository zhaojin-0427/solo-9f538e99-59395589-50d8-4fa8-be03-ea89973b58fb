"""Flask 网页与查询 API。

网页展示与 API 使用同一个 compute_hour_result()，保证两边结果一致；
小时雨量由 readings 表现算现算，补传/修订整点读数提交后，
前后两小时立即同时反映新值，不存在缓存不一致。
已供数登记、待复核清单与确认也走同一 served 服务层，网页与 API 同源。
"""
from __future__ import annotations

import os

from flask import (
    Flask, flash, g, jsonify, redirect, render_template, request, url_for,
)

from . import core, db, served


def create_app(db_path: str | None = None) -> Flask:
    app = Flask(__name__)
    app.config["DB_PATH"] = db_path or os.environ.get("RAIN_DB", "rainfall.db")
    app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 上传上限 16MB
    app.secret_key = os.environ.get("SECRET_KEY", os.urandom(32))

    # 启动时建库建表（幂等），保证重启后可直接查询
    conn = db.connect(app.config["DB_PATH"])
    try:
        db.init_db(conn)
    finally:
        conn.close()

    def get_conn() -> db.sqlite3.Connection:
        if "db_conn" not in g:
            g.db_conn = db.connect(app.config["DB_PATH"])
        return g.db_conn

    @app.teardown_appcontext
    def _close_conn(exc):
        conn = g.pop("db_conn", None)
        if conn is not None:
            conn.close()

    @app.template_filter("iso")
    def _iso_filter(epoch):
        return core.iso_utc(int(epoch))

    def compute_hour_result(station: str, hour_epoch: int) -> dict:
        """网页与 API 共用的小时计算入口。"""
        return served.compute_hour_result(get_conn(), station, hour_epoch)

    def read_upload_text():
        """从 multipart 文件或文本框读取 CSV 文本，返回 (text, filename)。"""
        file = request.files.get("file")
        if file is not None and file.filename:
            try:
                return file.read().decode("utf-8-sig"), file.filename
            except UnicodeDecodeError:
                raise core.CsvFormatError("文件不是有效的 UTF-8 编码")
        text = request.form.get("csv_text", "")
        if text.strip():
            return text, None
        raise core.CsvFormatError("请选择 CSV 文件或粘贴 CSV 文本")

    # ---------------- 网页 ----------------

    @app.get("/")
    def index():
        conn = get_conn()
        served_items, reviews = served.summarize(conn)
        return render_template(
            "index.html",
            stations=db.list_stations(conn),
            batches=db.list_batches(conn),
            reviews=reviews,
            served_count=len(served_items),
        )

    @app.post("/upload")
    def upload_html():
        try:
            text, filename = read_upload_text()
            valid, errors = core.parse_csv_text(text)
        except core.CsvFormatError as exc:
            flash(f"上传失败：{exc}", "error")
            return redirect(url_for("index"))
        report = db.apply_batch(get_conn(), valid, errors, filename)
        reviews = served.reviews_for_hours(get_conn(), report["changed_served_hours"])
        return render_template("batch.html", report=report, reviews=reviews)

    @app.get("/report")
    def report_html():
        station = request.args.get("station", "").strip()
        hour_text = request.args.get("hour", "").strip()
        if not station or not hour_text:
            flash("请填写站点和整点小时", "error")
            return redirect(url_for("index"))
        try:
            hour = core.parse_hour(hour_text)
        except ValueError as exc:
            flash(f"小时参数无效：{exc}", "error")
            return redirect(url_for("index"))
        conn = get_conn()
        # 单页内的雨量、登记读取自同一快照，避免页面上新旧值混杂
        with db.read_snapshot(conn):
            result = compute_hour_result(station, hour)
            row = conn.execute(
                "SELECT * FROM served_hours WHERE station=? AND hour_epoch=?",
                (station, hour),
            ).fetchone()
            served_state = None
            if row is not None:
                served_state = served.build_state(conn, row, result)
        return render_template(
            "report.html",
            r=result,
            served_state=served_state,
            prev_hour=core.iso_utc(hour - core.HOUR_SECONDS),
            next_hour=core.iso_utc(hour + core.HOUR_SECONDS),
        )

    @app.post("/served")
    def register_served_html():
        station = request.form.get("station", "").strip()
        hour_text = request.form.get("hour", "").strip()
        try:
            hour = core.parse_hour(hour_text)
            state, created = served.register_served(get_conn(), station, hour)
        except ValueError as exc:
            flash(f"登记失败：{exc}", "error")
            return redirect(url_for("index"))
        if created:
            flash(
                f"已登记 {state['station']} @ {state['hour']}，"
                f"登记雨量 {state['registered_rainfall_mm']:.1f} mm",
                "info",
            )
        else:
            flash(
                f"该小时此前已登记（登记雨量 {state['registered_rainfall_mm']:.1f} mm），"
                "未覆盖原登记",
                "info",
            )
        return redirect(url_for("served_detail_html", served_id=state["served_id"]))

    @app.get("/served")
    def served_list_html():
        conn = get_conn()
        station = request.args.get("station", "").strip() or None
        items, reviews = served.summarize(conn, station)
        return render_template(
            "served_list.html",
            items=items,
            reviews=reviews,
            station=station or "",
        )

    @app.get("/served/<int:served_id>")
    def served_detail_html(served_id):
        conn = get_conn()
        with db.read_snapshot(conn):
            state = served.get_served(conn, served_id)
            events = served.list_events(conn, served_id) if state else []
        if state is None:
            flash("已供数登记不存在", "error")
            return redirect(url_for("served_list_html"))
        return render_template("served_detail.html", s=state, events=events)

    @app.post("/served/<int:served_id>/confirm")
    def confirm_served_html(served_id):
        try:
            expected_version = int(request.form.get("expected_version", ""))
            expected_rainfall = float(request.form.get("expected_rainfall", ""))
        except ValueError:
            flash("确认被拒绝：缺少页面所见雨量或数据版本号，请刷新页面后重试", "error")
            return redirect(url_for("served_detail_html", served_id=served_id))
        try:
            state = served.confirm_served(
                get_conn(), served_id, expected_version, expected_rainfall)
        except served.ServedError as exc:
            flash(f"确认被拒绝：{exc}", "error")
            return redirect(url_for("served_detail_html", served_id=served_id))
        flash(
            f"已确认 {state['station']} @ {state['hour']} 的雨量"
            f" {state['baseline_rainfall_mm']:.1f} mm",
            "info",
        )
        return redirect(url_for("served_detail_html", served_id=served_id))

    @app.get("/reviews")
    def reviews_html():
        conn = get_conn()
        station = request.args.get("station", "").strip() or None
        return render_template(
            "reviews.html",
            reviews=served.list_reviews(conn, station),
            station=station or "",
        )

    @app.get("/batches/<int:batch_id>")
    def batch_detail(batch_id):
        batch, rows = db.get_batch(get_conn(), batch_id)
        if batch is None:
            flash("批次不存在", "error")
            return redirect(url_for("index"))
        return render_template("batch_detail.html", batch=batch, rows=rows)

    # ---------------- API（与网页同一计算入口，结果一致） ----------------

    @app.post("/api/upload")
    def upload_api():
        try:
            text, filename = read_upload_text()
            valid, errors = core.parse_csv_text(text)
        except core.CsvFormatError as exc:
            return jsonify({"error": str(exc)}), 400
        report = db.apply_batch(get_conn(), valid, errors, filename)
        out = _report_json(report)
        out["changed_served_hours"] = served.reviews_for_hours(
            get_conn(), report["changed_served_hours"])
        return jsonify(out)

    @app.get("/api/hourly")
    def hourly_api():
        station = request.args.get("station", "").strip()
        if not station:
            return jsonify({"error": "缺少 station 参数"}), 400
        try:
            hour = core.parse_hour(request.args.get("hour", ""))
        except ValueError as exc:
            return jsonify({"error": f"hour 参数无效: {exc}"}), 400
        return jsonify(compute_hour_result(station, hour))

    @app.get("/api/stations")
    def stations_api():
        out = [{
            "station": r["station"],
            "readings": r["n"],
            "first_minute": core.iso_utc(r["first_minute"]),
            "last_minute": core.iso_utc(r["last_minute"]),
        } for r in db.list_stations(get_conn())]
        return jsonify(out)

    @app.get("/api/batches")
    def batches_api():
        return jsonify([dict(r) for r in db.list_batches(get_conn(), limit=100)])

    # ---------------- 已供数 / 待复核 API（与网页同一服务层） ----------------

    @app.post("/api/served")
    def register_served_api():
        payload = request.get_json(silent=True) or request.form
        station = str(payload.get("station") or "").strip()
        hour_text = str(payload.get("hour") or "")
        try:
            hour = core.parse_hour(hour_text)
            state, created = served.register_served(get_conn(), station, hour)
        except (core.CsvFormatError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 400
        state = dict(state)
        state["created"] = created
        return jsonify(state), 201 if created else 200

    @app.get("/api/served")
    def served_list_api():
        station = request.args.get("station", "").strip() or None
        return jsonify(served.list_served(get_conn(), station))

    @app.get("/api/served/<int:served_id>")
    def served_detail_api(served_id):
        conn = get_conn()
        with db.read_snapshot(conn):
            state = served.get_served(conn, served_id)
            events = served.list_events(conn, served_id) if state else []
        if state is None:
            return jsonify({"error": "已供数登记不存在"}), 404
        state["events"] = events
        return jsonify(state)

    @app.post("/api/served/<int:served_id>/confirm")
    def confirm_served_api(served_id):
        payload = request.get_json(silent=True) or request.form
        try:
            expected_version = int(payload.get("expected_version"))
            expected_rainfall = float(payload.get("expected_rainfall_mm"))
        except (TypeError, ValueError):
            return jsonify({
                "error": "必须提供 expected_version 与 expected_rainfall_mm"
                "（页面所见值与版本号）"
            }), 400
        try:
            state = served.confirm_served(
                get_conn(), served_id, expected_version, expected_rainfall
            )
        except served.ServedError as exc:
            return jsonify({"error": str(exc)}), exc.status
        return jsonify(state)

    @app.get("/api/reviews")
    def reviews_api():
        station = request.args.get("station", "").strip() or None
        return jsonify(served.list_reviews(get_conn(), station))

    @app.get("/api/health")
    def health():
        get_conn().execute("SELECT 1")
        return jsonify({"status": "ok"})

    @app.errorhandler(413)
    def too_large(_):
        return "上传文件过大（上限 16MB）", 413

    return app


def _report_json(report: dict) -> dict:
    return {
        "batch_id": report["batch_id"],
        "filename": report["filename"],
        "uploaded_at": report["uploaded_at"],
        "total_rows": report["total_rows"],
        "accepted_count": report["accepted_count"],
        "rejected_count": report["rejected_count"],
        "accepted": [{
            "row_no": a["row_no"],
            "action": a["action"],
            "station": a["reading"].station,
            "device": a["reading"].device,
            "minute": core.iso_utc(a["reading"].minute_epoch),
            "counter": a["reading"].counter,
            "revision": a["reading"].revision,
        } for a in report["accepted"]],
        "rejected": report["rejected"],
    }


def main():
    app = create_app()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), threaded=True)


if __name__ == "__main__":
    main()
