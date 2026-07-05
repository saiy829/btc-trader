#!/usr/bin/env python3
"""
市场结构预警监控
上传位置：/opt/btc-trader/monitor/structure_monitor.py
Supervisor 服务名：btc-structure-monitor

监控内容（与现有监控互补，不重复）：
  ✅ Q1/Q2 象限确认进入（连续 2 次 = 10 分钟稳定）→ TG 推送
  ✅ 大户多空比极端值（>3.0 偏多 / <0.5 偏空）→ TG 推送
  ✅ 周一 TradFi 周初开盘窗口插针（北京时间夏令时05:00/冬令时06:00 至08:00，
     扫过 PDH/PDL 又收回）→ TG 推送（2026-07 新增，见 monday_sweep_loop）

不覆盖（已有服务处理）：
  ❌ 资金费率极端值 → btc-funding-monitor
  ❌ OI 百分比突变 → btc-oi-monitor

冷却期：
  象限告警：同一象限 60 分钟内不重复
  多空比告警：同方向 120 分钟内不重复

检查频率：每 6 分钟（略长于数据 5 分钟更新间隔）
"""

import asyncio
import aiohttp
import sqlite3
import logging
import time
import os
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from collections import deque
from dotenv import load_dotenv

BASE_DIR = Path("/opt/btc-trader")
load_dotenv(BASE_DIR / ".env")

DB_PATH   = BASE_DIR / "btc_history.db"
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

COOLDOWN_QUAD = 3600   # 象限告警冷却：60 分钟
COOLDOWN_LS   = 7200   # 多空比告警冷却：120 分钟
CHECK_INTERVAL = 360   # 检查间隔：6 分钟

# ── 周一 TradFi 周初开盘窗口插针检测参数 ──────────────────────────────
FUTURE_BASE   = "https://fapi.binance.com"
SYMBOL        = "BTCUSDT"
SWEEP_BREACH_MIN_PCT = float(os.getenv("SWEEP_BREACH_MIN_PCT", "0.03"))
SWEEP_CHECK_INTERVAL = 60    # 窗口内检查间隔：60 秒
SWEEP_IDLE_INTERVAL  = 60    # 窗口外休眠间隔：60 秒（按分钟粒度判断是否进窗口）
SWEEP_LOOKBACK_MIN   = 15    # 插针判定回看最近 15 分钟

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("structure_monitor")


# ══════════════════════════════════════════════════
#  数据库读取
# ══════════════════════════════════════════════════

def _q(sql: str, params: tuple = ()):
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        log.error(f"DB 查询错误: {e}")
        return []


# ══════════════════════════════════════════════════
#  Telegram 发送
# ══════════════════════════════════════════════════

async def send_tg(session: aiohttp.ClientSession, text: str):
    url     = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id":                  CHAT_ID,
        "text":                     text,
        "parse_mode":               "HTML",
        "disable_web_page_preview": True,
    }
    try:
        async with session.post(
            url, json=payload,
            timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            if resp.status == 200:
                log.info("TG 消息发送成功")
            else:
                body = await resp.text()
                log.error(f"TG 发送失败 HTTP {resp.status}: {body[:100]}")
    except Exception as e:
        log.error(f"TG 发送异常: {e}")


# ══════════════════════════════════════════════════
#  消息构建
# ══════════════════════════════════════════════════

def _fmt_b(val):
    if val is None:
        return "N/A"
    return f"{val / 1e8:.2f}亿"


def _build_quad_msg(s: dict, ls: dict, fr: dict, oi: dict) -> str:
    quad = s["quadrant"]
    note = s["note"]

    if quad == "Q1":
        emoji = "🟢"
        title = "多头新仓确认 · 趋势性上涨"
        desc  = ("新资金以多头方向进场，OI与价格同步上升。\n"
                 "趋势有真实资金支撑，可持续性强。")
        op    = "顺势做多方向，注意 OI 增速是否放缓"
    else:  # Q2
        emoji = "🔴"
        title = "空头新仓确认 · 趋势性下跌"
        desc  = ("新资金以空头方向进场，OI上升价格下降。\n"
                 "趋势有真实资金支撑，可持续性强。")
        op    = "顺势做空方向，注意资金费率是否极端"

    lines = [
        f"{emoji} <b>市场结构预警 · {quad} 确认</b>",
        f"",
        f"<b>{title}</b>",
        f"{desc}",
        f"",
        f"OI 变化：<b>{s['oi_chg']:+.3f}%</b>　价格变化：<b>{s['px_chg']:+.3f}%</b>",
    ]

    if oi:
        lines.append(f"当前 OI：<b>{_fmt_b(oi['oi_usd'])} USD</b>（{oi['oi_btc']:.0f} BTC）")

    if fr:
        rate_pct = fr["rate"] * 100
        lines.append(f"资金费率：<b>{rate_pct:+.4f}%</b>")

    if ls:
        ratio = ls["ls_ratio"]
        lp    = ls["long_pct"] * 100
        sp    = ls["short_pct"] * 100

        # 方向共鸣 / 拥挤风险提示
        if ratio > 2.0 and quad == "Q1":
            ls_warn = " ⚠️ 大户已偏多，注意拥挤"
        elif ratio < 0.8 and quad == "Q2":
            ls_warn = " ✅ 大户亦偏空，方向共鸣"
        elif ratio < 0.8 and quad == "Q1":
            ls_warn = " ✅ 大户偏空，价格逆向拉升"
        elif ratio > 2.0 and quad == "Q2":
            ls_warn = " ⚠️ 大户偏多，价格逆向下杀"
        else:
            ls_warn = ""

        lines.append(f"大户多空比：<b>{ratio:.3f}</b>（多{lp:.1f}%/空{sp:.1f}%）{ls_warn}")

    lines += [
        f"",
        f"操作参考：{op}",
        f"",
        f"<i>辅助判断信号，非交易建议</i>",
    ]
    return "\n".join(lines)


def _build_ls_msg(ls: dict, s: dict, direction: str) -> str:
    ratio = ls["ls_ratio"]
    lp    = ls["long_pct"] * 100
    sp    = ls["short_pct"] * 100

    if direction == "high":
        emoji  = "⚠️"
        title  = "大户持仓极度偏多（逆向看空信号）"
        interp = (f"大户多空比已达 <b>{ratio:.2f}</b>，多头高度拥挤。\n"
                  f"历史规律：此类极端值往往出现在潜在阶段顶部附近。")
        action = ("关注后续 OI 是否开始下降，\n"
                  "若 OI↓+价格↓（Q4象限）出现，去杠杆行情可能开始。")
    else:
        emoji  = "⚠️"
        title  = "大户持仓极度偏空（逆向看多信号）"
        interp = (f"大户多空比已降至 <b>{ratio:.2f}</b>，空头高度拥挤。\n"
                  f"历史规律：极端空头共识往往预示轧空风险上升。")
        action = ("关注后续 OI 是否下降配合价格上涨（Q3象限），\n"
                  "若出现则为空头被迫平仓引发的轧空行情。")

    lines = [
        f"{emoji} <b>多空比极端预警</b>",
        f"",
        f"<b>{title}</b>",
        f"大户多空比：<b>{ratio:.3f}</b>（多{lp:.1f}% / 空{sp:.1f}%）",
        f"",
        f"{interp}",
        f"",
        f"{action}",
        f"",
        f"当前象限：<b>{s['quadrant']} — {s['note']}</b>",
        f"",
        f"<i>逆向指标，非趋势跟随信号</i>",
    ]
    return "\n".join(lines)


# ══════════════════════════════════════════════════
#  周一 TradFi 周初开盘窗口插针检测
#  （与 ai_analyst/briefing.py._monday_open_window() 同款夏令时判断逻辑）
# ══════════════════════════════════════════════════

def _monday_window_start_hour() -> int:
    """周一开盘窗口起点（北京时间小时数），随美东夏令时自动切换。
    夏令时 -> 05:00 起；冬令时 -> 06:00 起。窗口终点固定 08:00（IB 起点）。"""
    try:
        ny = datetime.now(ZoneInfo("America/New_York"))
        return 5 if ny.dst() else 6
    except Exception:
        return 5


def _in_monday_window(now_bj: datetime) -> bool:
    """判断给定的北京时间是否落在周一 TradFi 周初开盘窗口内（窗口起点-08:00）。"""
    if now_bj.weekday() != 0:   # 0 = 周一
        return False
    start_hour = _monday_window_start_hour()
    return start_hour <= now_bj.hour < 8


async def _fetch_klines(session: aiohttp.ClientSession, interval: str, limit: int):
    """拉取 Binance USDT永续 K线（REST，德国IP直连正常）"""
    url = f"{FUTURE_BASE}/fapi/v1/klines"
    params = {"symbol": SYMBOL, "interval": interval, "limit": limit}
    async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
        resp.raise_for_status()
        return await resp.json()


async def _get_pdh_pdl(session: aiohttp.ClientSession):
    """
    取前一根日线（UTC自然日00:00-24:00，即北京时间昨日08:00-今日08:00，
    与现有 Market Profile / IB 定义一致）的 high/low。
    用 limit=3 取 k[-2]（而非 limit=2 取 k[0]）：与
    data_collector/binance_data.py 的 get_yesterday_ohlcv() 保持同一套已验证
    写法，避免重新引入该函数注释里提到的历史踩坑。
    """
    try:
        k = await _fetch_klines(session, "1d", 3)
        y = k[-2]
        return float(y[2]), float(y[3])   # high, low
    except Exception as e:
        log.warning(f"周一插针监控：PDH/PDL 获取失败: {e}")
        return None, None


def _check_sweep(klines_1m: list, pdh: float, pdl: float, breach_min_pct: float):
    """
    检测最近 SWEEP_LOOKBACK_MIN 分钟内是否出现"扫过 PDH/PDL 又收回"的插针。
    klines_1m: Binance 1m K线列表（按时间升序），至少需要 lookback 根数据。
    返回 (direction, extreme_price, breach_pct) 或 None；
    direction: "BSL"（上扫 PDH）/ "SSL"（下扫 PDL）。
    """
    if not klines_1m or pdh is None or pdl is None:
        return None
    recent = klines_1m[-SWEEP_LOOKBACK_MIN:]
    latest_close = float(klines_1m[-1][4])

    highs = [float(k[2]) for k in recent]
    max_high = max(highs)
    if max_high > pdh:
        breach_pct = (max_high - pdh) / pdh * 100
        if breach_pct >= breach_min_pct and latest_close < pdh:
            return ("BSL", max_high, breach_pct)

    lows = [float(k[3]) for k in recent]
    min_low = min(lows)
    if min_low < pdl:
        breach_pct = (pdl - min_low) / pdl * 100
        if breach_pct >= breach_min_pct and latest_close > pdl:
            return ("SSL", min_low, breach_pct)

    return None


def _build_sweep_msg(direction: str, pd_price: float, extreme_price: float,
                      breach_pct: float, current_price: float, now_bj: datetime) -> str:
    """
    周一插针 TG 消息（HTML模式，风格与 _build_quad_msg 一致）。
    不用 🟢/🔴：那是本文件/系统里爆仓和涨跌方向的固定配色语义，
    插针清扫不代表趋势方向，用中性提示符号避免混淆。
    """
    dir_label = "上扫 BSL（清扫多头止损）" if direction == "BSL" else "下扫 SSL（清扫空头止损）"
    pd_label  = "PDH" if direction == "BSL" else "PDL"
    time_str  = now_bj.strftime("%Y-%m-%d %H:%M")

    lines = [
        f"📌 <b>周一开盘窗口流动性清扫 · {dir_label}</b>",
        f"",
        f"{pd_label}：<b>${pd_price:,.0f}</b>",
        f"插针极值：<b>${extreme_price:,.0f}</b>（突破 {breach_pct:.3f}%）",
        f"当前价：<b>${current_price:,.0f}</b>",
        f"发生时间：{time_str}（北京时间）",
        f"",
        f"周一开盘窗口流动性清扫，IB（08:00）形成前勿追第一波方向，"
        f"等待 IB 与开盘类型确认",
        f"",
        f"<i>辅助判断信号，非交易建议</i>",
    ]
    return "\n".join(lines)


async def monday_sweep_loop():
    """
    独立协程：仅周一 TradFi 周初开盘窗口内（窗口起点-08:00 北京时间）检测插针。
    窗口外每分钟检查一次是否进入窗口即返回休眠，不拉取任何行情数据。
    """
    log.info(
        f"周一开盘窗口插针监控已启动（窗口：周一 夏令时05:00/冬令时06:00 至 08:00 "
        f"北京时间，突破阈值 {SWEEP_BREACH_MIN_PCT}%）"
    )

    sweep_alerted: dict = {}   # {"YYYY-MM-DD_方向": True}，冷却用
    cached_date = None
    pdh = pdl = None

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                now_bj = datetime.now(ZoneInfo("Asia/Shanghai"))

                if not _in_monday_window(now_bj):
                    await asyncio.sleep(SWEEP_IDLE_INTERVAL)
                    continue

                date_str = now_bj.strftime("%Y-%m-%d")
                if cached_date != date_str:
                    pdh, pdl = await _get_pdh_pdl(session)
                    cached_date = date_str
                    if pdh is not None and pdl is not None:
                        log.info(f"周一插针监控：窗口启动，PDH=${pdh:,.0f} PDL=${pdl:,.0f}")

                if pdh is None or pdl is None:
                    log.warning("周一插针监控：本窗口 PDH/PDL 不可用，跳过本次检测")
                    await asyncio.sleep(SWEEP_CHECK_INTERVAL)
                    continue

                klines_1m = await _fetch_klines(session, "1m", 16)
                result = _check_sweep(klines_1m, pdh, pdl, SWEEP_BREACH_MIN_PCT)
                if result:
                    direction, extreme_price, breach_pct = result
                    key = f"{date_str}_{direction}"
                    if key not in sweep_alerted:
                        current_price = float(klines_1m[-1][4])
                        pd_price = pdh if direction == "BSL" else pdl
                        msg = _build_sweep_msg(
                            direction, pd_price, extreme_price,
                            breach_pct, current_price, now_bj
                        )
                        await send_tg(session, msg)
                        sweep_alerted[key] = True
                        log.info(f"周一插针告警已发送: {direction} 突破{breach_pct:.3f}%")

            except Exception as e:
                log.error(f"周一插针监控循环异常: {e}", exc_info=True)

            await asyncio.sleep(SWEEP_CHECK_INTERVAL)


# ══════════════════════════════════════════════════
#  监控主循环
# ══════════════════════════════════════════════════

async def monitor_loop():
    # 连续两次象限记录，用于确认（需连续 2 次 = 10 分钟稳定）
    quad_history = deque(maxlen=2)

    # 每个象限的上次告警时间
    quad_cooldowns: dict = {}   # {quadrant: timestamp}
    ls_cooldowns: dict   = {}   # {'high'/'low': timestamp}

    log.info("市场结构预警监控启动")
    log.info(f"象限冷却：{COOLDOWN_QUAD//60}分钟  多空比冷却：{COOLDOWN_LS//60}分钟")
    log.info(f"检查间隔：{CHECK_INTERVAL//60}分钟")

    # 启动时先等待，让数据采集服务写入第一条数据
    await asyncio.sleep(30)

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                now = time.time()

                # 读取最新数据
                struct = _q("SELECT * FROM binance_structure ORDER BY ts DESC LIMIT 1")
                ls     = _q("SELECT * FROM binance_ls_top    ORDER BY ts DESC LIMIT 1")
                fr     = _q("SELECT * FROM binance_funding   ORDER BY ts DESC LIMIT 1")
                oi     = _q("SELECT * FROM binance_oi        ORDER BY ts DESC LIMIT 1")

                # 数据新鲜度检查（超过 15 分钟未更新则跳过，不告警）
                if not struct or now - struct[0]["ts"] > 900:
                    log.warning("binance_structure 数据超过15分钟未更新，跳过本次检查")
                    await asyncio.sleep(CHECK_INTERVAL)
                    continue

                s            = struct[0]
                current_quad = s["quadrant"]
                quad_history.append(current_quad)

                log.info(
                    f"当前象限: {current_quad} | "
                    f"OI{s['oi_chg']:+.3f}% P{s['px_chg']:+.3f}% | "
                    f"L/S大户: {ls[0]['ls_ratio']:.3f}" if ls else "L/S: N/A"
                )

                # ── 告警1：Q1 / Q2 象限确认 ──────────────────
                # 两次相同 = 10分钟稳定，过滤一次性噪音
                if (len(quad_history) == 2
                        and quad_history[0] == quad_history[1]
                        and current_quad in ("Q1", "Q2")):

                    last_alert = quad_cooldowns.get(current_quad, 0)
                    if now - last_alert > COOLDOWN_QUAD:
                        msg = _build_quad_msg(
                            s,
                            ls[0] if ls else None,
                            fr[0] if fr else None,
                            oi[0] if oi else None,
                        )
                        await send_tg(session, msg)
                        quad_cooldowns[current_quad] = now
                        log.info(f"象限告警已发送: [{current_quad}] {s['note']}")
                    else:
                        remaining = int((last_alert + COOLDOWN_QUAD - now) / 60)
                        log.info(f"{current_quad} 冷却中，剩余 {remaining} 分钟")

                # ── 告警2：大户多空比极端值 ───────────────────
                if ls:
                    ratio = ls[0]["ls_ratio"]

                    if ratio > 3.0:
                        last = ls_cooldowns.get("high", 0)
                        if now - last > COOLDOWN_LS:
                            msg = _build_ls_msg(ls[0], s, "high")
                            await send_tg(session, msg)
                            ls_cooldowns["high"] = now
                            log.info(f"L/S极端告警(偏多)已发送: {ratio:.3f}")

                    elif ratio < 0.5:
                        last = ls_cooldowns.get("low", 0)
                        if now - last > COOLDOWN_LS:
                            msg = _build_ls_msg(ls[0], s, "low")
                            await send_tg(session, msg)
                            ls_cooldowns["low"] = now
                            log.info(f"L/S极端告警(偏空)已发送: {ratio:.3f}")

            except Exception as e:
                log.error(f"监控循环异常: {e}", exc_info=True)

            await asyncio.sleep(CHECK_INTERVAL)


# ══════════════════════════════════════════════════
#  入口
# ══════════════════════════════════════════════════

async def _main():
    await asyncio.gather(monitor_loop(), monday_sweep_loop())


if __name__ == "__main__":
    if not BOT_TOKEN or not CHAT_ID:
        log.error("TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID 未配置，检查 .env")
        exit(1)
    asyncio.run(_main())
