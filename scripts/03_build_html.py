"""
03_build_html.py — 完整 Dashboard（深夜咖啡館主題）v2
改動：
  - 三欄版面（新進榜 / 連續入選 / 搜尋+自選股）
  - 新進榜加入漲幅%
  - 股票中文名亮度提升
  - 產業從 JSON 快取讀取
  - 導覽非選中項更明顯
  - 右欄整合搜尋框
"""
import sqlite3, os, json, time
from datetime import datetime, timedelta
from collections import defaultdict

DB_PATH           = 'data/stock_history.db'
INDUSTRY_CACHE    = 'data/industry_map.json'
OUTPUT            = 'docs/index.html'

def load_industry_map():
    if os.path.exists(INDUSTRY_CACHE):
        try:
            with open(INDUSTRY_CACHE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except: pass
    return {}

# 代碼前綴 fallback（API 和 fallback dict 都找不到時用）
_PREFIX_INDUSTRY = {
    '1': '傳統產業', '2': '電子業', '3': '電子零組件',
    '4': '生技醫療', '5': '金融保險', '6': '新興電子',
    '7': '文化創意', '8': '其他電子', '9': '其他',
}

def get_industry(sid, imap):
    sid = str(sid)
    if sid in imap:
        return imap[sid]
    # 代碼前綴 fallback
    prefix = sid[0] if sid else ''
    return _PREFIX_INDUSTRY.get(prefix, '其他')

def get_all_data(conn, imap):
    today = conn.execute('SELECT MAX(date) FROM stock_daily').fetchone()[0] or ''
    yday_row = conn.execute(
        "SELECT DISTINCT date FROM stock_daily WHERE date < ? ORDER BY date DESC LIMIT 1", [today]
    ).fetchone()
    yesterday = yday_row[0] if yday_row else ''

    cols = ['stock_id','name','market','close','composite_score','total_score','early_score',
            'is_strong_confirm','is_early_breakout','vol_ratio','daily_return_pct',
            'ma28_bias_pct','rsi14','inst_consec_days','yoy_revenue_pct',
            'foreign_today','trust_today','foreign_3d','trust_3d']
    today_df = conn.execute('''
        SELECT stock_id,name,market,close,composite_score,total_score,early_score,
               is_strong_confirm,is_early_breakout,vol_ratio,daily_return_pct,
               ma28_bias_pct,rsi14,inst_consec_days,yoy_revenue_pct,
               foreign_today,trust_today,foreign_3d,trust_3d
        FROM stock_daily WHERE date=? ORDER BY composite_score DESC
    ''', [today]).fetchall()
    today_list = [dict(zip(cols, r)) for r in today_df]

    yday_ids  = set(r[0] for r in conn.execute('SELECT stock_id FROM stock_daily WHERE date=?', [yesterday]).fetchall()) if yesterday else set()
    today_ids = set(r['stock_id'] for r in today_list)
    new_ids   = today_ids - yday_ids

    # 7日連續
    dates7 = [r[0] for r in conn.execute('SELECT DISTINCT date FROM stock_daily ORDER BY date DESC LIMIT 7').fetchall()]
    streak_map = {}
    if dates7:
        ph = ','.join(['?']*len(dates7))
        for sid,nm,mkt,dt,cs in conn.execute(f'SELECT stock_id,name,market,date,composite_score FROM stock_daily WHERE date IN ({ph})', dates7).fetchall():
            if sid not in streak_map:
                streak_map[sid] = {'name':nm,'market':mkt,'dates':[],'scores':[]}
            streak_map[sid]['dates'].append(dt)
            streak_map[sid]['scores'].append(cs or 0)
    streak_list = sorted([
        {'stock_id':sid,'name':v['name'],'market':v['market'],
         'count':len(v['dates']),'avg_score':round(sum(v['scores'])/len(v['scores']),1),
         'latest':max(v['dates'])}
        for sid,v in streak_map.items()
    ], key=lambda x: (-x['count'], -x['avg_score']))

    # 強度排行
    strength = {}
    for window, key in [(7,'w7'),(14,'w14'),(30,'w30')]:
        rows = conn.execute(f'''
            SELECT stock_id,name,market,COUNT(*) as cnt,AVG(composite_score) as avg_cs,MAX(composite_score) as max_cs
            FROM stock_daily WHERE date >= date('now','-{window} days')
            GROUP BY stock_id,name,market ORDER BY avg_cs DESC LIMIT 20
        ''').fetchall()
        strength[key] = [{'stock_id':r[0],'name':r[1],'market':r[2],'cnt':r[3],'avg':round(r[4],1),'max':round(r[5],1)} for r in rows]

    # 績效
    perf_rows = conn.execute(
        'SELECT close,price_t1,price_t3,price_t5,is_strong_confirm,is_early_breakout,market FROM stock_daily WHERE price_t3 IS NOT NULL'
    ).fetchall()
    perf = {}
    perf_tse = {}
    perf_otc = {}
    if perf_rows:
        def cp(subset):
            if not subset: return None
            r1 = [(r[1]-r[0])/r[0]*100 for r in subset if r[1] and r[0]]
            r3 = [(r[2]-r[0])/r[0]*100 for r in subset if r[2] and r[0]]
            r5 = [(r[3]-r[0])/r[0]*100 for r in subset if r[3] and r[0]]
            def wr(lst): return round(sum(1 for x in lst if x>0)/len(lst)*100,1) if lst else None
            def av(lst): return round(sum(lst)/len(lst),2) if lst else None
            return {'count':len(subset),'t1_win':wr(r1),'t1_avg':av(r1),'t3_win':wr(r3),'t3_avg':av(r3),'t5_win':wr(r5),'t5_avg':av(r5)}
        def calc_perf_for(rows):
            combo  = [r for r in rows if str(r[4]).upper()=='TRUE' and str(r[5]).upper()=='TRUE']
            strong = [r for r in rows if str(r[4]).upper()=='TRUE']
            early  = [r for r in rows if str(r[5]).upper()=='TRUE']
            return {'綜合轉強':cp(combo),'強勢確認':cp(strong),'起漲預警':cp(early),'全部':cp(rows)}
        perf     = calc_perf_for(perf_rows)
        tse_rows = [r for r in perf_rows if r[6]=='TSE']
        otc_rows = [r for r in perf_rows if r[6]=='OTC']
        perf_tse = calc_perf_for(tse_rows)
        perf_otc = calc_perf_for(otc_rows)

    # 黑名單
    blacklist = []
    for r in conn.execute('''
        SELECT stock_id,name,market,COUNT(*) as total,
               SUM(CASE WHEN price_t3 IS NOT NULL AND (price_t3-close)/close<0 THEN 1 ELSE 0 END) as neg3,
               SUM(CASE WHEN price_t5 IS NOT NULL AND (price_t5-close)/close<0 THEN 1 ELSE 0 END) as neg5
        FROM stock_daily WHERE price_t3 IS NOT NULL
        GROUP BY stock_id,name,market HAVING neg3>=3 OR neg5>=3
    ''').fetchall():
        reasons = []
        if r[4]>=3: reasons.append(f'T+3負報酬{r[4]}次')
        if r[5]>=3: reasons.append(f'T+5負報酬{r[5]}次')
        blacklist.append({'stock_id':r[0],'name':r[1],'market':r[2],'reason':'·'.join(reasons)})

    # 個股歷史（modal 用）
    stock_history = {}
    for sid,nm,mkt in conn.execute('SELECT DISTINCT stock_id,name,market FROM stock_daily').fetchall():
        rows = conn.execute('''
            SELECT date,close,composite_score,total_score,early_score,
                   is_strong_confirm,is_early_breakout,
                   price_t1,price_t3,price_t5,
                   vol_ratio,daily_return_pct,ma28_bias_pct,rsi14,
                   inst_consec_days,yoy_revenue_pct
            FROM stock_daily WHERE stock_id=? ORDER BY date DESC LIMIT 30
        ''', [sid]).fetchall()
        if not rows: continue
        history = []
        for r in rows:
            ret3 = round((r[8]-r[1])/r[1]*100,2) if r[8] and r[1] else None
            _s = str(r[5]).strip().upper()=='TRUE'
            _e = str(r[6]).strip().upper()=='TRUE'
            if _s and _e: cat = '綜合'
            elif _s: cat = '強勢'
            elif _e: cat = '起漲'
            else: cat = '—'  # 防呆，不應出現
            history.append({'date':r[0],'close':r[1],'composite':round(r[2] or 0,1),
                            'cat':cat,'t1':r[7],'t3':r[8],'t5':r[9],'ret3':ret3,
                            'vr':round(r[10] or 0,2),'ret':round(r[11] or 0,2),
                            'ma28':round(r[12] or 0,1),'rsi':round(r[13] or 0,1),
                            'inst':r[14],'yoy':round(r[15] or 0,1) if r[15] else None})
        t3_vals = [h['ret3'] for h in history if h['ret3'] is not None]
        wr = round(sum(1 for x in t3_vals if x>0)/len(t3_vals)*100) if t3_vals else None
        stock_history[str(sid)] = {'name':nm,'market':mkt,'history':history,
                                   'appear':len(history),'win_rate':wr,
                                   'industry':get_industry(str(sid), imap)}

    # 產業熱度
    today_ind = defaultdict(int)
    yday_ind  = defaultdict(int)
    for r in today_list:
        today_ind[get_industry(r['stock_id'], imap)] += 1
    if yesterday:
        for (sid,) in conn.execute('SELECT stock_id FROM stock_daily WHERE date=?', [yesterday]).fetchall():
            yday_ind[get_industry(str(sid), imap)] += 1
    industry_heat = sorted([
        {'name':ind,'today':td,'yesterday':yday_ind.get(ind,0),'delta':td-yday_ind.get(ind,0)}
        for ind,td in today_ind.items() if td > 0 and ind != '其他'
    ], key=lambda x: -x['today'])
    # 加上「其他」排最後
    other_td = today_ind.get('其他',0)
    if other_td > 0:
        industry_heat.append({'name':'其他','today':other_td,'yesterday':yday_ind.get('其他',0),'delta':other_td-yday_ind.get('其他',0)})

    # 每日統計
    daily_map = defaultdict(lambda: {'TSE':0,'OTC':0})
    for dt,mkt,cnt in conn.execute('SELECT date,market,COUNT(*) FROM stock_daily GROUP BY date,market ORDER BY date DESC LIMIT 20').fetchall():
        daily_map[dt][mkt] = cnt
    daily_list = sorted(daily_map.items(), reverse=True)[:10]

    return {
        'today':today,'yesterday':yesterday,'today_list':today_list,'new_ids':new_ids,
        'streak_list':streak_list,'strength':strength,'perf':perf,'perf_tse':perf_tse,'perf_otc':perf_otc,'blacklist':blacklist,
        'stock_history':stock_history,'industry_heat':industry_heat,'daily_list':daily_list,
        'total_records':conn.execute('SELECT COUNT(*) FROM stock_daily').fetchone()[0],
        'trade_days':conn.execute('SELECT COUNT(DISTINCT date) FROM stock_daily').fetchone()[0],
        't3_sample':len(perf_rows),'t3_sample_tse':len([r for r in perf_rows if r[6]=='TSE']),'t3_sample_otc':len([r for r in perf_rows if r[6]=='OTC']),
    }

def fmt_pct(v, d=1):
    if v is None: return '—'
    return f'+{v:.{d}f}%' if v >= 0 else f'{v:.{d}f}%'

def wc(v): # win color
    if v is None: return '#6a5f54'
    return '#5a9e6f' if v>=65 else ('#b07d2a' if v>=55 else '#c4572a')

def ac(v): # avg color
    if v is None: return '#6a5f54'
    return '#5a9e6f' if v>=0 else '#c4572a'

def perf_row(cat, data, sq):
    if not data: return f'<tr><td><div class="pt-cat"><div class="pt-sq" style="background:{sq};"></div>{cat}</div></td><td colspan="6" class="nd-cell">累積中</td></tr>'
    def wcel(v):
        if v is None: return '<td class="nd-cell">—</td>'
        cl = wc(v); w = min(int(v),100)
        return f'<td><div class="win-wrap"><div class="win-track"><div class="win-fill" style="width:{w}%;background:{cl};"></div></div><span class="wn" style="color:{cl};">{v}%</span></div></td>'
    def acel(v):
        if v is None: return '<td class="nd-cell">—</td>'
        cl = ac(v); s = fmt_pct(v)
        return f'<td class="av" style="color:{cl};">{s}</td>'
    return f'<tr><td><div class="pt-cat"><div class="pt-sq" style="background:{sq};"></div>{cat}</div></td>{wcel(data.get("t1_win"))}{acel(data.get("t1_avg"))}{wcel(data.get("t3_win"))}{acel(data.get("t3_avg"))}{wcel(data.get("t5_win"))}{acel(data.get("t5_avg"))}<td class="sn">{data.get("count",0)}</td></tr>'

def build_html(d):
    now_str       = datetime.now().strftime('%Y/%m/%d %H:%M')
    today_display = d['today'].replace('-','/') if d['today'] else '—'
    today_count   = len(d['today_list'])
    new_count     = len(d['new_ids'])
    t3_win  = (d['perf'].get('全部') or {}).get('t3_win')
    t3_avg  = (d['perf'].get('全部') or {}).get('t3_avg')
    kpi_t3w = f'<span style="color:{wc(t3_win)};">{t3_win}%</span>' if t3_win else '<span style="color:#6a5f54;font-size:20px;font-style:italic;">累積中</span>'
    kpi_t3a = f'<span style="color:{ac(t3_avg)};">{fmt_pct(t3_avg)}</span>' if t3_avg is not None else '<span style="color:#6a5f54;font-size:20px;font-style:italic;">累積中</span>'

    # 過熱警示
    hot = [s for s in d['streak_list'] if s['count']>=5]
    alert_html = ''
    if hot:
        items = ' &nbsp;|&nbsp; '.join(f"{s['stock_id']} {s['name']} 連續{s['count']}天" for s in hot[:5])
        alert_html = f'<div class="a-alert"><div class="a-alert-dot"></div><span class="al-tag">過熱警示</span>{items}</div>'

    # ── 新進榜 ──
    def cat_info(r):
        s = str(r.get('is_strong_confirm','')).upper()=='TRUE'
        e = str(r.get('is_early_breakout','')).upper()=='TRUE'
        if s and e: return '綜合', '#c4572a'
        if s: return '強勢', '#5a9e6f'
        return '起漲', '#b07d2a'

    new_rows = ''
    shown = 0
    for r in d['today_list']:
        if r['stock_id'] not in d['new_ids']: continue
        if shown >= 10: break
        cat, acc = cat_info(r)
        cs  = round(r.get('composite_score') or 0, 1)
        ret = r.get('daily_return_pct') or 0
        ret_s = f'+{ret:.1f}%' if ret >= 0 else f'{ret:.1f}%'
        ret_c = '#5a9e6f' if ret >= 0 else '#c4572a'
        new_rows += f'''<div class="ne-item" onclick="openModal('{r['stock_id']}')">
          <div class="ne-acc" style="background:{acc};"></div>
          <div class="ne-main">
            <div class="ne-top">
              <span class="ne-code">{r['stock_id']}</span>
              <span class="ne-ret" style="color:{ret_c};">{ret_s}</span>
              <span class="star-btn" onclick="event.stopPropagation();toggleStar('{r['stock_id']}')" id="star-{r['stock_id']}">☆</span>
            </div>
            <div class="ne-name">{r['name']} · {r['market']}</div>
          </div>
          <div class="ne-right">
            <div class="ne-score">{cs}</div>
            <div class="ne-type">{cat}轉強</div>
          </div>
        </div>'''
        shown += 1
    if today_count > shown:
        rest = today_count - shown
        new_rows += f'<div class="more-hint">還有 {rest} 檔 · 強度排行查看全部</div>'

    # ── 連續入選 ──
    streak_rows = ''
    for i, s in enumerate(d['streak_list'][:10]):
        hot_tag = '<span class="hot-tag">過熱</span>' if s['count']>=5 else ''
        dc = '#c4572a' if s['count']>=5 else ('#b07d2a' if s['count']>=3 else '#e8d9bc')
        streak_rows += f'''<div class="st-item" onclick="openModal('{s['stock_id']}')">
          <div class="st-rank">{str(i+1).zfill(2)}</div>
          <div class="st-code">{s['stock_id']}</div>
          <div class="st-info">
            <div class="st-name">{s['name']} {hot_tag}</div>
            <div class="st-sub">{s['market']} · 均分{s['avg_score']}</div>
          </div>
          <div class="st-days" style="color:{dc};">{s['count']}</div>
        </div>'''

    # ── 績效 ──
    # TSE 表
    phtml_tse  = perf_row('綜合轉強', d['perf_tse'].get('綜合轉強'), '#c4572a')
    phtml_tse += perf_row('強勢確認', d['perf_tse'].get('強勢確認'), '#5a9e6f')
    phtml_tse += perf_row('起漲預警', d['perf_tse'].get('起漲預警'), '#b07d2a')
    phtml_tse += perf_row('全部合計', d['perf_tse'].get('全部'), '#5a5048')
    # OTC 表
    phtml_otc  = perf_row('綜合轉強', d['perf_otc'].get('綜合轉強'), '#c4572a')
    phtml_otc += perf_row('強勢確認', d['perf_otc'].get('強勢確認'), '#5a9e6f')
    phtml_otc += perf_row('起漲預警', d['perf_otc'].get('起漲預警'), '#b07d2a')
    phtml_otc += perf_row('全部合計', d['perf_otc'].get('全部'), '#5a5048')

    # ── 產業熱度 ──
    max_ind = max((x['today'] for x in d['industry_heat']), default=1)
    ind_html = ''
    for ind in d['industry_heat'][:9]:
        pct = int(ind['today']/max_ind*100) if max_ind else 0
        delta = ind['delta']
        dc2 = '#5a9e6f' if delta>0 else ('#c4572a' if delta<0 else 'rgba(232,217,188,.2)')
        ds  = f'+{delta}' if delta>0 else (str(delta) if delta<0 else '—')
        bar_color = '#c4572a' if ind['name']!='其他' else '#5a5048'
        ind_html += f'''<div class="ind-row">
          <div class="ind-name">{ind['name']}</div>
          <div class="ind-track"><div class="ind-fill" style="width:{pct}%;background:{bar_color};"></div></div>
          <div class="ind-c">{ind['today']}</div>
          <div class="ind-d" style="color:{dc2};">{ds}</div>
        </div>'''

    # ── 黑名單 ──
    bl_html = ''
    for b in d['blacklist'][:5]:
        bl_html += f'''<div class="bl-item">
          <div class="bl-acc"></div>
          <div>
            <div style="display:flex;align-items:center;gap:8px;">
              <span class="bl-code" onclick="openModal('{b['stock_id']}')">{b['stock_id']}</span>
              <span class="bl-name">{b['name']}</span>
              <span class="bl-mkt">{b['market']}</span>
            </div>
            <div class="bl-reason">{b['reason']}</div>
          </div>
        </div>'''
    if not bl_html:
        bl_html = '<div class="no-data">目前無黑名單（資料累積中）</div>'

    # ── 強度排行 ──
    def strength_rows_html(key):
        out = ''
        for i, s in enumerate(d['strength'].get(key, [])[:20]):
            out += f'''<div class="sr-item" onclick="openModal('{s['stock_id']}')">
              <div class="sr-rank">{i+1}</div>
              <div class="sr-code">{s['stock_id']} <span class="star-btn" onclick="event.stopPropagation();toggleStar('{s['stock_id']}')" id="star-sr-{s['stock_id']}-{key}">☆</span></div>
              <div class="sr-name">{s['name']}<span class="sr-mkt">{s['market']}</span></div>
              <div class="sr-cnt">{s['cnt']}天</div>
              <div class="sr-avg">{s['avg']}</div>
            </div>'''
        return out

    # JS 資料
    stock_js   = json.dumps(d['stock_history'], ensure_ascii=False)
    strength_js= json.dumps(d['strength'], ensure_ascii=False)
    bl_codes   = json.dumps([b['stock_id'] for b in d['blacklist']])
    daily_labels = json.dumps([r[0] for r in reversed(d['daily_list'])])
    daily_tse    = json.dumps([r[1].get('TSE',0) for r in reversed(d['daily_list'])])
    daily_otc    = json.dumps([r[1].get('OTC',0) for r in reversed(d['daily_list'])])

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
  --ink:#e8d9bc;--ink2:#c4a06e;--ink3:rgba(232,217,188,.65);--ink4:rgba(232,217,188,.45);--ink5:rgba(232,217,188,.22);
  --red:#c4572a;--red2:#2a1a0f;
  --grn:#5a9e6f;--grn2:#1a2f20;
  --amb:#b07d2a;--amb2:#2a1f0a;
  --border:rgba(232,217,188,.12);--border2:rgba(232,217,188,.07);
}}
body{{background:var(--bg);color:var(--ink);font-family:'Noto Sans TC',sans-serif;font-size:14px;line-height:1.6;min-height:100vh;}}

.hdr{{background:#150f0a;border-bottom:1px solid var(--border);padding:0 28px;display:flex;align-items:center;height:52px;gap:0;}}
.hdr-logo{{font-family:'Fraunces',serif;font-style:italic;font-size:22px;font-weight:300;color:var(--ink);margin-right:24px;white-space:nowrap;}}
.hdr-logo em{{font-style:normal;color:var(--red);}}
.nav{{display:flex;flex:1;gap:0;}}
.nav-btn{{height:52px;display:flex;align-items:center;padding:0 13px;font-size:10px;letter-spacing:1.5px;color:rgba(232,217,188,.55);cursor:pointer;border-bottom:2px solid transparent;margin-bottom:-1px;transition:.15s;white-space:nowrap;user-select:none;}}
.nav-btn:hover{{color:rgba(232,217,188,.8);}}
.nav-btn.on{{color:var(--ink2);border-bottom-color:var(--red);}}
.hdr-right{{display:flex;align-items:center;gap:12px;margin-left:auto;}}
.live-ind{{display:flex;align-items:center;gap:5px;font-family:'DM Mono',monospace;font-size:9px;color:var(--ink3);letter-spacing:2px;}}
.live-dot{{width:5px;height:5px;border-radius:50%;background:#4ade80;animation:bk 2s ease-in-out infinite;}}
@keyframes bk{{0%,100%{{opacity:1}}50%{{opacity:.2}}}}
.hdr-date{{font-family:'DM Mono',monospace;font-size:10px;color:var(--ink3);letter-spacing:1px;}}

.hero{{background:#150f0a;display:grid;grid-template-columns:repeat(5,1fr);border-bottom:1px solid var(--border);}}
.hkpi{{padding:18px 22px;border-right:1px solid var(--border);}}
.hkpi:last-child{{border-right:none;}}
.hkpi-n{{font-family:'Fraunces',serif;font-weight:700;font-size:40px;color:var(--ink);line-height:1;letter-spacing:-2px;}}
.hkpi-l{{font-size:9px;letter-spacing:2px;color:var(--ink3);margin-top:7px;text-transform:uppercase;}}
.hkpi-s{{font-family:'DM Mono',monospace;font-size:10px;color:var(--ink3);margin-top:3px;}}

.a-alert{{background:var(--amb2);border-bottom:1px solid rgba(176,125,42,.18);padding:7px 28px;display:flex;align-items:center;gap:10px;font-size:10px;color:var(--amb);}}
.a-alert-dot{{width:5px;height:5px;border-radius:50%;background:var(--amb);flex-shrink:0;animation:bk 1.5s ease-in-out infinite;}}
.al-tag{{font-size:8px;letter-spacing:2px;margin-right:4px;opacity:.7;}}

.page{{display:none;}}.page.on{{display:block;}}

/* ── OVERVIEW 三欄 ── */
.ov-grid{{display:grid;grid-template-columns:1fr 1fr 1fr;border-top:1px solid var(--border);}}
.col-full{{grid-column:1/-1;border-bottom:1px solid var(--border);}}
.col{{border-right:1px solid var(--border);}}
.col:last-child{{border-right:none;}}

.panel-hd{{padding:11px 18px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:8px;background:var(--card);}}
.ph-t{{font-size:10px;letter-spacing:1px;color:var(--ink);text-transform:uppercase;font-weight:500;}}
.ph-b{{margin-left:auto;font-family:'DM Mono',monospace;font-size:8px;padding:2px 7px;border:1px solid var(--border);color:var(--ink4);}}
.ph-b.on{{border-color:var(--red);color:var(--red);}}
.ph-b.warn{{border-color:var(--amb);color:var(--amb);}}

/* ── 新進榜 ── */
.ne-item{{display:grid;grid-template-columns:3px 1fr auto;gap:10px;align-items:center;padding:9px 16px;border-bottom:1px solid var(--border2);cursor:pointer;transition:.15s;}}
.ne-item:hover{{background:var(--bg2);}}
.ne-item:last-child{{border-bottom:none;}}
.ne-acc{{height:36px;flex-shrink:0;}}
.ne-main{{min-width:0;}}
.ne-top{{display:flex;align-items:center;gap:6px;}}
.ne-code{{font-family:'DM Mono',monospace;font-size:13px;font-weight:500;color:var(--ink2);}}
.ne-ret{{font-family:'DM Mono',monospace;font-size:11px;font-weight:500;}}
.ne-name{{font-size:11px;color:var(--ink2);margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}}
.ne-right{{text-align:right;flex-shrink:0;}}
.ne-score{{font-family:'DM Mono',monospace;font-size:15px;font-weight:500;color:var(--ink);}}
.ne-type{{font-size:10px;color:var(--ink3);letter-spacing:.5px;}}
.more-hint{{padding:8px 16px;font-family:'DM Mono',monospace;font-size:9px;color:var(--ink4);letter-spacing:1px;background:var(--bg2);}}

/* ── 連續入選 ── */
.st-item{{display:grid;grid-template-columns:22px 46px 1fr 26px;gap:6px;align-items:center;padding:9px 16px;border-bottom:1px solid var(--border2);cursor:pointer;transition:.15s;}}
.st-item:hover{{background:var(--bg2);}}
.st-item:last-child{{border-bottom:none;}}
.st-rank{{font-size:9px;color:var(--ink4);font-family:'DM Mono',monospace;}}
.st-code{{font-family:'DM Mono',monospace;font-size:12px;font-weight:500;color:var(--ink2);}}
.st-info{{min-width:0;}}
.st-name{{font-size:11px;color:var(--ink);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}}
.st-sub{{font-size:10px;color:var(--ink3);margin-top:1px;}}
.st-days{{font-family:'DM Mono',monospace;font-size:14px;font-weight:500;text-align:right;}}
.hot-tag{{font-size:8px;padding:1px 4px;background:rgba(176,125,42,.15);color:var(--amb);border:1px solid rgba(176,125,42,.25);margin-left:4px;vertical-align:middle;}}

/* ── 搜尋+自選 ── */
.search-box{{padding:10px 14px;border-bottom:1px solid var(--border);background:var(--card);}}
.search-input{{width:100%;background:var(--bg2);border:1px solid var(--border);color:var(--ink);padding:7px 12px;font-family:'DM Mono',monospace;font-size:12px;outline:none;}}
.search-input::placeholder{{color:var(--ink4);}}
.search-input:focus{{border-color:rgba(196,87,42,.4);}}
.search-tabs{{display:flex;border-bottom:1px solid var(--border);background:var(--card);}}
.stab{{flex:1;height:34px;display:flex;align-items:center;justify-content:center;font-size:9px;letter-spacing:1px;color:var(--ink4);cursor:pointer;border-bottom:2px solid transparent;transition:.15s;}}
.stab.on{{color:var(--ink2);border-bottom-color:var(--red);}}
.sw-item{{display:flex;align-items:center;gap:8px;padding:9px 14px;border-bottom:1px solid var(--border2);cursor:pointer;transition:.15s;}}
.sw-item:hover{{background:var(--bg2);}}
.sw-code{{font-family:'DM Mono',monospace;font-size:12px;font-weight:500;color:var(--ink2);width:40px;flex-shrink:0;}}
.sw-name{{flex:1;font-size:12px;color:var(--ink);}}
.sw-mkt{{font-size:8px;color:var(--ink4);padding:1px 4px;border:1px solid var(--border);}}
.sw-score{{font-family:'DM Mono',monospace;font-size:11px;color:var(--ink3);}}

/* ── 下方四格 ── */
.bot-grid{{display:grid;grid-template-columns:1fr 1fr 1fr 1fr;border-top:1px solid var(--border);}}
.bot-col[style*="span 2"]{{grid-column:span 2;}}
.bot-col{{border-right:1px solid var(--border);}}
.bot-col:last-child{{border-right:none;}}
.bot-col[style*="span 4"]{{border-right:none;}}

/* ── 績效表 ── */
.pt{{width:100%;border-collapse:collapse;}}
.pt th{{padding:7px 12px;font-size:9px;letter-spacing:1px;color:var(--ink3);text-transform:uppercase;border-bottom:1px solid var(--border2);text-align:center;background:var(--bg);}}
.pt th:first-child{{text-align:left;}}
.pt td{{padding:8px 12px;border-bottom:1px solid var(--border2);font-size:10px;vertical-align:middle;}}
.pt tr:last-child td{{border-bottom:none;}}
.pt tr:hover td{{background:var(--bg2);}}
.pt-cat{{display:flex;align-items:center;gap:5px;color:var(--ink);font-size:11px;}}
.pt-sq{{width:3px;height:11px;flex-shrink:0;}}
.win-wrap{{display:flex;align-items:center;gap:4px;justify-content:center;}}
.win-track{{width:24px;height:2px;background:var(--bg3);}}
.win-fill{{height:2px;}}
.wn{{font-family:'DM Mono',monospace;font-size:10px;font-weight:500;min-width:34px;text-align:right;}}
.av{{font-family:'DM Mono',monospace;font-size:10px;font-weight:500;text-align:center;}}
.sn{{font-family:'DM Mono',monospace;font-size:10px;color:var(--ink3);text-align:center;}}
.nd-cell{{color:var(--ink3);font-size:10px;text-align:center;}}

/* ── 產業熱度 ── */
.ind-row{{display:flex;align-items:center;gap:6px;padding:7px 14px;border-bottom:1px solid var(--border2);}}
.ind-row:last-child{{border-bottom:none;}}
.ind-name{{font-size:11px;color:var(--ink);width:80px;flex-shrink:0;}}
.ind-track{{flex:1;height:2px;background:var(--bg3);}}
.ind-fill{{height:2px;}}
.ind-c{{font-family:'DM Mono',monospace;font-size:11px;color:var(--ink);width:20px;text-align:right;}}
.ind-d{{font-family:'DM Mono',monospace;font-size:10px;width:22px;text-align:right;}}

/* ── 黑名單 ── */
.bl-item{{display:flex;align-items:flex-start;gap:8px;padding:9px 14px;border-bottom:1px solid var(--border2);background:rgba(196,87,42,.03);}}
.bl-item:last-child{{border-bottom:none;}}
.bl-acc{{width:2px;height:28px;background:var(--red);flex-shrink:0;margin-top:2px;}}
.bl-code{{font-family:'DM Mono',monospace;font-size:12px;font-weight:500;color:var(--red);cursor:pointer;}}
.bl-name{{font-size:11px;color:var(--ink);font-weight:500;}}
.bl-mkt{{font-size:8px;color:var(--ink4);padding:1px 4px;border:1px solid var(--border);}}
.bl-reason{{font-size:10px;color:rgba(196,87,42,.5);margin-top:2px;}}
.no-data{{padding:18px;font-size:10px;color:var(--ink4);text-align:center;}}

/* ── 每日圖 ── */
.chart-pad{{padding:12px 14px 8px;background:var(--card);}}

/* ── 信心值佔位 ── */
.ph-block{{padding:24px 16px;text-align:center;margin:10px;border:1px dashed var(--border);}}
.ph-tl{{font-size:12px;color:var(--ink3);font-weight:500;}}
.ph-sl{{font-family:'DM Mono',monospace;font-size:10px;color:var(--ink4);margin-top:5px;line-height:1.8;}}

/* ── 強度排行 ── */
.sr-hd{{display:flex;gap:0;border-bottom:1px solid var(--border);background:var(--card);padding:0 16px;}}
.sr-tab{{height:38px;display:flex;align-items:center;padding:0 12px;font-size:9px;letter-spacing:1px;color:rgba(232,217,188,.5);cursor:pointer;border-bottom:2px solid transparent;margin-bottom:-1px;}}
.sr-tab.on{{color:var(--ink2);border-bottom-color:var(--red);}}
.sr-item{{display:grid;grid-template-columns:26px 54px 1fr 38px 42px;gap:6px;align-items:center;padding:9px 16px;border-bottom:1px solid var(--border2);cursor:pointer;transition:.15s;}}
.sr-item:hover{{background:var(--bg2);}}
.sr-item:last-child{{border-bottom:none;}}
.sr-rank{{font-family:'DM Mono',monospace;font-size:10px;color:var(--ink4);}}
.sr-code{{font-family:'DM Mono',monospace;font-size:12px;font-weight:500;color:var(--ink2);}}
.sr-name{{font-size:11px;color:var(--ink);}}
.sr-mkt{{font-size:8px;color:var(--ink4);padding:1px 4px;border:1px solid var(--border);margin-left:5px;}}
.sr-cnt{{font-family:'DM Mono',monospace;font-size:11px;color:var(--ink);text-align:center;}}
.sr-avg{{font-family:'DM Mono',monospace;font-size:13px;font-weight:500;color:var(--ink2);text-align:right;}}

/* ── Star ── */
.star-btn{{font-size:13px;color:var(--ink4);cursor:pointer;transition:.15s;user-select:none;}}
.star-btn.on{{color:#f0c040;}}

/* ── Modal ── */
.modal-backdrop{{display:none;position:fixed;inset:0;background:rgba(0,0,0,.8);z-index:100;align-items:flex-start;justify-content:center;padding-top:36px;overflow-y:auto;}}
.modal-backdrop.show{{display:flex;}}
.modal{{background:var(--bg);border:1px solid var(--border);width:680px;max-width:96vw;max-height:82vh;overflow-y:auto;margin-bottom:40px;}}
.modal-hdr{{background:#150f0a;padding:14px 18px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid var(--border);position:sticky;top:0;z-index:10;}}
.modal-title{{font-family:'Fraunces',serif;font-size:19px;font-weight:700;color:var(--ink);}}
.modal-title a{{font-family:'Fraunces',serif;font-size:19px;font-weight:700;}}
.modal-sub{{font-size:11px;color:var(--ink3);margin-top:2px;font-family:'DM Mono',monospace;}}
.modal-close{{font-size:18px;color:var(--ink4);cursor:pointer;padding:4px 8px;}}
.modal-close:hover{{color:var(--ink);}}
.modal-body{{padding:14px 18px;}}
.modal-stats{{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:var(--border);border:1px solid var(--border);margin-bottom:14px;}}
.ms-cell{{background:var(--bg2);padding:10px 12px;}}
.ms-n{{font-family:'DM Mono',monospace;font-size:18px;font-weight:500;color:var(--ink);}}
.ms-l{{font-size:9px;letter-spacing:1px;color:var(--ink3);margin-top:3px;text-transform:uppercase;}}
.hist-table{{width:100%;border-collapse:collapse;font-size:11px;}}
.hist-table th{{padding:6px 9px;font-size:9px;letter-spacing:1px;color:var(--ink3);text-transform:uppercase;border-bottom:1px solid var(--border);text-align:center;background:var(--bg);}}
.hist-table th:first-child{{text-align:left;}}
.hist-table td{{padding:7px 9px;border-bottom:1px solid var(--border2);text-align:center;}}
.hist-table td:first-child{{text-align:left;font-family:'DM Mono',monospace;font-size:10px;color:var(--ink3);}}
.hist-table tr:hover td{{background:var(--bg2);}}
.cat-chip{{font-size:8px;padding:1px 5px;letter-spacing:.5px;}}
.chip-combo{{background:rgba(196,87,42,.15);color:var(--red);border:1px solid rgba(196,87,42,.25);}}
.chip-strong{{background:rgba(90,158,111,.12);color:var(--grn);border:1px solid rgba(90,158,111,.25);}}
.chip-early{{background:rgba(176,125,42,.12);color:var(--amb);border:1px solid rgba(176,125,42,.25);}}

/* ── Footer ── */
.foot{{background:#150f0a;border-top:1px solid var(--border);padding:10px 28px;display:flex;align-items:center;justify-content:space-between;}}
.foot-legend{{display:flex;gap:12px;}}
.fl-i{{display:flex;align-items:center;gap:4px;font-size:10px;color:var(--ink3);letter-spacing:1px;}}
.fl-sq{{width:6px;height:6px;}}
.foot-r{{font-family:'Fraunces',serif;font-style:italic;font-size:10px;color:rgba(232,217,188,.12);}}
</style>
</head>
<body>

<div class="hdr">
  <div class="hdr-logo">台股<em>雷</em>達</div>
  <nav class="nav">
    <div class="nav-btn on" onclick="showPage('overview',this)">總覽</div>
    <div class="nav-btn" onclick="showPage('strength',this)">強度排行 ②</div>
    <div class="nav-btn" onclick="showPage('watchlist',this)">自選股 ③</div>
    <div class="nav-btn" onclick="showPage('exit',this)">出場分析 ⑤</div>
    <div class="nav-btn" onclick="showPage('backtest',this)">回測 ⑦</div>
  </nav>
  <div class="hdr-right">
    <div class="live-ind"><div class="live-dot"></div>LIVE</div>
    <div class="hdr-date">{now_str}</div>
  </div>
</div>

<div class="hero">
  <div class="hkpi"><div class="hkpi-n" style="color:var(--red);">{today_count}</div><div class="hkpi-l">今日入選</div><div class="hkpi-s">{today_display}</div></div>
  <div class="hkpi"><div class="hkpi-n">{kpi_t3w}</div><div class="hkpi-l">T+3 勝率</div><div class="hkpi-s">{d['t3_sample']} 筆樣本</div></div>
  <div class="hkpi"><div class="hkpi-n">{kpi_t3a}</div><div class="hkpi-l">T+3 均報酬</div><div class="hkpi-s">入選日收盤基準</div></div>
  <div class="hkpi"><div class="hkpi-n" style="color:var(--ink2);">{new_count}</div><div class="hkpi-l">今日新進榜</div><div class="hkpi-s">首次出現</div></div>
  <div class="hkpi"><div class="hkpi-n" style="font-size:18px;color:var(--ink4);font-weight:300;font-style:italic;font-family:'Fraunces',serif;">累積中</div><div class="hkpi-l">最終信心值 ⑥</div><div class="hkpi-s">需14天資料</div></div>
</div>

{alert_html}

<!-- ════ OVERVIEW ════ -->
<div class="page on" id="page-overview">
  <div class="ov-grid">
    <div class="col">
      <div class="panel-hd"><div class="ph-t">今日新進榜</div><div class="ph-b on">{new_count} 檔</div></div>
      {new_rows}
    </div>
    <div class="col">
      <div class="panel-hd"><div class="ph-t">連續入選排行</div><div class="ph-b">近7日</div></div>
      {streak_rows}
    </div>
    <div class="col">
      <div class="panel-hd"><div class="ph-t">搜尋 / 自選股 ③</div></div>
      <div class="search-box">
        <input class="search-input" id="search-input" placeholder="輸入代碼或名稱..." oninput="onSearch(this.value)">
      </div>
      <div class="search-tabs">
        <div class="stab on" id="stab-search" onclick="switchSearchTab('search')">搜尋結果</div>
        <div class="stab" id="stab-wl" onclick="switchSearchTab('wl')">自選清單</div>
      </div>
      <div id="search-results"></div>
      <div id="wl-list" style="display:none;"></div>
    </div>
  </div>

  <div class="bot-grid">

    <!-- 行1：績效統計（全寬，span 4） -->
    <div class="bot-col" style="grid-column:span 4;border-bottom:1px solid var(--border);">
      <div class="panel-hd">
        <div class="ph-t">模型績效統計</div>
        <div class="ph-b on">{d['t3_sample']} 筆有效樣本</div>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;">
        <div style="border-right:1px solid var(--border);">
          <div style="padding:6px 16px;font-size:9px;letter-spacing:2px;color:var(--ink2);background:var(--bg2);border-bottom:1px solid var(--border);">
            上市 TSE &nbsp;<span style="color:var(--ink4);font-size:9px;">{d['t3_sample_tse']} 筆樣本</span>
          </div>
          <table class="pt">
            <thead><tr><th>分類</th><th>T+1勝</th><th>T+1均</th><th>T+3勝</th><th>T+3均</th><th>T+5勝</th><th>T+5均</th><th>N</th></tr></thead>
            <tbody>{phtml_tse}</tbody>
          </table>
        </div>
        <div>
          <div style="padding:6px 16px;font-size:9px;letter-spacing:2px;color:var(--ink2);background:var(--bg2);border-bottom:1px solid var(--border);">
            上櫃 OTC &nbsp;<span style="color:var(--ink4);font-size:9px;">{d['t3_sample_otc']} 筆樣本</span>
          </div>
          <table class="pt">
            <thead><tr><th>分類</th><th>T+1勝</th><th>T+1均</th><th>T+3勝</th><th>T+3均</th><th>T+5勝</th><th>T+5均</th><th>N</th></tr></thead>
            <tbody>{phtml_otc}</tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- 行2：產業 + 黑名單+圖 + 信心值 -->
    <div class="bot-col">
      <div class="panel-hd"><div class="ph-t">產業熱度</div><div class="ph-b">今日 vs 昨日</div></div>
      {ind_html}
    </div>
    <div class="bot-col">
      <div class="panel-hd"><div class="ph-t">黑名單警示 ④</div><div class="ph-b warn">{len(d['blacklist'])} 檔</div></div>
      {bl_html}
    </div>
    <div class="bot-col">
      <div class="panel-hd"><div class="ph-t">每日統計</div></div>
      <div class="chart-pad"><div style="position:relative;height:100px;"><canvas id="dc" role="img" aria-label="每日入選統計">每日入選統計</canvas></div></div>
    </div>
    <div class="bot-col" style="border-right:none;">
      <div class="panel-hd"><div class="ph-t">最終信心值 ⑥</div><div class="ph-b">需14天</div></div>
      <div class="ph-block">
        <div class="ph-tl">累積資料中</div>
        <div class="ph-sl">composite 50%<br>連續入選 20%<br>歷史勝率 15%<br>籌碼強度 15%</div>
      </div>
    </div>

  </div>
</div>

<!-- ════ STRENGTH ════ -->
<div class="page" id="page-strength">
  <div class="sr-hd">
    <div class="sr-tab on" onclick="showStrength('w7',this)">7天滾動</div>
    <div class="sr-tab" onclick="showStrength('w14',this)">14天</div>
    <div class="sr-tab" onclick="showStrength('w30',this)">30天</div>
  </div>
  <div id="strength-body">{strength_rows_html('w7')}</div>
</div>

<!-- ════ WATCHLIST PAGE ════ -->
<div class="page" id="page-watchlist">
  <div class="panel-hd"><div class="ph-t">自選股清單</div></div>
  <div id="page-wl-body"></div>
</div>

<!-- ════ EXIT ════ -->
<div class="page" id="page-exit">
  <div class="ph-block" style="margin:20px;">
    <div class="ph-tl">最佳出場時機分析 ⑤</div>
    <div class="ph-sl">按訊號類型 / 市場 / 分數區間分析 T+1/T+3/T+5 最佳出場<br>預計累積30個交易日後啟用</div>
  </div>
</div>

<!-- ════ BACKTEST ════ -->
<div class="page" id="page-backtest">
  <div class="ph-block" style="margin:20px;">
    <div class="ph-tl">回測系統 + 資金曲線 ⑦</div>
    <div class="ph-sl">模擬每日買入前N檔、T+3平倉，累積報酬曲線<br>預計累積30個交易日後啟用</div>
  </div>
</div>

<!-- ════ MODAL ════ -->
<div class="modal-backdrop" id="modal-bd" onclick="if(event.target===this)closeModal()">
  <div class="modal">
    <div class="modal-hdr">
      <div><div class="modal-title" id="modal-title">—</div><div class="modal-sub" id="modal-sub">—</div></div>
      <div style="display:flex;align-items:center;gap:10px;">
        <span class="star-btn" id="modal-star" style="font-size:18px;" onclick="toggleStar(currentSid)">☆</span>
        <span class="modal-close" onclick="closeModal()">✕</span>
      </div>
    </div>
    <div class="modal-body">
      <div class="modal-stats" id="modal-stats"></div>
      <table class="hist-table">
        <thead><tr><th>日期</th><th>類型</th><th>分數</th><th>收盤</th><th>T+1</th><th>T+3報酬</th><th>T+5</th><th>量比</th><th>RSI</th></tr></thead>
        <tbody id="modal-tbody"></tbody>
      </table>
    </div>
  </div>
</div>

<div class="foot">
  <div class="foot-legend">
    <div class="fl-i"><div class="fl-sq" style="background:var(--red);"></div>綜合轉強</div>
    <div class="fl-i"><div class="fl-sq" style="background:var(--grn);"></div>強勢確認</div>
    <div class="fl-i"><div class="fl-sq" style="background:var(--amb);"></div>起漲預警</div>
    <div class="fl-i"><div class="fl-sq" style="background:rgba(196,87,42,.35);"></div>黑名單</div>
  </div>
  <div class="foot-r">TWSE · TPEX · FinMind — 僅供參考，不構成投資建議 · {d['trade_days']} 個交易日 · {d['total_records']} 筆記錄</div>
</div>

<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>
<script>
const SD = {stock_js};
const STR = {strength_js};
const BL = new Set({bl_codes});
let currentSid = '';
let searchTab = 'search';

// ── 頁面切換 ──
function showPage(id, el) {{
  document.querySelectorAll('.page').forEach(p => p.classList.remove('on'));
  document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('on'));
  document.getElementById('page-'+id).classList.add('on');
  if(el) el.classList.add('on');
  if(id==='watchlist') renderPageWl();
  if(id==='overview') initSearch();
}}

// ── 搜尋 ──
function initSearch() {{
  onSearch('');
  renderWlSide();
}}
function onSearch(q) {{
  q = q.trim().toLowerCase();
  const res = document.getElementById('search-results');
  const all = Object.entries(SD);
  const filtered = q
    ? all.filter(([sid, d]) => sid.includes(q) || d.name.toLowerCase().includes(q))
    : all.sort((a,b) => {{
        const ah = a[1].history[0]; const bh = b[1].history[0];
        return (bh?.composite||0) - (ah?.composite||0);
      }}).slice(0,15);
  res.innerHTML = filtered.slice(0,20).map(([sid,d]) => {{
    const h = d.history[0]||{{}};
    const cs = h.composite||'—';
    const wl = getWl();
    const starred = wl.includes(sid);
    return `<div class="sw-item" onclick="openModal('${{sid}}')">
      <div class="sw-code">${{sid}}</div>
      <div class="sw-name">${{d.name}}</div>
      <div class="sw-mkt">${{d.market}}</div>
      <div class="sw-score">${{cs}}</div>
      <span class="star-btn ${{starred?'on':''}}" onclick="event.stopPropagation();toggleStar('${{sid}}')" id="star-sw-${{sid}}">${{starred?'★':'☆'}}</span>
    </div>`;
  }}).join('') || '<div class="no-data">無符合結果</div>';
}}
function switchSearchTab(tab) {{
  searchTab = tab;
  document.getElementById('stab-search').classList.toggle('on', tab==='search');
  document.getElementById('stab-wl').classList.toggle('on', tab==='wl');
  document.getElementById('search-results').style.display = tab==='search'?'':'none';
  document.getElementById('wl-list').style.display = tab==='wl'?'':'none';
  if(tab==='wl') renderWlSide();
}}
function renderWlSide() {{
  const wl = getWl();
  const el = document.getElementById('wl-list');
  if(!wl.length) {{ el.innerHTML = '<div class="no-data">尚無自選股 · 點 ☆ 加入</div>'; return; }}
  el.innerHTML = wl.map(sid => {{
    const d = SD[sid]; if(!d) return '';
    const h = d.history[0]||{{}};
    return `<div class="sw-item" onclick="openModal('${{sid}}')">
      <div class="sw-code">${{sid}}</div>
      <div class="sw-name">${{d.name}}</div>
      <div class="sw-mkt">${{d.market}}</div>
      <div class="sw-score">${{h.composite||'—'}}</div>
      <span class="star-btn on" onclick="event.stopPropagation();toggleStar('${{sid}}')" id="star-wls-${{sid}}">★</span>
    </div>`;
  }}).join('');
}}

// ── Watchlist ──
function getWl() {{ try{{ return JSON.parse(localStorage.getItem('tw_wl')||'[]'); }}catch{{return[];}} }}
function saveWl(wl) {{ try{{ localStorage.setItem('tw_wl', JSON.stringify(wl)); }}catch{{}} }}
function toggleStar(sid) {{
  let wl = getWl();
  const i = wl.indexOf(sid);
  if(i>=0) wl.splice(i,1); else wl.unshift(sid);
  saveWl(wl);
  updateAllStars(sid, wl.includes(sid));
  renderWlSide();
  if(document.getElementById('page-watchlist').classList.contains('on')) renderPageWl();
}}
function updateAllStars(sid, on) {{
  document.querySelectorAll('[id^="star-"]').forEach(el => {{
    if(el.id.endsWith('-'+sid) || el.id==='modal-star' && currentSid===sid) {{
      el.textContent = on ? '★' : '☆';
      el.classList.toggle('on', on);
    }}
  }});
}}
function renderPageWl() {{
  const wl = getWl();
  const el = document.getElementById('page-wl-body');
  if(!wl.length) {{ el.innerHTML='<div class="no-data" style="padding:32px">點擊任何股票的 ☆ 加入自選股</div>'; return; }}
  el.innerHTML = wl.map(sid => {{
    const d=SD[sid]; if(!d) return '';
    const h=d.history[0]||{{}};
    const cat=h.cat||'—';
    const acc=cat==='綜合'?'#c4572a':(cat==='強勢'?'#5a9e6f':'#b07d2a');
    return `<div class="ne-item" onclick="openModal('${{sid}}')">
      <div class="ne-acc" style="background:${{acc}};"></div>
      <div class="ne-main">
        <div class="ne-top"><span class="ne-code">${{sid}}</span></div>
        <div class="ne-name">${{d.name}} · ${{d.market}}</div>
      </div>
      <div class="ne-right">
        <div class="ne-score">${{h.composite||'—'}}</div>
        <div class="ne-type">${{cat}}轉強</div>
      </div>
    </div>`;
  }}).join('');
}}

// ── Modal ──
function openModal(sid) {{
  const d = SD[sid]; if(!d) return;
  currentSid = sid;
  const bl = BL.has(sid);
  const suffix = d.market === 'TSE' ? '.TW' : '.TWO';
  const yahooUrl = `https://tw.stock.yahoo.com/quote/${{sid}}${{suffix}}`;
  document.getElementById('modal-title').innerHTML =
    `<a href="${{yahooUrl}}" target="_blank" rel="noopener"
       style="color:var(--ink);text-decoration:none;border-bottom:1px solid rgba(196,87,42,.5);padding-bottom:1px;"
       onmouseover="this.style.borderBottomColor='#c4572a'"
       onmouseout="this.style.borderBottomColor='rgba(196,87,42,.5)'"
    >${{sid}} ${{d.name}}</a>${{bl?' <span style=\"color:#c4572a;font-size:14px;\">⚠</span>':''}}`;
  document.getElementById('modal-sub').textContent = d.market+' · '+d.industry+' · 出現'+d.appear+'次';
  const wr = d.win_rate!==null ? d.win_rate+'%' : '—';
  const wrc = d.win_rate>=60?'#5a9e6f':(d.win_rate>=50?'#b07d2a':'#c4572a');
  document.getElementById('modal-stats').innerHTML = `
    <div class="ms-cell"><div class="ms-n">${{d.appear}}</div><div class="ms-l">入選次數</div></div>
    <div class="ms-cell"><div class="ms-n" style="color:${{wrc}}">${{wr}}</div><div class="ms-l">T+3 勝率</div></div>
    <div class="ms-cell"><div class="ms-n">${{d.history[0]?.close||'—'}}</div><div class="ms-l">最近收盤</div></div>
    <div class="ms-cell"><div class="ms-n">${{d.history[0]?.composite||'—'}}</div><div class="ms-l">最近綜合分</div></div>`;
  document.getElementById('modal-tbody').innerHTML = d.history.map(h => {{
    const chip = h.cat==='綜合'?'<span class="cat-chip chip-combo">綜合</span>':
                 h.cat==='強勢'?'<span class="cat-chip chip-strong">強勢</span>':
                                '<span class="cat-chip chip-early">起漲</span>';
    const r3 = h.ret3!==null?`<span style="color:${{h.ret3>=0?'#5a9e6f':'#c4572a'}}">${{h.ret3>=0?'+':''}}${{h.ret3}}%</span>`:'—';
    const t1 = h.t1?h.t1.toFixed(1):'—', t5=h.t5?h.t5.toFixed(1):'—';
    return `<tr><td>${{h.date}}</td><td style="text-align:center">${{chip}}</td>
      <td style="font-family:'DM Mono',monospace;font-weight:500;color:#c4a06e">${{h.composite}}</td>
      <td style="font-family:'DM Mono',monospace">${{h.close}}</td>
      <td style="font-family:'DM Mono',monospace;color:rgba(232,217,188,.7)">${{t1}}</td>
      <td>${{r3}}</td>
      <td style="font-family:'DM Mono',monospace;color:rgba(232,217,188,.7)">${{t5}}</td>
      <td style="font-family:'DM Mono',monospace;color:rgba(232,217,188,.65)">${{h.vr}}x</td>
      <td style="font-family:'DM Mono',monospace;color:rgba(232,217,188,.65)">${{h.rsi}}</td></tr>`;
  }}).join('');
  const on = getWl().includes(sid);
  const ms = document.getElementById('modal-star');
  ms.textContent = on?'★':'☆'; ms.classList.toggle('on',on);
  document.getElementById('modal-bd').classList.add('show');
}}
function closeModal() {{ document.getElementById('modal-bd').classList.remove('show'); }}

// ── 強度排行 ──
function showStrength(key, el) {{
  document.querySelectorAll('.sr-tab').forEach(t=>t.classList.remove('on'));
  el.classList.add('on');
  const data = STR[key]||[];
  document.getElementById('strength-body').innerHTML = data.map((s,i)=>`
    <div class="sr-item" onclick="openModal('${{s.stock_id}}')">
      <div class="sr-rank">${{i+1}}</div>
      <div class="sr-code">${{s.stock_id}} <span class="star-btn" onclick="event.stopPropagation();toggleStar('${{s.stock_id}}')" id="star-sr2-${{s.stock_id}}">☆</span></div>
      <div class="sr-name">${{s.name}}<span class="sr-mkt">${{s.market}}</span></div>
      <div class="sr-cnt">${{s.cnt}}天</div>
      <div class="sr-avg">${{s.avg}}</div>
    </div>`).join('');
}}

// ── 初始化 ──
window.addEventListener('load', ()=>{{
  initSearch();
  const wl=getWl();
  wl.forEach(sid=>{{
    ['star-','star-sw-','star-sr-','star-sr2-','star-wls-'].forEach(p=>{{
      const el=document.getElementById(p+sid);
      if(el){{el.textContent='★';el.classList.add('on');}}
    }});
  }});
}});

// ── Chart ──
const ctx=document.getElementById('dc');
if(ctx) new Chart(ctx.getContext('2d'),{{
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
      x:{{stacked:true,ticks:{{color:'rgba(232,217,188,.4)',font:{{size:8}}}},grid:{{display:false}},border:{{color:'rgba(232,217,188,.12)'}}}},
      y:{{stacked:true,ticks:{{color:'rgba(232,217,188,.4)',font:{{size:8}},maxTicksLimit:3}},grid:{{color:'rgba(232,217,188,.08)'}},border:{{display:false}}}}
    }}
  }}
}});
</script>
</body></html>'''
    return html

def main():
    print('='*50)
    print(f'[03_build_html] {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
    print('='*50)
    if not os.path.exists(DB_PATH):
        print('❌ DB 不存在'); return
    imap = load_industry_map()
    print(f'  產業對照表：{len(imap)} 筆')
    conn = sqlite3.connect(DB_PATH)
    data = get_all_data(conn, imap)
    conn.close()
    print(f"  今日 {data['today']}，入選 {len(data['today_list'])} 筆，新進榜 {len(data['new_ids'])} 檔")
    html = build_html(data)
    os.makedirs('docs', exist_ok=True)
    with open(OUTPUT, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f'✅ Dashboard 產出：{OUTPUT}（{len(html)//1024} KB）')

if __name__ == '__main__':
    main()
