"""Flask review application: upload, per-household corrections, analysis view."""

from __future__ import annotations

import os
from datetime import datetime, timezone

from flask import Flask, flash, redirect, render_template, request, url_for

from .csv_io import CSVValidationError, parse_scores
from .db import (
    apply_correction,
    apply_pairings,
    connect,
    get_household,
    has_dataset,
    init_db,
    replace_original,
    reset_corrected,
    unpair_corrected,
)
from .stats import analyze


def _pair_values(form) -> list[tuple[str, str]]:
    """Read ``(原表号, 更正表号)`` rows from the pairing form.

    Fully blank rows are ignored (the form ships a few spare rows); a row
    with only one side filled is kept so the data layer rejects the whole
    submission.
    """
    origs = form.getlist("original_id")
    corrs = form.getlist("corrected_id")
    if len(origs) != len(corrs):
        raise ValueError("配对表单不完整，整单已退回")
    pairs: list[tuple[str, str]] = []
    for orig, corr in zip(origs, corrs):
        orig, corr = orig.strip(), corr.strip()
        if not orig and not corr:
            continue
        pairs.append((orig, corr))
    return pairs


def create_app(db_path: str | None = None) -> Flask:
    app = Flask(__name__)
    app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-in-prod")
    app.config["DB_PATH"] = db_path or os.environ.get(
        "DB_PATH", os.path.join(os.getcwd(), "data", "survey.db")
    )
    os.makedirs(os.path.dirname(app.config["DB_PATH"]), exist_ok=True)

    def get_conn():
        conn = connect(app.config["DB_PATH"])
        init_db(conn)
        return conn

    @app.route("/", methods=["GET"])
    def index():
        conn = get_conn()
        try:
            uploaded = has_dataset(conn)
            result = analyze(conn) if uploaded else None
            lookup = None
            lookup_id = request.args.get("household_id", "").strip()
            if uploaded and lookup_id:
                lookup = get_household(conn, lookup_id)
                if lookup is None:
                    flash(f"未找到住户 {lookup_id}（原表与更正表均无此户）。", "warn")
            return render_template(
                "index.html", result=result, uploaded=uploaded,
                lookup=lookup, lookup_id=lookup_id,
            )
        finally:
            conn.close()

    @app.route("/upload", methods=["POST"])
    def upload():
        file = request.files.get("csv_file")
        if file is None or file.filename == "":
            flash("请选择要上传的 CSV 文件。", "error")
            return redirect(url_for("index"))
        raw = file.read()
        try:
            rows = parse_scores(raw)
        except CSVValidationError as exc:
            flash(f"上传被拒绝，数据库未改动：{exc}", "error")
            return redirect(url_for("index"))

        conn = get_conn()
        try:
            replace_original(
                conn, rows,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )
        except Exception as exc:  # pragma: no cover - defensive rollback path
            conn.rollback()
            flash(f"入库失败，已保留上一结果：{exc}", "error")
            return redirect(url_for("index"))
        finally:
            conn.close()
        flash(f"上传成功：{len(rows)} 户已原样入库，更正表已初始化为原表副本。", "ok")
        return redirect(url_for("index"))

    @app.route("/correct", methods=["POST"])
    def correct():
        household_id = request.form.get("household_id", "").strip()
        action = request.form.get("action", "").strip()
        score_text = request.form.get("score", "").strip()
        if not household_id:
            flash("请填写住户号。", "error")
            return redirect(url_for("index"))

        new_score: int | None = None
        if action != "remove":
            try:
                new_score = int(score_text)
            except ValueError:
                flash("评分必须是 0～10 的整数。", "error")
                return redirect(url_for("index"))

        conn = get_conn()
        try:
            if not has_dataset(conn):
                flash("尚未上传原表，无法更正。", "error")
                return redirect(url_for("index"))
            apply_correction(conn, household_id, action, new_score)
        except ValueError as exc:
            conn.rollback()
            flash(f"更正失败，已保留上一结果：{exc}", "error")
            return redirect(url_for("index"))
        except Exception as exc:  # pragma: no cover - defensive rollback path
            conn.rollback()
            flash(f"更正失败，已保留上一结果：{exc}", "error")
            return redirect(url_for("index"))
        finally:
            conn.close()

        verb = {"add": "补录", "update": "改分", "remove": "移除"}.get(action, action)
        flash(f"已对住户 {household_id} 执行「{verb}」，更正表与汇总已一同生效。", "ok")
        return redirect(url_for("index"))

    @app.route("/pair", methods=["POST"])
    def pair():
        try:
            pairs = _pair_values(request.form)
        except ValueError as exc:
            flash(f"改配被整单退回，已保留原结果：{exc}", "error")
            return redirect(url_for("index"))
        if not pairs:
            flash("请至少填写一组合法的原表号与更正表号。", "error")
            return redirect(url_for("index"))
        conn = get_conn()
        try:
            if not has_dataset(conn):
                flash("尚未上传原表，无法改配。", "error")
                return redirect(url_for("index"))
            summary = apply_pairings(conn, pairs)
        except ValueError as exc:
            conn.rollback()
            flash(f"改配被整单退回，已保留原结果：{exc}", "error")
            return redirect(url_for("index"))
        except Exception as exc:  # pragma: no cover - defensive rollback path
            conn.rollback()
            flash(f"改配失败，已保留原结果：{exc}", "error")
            return redirect(url_for("index"))
        finally:
            conn.close()
        flash(
            f"改配已生效：提交 {summary['paired']} 组配对，"
            f"重算 {summary['affected']} 户明细（其中 {summary['unpaired']} 户"
            "因失去伙伴且未获新配对而解配），更正表与汇总同事务提交。",
            "ok",
        )
        return redirect(url_for("index"))

    @app.route("/unpair", methods=["POST"])
    def unpair():
        corrected_id = request.form.get("household_id", "").strip()
        if not corrected_id:
            flash("请填写要解配的更正表号。", "error")
            return redirect(url_for("index"))
        conn = get_conn()
        try:
            if not has_dataset(conn):
                flash("尚未上传原表，无法解配。", "error")
                return redirect(url_for("index"))
            changed = unpair_corrected(conn, corrected_id)
        except ValueError as exc:
            conn.rollback()
            flash(f"解配失败，已保留原结果：{exc}", "error")
            return redirect(url_for("index"))
        except Exception as exc:  # pragma: no cover - defensive rollback path
            conn.rollback()
            flash(f"解配失败，已保留原结果：{exc}", "error")
            return redirect(url_for("index"))
        finally:
            conn.close()
        if changed:
            flash(
                f"更正户 {corrected_id} 已解配，改用固定独立权重，汇总已同步。",
                "ok",
            )
        else:
            flash(f"更正户 {corrected_id} 原本就未配对，无变化。", "warn")
        return redirect(url_for("index"))

    @app.route("/reset", methods=["POST"])
    def reset():
        conn = get_conn()
        try:
            if not has_dataset(conn):
                flash("尚未上传原表，无需重置。", "warn")
                return redirect(url_for("index"))
            reset_corrected(conn)
        except Exception as exc:  # pragma: no cover - defensive rollback path
            conn.rollback()
            flash(f"重置失败，已保留上一结果：{exc}", "error")
            return redirect(url_for("index"))
        finally:
            conn.close()
        flash("更正表已重置为原表副本，汇总已同步重建。", "ok")
        return redirect(url_for("index"))

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    return app
