#!/usr/bin/env python3
"""
generate_html.py — 静的HTML生成スクリプト（v2.1）

提案B: 2層ハブ＆スポーク型 + 部門別
  Layer-1: portal.html   — 信号機ポータル
  Layer-2: detail.html   — 統合詳細ダッシュボード
  Layer-3: dept.html     — 部門別ダッシュボード（診療科・病棟切替）

v2.1 変更点:
  - 出力ファイル: 7種 → 3種（portal.html + detail.html + dept.html）
  - doctor.html / nurse.html / admission/ / inpatient/ / reports/ は廃止
  - 旧URLからのリダイレクトHTML を自動生成（互換性維持）
"""

import argparse
import sys
import json
from pathlib import Path
from datetime import datetime
from typing import Optional

# ── ローカルモジュール ──
sys.path.insert(0, str(Path(__file__).parent))
from app.lib.config import DEFAULT_DATA_DIR
from app.lib.html_builder import build_portal_context, build_detail_json


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="診療ダッシュボード HTML生成（v2.1）")
    p.add_argument("--data-dir", default=DEFAULT_DATA_DIR, help="データフォルダ")
    p.add_argument("--output-dir", default=".", help="出力先ディレクトリ")
    p.add_argument("--base-date", default=None, help="基準日 YYYY-MM-DD")
    p.add_argument("--sort-by", default="achievement", choices=["achievement", "actual"])
    p.add_argument("--no-validate", action="store_true")
    p.add_argument("--no-redirect", action="store_true", help="旧URLリダイレクト生成をスキップ")
    p.add_argument("--quiet", "-q", action="store_true")
    p.add_argument("--setup", action="store_true", help="データフォルダの初期化のみ")
    return p.parse_args()


def log(msg: str, level: str = "info"):
    ts = datetime.now().strftime("%H:%M:%S")
    prefix = {"info": "ℹ️ ", "ok": "✅", "warn": "⚠️ ", "err": "❌"}
    print(f"  {prefix.get(level, '')} [{ts}] {msg}")


def load_and_preprocess(data_dir: str, base_date_str: Optional[str] = None,
                        no_validate: bool = False):
    """データ読込・前処理（既存 data_loader / preprocess モジュール流用）"""
    from app.lib.data_loader import load_all
    from app.lib.preprocess import (
        preprocess_admission, preprocess_surgery,
        build_target_lookup, build_surgery_target_lookup,
    )
    import pandas as pd

    # ── ファイル存在確認 ──
    if not no_validate:
        try:
            from app.lib.validate import check_files
            fr = check_files(data_dir)
            fr.raise_if_error()
        except ImportError:
            pass  # validate モジュールが無い場合はスキップ

    # ── データ読込（load_all で一括） ──
    log("データ読込中...")
    data = load_all(data_dir)
    log(f"入院: {len(data['admission']):,} 行 / 手術: {len(data['surgery']):,} 件")

    # ── 前処理 ──
    log("前処理中...")
    adm  = preprocess_admission(data["admission"])
    surg = preprocess_surgery(data["surgery"])
    targets      = build_target_lookup(data["inpatient_targets"])
    surg_targets = build_surgery_target_lookup(data["surgery_targets"])

    # ── 粗利（オプション） ──
    profit_monthly = pd.DataFrame()
    if "profit_data" in data and len(data.get("profit_data", pd.DataFrame())) > 0:
        try:
            from app.lib.profit import build_profit_monthly
            profit_monthly = build_profit_monthly(
                data["profit_data"], data.get("profit_targets", pd.DataFrame())
            )
            log(f"粗利: {len(profit_monthly):,} 行")
        except Exception as e:
            log(f"粗利データ前処理スキップ: {e}", "warn")
    else:
        # load_all に profit_data が含まれない場合、個別読込を試行
        try:
            from app.lib.data_loader import load_profit_data, load_profit_targets
            from app.lib.profit import build_profit_monthly
            pd_raw = load_profit_data(data_dir)
            pt_raw = load_profit_targets(data_dir)
            profit_monthly = build_profit_monthly(pd_raw, pt_raw)
            log(f"粗利（個別読込）: {len(profit_monthly):,} 行")
        except Exception as e:
            log(f"粗利データなし（スキップ）: {e}", "warn")

    # ── 基準日 ──
    if base_date_str:
        base_date = pd.Timestamp(base_date_str)
    else:
        base_date = adm["日付"].max()
    log(f"基準日: {base_date.strftime('%Y-%m-%d')}")

    return adm, surg, targets, surg_targets, profit_monthly, base_date


def _build_jinja_env():
    """Jinja2環境構築"""
    from jinja2 import Environment, FileSystemLoader
    env = Environment(
        loader=FileSystemLoader(str(Path(__file__).parent / "app" / "templates")),
        autoescape=False,
    )
    # カスタムフィルタ
    env.filters["numfmt"] = lambda v, fmt=",": f"{v:{fmt}}" if v is not None else "—"
    env.filters["pct"] = lambda v: f"{v:.1f}%" if v is not None else "—"
    env.filters["to_float"] = lambda v: float(v) if v is not None else 0
    return env


def _generate_redirects(out_dir: Path):
    """旧URLからの自動リダイレクト"""
    redirects = {
        "doctor.html":          "detail.html#admission?axis=dept",
        "nurse.html":           "detail.html#inpatient?axis=ward",
        "admission/index.html": "../detail.html#admission",
        "inpatient/index.html": "../detail.html#inpatient",
        "operation/index.html": "../detail.html#operation",
    }
    for old_path, new_url in redirects.items():
        full_path = out_dir / old_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(
            f'<!DOCTYPE html><html><head>'
            f'<meta http-equiv="refresh" content="0;url={new_url}">'
            f'<title>リダイレクト中...</title></head>'
            f'<body><p><a href="{new_url}">こちら</a>に移動しました</p></body></html>',
            encoding="utf-8"
        )
    return list(redirects.keys())


def generate(data_dir: str = DEFAULT_DATA_DIR,
             output_dir: str = ".",
             base_date_str: str = None,
             sort_by: str = "achievement",
             no_validate: bool = False,
             no_redirect: bool = False,
             quiet: bool = False) -> dict:
    """メイン生成処理"""
    generated_at = datetime.now()
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # ── データ読込 ──
    adm, surg, targets, surg_targets, profit_monthly, base_date = \
        load_and_preprocess(data_dir, base_date_str, no_validate)

    env = _build_jinja_env()
    results = {}

    # ════════════════════════════════════════
    # Layer-1: portal.html
    # ════════════════════════════════════════
    log("portal.html 生成中...")
    portal_ctx = build_portal_context(
        adm, surg, targets, surg_targets, base_date, generated_at
    )
    portal_tmpl = env.get_template("portal.html")
    portal_html = portal_tmpl.render(**portal_ctx)
    portal_path = out_dir / "portal.html"
    portal_path.write_text(portal_html, encoding="utf-8")
    results["portal"] = str(portal_path.resolve())
    log(f"portal.html → {portal_path.resolve()}", "ok")

    # ════════════════════════════════════════
    # Layer-2: detail.html
    # ════════════════════════════════════════
    log("detail.html 生成中...")
    detail_json = build_detail_json(
        adm, surg, targets, surg_targets, profit_monthly, base_date, generated_at
    )
    detail_ctx = {
        "data_json": detail_json,
        "base_date": base_date.strftime("%Y-%m-%d"),
        "generated_at": generated_at.strftime("%Y/%m/%d %H:%M"),
    }
    detail_tmpl = env.get_template("detail.html")
    detail_html = detail_tmpl.render(**detail_ctx)
    detail_path = out_dir / "detail.html"
    detail_path.write_text(detail_html, encoding="utf-8")
    results["detail"] = str(detail_path.resolve())
    log(f"detail.html → {detail_path.resolve()}", "ok")

    # ════════════════════════════════════════
    # Layer-3: dept.html（部門別ダッシュボード）
    # ════════════════════════════════════════
    log("dept.html 生成中...")
    dept_tmpl = env.get_template("dept.html")
    dept_html = dept_tmpl.render(**detail_ctx)
    dept_path = out_dir / "dept.html"
    dept_path.write_text(dept_html, encoding="utf-8")
    results["dept"] = str(dept_path.resolve())
    log(f"dept.html → {dept_path.resolve()}", "ok")

    # ════════════════════════════════════════
    # 旧URLリダイレクト
    # ════════════════════════════════════════
    if not no_redirect:
        log("旧URLリダイレクト生成中...")
        redirected = _generate_redirects(out_dir)
        results["redirects"] = redirected
        log(f"リダイレクト: {', '.join(redirected)}", "ok")

    # ════════════════════════════════════════
    # サマリー
    # ════════════════════════════════════════
    print(f"\n{'='*50}")
    print(f"  生成完了 — {generated_at.strftime('%Y/%m/%d %H:%M')}")
    print(f"  基準日: {base_date.strftime('%Y-%m-%d')}")
    print(f"  出力:")
    for k, v in results.items():
        if k != "redirects":
            print(f"    {k}: {v}")
    if "redirects" in results:
        print(f"    リダイレクト: {len(results['redirects'])}件")
    print(f"{'='*50}\n")

    return results


def setup_data_dir(data_dir: str = DEFAULT_DATA_DIR):
    """データフォルダの初期化"""
    from app.lib.config import DATA_FOLDERS
    base = Path(data_dir)
    for folder_name in DATA_FOLDERS.values():
        (base / folder_name).mkdir(parents=True, exist_ok=True)
        log(f"フォルダ作成: {base / folder_name}", "ok")
    log("データフォルダの初期化完了", "ok")


def main():
    args = parse_args()

    if args.setup:
        setup_data_dir(args.data_dir)
        return

    try:
        generate(
            data_dir=args.data_dir,
            output_dir=args.output_dir,
            base_date_str=args.base_date,
            sort_by=args.sort_by,
            no_validate=args.no_validate,
            no_redirect=args.no_redirect,
            quiet=args.quiet,
        )
    except FileNotFoundError as e:
        log(f"ファイルが見つかりません: {e}", "err")
        sys.exit(1)
    except Exception as e:
        log(f"エラー: {e}", "err")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
