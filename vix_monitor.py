#!/usr/bin/env python3
"""
VIX / VIXTWN 恐慌指數監控通知程式
========================================
功能：
  - 每 5 分鐘抓取 VIX（美股）和 VIXTWN（台股）指數
  - 超過門檻時透過 Telegram Bot 推播建議（不自動下單）
  - 每天早上 9 點發一次日報
  - 同一警報 1 小時內不重複通知
  - 狀態存檔，重啟後不重複通知
"""
import argparse
import json
import os
import time
from datetime import datetime, date
import pytz
import requests
import yfinance as yf

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN","8760860513:AAHcuVT48QbweNTjPE3LwulMh_F5VhqBps8")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID","8776203440")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "vix_state.json")
TW_TZ = pytz.timezone("Asia/Taipei")
VIXTWN_THRESHOLDS = [(40,"🚨 極度恐慌！建議買進 0050【三張】","VIXTWN_40"),(30,"⚠️ 高度恐慌！建議買進 0050【兩張】","VIXTWN_30"),(25,"📢 恐慌訊號！建議買進 0050【一張】","VIXTWN_25")]
VIX_THRESHOLDS = [(40,"🚨 極度恐慌！建議複委託買進 VOO【五萬台幣】","VIX_40"),(30,"⚠️ 高度恐慌！建議複委託買進 VOO【三萬台幣】","VIX_30"),(25,"📢 恐慌訊號！建議複委託買進 VOO【一萬台幣】","VIX_25")]
ALERT_COOLDOWN_SECS = 3600

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE,"r",encoding="utf-8") as f: return json.load(f)
        except: pass
    return {"last_alerts":{},"last_daily_report":""}

def save_state(state):
    with open(STATE_FILE,"w",encoding="utf-8") as f: json.dump(state,f,ensure_ascii=False,indent=2)

def can_alert(state,key):
    return (time.time()-state.get("last_alerts",{}).get(key,0))>=ALERT_COOLDOWN_SECS

def mark_alerted(state,key):
    state.setdefault("last_alerts",{})[key]=time.time()

def send_telegram(message):
    url=f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp=requests.post(url,data={"chat_id":TELEGRAM_CHAT_ID,"text":message,"parse_mode":"HTML"},timeout=10)
        if resp.status_code==200: print("[Telegram ✓] 通知已發送"); return True
        print(f"[Telegram ✗] HTTP {resp.status_code}"); return False
    except Exception as e: print(f"[Telegram ✗] {e}"); return False

def get_vix():
    try:
        hist=yf.Ticker("^VIX").history(period="5d")
        if not hist.empty: val=float(hist["Close"].iloc[-1]); print(f"[資料] VIX={val:.2f}"); return val
    except Exception as e: print(f"[錯誤] VIX: {e}")
    return None

def get_vixtwn():
    try:
        hist=yf.Ticker("^VXTWN").history(period="5d")
        if not hist.empty: val=float(hist["Close"].iloc[-1]); print(f"[資料] VIXTWN={val:.2f}"); return val
    except Exception as e: print(f"[警告] VXTWN: {e}")
    try:
        import re
        resp=requests.get("https://www.taifex.com.tw/cht/11/vixFrontMonthDetail",timeout=10,headers={"User-Agent":"Mozilla/5.0"})
        m=re.search(r"台灣波動率指數[^\d]*([\d.]+)",resp.text)
        if m: val=float(m.group(1)); print(f"[資料] VIXTWN(TAIFEX)={val:.2f}"); return val
    except Exception as e: print(f"[警告] TAIFEX: {e}")
    return None

def check_and_alert():
    now_tw=datetime.now(TW_TZ)
    print(f"\n{'='*50}\n[{now_tw.strftime('%Y-%m-%d %H:%M:%S')} 台灣時間] 開始檢查...")
    state=load_state(); vix=get_vix(); vixtwn=get_vixtwn(); alerts=[]
    if vixtwn is not None:
        for thr,msg,key in VIXTWN_THRESHOLDS:
            if vixtwn>=thr:
                if can_alert(state,key): alerts.append(f"🇹🇼 <b>台灣 VIXTWN：{vixtwn:.2f}</b>（門檻{thr}）\n{msg}"); mark_alerted(state,key)
                else: print(f"[冷卻] {key}")
                break
    if vix is not None:
        for thr,msg,key in VIX_THRESHOLDS:
            if vix>=thr:
                if can_alert(state,key): alerts.append(f"🇺🇸 <b>美股 VIX：{vix:.2f}</b>（門檻{thr}）\n{msg}"); mark_alerted(state,key)
                else: print(f"[冷卻] {key}")
                break
    if alerts: send_telegram("📊 <b>VIX 恐慌指數警報！</b>\n\n"+"\n\n".join(alerts)+"\n\n⚠️ 請至元大 App 手動執行交易")
    else: print(f"[正常] VIX={vix or 'N/A'} VIXTWN={vixtwn or 'N/A'}")
    save_state(state)

def send_daily_report():
    state=load_state(); today=date.today().isoformat()
    if state.get("last_daily_report")==today: print("日報已發過"); return
    now_tw=datetime.now(TW_TZ); vix=get_vix(); vixtwn=get_vixtwn()
    def lv(v):
        if v is None: return "─"
        if v>=40: return "🚨極度恐慌"
        if v>=30: return "⚠️高度恐慌"
        if v>=25: return "📢恐慌訊號"
        if v>=20: return "🟡輕度恐慌"
        return "🟢正常"
    msg=(f"📊 <b>VIX 每日早報</b>\n📅 {now_tw.strftime('%Y/%m/%d')} 早安！\n\n"
         f"🇹🇼 VIXTWN：<b>{vixtwn:.2f if vixtwn else 'N/A'}</b> {lv(vixtwn)}\n"
         f"🇺🇸 VIX：<b>{vix:.2f if vix else 'N/A'}</b> {lv(vix)}\n\n"
         f"📌 0050：&gt;25買1張 &gt;30買2張 &gt;40買3張\n"
         f"📌 VOO複委託：&gt;25買1萬 &gt;30買3萬 &gt;40買5萬\n\n"
         f"⚠️ 請至元大 App 手動執行交易")
    if send_telegram(msg): state["last_daily_report"]=today; save_state(state)

def send_startup_notification():
    now_tw=datetime.now(TW_TZ)
    send_telegram(f"✅ <b>VIX 監控系統已啟動</b>\n⏰ {now_tw.strftime('%Y/%m/%d %H:%M:%S')}\n\n"
                  f"📋 每15分鐘檢查・每天09:00日報・1小時冷卻\n"
                  f"🇹🇼 0050門檻：25/30/40　🇺🇸 VOO門檻：25/30/40\n⚠️ 僅通知不自動下單")

if __name__=="__main__":
    parser=argparse.ArgumentParser()
    g=parser.add_mutually_exclusive_group()
    g.add_argument("--check",action="store_true")
    g.add_argument("--daily",action="store_true")
    g.add_argument("--startup",action="store_true")
    args=parser.parse_args()
    if args.daily: send_daily_report()
    elif args.startup: send_startup_notification()
    else: check_and_alert()
