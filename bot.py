#!/usr/bin/env python3
import os, sys, asyncio, json, logging, subprocess
from datetime import datetime, timedelta
from collections import deque

import websockets
from telegram import Bot
from telegram.constants import ParseMode
from dotenv import load_dotenv

# ===================== WINDOWS FIX =====================
if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# ===================== ENV =====================
load_dotenv()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

if not BOT_TOKEN or not CHAT_ID:
    raise SystemExit("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")

# ===================== LOG =====================
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")
logger = logging.getLogger("ProfitHub")

# ===================== JSON + GIT =====================
SIGNAL_JSON_PATH = "signals.json"

def git_push_if_changed():
    try:
        status = subprocess.check_output(
            ["git", "status", "--porcelain", SIGNAL_JSON_PATH]
        ).decode().strip()
        if not status:
            return
        subprocess.run(["git", "add", SIGNAL_JSON_PATH], check=True)
        subprocess.run(["git", "commit", "-m", "auto update signal"], check=False)
        subprocess.run(["git", "push", "origin", "main"], check=True)
        logger.info("signals.json pushed to GitHub")
    except Exception as e:
        logger.error(f"Git sync failed: {e}")

def update_signal_json(**k):
    data = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "bot_running": True,
        "current": {
            "bot_name": k.get("current_bot", "-"),
            "market": k.get("market", "-"),
            "signal_type": k.get("signal_type", "-"),
            "status": k.get("status", "IDLE"),
            "expires_at": k.get("expires_at", "-"),
        },
        "next": {
            "bot_name": k.get("next_bot", "-"),
            "time": k.get("next_time", "-"),
        },
    }
    with open(SIGNAL_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    git_push_if_changed()

# ===================== MARKETS =====================
MARKETS = [
    ('R_10', 'Volatility 10 Index'),
    ('1HZ10V', 'Volatility 10 (1s) Index'),
    ('R_25', 'Volatility 25 Index'),
    ('1HZ25V', 'Volatility 25 (1s) Index'),
]

BOT_SEQUENCE = ['V1', 'V2', 'V4', 'V5']
BOT_IMAGES = {b: f"images/{b}.png" for b in BOT_SEQUENCE}

bot_index = 0
sent_messages = []

tick_data = {s: deque(maxlen=120) for s, _ in MARKETS}
price_history = {s: deque(maxlen=120) for s, _ in MARKETS}

# ===================== UTILS =====================
def last_digit(price):
    return int(str(price).split('.')[-1][-1])

def digit_stats(symbol):
    ticks = tick_data[symbol]
    if len(ticks) < 60:
        return None
    counts = [ticks.count(i) for i in range(10)]
    total = len(ticks)
    return {
        "freq": sorted(range(10), key=lambda x: counts[x], reverse=True),
        "under6": sum(counts[:6]) / total * 100,
        "under7": sum(counts[:7]) / total * 100,
        "even": sum(counts[i] for i in [0,2,4,6,8]) / total * 100,
        "odd": sum(counts[i] for i in [1,3,5,7,9]) / total * 100,
    }

def calc_rsi(prices, period=14):
    if len(prices) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, period + 1):
        diff = prices[-i] - prices[-i-1]
        gains.append(max(diff, 0))
        losses.append(abs(min(diff, 0)))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period or 0.0001
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)

def calc_macd(prices):
    if len(prices) < 35:
        return None
    def ema(data, p):
        k = 2 / (p + 1)
        e = data[0]
        for v in data[1:]:
            e = v * k + e * (1 - k)
        return e
    macd = ema(prices[-26:], 12) - ema(prices[-26:], 26)
    signal = ema(prices[-9:], 9)
    return round(macd - signal, 5)

# ===================== SIGNAL LOGIC =====================
def signal_v1(sym, name):
    s = digit_stats(sym)
    if s and s["under6"] >= 60:
        return {
            "trade": "UNDER 6",
            "entry": s["freq"][0],
            "prob": s["under6"]
        }, name

def signal_v2(sym, name):
    s = digit_stats(sym)
    if s and s["under7"] >= 60:
        return {
            "trade": "UNDER 7",
            "entry": s["freq"][0],
            "prob": s["under7"]
        }, name

def signal_v4(sym, name):
    prices = list(price_history[sym])
    rsi = calc_rsi(prices)
    macd = calc_macd(prices)
    if rsi and macd and rsi > 60 and macd > 0:
        return {
            "trade": "RISE",
            "rsi": rsi,
            "macd": macd,
            "momentum": round(rsi, 1)
        }, name

def signal_v5(sym, name):
    s = digit_stats(sym)
    if s and s["even"] >= 60:
        return {
            "trade": "EVEN",
            "prob": s["even"]
        }, name

# ===================== TELEGRAM =====================
async def cleanup(bot):
    while sent_messages:
        try:
            await bot.delete_message(CHAT_ID, sent_messages.pop())
        except:
            pass

async def send(bot, text):
    m = await bot.send_message(CHAT_ID, text, parse_mode=ParseMode.HTML)
    sent_messages.append(m.message_id)

async def send_with_image(bot, bot_type, caption):
    img = BOT_IMAGES.get(bot_type)
    if img and os.path.exists(img):
        with open(img, "rb") as f:
            m = await bot.send_photo(CHAT_ID, f, caption=caption, parse_mode=ParseMode.HTML)
            sent_messages.append(m.message_id)
    else:
        await send(bot, caption)

# ===================== TICKS =====================
async def collect_ticks():
    uri = "wss://ws.derivws.com/websockets/v3?app_id=1089"
    while True:
        try:
            async with websockets.connect(uri) as ws:
                for s,_ in MARKETS:
                    await ws.send(json.dumps({"ticks": s, "subscribe": 1}))
                async for msg in ws:
                    d = json.loads(msg)
                    if "tick" in d:
                        p = float(d["tick"]["quote"])
                        s = d["tick"]["symbol"]
                        tick_data[s].append(last_digit(p))
                        price_history[s].append(p)
        except:
            await asyncio.sleep(5)

# ===================== MAIN =====================
async def main():
    global bot_index
    bot = Bot(BOT_TOKEN)
    asyncio.create_task(collect_ticks())

    while True:
        await cleanup(bot)

        current_bot = BOT_SEQUENCE[bot_index]
        next_bot = BOT_SEQUENCE[(bot_index+1)%len(BOT_SEQUENCE)]
        bot_index = (bot_index+1)%len(BOT_SEQUENCE)

        await send(bot, f"⚠️ <b>Be Prepared!</b>\nLoad <b>ProfitHub – Signal Bot {current_bot}</b>\nSignal incoming in <b>2 minutes</b>...")
        await asyncio.sleep(120)

        fn = {"V1":signal_v1,"V2":signal_v2,"V4":signal_v4,"V5":signal_v5}[current_bot]
        result = next((fn(s,n) for s,n in MARKETS if fn(s,n)), None)

        now = datetime.now()
        nxt = (now + timedelta(minutes=10)).strftime("%H:%M")

        if not result:
            update_signal_json(current_bot=current_bot, status="NO_SIGNAL", next_bot=next_bot, next_time=nxt)
            await send(bot, f"ℹ️ <b>No valid signal for ProfitHub – Signal Bot {current_bot}</b>\n⏭ Next check at <b>{nxt}</b>")
            await asyncio.sleep(600)
            continue

        data, market = result
        exp = (now + timedelta(minutes=5)).strftime("%H:%M")

        msg = f"""<b>ProfitHub – Signal Bot {current_bot}</b>

📊 <b>Market:</b> {market}
🎯 <b>Trade:</b> {data['trade']}"""

        if "entry" in data:
            msg += f"\n🎯 <b>Entry Digit:</b> {data['entry']}"
        if "prob" in data:
            msg += f"\n📊 <b>Probability:</b> {data['prob']:.1f}%"
        if current_bot == "V4":
            msg += f"""

📊 <b>Confirmations:</b>
• RSI (14): {data['rsi']} ✅
• MACD Histogram: +{data['macd']} ✅
• Momentum Strength: {data['momentum']}%"""

        msg += f"\n\n⏳ <b>Validity:</b> 5 minutes\n⏱ <b>Expires at:</b> {exp}"

        update_signal_json(current_bot=current_bot, market=market, signal_type=data["trade"], status="ACTIVE", expires_at=exp, next_bot=next_bot, next_time=nxt)
        await send_with_image(bot, current_bot, msg)

        await asyncio.sleep(300)

        update_signal_json(current_bot=current_bot, market=market, signal_type=data["trade"], status="EXPIRED", expires_at=exp, next_bot=next_bot, next_time=nxt)
        await send(bot, f"❌ <b>Above signal is no longer valid</b>\n\n⏭ Next signal at <b>{nxt}</b>")
        await asyncio.sleep(300)

if __name__ == "__main__":
    asyncio.run(main())
