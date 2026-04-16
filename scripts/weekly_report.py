"""
weekly_report.py — 每週五自動寄出週報 Email
內容：本週績效、Top10、黑名單警示、產業熱度
由 weekly_pipeline.yml 每週五觸發
"""
import sqlite3, smtplib, os
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timedelta

DB_PATH = 'data/stock_history.db'
GMAIL_USER    = os.environ.get('GMAIL_USER','')
GMAIL_PASS    = os.environ.get('GMAIL_APP_PASS','')
EMAIL_TO      = os.environ.get('EMAIL_TO','')
REPORT_URL    = os.environ.get('REPORT_URL','')

def main():
    if not all([GMAIL_USER, GMAIL_PASS, EMAIL_TO]):
        print('⚠️ Email 環境變數未設定'); return

    conn = sqlite3.connect(DB_PATH)
    today = datetime.today()
    week_ago = (today - timedelta(days=7)).strftime('%Y-%m-%d')

    # 本週績效
    rows = conn.execute('''
        SELECT close, price_t1, price_t3, is_strong_confirm, is_early_breakout
        FROM stock_daily WHERE date >= ? AND price_t3 IS NOT NULL
    ''', [week_ago]).fetchall()

    total = len(rows)
    if total:
        ret3 = [(r[2]-r[0])/r[0]*100 for r in rows if r[2] and r[0]]
        win3 = sum(1 for x in ret3 if x>0)
        avg3 = sum(ret3)/len(ret3) if ret3 else 0
        win_rate = round(win3/len(ret3)*100,1) if ret3 else 0
        avg_ret  = round(avg3,2)
    else:
        win_rate, avg_ret = None, None

    # Top10（本週平均分數）
    top10 = conn.execute('''
        SELECT stock_id, name, market, COUNT(*) as cnt, AVG(composite_score) as avg_cs
        FROM stock_daily WHERE date >= ?
        GROUP BY stock_id, name, market
        ORDER BY avg_cs DESC LIMIT 10
    ''', [week_ago]).fetchall()

    # 黑名單
    bl = conn.execute('''
        SELECT stock_id, name,
               SUM(CASE WHEN price_t3 IS NOT NULL AND (price_t3-close)/close < 0 THEN 1 ELSE 0 END) as neg3
        FROM stock_daily WHERE price_t3 IS NOT NULL
        GROUP BY stock_id, name HAVING neg3 >= 3
    ''').fetchall()

    conn.close()

    # 組裝 HTML Email
    perf_row = f'{win_rate}% 勝率 · 均報 +{avg_ret}%' if win_rate else '樣本不足'
    top10_rows = ''.join(f'<tr><td>{i+1}</td><td>{r[0]} {r[1]}</td><td>{r[2]}</td><td>{r[3]}天</td><td>{round(r[4],1)}</td></tr>'
                         for i,r in enumerate(top10))
    bl_rows = ''.join(f'<tr style="color:#c4572a;"><td>{b[0]}</td><td>{b[1]}</td><td>T+3負報酬{b[2]}次</td></tr>'
                      for b in bl) if bl else '<tr><td colspan="3">本週無黑名單</td></tr>'

    html_body = f'''<html><body style="font-family:monospace;background:#1c1510;color:#e8d9bc;padding:20px;">
<h2 style="color:#c4572a;font-size:18px;">台股雷達 週報 · {today.strftime("%Y/%m/%d")}</h2>
<p style="color:rgba(232,217,188,.5);font-size:12px;">{today.strftime("%Y/%m/%d")} · 本週摘要</p>

<h3 style="color:#c4a06e;margin-top:20px;">📊 本週績效</h3>
<p>{perf_row}</p>

<h3 style="color:#c4a06e;margin-top:20px;">🏆 本週 Top 10（平均分數）</h3>
<table border="0" cellpadding="6" style="width:100%;border-collapse:collapse;">
<tr style="color:rgba(232,217,188,.3);font-size:11px;"><th>#</th><th>股票</th><th>市場</th><th>出現</th><th>均分</th></tr>
{top10_rows}
</table>

<h3 style="color:#c4a06e;margin-top:20px;">⚠️ 黑名單警示</h3>
<table border="0" cellpadding="6" style="width:100%;border-collapse:collapse;">
<tr style="color:rgba(232,217,188,.3);font-size:11px;"><th>代碼</th><th>名稱</th><th>原因</th></tr>
{bl_rows}
</table>

<p style="margin-top:24px;"><a href="{REPORT_URL}" style="color:#c4572a;">→ 查看完整 Dashboard</a></p>
<p style="color:rgba(232,217,188,.2);font-size:10px;margin-top:16px;">TWSE · TPEX · FinMind — 僅供參考，不構成投資建議</p>
</body></html>'''

    msg = MIMEMultipart('mixed')
    msg['Subject'] = f'台股雷達週報 {today.strftime("%Y/%m/%d")} · T+3勝率 {win_rate or "—"}%'
    msg['From'] = GMAIL_USER
    msg['To'] = EMAIL_TO
    msg.attach(MIMEText(html_body, 'html', 'utf-8'))

    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as s:
        s.login(GMAIL_USER, GMAIL_PASS)
        s.sendmail(GMAIL_USER, EMAIL_TO.split(','), msg.as_string())
    print('✅ 週報 Email 已發送')

if __name__ == '__main__':
    main()
