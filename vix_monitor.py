#!/usr/bin/env python3
"""VIX / VIXTWN 恐慌指數監控通知程式
========================================
推播規則：
- 往上突破新門檻 → 立即推播
- 維持同一門檻（未突破更高）→ 每天早上 10:00 推一次提醒
- 往下跌破（含跌出所有門檻）→ 不推播，靜默更新

台股（0050）門檻：VIXTWN > 30/32/35/38/40/42/45
美股（VOO）門檻：VIX > 25/28/30/32/35/38/40/42/45（複委託）

使用方式：
  python vix_monitor.py --check    # 執行一次VIX檢查（排程用）
  python vix_monitor.py --daily    # 發送每日早報（排程用）
  python vix_monitor.py --startup  # 發送啟動測試通知
"""
import argparse
import json
import os
import re
from datetime import datetime, date, timedelta

import pytz
import requests
import yfinance as yf

# ──────────────────────────────────────────────
# Telegram 設定
# ──────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get(
    "TELEGRAM_BOT_TOKEN",
    "8760860513:AAHcuVT48QbweNTjPE3LwulMh_F5VhqBps8"
)
TELEGRAM_CHAT_ID = os.environ.get(
    "TELEGRAM_CHAT_ID",
    "8776203440"
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "vix_state.json")
TW_TZ = pytz.timezone("Asia/Taipei")

# ──────────────────────────────────────────────
# 門檻設定（由高到低排列）
# 格式6 (門檻值, 建議說明)
# ──────────────────────────────────────────────
VIXTWN_THRESHOLDS = [
    (45, "🆘 史詩級恐慌！建議買進 0050【四張】"),
    (42, "🚨 極端恐慌！建議買進 0050【三張】"),
    (40, "🚨 極度恐慌！建議買進 0050【三張】"),
    (38, "🚨 強烈恐慌！建議買進 0050【兩張】"),
    (35, "⚠️ 高度恐慌！建議買進 0050【兩張】"),
    (32, "⚠️ 恐慌升溫！建議買進 0050【一張】"),
    (30, "📢 恐慌訊號！建議買進 0050【一張】"),
]

VIX_THRESHOLDS = [
    (45, "🆘 史詩級恐慌！建議複委託買進 VOO【十萬台幣】"),
    (42, "🚨 極端恐慌！建議複委託買進 VOO【七萬台幣】"),
    (40, "🚨 極度恐慌！建議複委託買進 VOO【六萬台幣】"),
    (38, "🚨 嚴重恐慌！建議複委託買進 VOO【五萬台幣】"),
    (35, "⚠️ 強烈恐慌！建議複委託買進 VOO【四萬台幣】"),
    (32, "⚠️ 恐慌加劇！建議複委託買進 VOO【三萬台幣】"),
    (30, "⚠️ 高度恐慌！建議複委託買進 VOO【兩萬台幣】"),
    (28, "📢 恐慌升溫！建議複委託買進 VOO【一萬五台幣】"),
    (25, "📢 恐慌訊號！建議複委託買進 VOO【一萬台幣】"),
]

# ──────────────────────────────────────────────
# 狀態管理
# state 結構：
#   vix_active      : 目前 VIX 觸及的最高門檻（int 或 null）
#   vix_daily_date  : 最後一次「維持提醒」的日期
#   vixtwn_active   : 目前 VIXTWN 觸及的最高門檻（int 或 null）
#   vixtwn_daily_date: 最後一次「維持提醒」的日期
#   last_daily_report: 每日早報日期
# ──────────────────────────────────────────────
def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {
        "vix_active": None,
        "vix_daily_date": "",
        "vixtwn_active": None,
        "vixtwn_daily_date": "",
        "last_daily_report": "",
    }


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ──────────────────────────────────────────────
# Telegram 通知
# ──────────────────────────────────────────────
def send_telegram(message: str) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        resp = requests.post(url, data=data, timeout=10)
        if resp.status_code == 200:
            print("[Telegram ✓] 通知已發送")
            return True
        print(f"[Telegram ✗] 失敗 HTTP {resp.status_code}: {resp.text}")
        return False
    except requests.RequestException as e:
        print(f"[Telegram ✗] 連線錯誤: {e}")
        return False


# ──────────────────────────────────────────────
# 資料抓取
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
    # ── 來源0a：TAIFEX 丷站 HTML（www.taifex.com.tw）─────────────────────
    try:
        resp = requests.get(
            "https://www.taifex.com.tw/cht/3/volIndx",
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
            },
            timeout=15,
        )
        if resp.status_code == 200:
            html = resp.text
            patterns = [
                r'VIXMTX.*?>\s*([\d]{2,3}\.[\d]{2})\s*<',
                r'波動率指數.*?>\s*([\d]{2,3}\.[\d]{2})\s*<',
                r'臺指.*?波動.*?>\s*([\d]{2,3}\.[\d]{2})\s*<',
                r'>([\d]{2,3}\.[\d]{2})<',  # 備用：任何符合範圍的數字
            ]
            for pat in patterns:
                for m in re.finditer(pat, html, re.DOTALL | re.IGNORECASE):
                    try:
                        val = float(m.group(1))
                        if 5 < val < 100:
                            print(f"[資料] VIXTWN (TAIFEX www HTML) = {val:.2f}")
                            return val
                    except ValueError:
                        continue
            print(f"[警告] TAIFEX www HTML 無法解析數值，長度: {len(html)}")
        else:
            print(f"[警告] TAIFEX www HTML HTTP {resp.status_code}")
    except Exception as e:
        print(f"[警告] TAIFEX www HTML 失敗: {e}")

    # ── $��源0b：TAIFEX $��站下載頁（盤後 CSV，向前找最近 3 天）───────────
    try:
        dl_headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://www.taifex.com.tw/cht/3/volIndx",
        }
        for delta in range(3):  # 今天 → 昨天 → 前天
            check_date = (date.today() - timedelta(days=delta)).strftime("%Y/%m/%d")
            try:
                resp = requests.get(
                    "https://www.taifex.com.tw/cht/3/volIndxDown",
                    params={
                        "queryStartDate": check_date,
                        "queryEndDate": check_date,
                        "button": "送出",
                    },
                    headers=dl_headers,
                    timeout=15,
                )
                if resp.status_code == 200 and len(resp.text) > 50:
                    matches = re.findall(r'\b(\d{2,3}\.\d{1,4})\b', resp.text)
                    for m in reversed(matches):
                        try:
                            val = float(m)
                            if 5 < val < 100:
                                print(f"[資料] VIXTWN (TAIFEX $��載 {check_date}) = {val:.2f}")
                                return val
                        except ValueError:
                            continue
            except Exception:
                continue
    except Exception as e:
        print(f"[警告] TAIFEX 下載頁失敗: {e}")

    # ── $��源1：TAIFEX MIS 行情網（可能已改為 WebSocket，備用）──────────
    try:
        resp = requests.get(
            "https://mis.taifex.com.tw/futures/VolatilityQuotes/",
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
        )
        if resp.status_code == 200:
            m = re.search(
                r"臺指選擇權波動率指數.*?<span[^>]*>([\d.]+)</span>",
                resp.text,
                re.DOTALL,
            )
            if m:
                val = float(m.group(1))
                print(f"[資料] VIXTWN (TAIFEX MIS) = {val:.2f}")
                return val
            print("[警告] TAIFEX MIS 頁面無法解析數值")
        else:
            print(f"[警告] TAIFEX MIS HTTP {resp.status_code}")
    except Exception as e:
        print(f"[警告] TAIFEX MIS 失敗: {e}")

    # ── 來源2：Yahoo Finance（^VXTWN，目前可能已下架）───────────────────
    try:
        hist = yf.Ticker("^VXTWN").history(period="5d")
        if not hist.empty:
            val = float(hist["Close"].iloc[-1])
            print(f"[資料] VIXTWN (Yahoo) = {val:.2f}")
            return val
    except Exception as e:
        print(f"[警告] Yahoo Finance ^VXTWN 失敗: {e}")

    print("[錯誤] 無法取得 VIXTWN 資料")
    return None


# ──────────────────────────────────────────────
# 核心推播判斷邏輯
# ──────────────────────────────────────────────
def process_index(value, thresholds, state, prefix, flag, label) -> str | None:
    """根據當前值與歷史狀態決定是否推播。回傳推播文字，或 None（不推播）。"""
    active_key = f"{prefix}_active"
    daily_key = f"{prefix}_daily_date"

    # 找出當前命中的最高門檻
    current_thr = None
    current_msg = None
    if value is not None:
        for thr, msg in thresholds:
            if value >= thr:
                current_thr = thr
                current_msg = msg
                break

    prev_thr = state.get(active_key)  # 上次記錄的門檻（None 或數字）
    now_tw = datetime.now(TW_TZ)
    today = now_tw.date().isoformat()
    val_s = f"{value:.2f}" if value is not None else "N/A"

    # ── 跌破所有門檻 ────────────────────────────────────────────────────
    if current_thr is None:
        if prev_thr is not None:
            print(f"[{label}] {val_s} 跌破所有門檻，重置狀態，不推播")
            state[active_key] = None
            state[daily_key] = ""
        else:
            print(f"[{label}] {val_s} 未超過任何門檻")
        return None

    # ── 往上突破新門檻 → 立即推播 ───────────────────────────────────────
    if prev_thr is None or current_thr > prev_thr:
        print(f"[{label}] 突破新門檻 {current_thr}（前：{prev_thr}），立即推播")
        state[active_key] = current_thr
        state[daily_key] = today  # $��天已推，跳過今天的維持提醒
        return (
            f"{flag} <b>{label}：{val_s}</b>（突破門檻 {current_thr}）\n{current_msg}"
        )

    # ── 往下跌至較低門檻 → 不推播 ──────────────────────────────────────
    if current_thr < prev_thr:
        print(f"[{label}] 從門檻 {prev_thr} 跌回門檻 {current_thr}，不推播")
        state[active_key] = current_thr
        return None

    # ── 維持同一門檻 → 每天 10:00 提醒一次 ────────────────────────────
    # current_thr == prev_thr
    if now_tw.hour == 10 and state.get(daily_key) != today:
        print(f"[{label}] 維持門檻 {current_thr}，10AM 每日提醒")
        state[daily_key] = today
        return (
            f"{flag} <b>{label}：{val_s}</b>（持續維持門檻 {current_thr}）\n"
            f"{current_msg}\n"
            f"📌 今日仍位於恐慌區間，請留意"
        )

    print(f"[{label}] {val_s} 維持門檻 {current_thr}，非推播時段")
    return None


# ──────────────────────────────────────────────
# 主要邏輯
# ──────────────────────────────────────────────
def check_and_alert() -> None:
    now_tw = datetime.now(TW_TZ)
    print(f"\n{'='*50}")
    print(f"[{now_tw.strftime('%Y-%m-%d %H:%M:%S')} 台灣時間] 開始檢查...")

    state = load_state()
    vix = get_vix()

    alerts = []
    r = process_index(vix, VIX_THRESHOLDS, state, "vix", "🇺🇸", "美股 VIX")
    if r:
        alerts.append(r)

    if alerts:
        msg = (
            "📊 <b>VIX 恐慌指數警報！</b>\n\n"
            + "\n\n".join(alerts)
            + "\n\n⚠️ 請至元大 App 手動執行交易\n（本系統僅通知，不自動下單）"
        )
        send_telegram(msg)

    save_state(state)

    # ── 補發晚報（GitHub Actions 排程不保證準時，21:00~22:59 內自動補發）──
    today_str = date.today().isoformat()
    if 21 <= now_tw.hour < 23 and state.get("last_daily_report") != today_str:
        print(f"[晚報] 21:00~22:59 內尚未發送今日晚報，補發中...")
        send_daily_report()


# ──────────────────────────────────────────────
# 每日晚報（台灣時間 21:00，美股開盤前）
# ──────────────────────────────────────────────
def send_daily_report() -> None:
    state = load_state()
    today = date.today().isoformat()
    if state.get("last_daily_report") == today:
        print(f"[晚報] 今天（{today}）已發過，略過")
        return

    now_tw = datetime.now(TW_TZ)
    vix = get_vix()

    def level(v):
        if v is None:
            return "─"
        if v >= 45:
            return "🆘 史詩恐慌"
        if v >= 40:
            return "🚨 極度恐慌"
        if v >= 35:
            return "🚨 高度恐慌"
        if v >= 30:
            return "⚠️ 恐慌訊號"
        if v >= 20:
            return "🟡 輕度波動"
        return "🟢 正常"

    vix_s = f"{vix:.2f}" if vix else "無法取得"

    msg = (
        f"📊 <b>VIX 每日晚報</b>\n"
        f"📅 {now_tw.strftime('%Y/%m/%d')} 晚安！\n\n"
        f"🇺🇸 美股 VIX：<b>{vix_s}</b> {level(vix)}\n\n"
        f"📌 <b>美股（VOO）門檻</b>：25／28／30／32／35／38／40／42／45\n\n"
        f"⚠️ 請至元大 App 手動執行交易"
    )
    if send_telegram(msg):
        state["last_daily_report"] = today
        save_state(state)
        print(f"[晚報] {today} 晚報已發送")


# ──────────────────────────────────────────────
# 啟動通知
# ──────────────────────────────────────────────
def send_startup_notification() -> None:
    now_tw = datetime.now(TW_TZ)
    msg = (
        f"✅ <b>VIX 監控系統已啟動</b>\n"
        f"⏰ {now_tw.strftime('%Y/%m/%d %H:%M:%S')}\n\n"
        f"📋 <b>推播規則：</b>\n"
        f" • 突破新門檻 → 立即推播\n"
        f" • 維持同一門檻 → 每天 10:00 提醒\n"
        f" • 跌破門檻 → 不推播\n\n"
        f"🇺🇸 美股（VOO）門檻：25 / 28 / 30 / 32 / 35 / 38 / 40 / 42 / 45\n\n"
        f"⚠️ 本系統僅通知，不自動下單"
    )
    send_telegram(msg)


# ──────────────────────────────────────────────
# 入口
# ──────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="VIX 恐慌指數監控")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="執行一次 VIX 檢查")
    group.add_argument("--daily", action="store_true", help="發送每日早報")
    group.add_argument("--startup", action="store_true", help="發送啟動通知")
    args = parser.parse_args()

    if args.daily:
        send_daily_report()
    elif args.startup:
        send_startup_notification()
    else:
        check_and_alert()
