"""
cleanup_db.py — 一次性清理髒資料
刪除 is_strong_confirm 和 is_early_breakout 都不是 TRUE 的紀錄
執行後印出刪了幾筆、剩下幾筆
"""
import sqlite3
import os

DB_PATH = 'data/stock_history.db'

def main():
    if not os.path.exists(DB_PATH):
        print(f'❌ 找不到 DB：{DB_PATH}')
        return

    conn = sqlite3.connect(DB_PATH)

    # 先看看有多少壞資料
    bad_count = conn.execute('''
        SELECT COUNT(*) FROM stock_daily
        WHERE UPPER(TRIM(is_strong_confirm)) != 'TRUE'
          AND UPPER(TRIM(is_early_breakout)) != 'TRUE'
    ''').fetchone()[0]

    total_before = conn.execute('SELECT COUNT(*) FROM stock_daily').fetchone()[0]

    print(f'清理前總筆數：{total_before}')
    print(f'待刪除（兩個都不是TRUE）：{bad_count} 筆')

    if bad_count == 0:
        print('✅ 資料庫已乾淨，無需清理')
        conn.close()
        return

    # 刪除壞資料
    conn.execute('''
        DELETE FROM stock_daily
        WHERE UPPER(TRIM(is_strong_confirm)) != 'TRUE'
          AND UPPER(TRIM(is_early_breakout)) != 'TRUE'
    ''')
    conn.commit()

    total_after = conn.execute('SELECT COUNT(*) FROM stock_daily').fetchone()[0]
    print(f'✅ 刪除完成，剩餘：{total_after} 筆（刪了 {total_before - total_after} 筆）')

    # 順便印出各日期剩餘筆數確認
    print('\n各日期剩餘筆數：')
    rows = conn.execute('''
        SELECT date, market, COUNT(*) as cnt
        FROM stock_daily
        GROUP BY date, market
        ORDER BY date DESC
        LIMIT 20
    ''').fetchall()
    for r in rows:
        print(f'  {r[0]} {r[1]}: {r[2]} 筆')

    conn.close()

if __name__ == '__main__':
    main()
