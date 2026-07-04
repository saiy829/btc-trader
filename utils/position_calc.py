"""
仓位风控计算模块（Phase 7B）
纯计算，不依赖网络/数据库，供 scheduler.py 的 /pos 命令调用。

本版仅实现固定风险比例（Fixed Fractional）计算：
- 方向由入场价/止损价大小关系自动判断
- 风险金额 = 资金 × 风险%
- 建议仓位 = 风险金额 / 止损距离，向下取整到 0.001 BTC
- 5x/10x/20x 三档杠杆分别给出所需保证金 + 估算强平价
- 强平价用隔离保证金近似公式（维持保证金率按 0.4% 估算），仅供参考

预留扩展位（Phase 5B 胜率数据就绪后接入，本版不实现）：
- ATR 动态止损/仓位换算：需要 K 线 ATR 数据源，止损距离由 ATR 倍数决定而非手动输入
- Kelly 公式仓位建议：需要历史胜率 + 盈亏比样本，用 f = W - (1-W)/R 计算最优仓位比例
"""
import math

MAINTENANCE_MARGIN_RATE = 0.004  # 维持保证金率，估算值，各交易所实际值不同且分档
LEVERAGES = (5, 10, 20)
STOP_TOO_CLOSE_PCT = 0.15  # 止损距离百分比阈值，低于此值提示"止损过近"
MIN_STEP_BTC = 0.001

USAGE_TEXT = (
    "用法：/pos <入场价> <止损价> [资金USDT] [风险%]\n"
    "示例：\n"
    "  /pos 62800 62550\n"
    "  /pos 62800 62550 20000 0.5\n"
    "不填资金/风险% 时使用 .env 默认值（POS_ACCOUNT_USDT / POS_RISK_PCT）"
)


class PositionCalcError(Exception):
    """参数错误或计算前置条件不满足，调用方应捕获并展示 str(e) 给用户"""
    pass


def _floor_step(value: float, step: float) -> float:
    """向下取整到指定步长（避免浮点误差用 round 先做一次修正）"""
    return math.floor(round(value / step, 8)) * step


def _fmt_price(x: float) -> str:
    if abs(x - round(x)) < 1e-9:
        return f"${x:,.0f}"
    return f"${x:,.2f}"


def _liq_price(entry: float, leverage: int, is_short: bool) -> float:
    """
    估算强平价（隔离保证金近似公式，忽略手续费/资金费影响）：
    做多：Liq ≈ Entry × (1 − 1/杠杆 + 维持保证金率)
    做空：Liq ≈ Entry × (1 + 1/杠杆 − 维持保证金率)
    """
    im_rate = 1 / leverage
    if is_short:
        return entry * (1 + im_rate - MAINTENANCE_MARGIN_RATE)
    return entry * (1 - im_rate + MAINTENANCE_MARGIN_RATE)


def calc_position(entry: float, stop: float, account_usdt: float, risk_pct: float) -> dict:
    """核心仓位计算，返回结构化结果供 format_message() 渲染"""
    if entry <= 0 or stop <= 0:
        raise PositionCalcError("入场价/止损价必须为正数")
    if entry == stop:
        raise PositionCalcError("入场价与止损价不能相同")
    if account_usdt <= 0:
        raise PositionCalcError("资金必须为正数")
    if risk_pct <= 0:
        raise PositionCalcError("风险% 必须为正数")

    is_short = stop > entry
    direction = "做空" if is_short else "做多"

    stop_distance = abs(entry - stop)
    stop_distance_pct = stop_distance / entry * 100
    stop_too_close = stop_distance_pct < STOP_TOO_CLOSE_PCT

    risk_amount = account_usdt * risk_pct / 100
    raw_size = risk_amount / stop_distance
    position_btc = _floor_step(raw_size, MIN_STEP_BTC)
    notional_usdt = position_btc * entry

    leverage_rows = []
    for lev in LEVERAGES:
        margin = notional_usdt / lev
        liq_price = _liq_price(entry, lev, is_short)
        danger = (liq_price <= stop) if is_short else (liq_price >= stop)
        leverage_rows.append({
            "leverage": lev,
            "margin_usdt": margin,
            "liq_price": liq_price,
            "danger": danger,
        })

    return {
        "direction": direction,
        "is_short": is_short,
        "entry": entry,
        "stop": stop,
        "stop_distance": stop_distance,
        "stop_distance_pct": stop_distance_pct,
        "stop_too_close": stop_too_close,
        "risk_amount": risk_amount,
        "position_btc": position_btc,
        "notional_usdt": notional_usdt,
        "leverages": leverage_rows,
    }


def parse_and_calc(args, default_account: float, default_risk: float) -> dict:
    """
    解析 /pos 命令参数（字符串列表）并调用 calc_position。
    args 长度必须是 2~4；缺省的资金/风险% 用调用方传入的默认值。
    参数不合法时统一抛出 PositionCalcError，调用方负责回复 USAGE_TEXT。
    """
    if not (2 <= len(args) <= 4):
        raise PositionCalcError("参数数量不对，需要 2~4 个参数（入场价 止损价 [资金] [风险%]）")

    try:
        entry = float(args[0])
        stop = float(args[1])
    except ValueError:
        raise PositionCalcError("入场价/止损价必须是数字")

    account = default_account
    if len(args) >= 3:
        try:
            account = float(args[2])
        except ValueError:
            raise PositionCalcError("资金必须是数字")

    risk = default_risk
    if len(args) == 4:
        try:
            risk = float(args[3])
        except ValueError:
            raise PositionCalcError("风险% 必须是数字")

    return calc_position(entry, stop, account, risk)


def format_message(r: dict) -> str:
    """把 calc_position() 的结果渲染成 Telegram 纯文本消息"""
    emoji = "🔴" if r["is_short"] else "🟢"
    lines = [
        f"{emoji} {r['direction']}方案",
        "",
        f"入场：{_fmt_price(r['entry'])}　止损：{_fmt_price(r['stop'])}",
        f"止损距离：{_fmt_price(r['stop_distance'])}（{r['stop_distance_pct']:.2f}%）",
    ]
    if r["stop_too_close"]:
        lines.append(f"⚠️ 止损距离低于 {STOP_TOO_CLOSE_PCT:.2f}%，易被噪音扫损，建议放宽止损")

    lines.append("")
    lines.append(f"风险金额：${r['risk_amount']:,.2f}")
    if r["position_btc"] <= 0:
        lines.append("建议仓位：不足 0.001 BTC（风险金额相对止损距离过小，"
                      "低于最小下单单位，建议提高风险% 或放宽止损）")
    else:
        lines.append(f"建议仓位：{r['position_btc']:.3f} BTC（名义价值 ${r['notional_usdt']:,.2f}）")

    lines.append("")
    lines.append("杠杆方案（保证金 / 预估强平价）：")
    for row in r["leverages"]:
        warn = "　⚠️ 危险：强平先于止损，禁用此杠杆" if row["danger"] else ""
        lines.append(
            f"  {row['leverage']}x　保证金 ${row['margin_usdt']:,.2f}　"
            f"预估强平价 {_fmt_price(row['liq_price'])}{warn}"
        )

    lines.append("")
    lines.append("＊强平价为估算值（维持保证金率按0.4%估算），以交易所实际显示为准")
    return "\n".join(lines)
