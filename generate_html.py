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
import re
import sys
import json
from pathlib import Path
from datetime import datetime
from typing import Optional

# ── ローカルモジュール ──
sys.path.insert(0, str(Path(__file__).parent))
from app.lib.config import DEFAULT_DATA_DIR
from app.lib.html_builder import (build_portal_context, build_detail_json,
                                  strip_detail_only_json)


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
    profit_breakdown_raw = None
    if "profit_data" in data and len(data.get("profit_data", pd.DataFrame())) > 0:
        try:
            from app.lib.profit import build_profit_monthly
            pb  = data.get("profit_breakdown")
            ptb = data.get("profit_targets_breakdown")
            profit_monthly = build_profit_monthly(
                data["profit_data"], data.get("profit_targets", pd.DataFrame()),
                profit_breakdown=pb, profit_targets_breakdown=ptb,
            )
            profit_breakdown_raw = pb
            mode = "内訳" if (pb is not None and ptb is not None) else "旧式"
            log(f"粗利({mode}): {len(profit_monthly):,} 行")
        except Exception as e:
            log(f"粗利データ前処理スキップ: {e}", "warn")
    else:
        # load_all に profit_data が含まれない場合、個別読込を試行
        try:
            from app.lib.data_loader import (
                load_profit_data, load_profit_targets,
                load_profit_breakdown, load_profit_targets_breakdown,
            )
            from app.lib.profit import build_profit_monthly
            pd_raw  = load_profit_data(data_dir)
            pt_raw  = load_profit_targets(data_dir)
            pb_raw  = load_profit_breakdown(data_dir)
            ptb_raw = load_profit_targets_breakdown(data_dir)
            profit_monthly = build_profit_monthly(
                pd_raw, pt_raw,
                profit_breakdown=pb_raw, profit_targets_breakdown=ptb_raw,
            )
            profit_breakdown_raw = pb_raw
            mode = "内訳" if (pb_raw is not None and ptb_raw is not None) else "旧式"
            log(f"粗利({mode}・個別読込): {len(profit_monthly):,} 行")
        except Exception as e:
            log(f"粗利データなし（スキップ）: {e}", "warn")

    # ── 基準日 ──
    if base_date_str:
        base_date = pd.Timestamp(base_date_str)
    else:
        base_date = adm["日付"].max()
    log(f"基準日: {base_date.strftime('%Y-%m-%d')}")

    return adm, surg, targets, surg_targets, profit_monthly, base_date, profit_breakdown_raw


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
            f'<meta name="robots" content="noindex, nofollow">'
            f'<meta http-equiv="refresh" content="0;url={new_url}">'
            f'<title>リダイレクト中...</title></head>'
            f'<body><p><a href="{new_url}">こちら</a>に移動しました</p></body></html>',
            encoding="utf-8"
        )
    return list(redirects.keys())


# ════════════════════════════════════════
# ナラティブ生成キャッシュ（scripts/build_dept_reports.py と状態ファイルを共有）
# ════════════════════════════════════════
# 同一データでの再ビルド（make の再実行）でも、部門別レポートPDF生成
# （build_dept_reports.py）と同じ dept_reports/_state/narrative_cache_{基準日}.json を
# 読み書きし、narrate_leveling_actions（_generate_checked 経由）のAI一手を使い回す。
# キーは日付非依存（プロンプト全文のSHA1）なので、同一データ断面なら再生成と
# バイト単位で同一の文が得られる（LLM呼び出しゼロ）。narrate_alerts はこの機構を
# 持たない別経路のため対象外（従来どおり毎回ライブ生成）。

_NARR_CACHE_FNAME_RE = re.compile(r"^narrative_cache_(\d{4}-\d{2}-\d{2})\.json$")


def _parse_narr_cache_date(name: str):
    """narrative_cache_YYYY-MM-DD.json のファイル名から日付を取り出す（不一致はNone）。
    scripts/build_dept_reports.py find_narr_cache_seed と同等ロジックの複製
    （モジュールレベル副作用を避けるため scripts/ からは import しない）。"""
    m = _NARR_CACHE_FNAME_RE.match(name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d").date()
    except ValueError:
        return None


def _find_narr_cache_seed(state_dir: Path, base_date, exclude: Path):
    """基準日の生成キャッシュが無いとき、引き継ぎ元（base_date 以前で最新）を探す。
    scripts/build_dept_reports.py の同名関数と同じ選択ロジック。"""
    if not state_dir.is_dir():
        return None
    base = base_date.date() if hasattr(base_date, "date") else base_date
    best_path, best_date = None, None
    for p in sorted(state_dir.glob("narrative_cache_*.json")):
        if p == exclude:
            continue
        d = _parse_narr_cache_date(p.name)
        if d is None or d > base:
            continue
        if best_date is None or d > best_date:
            best_path, best_date = p, d
    return best_path


def _resolve_narr_cache_seed(state_dir: Path, base_date):
    """基準日のキャッシュパスと、実際に読み込む種ファイルのパスを解決する。

    基準日ファイルが存在すればそれ自身を種にする。無ければ基準日以前で最新の
    過去ファイルを種にする。過去ファイルも1件も無ければ基準日パス自体を返す
    （存在しないパス＝load_narrative_cache が fail-soft で空キャッシュとして
    有効化する仕様を利用する）。

    戻り値: (narr_cache_path, narr_cache_seed)
      narr_cache_path — 常に基準日のファイルパス（保存先はここに固定）
      narr_cache_seed — 実際に読み込むファイルパス（種ファイル or 基準日自身）
    """
    narr_cache_path = state_dir / f"narrative_cache_{base_date.strftime('%Y-%m-%d')}.json"
    if narr_cache_path.is_file():
        return narr_cache_path, narr_cache_path
    seed = _find_narr_cache_seed(state_dir, base_date, narr_cache_path)
    return narr_cache_path, (seed or narr_cache_path)


def _count_narr_cache_entries(path: Path) -> int:
    """ログ用にキャッシュファイルのエントリ数を数える（fail-soft・読めなければ0件）。"""
    try:
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return len(data)
    except Exception:
        pass
    return 0


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
    adm, surg, targets, surg_targets, profit_monthly, base_date, profit_breakdown_raw = \
        load_and_preprocess(data_dir, base_date_str, no_validate)

    # ── ナラティブ生成キャッシュ 読込（基準日確定直後）──
    # 以後の narrate_leveling_actions（_generate_checked 経由）が自動でキャッシュを使う。
    from app.lib.ai_narrative import load_narrative_cache
    narr_state_dir = Path("dept_reports") / "_state"
    narr_cache_path, narr_cache_seed = _resolve_narr_cache_seed(narr_state_dir, base_date)
    n_seed = _count_narr_cache_entries(narr_cache_seed)
    if narr_cache_seed.is_file():
        log(f"生成キャッシュ読込: {n_seed}件 ← {narr_cache_seed.name}")
    else:
        log("生成キャッシュ読込: 0件（新規）")
    load_narrative_cache(narr_cache_seed)

    env = _build_jinja_env()
    results = {}

    # ════════════════════════════════════════
    # 週次ストーリー（WoW差分特化）— portal に埋め込むため先行生成
    # ════════════════════════════════════════
    weekly_story_result = None
    try:
        from app.lib.metrics import build_kpi_summary
        from app.lib.weekly_story import build_weekly_story
        log("週次ストーリー生成中...")
        kpi = build_kpi_summary(adm, surg, base_date, targets, surg_targets)
        snapshot_path = out_dir / "output" / "last_kpi.json"
        weekly_story_result = build_weekly_story(
            adm, surg, kpi, profit_monthly, base_date, snapshot_path,
            quiet=quiet,
        )
        if weekly_story_result.get("story"):
            log(f"週次ストーリー: {weekly_story_result['story']}", "ok")
        elif weekly_story_result.get("diffs"):
            log(f"週次ストーリー: 差分{len(weekly_story_result['diffs'])}件（LLM要約なし）", "warn")
        else:
            log("週次ストーリー: 差分なし / 前回スナップショット無し", "info")
    except Exception as e:
        log(f"週次ストーリー生成スキップ: {e}", "warn")
        # D3裁定(a): 生成失敗の欠落は紙面で可視化する（正当な欠落=初回/差分なし週は
        # weekly_story_result 自体が diffs空・failed無しになるため縮退表示は出ない）。
        weekly_story_result = {"base_date": None, "prior_date": None,
                               "diffs": [], "story": None, "failed": True}
    results["weekly_story"] = weekly_story_result

    # ════════════════════════════════════════
    # Layer-1: portal.html
    # ════════════════════════════════════════
    log("portal.html 生成中...")
    portal_ctx = build_portal_context(
        adm, surg, targets, surg_targets, base_date, generated_at,
        weekly_story=weekly_story_result,
        profit_monthly=profit_monthly,
        kpi_history_path=out_dir / "output" / "last_kpi.json",
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
        adm, surg, targets, surg_targets, profit_monthly, base_date, generated_at,
        profit_breakdown=profit_breakdown_raw,
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
    dept_ctx = dict(detail_ctx, data_json=strip_detail_only_json(detail_json))
    dept_html = dept_tmpl.render(**dept_ctx)
    dept_path = out_dir / "dept.html"
    dept_path.write_text(dept_html, encoding="utf-8")
    results["dept"] = str(dept_path.resolve())
    log(f"dept.html → {dept_path.resolve()}", "ok")

    # ════════════════════════════════════════
    # ナラティブ生成キャッシュ 保存（生成処理が正常完了した箇所で1回）
    # ════════════════════════════════════════
    from app.lib.ai_narrative import save_narrative_cache
    save_narrative_cache(narr_cache_path)
    n_saved = _count_narr_cache_entries(narr_cache_path)
    log(f"生成キャッシュ保存: {n_saved}件 → {narr_cache_path.name}")

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
        if k in ("redirects", "weekly_story"):
            continue
        print(f"    {k}: {v}")
    if "redirects" in results:
        print(f"    リダイレクト: {len(results['redirects'])}件")
    ws = results.get("weekly_story")
    if ws and (ws.get("story") or ws.get("diffs")):
        print(f"    週次ストーリー（vs {ws.get('prior_date') or '—'}）:")
        for d in ws.get("diffs", []):
            print(f"      - {d}")
        if ws.get("story"):
            print(f"    要約: {ws['story']}")
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
