"""
02_analyze.py — 連續入選偵測 + 補抓 T+1/T+3/T+5 價格
使用 TWSE/TPEX OpenAPI 抓價格，不用 yfinance

價格抓取邏輯：
- 每次執行找出 DB 裡 price_t1/t3/t5 還是 NULL 的紀錄
- 根據入選日往後數第 N 個交易日，抓那天的收盤價
- 與股票有沒有再次入選完全無關
"""
import sqlite3
import requests
import pandas as pd
import time
from datetime import datetime, timedelta

DB_PATH = 'data/stock_history.db'

def fetch_real_trade_dates_twse(year_month_str):
    date_str = year_month_str + '01'
    url = f'https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY_ALL?date={date_str}&response=json'
    try:
        r = requests.get(url, timeout=15, headers={'User-Agent': 'Mozilla/5.0'})
        j = r.json()
        if j.get('stat') != 'OK' or not j.get('data'):
            return []
        dates = []
        for row in j['data']:
            tw_date = str(row[0]).strip()
            parts = tw_date.split('/')
            if len(parts) == 3:
                year = int(parts[0]) + 1911
                mmdd = parts[1].zfill(2) + parts[2].zfill(2)
                dates.append(f'{year}{mmdd}')
        return sorted(set(dates))
    except:
        return []

def build_trade_calendar(days_back=90):
    today = datetime.today()
    year_months = set()
    d = today
    for _ in range(days_back + 30):
        year_months.add(d.strftime('%Y%m'))
        d -= timedelta(days=1)

    real_dates = set()
    for ym in sorted(year_months):
        dates = fetch_real_trade_dates_twse(ym)
        real_dates.update(dates)
        time.sleep(0.3)

    today_str = today.strftime('%Y%m%d')
    if len(real_dates) > 10:
        result = sorted([d for d in real_dates if d <= today_str])
        print(f'  交易日曆：{len(result)} 天（真實）')
        return result

    print('  ⚠️ fallback 至週一到週五')
    result = []
    d = today
    for _ in range(days_back):
        if d.weekday() < 5:
            result.append(d.strftime('%Y%m%d'))
        d -= timedelta(days=1)
    return sorted(result)

def get_nth_trading_day_after(entry_date_str, n, trade_dates_sorted):
    entry = entry_date_str.replace('-', '')
    future = [d for d in trade_dates_sorted if d > entry]
    return future[n - 1] if len(future) >= n else None

def fetch_price_twse_on_date(stock_id, target_date_str):
    url = (f'https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY'
           f'?date={target_date_str}&stockNo={stock_id}&response=json')
    try:
        r = requests.get(url, timeout=15, headers={'User-Agent': 'Mozilla/5.0'})
        j = r.json()
        if j.get('stat') != 'OK' or not j.get('data'):
            return None
        target_mmdd = f'{target_date_str[4:6]}/{target_date_str[6:]}'
        for row in j['data']:
            parts = str(row[0]).strip().split('/')
            if len(parts) == 3:
                mmdd = f'{parts[1].zfill(2)}/{parts[2].zfill(2)}'
                if mmdd == target_mmdd:
                    close_str = str(row[6]).replace(',', '').strip()
                    if close_str not in ('--', '', 'X'):
                        return float(close_str)
        return None
    except:
        return None

def fetch_price_tpex_on_date(stock_id, target_date_str):
    year = int(target_date_str[:4]) - 1911
    mm   = target_date_str[4:6]
    dd   = target_date_str[6:]
    date_tw = f'{year}/{mm}/{dd}'
    url = (f'https://www.tpex.org.tw/web/stock/aftertrading/daily_trading_info/'
           f'st43_result.php?l=zh-tw&d={date_tw}&s={stock_id}')
    try:
        r = requests.get(url, timeout=15, headers={'User-Agent': 'Mozilla/5.0'})
        j = r.json()
        if not j.get('aaData'):
            return None
        target_mmdd = f'{mm}/{dd}'
        for row in j['aaData']:
            parts = str(row[0]).strip().split('/')
            if len(parts) == 3:
                mmdd = f'{parts[1].zfill(2)}/{parts[2].zfill(2)}'
                if mmdd == target_mmdd:
                    close_str = str(row[6]).replace(',', '').strip()
                    if close_str not in ('--', ''):
                        return float(close_str)
        return None
    except:
        return None

def fetch_close_on_date(stock_id, market, target_date_str):
    if market == 'TSE':
        p = fetch_price_twse_on_date(stock_id, target_date_str)
        return p if p else fetch_price_tpex_on_date(stock_id, target_date_str)
    else:
        p = fetch_price_tpex_on_date(stock_id, target_date_str)
        return p if p else fetch_price_twse_on_date(stock_id, target_date_str)

def fill_future_prices(conn):
    print('\n[補抓價格]')
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

    print(f'  需補抓：{len(df)} 筆')
    trade_dates = build_trade_calendar(days_back=90)
    today_str   = datetime.today().strftime('%Y%m%d')

    updated = 0
    for idx, (_, row) in enumerate(df.iterrows()):
        sid    = str(row['stock_id'])
        market = str(row['market'])
        entry  = str(row['date'])
        updates = {}

        for n, col in [(1, 'price_t1'), (3, 'price_t3'), (5, 'price_t5')]:
            if pd.isna(row[col]):
                td = get_nth_trading_day_after(entry, n, trade_dates)
                if td and td <= today_str:
                    p = fetch_close_on_date(sid, market, td)
                    if p:
                        updates[col] = p
                        print(f'    {sid} T+{n}({td})={p}')
                    time.sleep(0.4)

        if updates:
            set_clause = ', '.join([f'{k} = ?' for k in updates])
            conn.execute(f'UPDATE stock_daily SET {set_clause} WHERE id = ?',
                         list(updates.values()) + [row['id']])
            updated += 1

        if (idx + 1) % 10 == 0:
            conn.commit()
            print(f'  進度：{idx+1}/{len(df)}')

    conn.commit()
    print(f'  ✅ 補抓完成，更新 {updated} 筆')

def detect_consecutive(conn):
    print('\n[連續入選偵測]')
    dates = pd.read_sql('SELECT DISTINCT date FROM stock_daily ORDER BY date DESC LIMIT 7', conn)['date'].tolist()
    if not dates:
        return pd.DataFrame()
    df = pd.read_sql(f'''
        SELECT stock_id, name, market, date, composite_score
        FROM stock_daily WHERE date IN ({",".join(["?"]*len(dates))})
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
    dates = pd.read_sql('SELECT DISTINCT date FROM stock_daily ORDER BY date DESC LIMIT 2', conn)['date'].tolist()
    if len(dates) < 2:
        return pd.DataFrame()
    today_dt, yesterday_dt = dates[0], dates[1]
    today_ids     = set(pd.read_sql('SELECT stock_id FROM stock_daily WHERE date=?', conn, params=[today_dt])['stock_id'])
    yesterday_ids = set(pd.read_sql('SELECT stock_id FROM stock_daily WHERE date=?', conn, params=[yesterday_dt])['stock_id'])
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
    df['ret_t1'] = (df['price_t1'] - df['entry_price']) / df['entry_price'] * 100
    df['ret_t3'] = (df['price_t3'] - df['entry_price']) / df['entry_price'] * 100
    df['ret_t5'] = (df['price_t5'] - df['entry_price']) / df['entry_price'] * 100
    df['cat'] = '強勢確認'
    df.loc[df['is_early_breakout'].str.upper()=='TRUE','cat'] = '起漲預警'
    df.loc[(df['is_strong_confirm'].str.upper()=='TRUE')&(df['is_early_breakout'].str.upper()=='TRUE'),'cat'] = '綜合轉強'
    results = {}
    for cat in ['綜合轉強','強勢確認','起漲預警','全部']:
        sub = df if cat=='全部' else df[df['cat']==cat]
        if not len(sub): continue
        r = {'count':len(sub),
             't1_win': round((sub['ret_t1']>0).mean()*100,1),
             't1_avg': round(sub['ret_t1'].mean(),2),
             't3_win': round((sub['ret_t3']>0).mean()*100,1),
             't3_avg': round(sub['ret_t3'].mean(),2)}
        if sub['ret_t5'].notna().any():
            r['t5_win'] = round((sub['ret_t5']>0).mean()*100,1)
            r['t5_avg'] = round(sub['ret_t5'].mean(),2)
        results[cat] = r
        print(f'  {cat}（{len(sub)}筆）T+3勝率={r["t3_win"]}% 均報={r["t3_avg"]}%')
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
    import pickle
    with open('data/analysis_cache.pkl','wb') as f:
        pickle.dump({'consec':consec_df,'new_entries':new_df,
                     'performance':perf,'generated':datetime.now().strftime('%Y-%m-%d %H:%M')}, f)
    print('\n✅ analyze 完成')

if __name__ == '__main__':
    main()
