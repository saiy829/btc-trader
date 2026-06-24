#!/usr/bin/env python3
"""
市场结构预警监控
上传位置：/opt/btc-trader/monitor/structure_monitor.py
Supervisor 服务名：btc-structure-monitor

监控内容（与现有监控互补，不重复）：
  ✅ Q1/Q2 象限确认进入（连续 2 次 = 10 分钟稳定）→ TG 推送
  ✅ 大户多空比极端值（>3.0 偏多 / <0.5 偏空）→ TG 推送

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

if __name__ == "__main__":
    if not BOT_TOKEN or not CHAT_ID:
        log.error("TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID 未配置，检查 .env")
        exit(1)
    asyncio.run(monitor_loop())
