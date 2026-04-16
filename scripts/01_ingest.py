"""
01_ingest.py — 讀取 CSV 寫入 SQLite
每次有新 tse_YYYYMMDD.csv 或 otc_YYYYMMDD.csv 進來就執行
UNIQUE(date, market, stock_id) 防止重複入庫
"""
import os
import glob
import sqlite3
import pandas as pd
from datetime import datetime

DB_PATH   = 'data/stock_history.db'
DATA_DIR  = 'data'

EXPECTED_COLS = [
    'stock_id', 'name', 'close', 'vol_ratio', 'daily_return_pct',
    'ma28_bias_pct', 'turnover_億', 'rsi14', 'inst_consec_days',
    'yoy_revenue_pct', 'foreign_today', 'trust_today',
    'foreign_3d', 'trust_3d', 'is_strong_confirm', 'is_early_breakout',
    'total_score', 'early_score', 'composite_score'
]

def init_db(conn):
    conn.execute('''
        CREATE TABLE IF NOT EXISTS stock_daily (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            date              TEXT,
            market            TEXT,
            stock_id          TEXT,
            name              TEXT,
            close             REAL,
            vol_ratio         REAL,
            daily_return_pct  REAL,
            ma28_bias_pct     REAL,
            turnover_億        REAL,
            rsi14             REAL,
            inst_consec_days  INTEGER,
            yoy_revenue_pct   REAL,
            foreign_today     REAL,
            trust_today       REAL,
            foreign_3d        REAL,
            trust_3d          REAL,
            is_strong_confirm TEXT,
            is_early_breakout TEXT,
            total_score       REAL,
            early_score       REAL,
            composite_score   REAL,
            price_t1          REAL,
            price_t3          REAL,
            price_t5          REAL,
            UNIQUE(date, market, stock_id)
        )
    ''')
    conn.commit()
    print('✅ DB 初始化完成')

def parse_market_date(filename):
    """從檔名解析 market 和 date，例如 tse_20260415.csv → ('TSE', '2026-04-15')"""
    base = os.path.basename(filename).replace('.csv', '')
    parts = base.split('_')
    if len(parts) < 2:
        return None, None
    market_raw = parts[0].upper()   # tse → TSE, otc → OTC
    date_raw   = parts[1]           # 20260415
    if len(date_raw) != 8:
        return None, None
    date_fmt = f'{date_raw[:4]}-{date_raw[4:6]}-{date_raw[6:]}'
    return market_raw, date_fmt

def ingest_csv(conn, filepath):
    market, date_str = parse_market_date(filepath)
    if not market or not date_str:
        print(f'  ⚠️ 無法解析檔名：{filepath}，跳過')
        return 0

    try:
        df = pd.read_csv(filepath, encoding='utf-8-sig', dtype=str)
    except Exception as e:
        print(f'  ❌ 讀取失敗：{filepath}，{e}')
        return 0

    # 防呆：確認欄位存在
    missing = [c for c in EXPECTED_COLS if c not in df.columns]
    if missing:
        print(f'  ❌ 缺少欄位：{missing}，跳過 {filepath}')
        return 0

    df['date']   = date_str
    df['market'] = market

    # 型別轉換
    bool_cols = ['is_strong_confirm', 'is_early_breakout']
    num_cols  = ['close','vol_ratio','daily_return_pct','ma28_bias_pct',
                 'turnover_億','rsi14','yoy_revenue_pct','foreign_today',
                 'trust_today','foreign_3d','trust_3d',
                 'total_score','early_score','composite_score']
    int_cols  = ['inst_consec_days']

    for c in num_cols:
        df[c] = pd.to_numeric(df[c], errors='coerce')
    for c in int_cols:
        df[c] = pd.to_numeric(df[c], errors='coerce').fillna(0).astype(int)
    for c in bool_cols:
        df[c] = df[c].astype(str).str.upper().str.strip()

    inserted = 0
    for _, row in df.iterrows():
        try:
            conn.execute('''
                INSERT OR IGNORE INTO stock_daily
                (date, market, stock_id, name, close, vol_ratio, daily_return_pct,
                 ma28_bias_pct, turnover_億, rsi14, inst_consec_days, yoy_revenue_pct,
                 foreign_today, trust_today, foreign_3d, trust_3d,
                 is_strong_confirm, is_early_breakout,
                 total_score, early_score, composite_score)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ''', (
                row['date'], row['market'], row['stock_id'], row['name'],
                row['close'], row['vol_ratio'], row['daily_return_pct'],
                row['ma28_bias_pct'], row['turnover_億'], row['rsi14'],
                row['inst_consec_days'], row['yoy_revenue_pct'],
                row['foreign_today'], row['trust_today'],
                row['foreign_3d'], row['trust_3d'],
                row['is_strong_confirm'], row['is_early_breakout'],
                row['total_score'], row['early_score'], row['composite_score']
            ))
            if conn.execute('SELECT changes()').fetchone()[0] > 0:
                inserted += 1
        except Exception as e:
            print(f'    ⚠️ 插入失敗 {row.get("stock_id","?")}：{e}')

    conn.commit()
    print(f'  ✅ {filepath} → {market} {date_str}，新增 {inserted} 筆')
    return inserted

def main():
    print('='*50)
    print(f'[01_ingest] {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
    print('='*50)

    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    # 找所有 CSV
    tse_files = sorted(glob.glob(f'{DATA_DIR}/tse_*.csv'))
    otc_files = sorted(glob.glob(f'{DATA_DIR}/otc_*.csv'))
    all_files = tse_files + otc_files

    if not all_files:
        print('⚠️ 找不到任何 CSV，結束')
        conn.close()
        return

    print(f'找到 {len(all_files)} 個 CSV（TSE:{len(tse_files)} OTC:{len(otc_files)}）')
    total = 0
    for f in all_files:
        total += ingest_csv(conn, f)

    # 統計
    row = conn.execute('SELECT COUNT(*), COUNT(DISTINCT date), COUNT(DISTINCT stock_id) FROM stock_daily').fetchone()
    print(f'\n📊 DB 統計：總筆數={row[0]}，交易日={row[1]}，不重複股票={row[2]}')
    conn.close()
    print(f'✅ ingest 完成，本次新增 {total} 筆')

if __name__ == '__main__':
    main()
