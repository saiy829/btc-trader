"""
BTC 实时数据面板 · FastAPI 后端  v5.0
三大盘口 IB / VP / MP / ETF / 清算 实时数据
新增：历史数据存储层（SQLite）
  - 5分钟快照：价格/成交量/OI/Funding/CB溢价
  - 每日存档：IB/VP/MP/ETF/清算统计
"""
import asyncio, importlib, json, logging, sqlite3, sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta, date
from typing import Set, Optional

import aiohttp, websockets
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware

SGT = timezone(timedelta(hours=8))
logging.basicConfig(level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("btc-api")

DB_PATH = "/opt/btc-trader/data/dashboard_history.db"

S: dict = {
    "price": 0.0, "change_24h": 0.0, "volume_24h": 0.0,
    "funding": {},
    "oi_btc": 0.0, "oi_changes": {},
    "cb_premium": 0.0, "cb_pct": 0.0,
    "liq": [],
    "okx_liq_connected": False, "okx_liq_last_event_ts": 0.0,
    "gate_liq_connected": False, "gate_liq_last_poll_ts": 0.0, "gate_liq_last_id": 0,
    "ib_asia":   {}, "ib_europe": {}, "ib_us": {},
    "vp": {}, "mp": {}, "etf": {},
    "ts": "--:--:--",
    # 当日清算运行统计（用于每日归档，遇日界自动结转）
    "liq_daily_date": "", "liq_daily_count": 0, "liq_daily_max": 0.0,
    "liq_daily_long_total": 0.0, "liq_daily_short_total": 0.0,
    "liq_yesterday": {},
    # CVD（累积成交量差值）：每UTC自然日重置，与IB/VP/MP日界统一
    "cvd_today": 0.0, "cvd_date": "", "cvd_changes": {}, "last_agg_trade_id": None,
}
_oi_hist: list = []
_cvd_hist: list = []
_clients: Set[WebSocket] = set()

IB_SESSIONS = {
    "ib_europe": {"name": "欧盘", "start_h": 8,  "end_h": 9,  "sgt": "16:00–17:00"},
    "ib_us":     {"name": "美盘", "start_h": 13, "end_h": 14, "sgt": "21:00–22:00"},
}

if "/opt/btc-trader" not in sys.path:
    sys.path.insert(0, "/opt/btc-trader")


# ══════════════════════════════════════════════════════════════════
#  历史存储：SQLite 初始化 + 读写函数
# ══════════════════════════════════════════════════════════════════
def db_init() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS snapshots (
            ts INTEGER PRIMARY KEY,
            price REAL, volume_24h REAL, oi_btc REAL,
            funding_binance REAL, funding_avg REAL, cb_premium REAL,
            cvd REAL
        )
    """)
    # 兼容旧库：若表已存在但缺 cvd 列，补加（不影响已有数据）
    try:
        conn.execute("ALTER TABLE snapshots ADD COLUMN cvd REAL")
    except Exception:
        pass  # 列已存在
    conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_archive (
            date TEXT PRIMARY KEY,
            ib_asia_high REAL, ib_asia_low REAL, ib_asia_range REAL, ib_asia_type TEXT,
            ib_europe_high REAL, ib_europe_low REAL,
            ib_us_high REAL, ib_us_low REAL,
            vp_poc REAL, vp_vah REAL, vp_val REAL, vp_shape TEXT,
            mp_high REAL, mp_low REAL, mp_close REAL,
            etf_flow REAL, etf_source TEXT,
            liq_count INTEGER, liq_max REAL, liq_long_total REAL, liq_short_total REAL
        )
    """)
    conn.commit()
    conn.close()
    log.info(f"SQLite 历史库已初始化: {DB_PATH}")


def db_insert_snapshot() -> None:
    try:
        avg_fr = (sum(S["funding"].values()) / len(S["funding"])) if S["funding"] else None
        conn = sqlite3.connect(DB_PATH, timeout=5)
        conn.execute(
            "INSERT OR REPLACE INTO snapshots "
            "(ts, price, volume_24h, oi_btc, funding_binance, funding_avg, cb_premium, cvd) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (int(datetime.now(SGT).timestamp()), S["price"], S["volume_24h"], S["oi_btc"],
             S["funding"].get("Binance"), avg_fr, S["cb_premium"], S["cvd_today"])
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning(f"db_insert_snapshot error: {e}")


def db_insert_daily_archive(date_str: str, etf_unavailable: bool = False) -> bool:
    """归档指定日期（若已存在则跳过）。返回是否新插入。
    etf_unavailable=True 时 ETF 字段显式存 NULL（ETF 数据游标已经跑过这一天、
    拿不到准确值了），好过存一个属于别的日期的错误数字。"""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        existing = conn.execute("SELECT 1 FROM daily_archive WHERE date=?", (date_str,)).fetchone()
        if existing:
            conn.close()
            return False

        ib_a, ib_e, ib_u = S["ib_asia"], S["ib_europe"], S["ib_us"]
        vp, mp, etf = S["vp"], S["mp"], S["etf"]
        liq_y = S["liq_yesterday"]

        etf_flow   = None if etf_unavailable else etf.get("total_yest")
        etf_source = None if etf_unavailable else etf.get("source")

        conn.execute(
            "INSERT INTO daily_archive (date, ib_asia_high, ib_asia_low, ib_asia_range, ib_asia_type, "
            "ib_europe_high, ib_europe_low, ib_us_high, ib_us_low, "
            "vp_poc, vp_vah, vp_val, vp_shape, mp_high, mp_low, mp_close, "
            "etf_flow, etf_source, liq_count, liq_max, liq_long_total, liq_short_total) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (date_str,
             ib_a.get("ib_high"), ib_a.get("ib_low"), ib_a.get("ib_range"), ib_a.get("opening_type"),
             ib_e.get("ib_high"), ib_e.get("ib_low"),
             ib_u.get("ib_high"), ib_u.get("ib_low"),
             vp.get("poc"), vp.get("vah"), vp.get("val"), vp.get("profile_shape"),
             mp.get("high"), mp.get("low"), mp.get("close"),
             etf_flow, etf_source,
             liq_y.get("count"), liq_y.get("max"), liq_y.get("long_total"), liq_y.get("short_total"))
        )
        conn.commit()
        conn.close()
        log.info(f"每日归档已写入: {date_str}" + ("（ETF数据不可用，留空）" if etf_unavailable else ""))
        return True
    except Exception as e:
        log.warning(f"db_insert_daily_archive error: {e}")
        return False


def db_query_metrics(metric: str, period: str) -> list:
    """查询指定指标的历史序列。period: 1d/7d/30d/90d/all"""
    col_map = {
        "price": "price", "volume_24h": "volume_24h", "oi_btc": "oi_btc",
        "funding_binance": "funding_binance", "funding_avg": "funding_avg",
        "cb_premium": "cb_premium", "cvd": "cvd",
    }
    col = col_map.get(metric, "price")
    days_map = {"1d": 1, "7d": 7, "30d": 30, "90d": 90, "all": 36500}
    days = days_map.get(period, 7)
    cutoff = int((datetime.now(SGT) - timedelta(days=days)).timestamp())

    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        rows = conn.execute(
            f"SELECT ts, {col} FROM snapshots WHERE ts >= ? AND {col} IS NOT NULL ORDER BY ts ASC",
            (cutoff,)
        ).fetchall()
        conn.close()
        return [{"ts": r[0], "value": r[1]} for r in rows]
    except Exception as e:
        log.warning(f"db_query_metrics error: {e}")
        return []


def db_query_daily_archive(days: int = 30) -> list:
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM daily_archive ORDER BY date DESC LIMIT ?", (days,)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        log.warning(f"db_query_daily_archive error: {e}")
        return []


def _check_liq_day_rollover() -> None:
    """检测SGT日界变化，结转当日清算统计到 liq_yesterday，供归档使用"""
    today_str = datetime.now(SGT).strftime("%Y-%m-%d")
    if S["liq_daily_date"] != today_str:
        if S["liq_daily_date"]:
            S["liq_yesterday"] = {
                "date": S["liq_daily_date"], "count": S["liq_daily_count"],
                "max": S["liq_daily_max"], "long_total": S["liq_daily_long_total"],
                "short_total": S["liq_daily_short_total"],
            }
        S["liq_daily_date"] = today_str
        S["liq_daily_count"] = 0
        S["liq_daily_max"] = 0.0
        S["liq_daily_long_total"] = 0.0
        S["liq_daily_short_total"] = 0.0


def calc_oi_changes() -> dict:
    if len(_oi_hist) < 2: return {}
    now_ts = datetime.now(SGT).timestamp()
    cur = S["oi_btc"]
    TFS = [("5m",300),("15m",900),("30m",1800),("1H",3600),("4H",14400),("12H",43200),("24H",86400)]
    result = {}
    for label, secs in TFS:
        ref_ts = now_ts - secs
        past = [(t, v) for t, v in _oi_hist if t <= ref_ts + 30]
        if not past: continue
        _, ref = min(past, key=lambda x: abs(x[0] - ref_ts))
        if ref > 0:
            result[label] = {"pct": round((cur-ref)/ref*100, 3), "val": round(cur-ref, 0)}
    return result


def calc_cvd_changes() -> dict:
    """CVD多时间段变化，与calc_oi_changes()逻辑完全一致"""
    if len(_cvd_hist) < 2: return {}
    now_ts = datetime.now(SGT).timestamp()
    cur = S["cvd_today"]
    TFS = [("5m",300),("15m",900),("30m",1800),("1H",3600),("4H",14400)]
    result = {}
    for label, secs in TFS:
        ref_ts = now_ts - secs
        past = [(t, v) for t, v in _cvd_hist if t <= ref_ts + 30]
        if not past: continue
        _, ref = min(past, key=lambda x: abs(x[0] - ref_ts))
        result[label] = round(cur - ref, 1)
    return result


def snap() -> dict:
    return {
        "price": S["price"], "change_24h": S["change_24h"],
        "volume_24h": S["volume_24h"], "funding": S["funding"],
        "oi_btc": S["oi_btc"], "oi_changes": S["oi_changes"],
        "cb_premium": S["cb_premium"], "cb_pct": S["cb_pct"],
        "liq_feed": S["liq"][:30],
        "ib_asia": S["ib_asia"], "ib_europe": S["ib_europe"], "ib_us": S["ib_us"],
        "vp": S["vp"], "mp": S["mp"], "etf": S["etf"],
        "cvd_today": round(S["cvd_today"], 1), "cvd_changes": S["cvd_changes"],
        "liq_today": {
            "count": S["liq_daily_count"], "max": S["liq_daily_max"],
            "long_total": S["liq_daily_long_total"], "short_total": S["liq_daily_short_total"],
        },
        "last_update": S["ts"],
    }


async def bcast(msg: dict) -> None:
    dead: Set[WebSocket] = set()
    for ws in _clients.copy():
        try: await ws.send_json(msg)
        except Exception: dead.add(ws)
    _clients.difference_update(dead)


TIMEOUT = aiohttp.ClientTimeout(total=8)
SHORT   = aiohttp.ClientTimeout(total=5)


async def _fetch_core() -> None:
    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as sess:
            async with sess.get(
                "https://fapi.binance.com/fapi/v1/ticker/24hr?symbol=BTCUSDT") as r:
                d = await r.json()
                S["price"]      = float(d["lastPrice"])
                S["change_24h"] = float(d["priceChangePercent"])
                S["volume_24h"] = float(d["quoteVolume"])

            async with sess.get(
                "https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT") as r:
                d = await r.json()
                if d and "lastFundingRate" in d:
                    S["funding"]["Binance"] = round(float(d["lastFundingRate"]) * 100, 4)

            async with sess.get(
                "https://fapi.binance.com/fapi/v1/openInterest?symbol=BTCUSDT") as r:
                d = await r.json()
                oi  = float(d["openInterest"])
                now = datetime.now(SGT).timestamp()
                _oi_hist.append((now, oi))
                _oi_hist[:] = [(t, v) for t, v in _oi_hist if t >= now - 90000]
                S["oi_btc"]     = oi
                S["oi_changes"] = calc_oi_changes()

            spot_prices = []
            try:
                async with sess.get(
                    "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT", timeout=SHORT) as r:
                    spot_prices.append(float((await r.json())["price"]))
            except Exception: pass
            try:
                async with sess.get(
                    "https://www.okx.com/api/v5/market/ticker?instId=BTC-USDT", timeout=SHORT) as r:
                    d = await r.json()
                    if d.get("code")=="0" and d.get("data"):
                        spot_prices.append(float(d["data"][0]["last"]))
            except Exception: pass
            try:
                async with sess.get(
                    "https://api.bybit.com/v5/market/tickers?category=spot&symbol=BTCUSDT", timeout=SHORT) as r:
                    d = await r.json()
                    if d.get("retCode")==0 and d["result"]["list"]:
                        spot_prices.append(float(d["result"]["list"][0]["lastPrice"]))
            except Exception: pass
            coinbase_price = 0.0
            try:
                async with sess.get(
                    "https://api.exchange.coinbase.com/products/BTC-USD/ticker", timeout=SHORT) as r:
                    coinbase_price = float((await r.json()).get("price", 0))
            except Exception: pass
            if coinbase_price and spot_prices:
                avg = sum(spot_prices) / len(spot_prices)
                S["cb_premium"] = round(coinbase_price - avg, 2)
                S["cb_pct"]     = round((coinbase_price - avg) / avg * 100, 4)

        S["ts"] = datetime.now(SGT).strftime("%H:%M:%S")
        _check_liq_day_rollover()
    except Exception as e:
        log.warning(f"core: {e}")


async def task_core() -> None:
    while True:
        await _fetch_core()
        if _clients: await bcast({"type": "snapshot", "data": snap()})
        await asyncio.sleep(5)


async def task_snapshot_recorder() -> None:
    """
    每5分钟把核心指标写入历史快照表，对齐到整点边界(:00/:05/:10/:15...)
    v2修复：原逻辑固定sleep(300)，起始点取决于服务启动时刻，导致时间戳
    偏移（如15:53/15:58/16:03），与人类习惯的整点刻度不一致。
    改为每轮动态计算"距下一个5分钟整点还有多久"，自动对齐时钟，
    不受服务重启时刻影响。
    """
    while True:
        now = datetime.now(SGT)
        seconds_since_hour = now.minute * 60 + now.second
        next_boundary = ((seconds_since_hour // 300) + 1) * 300
        wait_seconds = next_boundary - seconds_since_hour
        await asyncio.sleep(wait_seconds)
        db_insert_snapshot()
        log.info(f"快照已记录(整点对齐 {datetime.now(SGT).strftime('%H:%M')}): "
                 f"price={S['price']:.1f} OI={S['oi_btc']:.0f} vol={S['volume_24h']:.0f}")


async def task_archive_daily() -> None:
    """每小时检查并归档昨日数据（VP/MP的date字段代表昨日，凡未归档则补上）。
    ETF数据要到北京时间04:00-12:00更新窗口结束才算"已稳定"（is_settling=False），
    如果归档这一刻ETF还没轮到target_date、或者还在更新窗口内，本轮先不归档，
    等下一小时再查——避免把还没对上号/属于别的日期的ETF数值錯误地存进当天归档
    （这正是之前每日存档表里连续两天ETF数值一模一样的根本原因）。"""
    while True:
        await asyncio.sleep(3600)
        target_date = S["vp"].get("date") or S["liq_yesterday"].get("date")
        if not target_date:
            continue

        etf = S["etf"]
        etf_date = etf.get("date")
        if etf_date == target_date and not etf.get("is_settling", False):
            db_insert_daily_archive(target_date)
        elif etf_date and etf_date > target_date:
            # ETF游标已经跑过了这一天（比如中间掉线太久没追上），拿不到准确值了，
            # 归档时ETF字段留空，好过存一个属于别的日期的错误数字
            log.warning(f"归档 {target_date} 时ETF数据已跑过（当前ETF日期={etf_date}），ETF字段留空")
            db_insert_daily_archive(target_date, etf_unavailable=True)
        else:
            log.info(f"归档暂缓：{target_date} 的ETF数据尚未就绪/未稳定（当前ETF日期={etf_date}），下小时重试")


async def task_funding() -> None:
    """OKX/Bybit/Bitget/Gate funding，复用 multi_funding.py 已验证实现"""
    while True:
        result = await asyncio.get_event_loop().run_in_executor(None, _load_funding_others_sync)
        if result:
            S["funding"].update(result)
            log.info(f"Funding 采集完成(OKX/Bybit/Bitget/Gate): {list(result.keys())}")
        else:
            log.warning("Funding(others): 返回空数据")
        await asyncio.sleep(30)


def _load_funding_others_sync() -> dict:
    try:
        mod_name = "data_collector.multi_funding"
        if mod_name in sys.modules:
            mod = importlib.reload(sys.modules[mod_name])
        else:
            import data_collector.multi_funding as mod
        result = {}
        for fn, key in [(mod._okx, "OKX"), (mod._bybit, "Bybit"),
                         (mod._bitget, "Bitget"), (mod._gate, "Gate")]:
            try:
                r = fn()
                if r:
                    result[key] = round(r["rate"], 4)
            except Exception as e:
                log.warning(f"Funding {key} 错误: {e}")
        return result
    except Exception as e:
        log.warning(f"Funding(others) load error: {e}")
        return {}


def _load_ib_sync() -> dict:
    try:
        mod_name = "data_collector.binance_data"
        if mod_name in sys.modules:
            mod = importlib.reload(sys.modules[mod_name])
        else:
            import data_collector.binance_data as mod
        result = mod.get_todays_ib()
        return result or {}
    except Exception as e:
        log.warning(f"IB (Asia) load error: {e}")
        return {}


async def task_ib_asia() -> None:
    while True:
        result = await asyncio.get_event_loop().run_in_executor(None, _load_ib_sync)
        if result:
            S["ib_asia"] = result
            log.info(f"亚盘IB  H={result.get('ib_high')}  L={result.get('ib_low')}")
        else:
            log.warning("亚盘IB: 返回空数据")
        await asyncio.sleep(900)


async def fetch_kline_ib(sess_key: str) -> dict:
    cfg     = IB_SESSIONS[sess_key]
    now_utc = datetime.now(timezone.utc)
    today   = now_utc.date()

    start_utc = datetime(today.year, today.month, today.day,
                         cfg["start_h"], 0, 0, tzinfo=timezone.utc)
    end_utc   = datetime(today.year, today.month, today.day,
                         cfg["end_h"],   0, 0, tzinfo=timezone.utc)

    if now_utc < start_utc:
        return {"name": cfg["name"], "sgt": cfg["sgt"], "status": "pending"}

    actual_end = min(now_utc, end_utc)
    is_forming = now_utc < end_utc

    start_ms = int(start_utc.timestamp()  * 1000)
    end_ms   = int(actual_end.timestamp() * 1000)

    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as sess:
            async with sess.get(
                "https://fapi.binance.com/fapi/v1/klines",
                params={"symbol": "BTCUSDT", "interval": "5m",
                        "startTime": start_ms, "endTime": end_ms, "limit": 24}
            ) as r:
                klines = await r.json()

        if not klines or not isinstance(klines, list):
            return {"name": cfg["name"], "sgt": cfg["sgt"], "status": "no_data"}

        highs    = [float(k[2]) for k in klines]
        lows     = [float(k[3]) for k in klines]
        ib_high  = max(highs)
        ib_low   = min(lows)

        return {
            "name":     cfg["name"],
            "sgt":      cfg["sgt"],
            "status":   "forming" if is_forming else "complete",
            "ib_high":  round(ib_high, 1),
            "ib_low":   round(ib_low, 1),
            "ib_mid":   round((ib_high + ib_low) / 2, 1),
            "ib_range": round(ib_high - ib_low, 1),
            "ib_pct":   round((ib_high - ib_low) / ib_low * 100, 3) if ib_low else 0,
        }
    except Exception as e:
        log.warning(f"{cfg['name']} IB 错误: {e}")
        return {"name": cfg["name"], "sgt": cfg["sgt"], "status": "error"}


async def task_ib_sessions() -> None:
    while True:
        S["ib_europe"] = await fetch_kline_ib("ib_europe")
        S["ib_us"]     = await fetch_kline_ib("ib_us")
        log.info(f"欧盘IB {S['ib_europe'].get('status')}  美盘IB {S['ib_us'].get('status')}")
        await asyncio.sleep(900)


def _load_vp_sync() -> dict:
    try:
        mod_name = "data_collector.binance_data"
        mod = sys.modules.get(mod_name) or importlib.import_module(mod_name)
        return mod.get_yesterday_volume_profile() or {}
    except Exception as e:
        log.warning(f"VP load error: {e}")
        return {}


async def task_vp() -> None:
    while True:
        result = await asyncio.get_event_loop().run_in_executor(None, _load_vp_sync)
        if result:
            S["vp"] = result
            log.info(f"VP  POC=${result.get('poc')}  VA=${result.get('val')}-${result.get('vah')}")
        else:
            log.warning("VP: 返回空数据")
        await asyncio.sleep(900)


def _load_mp_sync() -> dict:
    try:
        mod_name = "data_collector.binance_data"
        mod = sys.modules.get(mod_name) or importlib.import_module(mod_name)
        y = mod.get_yesterday_ohlcv()
        if not y:
            return {}
        return {"has_data": True, "high": y.get("high",0), "low": y.get("low",0),
                "close": y.get("close",0), "open": y.get("open",0)}
    except Exception as e:
        log.warning(f"MP load error: {e}")
        return {}


async def task_mp() -> None:
    while True:
        result = await asyncio.get_event_loop().run_in_executor(None, _load_mp_sync)
        if result:
            S["mp"] = result
            log.info(f"MP  PDH=${result.get('high')}  PDL=${result.get('low')}  PDC=${result.get('close')}")
        else:
            log.warning("MP: 返回空数据")
        await asyncio.sleep(900)


def _load_etf_sync() -> dict:
    try:
        mod_name = "data_collector.etf_data"
        mod = sys.modules.get(mod_name) or importlib.import_module(mod_name)
        return mod.fetch_etf_flows() or {}
    except Exception as e:
        log.warning(f"ETF load error: {e}")
        return {}


async def task_etf() -> None:
    while True:
        result = await asyncio.get_event_loop().run_in_executor(None, _load_etf_sync)
        if result:
            S["etf"] = result
            log.info(f"ETF  来源={result.get('source')}  最新={result.get('yest_str')}")
        else:
            log.warning("ETF: 返回空数据")
        await asyncio.sleep(1800)


async def task_cvd() -> None:
    """
    CVD（累积成交量差值）：轮询Binance aggTrades增量计算
    用REST而非WebSocket，规避fstream.binance.com的德国IP封锁。
    每UTC自然日（北京时间08:00）重置归零，与IB/VP/MP的日界口径统一。
    """
    while True:
        try:
            today_str = datetime.now(SGT).strftime("%Y-%m-%d")
            if S["cvd_date"] != today_str:
                S["cvd_today"] = 0.0
                S["cvd_date"] = today_str
                S["last_agg_trade_id"] = None
                _cvd_hist.clear()
                log.info(f"CVD 日界重置: {today_str}")

            async with aiohttp.ClientSession(timeout=TIMEOUT) as sess:
                params = {"symbol": "BTCUSDT", "limit": 1000}
                if S["last_agg_trade_id"] is not None:
                    params["fromId"] = S["last_agg_trade_id"] + 1
                async with sess.get(
                    "https://fapi.binance.com/fapi/v1/aggTrades", params=params) as r:
                    trades = await r.json()

            if trades and isinstance(trades, list):
                if S["last_agg_trade_id"] is None:
                    # 首次运行：仅建立游标起点，不计入该批次（避免把过去未知时段的成交算进今日CVD）
                    S["last_agg_trade_id"] = trades[-1]["a"]
                else:
                    delta = 0.0
                    for t in trades:
                        qty = float(t["q"])
                        delta += -qty if t["m"] else qty  # m=True:卖方主动 / m=False:买方主动
                    S["cvd_today"] += delta
                    S["last_agg_trade_id"] = trades[-1]["a"]

                    now = datetime.now(SGT).timestamp()
                    _cvd_hist.append((now, S["cvd_today"]))
                    _cvd_hist[:] = [(t,v) for t,v in _cvd_hist if t >= now - 18000]
                    S["cvd_changes"] = calc_cvd_changes()
        except Exception as e:
            log.warning(f"CVD task error: {e}")
        await asyncio.sleep(8)


async def task_okx_liq() -> None:
    sub_msg = json.dumps({"op":"subscribe","args":[{"channel":"liquidation-orders","instType":"SWAP"}]})
    while True:
        try:
            async with websockets.connect("wss://ws.okx.com:8443/ws/v5/public",
                                          ping_interval=20,ping_timeout=10,close_timeout=5) as ws:
                await ws.send(sub_msg)
                log.info("OKX liq WS connected")
                S["okx_liq_connected"] = True
                async for raw in ws:
                    try: msg = json.loads(raw)
                    except Exception: continue
                    if msg.get("event"): continue
                    if msg.get("arg",{}).get("channel") != "liquidation-orders": continue
                    # 只要收到该频道任意一条消息（不论哪个币种、金额多小）就更新"最后收到时间"，
                    # 这是判断连接真的在收数据、而不只是握手成功的关键信号
                    S["okx_liq_last_event_ts"] = _time.time()
                    for item in msg.get("data",[]):
                        if not item.get("instId","").startswith("BTC"): continue
                        for det in item.get("details",[]):
                            try:
                                sz,px = float(det.get("sz",0)),float(det.get("bkPx",0))
                                usd = sz*0.01*px
                                # 诊断日志：不管金额多小，只要是BTC就记一笔，用来确认OKX到底有没有
                                # 真的推送过BTC爆仓——跟"usd<10000被过滤掉"和"压根没收到"是两件事
                                log.info(f"OKX BTC爆仓明细 收到: instId={item.get('instId')} side={det.get('side')} usd=${usd:,.0f}")
                                if usd < 10_000: continue
                                # OKX含义：side="sell" 是系统强制卖出 = 多头被强平（多爆）
                                #          side="buy"  是系统强制买入 = 空头被强平（空爆）
                                e = {"time": datetime.now(SGT).strftime("%H:%M:%S"),
                                     "side": "多头爆仓" if det.get("side")=="sell" else "空头爆仓",
                                     "price": round(px,1), "usd": usd, "exchange":"OKX"}
                                S["liq"].insert(0,e); S["liq"] = S["liq"][:100]

                                _check_liq_day_rollover()
                                S["liq_daily_count"] += 1
                                S["liq_daily_max"] = max(S["liq_daily_max"], usd)
                                if det.get("side") == "sell":
                                    S["liq_daily_long_total"] += usd
                                else:
                                    S["liq_daily_short_total"] += usd

                                await bcast({"type":"liq","data":e})
                            except Exception as e:
                                log.warning(f"OKX liq detail 解析失败: {e} | raw={det}")
        except Exception as e:
            S["okx_liq_connected"] = False
            log.warning(f"OKX WS error, retry 5s: {e}")
            await asyncio.sleep(5)


# ── Gate.io 爆仓：不重复连 Gate API，直接读 gate_liq_monitor.py（btc-gate-liq
#    服务）已经在写的 gate_liquidations 表，跟 OKX 合并进同一份 S["liq"] 列表 ──
def _poll_gate_liq_sync(after_id: int) -> list:
    """同步查询：取 id > after_id 的新爆仓记录，按 id 升序返回"""
    try:
        con = sqlite3.connect("/opt/btc-trader/btc_history.db", timeout=5)
        cur = con.cursor()
        cur.execute(
            "SELECT id, ts, direction, price, usd_value FROM gate_liquidations "
            "WHERE id > ? ORDER BY id ASC LIMIT 200",
            (after_id,)
        )
        rows = cur.fetchall()
        con.close()
        return rows
    except Exception as e:
        log.warning(f"Gate liq DB 查询失败: {e}")
        return []


async def task_gate_liq() -> None:
    # 启动时先定位到当前最大 id，避免把历史存量数据一次性灌进今日面板
    try:
        init_rows = await asyncio.get_event_loop().run_in_executor(
            None, _poll_gate_liq_sync, -1)
        S["gate_liq_last_id"] = init_rows[-1][0] if init_rows else 0
    except Exception:
        S["gate_liq_last_id"] = 0

    while True:
        try:
            rows = await asyncio.get_event_loop().run_in_executor(
                None, _poll_gate_liq_sync, S.get("gate_liq_last_id", 0))
            # 只要这次查询没抛异常就算"轮询健康"——跟"有没有新爆仓事件"是两回事，
            # Gate上BTC单笔爆仓本来就可能几十分钟都没有一次，不能拿"多久没新事件"当异常
            S["gate_liq_connected"] = True
            S["gate_liq_last_poll_ts"] = _time.time()
            for row_id, ts, direction, price, usd in rows:
                S["gate_liq_last_id"] = row_id
                e = {"time": datetime.fromtimestamp(ts, SGT).strftime("%H:%M:%S"),
                     "side": direction,
                     "price": round(price, 1), "usd": usd, "exchange": "Gate"}
                S["liq"].insert(0, e); S["liq"] = S["liq"][:100]

                _check_liq_day_rollover()
                S["liq_daily_count"] += 1
                S["liq_daily_max"] = max(S["liq_daily_max"], usd)
                if direction == "多头爆仓":
                    S["liq_daily_long_total"] += usd
                else:
                    S["liq_daily_short_total"] += usd

                await bcast({"type": "liq", "data": e})
        except Exception as e:
            S["gate_liq_connected"] = False
            log.warning(f"Gate liq 轮询异常: {e}")
        await asyncio.sleep(3)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    log.info("初始化...")
    db_init()
    _atas_db_init()
    _check_liq_day_rollover()

    try: await asyncio.wait_for(_fetch_core(), timeout=20)
    except asyncio.TimeoutError: log.warning("初始化超时")

    ib_a = await asyncio.get_event_loop().run_in_executor(None, _load_ib_sync)
    if ib_a: S["ib_asia"] = ib_a

    S["ib_europe"] = await fetch_kline_ib("ib_europe")
    S["ib_us"]     = await fetch_kline_ib("ib_us")

    vp_init = await asyncio.get_event_loop().run_in_executor(None, _load_vp_sync)
    if vp_init: S["vp"] = vp_init

    mp_init = await asyncio.get_event_loop().run_in_executor(None, _load_mp_sync)
    if mp_init: S["mp"] = mp_init

    etf_init = await asyncio.get_event_loop().run_in_executor(None, _load_etf_sync)
    if etf_init: S["etf"] = etf_init

    # 启动时检查是否有遗漏的归档（例如服务刚部署，VP已有数据但今日还没归档过昨日）。
    # 跟每小时那个任务用同一套ETF就绪判断——避免每次重启服务时，
    # 用还没轮到/还没稳定的ETF数值把当天归档提前锁死成错误值。
    if S["vp"].get("date"):
        _boot_target = S["vp"]["date"]
        _boot_etf = S["etf"]
        if _boot_etf.get("date") == _boot_target and not _boot_etf.get("is_settling", False):
            db_insert_daily_archive(_boot_target)
        elif _boot_etf.get("date") and _boot_etf.get("date") > _boot_target:
            db_insert_daily_archive(_boot_target, etf_unavailable=True)
        # 否则（ETF还没轮到/还在更新窗口内）：本轮启动先不归档，
        # 等 task_archive_daily() 下一个整点自动补上

    asyncio.create_task(task_core())
    asyncio.create_task(task_funding())
    asyncio.create_task(task_ib_asia())
    asyncio.create_task(task_ib_sessions())
    asyncio.create_task(task_vp())
    asyncio.create_task(task_mp())
    asyncio.create_task(task_etf())
    asyncio.create_task(task_okx_liq())
    asyncio.create_task(task_gate_liq())
    asyncio.create_task(task_cvd())
    asyncio.create_task(task_snapshot_recorder())
    asyncio.create_task(task_archive_daily())
    log.info("BTC Dashboard API v5.0 就绪 · 端口 8001 · 历史存储已启用")
    yield


app = FastAPI(title="BTC Dashboard API", version="5.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["GET","POST"], allow_headers=["*"])


@app.get("/api/snapshot")
async def get_snapshot(): return snap()

@app.get("/api/health")
async def health():
    okx_ts  = S["okx_liq_last_event_ts"]
    gate_ts = S["gate_liq_last_poll_ts"]
    return {
        "ok": True, "clients": len(_clients), "ts": S["ts"],
        "okx_liq": {
            "connected": S["okx_liq_connected"],
            "last_event_sec_ago": (_time.time() - okx_ts) if okx_ts else None,
        },
        "gate_liq": {
            "connected": S["gate_liq_connected"],
            "last_poll_sec_ago": (_time.time() - gate_ts) if gate_ts else None,
        },
    }

@app.get("/api/history/metrics")
async def history_metrics(metric: str = "price", period: str = "7d"):
    data = db_query_metrics(metric, period)
    return {"metric": metric, "period": period, "data": data}

@app.get("/api/history/daily-archive")
async def history_daily_archive(days: int = 30):
    data = db_query_daily_archive(days)
    return {"days": days, "data": data}

@app.post("/api/refresh-ib")
async def refresh_ib():
    result = await asyncio.get_event_loop().run_in_executor(None, _load_ib_sync)
    if result: S["ib_asia"] = result
    S["ib_europe"] = await fetch_kline_ib("ib_europe")
    S["ib_us"]     = await fetch_kline_ib("ib_us")
    return {"ok": True}

@app.websocket("/ws/live")
async def ws_live(websocket: WebSocket):
    await websocket.accept(); _clients.add(websocket)
    log.info(f"+ client  online={len(_clients)}")
    try:
        await websocket.send_json({"type":"snapshot","data":snap()})
        while True:
            try: await asyncio.wait_for(websocket.receive_text(), timeout=60)
            except asyncio.TimeoutError: await websocket.send_json({"type":"ping"})
    except WebSocketDisconnect: pass
    except Exception: pass
    finally:
        _clients.discard(websocket)
        log.info(f"- client  online={len(_clients)}")





# ══════════════════════════════════════════════════════════════════
#  以下代码追加到 /opt/btc-trader/api/main.py 的末尾
#  不需要单独文件，不需要 import，直接粘贴进去即可
#  规避 uvicorn 启动时 binance_routes 模块路径找不到的问题
# ══════════════════════════════════════════════════════════════════

import sqlite3 as _sqlite3
import time as _time

_BINANCE_DB = "/opt/btc-trader/btc_history.db"


def _bq(sql: str, params: tuple = ()):
    """Binance 数据专用查询工具"""
    try:
        conn = _sqlite3.connect(_BINANCE_DB, timeout=5)
        conn.row_factory = _sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


@app.get("/api/binance/summary")
async def binance_summary():
    """
    Binance 数据摘要：OI / 资金费率 / 多空比 / 当前象限
    供 mb.661688.xyz 面板调用
    """
    now = int(_time.time())

    oi      = _bq("SELECT * FROM binance_oi        ORDER BY ts DESC LIMIT 1")
    funding = _bq("SELECT * FROM binance_funding    ORDER BY ts DESC LIMIT 1")
    ls_top  = _bq("SELECT * FROM binance_ls_top     ORDER BY ts DESC LIMIT 1")
    ls_gl   = _bq("SELECT * FROM binance_ls_global  ORDER BY ts DESC LIMIT 1")
    struct  = _bq("SELECT * FROM binance_structure  ORDER BY ts DESC LIMIT 1")

    return {
        "server_ts":        now,
        "open_interest":    oi[0]      if oi      else None,
        "funding_rate":     funding[0] if funding  else None,
        "ls_top":           ls_top[0]  if ls_top   else None,
        "ls_global":        ls_gl[0]   if ls_gl    else None,
        "market_structure": struct[0]  if struct   else None,
    }


@app.get("/api/binance/oi/history")
async def binance_oi_history(hours: int = 24):
    """OI 时序数据，供面板折线图使用，默认 24 小时"""
    cutoff = int(_time.time()) - hours * 3600
    rows = _bq(
        "SELECT ts, oi_usd, mark_px FROM binance_oi WHERE ts >= ? ORDER BY ts ASC",
        (cutoff,)
    )
    return {"hours": hours, "count": len(rows), "data": rows}


@app.get("/api/binance/funding/history")
async def binance_funding_history(hours: int = 48):
    """资金费率历史，默认 48 小时"""
    cutoff = int(_time.time()) - hours * 3600
    rows = _bq(
        "SELECT ts, rate, premium_pct FROM binance_funding WHERE ts >= ? ORDER BY ts ASC",
        (cutoff,)
    )
    return {"hours": hours, "count": len(rows), "data": rows}


@app.get("/api/binance/ls/history")
async def binance_ls_history(hours: int = 24):
    """大户多空比历史，默认 24 小时"""
    cutoff = int(_time.time()) - hours * 3600
    top = _bq(
        "SELECT ts, long_pct, short_pct, ls_ratio FROM binance_ls_top WHERE ts >= ? ORDER BY ts ASC",
        (cutoff,)
    )
    gl = _bq(
        "SELECT ts, long_pct, short_pct, ls_ratio FROM binance_ls_global WHERE ts >= ? ORDER BY ts ASC",
        (cutoff,)
    )
    return {"hours": hours, "top_traders": top, "global": gl}


@app.get("/api/binance/structure")
async def binance_structure(hours: int = 24):
    """市场结构象限历史，供简报和面板展示"""
    cutoff = int(_time.time()) - hours * 3600
    rows = _bq("""
        SELECT ts, quadrant, oi_chg, px_chg, oi_usd, mark_px, funding, top_ls, note
        FROM binance_structure
        WHERE ts >= ?
        ORDER BY ts DESC
    """, (cutoff,))
    return {"hours": hours, "count": len(rows), "data": rows}


# ══════════════════════════════════════════════════════════════════
#  ATAS Bridge — Phase 1-3：Webhook信号 / K线+Footprint / 大单捕获
#  2026-07-01 恢复重建：今日 Gate.io+OKX 健康监控上线部署时，
#  本整段代码（约450行）被意外整体删除，AtasBridge.dll 推送全部404，
#  atas_bars / atas_large_trades / atas_signals 三张表自当时起停止更新。
#  本次恢复的同时，加入多市场支持（exchange + market_type 标签），
#  配合 AtasBridge.cs v5.0（新增 Exchange / MarketType 设置项，
#  币安/OKX 现货/合约 四个图表各自标注身份后推送，不再混算）。
# ══════════════════════════════════════════════════════════════════

_ATAS_DB = "/opt/btc-trader/btc_history.db"

# 中文展示映射（Telegram 大单告警用）
_ATAS_EXCHANGE_CN = {"binance": "币安", "okx": "OKX", "unset": "未知(图表未配置)"}
_ATAS_MARKET_CN   = {"perp": "永续", "spot": "现货", "unset": "未知(图表未配置)"}


def _atas_resolve_market(data: dict) -> tuple:
    """
    从推送数据里解析 exchange/market_type，区分两种情况：
      1. 字段整个不存在 → 旧版指标(未重新编译到v5.0)，按老逻辑默认 binance/perp，
         保持向后兼容，不影响还没来得及升级的图表。
      2. 字段存在但值是 "unset" → 新版指标(v5.1+)已经在推送，但这张图表的
         Exchange/Market Type 设置面板还没手动选过——保留 "unset" 原样，不
         悄悄冒充 binance/perp，同时记一条警告日志，方便定位到底哪张图表
         漏配置了。
    """
    exchange = data.get("exchange")
    market   = data.get("market_type")
    if exchange is None and market is None:
        return "binance", "perp"   # 旧版指标，字段整个不存在
    exchange = exchange or "unset"
    market   = market or "unset"
    if exchange == "unset" or market == "unset":
        log.warning(
            f"[ATAS] 收到未配置身份的图表数据 exchange={exchange} market={market} "
            f"—— 去 ATAS 里检查是不是有图表的 Exchange/Market Type 设置面板忘了选"
        )
    return exchange, market


def _atas_db_init():
    """建表（首次部署走这里）+ 补充多市场字段迁移（老表安全幂等追加）

    2026-07-01 修复：原顺序是"建表+建索引"写在同一个 executescript() 里，
    但 idx_atas_bars_mkt / idx_atas_trades_mkt 这两个索引依赖 exchange/
    market_type 字段——VPS上 atas_bars/atas_large_trades 两张表早就存在
    （CREATE TABLE IF NOT EXISTS 对已存在的表是空操作，不会补字段），
    建索引时字段还不存在，直接报 "no such column: exchange" 中断整个函数，
    btc-api 启动阶段崩溃重启。现改为三步严格分离：
      1) 建表（新装环境这步已经带全字段，兼容处理）
      2) ALTER TABLE 给老表补字段（幂等，已存在的字段会报duplicate column，忽略）
      3) 字段确保存在后，再建依赖这些字段的索引
    """
    conn = sqlite3.connect(_ATAS_DB)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS atas_bars (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT, timeframe TEXT DEFAULT '5m',
            exchange TEXT DEFAULT 'binance', market_type TEXT DEFAULT 'perp',
            open REAL, high REAL, low REAL, close REAL,
            volume REAL, ask_vol REAL, bid_vol REAL,
            delta REAL, cumulative_delta REAL,
            max_delta REAL, min_delta REAL,
            max_oi REAL, min_oi REAL, oi_change REAL,
            poc_price REAL, max_vol_price REAL,
            max_pos_delta_price REAL, max_neg_delta_price REAL,
            footprint_json TEXT,
            dom_cum_bids REAL, dom_cum_asks REAL,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS atas_large_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT, price REAL, volume REAL, volume_usd REAL,
            exchange TEXT DEFAULT 'binance', market_type TEXT DEFAULT 'perp',
            direction TEXT, threshold_level TEXT,
            near_poc INTEGER DEFAULT 0, poc_price REAL,
            distance_from_poc_pct REAL, current_delta REAL,
            dom_bid_pressure REAL, dom_ask_pressure REAL,
            first_seen_volume REAL, growth_seconds REAL, update_count INTEGER,
            telegram_sent INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS atas_signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT, indicator_name TEXT, signal_type TEXT,
            price REAL, raw_payload TEXT,
            exchange TEXT, market_type TEXT, raw_instrument TEXT,
            telegram_sent INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_atas_bars_ts    ON atas_bars(timestamp);
        CREATE INDEX IF NOT EXISTS idx_atas_trades_ts  ON atas_large_trades(timestamp);
        CREATE INDEX IF NOT EXISTS idx_atas_signals_ts ON atas_signals(timestamp);
    """)
    conn.commit()
    conn.close()

    # ── 第二步：给"部署前已存在"的老表补新字段。对已经有该字段的表会命中
    #    "duplicate column"异常，直接忽略即可——这是幂等设计，服务重启/
    #    重复部署多少次都安全，不是报错信号 ────────────────────────────
    migrations = [
        "ALTER TABLE atas_bars ADD COLUMN exchange TEXT DEFAULT 'binance'",
        "ALTER TABLE atas_bars ADD COLUMN market_type TEXT DEFAULT 'perp'",
        "ALTER TABLE atas_bars ADD COLUMN footprint_json TEXT",
        "ALTER TABLE atas_large_trades ADD COLUMN exchange TEXT DEFAULT 'binance'",
        "ALTER TABLE atas_large_trades ADD COLUMN market_type TEXT DEFAULT 'perp'",
        "ALTER TABLE atas_large_trades ADD COLUMN first_seen_volume REAL",
        "ALTER TABLE atas_large_trades ADD COLUMN growth_seconds REAL",
        "ALTER TABLE atas_large_trades ADD COLUMN update_count INTEGER",
        "ALTER TABLE atas_signals ADD COLUMN exchange TEXT",
        "ALTER TABLE atas_signals ADD COLUMN market_type TEXT",
        "ALTER TABLE atas_signals ADD COLUMN raw_instrument TEXT",
    ]
    conn = sqlite3.connect(_ATAS_DB)
    for sql in migrations:
        try:
            conn.execute(sql)
            conn.commit()
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                log.warning(f"[ATAS] 字段迁移失败: {sql} | {e}")
    conn.close()

    # ── 第三步：字段现在保证存在了（无论老表刚被迁移、还是新表建表时就带），
    #    这时候才能安全建这两个依赖 exchange/market_type 的索引 ──────────
    conn = sqlite3.connect(_ATAS_DB)
    conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_atas_bars_mkt   ON atas_bars(exchange, market_type);
        CREATE INDEX IF NOT EXISTS idx_atas_trades_mkt ON atas_large_trades(exchange, market_type);
    """)
    conn.commit()
    conn.close()

    log.info("[ATAS] btc_history.db 三张表已就绪（含多市场字段）")


# ─── ATAS 信号冷却（内存，重启清零）──────────────────────────────
_atas_cooldown: dict = {}
_ATAS_COOLDOWN_SEC = 60   # 同类信号60秒内不重复发Telegram

# ─── ATAS 吸收信号冷却（Phase 7F，内存，重启清零）──────────────────
# 按 (exchange, market_type, side) 三元组冷却：现有 Absorption 走旧通道时
# 7天356条(≈每天51条)太吵，新通道给更长冷却
_atas_absorb_cooldown: dict = {}
_ATAS_ABSORB_COOLDOWN_SEC = 600   # 同一路同方向10分钟内不重复发Telegram


def _parse_atas_messages(messages: list) -> list:
    """解析 ATAS Webhook messages 数组
    格式：[2026-06-29 21:11:26] [BTCUSDT]: Price reached 60080.0 level
    返回：[{time, instrument, desc, price}]
    """
    import re as _re
    parsed = []
    for msg in messages:
        m = _re.search(r'\[([^\]]+)\]\s+\[([^\]]+)\]:\s+(.+)', msg)
        if not m:
            continue
        item = {
            'time':       m.group(1),
            'instrument': m.group(2),
            'desc':       m.group(3).strip(),
        }
        price_m = _re.search(r'([\d]+\.?\d*)', item['desc'].replace(',', ''))
        if price_m:
            item['price'] = float(price_m.group())
        parsed.append(item)
    return parsed


def _infer_indicator(parsed: list) -> str:
    """从解析结果推断指标名称"""
    if not parsed:
        return 'Unknown'
    desc = parsed[0].get('desc', '').lower()
    if 'price reached' in desc or 'absorption' in desc:
        return 'Absorption'
    if 'delta' in desc:
        return 'DeltaSurge'
    if 'trapped' in desc:
        return 'TrappedTraders'
    if 'power' in desc:
        return 'PowerBars'
    return 'ATASSignal'


@app.post("/atas/signal")
async def atas_signal(request: Request):
    """
    接收 ATAS 内置 Webhook 推送（Absorption 等指标告警通道）。
    走的是 ATAS 自带 Webhook 机制，不经过 AtasBridge.dll 的自定义代码，
    所以暂时还没有 exchange/market_type 标签——messages 里的 instrument
    字段先原样存进 raw_instrument，观察四个图表各自实际报什么值之后，
    再定规则回填 exchange/market_type（这次不瞎猜，等真实数据）。
    """
    try:
        raw = await request.json()
    except Exception:
        raw = {}

    ts       = datetime.now(SGT).isoformat()
    messages = raw.get('messages', [])
    parsed   = _parse_atas_messages(messages)

    indicator  = _infer_indicator(parsed)
    first_desc = parsed[0].get('desc', '') if parsed else ''
    first_px   = parsed[0].get('price')    if parsed else None
    first_inst = parsed[0].get('instrument') if parsed else None

    # ── 写库 ──────────────────────────────────────────────────────
    try:
        conn = sqlite3.connect(_ATAS_DB, timeout=5)
        conn.execute(
            'INSERT INTO atas_signals '
            '(timestamp, indicator_name, signal_type, price, raw_payload, '
            ' raw_instrument, created_at) '
            'VALUES (?,?,?,?,?,?,?)',
            (ts, indicator, first_desc, first_px,
             json.dumps(raw, ensure_ascii=False),
             first_inst,
             datetime.now(SGT).strftime('%Y-%m-%d %H:%M:%S'))
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning(f'[ATAS] signal 写库失败: {e}')

    log.info(f'[ATAS] signal [{indicator}] instrument={first_inst} {len(parsed)}条 首价={first_px}')

    # ── 过滤测试消息 ───────────────────────────────────────────────
    is_test = any('Test alert' in (p.get('desc', '')) for p in parsed)
    if is_test or not parsed:
        return {'status': 'ok', 'parsed': parsed, 'telegram': 'skipped_test'}

    # ── 冷却检查 ───────────────────────────────────────────────────
    now_ts  = datetime.now(SGT).timestamp()
    last_ts = _atas_cooldown.get(indicator, 0)
    if now_ts - last_ts < _ATAS_COOLDOWN_SEC:
        remain = int(_ATAS_COOLDOWN_SEC - (now_ts - last_ts))
        log.info(f'[ATAS] {indicator} 冷却中 剩余{remain}s')
        return {'status': 'ok', 'parsed': parsed, 'telegram': f'cooldown_{remain}s'}

    # ── 构建 Telegram 消息 ─────────────────────────────────────────
    prices     = [p['price'] for p in parsed if 'price' in p]
    time_str   = parsed[0]['time'].split(' ')[-1] if parsed else '--:--:--'
    instrument = parsed[0].get('instrument', 'BTCUSDT')

    if len(prices) > 1:
        price_str = f'{min(prices):,.0f} ~ {max(prices):,.0f}'
    elif prices:
        price_str = f'{prices[0]:,.0f}'
    else:
        price_str = '触发（无具体价格）'

    tg_msg = (
        f'📡 {indicator} | {instrument}\n\n'
        f'💰 价格：{price_str}\n'
        f'📊 触发档位：{len(parsed)} 个\n'
        f'⏰ {time_str}（北京）'
    )

    # ── 发送 Telegram ──────────────────────────────────────────────
    try:
        from alert_bot.send import async_send
        await async_send(tg_msg)
        _atas_cooldown[indicator] = now_ts
        log.info(f'[ATAS] Telegram 已推送: {indicator} {price_str}')
    except Exception as e:
        log.warning(f'[ATAS] Telegram 推送失败: {e}')

    return {'status': 'ok', 'parsed': parsed, 'indicator': indicator}


@app.post("/atas/trade")
async def atas_trade(request: Request, background_tasks: BackgroundTasks):
    try:
        data = await request.json()
    except Exception:
        return {"status": "error", "detail": "invalid json"}

    level     = data.get("threshold_level", "medium")
    volume    = float(data.get("volume", 0))
    price     = float(data.get("price", 0))
    vol_usd   = float(data.get("volume_usd", volume * price))
    direction = data.get("direction", "")
    exchange, market = _atas_resolve_market(data)

    try:
        conn = sqlite3.connect(_ATAS_DB, timeout=5)
        cur = conn.execute("""
            INSERT INTO atas_large_trades
            (timestamp, price, volume, volume_usd, exchange, market_type,
             direction, threshold_level,
             near_poc, poc_price, distance_from_poc_pct,
             current_delta, first_seen_volume, growth_seconds, update_count,
             created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            data.get("timestamp"), price, volume, vol_usd, exchange, market,
            direction, level,
            1 if data.get("near_poc") else 0,
            data.get("poc_price"),
            data.get("dist_from_poc_pct"),
            data.get("current_bar_delta"),
            data.get("first_seen_volume"),
            data.get("growth_seconds"),
            data.get("update_count"),
            datetime.now(SGT).strftime("%Y-%m-%d %H:%M:%S")
        ))
        record_id = cur.lastrowid
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning(f"[ATAS] trade write error: {e}")
        return {"status": "error", "detail": str(e)}

    log.info(f"[ATAS] trade [{exchange}/{market}] {direction} {volume:.1f}BTC @ {price:,.0f} [{level}]")

    if level in ("large", "whale"):
        background_tasks.add_task(_send_trade_alert, data, record_id)

    return {"status": "ok", "id": record_id, "level": level}


async def _send_trade_alert(data: dict, record_id: int):
    try:
        from alert_bot.send import async_send
        direction = data.get("direction", "buy")
        volume    = float(data.get("volume", 0))
        price     = float(data.get("price", 0))
        vol_usd   = float(data.get("volume_usd", volume * price))
        level     = data.get("threshold_level", "large")
        exchange, market = _atas_resolve_market(data)
        poc_price = data.get("poc_price")
        dist_pct  = data.get("dist_from_poc_pct")
        bar_delta = data.get("current_bar_delta", 0)
        ts        = data.get("timestamp", "")
        time_str  = ts.split("T")[-1][:8] if "T" in ts else ts
        wan       = vol_usd / 10000
        usd_str   = f"{wan/10000:.2f}yi" if wan >= 10000 else f"{wan:.0f}wan"
        is_buy    = (direction == "buy")

        # price range: start -> pushed（只在同一市场内找参考价，避免跨市场串价）
        try:
            _pc = sqlite3.connect(_ATAS_DB, timeout=3)
            _pr = _pc.execute(
                "SELECT close FROM atas_bars WHERE exchange=? AND market_type=? "
                "ORDER BY id DESC LIMIT 1",
                (exchange, market)
            ).fetchone()
            _pc.close()
            curr_px = float(_pr[0]) if _pr else None
        except Exception:
            curr_px = None

        px_diff = (curr_px - price) if curr_px else 0
        if curr_px and abs(px_diff) > 5:
            if is_buy and px_diff > 0:
                price_range = f"${price:,.0f}(起始) → ${curr_px:,.0f}(推进)"
            elif not is_buy and px_diff < 0:
                price_range = f"${price:,.0f}(起始) → ${curr_px:,.0f}(下测)"
            else:
                price_range = f"${price:,.0f}(未跟进)"
        else:
            price_range = f"${price:,.0f}"

        dir_cn       = "买入" if is_buy else "卖出"
        dir_icon     = "🟢" if is_buy else "🔴"
        whale_hdr    = "🚨🐋" if level == "whale" else "🐋"
        level_cn     = "鲸鱼级" if level == "whale" else "大额"
        market_label = (f"{_ATAS_EXCHANGE_CN.get(exchange, exchange)}BTCUSDT"
                         f"{_ATAS_MARKET_CN.get(market, market)}")

        try:
            from datetime import datetime as _dt
            _t = _dt.fromisoformat(ts.replace("Z",""))
            bar_min  = (_t.minute // 5) * 5
            bar_time = _t.strftime(f"%H:{bar_min:02d}")
        except Exception:
            bar_time = time_str

        poc_pos = ""
        if poc_price and dist_pct is not None:
            if dist_pct > 0.05:
                poc_rel = f"价格在上方 +{dist_pct:.2f}%"
            elif dist_pct < -0.05:
                poc_rel = f"价格在下方 {dist_pct:.2f}%"
            else:
                poc_rel = "价格就在VP-POC附近（强博弈区）"
            poc_pos = (f"\n  5min VP-POC：${poc_price:,.0f}(当前K线成交量最大价位)\n  与POC偏移：{poc_rel}")

        delta_line = ""
        if bar_delta:
            bd = float(bar_delta)
            ddir = "买方占优" if bd > 0 else "卖方占优"
            delta_line = f"\n  K线Delta({bar_time}起)：{bd:+.1f}({ddir})"

        cvd_line = ""
        if data.get("current_cvd"):
            cvd_line = f"\n  今日CVD累计：{float(data['current_cvd']):+,.1f}"

        if is_buy and poc_price and dist_pct is not None and dist_pct > 0:
            judge = f"VP-POC上方主动买入，短期偏多 |支撑参考：${poc_price:,.0f}"
        elif not is_buy and poc_price and dist_pct is not None and dist_pct < 0:
            judge = f"VP-POC下方主动卖出，短期偏空 |阻力参考：${poc_price:,.0f}"
        elif is_buy:
            judge = "主动大额买入，关注价格能否延续"
        else:
            judge = "主动大额卖出，关注价格能否继续下行"

        # 2026-07-01 新增：累计轨迹诊断——这笔单子首次被识别到时的量、从首次
        # 识别到现在过了多久、期间更新了几次。用来判断最终这个大数字是平缓
        # 累积上来的（大概率真实），还是几乎瞬间跳出来的（值得怀疑，去查
        # ATAS那边是不是把不相关的成交合并了）。旧版指标(未重新编译)不会带
        # 这三个字段，缺失时这行不显示，不影响其余内容。
        diag_line = ""
        fsv, gs, uc = data.get("first_seen_volume"), data.get("growth_seconds"), data.get("update_count")
        if fsv is not None and gs is not None and uc is not None:
            diag_line = f"\n累计轨迹：首见{float(fsv):.1f}BTC → {volume:.1f}BTC，{float(gs):.1f}秒内{int(uc)}次更新"

        msg = (
            f"{whale_hdr} **{level_cn}{dir_cn}** {dir_icon} | {market_label}\n\n"
            f"**{volume:.1f} BTC**（约 {usd_str} USDT）\n"
            f"成交价：**{price_range}**\n"
            f"类型：ATAS同价同向多笔聚合单{diag_line}\n"
            f"\n**订单流上下文**{poc_pos}{delta_line}{cvd_line}\n"
            f"\n**研判** {judge}\n"
            f"\n⏰ {time_str}（北京）"
        )
        await async_send(msg)
        conn = sqlite3.connect(_ATAS_DB, timeout=5)
        conn.execute("UPDATE atas_large_trades SET telegram_sent=1 WHERE id=?", (record_id,))
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning(f"[ATAS] trade tg error: {e}")


@app.get("/atas/status")
async def atas_status():
    """
    ATAS 数据接收状态。
    顶层字段（atas_bridge_online / last_bar）保持原语义不变——只反映
    币安永续这一路，跟现有面板 Vue3 代码（atasStatus）完全兼容，不用同步改前端。
    新增 by_market：四路（币安/OKX × 现货/合约）各自最后收到数据的时间，
    用于这次多市场上线后的验证；前端暂不消费，以后要做四路可视化时再接。
    """
    try:
        conn = sqlite3.connect(_ATAS_DB, timeout=3)

        last_sig = conn.execute(
            'SELECT timestamp, raw_payload, created_at FROM atas_signals ORDER BY id DESC LIMIT 1'
        ).fetchone()
        # 顶层 last_bar：固定看 binance/perp，保持与旧版语义一致
        last_bar = conn.execute(
            "SELECT timestamp, delta, poc_price, created_at FROM atas_bars "
            "WHERE exchange='binance' AND market_type='perp' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        today_signals = conn.execute(
            "SELECT COUNT(*) FROM atas_signals WHERE date(created_at)=date('now')"
        ).fetchone()[0]
        today_bars = conn.execute(
            "SELECT COUNT(*) FROM atas_bars WHERE date(created_at)=date('now')"
        ).fetchone()[0]
        today_trades = conn.execute(
            "SELECT COUNT(*) FROM atas_large_trades WHERE date(created_at)=date('now')"
        ).fetchone()[0]

        # 四路明细：每个 (exchange, market_type) 今天最后一条 bar 的时间
        by_market_rows = conn.execute("""
            SELECT exchange, market_type, MAX(created_at) AS last_at, COUNT(*) AS cnt
            FROM atas_bars
            WHERE date(created_at) = date('now')
            GROUP BY exchange, market_type
        """).fetchall()
        conn.close()

        bar_connected   = False
        bar_minutes_ago = None
        if last_bar and last_bar[3]:
            try:
                dt = datetime.fromisoformat(last_bar[3])
                bar_minutes_ago = int((datetime.now() - dt).total_seconds() / 60)
                bar_connected   = bar_minutes_ago < 10
            except Exception:
                pass

        by_market = {}
        for exch, mkt, last_at, cnt in by_market_rows:
            mins_ago = None
            online = False
            if last_at:
                try:
                    dt = datetime.fromisoformat(last_at)
                    mins_ago = int((datetime.now() - dt).total_seconds() / 60)
                    online = mins_ago < 10
                except Exception:
                    pass
            by_market[f"{exch}_{mkt}"] = {
                "online": online, "minutes_ago": mins_ago, "bars_today": cnt,
            }

        return {
            'phase':               'Phase 1-3 + 多市场标签（2026-07-01）',
            'atas_bridge_online':  bar_connected,
            'cooldown_status':     {k: int(_ATAS_COOLDOWN_SEC - (datetime.now(SGT).timestamp()-v))
                                    for k, v in _atas_cooldown.items()
                                    if datetime.now(SGT).timestamp()-v < _ATAS_COOLDOWN_SEC},
            'last_signal': {
                'timestamp':   last_sig[0],
                'payload':     json.loads(last_sig[1]) if last_sig and last_sig[1] else {},
                'received_at': last_sig[2],
            } if last_sig else None,
            'last_bar': {
                'timestamp':   last_bar[0],
                'delta':       last_bar[1],
                'poc_price':   last_bar[2],
                'minutes_ago': bar_minutes_ago,
            } if last_bar else None,
            'today_stats': {
                'signals':      today_signals,
                'bars':         today_bars,
                'large_trades': today_trades,
            },
            'by_market': by_market,
        }
    except Exception as e:
        return {'error': str(e)}


@app.post("/atas/bar")
async def atas_bar(request: Request):
    """接收 AtasBridge.dll 推送的 K 线摘要数据（含 exchange/market_type 标签）"""
    try:
        data = await request.json()
    except Exception:
        return {"status": "error", "detail": "invalid json"}

    exchange, market = _atas_resolve_market(data)

    try:
        conn = sqlite3.connect(_ATAS_DB, timeout=5)
        conn.execute("""
            INSERT INTO atas_bars
            (timestamp, timeframe, exchange, market_type,
             open, high, low, close, volume,
             ask_vol, bid_vol, delta, cumulative_delta,
             max_delta, min_delta, max_oi, min_oi, oi_change,
             poc_price, max_vol_price, max_pos_delta_price, max_neg_delta_price,
             footprint_json, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            data.get("timestamp"),
            data.get("timeframe", "5m"),
            exchange, market,
            data.get("open"),  data.get("high"),
            data.get("low"),   data.get("close"),
            data.get("volume"),
            data.get("ask_vol"),  data.get("bid_vol"),
            data.get("delta"),    data.get("cumulative_delta"),
            data.get("max_delta"), data.get("min_delta"),
            data.get("max_oi"),    data.get("min_oi"),
            data.get("oi_change"),
            data.get("poc_price"),     data.get("max_vol_price"),
            data.get("max_pos_delta_price"), data.get("max_neg_delta_price"),
            json.dumps(data.get("top_levels")) if data.get("top_levels") else None,
            datetime.now(SGT).strftime("%Y-%m-%d %H:%M:%S")
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning(f"[ATAS] bar 写库失败: {e}")
        return {"status": "error", "detail": str(e)}

    log.info(f"[ATAS] bar [{exchange}/{market}] {data.get('timestamp')} delta={data.get('delta')} poc={data.get('poc_price')}")
    return {"status": "ok"}


# ══════════════════════════════════════════════════════════════════
#  ATAS Bridge — Phase 7F：AtasBridge.dll 原生吸收检测（取代原
#  Absorption 走 /atas/signal 那条 ATAS 内置 Webhook——那条通道原理上
#  不带价格/数量，DLL 端在 footprint 数据流上直接算好吸收量再推送这里。
#  Bug#19 铁律：ATAS 端点块只增不删，本节新增 /atas/absorption，
#  不改动 /atas/signal、/atas/trade、/atas/bar 已有逻辑。
# ══════════════════════════════════════════════════════════════════

def _fmt_wan_yi(usd: float) -> str:
    """USD 金额转中文万/亿格式（吸收信号消息用）"""
    if usd >= 100_000_000:
        return f"{usd/100_000_000:.2f}亿美元"
    if usd >= 10_000:
        return f"{usd/10_000:.0f}万美元"
    return f"{usd:.0f}美元"


@app.post("/atas/absorption")
async def atas_absorption(request: Request, background_tasks: BackgroundTasks):
    """
    接收 AtasBridge.dll 原生吸收检测推送（v5.1+）。
    OKX 永续的张->BTC换算已经在 DLL 端完成，这里收到的 absorbed_btc/
    bid_vol/ask_vol 都已经是统一的 BTC 口径，不需要再处理。
    """
    try:
        data = await request.json()
    except Exception:
        return {"status": "error", "detail": "invalid json"}

    exchange, market = _atas_resolve_market(data)
    side         = data.get("side", "")
    price        = float(data.get("price", 0))
    absorbed_btc = float(data.get("absorbed_btc", 0))
    bid_vol      = float(data.get("bid_vol", 0))
    ask_vol      = float(data.get("ask_vol", 0))
    ratio        = float(data.get("ratio", 0))
    instrument   = data.get("instrument", "BTCUSDT")
    ts           = data.get("timestamp") or datetime.now(SGT).isoformat()

    # ── 写库（复用 atas_signals 表，不新建表）───────────────────────
    try:
        conn = sqlite3.connect(_ATAS_DB, timeout=5)
        conn.execute(
            'INSERT INTO atas_signals '
            '(timestamp, indicator_name, signal_type, price, raw_payload, '
            ' exchange, market_type, raw_instrument, created_at) '
            'VALUES (?,?,?,?,?,?,?,?,?)',
            (ts, "Absorption", side, price,
             json.dumps(data, ensure_ascii=False),
             exchange, market, instrument,
             datetime.now(SGT).strftime("%Y-%m-%d %H:%M:%S"))
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning(f"[ATAS] absorption 写库失败: {e}")
        return {"status": "error", "detail": str(e)}

    log.info(
        f"[ATAS] absorption [{exchange}/{market}] {side} @ {price:,.0f} "
        f"{absorbed_btc:.1f}BTC bid={bid_vol:.1f} ask={ask_vol:.1f} ratio={ratio:.1f}"
    )

    # ── 冷却：按 (exchange, market_type, side) 三元组，冷却期内仍写库，
    #    只是不发 TG ─────────────────────────────────────────────────
    key     = (exchange, market, side)
    now_ts  = datetime.now(SGT).timestamp()
    last_ts = _atas_absorb_cooldown.get(key, 0)
    if now_ts - last_ts < _ATAS_ABSORB_COOLDOWN_SEC:
        remain = int(_ATAS_ABSORB_COOLDOWN_SEC - (now_ts - last_ts))
        log.info(f"[ATAS] absorption {key} 冷却中 剩余{remain}s")
        return {"status": "ok", "telegram": f"cooldown_{remain}s"}

    _atas_absorb_cooldown[key] = now_ts
    background_tasks.add_task(
        _send_absorption_alert, exchange, market, instrument, side,
        price, absorbed_btc, bid_vol, ask_vol, ratio, ts
    )

    return {"status": "ok"}


async def _send_absorption_alert(exchange, market, instrument, side, price,
                                   absorbed_btc, bid_vol, ask_vol, ratio, ts):
    try:
        from alert_bot.send import async_send

        market_label = (f"{_ATAS_EXCHANGE_CN.get(exchange, exchange)}{instrument}"
                         f"{_ATAS_MARKET_CN.get(market, market)}")

        if side == "bid_absorb":
            dir_label = "买盘吸收（下方出现承接，潜在支撑）"
        elif side == "ask_absorb":
            dir_label = "卖盘吸收（上方出现压制，潜在阻力）"
        else:
            dir_label = side or "未知方向"

        usd_str  = _fmt_wan_yi(absorbed_btc * price)
        time_str = ts.split("T")[-1][:8] if "T" in ts else ts

        msg = (
            f"🧲 吸收信号 | {market_label}\n"
            f"方向：{dir_label}\n"
            f"价格：${price:,.0f}\n"
            f"吸收量：{absorbed_btc:.1f} BTC（约 {usd_str}）\n"
            f"买卖量比：{ratio:.1f} : 1\n"
            f"时间：{time_str}（北京）"
        )
        await async_send(msg)
        log.info(f"[ATAS] absorption Telegram已推送: {exchange}/{market} {side}")
    except Exception as e:
        log.warning(f"[ATAS] absorption tg error: {e}")


# ══════════════════════════════════════════════════════════════════
#  Phase 7G：信号引擎只读端点（为后续 7H 图表显示预埋）
#  monitor/signal_engine.py 是独立常驻服务，自己直接写 engine_signals 表，
#  这里只加一个只读查询接口，不涉及任何写操作，不影响其余端点。
# ══════════════════════════════════════════════════════════════════

@app.get("/api/signal/latest")
async def signal_latest():
    """返回 engine_signals 表最新一条记录（全字段），无记录返回 {"status":"empty"}"""
    try:
        conn = sqlite3.connect(_ATAS_DB, timeout=5)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM engine_signals ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        if not row:
            return {"status": "empty"}
        return dict(row)
    except Exception as e:
        return {"status": "error", "detail": str(e)}
