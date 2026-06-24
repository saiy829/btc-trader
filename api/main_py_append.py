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
