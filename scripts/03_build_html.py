"""
03_build_html.py — 產出 Dashboard HTML
讀取 analysis_cache.pkl + DB，產出 docs/index.html
"""
import sqlite3
import pickle
import os
import pandas as pd
from datetime import datetime

DB_PATH    = 'data/stock_history.db'
CACHE_PATH = 'data/analysis_cache.pkl'
OUTPUT     = 'docs/index.html'

def load_cache():
    if not os.path.exists(CACHE_PATH):
        return {}
    with open(CACHE_PATH, 'rb') as f:
        return pickle.load(f)

def pct_color(v):
    if v is None: return '#888'
    return '#4ade80' if v > 0 else ('#f87171' if v < 0 else '#888')

def win_color(v):
    if v is None: return '#888'
    return '#4ade80' if v >= 60 else ('#facc15' if v >= 50 else '#f87171')

def fmt_pct(v, digits=1):
    if v is None: return '-'
    sign = '+' if v > 0 else ''
    return f'{sign}{v:.{digits}f}%'

def build_html(cache, conn):
    consec_df  = cache.get('consec', pd.DataFrame())
    new_df     = cache.get('new_entries', pd.DataFrame())
    perf       = cache.get('performance', {})
    generated  = cache.get('generated', '-')

    # ── 頂部統計 ──
    total_rows = conn.execute('SELECT COUNT(*) FROM stock_daily').fetchone()[0]
    trade_days = conn.execute('SELECT COUNT(DISTINCT date) FROM stock_daily').fetchone()[0]
    today_date = conn.execute('SELECT MAX(date) FROM stock_daily').fetchone()[0] or '-'
    today_count= conn.execute(
        'SELECT COUNT(*) FROM stock_daily WHERE date = ?', [today_date]
    ).fetchone()[0]

    t3_win = perf.get('全部', {}).get('t3_win')
    t3_avg = perf.get('全部', {}).get('t3_avg')
    t3_sample = perf.get('全部', {}).get('count', 0)

    # ── 每日統計（最近7天）──
    daily_df = pd.read_sql('''
        SELECT date,
               market,
               COUNT(*) as cnt
        FROM stock_daily
        GROUP BY date, market
        ORDER BY date DESC
        LIMIT 14
    ''', conn)
    daily_pivot = daily_df.pivot_table(
        index='date', columns='market', values='cnt', aggfunc='sum', fill_value=0
    ).reset_index().sort_values('date', ascending=False).head(7)

    # ── 連續入選排行 ──
    top_consec = consec_df.head(15) if not consec_df.empty else pd.DataFrame()

    # ── 新進榜 ──
    top_new = new_df.head(20) if not new_df.empty else pd.DataFrame()

    # ══ HTML 組裝 ══
    now_str = datetime.now().strftime('%Y/%m/%d %H:%M')

    # 績效表格
    def perf_row(cat, data):
        if not data:
            return f'<tr><td>{cat}</td><td colspan="7" style="color:#666">資料不足</td></tr>'
        t1w = f'<span style="color:{win_color(data.get("t1_win"))}">{data.get("t1_win","−")}%</span>' if data.get("t1_win") is not None else '−'
        t1a = f'<span style="color:{pct_color(data.get("t1_avg"))}">{fmt_pct(data.get("t1_avg"))}</span>'
        t3w = f'<span style="color:{win_color(data.get("t3_win"))}">{data.get("t3_win","−")}%</span>' if data.get("t3_win") is not None else '−'
        t3a = f'<span style="color:{pct_color(data.get("t3_avg"))}">{fmt_pct(data.get("t3_avg"))}</span>'
        t5w = f'<span style="color:{win_color(data.get("t5_win"))}">{data.get("t5_win","−")}%</span>' if data.get("t5_win") is not None else '−'
        t5a = f'<span style="color:{pct_color(data.get("t5_avg"))}">{fmt_pct(data.get("t5_avg"))}</span>'
        return f'''<tr>
            <td style="font-weight:700;color:#c9b8ff">{cat}</td>
            <td>{t1w}</td><td>{t1a}</td>
            <td>{t3w}</td><td>{t3a}</td>
            <td>{t5w}</td><td>{t5a}</td>
            <td style="color:#888">{data.get("count","−")}</td>
        </tr>'''

    perf_html = ''.join([
        perf_row(cat, perf.get(cat, {}))
        for cat in ['綜合轉強','強勢確認','起漲預警','全部']
    ])

    # 連續入選表格
    consec_html = ''
    if not top_consec.empty:
        for _, r in top_consec.iterrows():
            market_badge = f'<span style="background:#7c3aed;padding:1px 6px;border-radius:4px;font-size:.75em">{r["market"]}</span>'
            consec_html += f'''<tr>
                <td><b style="color:#c9b8ff">{r["stock_id"]}</b> {r["name"]} {market_badge}</td>
                <td style="color:#facc15;font-weight:700">{int(r["appear_count"])} / 7</td>
                <td style="color:#4ade80">{r["avg_score"]:.1f}</td>
                <td style="color:#fb923c">{r["max_score"]:.1f}</td>
                <td style="color:#888;font-size:.85em">{r["latest_date"]}</td>
            </tr>'''
    else:
        consec_html = '<tr><td colspan="5" style="color:#666;text-align:center">累積中，資料不足</td></tr>'

    # 新進榜表格
    new_html = ''
    if not top_new.empty:
        for _, r in top_new.iterrows():
            sc_badge = ''
            if str(r.get('is_strong_confirm','')).upper() == 'TRUE' and str(r.get('is_early_breakout','')).upper() == 'TRUE':
                sc_badge = '<span style="color:#f59e0b">🔮綜合</span>'
            elif str(r.get('is_strong_confirm','')).upper() == 'TRUE':
                sc_badge = '<span style="color:#fb7185">🔥強勢</span>'
            else:
                sc_badge = '<span style="color:#2dd4bf">🌱起漲</span>'
            new_html += f'''<tr>
                <td><b style="color:#c9b8ff">{r["stock_id"]}</b> {r["name"]}
                    <span style="background:#312e81;padding:1px 5px;border-radius:3px;font-size:.75em">{r["market"]}</span>
                </td>
                <td>{sc_badge}</td>
                <td style="color:#fff">{r["close"]}</td>
                <td style="color:#4ade80;font-weight:700">{r["composite_score"]:.2f}</td>
            </tr>'''
    else:
        new_html = '<tr><td colspan="4" style="color:#666;text-align:center">今日無新進榜（或資料累積中）</td></tr>'

    # 每日統計表格
    daily_html = ''
    for _, r in daily_pivot.iterrows():
        tse_cnt = int(r.get('TSE', 0))
        otc_cnt = int(r.get('OTC', 0))
        total   = tse_cnt + otc_cnt
        daily_html += f'''<tr>
            <td style="color:#888">{r["date"]}</td>
            <td style="color:#fb923c;font-weight:700">{total}</td>
            <td style="color:#60a5fa">{tse_cnt}</td>
            <td style="color:#a78bfa">{otc_cnt}</td>
        </tr>'''

    t3_display = f'{t3_win}%' if t3_win is not None else '累積中'
    t3_avg_display = fmt_pct(t3_avg) if t3_avg is not None else '累積中'
    t3_color = win_color(t3_win)

    html = f'''<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>台股選股 Dashboard</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#0d0d1a;color:#e2e8f0;font-family:'Segoe UI',sans-serif;font-size:14px;line-height:1.6}}
.header{{background:linear-gradient(135deg,#1a1030,#0d1b2a);padding:28px 32px;border-bottom:1px solid #2d2d4e}}
.header h1{{font-size:1.6em;font-weight:900;color:#c9b8ff;letter-spacing:2px}}
.header p{{color:#7c7894;font-size:.85em;margin-top:6px}}
.stats-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:16px;padding:24px 32px;background:#11112a}}
.stat-card{{background:#1a1a35;border:1px solid #2d2d4e;border-radius:10px;padding:18px;text-align:center}}
.stat-label{{font-size:.72em;color:#7c7894;letter-spacing:2px;text-transform:uppercase;margin-bottom:8px}}
.stat-value{{font-size:1.8em;font-weight:900}}
.stat-sub{{font-size:.72em;color:#555;margin-top:4px}}
.sections{{padding:24px 32px;display:grid;grid-template-columns:1fr 1fr;gap:24px}}
@media(max-width:900px){{.sections{{grid-template-columns:1fr}}}}
.section{{background:#1a1a35;border:1px solid #2d2d4e;border-radius:12px;overflow:hidden}}
.section-header{{background:#12122a;padding:16px 20px;border-bottom:1px solid #2d2d4e}}
.section-header h2{{font-size:1em;font-weight:700;color:#c9b8ff}}
.section-header p{{font-size:.78em;color:#7c7894;margin-top:3px}}
table{{width:100%;border-collapse:collapse}}
th{{padding:10px 14px;text-align:left;color:#7c7894;font-size:.75em;letter-spacing:.5px;border-bottom:1px solid #2d2d4e;white-space:nowrap}}
td{{padding:9px 14px;border-bottom:1px solid #1e1e38;font-size:.85em}}
tr:hover td{{background:#1f1f40}}
.full-section{{padding:0 32px 24px;}}
.full-block{{background:#1a1a35;border:1px solid #2d2d4e;border-radius:12px;overflow:hidden;margin-bottom:24px}}
.footer{{text-align:center;padding:24px;color:#555;font-size:.78em;border-top:1px solid #2d2d4e}}
</style>
</head>
<body>

<div class="header">
  <h1>📊 台股選股 Dashboard</h1>
  <p>TSE 上市 + OTC 上櫃 ｜ 資料更新：{now_str} ｜ 累積 {trade_days} 個交易日</p>
</div>

<div class="stats-grid">
  <div class="stat-card">
    <div class="stat-label">累積入選紀錄</div>
    <div class="stat-value" style="color:#c9b8ff">{total_rows}</div>
    <div class="stat-sub">筆（{trade_days} 個交易日）</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">T+3 勝率（全部）</div>
    <div class="stat-value" style="color:{t3_color}">{t3_display}</div>
    <div class="stat-sub">{t3_sample} 筆有效樣本</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">T+3 平均報酬</div>
    <div class="stat-value" style="color:{pct_color(t3_avg)}">{t3_avg_display}</div>
    <div class="stat-sub">入選日收盤為基準</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">今日新進榜</div>
    <div class="stat-value" style="color:#facc15">{len(top_new)}</div>
    <div class="stat-sub">今日首次出現</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">今日入選總數</div>
    <div class="stat-value" style="color:#fb923c">{today_count}</div>
    <div class="stat-sub">{today_date}</div>
  </div>
</div>

<div class="sections">

  <!-- 連續入選排行 -->
  <div class="section">
    <div class="section-header">
      <h2>🔁 連續入選排行（近7日）</h2>
      <p>在最近7個交易日內重複出現次數</p>
    </div>
    <table>
      <tr>
        <th>股票</th><th>出現次數</th><th>平均分</th><th>最高分</th><th>最近日期</th>
      </tr>
      {consec_html}
    </table>
  </div>

  <!-- 今日新進榜 -->
  <div class="section">
    <div class="section-header">
      <h2>🆕 今日新進榜</h2>
      <p>昨天沒有、今天首次出現</p>
    </div>
    <table>
      <tr><th>股票</th><th>類型</th><th>收盤</th><th>綜合分</th></tr>
      {new_html}
    </table>
  </div>

</div>

<div class="full-section">

  <!-- 績效統計 -->
  <div class="full-block">
    <div class="section-header">
      <h2>📈 模型績效統計（有效樣本：{t3_sample} 筆）</h2>
      <p>T+1/T+3/T+5 以入選日收盤價為基準，需累積至少10個交易日才有意義</p>
    </div>
    <table>
      <tr>
        <th>分類</th>
        <th>T+1勝率</th><th>T+1均報</th>
        <th>T+3勝率</th><th>T+3均報</th>
        <th>T+5勝率</th><th>T+5均報</th>
        <th>樣本數</th>
      </tr>
      {perf_html}
    </table>
  </div>

  <!-- 每日統計 -->
  <div class="full-block">
    <div class="section-header">
      <h2>📅 每日入選統計（最近7天）</h2>
      <p>上市 / 上櫃 分開統計</p>
    </div>
    <table>
      <tr><th>日期</th><th>合計</th><th>上市 TSE</th><th>上櫃 OTC</th></tr>
      {daily_html}
    </table>
  </div>

</div>

<div class="footer">
  台股選股 Dashboard ｜ 資料來源：TWSE / TPEX / FinMind ｜ 僅供參考，不構成投資建議<br>
  最後更新：{now_str}
</div>

</body>
</html>'''

    os.makedirs('docs', exist_ok=True)
    with open(OUTPUT, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f'✅ Dashboard 已產出：{OUTPUT}（{len(html)//1024} KB）')

def main():
    print('='*50)
    print(f'[03_build_html] {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
    print('='*50)

    if not os.path.exists(DB_PATH):
        print('❌ DB 不存在，請先跑 01_ingest.py')
        return

    conn = sqlite3.connect(DB_PATH)
    cache = load_cache()
    if not cache:
        print('⚠️ 無 cache，僅用 DB 資料產出基本 Dashboard')

    build_html(cache, conn)
    conn.close()

if __name__ == '__main__':
    main()
