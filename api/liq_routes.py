"""
/opt/btc-trader/api/liq_routes.py
统一爆仓采集器（9B）只读路由模块

提供：
    GET /api/liq/health     三路通道健康状态
    GET /api/liq/summary    按交易所/方向的聚合（便于对账，不改采集层过滤）

── 挂载说明（本卡未执行，留给切换卡）────────────────────────────────
本卡范围声明明确要求「不改 api/main.py」，因此**本模块尚未被挂载**。
将来挂载时在 api/main.py 加两行：

    from api.liq_routes import router as liq_router
    app.include_router(liq_router)

注意两个坑：
  1. 必须写 `from api.liq_routes import ...`（带 api. 包前缀）。
     api/binance_routes.py 当年就是因为写成 `from binance_routes import ...`
     在 uvicorn 下找不到模块，最后把路由内联进了 main.py
     （见 api/main.py:840 的注释），导致 binance_routes.py 至今是死代码。
  2. 挂载需要重启 btc-api，而 btc-api 进程内存里存着面板的
     S["liq"] / S["liq_daily_*"]（今日爆仓笔数与最大单笔），重启即清零。
     切换时机要考虑这一点。

── 数据来源 ────────────────────────────────────────────────────────
健康数据由 monitor/unified_liq_collector.py 每 5 秒原子写入
/opt/btc-trader/data/liq_unified_health.json（跨进程读取，避免本模块
去连采集器进程的内存）。采集器自身也在 127.0.0.1:8011 直接提供同一个
端点，本卡的验证走的是那个端口。
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query

router = APIRouter(prefix="/api/liq", tags=["Liquidations-Unified"])

DB_PATH     = Path("/opt/btc-trader/btc_history.db")
HEALTH_JSON = Path("/opt/btc-trader/data/liq_unified_health.json")
UTC8        = timezone(timedelta(hours=8))
SNAPSHOT_STALE_SEC = 30      # 快照超过这个岁数就认为采集器没在写了


def _q(sql: str, params: tuple = ()) -> List[dict]:
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except sqlite3.Error as exc:
        raise HTTPException(status_code=500,
                            detail=f"DB 查询错误: {type(exc).__name__}: {exc}")


@router.get("/health")
async def liq_health() -> dict:
    """
    三路通道健康。每路返回 connected / last_ctrl_msg_sec_ago /
    last_liq_event_sec_ago / reconnects / errors_1h。

    判据提醒：**爆仓流长时间无数据不算异常**（9A 实测 Gate 首条爆仓
    延迟可达 39~53 分钟），只有对照流静默才是真异常。
    """
    if not HEALTH_JSON.exists():
        return {"ok": False,
                "error": "健康快照文件不存在，采集器可能未运行",
                "path": str(HEALTH_JSON)}
    try:
        data = json.loads(HEALTH_JSON.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500,
                            detail=f"读取健康快照失败: {type(exc).__name__}: {exc}")

    age = round(time.time() - data.get("server_ts_ms", 0) / 1000, 1)
    data["snapshot_age_sec"] = age
    data["snapshot_fresh"] = age <= SNAPSHOT_STALE_SEC
    data["ok"] = bool(data["snapshot_fresh"]) and all(
        ex.get("ctrl_healthy") for ex in data.get("exchanges", {}).values())
    return data


@router.get("/summary")
async def liq_summary(hours: int = Query(2, ge=1, le=168),
                      min_usd: float = Query(0, ge=0)) -> dict:
    """
    按交易所 + 方向聚合。金额过滤在**查询层**做，采集层不设门槛。
    """
    cutoff = int(time.time()) - hours * 3600
    rows = _q(
        "SELECT exchange, side, COUNT(*) AS cnt, "
        "       ROUND(SUM(qty_usd)) AS usd, ROUND(MAX(qty_usd)) AS max_one, "
        "       ROUND(SUM(qty_btc), 6) AS btc "
        "FROM liquidations WHERE ts >= ? AND qty_usd >= ? "
        "GROUP BY exchange, side ORDER BY exchange, side",
        (cutoff, min_usd))
    span = _q(
        "SELECT MIN(ts) AS t0, MAX(ts) AS t1, COUNT(*) AS n "
        "FROM liquidations WHERE ts >= ? AND qty_usd >= ?",
        (cutoff, min_usd))
    s = span[0] if span else {"t0": None, "t1": None, "n": 0}
    return {
        "hours": hours,
        "min_usd": min_usd,
        "total_rows": s["n"],
        "first_event_utc8": (datetime.fromtimestamp(s["t0"], UTC8).isoformat()
                             if s["t0"] else None),
        "last_event_utc8": (datetime.fromtimestamp(s["t1"], UTC8).isoformat()
                            if s["t1"] else None),
        "by_exchange_side": rows,
    }


@router.get("/recent")
async def liq_recent(limit: int = Query(50, ge=1, le=500),
                     exchange: Optional[str] = None,
                     min_usd: float = Query(0, ge=0),
                     with_raw: bool = False) -> dict:
    """最近若干条明细。with_raw=true 时带原始 JSON，供人工核对解析正确性。"""
    sql = ("SELECT uid, ts_ms, ts, exchange, symbol, side, price, qty_btc, "
           "qty_usd, collector, ingested_at" + (", raw" if with_raw else "") +
           " FROM liquidations WHERE qty_usd >= ?")
    params: list = [min_usd]
    if exchange:
        sql += " AND exchange = ?"
        params.append(exchange)
    sql += " ORDER BY ts_ms DESC LIMIT ?"
    params.append(limit)
    rows = _q(sql, tuple(params))
    for r in rows:
        r["event_time_utc8"] = datetime.fromtimestamp(
            r["ts_ms"] / 1000, UTC8).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    return {"count": len(rows), "rows": rows}
