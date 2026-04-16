"""
02_analyze.py — 連續入選偵測 + 補抓 T+1/T+3/T+5 價格
使用 TWSE/TPEX OpenAPI 抓價格，不用 yfinance
"""
import sqlite3
import requests
import pandas as pd
import time
from datetime import datetime, timedelta

DB_PATH = 'data/stock_history.db'

# ── 抓股價（TWSE/TPEX 免費 API）──────────────────────────────
def get_trade_dates(n=10):
    """從 TWSE 拿最近 n 個交易日清單"""
    dates = []
    d = datetime.today()
    checked = 0
    while len(dates) < n and checked < 30:
        if d.weekday() < 5:
            dates.append(d.strftime('%Y%m%d'))
        d -= timedelta(days=1)
        checked += 1
    return sorted(dates)

def fetch_price_twse(stock_id, date_str):
    """
    用 TWSE OpenAPI 抓單日收盤價
    date_str 格式：YYYYMMDD
    """
    url = f'https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY?date={date_str}&stockNo={stock_id}&response=json'
    try:
        r = requests.get(url, timeout=15, headers={'User-Agent': 'Mozilla/5.0'})
        j = r.json()
        if j.get('stat') != 'OK' or not j.get('data'):
            return None
        # 取最後一筆（該月最後交易日）
        last_row = j['data'][-1]
        # 欄位：日期,成交股數,成交金額,開盤價,最高價,最低價,收盤價,漲跌價差,成交筆數
        close_str = last_row[6].replace(',', '').strip()
        if close_str in ('--', ''):
            return None
        return float(close_str)
    except Exception as e:
        return None

def fetch_price_tpex(stock_id, date_str):
    """
    用 TPEX OpenAPI 抓上櫃股收盤價
    date_str 格式：YYYYMMDD
    """
    # 轉換民國年
    year = int(date_str[:4]) - 1911
    mmdd = date_str[4:6] + '/' + date_str[6:]
    date_tw = f'{year}/{mmdd}'
    url = f'https://www.tpex.org.tw/web/stock/aftertrading/daily_trading_info/st43_result.php?l=zh-tw&d={date_tw}&s={stock_id}'
    try:
        r = requests.get(url, timeout=15, headers={'User-Agent': 'Mozilla/5.0'})
        j = r.json()
        if not j.get('aaData'):
            return None
        last_row = j['aaData'][-1]
        # TPEX 欄位：日期,成交量,成交金額,開盤,最高,最低,收盤,漲跌,均價
        close_str = str(last_row[6]).replace(',', '').strip()
        if close_str in ('--', ''):
            return None
        return float(close_str)
    except Exception as e:
        return None

def fetch_close_price(stock_id, market, target_date_str):
    """
    target_date_str：YYYYMMDD
    先試 TWSE（上市），失敗再試 TPEX（上櫃）
    """
    if market == 'TSE':
        price = fetch_price_twse(stock_id, target_date_str)
        if price is None:
            price = fetch_price_tpex(stock_id, target_date_str)
    else:
        price = fetch_price_tpex(stock_id, target_date_str)
        if price is None:
            price = fetch_price_twse(stock_id, target_date_str)
    return price

def get_nth_trading_day(entry_date_str, n, trade_dates_sorted):
    """
    從 trade_dates_sorted（YYYYMMDD 升序）找 entry_date 之後第 n 個交易日
    """
    entry = entry_date_str.replace('-', '')
    future = [d for d in trade_dates_sorted if d > entry]
    if len(future) >= n:
        return future[n - 1]
    return None

# ── 補抓價格 ─────────────────────────────────────────────────
def fill_future_prices(conn):
    print('\n[補抓價格]')

    # 找出還缺 T+1/T+3/T+5 的紀錄，且入選日至少 1 天前
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

    # 準備交易日曆（最近 60 天）
    trade_dates = []
    d = datetime.today()
    for _ in range(90):
        if d.weekday() < 5:
            ds = d.strftime('%Y%m%d')
            # 簡單確認是否為交易日（週一到週五，跳過明顯國定假日略過）
            trade_dates.append(ds)
        d -= timedelta(days=1)
    trade_dates = sorted(trade_dates)

    updated = 0
    for _, row in df.iterrows():
        stock_id = str(row['stock_id'])
        market   = str(row['market'])
        entry    = str(row['date']).replace('-', '')

        updates = {}

        # T+1
        if pd.isna(row['price_t1']):
            td = get_nth_trading_day(entry, 1, trade_dates)
            if td and td <= datetime.today().strftime('%Y%m%d'):
                p = fetch_close_price(stock_id, market, td)
                if p: updates['price_t1'] = p
                time.sleep(0.3)

        # T+3
        if pd.isna(row['price_t3']):
            td = get_nth_trading_day(entry, 3, trade_dates)
            if td and td <= datetime.today().strftime('%Y%m%d'):
                p = fetch_close_price(stock_id, market, td)
                if p: updates['price_t3'] = p
                time.sleep(0.3)

        # T+5
        if pd.isna(row['price_t5']):
            td = get_nth_trading_day(entry, 5, trade_dates)
            if td and td <= datetime.today().strftime('%Y%m%d'):
                p = fetch_close_price(stock_id, market, td)
                if p: updates['price_t5'] = p
                time.sleep(0.3)

        if updates:
            set_clause = ', '.join([f'{k} = ?' for k in updates])
            vals = list(updates.values()) + [row['id']]
            conn.execute(f'UPDATE stock_daily SET {set_clause} WHERE id = ?', vals)
            updated += 1

    conn.commit()
    print(f'  ✅ 補抓完成，更新 {updated} 筆')

# ── 連續入選偵測 ──────────────────────────────────────────────
def detect_consecutive(conn):
    print('\n[連續入選偵測]')
    # 取最近 7 個交易日
    dates = pd.read_sql('''
        SELECT DISTINCT date FROM stock_daily
        ORDER BY date DESC LIMIT 7
    ''', conn)['date'].tolist()

    if not dates:
        print('  ⚠️ 無資料')
        return pd.DataFrame()

    df = pd.read_sql(f'''
        SELECT stock_id, name, market, date, composite_score,
               is_strong_confirm, is_early_breakout
        FROM stock_daily
        WHERE date IN ({",".join(["?"] * len(dates))})
    ''', conn, params=dates)

    # 各股在 7 天內出現幾次
    summary = df.groupby(['stock_id', 'name', 'market']).agg(
        appear_count=('date', 'count'),
        avg_score=('composite_score', 'mean'),
        max_score=('composite_score', 'max'),
        latest_date=('date', 'max')
    ).reset_index().sort_values('appear_count', ascending=False)

    print(f'  7日內有 {len(summary)} 檔曾入選')
    return summary

# ── 新進榜偵測 ────────────────────────────────────────────────
def detect_new_entries(conn):
    print('\n[新進榜偵測]')
    dates = pd.read_sql('''
        SELECT DISTINCT date FROM stock_daily
        ORDER BY date DESC LIMIT 2
    ''', conn)['date'].tolist()

    if len(dates) < 2:
        print('  ⚠️ 資料不足（需至少2個交易日）')
        return pd.DataFrame()

    today_dt, yesterday_dt = dates[0], dates[1]

    today_ids = set(pd.read_sql(
        'SELECT stock_id FROM stock_daily WHERE date = ?', conn, params=[today_dt]
    )['stock_id'].tolist())

    yesterday_ids = set(pd.read_sql(
        'SELECT stock_id FROM stock_daily WHERE date = ?', conn, params=[yesterday_dt]
    )['stock_id'].tolist())

    new_ids = today_ids - yesterday_ids
    if not new_ids:
        print('  今日無新進榜')
        return pd.DataFrame()

    placeholders = ','.join(['?'] * len(new_ids))
    new_df = pd.read_sql(f'''
        SELECT stock_id, name, market, close, composite_score,
               is_strong_confirm, is_early_breakout
        FROM stock_daily
        WHERE date = ? AND stock_id IN ({placeholders})
        ORDER BY composite_score DESC
    ''', conn, params=[today_dt] + list(new_ids))

    print(f'  今日新進榜：{len(new_df)} 檔')
    return new_df

# ── 績效統計 ─────────────────────────────────────────────────
def calc_performance(conn):
    print('\n[績效統計]')
    df = pd.read_sql('''
        SELECT stock_id, name, market,
               close as entry_price,
               price_t1, price_t3, price_t5,
               is_strong_confirm, is_early_breakout,
               composite_score
        FROM stock_daily
        WHERE price_t3 IS NOT NULL
    ''', conn)

    if df.empty:
        print('  ⚠️ 尚無足夠績效資料（需等 T+3 價格）')
        return {}

    df['ret_t1'] = (df['price_t1'] - df['entry_price']) / df['entry_price'] * 100
    df['ret_t3'] = (df['price_t3'] - df['entry_price']) / df['entry_price'] * 100
    df['ret_t5'] = ((df['price_t5'] - df['entry_price']) / df['entry_price'] * 100
                    if 'price_t5' in df.columns else None)

    # 分類
    df['category'] = '強勢確認'
    df.loc[df['is_early_breakout'].str.upper() == 'TRUE', 'category'] = '起漲預警'
    df.loc[
        (df['is_strong_confirm'].str.upper() == 'TRUE') &
        (df['is_early_breakout'].str.upper() == 'TRUE'),
        'category'
    ] = '綜合轉強'

    results = {}
    for cat in ['綜合轉強', '強勢確認', '起漲預警', '全部']:
        sub = df if cat == '全部' else df[df['category'] == cat]
        if len(sub) == 0:
            continue
        r = {
            'count':     len(sub),
            't1_win':    round((sub['ret_t1'] > 0).mean() * 100, 1) if sub['ret_t1'].notna().any() else None,
            't1_avg':    round(sub['ret_t1'].mean(), 2) if sub['ret_t1'].notna().any() else None,
            't3_win':    round((sub['ret_t3'] > 0).mean() * 100, 1),
            't3_avg':    round(sub['ret_t3'].mean(), 2),
        }
        if df['ret_t5'].notna().any():
            r['t5_win'] = round((sub['ret_t5'] > 0).mean() * 100, 1)
            r['t5_avg'] = round(sub['ret_t5'].mean(), 2)
        results[cat] = r
        print(f'  {cat}（{len(sub)}筆）T+3勝率={r["t3_win"]}% 均報={r["t3_avg"]}%')

    return results

def main():
    print('='*50)
    print(f'[02_analyze] {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
    print('='*50)

    conn = sqlite3.connect(DB_PATH)

    fill_future_prices(conn)
    consec_df  = detect_consecutive(conn)
    new_df     = detect_new_entries(conn)
    perf       = calc_performance(conn)

    conn.close()

    # 存成暫存供 03_build_html.py 讀取
    import pickle
    with open('data/analysis_cache.pkl', 'wb') as f:
        pickle.dump({
            'consec':      consec_df,
            'new_entries': new_df,
            'performance': perf,
            'generated':   datetime.now().strftime('%Y-%m-%d %H:%M')
        }, f)

    print('\n✅ analyze 完成，cache 已存')

if __name__ == '__main__':
    main()
