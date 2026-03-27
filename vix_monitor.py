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

台股策略（0050）：VIXTWN > 30/32/35/38/40/42/45
美股策略（VOO）：VIX > 25/28/30/32/35/38/40/42/45（複委託）

使用方式：
  python vix_monitor.py --check     # 執行一次VIX檢查（排程用）
  python vix_monitor.py --daily     # 發送每日早報（排程用）
  python vix_monitor.py --startup   # 發送啟動測試通知
"""

import argparse
import json
import os
import time
from datetime import datetime, date

import pytz
import requests
import yfinance as yf

# ──────────────────────────────────────────────
#  Telegram 設定
#  優先讀取環境變數（GitHub Actions）
#  若無環境變數則使用下方預設值（本機執行用）
# ──────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get(
    "TELEGRAM_BOT_TOKEN",
    "8760860513:AAHcuVT48QbweNTjPE3LwulMh_F5VhqBps8"
)
TELEGRAM_CHAT_ID = os.environ.get(
    "TELEGRAM_CHAT_ID",
    "8776203440"
)

# 狀態檔案（與本程式同目錄）
BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "vix_state.json")

# 台灣時區
TW_TZ = pytz.timezone("Asia/Taipei")

# ──────────────────────────────────────────────
#  門檻設定（由高到低排列，命中最高門檻後停止）
#  格式: (門檻值, 建議說明, 唯一識別key)
# ──────────────────────────────────────────────
VIXTWN_THRESHOLDS = [
    (45, "🆘 史詩級恐慌！建議買進 0050【四張】",    "VIXTWN_45"),
    (42, "🚨 極端恐慌！建議買進 0050【三張】",       "VIXTWN_42"),
    (40, "🚨 極度恐慌！建議買進 0050【三張】",       "VIXTWN_40"),
    (38, "🚨 強烈恐慌！建議買進 0050【兩張】",       "VIXTWN_38"),
    (35, "⚠️ 高度恐慌！建議買進 0050【兩張】",      "VIXTWN_35"),
    (32, "⚠️ 恐慌升溫！建議買進 0050【一張】",      "VIXTWN_32"),
    (30, "📢 恐慌訊號！建議買進 0050【一張】",       "VIXTWN_30"),
]

VIX_THRESHOLDS = [
    (45, "🆘 史詩級恐慌！建議複委託買進 VOO【十萬台幣】",   "VIX_45"),
    (42, "🚨 極端恐慌！建議複委託買進 VOO【七萬台幣】",     "VIX_42"),
    (40, "🚨 極度恐慌！建議複委託買進 VOO【六萬台幣】",     "VIX_40"),
    (38, "🚨 嚴重恐慌！建議複委託買進 VOO【五萬台幣】",     "VIX_38"),
    (35, "⚠️ 強烈恐慌！建議複委託買進 VOO【四萬台幣】",    "VIX_35"),
    (32, "⚠️ 恐慌加劇！建議複委託買進 VOO【三萬台幣】",    "VIX_32"),
    (30, "⚠️ 高度恐慌！建議複委託買進 VOO【兩萬台幣】",    "VIX_30"),
    (28, "📢 恐慌升溫！建議複委託買進 VOO【一萬五台幣】",   "VIX_28"),
    (25, "📢 恐慌訊號！建議複委託買進 VOO【一萬台幣】",    "VIX_25"),
]

ALERT_COOLDOWN_SECS = 3600  # 1 小時不重複

# ──────────────────────────────────────────────
#  狀態管理
# ──────────────────────────────────────────────
def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {"last_alerts": {}, "last_daily_report": ""}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def can_alert(state: dict, alert_key: str) -> bool:
    now  = time.time()
    last = state.get("last_alerts", {}).get(alert_key, 0)
    return (now - last) >= ALERT_COOLDOWN_SECS


def mark_alerted(state: dict, alert_key: str) -> None:
    state.setdefault("last_alerts", {})[alert_key] = time.time()


# ──────────────────────────────────────────────
#  Telegram 通知
# ──────────────────────────────────────────────
def send_telegram(message: str) -> bool:
    url  = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {
        "chat_id":    TELEGRAM_CHAT_ID,
        "text":       message,
        "parse_mode": "HTML",
    }
    try:
        resp = requests.post(url, data=data, timeout=10)
        if resp.status_code == 200:
            print("[Telegram ✓] 通知已發送")
            return True
        else:
            print(f"[Telegram ✗] 失敗 HTTP {resp.status_code}: {resp.text}")
            return False
    except requests.RequestException as e:
        print(f"[Telegram ✗] 連線錯誤: {e}")
        return False


# ──────────────────────────────────────────────
#  資料抓取
# ──────────────────────────────────────────────
def get_vix() -> float | None:
    try:
        hist = yf.Ticker("^VIX").history(period="5d")
        if not hist.empty:
            val = float(hist["Close"].iloc[-1])
            print(f"[資料] VIX = {val:.2f}")
            return val
    except Exception as e:
        print(f"[錯誤] 取得 VIX 失敗: {e}")
    return None


def get_vixtwn() -> float | None:
    # 主要來源：Yahoo Finance
    try:
        hist = yf.Ticker("^VXTWN").history(period="5d")
        if not hist.empty:
            val = float(hist["Close"].iloc[-1])
            print(f"[資料] VIXTWN = {val:.2f}")
            return val
    except Exception as e:
        print(f"[警告] Yahoo Finance ^VXTWN 失敗: {e}")

    # 備用來源：TAIFEX 官網
    try:
        import re
        resp = requests.get(
            "https://www.taifex.com.tw/cht/11/vixFrontMonthDetail",
            timeout=10, headers={"User-Agent": "Mozilla/5.0"}
        )
        m = re.search(r"台灣波動率指數[^\d]*([\.\d]+)", resp.text)
        if m:
            val = float(m.group(1))
            print(f"[資料] VIXTWN (TAIFEX) = {val:.2f}")
            return val
    except Exception as e:
        print(f"[警告] TAIFEX 備用來源失敗: {e}")

    print("[錯誤] 無法取得 VIXTWN 資料")
    return None


# ──────────────────────────────────────────────
#  主要邏輯
# ──────────────────────────────────────────────
def check_and_alert() -> None:
    now_tw = datetime.now(TW_TZ)
    print(f"\n{'='*50}")
    print(f"[{now_tw.strftime('%Y-%m-%d %H:%M:%S')} 台灣時間] 開始檢查...")

    state  = load_state()
    vix    = get_vix()
    vixtwn = get_vixtwn()
    alerts = []

    # 檢查 VIXTWN（命中最高門檻即停，各門檻有獨立冷卻）
    if vixtwn is not None:
        for threshold, message, key in VIXTWN_THRESHOLDS:
            if vixtwn >= threshold:
                if can_alert(state, key):
                    alerts.append(
                        f"🇹🇼 <b>台灣 VIXTWN：{vixtwn:.2f}</b>（門檻 {threshold}）\n{message}"
                    )
                    mark_alerted(state, key)
                else:
                    print(f"[冷卻] {key} 尚在冷卻期，跳過")
                break

    # 檢查 VIX（命中最高門檻即停，各門檻有獨立冷卻）
    if vix is not None:
        for threshold, message, key in VIX_THRESHOLDS:
            if vix >= threshold:
                if can_alert(state, key):
                    alerts.append(
                        f"🇺🇸 <b>美股 VIX：{vix:.2f}</b>（門檻 {threshold}）\n{message}"
                    )
                    mark_alerted(state, key)
                else:
                    print(f"[冷卻] {key} 尚在冷卻期，跳過")
                break

    if alerts:
        msg = (
            "📊 <b>VIX 恐慌指數警報！</b>\n\n"
            + "\n\n".join(alerts)
            + "\n\n⚠️ 請至元大 App 手動執行交易\n（本系統僅通知，不自動下單）"
        )
        send_telegram(msg)
    else:
        vix_s    = f"{vix:.2f}"    if vix    else "N/A"
        vixtwn_s = f"{vixtwn:.2f}" if vixtwn else "N/A"
        print(f"[正常] VIX={vix_s}，VIXTWN={vixtwn_s}，未超過任何門檻")

    save_state(state)


def send_daily_report() -> None:
    state = load_state()
    today = date.today().isoformat()

    if state.get("last_daily_report") == today:
        print(f"[日報] 今天（{today}）已發過，略過")
        return

    now_tw = datetime.now(TW_TZ)
    vix    = get_vix()
    vixtwn = get_vixtwn()

    def level_tw(v):
        if v is None: return "─"
        if v >= 45:   return "🆘 史詩恐慌"
        if v >= 40:   return "🚨 極度恐慌"
        if v >= 35:   return "🚨 高度恐慌"
        if v >= 30:   return "⚠️ 恐慌訊號"
        if v >= 20:   return "🟡 輕度波動"
        return              "🟢 正常"

    vix_s    = f"{vix:.2f}"    if vix    else "無法取得"
    vixtwn_s = f"{vixtwn:.2f}" if vixtwn else "無法取得"

    msg = (
        f"📊 <b>VIX 每日早報</b>\n"
        f"📅 {now_tw.strftime('%Y/%m/%d')} 早安！\n\n"
        f"🇹🇼 台灣 VIXTWN：<b>{vixtwn_s}</b>\n"
        f"   狀態：{level_tw(vixtwn)}\n\n"
        f"🇺🇸 美股 VIX：<b>{vix_s}</b>\n"
        f"   狀態：{level_tw(vix)}\n\n"
        f"📌 <b>台股（0050）警報門檻</b>\n"
        f"   30／32／35／38／40／42／45\n\n"
        f"📌 <b>美股（VOO 複委託）警報門檻</b>\n"
        f"   25／28／30／32／35／38／40／42／45\n\n"
        f"⚠️ 請至元大 App 手動執行交易"
    )

    if send_telegram(msg):
        state["last_daily_report"] = today
        save_state(state)
        print(f"[日報] {today} 早報已發送")


def send_startup_notification() -> None:
    now_tw = datetime.now(TW_TZ)
    msg = (
        f"✅ <b>VIX 監控系統已啟動</b>\n"
        f"⏰ {now_tw.strftime('%Y/%m/%d %H:%M:%S')}\n\n"
        f"📋 <b>監控設定：</b>\n"
        f"  • 每 5 分鐘檢查一次\n"
        f"  • 每天 09:00 發送日報\n"
        f"  • 相同警報 1 小時冷卻\n\n"
        f"🇹🇼 台股（0050）門檻：30 / 32 / 35 / 38 / 40 / 42 / 45\n"
        f"🇺🇸 美股（VOO）門檻：25 / 28 / 30 / 32 / 35 / 38 / 40 / 42 / 45\n\n"
        f"⚠️ 本系統僅通知，不自動下單"
    )
    send_telegram(msg)


# ──────────────────────────────────────────────
#  入口
# ──────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="VIX 恐慌指數監控")
    group  = parser.add_mutually_exclusive_group()
    group.add_argument("--check",   action="store_true", help="執行一次 VIX 檢查")
    group.add_argument("--daily",   action="store_true", help="發送每日早報")
    group.add_argument("--startup", action="store_true", help="發送啟動通知")
    args = parser.parse_args()

    if args.daily:
        send_daily_report()
    elif args.startup:
        send_startup_notification()
    else:
        check_and_alert()
