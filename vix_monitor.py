#!/usr/bin/env python3
"""VIX 恐慌指數監控通知程式（含市場全景分析）
========================================
推播規則：
- 往上突破新門檻 → 立即推播（含市場快照＋診斷）
- 維持同一門檻（未突破更高）→ 每天早上 10:00 提醒一次
- 往下跌破（含跌出所有門檻）→ 不推播，靜默更新

美股（VOO）門檻：VIX > 25/28/30/32/35/38/40/42/45（複委託）

使用方式：
  python vix_monitor.py --check    # 執行一次VIX檢查（排程用）
  python vix_monitor.py --daily    # 發送每日晚報（排程用）
  python vix_monitor.py --startup  # 發送啟動測試通知
"""
import argparse
import json
import os
from datetime import datetime, date

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
# ──────────────────────────────────────────────
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


def get_market_snapshot() -> dict:
    """
    抓取市場全景指標：
      spx  = S&P 500 指數（^GSPC）
      hyg  = 高收益債 ETF（HYG）—— 信用市場壓力指標
      dxy  = 美元指數（DX-Y.NYB）—— 避險資金流向
      gold = 黃金期貨（GC=F）—— 終極避險資產
      tnx  = 10年期美債殖利率（^TNX）—— 利率環境
    每項包含: price（最新收盤）、chg_pct（與前一交易日漲跌幅%）
    spx 額外包含: drawdown（距52週高點跌幅%）
    """
    snapshot: dict = {}

    tickers = {
        "spx":  "^GSPC",
        "hyg":  "HYG",
        "dxy":  "DX-Y.NYB",
        "gold": "GC=F",
        "tnx":  "^TNX",
    }

    for key, symbol in tickers.items():
        try:
            hist = yf.Ticker(symbol).history(period="5d")
            if not hist.empty:
                last = float(hist["Close"].iloc[-1])
                chg_pct = None
                if len(hist) >= 2:
                    prev = float(hist["Close"].iloc[-2])
                    if prev != 0:
                       chg_pct = (last - prev) / prev * 100
                snapshot[key] = {"price": last, "chg_pct": chg_pct}
                chg_s = f" ({'+' if (chg_pct or 0)>=0 else ''}{chg_pct:.2f}%)" if chg_pct is not None else ""
                print(f"[資料] {key.upper()} = {last:.2f}{chg_s}")
            else:
                snapshot[key] = None
                print(f"[警告] {symbol} 無資料")
        except Exception as e:
            snapshot[key] = None
            print(f"[警告] 無法取得 {symbol}: {e}")

    # S&P 500 距52週高點跌幅（作為「距歷史高點」代理指標）
    try:
        hist_1y = yf.Ticker("^GSPC").history(period="1y")
        if not hist_1y.empty:
            high_1y = float(hist_1y["High"].max())
            spx_data = snapshot.get("spx")
            if spx_data and high_1y > 0:
                dd = (spx_data["price"] - high_1y) / high_1y * 100
                snapshot["spx"]["drawdown"] = dd
                print(f"[資料] SPX 距52週高點: {dd:.1f}%")
    except Exception as e:
        print(f"[警告] 無法計算 SPX 跌幅: {e}")

    return snapshot


# ──────────────────────────────────────────────
# 市場全景分析
# ──────────────────────────────────────────────
def analyze_market_condition(
    vix: float | None, snapshot: dict
) -> tuple[str, list[str]]:
    """
    綜合 VIX、S&P500、HYG、DXY、黃金 五項指標，
    輸出市場狀態標籤與逐條分析訊息。
    風險分數 → 狀態：0=🟢正常 / 1-2=🟡輕度 / 3-4=🟠中度 / 5-6=🔴高度 / 7+=🆘極端
    """
    risk_score = 0
    signals: list[str] = []

    spx  = snapshot.get("spx")  or {}
    hyg  = snapshot.get("hyg")  or {}
    dxy  = snapshot.get("dxy")  or {}
    gold = snapshot.get("gold") or {}

    # ── VIX 恐慌指數 ──────────────────────────────
    if vix is not None:
        if vix >= 40:
            risk_score += 3
            signals.append("VIX ≥ 40：市場陷入極度恐慌，歷史上的最佳長期買點區間")
        elif vix >= 30:
            risk_score += 2
            signals.append("VIX ≥ 30：恐慌情緒顯著，市場波動劇烈")
        elif vix >= 25:
            risk_score += 1
            signals.append("VIX ≥ 25：市場進入警戒狀態，波動性上升")

    # ── S&P 500 位置與動能 ─────────────────────────
    spx_chg = spx.get("chg_pct")
    spx_dd  = spx.get("drawdown")

    if spx_chg is not None:
        if spx_chg <= -3.0:
            risk_score += 2
            signals.append(f"S&amp;P 500 單日重挫 {spx_chg:.1f}%，賣壓沉重")
        elif spx_chg <= -1.5:
            risk_score += 1
            signals.append(f"S&amp;P 500 下跌 {spx_chg:.1f}%，市場偏空")
        elif spx_chg >= 1.5:
            risk_score = max(0, risk_score - 1)
            signals.append(f"S&amp;P 500 反彈 +{spx_chg:.1f}%，恐慌有所消退")

    if spx_dd is not None:
        if spx_dd <= -20.0:
            risk_score += 2
            signals.append(f"S&amp;P 500 距高點 {spx_dd:.1f}%：已進入技術性熊市（熊市加碼區）")
        elif spx_dd <= -10.0:
            risk_score += 1
            signals.append(f"S&amp;P 500 距高點 {spx_dd:.1f}%：已進入修正區間（-10% ~ -20%）")
        elif spx_dd <= -5.0:
            signals.append(f"S&amp;P 500 距高點 {spx_dd:.1f}%：輕度回撤，可留意布局機會")

    # ── HYG 高收益債（信用市場壓力）──────────────────
    hyg_chg = hyg.get("chg_pct")
    if hyg_chg is not None:
        if hyg_chg <= -1.5:
            risk_score += 2
            signals.append("高收益債 HYG 大跌：信用市場出現系統性壓力，需警惕連鎖效應")
        elif hyg_chg <= -0.5:
            risk_score += 1
            signals.append("高收益債 HYG 走弱：企業融資成本上升，信用壓力初現")
        elif hyg_chg >= 0.5:
            signals.append("高收益債 HYG 走強：信用市場穩定，企業違約風險低")

    # ── DXY 美元指數（避險資金流向）──────────────────
    dxy_chg = dxy.get("chg_pct")
    if dxy_chg is not None:
        if dxy_chg >= 1.0:
            risk_score += 1
            signals.append(f"美元急升 +{dxy_chg:.1f}%：資金大量逃向美元避險（台幣貶值，換匯成本上升）")
        elif dxy_chg >= 0.5:
            signals.append(f"美元上漲 +{dxy_chg:.1f}%：輕度避險需求，注意換匯時機")
        elif dxy_chg <= -0.5:
            signals.append(f"美元走弱 {dxy_chg:.1f}%：風險偏好回升，台幣相對較強")

    # ── 黃金（終極避險資產）──────────────────────────
    gold_chg = gold.get("chg_pct")
    if gold_chg is not None:
        if gold_chg >= 1.5:
            risk_score += 1
            signals.append(f"黃金急漲 +{gold_chg:.1f}%：避險需求強烈，與 VIX 共振確認危機信號")
        elif gold_chg >= 0.5:
            signals.append(f"黃金上漲 +{gold_chg:.1f}%：避險情緒升溫")
        elif gold_chg <= -1.0:
            signals.append(f"黃金下跌 {gold_chg:.1f}%：避險需求減弱或美元強勢壓制")

    # ── 綜合評分 → 狀態標籤 ──────────────────────────
    risk_score = max(0, risk_score)
    if risk_score == 0:
        status = "🟢 市場平靜"
    elif risk_score <= 2:
        status = "🟡 輕度警戒"
    elif risk_score <= 4:
        status = "🟠 中度警戒"
    elif risk_score <= 6:
        status = "🔴 高度警戒"
    else:
        status = "🆘 極端恐慌"

    # ── 布局建議 ─────────────────────────────────────
    if vix is not None:
        if vix >= 40:
            signals.append("→ 歷史統計：此位階分批加碼 VOO，3–5 年後報酬率極佳")
        elif vix >= 30:
            signals.append("→ 已進入深度加碼區，建議按門檻分批布局")
        elif vix >= 25:
            signals.append("→ 已達 VOO 首批加碼門檻，可開始少量布局")
        else:
            signals.append("→ VIX 正常，暫無需特別操作，持續觀察")

    if not signals:
        signals.append("各項指標正常，市場無特殊訊號")

    return status, signals


# ──────────────────────────────────────────────
# 核心推播判斷邏輯
# ──────────────────────────────────────────────
def process_index(value, thresholds, state, prefix, flag, label) -> str | None:
    """根據當前值與歷史狀態決定是否推播。回傳推播文字，或 None（不推播）。"""
    active_key = f"{prefix}_active"
    daily_key  = f"{prefix}_daily_date"

    current_thr = None
    current_msg = None
    if value is not None:
        for thr, msg in thresholds:
            if value >= thr:
                current_thr = thr
                current_msg = msg
                break

    prev_thr = state.get(active_key)
    now_tw   = datetime.now(TW_TZ)
    today    = now_tw.date().isoformat()
    val_s    = f"{value:.2f}" if value is not None else "N/A"

    # ── 跌破所有門檻 ────────────────────────────────
    if current_thr is None:
        if prev_thr is not None:
            print(f"[{label}] {val_s} 跌破所有門檻，重置狀態，不推播")
            state[active_key] = None
            state[daily_key]  = ""
        else:
            print(f"[{label}] {val_s} 未超過任何門檻")
        return None

    # ── 往上突破新門檻 → 立即推播 ──────────────────
    if prev_thr is None or current_thr > prev_thr:
        print(f"[{label}] 突破新門檻 {current_thr}（前：{prev_thr}），立即推播")
        state[active_key] = current_thr
        state[daily_key]  = today
        return (
            f"{flag} <b>{label}：{val_s}</b>（突破門檻 {current_thr}）\n{current_msg}"
        )

    # ── 往下跌至較低門檻 → 不推播 ──────────────────
    if current_thr < prev_thr:
        print(f"[{label}] 從門檻 {prev_thr} 跌回門檻 {current_thr}，不推播")
        state[active_key] = current_thr
        return None

    # ── 維持同一門檻 → 每天 10:00 提醒一次 ─────────
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
    vix   = get_vix()

    alerts = []
    r = process_index(vix, VIX_THRESHOLDS, state, "vix", "🇺🇸", "美股 VIX")
    if r:
        alerts.append(r)

    if alerts:
        # 有警報時抓取市場快照並附上診斷
        print("[快照] 抓取市場全景資料...")
        snapshot = get_market_snapshot()
        status, signals = analyze_market_condition(vix, snapshot)

        spx  = snapshot.get("spx")  or {}
        gold = snapshot.get("gold") or {}
        dxy  = snapshot.get("dxy")  or {}
        hyg  = snapshot.get("hyg")  or {}

        def fmt(v, d=2):
            return f"{v:,.{d}f}" if v is not None else "─"

        def fmt_chg(c):
            if c is None:
                return ""
            return f" ({'+' if c >= 0 else ''}{c:.1f}%)"

        spx_dd = (
            f" | 距高點 {spx['drawdown']:.1f}%"
            if spx.get("drawdown") is not None else ""
        )

        snapshot_block = (
            f"\n\n━━━━ 市場快照 ━━━━\n"
            f"📈 S&amp;P 500：{fmt(spx.get('price'), 0)}"
            f"{fmt_chg(spx.get('chg_pct'))}{spx_dd}\n"
            f"💰 黃金：{fmt(gold.get('price'), 0)}"
            f"{fmt_chg(gold.get('chg_pct'))}\n"
            f"💵 美元指數：{fmt(dxy.get('price'), 1)}"
            f"{fmt_chg(dxy.get('chg_pct'))}\n"
            f"📉 高收益債 HYG：{fmt(hyg.get('price'), 2)}"
            f"{fmt_chg(hyg.get('chg_pct'))}\n\n"
            f"━━━━ 市場診斷 ━━━━\n"
            f"{status}\n"
            + "\n".join(f"  • {s}" for s in signals)
        )

        msg = (
            "📊 <b>VIX 恐慌指數警報！</b>\n\n"
            + "\n\n".join(alerts)
            + snapshot_block
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

    now_tw   = datetime.now(TW_TZ)
    vix      = get_vix()
    snapshot = get_market_snapshot()
    status, signals = analyze_market_condition(vix, snapshot)

    # ── 格式化輔助函數 ────────────────────────────
    def vix_level(v):
        if v is None: return "─"
        if v >= 45:   return "🆘 史詩恐慌"
        if v >= 40:   return "🚨 極度恐慌"
        if v >= 35:   return "🚨 高度恐慌"
        if v >= 30:   return "⚠️ 恐慌訊號"
        if v >= 20:   return "🟡 輕度波動"
        return "🟢 正常"

    def fmt(v, d=2):
        return f"{v:,.{d}f}" if v is not None else "─"

    def fmt_chg(c):
        if c is None: return ""
        return f" ({'+' if c >= 0 else ''}{c:.1f}%)"

    # ── 組裝資料 ─────────────────────────────────
    vix_s = f"{vix:.2f}" if vix is not None else "無法取得"

    spx  = snapshot.get("spx")  or {}
    hyg  = snapshot.get("hyg")  or {}
    dxy  = snapshot.get("dxy")  or {}
    gold = snapshot.get("gold") or {}
    tnx  = snapshot.get("tnx")  or {}

    spx_dd = (
        f" | 距高點 {spx['drawdown']:.1f}%"
        if spx.get("drawdown") is not None else ""
    )
    signals_text = (
        "\n".join(f"  • {s}" for s in signals)
        if signals else "  • 各項指標正常，無特殊訊號"
    )

    # ── 組裝訊息 ─────────────────────────────────
    msg = (
        f"📊 <b>VIX 每日晚報</b>\n"
        f"📅 {now_tw.strftime('%Y/%m/%d')} 晚安！\n\n"
        f"━━━━ 恐慌指標 ━━━━\n"
        f"🇺🇸 VIX：<b>{vix_s}</b> {vix_level(vix)}\n\n"
        f"━━━━ 市場概況 ━━━━\n"
        f"📈 S&amp;P 500：<b>{fmt(spx.get('price'), 0)}</b>"
        f"{fmt_chg(spx.get('chg_pct'))}{spx_dd}\n"
        f"💰 黃金：<b>{fmt(gold.get('price'), 0)}</b>"
        f"{fmt_chg(gold.get('chg_pct'))}\n"
        f"💵 美元指數：<b>{fmt(dxy.get('price'), 1)}</b>"
        f"{fmt_chg(dxy.get('chg_pct'))}\n"
        f"📉 高收益債 HYG：<b>{fmt(hyg.get('price'), 2)}</b>"
        f"{fmt_chg(hyg.get('chg_pct'))}\n"
        f"🏦 10年期殖利率：<b>{fmt(tnx.get('price'), 2)}%</b>"
        f"{fmt_chg(tnx.get('chg_pct'))}\n\n"
        f"━━━━ 市場診斷 ━━━━\n"
        f"{status}\n"
        f"{signals_text}\n\n"
        f"📌 <b>VOO 加碼門檻</b>：25／28／30／32／35／38／40／42／45\n"
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
        f" • 突破新門檻 → 立即推播（含市場全景）\n"
        f" • 維持同一門檻 → 每天 10:00 提醒\n"
        f" • 跌破門檻 → 不推播\n\n"
        f"🇺🇸 美股（VOO）門檻：25 / 28 / 30 / 32 / 35 / 38 / 40 / 42 / 45\n\n"
        f"📊 晚報內容：VIX + S&amp;P500 + 黃金 + 美元指數 + 高收益債 + 10年殖利率 + 市場診斷\n\n"
        f"⚠️ 本系統僅通知，不自動下單"
    )
    send_telegram(msg)


# ──────────────────────────────────────────────
# 入口
# ──────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="VIX 恐慌指數監控")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check",   action="store_true", help="執行一次 VIX 檢查")
    group.add_argument("--daily",   action="store_true", help="發送每日晚報")
    group.add_argument("--startup", action="store_true", help="發送啟動通知")
    args = parser.parse_args()

    if args.daily:
        send_daily_report()
    elif args.startup:
        send_startup_notification()
    else:
        check_and_alert()
