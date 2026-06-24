"""
清算事件专用 AI 分析简报
"""
import anthropic
from utils.helpers import setup_logger, get_env

logger = setup_logger()


def generate_liq_briefing(data, long_liq, short_liq, total_liq):
    try:
        p  = data.get("price", {})
        y  = data.get("yesterday", {})
        f  = data.get("funding", {})
        oi = data.get("oi", {})
        ls = data.get("ls_ratio", {})

        fr        = f.get("rate", 0.0)
        price_chg = p.get("change_pct", 0.0)
        oi_chg    = oi.get("change_24h_pct", 0.0)

        if long_liq > short_liq * 1.5:
            liq_bias = f"多头清算主导（{long_liq/total_liq*100:.0f}%），抛压较重"
        elif short_liq > long_liq * 1.5:
            liq_bias = f"空头清算主导（{short_liq/total_liq*100:.0f}%），轧空动能较强"
        else:
            liq_bias = (f"多空清算均衡"
                        f"（多头{long_liq/total_liq*100:.0f}% / "
                        f"空头{short_liq/total_liq*100:.0f}%）")

        prompt = f"""你是专业 BTC 期货交易分析师，精通 AMT、Market Profile、Volume Profile、Order Flow。

刚刚发生大额清算事件，请生成针对性实时分析简报。

当前时间：{data.get("timestamp", "")}

=== 清算事件数据 ===
多头清算：${long_liq:,.0f}
空头清算：${short_liq:,.0f}
1小时合计：${total_liq:,.0f}
清算方向：{liq_bias}

=== 当前市场数据 ===
BTC价格：${p.get("price", 0):,.0f}  24H涨跌：{price_chg:+.2f}%
昨日PDH：${y.get("high", 0):,.0f}  PDL：${y.get("low", 0):,.0f}  PDC：${y.get("close", 0):,.0f}
Funding：{fr:+.4f}%
OI 24H变化：{oi_chg:+.2f}%
大户多空比：{ls.get("top_long_pct", 50):.1f}%多 / {ls.get("top_short_pct", 50):.1f}%空

=== 请按以下5节输出中文简报（纯文本，800字以内）===

1.【清算事件解读】性质判断，对短期价格的影响
2.【当前价格结构】相对PDH/PDL/PDC的位置，是否在关键支阻位
3.【衍生品背景】Funding+OI+多空比综合，是否有进一步清算风险
4.【Order Flow关注点】建议在ATAS重点观察什么信号
5.【操作建议】看多条件/看空条件/观望条件，关键止损位参考

格式：纯文本，用 = - > 符号，不用 * # 等Markdown"""

        client = anthropic.Anthropic(api_key=get_env("ANTHROPIC_API_KEY"))
        msg = client.messages.create(
            model="claude-sonnet-4-5", max_tokens=1500,
            messages=[{"role": "user", "content": prompt}])
        result = msg.content[0].text
        logger.info(f"清算简报生成完成（{len(result)} 字）")
        return result

    except Exception as e:
        logger.error(f"generate_liq_briefing 失败: {e}")
        return f"AI 分析生成失败：{e}"
