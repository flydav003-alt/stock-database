"""
02_analyze.py — 連續入選偵測 + 補抓 T+1/T+3/T+5 價格（優化版）

優化重點：
1. 交易日曆只抓必要的月份
2. 按月份批次抓價格，不是一筆一筆問
3. 同一檔股票同一個月只打一次 API
"""
import sqlite3
import requests
import pandas as pd
import time
import pickle
from datetime import datetime, timedelta
from collections import defaultdict

DB_PATH = 'data/stock_history.db'

# ══════════════════════════════════════════════════════════
# 交易日曆
# ══════════════════════════════════════════════════════════

def build_trade_calendar_simple(needed_months):
    """
    用週一到週五近似交易日，再排除週末
    不依賴 TWSE API，完全本地計算，快速穩定
    """
    today = datetime.today()
    today_str = today.strftime('%Y%m%d')
    all_dates = set()

    for ym in needed_months:
        yr = int(ym[:4])
        mm = int(ym[4:])
        import calendar
        _, days_in_month = calendar.monthrange(yr, mm)
        for day in range(1, days_in_month + 1):
            d = datetime(yr, mm, day)
            if d.weekday() < 5:  # 週一到週五
                all_dates.add(d.strftime('%Y%m%d'))

    result = sorted([d for d in all_dates if d <= today_str])
    print(f'  交易日曆：{len(result)} 天（週一到週五近似，涵蓋 {len(needed_months)} 個月）')
    return result

def build_trade_calendar(needed_months):
    return build_trade_calendar_simple(needed_months)

def get_nth_after(entry_date_str, n, trade_dates):
    entry = entry_date_str.replace('-', '')
    future = [d for d in trade_dates if d > entry]
    return future[n-1] if len(future) >= n else None

# ══════════════════════════════════════════════════════════
# 批次抓價格（同股票同月份只打一次 API）
# ══════════════════════════════════════════════════════════

def fetch_month_prices_twse(stock_id, ym):
    """抓上市股某月全部收盤價，回傳 {YYYYMMDD: float}"""
    url = (f'https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY'
           f'?date={ym}01&stockNo={stock_id}&response=json')
    try:
        r = requests.get(url, timeout=15, headers={'User-Agent': 'Mozilla/5.0'})
        j = r.json()
        if j.get('stat') != 'OK' or not j.get('data'):
            return {}
        result = {}
        for row in j['data']:
            parts = str(row[0]).strip().split('/')
            if len(parts) == 3:
                yr = int(parts[0]) + 1911
                key = f'{yr}{parts[1].zfill(2)}{parts[2].zfill(2)}'
                val = str(row[6]).replace(',', '').strip()
                if val not in ('--', '', 'X'):
                    try: result[key] = float(val)
                    except: pass
        return result
    except:
        return {}

def fetch_month_prices_tpex(stock_id, ym):
    """抓上櫃股某月全部收盤價，回傳 {YYYYMMDD: float}"""
    yr_tw = int(ym[:4]) - 1911
    mm = ym[4:]
    url = (f'https://www.tpex.org.tw/web/stock/aftertrading/daily_trading_info/'
           f'st43_result.php?l=zh-tw&d={yr_tw}/{mm}/01&s={stock_id}')
    try:
        r = requests.get(url, timeout=15, headers={'User-Agent': 'Mozilla/5.0'})
        j = r.json()
        if not j.get('aaData'):
            return {}
        result = {}
        for row in j['aaData']:
            parts = str(row[0]).strip().split('/')
            if len(parts) == 3:
                yr = int(parts[0]) + 1911
                key = f'{yr}{parts[1].zfill(2)}{parts[2].zfill(2)}'
                val = str(row[6]).replace(',', '').strip()
                if val not in ('--', ''):
                    try: result[key] = float(val)
                    except: pass
        return result
    except:
        return {}

def fetch_month_prices(stock_id, market, ym):
    if market == 'TSE':
        p = fetch_month_prices_twse(stock_id, ym)
        return p if p else fetch_month_prices_tpex(stock_id, ym)
    else:
        p = fetch_month_prices_tpex(stock_id, ym)
        return p if p else fetch_month_prices_twse(stock_id, ym)

# ══════════════════════════════════════════════════════════
# 主函數：補抓 T+1/T+3/T+5
# ══════════════════════════════════════════════════════════

def fill_future_prices(conn):
    print('\n[補抓價格]')
    today_str = datetime.today().strftime('%Y%m%d')

    df = pd.read_sql('''
        SELECT id, stock_id, market, date, close,
               price_t1, price_t3, price_t5
        FROM stock_daily
        WHERE (price_t1 IS NULL OR price_t3 IS NULL OR price_t5 IS NULL)
          AND date <= date('now', '-1 day')
        ORDER BY date
    ''', conn)

    if df.empty:
        print('  ✅ 無需補抓')
        return

    print(f'  待補抓：{len(df)} 筆')

    # 找出需要哪些月份的交易日曆
    needed_months = set()
    for _, row in df.iterrows():
        entry = str(row['date']).replace('-', '')  # 統一轉成 YYYYMMDD
        d = datetime.strptime(entry, '%Y%m%d')
        # T+5 大概需要再往後 10 個日曆天，多抓一個月保險
        for delta in range(0, 20):
            nd = d + timedelta(days=delta)
            needed_months.add(nd.strftime('%Y%m'))

    trade_dates = build_trade_calendar(needed_months)

    # 計算每筆需要抓的目標日期
    # 格式：{(stock_id, market, target_date): [(id, col), ...]}
    target_map = defaultdict(list)  # (sid, mkt, ym) -> {date: price}
    task_list  = []  # (id, col, target_date, sid, mkt)

    for _, row in df.iterrows():
        sid    = str(row['stock_id'])
        market = str(row['market'])
        entry  = str(row['date']).replace('-', '')  # 確保 YYYYMMDD 格式

        for n, col in [(1,'price_t1'), (3,'price_t3'), (5,'price_t5')]:
            if pd.isna(row[col]):
                td = get_nth_after(entry, n, trade_dates)
                if td and td <= today_str:
                    ym = td[:6]
                    target_map[(sid, market, ym)].append(td)
                    task_list.append((row['id'], col, td, sid, market, ym))

    if not task_list:
        print('  ✅ 目標日期都還沒到，無需補抓')
        return

    # 批次抓：同 stock+market+月份 只打一次 API
    price_cache = {}  # (sid, market, ym) -> {date: price}
    fetch_keys = set((t[3], t[4], t[5]) for t in task_list)
    total_keys = len(fetch_keys)
    print(f'  需打 API：{total_keys} 次（股票×月份組合）')

    for i, (sid, mkt, ym) in enumerate(sorted(fetch_keys)):
        prices = fetch_month_prices(sid, mkt, ym)
        price_cache[(sid, mkt, ym)] = prices
        if (i+1) % 10 == 0:
            print(f'  API 進度：{i+1}/{total_keys}')
        time.sleep(0.35)

    # 回填
    updated = 0
    for (row_id, col, td, sid, mkt, ym) in task_list:
        prices = price_cache.get((sid, mkt, ym), {})
        price = prices.get(td)
        if price:
            conn.execute(f'UPDATE stock_daily SET {col}=? WHERE id=?', (price, row_id))
            updated += 1

    conn.commit()
    print(f'  ✅ 補抓完成，成功填入 {updated} 筆')

# ══════════════════════════════════════════════════════════
# 連續入選 / 新進榜 / 績效
# ══════════════════════════════════════════════════════════

def detect_consecutive(conn):
    print('\n[連續入選偵測]')
    dates = pd.read_sql(
        'SELECT DISTINCT date FROM stock_daily ORDER BY date DESC LIMIT 7', conn
    )['date'].tolist()
    if not dates:
        return pd.DataFrame()
    ph = ','.join(['?']*len(dates))
    df = pd.read_sql(f'''
        SELECT stock_id, name, market, date, composite_score
        FROM stock_daily WHERE date IN ({ph})
    ''', conn, params=dates)
    summary = df.groupby(['stock_id','name','market']).agg(
        appear_count=('date','count'),
        avg_score=('composite_score','mean'),
        max_score=('composite_score','max'),
        latest_date=('date','max')
    ).reset_index().sort_values('appear_count', ascending=False)
    print(f'  7日內 {len(summary)} 檔曾入選')
    return summary

def detect_new_entries(conn):
    print('\n[新進榜偵測]')
    dates = pd.read_sql(
        'SELECT DISTINCT date FROM stock_daily ORDER BY date DESC LIMIT 2', conn
    )['date'].tolist()
    if len(dates) < 2:
        return pd.DataFrame()
    today_dt, yesterday_dt = dates[0], dates[1]
    today_ids = set(pd.read_sql(
        'SELECT stock_id FROM stock_daily WHERE date=?', conn, params=[today_dt]
    )['stock_id'])
    yesterday_ids = set(pd.read_sql(
        'SELECT stock_id FROM stock_daily WHERE date=?', conn, params=[yesterday_dt]
    )['stock_id'])
    new_ids = today_ids - yesterday_ids
    if not new_ids:
        print('  今日無新進榜')
        return pd.DataFrame()
    ph = ','.join(['?']*len(new_ids))
    new_df = pd.read_sql(f'''
        SELECT stock_id, name, market, close, composite_score,
               is_strong_confirm, is_early_breakout
        FROM stock_daily WHERE date=? AND stock_id IN ({ph})
        ORDER BY composite_score DESC
    ''', conn, params=[today_dt]+list(new_ids))
    print(f'  新進榜：{len(new_df)} 檔')
    return new_df

def calc_performance(conn):
    print('\n[績效統計]')
    df = pd.read_sql('''
        SELECT close as entry_price, price_t1, price_t3, price_t5,
               is_strong_confirm, is_early_breakout
        FROM stock_daily WHERE price_t3 IS NOT NULL
    ''', conn)
    if df.empty:
        print('  ⚠️ 尚無 T+3 資料')
        return {}
    df['ret_t1'] = (df['price_t1']-df['entry_price'])/df['entry_price']*100
    df['ret_t3'] = (df['price_t3']-df['entry_price'])/df['entry_price']*100
    df['ret_t5'] = (df['price_t5']-df['entry_price'])/df['entry_price']*100
    # 正確分類：兩個都FALSE的不應出現（已在ingest過濾），防呆用'—'標記
    def classify(row):
        s = str(row['is_strong_confirm']).strip().upper() == 'TRUE'
        e = str(row['is_early_breakout']).strip().upper() == 'TRUE'
        if s and e: return '綜合轉強'
        if s: return '強勢確認'
        if e: return '起漲預警'
        return '—'  # 不應出現，保護用
    df['cat'] = df.apply(classify, axis=1)
    df = df[df['cat'] != '—']  # 過濾掉兩個都FALSE的髒資料
    results = {}
    for cat in ['綜合轉強','強勢確認','起漲預警','全部']:
        sub = df if cat=='全部' else df[df['cat']==cat]
        if not len(sub): continue
        r = {'count':len(sub),
             't1_win':round((sub['ret_t1']>0).mean()*100,1),
             't1_avg':round(sub['ret_t1'].mean(),2),
             't3_win':round((sub['ret_t3']>0).mean()*100,1),
             't3_avg':round(sub['ret_t3'].mean(),2)}
        if sub['ret_t5'].notna().any():
            r['t5_win']=round((sub['ret_t5']>0).mean()*100,1)
            r['t5_avg']=round(sub['ret_t5'].mean(),2)
        results[cat]=r
        print(f'  {cat}（{len(sub)}筆）T+3={r["t3_win"]}% 均報={r["t3_avg"]}%')
    return results

# ══════════════════════════════════════════════════════════
# 產業對照表快取
# ══════════════════════════════════════════════════════════

INDUSTRY_CACHE_PATH = 'data/industry_map.json'

# 內建 fallback（API 抓不到時使用）
_FALLBACK_INDUSTRY = {
    '2330':'半導體','2303':'半導體','2454':'半導體','3034':'半導體',
    '3037':'半導體','6415':'半導體','8150':'半導體','3016':'半導體',
    '2344':'半導體','2408':'半導體','2449':'半導體','3711':'半導體',
    '6669':'半導體','4966':'半導體','2337':'半導體','2369':'半導體',
    '6443':'半導體','3533':'半導體','2436':'半導體','6531':'半導體',
    '3443':'半導體','3014':'半導體',
    '2317':'電腦及週邊','2382':'電腦及週邊','2376':'電腦及週邊',
    '2357':'電腦及週邊','2353':'電腦及週邊','2323':'電腦及週邊',
    '3231':'電腦及週邊','2301':'電腦及週邊','6269':'電腦及週邊',
    '2365':'電腦及週邊','2399':'電腦及週邊',
    '2474':'電子零組件','2395':'電子零組件','2312':'電子零組件',
    '3081':'電子零組件','2059':'電子零組件','6239':'電子零組件',
    '2329':'電子零組件','3189':'電子零組件','2388':'電子零組件',
    '3035':'電子零組件','6278':'電子零組件','3661':'電子零組件',
    '2478':'電子零組件','2421':'電子零組件','4958':'電子零組件',
    '3023':'電子零組件','2458':'電子零組件','4916':'電子零組件',
    '2014':'電子零組件',
    '2308':'光電業','3008':'光電業','1785':'光電業','3703':'光電業',
    '2499':'光電業','3741':'光電業','2448':'光電業','6488':'光電業',
    '3045':'通信網路','4904':'通信網路','2412':'通信網路','4906':'通信網路',
    '1503':'電機機械','1519':'電機機械','1504':'電機機械',
    '1605':'電線電纜','1603':'電線電纜','1602':'電線電纜',
    '2881':'金融保險','2882':'金融保險','2883':'金融保險',
    '2884':'金融保險','2885':'金融保險','2886':'金融保險',
    '2887':'金融保險','2891':'金融保險','2892':'金融保險',
    '5880':'金融保險','2801':'金融保險','5876':'金融保險',
    '1301':'塑膠工業','1303':'塑膠工業','6505':'塑膠工業',
    '2002':'鋼鐵工業','2006':'鋼鐵工業','2007':'鋼鐵工業',
    '2207':'汽車工業','2204':'汽車工業',
    '2912':'貿易百貨','2903':'貿易百貨',
    '4711':'生技醫療','4726':'生技醫療','6547':'生技醫療',
    '4168':'生技醫療','4142':'生技醫療','6550':'生技醫療',
    '5536':'建材營造','2501':'建材營造','2511':'建材營造',
    '1216':'食品工業','1201':'食品工業','1210':'食品工業',
    '1402':'紡織纖維','1407':'紡織纖維','1409':'紡織纖維',
    '1702':'化學工業','1704':'化學工業','4102':'化學工業',
    '2049':'機械工業','3035':'機械工業',
    '6121':'其他電子','4772':'其他電子','4979':'其他電子',
    '6146':'其他電子','6173':'其他電子','6667':'其他電子',
    '3236':'其他電子','2375':'其他電子','2359':'其他電子',
}

def build_industry_cache():
    """
    從 TWSE + TPEX OpenAPI 抓完整產業分類，快取30天。
    API 失敗時使用內建 fallback dict（方向C：代碼前綴）補足。
    """
    import json, os
    if os.path.exists(INDUSTRY_CACHE_PATH):
        age = (datetime.now().timestamp() - os.path.getmtime(INDUSTRY_CACHE_PATH)) / 86400
        if age < 30:
            with open(INDUSTRY_CACHE_PATH, 'r', encoding='utf-8') as f:
                data = json.load(f)
            print(f'  產業快取有效（{age:.0f}天前），共 {len(data)} 筆')
            return data

    print('  重新抓取產業分類...')
    result = dict(_FALLBACK_INDUSTRY)  # 先用 fallback 當底

    # TWSE 上市 — 試多個可能的欄位名稱
    twse_ok = False
    try:
        r = requests.get('https://openapi.twse.com.tw/v1/opendata/t187ap03_L',
                         timeout=25, headers={'User-Agent': 'Mozilla/5.0'})
        print(f'  TWSE HTTP {r.status_code}')
        if r.status_code == 200:
            rows = r.json()
            print(f'  TWSE 回傳 {len(rows)} 筆，範例欄位：{list(rows[0].keys()) if rows else "空"}')
            before = len(result)
            for row in rows:
                code = str(row.get('公司代號', row.get('Code', ''))).strip()
                # 嘗試各種可能欄位名
                ind = (row.get('產業類別') or row.get('industry_category') or
                       row.get('產業') or row.get('Industry') or '').strip()
                if code and ind:
                    result[code] = ind
            added = len(result) - before
            print(f'  TWSE 上市：新增 {added} 筆（共 {len(result)} 筆）')
            twse_ok = added > 0
        time.sleep(0.5)
    except Exception as e:
        print(f'  ⚠️ TWSE 失敗：{e}')

    # TPEX 上櫃
    tpex_ok = False
    try:
        r = requests.get('https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O',
                         timeout=25, headers={'User-Agent': 'Mozilla/5.0'})
        print(f'  TPEX HTTP {r.status_code}')
        if r.status_code == 200:
            rows = r.json()
            print(f'  TPEX 回傳 {len(rows)} 筆，範例欄位：{list(rows[0].keys()) if rows else "空"}')
            before = len(result)
            for row in rows:
                code = str(row.get('公司代號', row.get('Code', ''))).strip()
                ind = (row.get('產業類別') or row.get('industry_category') or
                       row.get('產業') or row.get('Industry') or '').strip()
                if code and ind and code not in result:
                    result[code] = ind
            added = len(result) - before
            print(f'  TPEX 上櫃：新增 {added} 筆（共 {len(result)} 筆）')
            tpex_ok = added > 0
        time.sleep(0.5)
    except Exception as e:
        print(f'  ⚠️ TPEX 失敗：{e}')

    # 方向 C fallback：代碼前綴補足未知股票（確保「其他」最小化）
    PREFIX_MAP = {
        '1': '傳統產業', '2': '電子業', '3': '電子零組件',
        '4': '生技醫療', '5': '金融保險', '6': '新興電子',
        '7': '文化創意', '8': '其他電子', '9': '其他',
    }
    prefix_filled = 0
    # 這個補充只針對 DB 裡實際出現過但對照表沒有的股票
    # （在 03_build_html.py 的 get_industry 裡做 fallback，這裡不需要全補）

    os.makedirs('data', exist_ok=True)
    with open(INDUSTRY_CACHE_PATH, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False)
    print(f'  ✅ 產業快取儲存：{len(result)} 筆（API {"成功" if twse_ok or tpex_ok else "失敗，使用fallback"}）')
    return result


def main():
    print('='*50)
    print(f'[02_analyze] {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
    print('='*50)
    print('\n[產業分類快取]')
    build_industry_cache()
    conn = sqlite3.connect(DB_PATH)
    fill_future_prices(conn)
    consec_df = detect_consecutive(conn)
    new_df    = detect_new_entries(conn)
    perf      = calc_performance(conn)
    conn.close()
    with open('data/analysis_cache.pkl','wb') as f:
        pickle.dump({
            'consec':consec_df, 'new_entries':new_df,
            'performance':perf,
            'generated':datetime.now().strftime('%Y-%m-%d %H:%M')
        }, f)
    print('\n✅ analyze 完成')

if __name__ == '__main__':
    main()
