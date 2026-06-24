#!/usr/bin/env python3
"""
Binance Futures 数据采集服务 v4（最终版）
上传位置：/opt/btc-trader/monitor/binance_data_service.py
Supervisor 服务名：btc-binance-data

【v4 变更】
  移除爆仓 REST 任务：/fapi/v1/allForceOrders 已被 Binance 永久下线
                      ("The endpoint has been out of maintenance")
  移除 WebSocket 任务：fstream.binance.com 同样拒绝 CF Worker 连接（502）
  爆仓数据继续由现有 btc-liq-monitor（OKX WebSocket）承担

当前正常运行的任务：
  1. 持仓量 OI       每 5 分钟  ✅
  2. 资金费率        每 5 分钟  ✅
  3. 多空比（全市场 + 大户持仓）每 5 分钟  ✅

数据库：/opt/btc-trader/btc_history.db（binance_ 前缀表）
日志：  Supervisor 重定向至 /opt/btc-trader/logs/binance_data_out.log
"""

import asyncio
import aiohttp
import sqlite3
import logging
import time
import os
from pathlib import Path
from dotenv import load_dotenv
from typing import Optional, Any, List

# ══════════════════════════════════════════════════
#  路径 & 配置
# ══════════════════════════════════════════════════

BASE_DIR     = Path("/opt/btc-trader")
BINANCE_REST = "https://fapi.binance.com"
DB_PATH      = BASE_DIR / "btc_history.db"
SYMBOL       = "BTCUSDT"

load_dotenv(BASE_DIR / ".env")

# 轮询间隔（秒）
POLL_OI      = 300   # 5 分钟
POLL_FUNDING = 300
POLL_LS      = 300

# 四象限判定阈值（%）
OI_CHG_THRESHOLD    = 0.05
PRICE_CHG_THRESHOLD = 0.05

# ══════════════════════════════════════════════════
#  日志
# ══════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("binance_data")


# ══════════════════════════════════════════════════
#  数据库初始化
# ══════════════════════════════════════════════════

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # 持仓量
    c.execute("""
        CREATE TABLE IF NOT EXISTS binance_oi (
            ts      INTEGER PRIMARY KEY,
            oi_btc  REAL,
            oi_usd  REAL,
            mark_px REAL
        )
    """)

    # 资金费率（使用 premiumIndex，不能用 fundingRate——项目文档已记录此坑）
    c.execute("""
        CREATE TABLE IF NOT EXISTS binance_funding (
            ts          INTEGER PRIMARY KEY,
            rate        REAL,
            next_settle INTEGER,
            mark_px     REAL,
            index_px    REAL,
            premium_pct REAL
        )
    """)

    # 全市场账户多空比
    c.execute("""
        CREATE TABLE IF NOT EXISTS binance_ls_global (
            ts        INTEGER PRIMARY KEY,
            long_pct  REAL,
            short_pct REAL,
            ls_ratio  REAL
        )
    """)

    # 大户持仓多空比（Top 20%，参考价值更高）
    c.execute("""
        CREATE TABLE IF NOT EXISTS binance_ls_top (
            ts        INTEGER PRIMARY KEY,
            long_pct  REAL,
            short_pct REAL,
            ls_ratio  REAL
        )
    """)

    # 市场结构四象限快照（随每次 OI 采集自动生成）
    c.execute("""
        CREATE TABLE IF NOT EXISTS binance_structure (
            ts       INTEGER PRIMARY KEY,
            quadrant TEXT,
            oi_chg   REAL,
            px_chg   REAL,
            oi_usd   REAL,
            mark_px  REAL,
            funding  REAL,
            top_ls   REAL,
            note     TEXT
        )
    """)

    conn.commit()
    conn.close()
    log.info(f"DB 表初始化完成 → {DB_PATH}")


# ══════════════════════════════════════════════════
#  工具函数
# ══════════════════════════════════════════════════

def db_write(sql: str, params: tuple = ()) -> bool:
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        conn.execute(sql, params)
        changed = conn.total_changes
        conn.commit()
        conn.close()
        return changed > 0
    except sqlite3.IntegrityError:
        return False
    except Exception as e:
        log.error(f"DB 写入错误: {e}")
        return False


def db_read(sql: str, params: tuple = ()) -> List[dict]:
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        log.error(f"DB 查询错误: {e}")
        return []


def classify_quadrant(oi_chg: float, px_chg: float) -> tuple:
    """
    Q1: OI↑ + 价格↑  多头新仓·趋势上涨（最强做多信号）
    Q2: OI↑ + 价格↓  空头新仓·趋势下跌（最强做空信号）
    Q3: OI↓ + 价格↑  空头爆仓·轧空反弹（弱多，可持续性差）
    Q4: OI↓ + 价格↓  多头爆仓·去杠杆下跌（弱空，警惕反弹）
    """
    oi_up = oi_chg >  OI_CHG_THRESHOLD
    oi_dn = oi_chg < -OI_CHG_THRESHOLD
    px_up = px_chg >  PRICE_CHG_THRESHOLD
    px_dn = px_chg < -PRICE_CHG_THRESHOLD

    if oi_up and px_up: return "Q1", "多头新仓·趋势上涨"
    if oi_up and px_dn: return "Q2", "空头新仓·趋势下跌"
    if oi_dn and px_up: return "Q3", "空头爆仓·轧空反弹"
    if oi_dn and px_dn: return "Q4", "多头爆仓·去杠杆"
    return "FLAT", f"震荡 OI={oi_chg:+.3f}% P={px_chg:+.3f}%"


# ══════════════════════════════════════════════════
#  数据采集主类
# ══════════════════════════════════════════════════

class BinanceDataService:

    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None
        self._last_oi_usd: Optional[float] = None
        self._last_price:  Optional[float] = None

    async def run(self):
        timeout   = aiohttp.ClientTimeout(total=15, connect=5)
        connector = aiohttp.TCPConnector(limit=10, ttl_dns_cache=300)
        self.session = aiohttp.ClientSession(timeout=timeout, connector=connector)

        log.info(f"采集服务启动 | 直连 Binance REST | DB={DB_PATH}")
        log.info("运行任务：OI / 资金费率 / 多空比（全市场 + 大户持仓）")

        await asyncio.gather(
            self._task_oi(),
            self._task_funding(),
            self._task_ls_ratio(),
            return_exceptions=True,
        )

    # ── HTTP GET（直连 Binance，带重试）──────────────────────
    async def _get(self, path: str, params: dict = None) -> Optional[Any]:
        url = BINANCE_REST + path
        for attempt in range(3):
            try:
                async with self.session.get(url, params=params) as resp:
                    if resp.status == 200:
                        return await resp.json(content_type=None)
                    if resp.status == 429:
                        log.warning("Binance 限速，等待 60 秒")
                        await asyncio.sleep(60)
                        continue
                    log.warning(f"GET {path} → HTTP {resp.status}")
                    return None
            except aiohttp.ServerTimeoutError:
                log.warning(f"GET {path} 超时（第 {attempt+1} 次）")
                await asyncio.sleep(3)
            except aiohttp.ClientError as e:
                log.warning(f"GET {path} 网络错误（第 {attempt+1} 次）: {e}")
                await asyncio.sleep(2 ** attempt)
        log.error(f"GET {path} 连续 3 次失败")
        return None


    # ══════════════════════════════════════════════
    #  任务 1：持仓量（OI）
    # ══════════════════════════════════════════════
    async def _task_oi(self):
        log.info("OI 采集启动（每 5 分钟）")
        while True:
            try:
                # 并发拉 OI + premiumIndex（含 markPrice）
                oi_data, px_data = await asyncio.gather(
                    self._get("/fapi/v1/openInterest", {"symbol": SYMBOL}),
                    self._get("/fapi/v1/premiumIndex",  {"symbol": SYMBOL}),
                )
                if oi_data and px_data:
                    ts      = int(time.time())
                    oi_btc  = float(oi_data["openInterest"])
                    mark_px = float(px_data["markPrice"])
                    oi_usd  = oi_btc * mark_px

                    db_write(
                        "INSERT OR REPLACE INTO binance_oi VALUES (?,?,?,?)",
                        (ts, oi_btc, oi_usd, mark_px)
                    )

                    if self._last_oi_usd and self._last_price:
                        oi_chg     = (oi_usd  - self._last_oi_usd) / self._last_oi_usd  * 100
                        px_chg     = (mark_px - self._last_price)  / self._last_price    * 100
                        quad, note = classify_quadrant(oi_chg, px_chg)

                        fr = db_read("SELECT rate    FROM binance_funding ORDER BY ts DESC LIMIT 1")
                        ls = db_read("SELECT ls_ratio FROM binance_ls_top  ORDER BY ts DESC LIMIT 1")

                        db_write(
                            "INSERT OR REPLACE INTO binance_structure VALUES (?,?,?,?,?,?,?,?,?)",
                            (ts, quad, oi_chg, px_chg, oi_usd, mark_px,
                             fr[0]["rate"]     if fr else None,
                             ls[0]["ls_ratio"] if ls else None,
                             note)
                        )
                        log.info(
                            f"OI ${oi_usd/1e9:.3f}B | "
                            f"OI{oi_chg:+.2f}% P{px_chg:+.3f}% → [{quad}] {note}"
                        )
                    else:
                        log.info(f"OI ${oi_usd/1e9:.3f}B @{mark_px:,.0f}（首次采集，建立基准）")

                    self._last_oi_usd = oi_usd
                    self._last_price  = mark_px

            except Exception as e:
                log.error(f"OI 任务异常: {e}", exc_info=True)
            await asyncio.sleep(POLL_OI)


    # ══════════════════════════════════════════════
    #  任务 2：资金费率
    # ══════════════════════════════════════════════
    async def _task_funding(self):
        log.info("资金费率采集启动（每 5 分钟）")
        while True:
            try:
                data = await self._get("/fapi/v1/premiumIndex", {"symbol": SYMBOL})
                if data:
                    ts          = int(time.time())
                    rate        = float(data.get("lastFundingRate", 0))
                    next_settle = int(data.get("nextFundingTime", 0)) // 1000
                    mark_px     = float(data.get("markPrice", 0))
                    index_px    = float(data.get("indexPrice", 0))
                    premium_pct = ((mark_px - index_px) / index_px * 100) if index_px else 0

                    db_write(
                        "INSERT OR REPLACE INTO binance_funding VALUES (?,?,?,?,?,?)",
                        (ts, rate, next_settle, mark_px, index_px, premium_pct)
                    )

                    warn = ""
                    if rate > 0.001:    warn = " [多头过热]"
                    elif rate < -0.005: warn = " [空头过热]"
                    log.info(f"资金费率 {rate*100:+.4f}%  溢价 {premium_pct:+.4f}%{warn}")

            except Exception as e:
                log.error(f"资金费率任务异常: {e}", exc_info=True)
            await asyncio.sleep(POLL_FUNDING)


    # ══════════════════════════════════════════════
    #  任务 3：多空比（全市场账户 + 大户持仓）
    # ══════════════════════════════════════════════
    async def _task_ls_ratio(self):
        log.info("多空比采集启动（每 5 分钟）")
        while True:
            try:
                ts     = int(time.time())
                params = {"symbol": SYMBOL, "period": "5m", "limit": 1}

                global_data, top_data = await asyncio.gather(
                    self._get("/futures/data/globalLongShortAccountRatio", params),
                    self._get("/futures/data/topLongShortPositionRatio",   params),
                )

                if global_data and isinstance(global_data, list) and global_data:
                    d = global_data[0]
                    db_write(
                        "INSERT OR REPLACE INTO binance_ls_global VALUES (?,?,?,?)",
                        (ts,
                         float(d["longAccount"]),
                         float(d["shortAccount"]),
                         float(d["longShortRatio"]))
                    )

                if top_data and isinstance(top_data, list) and top_data:
                    d  = top_data[0]
                    ls = float(d["longShortRatio"])
                    db_write(
                        "INSERT OR REPLACE INTO binance_ls_top VALUES (?,?,?,?)",
                        (ts,
                         float(d["longAccount"]),
                         float(d["shortAccount"]),
                         ls)
                    )
                    warn = ""
                    if ls > 3.0:   warn = " [大户极度偏多]"
                    elif ls < 0.5: warn = " [大户极度偏空]"
                    log.info(
                        f"大户多空比 {ls:.3f} "
                        f"({float(d['longAccount'])*100:.1f}% 多 / "
                        f"{float(d['shortAccount'])*100:.1f}% 空){warn}"
                    )

            except Exception as e:
                log.error(f"多空比任务异常: {e}", exc_info=True)
            await asyncio.sleep(POLL_LS)


# ══════════════════════════════════════════════════
#  入口
# ══════════════════════════════════════════════════

async def main():
    init_db()
    svc = BinanceDataService()
    try:
        await svc.run()
    except KeyboardInterrupt:
        log.info("服务已停止")
    finally:
        if svc.session and not svc.session.closed:
            await svc.session.close()


if __name__ == "__main__":
    asyncio.run(main())
