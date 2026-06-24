"""
/opt/btc-trader/api/binance_routes.py
Binance 数据 FastAPI 路由模块

在 /opt/btc-trader/api/main.py 中引入（添加两行）：
    from binance_routes import router as binance_router
    app.include_router(binance_router)

所有接口前缀：/api/binance/
数据来自 /opt/btc-trader/btc_history.db（binance_ 前缀表）
"""

import sqlite3
import time
from pathlib import Path
from typing import Optional, List
from fastapi import APIRouter, HTTPException, Query

router   = APIRouter(prefix="/api/binance", tags=["Binance"])
DB_PATH  = Path("/opt/btc-trader/btc_history.db")


def _q(sql: str, params: tuple = ()) -> List[dict]:
    """通用查询，返回字典列表"""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB 查询错误: {e}")


# ── 核心摘要（仪表盘首屏，一次性取全部最新值）──────────────

@router.get("/summary")
async def get_summary():
    """
    返回全部最新指标，供 mb.661688.xyz 面板使用
    包含：OI、资金费率、多空比（全市场+大户）、Taker量、
          当前市场结构象限、最近5分钟爆仓统计、最近1小时大额爆仓
    """
    now = int(time.time())

    oi      = _q("SELECT * FROM binance_oi        ORDER BY ts DESC LIMIT 1")
    funding = _q("SELECT * FROM binance_funding    ORDER BY ts DESC LIMIT 1")
    ls_top  = _q("SELECT * FROM binance_ls_top     ORDER BY ts DESC LIMIT 1")
    ls_gl   = _q("SELECT * FROM binance_ls_global  ORDER BY ts DESC LIMIT 1")
    taker   = _q("SELECT * FROM binance_taker      ORDER BY ts DESC LIMIT 1")
    struct  = _q("SELECT * FROM binance_structure  ORDER BY ts DESC LIMIT 1")

    # 最近 5 分钟爆仓按方向汇总
    liq_5m = _q("""
        SELECT side,
               COUNT(*)     AS cnt,
               SUM(qty_usd) AS total_usd,
               MAX(qty_usd) AS max_single
        FROM binance_liq
        WHERE ts >= ?
        GROUP BY side
    """, (now - 300,))

    # 最近 1 小时大额爆仓（单笔 >= 50 万 USD）
    big_liqs = _q("""
        SELECT ts, side, price, qty_btc, qty_usd
        FROM binance_liq
        WHERE ts >= ? AND qty_usd >= 500000
        ORDER BY ts DESC
        LIMIT 10
    """, (now - 3600,))

    return {
        "server_ts":        now,
        "open_interest":    oi[0]      if oi      else None,
        "funding_rate":     funding[0] if funding  else None,
        "ls_top":           ls_top[0]  if ls_top   else None,
        "ls_global":        ls_gl[0]   if ls_gl    else None,
        "taker_volume":     taker[0]   if taker    else None,
        "market_structure": struct[0]  if struct   else None,
        "liq_5m": {
            r["side"]: {
                "count":   r["cnt"],
                "usd":     r["total_usd"],
                "max_one": r["max_single"],
            }
            for r in liq_5m
        },
        "big_liqs_1h": big_liqs,
    }


# ── OI 历史（绘图用）────────────────────────────────────────

@router.get("/oi/history")
async def get_oi_history(hours: int = Query(24, ge=1, le=168)):
    """OI 时序，供面板折线图使用。默认 24h，最长 7 天"""
    cutoff = int(time.time()) - hours * 3600
    rows = _q("""
        SELECT ts, oi_usd, mark_px
        FROM binance_oi
        WHERE ts >= ?
        ORDER BY ts ASC
    """, (cutoff,))
    return {"hours": hours, "count": len(rows), "data": rows}


# ── 资金费率历史 ─────────────────────────────────────────────

@router.get("/funding/history")
async def get_funding_history(hours: int = Query(48, ge=1, le=168)):
    cutoff = int(time.time()) - hours * 3600
    rows = _q("""
        SELECT ts, rate, next_settle, premium_pct
        FROM binance_funding
        WHERE ts >= ?
        ORDER BY ts ASC
    """, (cutoff,))
    return {"hours": hours, "count": len(rows), "data": rows}


# ── 爆仓明细 ─────────────────────────────────────────────────

@router.get("/liquidations")
async def get_liquidations(
    minutes:  int   = Query(60,  ge=1,  le=1440),
    side:     Optional[str] = None,   # LONG_LIQ 或 SHORT_LIQ
    min_usd:  float = Query(0,   ge=0),
):
    """
    最近 N 分钟爆仓明细，可按方向和最小金额过滤。
    返回汇总统计 + 明细列表（最多 500 条）
    """
    cutoff = int(time.time()) - minutes * 60

    sql    = "SELECT ts, side, price, qty_btc, qty_usd, source FROM binance_liq WHERE ts >= ?"
    params: list = [cutoff]

    if side in ("LONG_LIQ", "SHORT_LIQ"):
        sql += " AND side = ?"
        params.append(side)
    if min_usd > 0:
        sql += " AND qty_usd >= ?"
        params.append(min_usd)
    sql += " ORDER BY ts DESC LIMIT 500"

    rows      = _q(sql, tuple(params))
    total_usd = sum(r["qty_usd"] for r in rows)
    long_usd  = sum(r["qty_usd"] for r in rows if r["side"] == "LONG_LIQ")
    short_usd = sum(r["qty_usd"] for r in rows if r["side"] == "SHORT_LIQ")

    return {
        "minutes":   minutes,
        "count":     len(rows),
        "total_usd": total_usd,
        "long_usd":  long_usd,
        "short_usd": short_usd,
        "data":      rows,
    }


# ── 市场结构象限历史 ─────────────────────────────────────────

@router.get("/market-structure")
async def get_market_structure(hours: int = Query(24, ge=1, le=72)):
    """象限时序，供简报和面板展示市场结构变化"""
    cutoff = int(time.time()) - hours * 3600
    rows = _q("""
        SELECT ts, quadrant, oi_chg, px_chg, oi_usd, mark_px, funding, top_ls, note
        FROM binance_structure
        WHERE ts >= ?
        ORDER BY ts DESC
    """, (cutoff,))
    return {"hours": hours, "count": len(rows), "data": rows}


# ── 多空比历史 ───────────────────────────────────────────────

@router.get("/ls-ratio/history")
async def get_ls_history(hours: int = Query(24, ge=1, le=72)):
    cutoff = int(time.time()) - hours * 3600
    top    = _q("SELECT ts, long_pct, short_pct, ls_ratio FROM binance_ls_top    WHERE ts >= ? ORDER BY ts ASC", (cutoff,))
    gl     = _q("SELECT ts, long_pct, short_pct, ls_ratio FROM binance_ls_global WHERE ts >= ? ORDER BY ts ASC", (cutoff,))
    return {"hours": hours, "top_traders": top, "global": gl}
