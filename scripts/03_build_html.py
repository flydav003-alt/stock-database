"""
03_build_html.py — 完整 Dashboard（深夜咖啡館主題）
功能清單：
  1. 個股追蹤 modal（點代碼）
  2. 強度排行榜（7天滾動均分）
  3. 自選股 Watchlist（localStorage）
  4. 黑名單偵測
  5. 最佳出場分析（佔位，等30天資料）
  6. 最終信心值
  7. 回測（佔位，等30天資料）
  8. 週報 Email（由 weekly_report.py 負責）
"""
import sqlite3, os, json, pickle, requests, time
from datetime import datetime, timedelta
from collections import defaultdict

DB_PATH    = 'data/stock_history.db'
CACHE_PATH = 'data/analysis_cache.pkl'
OUTPUT     = 'docs/index.html'

# ── 產業對照表（TWSE 主要分類） ──────────────────────────────
INDUSTRY_MAP = {
    '2330':'半導體','2303':'半導體','2317':'電腦及週邊','2382':'電腦及週邊',
    '2376':'電腦及週邊','2379':'電子零組件','2308':'光電業','3008':'光電業',
    '2454':'半導體','2357':'電腦及週邊','2301':'電子零組件','2474':'電子零組件',
    '2395':'電子零組件','6505':'塑膠工業','2881':'金融保險','2882':'金融保險',
    '2886':'金融保險','2891':'金融保險','2002':'鋼鐵工業','1301':'塑膠工業',
    '1303':'塑膠工業','2207':'汽車工業','2912':'貿易百貨','2882':'金融保險',
    '3034':'半導體','3037':'電子零組件','3045':'通信網路','4904':'通信網路',
    '2412':'通信網路','6415':'半導體','8150':'半導體','3016':'半導體',
    '2353':'電腦及週邊','2312':'電子零組件','2049':'機械工業','1605':'電線電纜',
    '1503':'電機機械','2498':'半導體','3081':'電子零組件','1519':'電機機械',
    '1785':'光電業','2375':'電腦及週邊','2359':'電子零組件','2454':'半導體',
    '6667':'電子零組件','5536':'建材營造','2323':'電腦及週邊',
}

def get_industry(sid):
    return INDUSTRY_MAP.get(str(sid), '其他')

def load_cache():
    if not os.path.exists(CACHE_PATH): return {}
    try:
        with open(CACHE_PATH,'rb') as f: return pickle.load(f)
    except: return {}

# ── 從 DB 取資料 ────────────────────────────────────────────

def get_all_data(conn):
    today = conn.execute('SELECT MAX(date) FROM stock_daily').fetchone()[0] or ''
    yesterday_row = conn.execute(
        "SELECT DISTINCT date FROM stock_daily WHERE date < ? ORDER BY date DESC LIMIT 1", [today]
    ).fetchone()
    yesterday = yesterday_row[0] if yesterday_row else ''

    # 今日入選
    today_df = conn.execute('''
        SELECT stock_id, name, market, close, composite_score,
               total_score, early_score, is_strong_confirm, is_early_breakout,
               vol_ratio, daily_return_pct, ma28_bias_pct, rsi14,
               inst_consec_days, yoy_revenue_pct,
               foreign_today, trust_today, foreign_3d, trust_3d
        FROM stock_daily WHERE date=? ORDER BY composite_score DESC
    ''', [today]).fetchall()
    cols = ['stock_id','name','market','close','composite_score','total_score','early_score',
            'is_strong_confirm','is_early_breakout','vol_ratio','daily_return_pct',
            'ma28_bias_pct','rsi14','inst_consec_days','yoy_revenue_pct',
            'foreign_today','trust_today','foreign_3d','trust_3d']
    today_list = [dict(zip(cols, r)) for r in today_df]

    # 昨日
    yesterday_ids = set(r[0] for r in conn.execute(
        'SELECT stock_id FROM stock_daily WHERE date=?', [yesterday]
    ).fetchall()) if yesterday else set()
    today_ids = set(r['stock_id'] for r in today_list)
    new_entry_ids = today_ids - yesterday_ids

    # 7日連續入選
    dates7 = [r[0] for r in conn.execute(
        'SELECT DISTINCT date FROM stock_daily ORDER BY date DESC LIMIT 7'
    ).fetchall()]
    streak_data = {}
    if dates7:
        ph = ','.join(['?']*len(dates7))
        rows = conn.execute(f'''
            SELECT stock_id, name, market, date, composite_score
            FROM stock_daily WHERE date IN ({ph})
        ''', dates7).fetchall()
        for sid, nm, mkt, dt, cs in rows:
            if sid not in streak_data:
                streak_data[sid] = {'name':nm,'market':mkt,'dates':[],'scores':[]}
            streak_data[sid]['dates'].append(dt)
            streak_data[sid]['scores'].append(cs or 0)
    streak_list = []
    for sid, info in streak_data.items():
        cnt = len(info['dates'])
        avg = sum(info['scores'])/cnt if cnt else 0
        streak_list.append({
            'stock_id':sid,'name':info['name'],'market':info['market'],
            'count':cnt,'avg_score':round(avg,1),
            'latest':max(info['dates']) if info['dates'] else ''
        })
    streak_list.sort(key=lambda x: (-x['count'], -x['avg_score']))

    # 強度排行榜（7/14/30天滾動均分）
    strength = {}
    for window, key in [(7,'w7'),(14,'w14'),(30,'w30')]:
        rows = conn.execute(f'''
            SELECT stock_id, name, market,
                   COUNT(*) as cnt, AVG(composite_score) as avg_cs,
                   MAX(composite_score) as max_cs
            FROM stock_daily
            WHERE date >= date('now', '-{window} days')
            GROUP BY stock_id, name, market
            ORDER BY avg_cs DESC LIMIT 20
        ''').fetchall()
        strength[key] = [{'stock_id':r[0],'name':r[1],'market':r[2],
                          'cnt':r[3],'avg':round(r[4],1),'max':round(r[5],1)} for r in rows]

    # 績效統計
    perf_rows = conn.execute('''
        SELECT close, price_t1, price_t3, price_t5,
               is_strong_confirm, is_early_breakout
        FROM stock_daily WHERE price_t3 IS NOT NULL
    ''').fetchall()
    perf = {}
    if perf_rows:
        import statistics
        def calc_perf(subset):
            if not subset: return None
            ret1 = [(r[1]-r[0])/r[0]*100 for r in subset if r[1]]
            ret3 = [(r[2]-r[0])/r[0]*100 for r in subset if r[2]]
            ret5 = [(r[3]-r[0])/r[0]*100 for r in subset if r[3]]
            return {
                'count':len(subset),
                't1_win':round(sum(1 for x in ret1 if x>0)/len(ret1)*100,1) if ret1 else None,
                't1_avg':round(sum(ret1)/len(ret1),2) if ret1 else None,
                't3_win':round(sum(1 for x in ret3 if x>0)/len(ret3)*100,1) if ret3 else None,
                't3_avg':round(sum(ret3)/len(ret3),2) if ret3 else None,
                't5_win':round(sum(1 for x in ret5 if x>0)/len(ret5)*100,1) if ret5 else None,
                't5_avg':round(sum(ret5)/len(ret5),2) if ret5 else None,
            }
        combo  = [r for r in perf_rows if str(r[4]).upper()=='TRUE' and str(r[5]).upper()=='TRUE']
        strong = [r for r in perf_rows if str(r[4]).upper()=='TRUE' and str(r[5]).upper()!='TRUE']
        early  = [r for r in perf_rows if str(r[4]).upper()!='TRUE' and str(r[5]).upper()=='TRUE']
        perf = {
            '綜合轉強': calc_perf(combo),
            '強勢確認': calc_perf(strong),
            '起漲預警': calc_perf(early),
            '全部':     calc_perf(perf_rows),
        }

    # 黑名單
    blacklist = []
    bl_rows = conn.execute('''
        SELECT stock_id, name, market,
               COUNT(*) as total,
               SUM(CASE WHEN price_t3 IS NOT NULL AND (price_t3-close)/close < 0 THEN 1 ELSE 0 END) as neg3,
               SUM(CASE WHEN price_t5 IS NOT NULL AND (price_t5-close)/close < 0 THEN 1 ELSE 0 END) as neg5
        FROM stock_daily WHERE price_t3 IS NOT NULL
        GROUP BY stock_id, name, market
        HAVING (neg3 >= 3) OR (neg5 >= 3 AND total >= 3)
    ''').fetchall()
    for r in bl_rows:
        reasons = []
        if r[4] >= 3: reasons.append(f'T+3負報酬 {r[4]}/{r[2]} 次')
        if r[5] >= 3: reasons.append(f'T+5負報酬 {r[5]}/{r[2]} 次')
        blacklist.append({'stock_id':r[0],'name':r[1],'market':r[2],
                          'total':r[2],'neg3':r[4],'neg5':r[5],
                          'reason':' '.join(reasons)})

    # 個股完整歷史（for modal）
    stock_history = {}
    all_stocks = conn.execute('''
        SELECT DISTINCT stock_id, name, market FROM stock_daily
    ''').fetchall()
    for sid, nm, mkt in all_stocks:
        rows = conn.execute('''
            SELECT date, close, composite_score, total_score, early_score,
                   is_strong_confirm, is_early_breakout,
                   price_t1, price_t3, price_t5,
                   vol_ratio, daily_return_pct, ma28_bias_pct, rsi14,
                   inst_consec_days, yoy_revenue_pct
            FROM stock_daily WHERE stock_id=? ORDER BY date DESC LIMIT 30
        ''', [sid]).fetchall()
        history = []
        for r in rows:
            ret3 = round((r[7]-r[1])/r[1]*100,2) if r[7] and r[1] else None
            cat = '綜合' if str(r[5]).upper()=='TRUE' and str(r[6]).upper()=='TRUE' \
                 else ('強勢' if str(r[5]).upper()=='TRUE' else '起漲')
            history.append({
                'date':r[0],'close':r[1],'composite':round(r[2] or 0,1),
                'total':round(r[3] or 0,1),'early':round(r[4] or 0,1),
                'cat':cat,'t1':r[7],'t3':r[8],'t5':r[9],'ret3':ret3,
                'vr':round(r[10] or 0,2),'ret':round(r[11] or 0,2),
                'ma28':round(r[12] or 0,1),'rsi':round(r[13] or 0,1),
                'inst':r[14],'yoy':round(r[15] or 0,1) if r[15] else None,
            })
        if history:
            wins = [h for h in history if h['ret3'] is not None and h['ret3'] > 0]
            total_with_t3 = [h for h in history if h['ret3'] is not None]
            win_rate = round(len(wins)/len(total_with_t3)*100) if total_with_t3 else None
            stock_history[str(sid)] = {
                'name':nm,'market':mkt,'history':history,
                'appear':len(history),'win_rate':win_rate,
                'industry': get_industry(str(sid))
            }

    # 產業熱度
    today_industry = defaultdict(int)
    yesterday_industry = defaultdict(int)
    for r in today_list:
        today_industry[get_industry(r['stock_id'])] += 1
    if yesterday:
        yrows = conn.execute('SELECT stock_id FROM stock_daily WHERE date=?', [yesterday]).fetchall()
        for (sid,) in yrows:
            yesterday_industry[get_industry(str(sid))] += 1
    all_industries = set(today_industry) | set(yesterday_industry)
    industry_heat = []
    for ind in all_industries:
        td, yd = today_industry.get(ind,0), yesterday_industry.get(ind,0)
        if td > 0:
            industry_heat.append({'name':ind,'today':td,'yesterday':yd,'delta':td-yd})
    industry_heat.sort(key=lambda x: -x['today'])

    # 每日統計（最近10天）
    daily_stats = conn.execute('''
        SELECT date, market, COUNT(*) as cnt
        FROM stock_daily GROUP BY date, market
        ORDER BY date DESC LIMIT 20
    ''').fetchall()
    daily_map = defaultdict(lambda: {'TSE':0,'OTC':0})
    for dt, mkt, cnt in daily_stats:
        daily_map[dt][mkt] = cnt
    daily_list = sorted(daily_map.items(), reverse=True)[:10]

    # 總統計
    total_records = conn.execute('SELECT COUNT(*) FROM stock_daily').fetchone()[0]
    trade_days = conn.execute('SELECT COUNT(DISTINCT date) FROM stock_daily').fetchone()[0]
    t3_sample = len([r for r in perf_rows]) if perf_rows else 0

    return {
        'today': today,'yesterday': yesterday,
        'today_list': today_list,'new_entry_ids': new_entry_ids,
        'streak_list': streak_list,'strength': strength,
        'perf': perf,'blacklist': blacklist,
        'stock_history': stock_history,
        'industry_heat': industry_heat,
        'daily_list': daily_list,
        'total_records': total_records,
        'trade_days': trade_days,
        't3_sample': t3_sample,
    }

# ── HTML 產生器 ─────────────────────────────────────────────

def fmt_pct(v, digits=1):
    if v is None: return '—'
    return f'+{v:.{digits}f}%' if v >= 0 else f'{v:.{digits}f}%'

def win_color(v):
    if v is None: return '#6a5f54'
    if v >= 65: return '#5a9e6f'
    if v >= 55: return '#b07d2a'
    return '#c4572a'

def avg_color(v):
    if v is None: return '#6a5f54'
    return '#5a9e6f' if v >= 0 else '#c4572a'

def build_perf_row(cat, data, sq_color):
    if not data: return f'<tr><td><div class="pt-cat"><div class="pt-sq" style="background:{sq_color};"></div>{cat}</div></td><td colspan="6" class="nd-cell">累積中</td></tr>'
    def win_cell(v):
        if v is None: return '<td class="nd-cell">—</td>'
        c = win_color(v)
        w = min(int(v),100)
        return f'<td><div class="win-wrap"><div class="win-track"><div class="win-fill" style="width:{w}%;background:{c};"></div></div><span class="wn" style="color:{c};">{v}%</span></div></td>'
    def avg_cell(v):
        if v is None: return '<td class="nd-cell">—</td>'
        c = avg_color(v)
        s = f'+{v:.1f}%' if v >= 0 else f'{v:.1f}%'
        return f'<td class="av" style="color:{c};">{s}</td>'
    n = data.get('count',0)
    return f'''<tr>
      <td><div class="pt-cat"><div class="pt-sq" style="background:{sq_color};"></div>{cat}</div></td>
      {win_cell(data.get('t1_win'))}{avg_cell(data.get('t1_avg'))}
      {win_cell(data.get('t3_win'))}{avg_cell(data.get('t3_avg'))}
      {win_cell(data.get('t5_win'))}{avg_cell(data.get('t5_avg'))}
      <td class="sn">{n}</td>
    </tr>'''

def build_html(d):
    today_display = d['today'].replace('-','/') if d['today'] else '—'
    now_str = datetime.now().strftime('%Y/%m/%d %H:%M')
    t3_win = (d['perf'].get('全部') or {}).get('t3_win')
    t3_avg = (d['perf'].get('全部') or {}).get('t3_avg')
    today_count = len(d['today_list'])
    new_count = len(d['new_entry_ids'])

    # 過熱警示
    hot_stocks = [s for s in d['streak_list'] if s['count'] >= 5]
    alert_html = ''
    if hot_stocks:
        items = ' &nbsp;|&nbsp; '.join(f"{s['stock_id']} {s['name']} 連續{s['count']}天" for s in hot_stocks[:4])
        alert_html = f'<div class="a-alert"><div class="a-alert-dot"></div><span class="al-tag">過熱警示</span>{items}</div>'

    # 今日新進榜（前8筆）
    new_rows = ''
    shown = 0
    for r in d['today_list']:
        if r['stock_id'] not in d['new_entry_ids']: continue
        if shown >= 8: break
        cat = '綜合' if str(r.get('is_strong_confirm','')).upper()=='TRUE' and str(r.get('is_early_breakout','')).upper()=='TRUE' \
              else ('強勢' if str(r.get('is_strong_confirm','')).upper()=='TRUE' else '起漲')
        acc = '#c4572a' if cat=='綜合' else ('#5a9e6f' if cat=='強勢' else '#b07d2a')
        cs = round(r.get('composite_score') or 0, 1)
        star_attr = f"data-sid=\"{r['stock_id']}\""
        new_rows += f'''<div class="ne-item" onclick="openModal('{r['stock_id']}')">
          <div class="ne-acc" style="background:{acc};"></div>
          <div class="ne-main">
            <div class="ne-code">{r['stock_id']} <span class="star-btn" onclick="event.stopPropagation();toggleStar('{r['stock_id']}')" id="star-{r['stock_id']}">☆</span></div>
            <div class="ne-sub">{r['name']} · {r['market']}</div>
          </div>
          <div class="ne-right">
            <div class="ne-score">{cs}</div>
            <div class="ne-type">{cat}轉強</div>
          </div>
        </div>'''
        shown += 1
    if today_count > 8:
        new_rows += f'<div class="more-hint">顯示前 {shown} 筆 · 完整清單見強度排行</div>'

    # 連續入選
    streak_rows = ''
    for i, s in enumerate(d['streak_list'][:8]):
        hot = s['count'] >= 5
        row_cls = ' hot' if hot else ''
        hot_tag = '<span class="hot-tag">過熱</span>' if hot else ''
        days_color = '#c4572a' if hot else '#e8d9bc'
        streak_rows += f'''<div class="st-item{row_cls}" onclick="openModal('{s['stock_id']}')">
          <div class="st-rank">{str(i+1).zfill(2)}</div>
          <div class="st-code">{s['stock_id']}</div>
          <div><div class="st-name">{s['name']} {hot_tag}</div><div class="st-sub">{s['market']} · 均分 {s['avg_score']}</div></div>
          <div class="st-days" style="color:{days_color};">{s['count']}</div>
        </div>'''

    # 績效表
    perf_rows_html = build_perf_row('綜合轉強', d['perf'].get('綜合轉強'), '#c4572a')
    perf_rows_html += build_perf_row('強勢確認', d['perf'].get('強勢確認'), '#5a9e6f')
    perf_rows_html += build_perf_row('起漲預警', d['perf'].get('起漲預警'), '#b07d2a')
    perf_rows_html += build_perf_row('全部合計', d['perf'].get('全部'), '#5a5048')

    # 產業熱度
    max_ind = max((x['today'] for x in d['industry_heat']), default=1)
    ind_rows = ''
    for ind in d['industry_heat'][:8]:
        pct = int(ind['today']/max_ind*100) if max_ind else 0
        delta = ind['delta']
        if delta > 0: dc, ds = '#5a9e6f', f'+{delta}'
        elif delta < 0: dc, ds = '#c4572a', str(delta)
        else: dc, ds = 'rgba(232,217,188,.2)', '—'
        ind_rows += f'''<div class="ind-row">
          <div class="ind-name">{ind['name']}</div>
          <div class="ind-track"><div class="ind-fill" style="width:{pct}%;"></div></div>
          <div class="ind-c">{ind['today']}</div>
          <div class="ind-d" style="color:{dc};">{ds}</div>
        </div>'''

    # 黑名單
    bl_rows = ''
    for b in d['blacklist'][:5]:
        bl_rows += f'''<div class="bl-item">
          <div class="bl-acc"></div>
          <div>
            <div style="display:flex;align-items:center;gap:8px;">
              <span class="bl-code" onclick="openModal('{b['stock_id']}')">{b['stock_id']}</span>
              <span class="bl-name">{b['name']}</span>
              <span class="bl-mkt">{b['market']}</span>
            </div>
            <div class="bl-reason">{b.get('reason','訊號可靠度低')}</div>
          </div>
        </div>'''
    if not bl_rows:
        bl_rows = '<div class="no-data">目前無黑名單（累積資料中）</div>'

    # 強度排行（7天）
    strength_rows = ''
    for i, s in enumerate(d['strength'].get('w7', [])[:15]):
        strength_rows += f'''<div class="sr-item" onclick="openModal('{s['stock_id']}')">
          <div class="sr-rank">{i+1}</div>
          <div class="sr-code">{s['stock_id']} <span class="star-btn" onclick="event.stopPropagation();toggleStar('{s['stock_id']}')" id="star2-{s['stock_id']}">☆</span></div>
          <div class="sr-name">{s['name']}<span class="sr-mkt">{s['market']}</span></div>
          <div class="sr-cnt">{s['cnt']}天</div>
          <div class="sr-avg">{s['avg']}</div>
        </div>'''

    # 每日統計 JS 資料
    daily_labels = json.dumps([r[0] for r in reversed(d['daily_list'])])
    daily_tse = json.dumps([r[1].get('TSE',0) for r in reversed(d['daily_list'])])
    daily_otc = json.dumps([r[1].get('OTC',0) for r in reversed(d['daily_list'])])

    # 個股歷史 JS 資料（只傳入必要欄位減少體積）
    stock_js = json.dumps(d['stock_history'], ensure_ascii=False)

    t3_display = f'{t3_win}%' if t3_win is not None else '累積中'
    t3_avg_display = fmt_pct(t3_avg) if t3_avg is not None else '累積中'
    t3_color = win_color(t3_win) if t3_win else '#6a5f54'
    t3_avg_color = avg_color(t3_avg) if t3_avg is not None else '#6a5f54'

    kpi_t3_win = f'<span style="color:{t3_color};">{t3_display}</span>'
    kpi_t3_avg = f'<span style="color:{t3_avg_color};">{t3_avg_display}</span>'

    html = f'''<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>台股雷達 · {today_display}</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Fraunces:ital,wght@0,300;0,700;1,300;1,700&family=DM+Mono:wght@300;400;500&family=Noto+Sans+TC:wght@300;400;500&display=swap');
*{{box-sizing:border-box;margin:0;padding:0}}
:root{{
  --bg:#1c1510;--bg2:#221810;--bg3:#2d2318;
  --card:#1f1913;--card2:#251d16;
  --ink:#e8d9bc;--ink2:#c4a06e;--ink3:rgba(232,217,188,.45);--ink4:rgba(232,217,188,.2);
  --red:#c4572a;--red2:#2a1a0f;
  --grn:#5a9e6f;--grn2:#1a2f20;
  --amb:#b07d2a;--amb2:#2a1f0a;
  --border:rgba(232,217,188,.08);--border2:rgba(232,217,188,.05);
}}
body{{background:var(--bg);color:var(--ink);font-family:'Noto Sans TC',sans-serif;font-size:14px;line-height:1.6;}}

/* ── HEADER ── */
.hdr{{background:#150f0a;border-bottom:1px solid var(--border);padding:0 32px;display:flex;align-items:center;height:52px;}}
.hdr-logo{{font-family:'Fraunces',serif;font-style:italic;font-size:22px;font-weight:300;color:var(--ink);margin-right:28px;white-space:nowrap;}}
.hdr-logo em{{font-style:normal;color:var(--red);}}
.nav{{display:flex;flex:1;gap:0;}}
.nav-btn{{height:52px;display:flex;align-items:center;padding:0 14px;font-size:10px;letter-spacing:1.5px;color:var(--ink4);cursor:pointer;border-bottom:2px solid transparent;margin-bottom:-1px;transition:.15s;white-space:nowrap;}}
.nav-btn:hover{{color:var(--ink3);}}
.nav-btn.on{{color:var(--ink2);border-bottom-color:var(--red);}}
.hdr-right{{display:flex;align-items:center;gap:14px;margin-left:auto;}}
.live-ind{{display:flex;align-items:center;gap:5px;font-family:'DM Mono',monospace;font-size:9px;color:var(--ink4);letter-spacing:2px;}}
.live-dot{{width:5px;height:5px;border-radius:50%;background:#4ade80;animation:bk 2s ease-in-out infinite;}}
@keyframes bk{{0%,100%{{opacity:1}}50%{{opacity:.2}}}}
.hdr-date{{font-family:'DM Mono',monospace;font-size:10px;color:var(--ink4);letter-spacing:1px;}}

/* ── HERO ── */
.hero{{background:#150f0a;display:grid;grid-template-columns:repeat(5,1fr);border-bottom:1px solid var(--border);}}
.hkpi{{padding:20px 24px;border-right:1px solid var(--border);}}
.hkpi:last-child{{border-right:none;}}
.hkpi-n{{font-family:'Fraunces',serif;font-weight:700;font-size:42px;color:var(--ink);line-height:1;letter-spacing:-2px;}}
.hkpi-l{{font-size:8px;letter-spacing:3px;color:var(--ink4);margin-top:8px;text-transform:uppercase;}}
.hkpi-s{{font-family:'DM Mono',monospace;font-size:9px;color:var(--ink4);margin-top:3px;}}

/* ── ALERT ── */
.a-alert{{background:var(--amb2);border-bottom:1px solid rgba(176,125,42,.2);padding:7px 32px;display:flex;align-items:center;gap:10px;font-size:10px;color:var(--amb);}}
.a-alert-dot{{width:5px;height:5px;border-radius:50%;background:var(--amb);flex-shrink:0;animation:bk 1.5s ease-in-out infinite;}}
.al-tag{{font-size:8px;letter-spacing:2px;margin-right:4px;opacity:.7;}}

/* ── PAGE SECTIONS ── */
.page{{display:none;}}.page.on{{display:block;}}

/* ── OVERVIEW GRID ── */
.ov-grid{{display:grid;grid-template-columns:1fr 1fr;}}
.panel{{border-right:1px solid var(--border);border-bottom:1px solid var(--border);}}
.panel:nth-child(2n){{border-right:none;}}
.panel-hd{{padding:12px 20px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:8px;background:var(--card);}}
.ph-t{{font-size:9px;letter-spacing:2px;color:var(--ink3);text-transform:uppercase;}}
.ph-b{{margin-left:auto;font-family:'DM Mono',monospace;font-size:8px;padding:2px 7px;border:1px solid var(--border);color:var(--ink4);letter-spacing:.5px;}}
.ph-b.on{{border-color:var(--red);color:var(--red);}}
.ph-b.warn{{border-color:var(--amb);color:var(--amb);}}

/* ── NEW ENTRIES ── */
.ne-item{{display:grid;grid-template-columns:3px 1fr auto;gap:10px;align-items:center;padding:10px 18px;border-bottom:1px solid var(--border2);cursor:pointer;transition:.15s;}}
.ne-item:hover{{background:var(--bg2);}}
.ne-item:last-child{{border-bottom:none;}}
.ne-acc{{height:34px;flex-shrink:0;}}
.ne-code{{font-family:'DM Mono',monospace;font-size:13px;font-weight:500;color:var(--ink2);}}
.ne-sub{{font-size:10px;color:var(--ink4);margin-top:1px;}}
.ne-score{{font-family:'DM Mono',monospace;font-size:14px;font-weight:500;color:var(--ink);text-align:right;}}
.ne-type{{font-size:9px;color:var(--ink4);text-align:right;letter-spacing:.5px;}}
.more-hint{{padding:8px 18px;font-family:'DM Mono',monospace;font-size:9px;color:var(--ink4);letter-spacing:1px;background:var(--bg2);border-top:1px solid var(--border2);}}

/* ── STREAK ── */
.st-item{{display:grid;grid-template-columns:22px 48px 1fr 28px;gap:8px;align-items:center;padding:9px 18px;border-bottom:1px solid var(--border2);cursor:pointer;transition:.15s;}}
.st-item:hover{{background:var(--bg2);}}
.st-item:last-child{{border-bottom:none;}}
.st-item.hot{{background:rgba(176,125,42,.06);}}
.st-rank{{font-size:9px;color:var(--ink4);font-family:'DM Mono',monospace;}}
.st-code{{font-family:'DM Mono',monospace;font-size:12px;font-weight:500;color:var(--ink2);}}
.st-name{{font-size:11px;color:var(--ink);}}
.st-sub{{font-size:9px;color:var(--ink4);margin-top:1px;}}
.st-days{{font-family:'DM Mono',monospace;font-size:13px;font-weight:500;text-align:right;}}
.hot-tag{{font-size:8px;padding:1px 5px;background:rgba(176,125,42,.15);color:var(--amb);border:1px solid rgba(176,125,42,.25);margin-left:5px;vertical-align:middle;}}

/* ── PERF TABLE ── */
.pt{{width:100%;border-collapse:collapse;}}
.pt th{{padding:8px 14px;font-size:7px;letter-spacing:2px;color:var(--ink4);text-transform:uppercase;border-bottom:1px solid var(--border2);text-align:center;background:var(--bg);}}
.pt th:first-child{{text-align:left;}}
.pt td{{padding:9px 14px;border-bottom:1px solid var(--border2);font-size:11px;vertical-align:middle;}}
.pt tr:last-child td{{border-bottom:none;}}
.pt tr:hover td{{background:var(--bg2);}}
.pt-cat{{display:flex;align-items:center;gap:6px;color:rgba(232,217,188,.6);}}
.pt-sq{{width:3px;height:12px;flex-shrink:0;}}
.win-wrap{{display:flex;align-items:center;gap:5px;justify-content:center;}}
.win-track{{width:28px;height:2px;background:var(--bg3);}}
.win-fill{{height:2px;}}
.wn{{font-family:'DM Mono',monospace;font-size:10px;font-weight:500;min-width:36px;text-align:right;}}
.av{{font-family:'DM Mono',monospace;font-size:10px;font-weight:500;text-align:center;}}
.sn{{font-family:'DM Mono',monospace;font-size:9px;color:var(--ink4);text-align:center;}}
.nd-cell{{color:var(--ink4);font-size:9px;text-align:center;letter-spacing:1px;}}

/* ── INDUSTRY ── */
.ind-row{{display:flex;align-items:center;gap:8px;padding:8px 18px;border-bottom:1px solid var(--border2);}}
.ind-row:last-child{{border-bottom:none;}}
.ind-name{{font-size:10px;color:rgba(232,217,188,.5);width:82px;flex-shrink:0;}}
.ind-track{{flex:1;height:2px;background:var(--bg3);}}
.ind-fill{{height:2px;background:var(--red);}}
.ind-c{{font-family:'DM Mono',monospace;font-size:10px;color:var(--ink3);width:20px;text-align:right;}}
.ind-d{{font-family:'DM Mono',monospace;font-size:10px;width:24px;text-align:right;}}

/* ── BLACKLIST ── */
.bl-item{{display:flex;align-items:flex-start;gap:10px;padding:10px 18px;border-bottom:1px solid var(--border2);background:rgba(196,87,42,.04);}}
.bl-item:last-child{{border-bottom:none;}}
.bl-acc{{width:2px;height:32px;background:var(--red);flex-shrink:0;}}
.bl-code{{font-family:'DM Mono',monospace;font-size:12px;font-weight:500;color:var(--red);cursor:pointer;}}
.bl-name{{font-size:11px;color:var(--ink);font-weight:500;}}
.bl-mkt{{font-size:8px;color:var(--ink4);padding:1px 4px;border:1px solid var(--border);}}
.bl-reason{{font-size:10px;color:rgba(196,87,42,.6);margin-top:3px;}}
.no-data{{padding:20px;font-size:10px;color:var(--ink4);text-align:center;letter-spacing:1px;}}

/* ── DAILY CHART ── */
.chart-pad{{padding:14px 18px 10px;background:var(--card);}}

/* ── STRENGTH PAGE ── */
.sr-hd{{display:flex;gap:0;border-bottom:1px solid var(--border);background:var(--card);padding:0 20px;}}
.sr-tab{{height:40px;display:flex;align-items:center;padding:0 14px;font-size:10px;letter-spacing:1px;color:var(--ink4);cursor:pointer;border-bottom:2px solid transparent;margin-bottom:-1px;}}
.sr-tab.on{{color:var(--ink2);border-bottom-color:var(--red);}}
.sr-item{{display:grid;grid-template-columns:28px 56px 1fr 40px 44px;gap:8px;align-items:center;padding:10px 20px;border-bottom:1px solid var(--border2);cursor:pointer;transition:.15s;}}
.sr-item:hover{{background:var(--bg2);}}
.sr-item:last-child{{border-bottom:none;}}
.sr-rank{{font-family:'DM Mono',monospace;font-size:11px;color:var(--ink4);}}
.sr-code{{font-family:'DM Mono',monospace;font-size:13px;font-weight:500;color:var(--ink2);}}
.sr-name{{font-size:12px;color:var(--ink);}}
.sr-mkt{{font-size:8px;color:var(--ink4);padding:1px 4px;border:1px solid var(--border);margin-left:6px;}}
.sr-cnt{{font-family:'DM Mono',monospace;font-size:10px;color:var(--ink3);text-align:center;}}
.sr-avg{{font-family:'DM Mono',monospace;font-size:13px;font-weight:500;color:var(--ink2);text-align:right;}}

/* ── WATCHLIST ── */
.wl-empty{{padding:32px;text-align:center;color:var(--ink4);}}
.wl-title{{font-family:'Fraunces',serif;font-size:18px;font-weight:300;font-style:italic;color:var(--ink3);margin-bottom:8px;}}
.wl-sub{{font-size:11px;color:var(--ink4);letter-spacing:1px;}}
.wl-item{{display:grid;grid-template-columns:3px 1fr auto;gap:10px;align-items:center;padding:11px 20px;border-bottom:1px solid var(--border2);cursor:pointer;transition:.15s;}}
.wl-item:hover{{background:var(--bg2);}}

/* ── STAR ── */
.star-btn{{font-size:14px;color:var(--ink4);cursor:pointer;margin-left:4px;transition:.15s;user-select:none;}}
.star-btn.on{{color:#f0c040;}}

/* ── MODAL ── */
.modal-backdrop{{display:none;position:fixed;inset:0;background:rgba(0,0,0,.75);z-index:100;align-items:flex-start;justify-content:center;padding-top:40px;}}
.modal-backdrop.show{{display:flex;}}
.modal{{background:var(--bg);border:1px solid var(--border);width:700px;max-width:95vw;max-height:80vh;overflow-y:auto;}}
.modal-hdr{{background:#150f0a;padding:16px 20px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid var(--border);position:sticky;top:0;}}
.modal-title{{font-family:'Fraunces',serif;font-size:20px;font-weight:700;color:var(--ink);}}
.modal-sub{{font-size:10px;color:var(--ink4);margin-top:2px;font-family:'DM Mono',monospace;}}
.modal-close{{font-size:20px;color:var(--ink4);cursor:pointer;padding:4px 8px;transition:.15s;}}
.modal-close:hover{{color:var(--ink);}}
.modal-body{{padding:16px 20px;}}
.modal-stats{{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:var(--border);border:1px solid var(--border);margin-bottom:16px;}}
.ms-cell{{background:var(--bg2);padding:12px 14px;}}
.ms-n{{font-family:'DM Mono',monospace;font-size:20px;font-weight:500;color:var(--ink);}}
.ms-l{{font-size:9px;letter-spacing:2px;color:var(--ink4);margin-top:4px;text-transform:uppercase;}}
.hist-table{{width:100%;border-collapse:collapse;font-size:11px;}}
.hist-table th{{padding:6px 10px;font-size:8px;letter-spacing:2px;color:var(--ink4);text-transform:uppercase;border-bottom:1px solid var(--border);text-align:center;background:var(--bg);}}
.hist-table th:first-child{{text-align:left;}}
.hist-table td{{padding:7px 10px;border-bottom:1px solid var(--border2);vertical-align:middle;text-align:center;}}
.hist-table td:first-child{{text-align:left;font-family:'DM Mono',monospace;font-size:10px;color:var(--ink3);}}
.hist-table tr:hover td{{background:var(--bg2);}}
.cat-chip{{font-size:8px;padding:1px 6px;letter-spacing:.5px;}}
.chip-combo{{background:rgba(196,87,42,.15);color:var(--red);border:1px solid rgba(196,87,42,.25);}}
.chip-strong{{background:rgba(90,158,111,.12);color:var(--grn);border:1px solid rgba(90,158,111,.25);}}
.chip-early{{background:rgba(176,125,42,.12);color:var(--amb);border:1px solid rgba(176,125,42,.25);}}

/* ── PLACEHOLDER ── */
.ph-block{{padding:28px;text-align:center;margin:12px;border:1px dashed var(--border);}}
.ph-tl{{font-size:12px;color:var(--ink3);font-weight:500;}}
.ph-sl{{font-family:'DM Mono',monospace;font-size:10px;color:var(--ink4);margin-top:6px;}}

/* ── FOOTER ── */
.foot{{background:#150f0a;border-top:1px solid var(--border);padding:10px 32px;display:flex;align-items:center;justify-content:space-between;}}
.foot-legend{{display:flex;gap:14px;}}
.fl-i{{display:flex;align-items:center;gap:5px;font-size:9px;color:var(--ink4);letter-spacing:1px;}}
.fl-sq{{width:6px;height:6px;}}
.foot-r{{font-family:'Fraunces',serif;font-style:italic;font-size:10px;color:rgba(232,217,188,.12);}}

.page-inner{{max-width:1200px;margin:0 auto;}}
</style>
</head>
<body>

<div class="hdr">
  <div class="hdr-logo">台股<em>雷</em>達</div>
  <nav class="nav">
    <div class="nav-btn on" onclick="showPage('overview')">總覽</div>
    <div class="nav-btn" onclick="showPage('strength')">強度排行 ②</div>
    <div class="nav-btn" onclick="showPage('watchlist')">自選股 ③</div>
    <div class="nav-btn" onclick="showPage('exit')">出場分析 ⑤</div>
    <div class="nav-btn" onclick="showPage('backtest')">回測 ⑦</div>
  </nav>
  <div class="hdr-right">
    <div class="live-ind"><div class="live-dot"></div>LIVE</div>
    <div class="hdr-date">{now_str}</div>
  </div>
</div>

<!-- HERO -->
<div class="hero">
  <div class="hkpi">
    <div class="hkpi-n" style="color:var(--red);">{today_count}</div>
    <div class="hkpi-l">今日入選</div>
    <div class="hkpi-s">{today_display}</div>
  </div>
  <div class="hkpi">
    <div class="hkpi-n">{kpi_t3_win}</div>
    <div class="hkpi-l">T+3 勝率</div>
    <div class="hkpi-s">{d['t3_sample']} 筆樣本</div>
  </div>
  <div class="hkpi">
    <div class="hkpi-n">{kpi_t3_avg}</div>
    <div class="hkpi-l">T+3 均報酬</div>
    <div class="hkpi-s">入選日收盤基準</div>
  </div>
  <div class="hkpi">
    <div class="hkpi-n" style="color:var(--ink2);">{new_count}</div>
    <div class="hkpi-l">今日新進榜</div>
    <div class="hkpi-s">昨天未入選者</div>
  </div>
  <div class="hkpi">
    <div class="hkpi-n" style="font-size:20px;color:rgba(232,217,188,.25);font-weight:300;font-style:italic;">累積中</div>
    <div class="hkpi-l">最終信心值 ⑥</div>
    <div class="hkpi-s">需14天資料</div>
  </div>
</div>

{alert_html}

<!-- ════ OVERVIEW PAGE ════ -->
<div class="page on" id="page-overview">
<div class="ov-grid">

  <div class="panel">
    <div class="panel-hd">
      <div class="ph-t">今日新進榜</div>
      <div class="ph-b on">{new_count} 檔</div>
    </div>
    {new_rows}
  </div>

  <div class="panel">
    <div class="panel-hd">
      <div class="ph-t">連續入選</div>
      <div class="ph-b">近7日</div>
    </div>
    {streak_rows}
  </div>

  <div class="panel">
    <div class="panel-hd">
      <div class="ph-t">模型績效統計</div>
      <div class="ph-b on">{d['t3_sample']} 筆有效樣本</div>
    </div>
    <table class="pt">
      <thead><tr>
        <th>分類</th>
        <th>T+1勝率</th><th>T+1均報</th>
        <th>T+3勝率</th><th>T+3均報</th>
        <th>T+5勝率</th><th>T+5均報</th>
        <th>N</th>
      </tr></thead>
      <tbody>{perf_rows_html}</tbody>
    </table>
  </div>

  <div class="panel">
    <div class="panel-hd">
      <div class="ph-t">產業熱度</div>
      <div class="ph-b">今日 vs 昨日</div>
    </div>
    {ind_rows}

    <div class="panel-hd" style="border-top:1px solid var(--border);">
      <div class="ph-t">每日入選統計</div>
      <div style="display:flex;gap:10px;margin-left:auto;">
        <span style="display:flex;align-items:center;gap:4px;font-size:9px;color:var(--ink4);"><span style="width:7px;height:7px;background:var(--red);display:inline-block;"></span>TSE</span>
        <span style="display:flex;align-items:center;gap:4px;font-size:9px;color:var(--ink4);"><span style="width:7px;height:7px;background:var(--grn);display:inline-block;"></span>OTC</span>
      </div>
    </div>
    <div class="chart-pad">
      <div style="position:relative;height:80px;">
        <canvas id="dc" role="img" aria-label="每日入選統計">每日入選統計</canvas>
      </div>
    </div>
  </div>

  <div class="panel">
    <div class="panel-hd">
      <div class="ph-t">黑名單警示 ④</div>
      <div class="ph-b warn">{len(d['blacklist'])} 檔</div>
    </div>
    {bl_rows}
  </div>

  <div class="panel">
    <div class="panel-hd">
      <div class="ph-t">最終信心值 ⑥</div>
      <div class="ph-b">需14天資料</div>
    </div>
    <div class="ph-block">
      <div class="ph-tl">累積資料中</div>
      <div class="ph-sl">composite 50% + 連續入選 20%<br>歷史個人勝率 15% + 籌碼強度 15%</div>
    </div>
  </div>

</div>
</div>

<!-- ════ STRENGTH PAGE ════ -->
<div class="page" id="page-strength">
  <div class="sr-hd">
    <div class="sr-tab on" onclick="showStrength('w7',this)">7天</div>
    <div class="sr-tab" onclick="showStrength('w14',this)">14天</div>
    <div class="sr-tab" onclick="showStrength('w30',this)">30天</div>
  </div>
  <div id="strength-body">{strength_rows}</div>
</div>

<!-- ════ WATCHLIST PAGE ════ -->
<div class="page" id="page-watchlist">
  <div id="wl-body">
    <div class="wl-empty">
      <div class="wl-title">自選股清單</div>
      <div class="wl-sub">點擊任何股票代碼旁的 ☆ 加入自選</div>
    </div>
  </div>
</div>

<!-- ════ EXIT ANALYSIS PAGE ════ -->
<div class="page" id="page-exit">
  <div class="ph-block" style="margin:20px;">
    <div class="ph-tl">最佳出場時機分析 ⑤</div>
    <div class="ph-sl">按訊號類型 / 市場 / 分數區間分析 T+1/T+3/T+5 最佳出場天數<br>預計累積30個交易日後啟用</div>
  </div>
</div>

<!-- ════ BACKTEST PAGE ════ -->
<div class="page" id="page-backtest">
  <div class="ph-block" style="margin:20px;">
    <div class="ph-tl">回測系統 + 資金曲線 ⑦</div>
    <div class="ph-sl">模擬每日買入前N檔、T+3平倉，畫出累積報酬曲線<br>預計累積30個交易日後啟用</div>
  </div>
</div>

<!-- ════ MODAL ════ -->
<div class="modal-backdrop" id="modal-backdrop" onclick="closeModal(event)">
  <div class="modal" onclick="event.stopPropagation()">
    <div class="modal-hdr">
      <div>
        <div class="modal-title" id="modal-title">—</div>
        <div class="modal-sub" id="modal-sub">—</div>
      </div>
      <div style="display:flex;align-items:center;gap:12px;">
        <span class="star-btn" id="modal-star" style="font-size:20px;" onclick="toggleStar(currentSid)">☆</span>
        <span class="modal-close" onclick="closeModal()">✕</span>
      </div>
    </div>
    <div class="modal-body">
      <div class="modal-stats" id="modal-stats"></div>
      <table class="hist-table">
        <thead><tr>
          <th>日期</th><th>類型</th><th>分數</th><th>收盤</th>
          <th>T+1</th><th>T+3報酬</th><th>T+5</th>
          <th>量比</th><th>RSI</th>
        </tr></thead>
        <tbody id="modal-tbody"></tbody>
      </table>
    </div>
  </div>
</div>

<!-- ════ FOOTER ════ -->
<div class="foot">
  <div class="foot-legend">
    <div class="fl-i"><div class="fl-sq" style="background:var(--red);"></div>綜合轉強</div>
    <div class="fl-i"><div class="fl-sq" style="background:var(--grn);"></div>強勢確認</div>
    <div class="fl-i"><div class="fl-sq" style="background:var(--amb);"></div>起漲預警</div>
    <div class="fl-i"><div class="fl-sq" style="background:#c4572a;opacity:.4;"></div>黑名單</div>
  </div>
  <div class="foot-r">TWSE · TPEX · FinMind — 僅供參考，不構成投資建議</div>
</div>

<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>
<script>
// ── 資料 ──
const STOCK_DATA = {stock_js};
const STRENGTH = {json.dumps(d['strength'], ensure_ascii=False)};
const BL_CODES = new Set({json.dumps([b['stock_id'] for b in d['blacklist']])});

// ── 頁面切換 ──
function showPage(id) {{
  document.querySelectorAll('.page').forEach(p => p.classList.remove('on'));
  document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('on'));
  document.getElementById('page-'+id).classList.add('on');
  event && event.target && event.target.classList.add('on');
  if(id==='watchlist') renderWatchlist();
}}

// ── Modal ──
let currentSid = '';
function openModal(sid) {{
  const d = STOCK_DATA[sid];
  if(!d) return;
  currentSid = sid;
  const bl = BL_CODES.has(sid);
  document.getElementById('modal-title').textContent = sid + ' ' + d.name + (bl ? ' ⚠' : '');
  document.getElementById('modal-sub').textContent = d.market + ' · ' + d.industry + ' · 出現 ' + d.appear + ' 次';
  
  const wr = d.win_rate !== null ? d.win_rate+'%' : '—';
  document.getElementById('modal-stats').innerHTML = `
    <div class="ms-cell"><div class="ms-n">${{d.appear}}</div><div class="ms-l">入選次數</div></div>
    <div class="ms-cell"><div class="ms-n" style="color:${{d.win_rate>=60?'#5a9e6f':d.win_rate>=50?'#b07d2a':'#c4572a'}}">${{wr}}</div><div class="ms-l">T+3 勝率</div></div>
    <div class="ms-cell"><div class="ms-n">${{d.history[0]?.close || '—'}}</div><div class="ms-l">最近收盤</div></div>
    <div class="ms-cell"><div class="ms-n">${{d.history[0]?.composite || '—'}}</div><div class="ms-l">最近綜合分</div></div>
  `;
  
  const tbody = document.getElementById('modal-tbody');
  tbody.innerHTML = d.history.map(h => {{
    const chip = h.cat==='綜合' ? '<span class="cat-chip chip-combo">綜合</span>' :
                 h.cat==='強勢' ? '<span class="cat-chip chip-strong">強勢</span>' :
                                  '<span class="cat-chip chip-early">起漲</span>';
    const ret3 = h.ret3 !== null ? `<span style="color:${{h.ret3>=0?'#5a9e6f':'#c4572a'}}">${{h.ret3>=0?'+':''}}${{h.ret3}}%</span>` : '—';
    const t1 = h.t1 ? h.t1.toFixed(1) : '—';
    const t5 = h.t5 ? h.t5.toFixed(1) : '—';
    return `<tr>
      <td>${{h.date}}</td>
      <td style="text-align:center">${{chip}}</td>
      <td style="font-family:'DM Mono',monospace;font-weight:500;color:#c4a06e">${{h.composite}}</td>
      <td style="font-family:'DM Mono',monospace">${{h.close}}</td>
      <td style="font-family:'DM Mono',monospace;color:rgba(232,217,188,.5)">${{t1}}</td>
      <td>${{ret3}}</td>
      <td style="font-family:'DM Mono',monospace;color:rgba(232,217,188,.5)">${{t5}}</td>
      <td style="font-family:'DM Mono',monospace;color:rgba(232,217,188,.4)">${{h.vr}}x</td>
      <td style="font-family:'DM Mono',monospace;color:rgba(232,217,188,.4)">${{h.rsi}}</td>
    </tr>`;
  }}).join('');
  
  updateModalStar(sid);
  document.getElementById('modal-backdrop').classList.add('show');
}}
function closeModal(e) {{
  if(!e || e.target===document.getElementById('modal-backdrop'))
    document.getElementById('modal-backdrop').classList.remove('show');
}}

// ── Watchlist (localStorage) ──
function getWatchlist() {{
  try {{ return JSON.parse(localStorage.getItem('tw_watchlist') || '[]'); }}
  catch {{ return []; }}
}}
function saveWatchlist(wl) {{
  try {{ localStorage.setItem('tw_watchlist', JSON.stringify(wl)); }} catch{{}}
}}
function toggleStar(sid) {{
  let wl = getWatchlist();
  const idx = wl.indexOf(sid);
  if(idx >= 0) wl.splice(idx, 1);
  else wl.unshift(sid);
  saveWatchlist(wl);
  updateAllStars(sid);
  if(document.getElementById('page-watchlist').classList.contains('on')) renderWatchlist();
}}
function updateModalStar(sid) {{
  const wl = getWatchlist();
  const on = wl.includes(sid);
  const ms = document.getElementById('modal-star');
  if(ms) {{ ms.textContent = on ? '★' : '☆'; ms.classList.toggle('on', on); }}
}}
function updateAllStars(sid) {{
  document.querySelectorAll('[id^="star-"]').forEach(el => {{
    if(el.id === 'star-'+sid || el.id === 'star2-'+sid) {{
      const on = getWatchlist().includes(sid);
      el.textContent = on ? '★' : '☆';
      el.classList.toggle('on', on);
    }}
  }});
  updateModalStar(sid);
}}
function renderWatchlist() {{
  const wl = getWatchlist();
  const body = document.getElementById('wl-body');
  if(!wl.length) {{
    body.innerHTML = '<div class="wl-empty"><div class="wl-title">自選股清單</div><div class="wl-sub">點擊任何股票代碼旁的 ☆ 加入自選</div></div>';
    return;
  }}
  body.innerHTML = wl.map(sid => {{
    const d = STOCK_DATA[sid];
    if(!d) return '';
    const h = d.history[0] || {{}};
    const cs = h.composite || '—';
    const cat = h.cat || '—';
    const acc = cat==='綜合' ? '#c4572a' : (cat==='強勢' ? '#5a9e6f' : '#b07d2a');
    return `<div class="wl-item" onclick="openModal('${{sid}}')">
      <div class="ne-acc" style="background:${{acc}};height:34px;"></div>
      <div>
        <div style="display:flex;align-items:center;gap:8px;">
          <span style="font-family:'DM Mono',monospace;font-size:13px;font-weight:500;color:#c4a06e;">${{sid}}</span>
          <span style="font-size:11px;color:rgba(232,217,188,.45);">${{d.name}}</span>
          <span style="font-size:8px;color:rgba(232,217,188,.2);padding:1px 4px;border:1px solid rgba(232,217,188,.08);">${{d.market}}</span>
        </div>
        <div style="font-size:9px;color:rgba(232,217,188,.25);margin-top:2px;font-family:'DM Mono',monospace;">出現${{d.appear}}次 · 最近分數${{cs}}</div>
      </div>
      <span class="star-btn on" onclick="event.stopPropagation();toggleStar('${{sid}}')" id="star-wl-${{sid}}">★</span>
    </div>`;
  }}).join('');
}}

// ── 強度排行 ──
function showStrength(key, el) {{
  document.querySelectorAll('.sr-tab').forEach(t => t.classList.remove('on'));
  el.classList.add('on');
  const data = STRENGTH[key] || [];
  document.getElementById('strength-body').innerHTML = data.map((s,i) => `
    <div class="sr-item" onclick="openModal('${{s.stock_id}}')">
      <div class="sr-rank">${{i+1}}</div>
      <div class="sr-code">${{s.stock_id}} <span class="star-btn" onclick="event.stopPropagation();toggleStar('${{s.stock_id}}')" id="star-sr-${{s.stock_id}}">☆</span></div>
      <div class="sr-name">${{s.name}}<span class="sr-mkt">${{s.market}}</span></div>
      <div class="sr-cnt">${{s.cnt}}天</div>
      <div class="sr-avg">${{s.avg}}</div>
    </div>
  `).join('');
}}

// ── 初始化星星狀態 ──
window.addEventListener('load', () => {{
  const wl = getWatchlist();
  wl.forEach(sid => {{
    ['star','star2'].forEach(prefix => {{
      const el = document.getElementById(prefix+'-'+sid);
      if(el) {{ el.textContent='★'; el.classList.add('on'); }}
    }});
  }});
}});

// ── 每日統計圖 ──
const ctx = document.getElementById('dc');
if(ctx) {{
  new Chart(ctx.getContext('2d'), {{
    type:'bar',
    data:{{
      labels:{daily_labels},
      datasets:[
        {{label:'TSE',data:{daily_tse},backgroundColor:'#c4572a',borderRadius:0,barPercentage:.5}},
        {{label:'OTC',data:{daily_otc},backgroundColor:'#5a9e6f',borderRadius:0,barPercentage:.5}}
      ]
    }},
    options:{{
      responsive:true,maintainAspectRatio:false,
      plugins:{{legend:{{display:false}}}},
      scales:{{
        x:{{stacked:true,ticks:{{color:'rgba(232,217,188,.2)',font:{{size:9}}}},grid:{{display:false}},border:{{color:'rgba(232,217,188,.08)'}}}},
        y:{{stacked:true,ticks:{{color:'rgba(232,217,188,.2)',font:{{size:9}},maxTicksLimit:3}},grid:{{color:'rgba(232,217,188,.05)'}},border:{{display:false}}}}
      }}
    }}
  }});
}}
</script>
</body></html>'''
    return html

def main():
    print('='*50)
    print(f'[03_build_html] {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
    print('='*50)
    if not os.path.exists(DB_PATH):
        print('❌ DB 不存在'); return
    conn = sqlite3.connect(DB_PATH)
    data = get_all_data(conn)
    conn.close()
    print(f"  今日 {data['today']}，入選 {len(data['today_list'])} 筆，新進榜 {len(data['new_entry_ids'])} 檔")
    html = build_html(data)
    os.makedirs('docs', exist_ok=True)
    with open(OUTPUT, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f'✅ Dashboard 產出：{OUTPUT}（{len(html)//1024} KB）')

if __name__ == '__main__':
    main()
