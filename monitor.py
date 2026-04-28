import os
import json
import math
import time
import requests
from pathlib import Path
from datetime import datetime, timezone


API_URL = "https://yields.llama.fi/pools"

STATE_DIR = Path(".cache")
STATE_FILE = STATE_DIR / "state.json"

TG_TOKEN = os.getenv("TG_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")

APY_ABS_CHANGE_ALERT = float(os.getenv("APY_ABS_CHANGE_ALERT", "0.30"))
APY_REL_CHANGE_ALERT = float(os.getenv("APY_REL_CHANGE_ALERT", "20"))

TVL_ABS_CHANGE_ALERT = float(os.getenv("TVL_ABS_CHANGE_ALERT", "3000000"))
TVL_REL_CHANGE_ALERT = float(os.getenv("TVL_REL_CHANGE_ALERT", "3"))

SEND_FIRST_RUN = os.getenv("SEND_FIRST_RUN", "true").lower() == "true"

MAX_MESSAGE_LEN = 3900

TARGET_POOLS = {
    "aa70268e-4b52-42bf-a116-608b370f9501": "Aave V3 USDC",
    "f981a304-bb6c-45b8-b0c5-fd2f515ad23a": "Aave V3 USDT",
    "7da72d09-56ca-4ec5-a45f-59114353e487": "Compound V3 USDC",
    "f4d5b566-e815-4ca2-bb07-7bcd8bc797f1": "Compound V3 USDT",
    "c5c74dd1-995c-4445-9d84-3e710bad7d52": "Spark Savings USDC",
    "a5d67f7e-5b51-4a9d-969d-caf051a7f5a4": "Spark Savings USDT",
    "65ce8276-b4d9-41ba-9f6f-21fc374cf9bc": "SparkLend USDC",
    "8fbe28b8-140d-4e37-8804-5d2aba4daded": "SparkLend USDT",
}


def now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def safe_float(value, default=0.0):
    try:
        if value is None:
            return default
        value = float(value)
        if math.isnan(value) or math.isinf(value):
            return default
        return value
    except Exception:
        return default


def pct_change(old, new):
    old = safe_float(old)
    new = safe_float(new)

    if old == 0:
        if new == 0:
            return 0.0
        return 999999.0

    return ((new - old) / old) * 100


def fmt_usd(value):
    value = safe_float(value)

    if abs(value) >= 1_000_000_000:
        return f"${value / 1_000_000_000:.2f}B"

    if abs(value) >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"

    if abs(value) >= 1_000:
        return f"${value / 1_000:.2f}K"

    return f"${value:.2f}"


def fmt_pct(value):
    return f"{safe_float(value):.2f}%"


def load_state():
    if not STATE_FILE.exists():
        return {}

    try:
        with STATE_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    with STATE_FILE.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False, sort_keys=True)


def fetch_pools():
    response = requests.get(API_URL, timeout=60)
    response.raise_for_status()

    data = response.json()

    if data.get("status") != "success":
        raise RuntimeError(f"API status is not success: {data.get('status')}")

    return data.get("data", [])


def send_telegram(text):
    if not TG_TOKEN or not TG_CHAT_ID:
        print("Telegram secrets are missing. Print message only.")
        print(text)
        return

    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"

    chunks = []
    current = ""

    for line in text.splitlines():
        if len(current) + len(line) + 1 > MAX_MESSAGE_LEN:
            chunks.append(current)
            current = line
        else:
            current = current + "\n" + line if current else line

    if current:
        chunks.append(current)

    for chunk in chunks:
        payload = {
            "chat_id": TG_CHAT_ID,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }

        response = requests.post(url, data=payload, timeout=30)
        response.raise_for_status()
        time.sleep(1)


def build_first_run_message(items):
    lines = []
    lines.append("🟢 <b>DeFi Alert 初始化完成</b>")
    lines.append("")
    lines.append(f"时间：{now_utc()}")
    lines.append(f"监控池子数量：{len(items)}")
    lines.append("")
    lines.append("当前数据：")

    for item in items:
        lines.append("")
        lines.append(f"• <b>{item['name']}</b>")
        lines.append(f"  APY：{fmt_pct(item['apy'])}")
        lines.append(f"  TVL：{fmt_usd(item['tvlUsd'])}")
        lines.append(f"  project：{item['project']}")
        lines.append(f"  symbol：{item['symbol']}")

    return "\n".join(lines)


def build_alert_message(alerts):
    lines = []
    lines.append("🚨 <b>DeFi 利率 / 流动性变化提醒</b>")
    lines.append("")
    lines.append(f"时间：{now_utc()}")
    lines.append("")

    for alert in alerts:
        lines.append(f"• <b>{alert['name']}</b>")

        if alert["apy_alert"]:
            lines.append(
                f"  APY：{fmt_pct(alert['old_apy'])} → {fmt_pct(alert['new_apy'])}"
            )
            lines.append(
                f"  APY变化：{alert['apy_diff']:+.2f} pct point / {alert['apy_rel']:+.2f}%"
            )

        if alert["tvl_alert"]:
            lines.append(
                f"  TVL：{fmt_usd(alert['old_tvl'])} → {fmt_usd(alert['new_tvl'])}"
            )
            lines.append(
                f"  TVL变化：{fmt_usd(alert['tvl_diff'])} / {alert['tvl_rel']:+.2f}%"
            )

        lines.append("")

    return "\n".join(lines).strip()


def main():
    print(f"Start monitor at {now_utc()}")

    pools = fetch_pools()
    pool_map = {str(p.get("pool")): p for p in pools}

    old_state = load_state()
    old_pools = old_state.get("pools", {})

    current_pools = {}
    current_items = []
    missing_pools = []
    alerts = []

    for pool_id, display_name in TARGET_POOLS.items():
        pool = pool_map.get(pool_id)

        if not pool:
            missing_pools.append((pool_id, display_name))
            continue

        apy = safe_float(pool.get("apy"))
        apy_base = safe_float(pool.get("apyBase"))
        apy_reward = safe_float(pool.get("apyReward"))
        tvl = safe_float(pool.get("tvlUsd"))

        item = {
            "name": display_name,
            "pool": pool_id,
            "project": pool.get("project"),
            "chain": pool.get("chain"),
            "symbol": pool.get("symbol"),
            "poolMeta": pool.get("poolMeta"),
            "apy": apy,
            "apyBase": apy_base,
            "apyReward": apy_reward,
            "tvlUsd": tvl,
            "updatedAt": now_utc(),
        }

        current_pools[pool_id] = item
        current_items.append(item)

        old = old_pools.get(pool_id)

        if not old:
            continue

        old_apy = safe_float(old.get("apy"))
        old_tvl = safe_float(old.get("tvlUsd"))

        apy_diff = apy - old_apy
        apy_rel = pct_change(old_apy, apy)

        tvl_diff = tvl - old_tvl
        tvl_rel = pct_change(old_tvl, tvl)

        apy_alert = abs(apy_diff) >= APY_ABS_CHANGE_ALERT or abs(apy_rel) >= APY_REL_CHANGE_ALERT
        tvl_alert = abs(tvl_diff) >= TVL_ABS_CHANGE_ALERT or abs(tvl_rel) >= TVL_REL_CHANGE_ALERT

        if apy_alert or tvl_alert:
            alerts.append({
                "name": display_name,
                "apy_alert": apy_alert,
                "tvl_alert": tvl_alert,
                "old_apy": old_apy,
                "new_apy": apy,
                "apy_diff": apy_diff,
                "apy_rel": apy_rel,
                "old_tvl": old_tvl,
                "new_tvl": tvl,
                "tvl_diff": tvl_diff,
                "tvl_rel": tvl_rel,
            })

    new_state = {
        "updatedAt": now_utc(),
        "pools": current_pools,
        "missingPools": missing_pools,
    }

    save_state(new_state)

    print(f"Target pools: {len(TARGET_POOLS)}")
    print(f"Found pools: {len(current_items)}")
    print(f"Missing pools: {len(missing_pools)}")
    print(f"Alerts: {len(alerts)}")

    for item in current_items:
        print(
            item["name"],
            "APY:",
            item["apy"],
            "TVL:",
            item["tvlUsd"],
            "project:",
            item["project"],
            "symbol:",
            item["symbol"],
        )

    if missing_pools:
        print("Missing target pools:")
        for pool_id, display_name in missing_pools:
            print(display_name, pool_id)

    if not old_pools:
        print("No previous state found.")

        if SEND_FIRST_RUN:
            message = build_first_run_message(current_items)
            send_telegram(message)

        return

    if alerts:
        message = build_alert_message(alerts)
        send_telegram(message)
    else:
        print("No alert.")


if __name__ == "__main__":
    main()
