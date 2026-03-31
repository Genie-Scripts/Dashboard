import pandas as pd
import numpy as np
import json
from datetime import datetime
from pathlib import Path
import io

# ============================================================
# HTML テンプレート（データ埋め込み済みで出力）
# ============================================================

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>🏥 新入院患者 ダッシュボード</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Noto+Sans+JP:wght@300;400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
  <style>
    /* ══ DESIGN TOKENS ══ */
    :root {
      --bg-page:      #F0F2F5;
      --bg-card:      #FFFFFF;
      --bg-elevated:  #F7F9FC;
      --bg-header:    #1D2B3A;
      --border:       #DCE1E9;
      --border-mid:   #C4CCD8;
      --accent:       #3A6EA5;
      --accent-light: #EEF3FA;
      --accent-hover: #2F5A8A;
      --text-primary:   #1A2535;
      --text-secondary: #5A6A82;
      --text-muted:     #94A3B8;
      --success:      #1A9E6A;
      --success-bg:   #EDFAF4;
      --warning:      #C87A00;
      --warning-bg:   #FEF6E4;
      --danger:       #C0293B;
      --danger-bg:    #FEF0F2;
      --font-ui:   'Noto Sans JP', 'Hiragino Sans', sans-serif;
      --font-mono: 'IBM Plex Mono', monospace;
      --r-sm: 6px; --r-md: 10px; --r-lg: 14px;
    }
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: var(--font-ui);
      background: var(--bg-page);
      color: var(--text-primary);
      font-size: 14px;
      line-height: 1.6;
    }

    /* ══ STICKY HEADER ══ */
    .site-header {
      background: var(--bg-header);
      color: #E8EEF5;
      padding: 0 32px;
      height: 60px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      position: sticky; top: 0; z-index: 200;
      border-bottom: 2px solid var(--accent);
      gap: 16px;
    }
    .header-left { display: flex; align-items: center; gap: 14px; min-width: 0; }
    .header-badge {
      background: var(--accent); color: #fff;
      font-size: 0.6rem; font-weight: 700;
      letter-spacing: 0.14em; text-transform: uppercase;
      padding: 3px 8px; border-radius: 4px; white-space: nowrap;
    }
    .header-title { font-size: 1.0rem; font-weight: 600; color: #E8EEF5; white-space: nowrap; }
    .header-meta {
      display: flex; align-items: center; gap: 16px;
      font-size: 0.72rem; color: #7A90A8;
      font-family: var(--font-mono); flex-wrap: nowrap;
    }
    .header-actions { display: flex; gap: 8px; flex-shrink: 0; }
    .btn-header {
      display: inline-flex; align-items: center; gap: 5px;
      padding: 6px 14px; border-radius: var(--r-sm);
      font-size: 0.78rem; font-weight: 500; cursor: pointer;
      transition: all 0.15s; text-decoration: none;
      border: 1px solid transparent; font-family: var(--font-ui); line-height: 1;
    }
    .btn-outline { background: transparent; color: #AAB8C8; border-color: #3A4E62; }
    .btn-outline:hover { background: rgba(255,255,255,0.07); color: #E8EEF5; }
    .btn-solid { background: var(--accent); color: #fff; border-color: var(--accent); }
    .btn-solid:hover { background: var(--accent-hover); }

    /* ══ LAYOUT ══ */
    .wrap { max-width: 1200px; margin: 0 auto; padding: 28px 32px 48px; }

    /* ══ SECTION HEAD ══ */
    .section-head {
      display: flex; align-items: center; gap: 10px; margin: 0 0 16px;
    }
    .section-label {
      font-size: 0.67rem; font-weight: 700; letter-spacing: 0.14em;
      color: var(--text-muted); text-transform: uppercase; white-space: nowrap;
    }
    .section-head::after { content:''; flex:1; height:1px; background: var(--border); }
    .section-head.clickable { cursor: pointer; }
    .toggle-pill {
      font-size: 0.7rem; color: var(--text-muted);
      padding: 2px 10px; border: 1px solid var(--border);
      border-radius: 12px; white-space: nowrap; margin-left: auto;
    }

    /* ══ KPI GRID ══ */
    .kpi-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(175px, 1fr));
      gap: 14px; margin-bottom: 32px;
    }
    .kpi-card {
      background: var(--bg-card);
      border-radius: var(--r-lg);
      padding: 18px 18px 14px;
      border: 1px solid var(--border);
      border-top: 3px solid var(--border);
      box-shadow: 0 1px 4px rgba(0,0,0,0.05);
      transition: box-shadow 0.15s, transform 0.15s;
      display: flex; flex-direction: column;
    }
    .kpi-card:hover { transform: translateY(-2px); box-shadow: 0 4px 14px rgba(0,0,0,0.09); }
    .kpi-card.success { border-top-color: var(--success); }
    .kpi-card.warn    { border-top-color: var(--warning); }
    .kpi-card.danger  { border-top-color: var(--danger);  }
    .kpi-card.flat    { border-top-color: var(--border-mid); }
    .kpi-card-header  { display: flex; align-items: flex-start; justify-content: space-between; gap: 4px; }
    .kpi-label { font-size: 0.67rem; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase; color: var(--text-muted); line-height: 1.3; flex:1; }
    .kpi-icon  { font-size: 1rem; line-height: 1; flex-shrink: 0; }
    .kpi-value { font-size: 1.6rem; font-weight: 700; font-family: var(--font-mono); color: var(--text-primary); margin-top: 8px; line-height: 1; }
    .kpi-value.success { color: var(--success); }
    .kpi-value.warn    { color: var(--warning); }
    .kpi-value.danger  { color: var(--danger);  }
    .kpi-value.flat    { color: var(--text-muted); }
    .kpi-sub   { font-size: 0.67rem; color: var(--text-muted); margin-top: 5px; margin-bottom: 10px; }
    .kpi-gauge-wrap { height: 3px; background: var(--bg-elevated); border-radius: 99px; overflow: hidden; }
    .kpi-gauge-fill { height: 100%; border-radius: 99px; }
    .kpi-gauge-fill.success { background: var(--success); }
    .kpi-gauge-fill.warn    { background: var(--warning); }
    .kpi-gauge-fill.danger  { background: var(--danger);  }
    .kpi-gauge-fill.flat    { background: var(--border-mid); }
    .kpi-gauge-pct { font-size: 0.65rem; font-weight: 600; margin-top: 4px; font-family: var(--font-mono); }

    /* ══ CHART CARD ══ */
    .chart-card {
      background: var(--bg-card);
      border-radius: var(--r-lg);
      border: 1px solid var(--border);
      padding: 20px 22px 16px;
      box-shadow: 0 1px 4px rgba(0,0,0,0.05);
      margin-bottom: 32px;
    }
    .chart-controls {
      display: flex; align-items: center; gap: 10px;
      flex-wrap: wrap; margin-bottom: 14px;
    }
    .chart-select {
      font-size: 0.84rem; padding: 6px 12px;
      border: 1px solid var(--border); border-radius: var(--r-sm);
      background: var(--bg-elevated); color: var(--text-primary);
      cursor: pointer; flex:1; min-width: 160px; max-width: 280px;
      font-family: var(--font-ui);
    }
    .chart-select:focus { outline: none; border-color: var(--accent); }
    .range-group { display: flex; gap: 3px; }
    .range-btn {
      padding: 5px 13px; border-radius: var(--r-sm);
      border: 1px solid var(--border); background: var(--bg-elevated);
      color: var(--text-secondary); font-size: 0.77rem; font-weight: 500;
      cursor: pointer; transition: all 0.15s; white-space: nowrap;
      font-family: var(--font-ui);
    }
    .range-btn.active { background: var(--accent); color: #fff; border-color: var(--accent); }
    .range-btn:hover:not(.active) { background: var(--accent-light); color: var(--accent); border-color: var(--accent); }
    .chart-view-group { display: flex; gap: 3px; }
    .chart-view-btn {
      padding: 5px 13px; border-radius: var(--r-sm);
      border: 1px solid var(--border); background: var(--bg-elevated);
      color: var(--text-secondary); font-size: 0.77rem; font-weight: 500;
      cursor: pointer; transition: all 0.15s; white-space: nowrap;
      font-family: var(--font-ui);
    }
    .chart-view-btn.active { background: var(--text-primary); color: #fff; border-color: var(--text-primary); }
    .chart-view-btn:hover:not(.active) { background: var(--accent-light); color: var(--accent); border-color: var(--accent); }
    .chart-canvas-wrap { position: relative; height: 280px; width: 100%; }
    .chart-hint { font-size: 0.68rem; color: var(--text-muted); margin-top: 8px; }

    /* ══ VIEW TOGGLE ══ */
    .view-toggle { display: flex; gap: 3px; margin-bottom: 12px; }
    .view-btn {
      padding: 7px 18px; border-radius: var(--r-sm);
      border: 1px solid var(--border); background: var(--bg-elevated);
      color: var(--text-secondary); font-weight: 500; font-size: 0.82rem;
      cursor: pointer; transition: all 0.15s; font-family: var(--font-ui);
    }
    .view-btn.active { background: var(--text-primary); color: #fff; border-color: var(--text-primary); }
    .view-btn:hover:not(.active) { background: var(--accent-light); color: var(--accent); border-color: var(--accent); }

    /* ══ TAB BAR ══ */
    .tab-bar {
      display: flex; gap: 2px;
      background: var(--bg-card); border: 1px solid var(--border);
      border-radius: var(--r-md); padding: 4px;
      width: fit-content; margin-bottom: 16px;
      box-shadow: 0 1px 3px rgba(0,0,0,0.05);
    }
    .tab-btn {
      padding: 7px 20px; border-radius: var(--r-sm);
      font-size: 0.8rem; font-weight: 500; cursor: pointer;
      border: 1px solid transparent; background: transparent;
      color: var(--text-secondary); transition: all 0.15s;
      font-family: var(--font-ui); text-align: center; line-height: 1.3;
    }
    .tab-btn small { font-size: 0.63rem; display: block; color: var(--text-muted); margin-top: 1px; }
    .tab-btn.active { background: var(--accent); color: #fff; border-color: var(--accent); box-shadow: 0 1px 4px rgba(58,110,165,0.25); }
    .tab-btn.active small { color: rgba(255,255,255,0.72); }
    .tab-btn:hover:not(.active) { background: var(--accent-light); color: var(--accent); }

    /* ══ PERF BARS ══ */
    .perf-section { margin-bottom: 32px; }
    .perf-panel { display: none; }
    .perf-panel.active { display: block; }
    .perf-row {
      display: grid;
      grid-template-columns: 120px 160px 1fr 52px 16px;
      align-items: center; gap: 14px;
      padding: 8px 6px;
      border-bottom: 1px solid var(--border);
    }
    .perf-row:last-child { border-bottom: none; }
    .perf-row:hover { background: var(--bg-elevated); border-radius: var(--r-sm); }
    .perf-name {
      font-size: 0.76rem; font-weight: 600;
      padding: 3px 10px; border-radius: 4px; text-align: center;
      white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    }
    .perf-name.achieved { background: var(--success-bg); color: var(--success); }
    .perf-name.near     { background: var(--warning-bg); color: var(--warning); }
    .perf-name.below    { background: var(--danger-bg);  color: var(--danger);  }
    .perf-nums { font-family: var(--font-mono); font-size: 0.74rem; color: var(--text-secondary); white-space: nowrap; }
    .perf-bar-wrap {
      position: relative; height: 7px;
      background: var(--bg-elevated); border-radius: 4px;
      border: 1px solid var(--border); overflow: visible;
    }
    .perf-bar-fill {
      position: absolute; left: 0; top: -1px;
      height: calc(100% + 2px); border-radius: 3px;
      transition: width 0.6s cubic-bezier(.22,.68,0,1.1); min-width: 2px;
    }
    .perf-bar-fill.achieved { background: var(--success); }
    .perf-bar-fill.near     { background: var(--warning); }
    .perf-bar-fill.below    { background: var(--danger);  }
    .perf-target-line {
      position: absolute; top: -5px; width: 2px; height: 17px;
      background: var(--accent); border-radius: 1px;
      opacity: 0.75; z-index: 2; pointer-events: none;
    }
    .perf-rate { font-family: var(--font-mono); font-size: 0.86rem; font-weight: 700; text-align: right; color: var(--text-primary); }
    .perf-dot { width: 8px; height: 8px; border-radius: 50%; margin: auto; }
    .perf-dot.achieved { background: var(--success); }
    .perf-dot.near     { background: var(--warning); }
    .perf-dot.below    { background: var(--danger);  }
    .perf-legend {
      display: flex; gap: 18px; margin-top: 12px; flex-wrap: wrap;
      font-size: 0.72rem; color: var(--text-muted);
    }
    .legend-item { display: flex; align-items: center; gap: 6px; }
    .legend-dot  { width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0; }

    /* ══ PRINT ══ */
    @media print {
      body { background: white; }
      .site-header { position: static; -webkit-print-color-adjust: exact; print-color-adjust: exact; }
      .btn-header { display: none; }
      .tab-bar, .view-toggle { display: none; }
      .perf-panel { display: block !important; }
      @page { margin: 12mm; }
    }
    @media (max-width: 640px) {
      .wrap { padding: 16px; }
      .site-header { padding: 8px 14px; height: auto; min-height: 52px; flex-wrap: wrap; }
      .header-meta { display: none; }
      .perf-row { grid-template-columns: 90px 1fr 46px 14px; }
      .perf-nums { display: none; }
    }
  </style>
</head>
<body>

<header class="site-header">
  <div class="header-left">
    <span class="header-badge">新入院</span>
    <span class="header-title">🏥 新入院患者数ダッシュボード</span>
    <div class="header-meta">
      <span>📅 {{DATE_RANGE}}</span>
      <span>📊 集計: {{DATE_MAX}}</span>
      <span>🕐 生成: {{GENERATED_AT}}</span>
    </div>
  </div>
  <div class="header-actions">
    <button onclick="window.print()" class="btn-header btn-outline">🖨 印刷</button>
    <a href="../portal.html" class="btn-header btn-solid">🏠 ポータルに戻る</a>
  </div>
</header>

<div class="wrap">

  <!-- KPI -->
  <div class="section-head">
    <span class="section-label">病院全体 新入院患者 KPI</span>
  </div>
  <div class="kpi-grid" id="kpi-grid"></div>

  <!-- 推移グラフ（折りたたみ可） -->
  <div class="section-head clickable" onclick="toggleChart()">
    <span class="section-label">推移グラフ（7日 / 28日移動平均）</span>
    <span id="chart-toggle-icon" class="toggle-pill">▲ 閉じる</span>
  </div>
  <div class="chart-card" id="chart-container">
    <div class="chart-controls">
      <div class="chart-view-group">
        <button class="chart-view-btn active" id="cvbtn-dept" onclick="switchChartView('dept')">🏷 診療科</button>
        <button class="chart-view-btn"        id="cvbtn-ward" onclick="switchChartView('ward')">🏨 病棟</button>
      </div>
      <select id="chart-dept-select" class="chart-select" onchange="switchChartKey(this.value)">
      </select>
      <div class="range-group">
        <button class="range-btn active" id="rbtn-w24"  onclick="switchChartRange('w24')">直近24週</button>
        <button class="range-btn"        id="rbtn-fy"   onclick="switchChartRange('fy')">今年度</button>
        <button class="range-btn"        id="rbtn-y365" onclick="switchChartRange('y365')">直近365日</button>
      </div>
    </div>
    <div class="chart-canvas-wrap">
      <canvas id="mainChart"></canvas>
    </div>
    <div class="chart-hint">💡 ホバーで詳細表示</div>
  </div>

  <!-- パフォーマンスバー -->
  <div class="section-head">
    <span class="section-label">部門別 新入院患者 達成状況</span>
  </div>

  <div class="view-toggle">
    <button class="view-btn active" id="vbtn-dept" onclick="switchView('dept')">🏷 診療科別</button>
    <button class="view-btn"        id="vbtn-ward" onclick="switchView('ward')">🏨 病棟別</button>
  </div>

  <div class="tab-bar">
    <button class="tab-btn active" id="tbtn-7"  onclick="switchTab('7')">
      直近7日<br><small id="lbl-7"></small>
    </button>
    <button class="tab-btn" id="tbtn-28" onclick="switchTab('28')">
      直近28日<br><small id="lbl-28"></small>
    </button>
    <button class="tab-btn" id="tbtn-fy"  onclick="switchTab('fy')">
      今年度<br><small id="lbl-fy"></small>
    </button>
  </div>

  <div class="perf-section" id="perf-dept">
    <div class="perf-panel active" id="dept-7"></div>
    <div class="perf-panel"        id="dept-28"></div>
    <div class="perf-panel"        id="dept-fy"></div>
  </div>
  <div class="perf-section" id="perf-ward" style="display:none">
    <div class="perf-panel active" id="ward-7"></div>
    <div class="perf-panel"        id="ward-28"></div>
    <div class="perf-panel"        id="ward-fy"></div>
  </div>

  <div class="perf-legend">
    <div class="legend-item"><div class="legend-dot" style="background:var(--accent)"></div>縦線 = 週次目標ライン</div>
    <div class="legend-item"><div class="legend-dot" style="background:var(--success)"></div>達成率 ≥ 100%</div>
    <div class="legend-item"><div class="legend-dot" style="background:var(--warning)"></div>80〜99%</div>
    <div class="legend-item"><div class="legend-dot" style="background:var(--danger)"></div>&lt; 80%</div>
  </div>

</div><!-- /wrap -->

<script>
// ===== 埋め込みデータ =====
const DATA = {{EMBEDDED_DATA}};

// ===== KPI 描画 =====
function renderKPI() {
  const k = DATA.kpi;
  const grid = document.getElementById('kpi-grid');

  function clsCls(val, tgt) {
    return val >= tgt ? 'success' : val >= tgt * 0.8 ? 'warn' : 'danger';
  }

  function card(label, value, sub, cls, icon, gaugePct, gaugeLabel) {
    const pctW = Math.min(gaugePct ?? 0, 100).toFixed(1);
    const colMap = { success: 'var(--success)', warn: 'var(--warning)', danger: 'var(--danger)', flat: 'var(--text-muted)' };
    const gaugeHtml = gaugePct !== null ? `
      <div class="kpi-gauge-wrap"><div class="kpi-gauge-fill ${cls}" style="width:${pctW}%"></div></div>
      <div class="kpi-gauge-pct" style="color:${colMap[cls]}">${gaugeLabel}</div>
    ` : '';
    return `
      <div class="kpi-card ${cls}">
        <div class="kpi-card-header">
          <div class="kpi-label">${label}</div>
          <div class="kpi-icon">${icon}</div>
        </div>
        <div class="kpi-value ${cls}">${value}</div>
        <div class="kpi-sub">${sub}</div>
        ${gaugeHtml}
      </div>`;
  }

  const fyCls  = clsCls(k.fy_avg,   k.target_weekly);
  const rateCls= clsCls(k.fy_rate,  100);
  const d7Cls  = clsCls(k.actual_7d, k.target_weekly);
  const r7Cls  = clsCls(k.rate_7d,  100);

  const fyIcon   = fyCls  ==='success'?'✅':fyCls  ==='warn'?'⚠️':'🔴';
  const rateIcon = rateCls==='success'?'✅':rateCls==='warn'?'⚠️':'🔴';
  const d7Icon   = d7Cls  ==='success'?'✅':d7Cls  ==='warn'?'⚠️':'🔴';
  const r7Icon   = r7Cls  ==='success'?'✅':r7Cls  ==='warn'?'⚠️':'🔴';

  const prevRatio = k.prev_avg ? ((k.fy_avg / k.prev_avg) * 100).toFixed(1) + '%' : '―';

  grid.innerHTML = [
    card(k.fy_year + '（週平均）',
         k.fy_avg.toFixed(1) + ' 人',
         '目標 ' + k.target_weekly + ' 人/週',
         fyCls, fyIcon,
         k.fy_avg / k.target_weekly * 100,
         k.fy_avg.toFixed(1) + ' / ' + k.target_weekly + ' 人/週'),
    card(k.fy_year + ' 達成率',
         k.fy_rate.toFixed(1) + '%',
         k.fy_avg.toFixed(1) + ' / ' + k.target_weekly + ' 人/週',
         rateCls, rateIcon,
         k.fy_rate,
         k.fy_rate.toFixed(1) + '%'),
    card('直近7日 合計',
         k.actual_7d + ' 人',
         '目標換算 ' + k.target_weekly + ' 人/週',
         d7Cls, d7Icon,
         k.actual_7d / k.target_weekly * 100,
         (k.actual_7d / k.target_weekly * 100).toFixed(1) + '%'),
    card('直近7日 達成率',
         k.rate_7d.toFixed(1) + '%',
         '日平均 ' + (k.actual_7d / 7).toFixed(1) + ' 人/日',
         r7Cls, r7Icon,
         k.rate_7d,
         k.rate_7d.toFixed(1) + '%'),
    card((k.prev_year || '昨年度') + '（週平均）',
         k.prev_avg !== null ? k.prev_avg.toFixed(1) + ' 人' : '―',
         k.prev_avg !== null ? '前年比 ' + prevRatio : 'データ未入力',
         'flat', '―',
         k.prev_avg !== null ? k.prev_avg / k.target_weekly * 100 : null,
         k.prev_avg !== null ? (k.prev_avg / k.target_weekly * 100).toFixed(1) + '%' : ''),
  ].join('');
}

// ===== 推移グラフ =====
let chartOpen = true;
let mainChartInstance = null;
let currentChartView = 'dept';   // 'dept' | 'ward'
let currentChartKey  = '__all__';
let currentRange     = 'w24';

function toggleChart() {
  chartOpen = !chartOpen;
  document.getElementById('chart-container').style.display = chartOpen ? '' : 'none';
  document.getElementById('chart-toggle-icon').textContent = chartOpen ? '▲ 閉じる' : '▼ 開く';
}

function buildChartSelector(view) {
  const sel  = document.getElementById('chart-dept-select');
  const keys = view === 'ward'
    ? (DATA.ward_targets ? Object.keys(DATA.ward_targets) : [])
    : (DATA.dept_targets ? Object.keys(DATA.dept_targets) : []);
  const label = view === 'ward' ? '🏨 病棟全体' : '🏥 病院全体';
  let html = `<option value="__all__">${label}</option>`;
  keys.sort().forEach(k => { html += `<option value="${k}">${k}</option>`; });
  sel.innerHTML = html;
  sel.value = currentChartKey;
}

function rangeFrom(rangeKey) {
  const m = DATA.meta;
  if (rangeKey === 'w24')  return m.w24_from;
  if (rangeKey === 'fy')   return m.fy_from;
  if (rangeKey === 'y365') return m.y365_from;
  return m.w24_from;
}

function sliceByRange(cd, fromStr) {
  return cd.filter(d => d.d >= fromStr);
}

function niceStep(span) {
  const candidates = [0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50, 100];
  for (const s of candidates) {
    if (span / s <= 8) return s;
  }
  return 100;
}

function calcYAxis(cd, tgtVal, prevVal) {
  const maVals  = cd.map(d => d.ma).filter(v => v > 0);
  const refVals = maVals.concat([tgtVal, prevVal]).filter(v => v != null && v > 0);
  if (!refVals.length) return { yMin: 0, yMax: tgtVal * 2 };
  const rawMax = Math.max(...refVals) * 1.10;
  const rawMin = Math.min(...refVals) * 0.90;
  const step   = niceStep(rawMax - Math.max(0, rawMin));
  const yMax   = Math.ceil(rawMax  / step) * step;
  const yMin   = Math.max(0, Math.floor(rawMin / step) * step);
  return { yMin, yMax };
}

function getChartConfig(view, key, rangeKey) {
  const fromStr  = rangeFrom(rangeKey);
  const accentC  = '#3A6EA5';
  const accentBg = 'rgba(58,110,165,0.08)';

  // ── 病院/病棟全体 ──
  if (key === '__all__') {
    const cd   = sliceByRange(DATA.chart, fromStr);
    const tgt  = DATA.kpi.target_weekly / 7;
    const prev = DATA.kpi.prev_avg !== null ? DATA.kpi.prev_avg / 7 : null;
    const { yMin, yMax } = calcYAxis(cd, tgt, prev);
    const labels = cd.map(d => d.d.slice(5).replace('-', '/'));
    const datasets = [
      { label: '7日移動平均', data: cd.map(d => d.ma7),
        borderColor: accentC, backgroundColor: accentBg,
        borderWidth: 1.6, pointRadius: 0, pointHoverRadius: 3, fill: true, tension: 0.3 },
      { label: '28日移動平均', data: cd.map(d => d.ma),
        borderColor: accentC, backgroundColor: 'transparent',
        borderWidth: 2.5, pointRadius: 0, pointHoverRadius: 4, fill: false, tension: 0.3,
        borderDash: [5, 3] },
      { label: '目標 ' + DATA.kpi.target_weekly + '人/週',
        data: Array(cd.length).fill(tgt),
        borderColor: '#C0293B', borderDash: [6,4], borderWidth: 1.8, pointRadius: 0, fill: false }
    ];
    if (prev !== null) datasets.push({
      label: DATA.kpi.prev_year + ' ' + DATA.kpi.prev_avg + '人/週',
      data: Array(cd.length).fill(prev),
      borderColor: '#1A9E6A', borderDash: [4,4], borderWidth: 1.5, pointRadius: 0, fill: false
    });
    return { labels, datasets, yMin, yMax };
  }

  // ── 診療科個別 ──
  if (view === 'dept') {
    const cd   = sliceByRange(DATA.dept_chart[key], fromStr);
    const info = DATA.dept_targets[key];
    const tgt  = info.weekly / 7;
    const prev = info.prev_daily;
    const { yMin, yMax } = calcYAxis(cd, tgt, prev);
    const labels = cd.map(d => d.d.slice(5).replace('-', '/'));
    const datasets = [
      { label: '7日移動平均', data: cd.map(d => d.ma7),
        borderColor: accentC, backgroundColor: accentBg,
        borderWidth: 1.6, pointRadius: 0, pointHoverRadius: 3, fill: true, tension: 0.3 },
      { label: '28日移動平均', data: cd.map(d => d.ma),
        borderColor: accentC, backgroundColor: 'transparent',
        borderWidth: 2.5, pointRadius: 0, pointHoverRadius: 4, fill: false, tension: 0.3,
        borderDash: [5, 3] },
      { label: '目標 ' + info.weekly + '人/週',
        data: Array(cd.length).fill(tgt),
        borderColor: '#C0293B', borderDash: [6,4], borderWidth: 1.8, pointRadius: 0, fill: false }
    ];
    if (prev !== null) datasets.push({
      label: DATA.kpi.prev_year + ' ' + (prev * 7).toFixed(1) + '人/週',
      data: Array(cd.length).fill(prev),
      borderColor: '#1A9E6A', borderDash: [4,4], borderWidth: 1.5, pointRadius: 0, fill: false
    });
    return { labels, datasets, yMin, yMax };
  }

  // ── 病棟個別 ──
  const cd   = sliceByRange(DATA.ward_chart[key], fromStr);
  const info = DATA.ward_targets[key];
  const tgt  = info.weekly / 7;
  const prev = info.prev_daily;
  const { yMin, yMax } = calcYAxis(cd, tgt, prev);
  const labels = cd.map(d => d.d.slice(5).replace('-', '/'));
  const datasets = [
    { label: '7日移動平均', data: cd.map(d => d.ma7),
      borderColor: accentC, backgroundColor: accentBg,
      borderWidth: 1.6, pointRadius: 0, pointHoverRadius: 3, fill: true, tension: 0.3 },
    { label: '28日移動平均', data: cd.map(d => d.ma),
      borderColor: accentC, backgroundColor: 'transparent',
      borderWidth: 2.5, pointRadius: 0, pointHoverRadius: 4, fill: false, tension: 0.3,
      borderDash: [5, 3] },
    { label: '目標 ' + info.weekly + '人/週',
      data: Array(cd.length).fill(tgt),
      borderColor: '#C0293B', borderDash: [6,4], borderWidth: 1.8, pointRadius: 0, fill: false }
  ];
  if (prev !== null) datasets.push({
    label: DATA.kpi.prev_year + ' ' + (prev * 7).toFixed(1) + '人/週',
    data: Array(cd.length).fill(prev),
    borderColor: '#1A9E6A', borderDash: [4,4], borderWidth: 1.5, pointRadius: 0, fill: false
  });
  return { labels, datasets, yMin, yMax };
}

function applyChart() {
  if (!mainChartInstance) return;
  const cfg = getChartConfig(currentChartView, currentChartKey, currentRange);
  mainChartInstance.data.labels   = cfg.labels;
  mainChartInstance.data.datasets = cfg.datasets;
  mainChartInstance.options.scales.y.min = cfg.yMin;
  mainChartInstance.options.scales.y.max = cfg.yMax;
  mainChartInstance.update('none');
}

// プルダウン値変更
function switchChartKey(key) { currentChartKey = key; applyChart(); }

// 診療科/病棟ビュー切替 → プルダウンを差し替えて「全体」にリセット
function switchChartView(view) {
  currentChartView = view;
  currentChartKey  = '__all__';
  document.querySelectorAll('.chart-view-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('cvbtn-' + view).classList.add('active');
  buildChartSelector(view);
  applyChart();
}

function switchChartRange(key) {
  currentRange = key;
  document.querySelectorAll('.range-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('rbtn-' + key).classList.add('active');
  applyChart();
}

function renderChart() {
  buildChartSelector(currentChartView);
  const cfg    = getChartConfig(currentChartView, currentChartKey, currentRange);
  const canvas = document.getElementById('mainChart');
  if (mainChartInstance) { mainChartInstance.destroy(); mainChartInstance = null; }
  mainChartInstance = new Chart(canvas, {
    type: 'line',
    data: { labels: cfg.labels, datasets: cfg.datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { display: true, position: 'top',
          labels: { usePointStyle: true, padding: 14,
            font: { size: 11, family: "'Noto Sans JP', sans-serif" } } },
        tooltip: { backgroundColor: 'rgba(26,37,53,0.92)', padding: 10,
          bodyFont: { size: 12 }, titleFont: { size: 12 } }
      },
      scales: {
        x: { ticks: { font: { size: 10, family: "'IBM Plex Mono', monospace" },
               maxRotation: 45, maxTicksLimit: 24 }, grid: { display: false } },
        y: { min: cfg.yMin, max: cfg.yMax,
             ticks: { font: { size: 11, family: "'IBM Plex Mono', monospace" } },
             grid: { color: 'rgba(0,0,0,0.04)' } }
      }
    }
  });
}

// ===== パフォーマンスバー HTML 生成 =====
function buildPerfRows(rows) {
  if (!rows || rows.length === 0) return '<p style="color:var(--text-muted);font-size:0.82rem;padding:10px 0;">データなし</p>';
  const maxAct = Math.max(...rows.map(r => r.actual), ...rows.map(r => r.target), 1);
  return rows.map(r => {
    const cls     = r.rate >= 100 ? 'achieved' : (r.rate >= 80 ? 'near' : 'below');
    const fillPct = Math.min(r.actual / maxAct * 100, 100).toFixed(1);
    const tgtPct  = Math.min(r.target / maxAct * 100, 100).toFixed(1);
    return `<div class="perf-row">
      <div class="perf-name ${cls}" title="${r.name}">${r.name}</div>
      <div class="perf-nums">${r.actual}人/週 / 目標${r.target}人</div>
      <div class="perf-bar-wrap">
        <div class="perf-bar-fill ${cls}" style="width:${fillPct}%"></div>
        <div class="perf-target-line" style="left:${tgtPct}%"></div>
      </div>
      <div class="perf-rate">${r.rate}%</div>
      <div class="perf-dot ${cls}"></div>
    </div>`;
  }).join('');
}

function renderPerf() {
  const p = DATA.perf;
  ['7','28','fy'].forEach(t => {
    document.getElementById('dept-'+t).innerHTML = buildPerfRows(p.dept[t]);
    document.getElementById('ward-'+t).innerHTML = buildPerfRows(p.ward[t]);
  });
}

// ===== タブラベル =====
function setLabels() {
  const m = DATA.meta;
  document.getElementById('lbl-7').textContent  = m.d7_from  +' 〜 '+ m.date_max;
  document.getElementById('lbl-28').textContent = m.d28_from +' 〜 '+ m.date_max;
  document.getElementById('lbl-fy').textContent = m.fy_year  +' ('+ m.fy_from +' 〜 '+ m.date_max +')';
}

// ===== 切替 =====
let currentView = 'dept';
let currentTab  = '7';

function switchView(v) {
  currentView = v;
  document.getElementById('perf-dept').style.display = v==='dept' ? '' : 'none';
  document.getElementById('perf-ward').style.display = v==='ward' ? '' : 'none';
  document.querySelectorAll('.view-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('vbtn-'+v).classList.add('active');
}

function switchTab(t) {
  currentTab = t;
  ['7','28','fy'].forEach(x => {
    const panels = document.querySelectorAll('#dept-'+x+', #ward-'+x);
    panels.forEach(p => p.classList.toggle('active', x===t));
    document.getElementById('tbtn-'+x).classList.toggle('active', x===t);
  });
}

// ===== 初期化 =====
renderKPI();
renderChart();
renderPerf();
setLabels();
</script>
</body>
</html>
"""


# ============================================================
# Python 集計ロジック
# ============================================================

def load_and_process_from_dir(data_dir: str, prev_avg_weekly=None):
    """
    data/patient_data/ と data/patient_target/ から直接読み込んで集計する。
    generate_html.py の make deploy 統合用。Streamlit版の load_and_process とは独立。

    【重複除去】
    data_loader.py の load_admission_data() / _merge_admission_files() を
    そのまま再利用することで、既存ダッシュボードと完全に同一のマージ戦略を適用する。
      - ファイルは更新日時昇順（古い順）に読み込む
      - 全列が完全一致する行のみ除去（正当な複数行は保持）
      - 同一(日付・病棟・診療科)でも値が異なる行は両方保持
    """
    import sys
    from pathlib import Path as _Path

    # data_loader.py を import（admission_app.py と同じディレクトリ or app/lib/ に存在）
    _root = _Path(__file__).parent
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))

    # app/lib/data_loader を優先、なければルート直下の data_loader を使用
    try:
        from app.lib.data_loader import (
            load_admission_data as _load_adm,
            load_inpatient_targets as _load_tgt,
        )
    except ModuleNotFoundError:
        from data_loader import (
            load_admission_data as _load_adm,
            load_inpatient_targets as _load_tgt,
        )

    # ── 実績: data_loader の完全なマージ戦略を使用 ──
    # _list_files が mtime 昇順で読み込み → _merge_admission_files が全列一致重複のみ除去
    df = _load_adm(data_dir)

    # ── 目標: data_loader の load_inpatient_targets を使用 ──
    df_target = _load_tgt(data_dir)

    # 以降は共通処理関数で集計
    return _process_admission_df(df, df_target, prev_avg_weekly)


def load_and_process(log_files, target_file, prev_avg_weekly=None):
    """
    Streamlit版: アップロードされたファイルオブジェクトから読み込んで集計する。
    prev_avg_weekly: 昨年度の週平均（人/週）。Noneの場合は「データなし」表示。
    """
    # 目標読み込み
    target_bytes = target_file.read()
    df_target = pd.read_csv(io.BytesIO(target_bytes), encoding='utf-8-sig')

    # 実績読み込み
    dfs = []
    for f in log_files:
        if f.name.endswith('.xlsx'):
            dfs.append(pd.read_excel(f))
        else:
            dfs.append(pd.read_csv(f, encoding='utf-8-sig'))
    df = pd.concat(dfs, ignore_index=True)
    df['日付'] = pd.to_datetime(df['日付'])

    # クリーニング後に共通集計関数へ委譲
    df = df[df['診療科名'].notna()]
    if '転入患者数' not in df.columns:
        df['転入患者数'] = 0
    df['転入患者数'] = df['転入患者数'].fillna(0)

    # ① 複数ファイル読み込み時の完全重複行を除去（同一ファイルを2回アップした場合の2倍バグ対策）
    df = df.drop_duplicates()
    return _process_admission_df(df, df_target, prev_avg_weekly)


def _process_admission_df(df: pd.DataFrame, df_target: pd.DataFrame, prev_avg_weekly=None):
    """
    前処理済み DataFrame + 目標 DataFrame から KPI / チャート / パフォーマンスを集計して返す。
    load_and_process（Streamlit版）と load_and_process_from_dir（CLI版）の共通処理。
    """
    df = df[df['診療科名'].notna()]
    if '転入患者数' not in df.columns:
        df['転入患者数'] = 0
    df['転入患者数'] = df['転入患者数'].fillna(0)

    # ② 目標ファイルにない診療科（内科・健診センター・感染症・放射線診断科等）をすべて「総合内科」へ統合
    # 「週間新入院患者数」が設定されている診療科のみを「目標設定済み」とみなす
    target_depts = set(
        df_target[
            (df_target['部門種別'] == '診療科') &
            (df_target['指標タイプ'] == '週間新入院患者数') &
            (~df_target['部門名'].isin(['病院全体', '内科']))
        ]['部門名'].tolist()
    )
    df['診療科名'] = df['診療科名'].apply(
        lambda x: x if x in target_depts else '総合内科'
    )

    # 同一日付×病棟×診療科の合計（小児科等の分割出力対応）
    df = df.groupby(['日付', '病棟コード', '診療科名'], as_index=False)[
        ['入院患者数', '緊急入院患者数', '転入患者数']
    ].sum()

    # 診療科：入院＋緊急入院のみ、病棟：入院＋緊急入院＋転入
    df['実績_診療科'] = df['入院患者数'] + df['緊急入院患者数']
    df['実績_病棟']   = df['入院患者数'] + df['緊急入院患者数'] + df['転入患者数']

    ward_mapping = {
        '02A':'2階A病棟','02B':'2階B病棟','03A':'3階A病棟','03B':'3階B病棟',
        '04A':'4階A病棟','04B':'4B-ICU','04C':'4階C病棟','04D':'4B-HCU',
        '05A':'5階A病棟','05B':'5階B病棟','06A':'6階A病棟','06B':'6階B病棟',
        '07A':'7階A病棟','07B':'7階B病棟','08A':'8階A病棟','08B':'8階B病棟',
        '09A':'9階A病棟','09B':'9階B病棟'
    }
    df['病棟名'] = df['病棟コード'].map(ward_mapping)

    date_max = df['日付'].max()
    date_min = df['日付'].min()

    d7_from  = date_max - pd.Timedelta(days=6)
    d28_from = date_max - pd.Timedelta(days=27)

    # 今年度（4月始まり）
    fy_year  = date_max.year if date_max.month >= 4 else date_max.year - 1
    fy_from  = pd.Timestamp(f"{fy_year}-04-01")
    fy_from  = max(fy_from, date_min)
    fy_days  = (date_max - fy_from).days + 1
    fy_weeks = fy_days / 7.0

    # 前年度（データがある範囲のみ）
    prev_year    = fy_year - 1
    prev_fy_from = pd.Timestamp(f"{prev_year}-04-01")
    prev_fy_to   = pd.Timestamp(f"{fy_year}-03-31")
    prev_fy_from_actual = max(prev_fy_from, date_min)
    prev_fy_to_actual   = min(prev_fy_to,   date_max)
    if prev_fy_from_actual <= prev_fy_to_actual:
        prev_fy_days  = (prev_fy_to_actual - prev_fy_from_actual).days + 1
        prev_fy_weeks = prev_fy_days / 7.0
        prev_mask     = (df['日付'] >= prev_fy_from_actual) & (df['日付'] <= prev_fy_to_actual)
        prev_actual   = int(df[prev_mask].groupby('日付')['実績_診療科'].sum().sum())
        prev_avg_data = prev_actual / prev_fy_weeks   # データから計算した前年度週平均
    else:
        prev_avg_data = None   # 前年度データなし

    # 引数で渡された prev_avg_weekly を優先、なければデータから計算
    if prev_avg_weekly is not None:
        prev_avg = prev_avg_weekly
    elif prev_avg_data is not None:
        prev_avg = prev_avg_data
    else:
        prev_avg = None

    # --- 病院全体目標（週間新入院患者数・診療科・全日） ---
    row_hosp = df_target[
        (df_target['部門名']   == '病院全体') &
        (df_target['部門種別'] == '診療科') &
        (df_target['指標タイプ'] == '週間新入院患者数') &
        (df_target['期間区分'] == '全日')
    ]
    target_weekly = float(row_hosp['目標値'].values[0]) if len(row_hosp) else 385.0

    # --- KPI（今年度ベース） ---
    fy_mask   = (df['日付'] >= fy_from) & (df['日付'] <= date_max)
    fy_actual = int(df[fy_mask].groupby('日付')['実績_診療科'].sum().sum())
    fy_avg    = fy_actual / fy_weeks
    fy_rate   = fy_avg / target_weekly * 100

    actual_7d = int(df[df['日付'] >= d7_from]['実績_診療科'].sum())
    rate_7d   = actual_7d / target_weekly * 100

    kpi = {
        'target_weekly': round(target_weekly, 1),
        'fy_avg':        round(fy_avg, 1),
        'fy_rate':       round(fy_rate, 1),
        'actual_7d':     actual_7d,
        'rate_7d':       round(rate_7d, 1),
        'prev_avg':      round(prev_avg, 1) if prev_avg is not None else None,
        'fy_year':       f"{fy_year}年度",
        'prev_year':     f"{prev_year}年度",
    }

    # --- 時系列（直近365日を上限に格納・MA28は全期間から計算） ---
    all_dates_idx  = pd.date_range(date_min, date_max)
    daily_all_full = df.groupby('日付')['実績_診療科'].sum().reindex(all_dates_idx).fillna(0)
    ma7_all_full   = daily_all_full.rolling(7,  min_periods=1).mean()
    ma28_all_full  = daily_all_full.rolling(28, min_periods=1).mean()

    w24_from  = date_max - pd.Timedelta(days=167)
    y365_from = date_max - pd.Timedelta(days=364)
    w24_from_act  = max(w24_from,  date_min)
    y365_from_act = max(y365_from, date_min)

    # 格納範囲は直近365日（今年度・直近24週は必ずこの範囲内）
    y365_dates = pd.date_range(y365_from_act, date_max)
    chart_data = [
        {'d': dt.strftime('%Y-%m-%d'), 'v': int(daily_all_full[dt]),
         'ma': round(float(ma28_all_full[dt]), 2), 'ma7': round(float(ma7_all_full[dt]), 2)}
        for dt in y365_dates
    ]

    # --- パフォーマンス集計 ---
    # 「週間新入院患者数」「全日」行のみ使用（乳腺外科等が複数行持つ場合でも1行に絞れる）
    tgt_dept = df_target[
        (df_target['部門種別']  == '診療科') &
        (df_target['指標タイプ'] == '週間新入院患者数') &
        (df_target['期間区分']  == '全日') &
        (df_target['部門名'] != '病院全体') &
        (df_target['部門名'] != '内科')
    ].copy()
    tgt_ward = df_target[
        (df_target['部門種別']  == '病棟') &
        (df_target['指標タイプ'] == '週間新入院患者数') &
        (df_target['期間区分']  == '全日') &
        (df_target['部門名'] != '病院全体')
    ].copy()

    def calc_perf(df_sub, group_col, val_col, df_tgt, from_date, to_date):
        days  = (to_date - from_date).days + 1
        weeks = days / 7.0
        mask  = (df_sub['日付'] >= from_date) & (df_sub['日付'] <= to_date)
        actuals = df_sub[mask].groupby(group_col)[val_col].sum() / weeks
        rows = []
        for _, r in df_tgt.iterrows():
            act  = float(actuals.get(r['部門名'], 0))
            tgt  = float(r['目標値'])
            rate = round(act / tgt * 100, 1) if tgt > 0 else 0
            rows.append({'name': r['部門名'], 'actual': round(act, 1), 'target': tgt, 'rate': rate})
        rows.sort(key=lambda x: -x['rate'])
        return rows

    perf = {
        'dept': {
            '7':  calc_perf(df, '診療科名', '実績_診療科', tgt_dept, d7_from,  date_max),
            '28': calc_perf(df, '診療科名', '実績_診療科', tgt_dept, d28_from, date_max),
            'fy': calc_perf(df, '診療科名', '実績_診療科', tgt_dept, fy_from,  date_max),
        },
        'ward': {
            '7':  calc_perf(df, '病棟名', '実績_病棟', tgt_ward, d7_from,  date_max),
            '28': calc_perf(df, '病棟名', '実績_病棟', tgt_ward, d28_from, date_max),
            'fy': calc_perf(df, '病棟名', '実績_病棟', tgt_ward, fy_from,  date_max),
        }
    }

    # --- 診療科別 時系列（直近365日）＋目標・前年度 ---
    dept_chart   = {}
    dept_targets = {}
    for _, trow in tgt_dept.iterrows():
        dname  = trow['部門名']
        weekly = float(trow['目標値'])
        df_d   = df[df['診療科名'] == dname].copy()

        # 前年度週平均（診療科別）
        if prev_fy_from_actual <= prev_fy_to_actual:
            pm = (df_d['日付'] >= prev_fy_from_actual) & (df_d['日付'] <= prev_fy_to_actual)
            p_sum = float(df_d[pm].groupby('日付')['実績_診療科'].sum().sum())
            prev_fy_weeks_d = ((prev_fy_to_actual - prev_fy_from_actual).days + 1) / 7.0
            prev_daily_d = round(p_sum / prev_fy_weeks_d / 7, 3)
        else:
            prev_daily_d = None

        # 全期間MA28計算 → 直近365日分のみ格納
        all_d  = df_d.groupby('日付')['実績_診療科'].sum().reindex(all_dates_idx).fillna(0)
        ma7_d  = all_d.rolling(7,  min_periods=1).mean()
        ma28_d = all_d.rolling(28, min_periods=1).mean()
        dept_chart[dname] = [
            {'d': dt.strftime('%Y-%m-%d'), 'v': int(all_d[dt]),
             'ma': round(float(ma28_d[dt]), 2), 'ma7': round(float(ma7_d[dt]), 2)}
            for dt in y365_dates
        ]
        dept_targets[dname] = {'weekly': weekly, 'prev_daily': prev_daily_d}

    # --- 病棟別 時系列（直近365日）＋目標・前年度 ---
    ward_chart   = {}
    ward_targets = {}
    for _, trow in tgt_ward.iterrows():
        wname  = trow['部門名']
        weekly = float(trow['目標値'])
        df_w   = df[df['病棟名'] == wname].copy()

        # 前年度週平均（病棟別）
        if prev_fy_from_actual <= prev_fy_to_actual:
            pm    = (df_w['日付'] >= prev_fy_from_actual) & (df_w['日付'] <= prev_fy_to_actual)
            p_sum = float(df_w[pm].groupby('日付')['実績_病棟'].sum().sum())
            prev_fy_weeks_w = ((prev_fy_to_actual - prev_fy_from_actual).days + 1) / 7.0
            prev_daily_w = round(p_sum / prev_fy_weeks_w / 7, 3)
        else:
            prev_daily_w = None

        # 全期間MA28計算 → 直近365日分のみ格納
        all_w  = df_w.groupby('日付')['実績_病棟'].sum().reindex(all_dates_idx).fillna(0)
        ma7_w  = all_w.rolling(7,  min_periods=1).mean()
        ma28_w = all_w.rolling(28, min_periods=1).mean()
        ward_chart[wname] = [
            {'d': dt.strftime('%Y-%m-%d'), 'v': int(all_w[dt]),
             'ma': round(float(ma28_w[dt]), 2), 'ma7': round(float(ma7_w[dt]), 2)}
            for dt in y365_dates
        ]
        ward_targets[wname] = {'weekly': weekly, 'prev_daily': prev_daily_w}

    meta = {
        'date_min':  date_min.strftime('%Y-%m-%d'),
        'date_max':  date_max.strftime('%Y-%m-%d'),
        'd7_from':   d7_from.strftime('%Y-%m-%d'),
        'd28_from':  d28_from.strftime('%Y-%m-%d'),
        'fy_from':   fy_from.strftime('%Y-%m-%d'),
        'fy_year':   f"{fy_year}年度",
        'prev_year': f"{prev_year}年度",
        'w24_from':  w24_from_act.strftime('%Y-%m-%d'),
        'y365_from': y365_from_act.strftime('%Y-%m-%d'),
        'chart_from': w24_from_act.strftime('%Y-%m-%d'),
        'prev_avg_source': 'manual' if prev_avg_weekly is not None else ('data' if prev_avg_data is not None else 'none'),
    }

    return kpi, chart_data, perf, meta, dept_chart, dept_targets, ward_chart, ward_targets


def generate_html(kpi, chart_data, perf, meta, dept_chart, dept_targets, ward_chart, ward_targets):
    import math

    # 縦軸レンジ：MA28・目標・前年度ラインをすべて含む・動的刻み
    w24_str    = meta['w24_from']
    tgt_daily  = kpi['target_weekly'] / 7.0
    prev_daily = kpi['prev_avg'] / 7.0 if kpi['prev_avg'] is not None else None
    w24_cd     = [d for d in chart_data if d['d'] >= w24_str]
    ma_vals    = [d['ma'] for d in w24_cd if d['ma'] > 0]
    if not ma_vals:
        ma_vals = [tgt_daily]
    ref_vals = ma_vals + [tgt_daily] + ([prev_daily] if prev_daily else [])
    raw_max  = max(ref_vals) * 1.10
    raw_min  = min(ref_vals) * 0.90
    span     = raw_max - max(0, raw_min)
    def nice_step(s):
        for st in [0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50, 100]:
            if s / st <= 8:
                return st
        return 100
    step  = nice_step(span)
    y_max = math.ceil(raw_max  / step) * step
    y_min = max(0, math.floor(max(0, raw_min) / step) * step)

    chart_axis = {'yMin': y_min, 'yMax': y_max}

    data_json = json.dumps({
        'kpi': kpi, 'chart': chart_data, 'chart_axis': chart_axis,
        'perf': perf, 'meta': meta,
        'dept_chart': dept_chart, 'dept_targets': dept_targets,
        'ward_chart': ward_chart, 'ward_targets': ward_targets,
    }, ensure_ascii=False)

    now_str    = datetime.now().strftime('%Y-%m-%d %H:%M')
    date_range = f"{meta['date_min']} 〜 {meta['date_max']}"

    html = HTML_TEMPLATE
    html = html.replace('{{DATE_RANGE}}',    date_range)
    html = html.replace('{{DATE_MAX}}',      meta['date_max'])
    html = html.replace('{{GENERATED_AT}}',  now_str)
    html = html.replace('{{EMBEDDED_DATA}}', data_json)
    return html


# ============================================================
# Streamlit UI
# ============================================================


if __name__ == "__main__":
    import streamlit as st
    st.set_page_config(page_title="新入院ダッシュボード生成", layout="centered")

    st.title("🏥 新入院患者ダッシュボード 生成ツール")
    st.markdown("実績データ（xlsx / csv）と目標値ファイル（csv）をアップロードすると、HTMLダッシュボードを生成してダウンロードできます。")

    with st.sidebar:
        st.header("📂 ファイルアップロード")
        log_files   = st.file_uploader("実績データ（複数可）", type=["xlsx", "csv"], accept_multiple_files=True)
        target_file = st.file_uploader("目標値CSV", type=["csv"])
        st.divider()
        st.header("📈 昨年度実績（任意）")
        prev_input  = st.number_input(
            "昨年度 週平均新入院数（人/週）",
            min_value=0.0, max_value=9999.0, value=0.0, step=0.1,
            help="入力するとKPIに前年比が表示されます。0のまま生成すると「データ未入力」と表示されます。"
        )
        prev_avg_weekly = prev_input if prev_input > 0 else None
        generate_btn = st.button("🚀 HTMLを生成する", type="primary", disabled=not (log_files and target_file))

    if log_files and target_file:
        if generate_btn:
            with st.spinner("集計・HTML生成中..."):
                try:
                    kpi, chart_data, perf, meta, dept_chart, dept_targets, ward_chart, ward_targets = load_and_process(log_files, target_file, prev_avg_weekly)
                    html_str = generate_html(kpi, chart_data, perf, meta, dept_chart, dept_targets, ward_chart, ward_targets)

                    st.session_state['html_ready'] = html_str
                    st.session_state['kpi']  = kpi
                    st.session_state['meta'] = meta
                    st.success("✅ HTML生成完了！")
                except Exception as e:
                    st.error(f"エラーが発生しました: {e}")
                    st.exception(e)

    if 'html_ready' in st.session_state:
        html_str = st.session_state['html_ready']
        kpi  = st.session_state['kpi']
        meta = st.session_state['meta']

        # docsフォルダへ自動保存
        import os
        docs_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "admission")
        os.makedirs(docs_dir, exist_ok=True)
        save_path = os.path.join(docs_dir, "index.html")
        with open(save_path, "w", encoding="utf-8") as f:
            f.write(html_str)
        st.success(f"📁 `docs/admission/index.html` に保存しました（{save_path}）")

        # ダウンロードボタン
        fname = "admission_index.html"
        st.download_button(
            label="⬇️ HTMLファイルをダウンロード",
            data=html_str.encode('utf-8'),
            file_name=fname,
            mime="text/html",
            type="primary"
        )

        # プレビュー KPI
        st.subheader("📊 集計結果プレビュー")
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric(kpi['fy_year'] + "（週平均）", f"{kpi['fy_avg']:.1f}人", f"目標 {kpi['target_weekly']}/週")
        c2.metric(kpi['fy_year'] + " 達成率",    f"{kpi['fy_rate']:.1f}%")
        c3.metric("直近7日 合計",                f"{kpi['actual_7d']}人")
        c4.metric("直近7日 達成率",              f"{kpi['rate_7d']:.1f}%")
        prev_disp = f"{kpi['prev_avg']:.1f}人" if kpi['prev_avg'] is not None else "未入力"
        c5.metric(kpi.get('prev_year','昨年度') + "（週平均）", prev_disp)

        st.info(f"今年度: {meta['fy_from']} 〜 {meta['date_max']}  ／  データ全期間: {meta['date_min']} 〜 {meta['date_max']}")

        # HTMLプレビュー（iframe）
        with st.expander("🔍 HTMLプレビュー（iframe）", expanded=False):
            st.components.v1.html(html_str, height=800, scrolling=True)

    else:
        st.info("👈 サイドバーからファイルをアップロードし、「HTMLを生成する」ボタンを押してください。")
        with st.expander("📌 使い方"):
            st.markdown("""
            1. **実績データ**（`入退院クロス_新入院.xlsx` など）をアップロード
            2. **目標値CSV**（`新入院患者_目標値.csv`）をアップロード
            3. 昨年度の週平均新入院数を入力（任意）
            4. 「🚀 HTMLを生成する」をクリック
            5. 生成されたHTMLファイルをダウンロードして、ブラウザで開く
            """)
