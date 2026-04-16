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

def fetch_trade_dates_for_month(ym):
    """抓單一年月的交易日清單，回傳 set of YYYYMMDD"""
    url = f'https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY_ALL?date={ym}01&response=json'
    try:
        r = requests.get(url, timeout=15, headers={'User-Agent': 'Mozilla/5.0'})
        j = r.json()
        if j.get('stat') != 'OK' or not j.get('data'):
            return set()
        dates = set()
        for row in j['data']:
            parts = str(row[0]).strip().split('/')
            if len(parts) == 3:
                yr = int(parts[0]) + 1911
                dates.add(f'{yr}{parts[1].zfill(2)}{parts[2].zfill(2)}')
        return dates
    except:
        return set()

def build_trade_calendar(needed_months):
    """只抓 needed_months 裡的月份，needed_months = set of 'YYYYMM'"""
    all_dates = set()
    today_str = datetime.today().strftime('%Y%m%d')
    for ym in sorted(needed_months):
        dates = fetch_trade_dates_for_month(ym)
        all_dates.update(dates)
        time.sleep(0.25)
    result = sorted([d for d in all_dates if d <= today_str])
    print(f'  交易日曆：{len(result)} 天，涵蓋 {len(needed_months)} 個月')
    return result

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
    df['cat'] = '強勢確認'
    df.loc[df['is_early_breakout'].str.upper()=='TRUE','cat'] = '起漲預警'
    df.loc[(df['is_strong_confirm'].str.upper()=='TRUE')&
           (df['is_early_breakout'].str.upper()=='TRUE'),'cat'] = '綜合轉強'
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

def main():
    print('='*50)
    print(f'[02_analyze] {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
    print('='*50)
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
