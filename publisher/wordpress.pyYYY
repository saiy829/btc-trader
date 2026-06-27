"""
WordPress 自动发布模块 v6
表格式头部：永续+现货+两个成交量+Funding+Coinbase
颜色：绿涨红跌
"""
import subprocess, re
from utils.helpers import setup_logger, fmt_time, now_sgt

logger = setup_logger()
WP_PATH     = "/www/wwwroot/jianbao.661688.xyz"
WP_BASE_URL = "https://jianbao.661688.xyz"

C_UP   = "#2e7d32"
C_DOWN = "#c62828"
C_KEY  = "#e65100"
C_IB   = "#6a1b9a"
C_HEAD = "#1565c0"


def _colorize(text):
    text = re.sub(r'\$(\d{1,3}(?:,\d{3})+(?:\.\d+)?)',
        lambda m: f'<b style="color:{C_KEY};">${m.group(1)}</b>', text)
    text = re.sub(r'(\+[\d.]+%)',
        lambda m: f'<b style="color:{C_UP};">{m.group(1)}</b>', text)
    text = re.sub(r'(-[\d.]+%)',
        lambda m: f'<b style="color:{C_DOWN};">{m.group(1)}</b>', text)
    for kw in ["IB High","IB Low","IB Mid","IB区间","IB中点","IB宽度",
               "PDH","PDL","PDC","VAH","VAL","POC","HVN","LVN"]:
        text = text.replace(kw, f'<b style="color:{C_IB};">{kw}</b>')
    return text


def _body(text):
    out = []
    for line in text.split("\n"):
        s = line.strip()
        if not s:
            out.append('<div style="height:8px;"></div>')
        elif s.startswith("【") and "】" in s:
            out.append(f'<h3 style="color:{C_HEAD};font-size:17px;font-weight:700;'
                       f'border-left:4px solid {C_HEAD};padding-left:12px;'
                       f'margin:24px 0 12px;">{_colorize(s)}</h3>')
        elif set(s) <= set("=-") and len(s) > 3:
            out.append('<hr style="border:none;border-top:1px solid #e0e0e0;margin:16px 0;">')
        elif s.startswith(">") or s.startswith("▸"):
            out.append(f'<p style="color:#555;padding-left:18px;margin:5px 0;'
                       f'font-size:14px;line-height:1.9;border-left:3px solid #bdbdbd;">'
                       f'{_colorize(s)}</p>')
        else:
            out.append(f'<p style="margin:6px 0;line-height:1.9;color:#212121;'
                       f'font-size:15px;">{_colorize(s)}</p>')
    return "\n".join(out)


def _header_card(p, chg, fr, oi_chg, ts, extras):
    spot      = extras.get("spot_price", 0)
    spot_chg  = extras.get("spot_chg_pct", 0)
    spot_vol  = extras.get("spot_vol_str", "-")
    perp_vol  = extras.get("perp_vol_str", "-")
    cb_price  = extras.get("cb_price", 0)
    cb_prem   = extras.get("cb_premium", 0)
    cb_pct    = extras.get("cb_premium_pct", 0)
    cb_sig    = extras.get("cb_signal", "-")
    basis     = p - spot if spot and p else 0

    arrow     = "▲" if chg > 0 else "▼" if chg < 0 else "━"
    pc  = C_UP if chg > 0    else C_DOWN if chg < 0    else "#757575"
    sc  = C_UP if spot_chg>0 else C_DOWN if spot_chg<0 else "#757575"
    fc  = C_DOWN if fr < -0.01 else C_UP if fr > 0.01  else "#555"
    oc  = C_UP if oi_chg>0 else C_DOWN if oi_chg<0 else "#555"
    cbc = C_UP if cb_prem>0  else C_DOWN if cb_prem<0 else "#555"
    cb_bg = "#f1f8e9" if cb_prem > 0 else "#ffebee" if cb_prem < 0 else "#f5f5f5"
    cb_bd = "#a5d6a7" if cb_prem > 0 else "#ef9a9a" if cb_prem < 0 else "#e0e0e0"

    td = 'style="padding:13px 18px;vertical-align:top;border-bottom:1px solid #e8ecf0;'
    label = 'style="font-size:11px;color:#999;font-weight:600;text-transform:uppercase;letter-spacing:.04em;margin-bottom:6px;"'
    val   = 'style="font-size:17px;font-weight:700;margin-bottom:3px;"'
    sub   = 'style="font-size:12px;color:#888;"'

    return f'''<div style="border:1px solid #d0d7de;border-radius:12px;overflow:hidden;
                           margin-bottom:22px;background:#fff;font-family:'PingFang SC','Microsoft YaHei',Arial,sans-serif;">

  <!-- 时间戳栏 -->
  <div style="background:#f6f8fa;padding:9px 18px;border-bottom:1px solid #e8ecf0;
              display:flex;justify-content:space-between;flex-wrap:wrap;gap:4px;">
    <span style="font-size:12px;color:#656d76;">{ts}</span>
    <span style="font-size:12px;color:#656d76;">Binance USDT永续合约 · BTCUSDT</span>
  </div>

  <!-- 主价格 -->
  <div style="padding:16px 20px;border-bottom:1px solid #e8ecf0;">
    <div style="display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;">
      <span style="font-size:15px;font-weight:600;color:#444;">BTC</span>
      <span style="font-size:34px;font-weight:800;color:{pc};">{arrow} ${p:,.0f}</span>
      <span style="font-size:18px;font-weight:700;color:{pc};">{chg:+.2f}%</span>
    </div>
  </div>

  <!-- 数据表格 -->
  <table style="width:100%;border-collapse:collapse;">

    <!-- 行1：永续 vs 现货 -->
    <tr>
      <td {td}border-right:1px solid #e8ecf0;width:50%;">
        <div {label}>永续合约</div>
        <div {val} style="color:{pc};">${p:,.0f}</div>
        <div {sub}>24H 成交额 {perp_vol}</div>
      </td>
      <td {td}width:50%;">
        <div {label}>现货 BTC/USDT</div>
        <div {val} style="color:{sc};">${spot:,.0f}
          <span style="font-size:13px;color:{sc};">{spot_chg:+.2f}%</span>
        </div>
        <div {sub}>24H 成交额 {spot_vol}&nbsp;·&nbsp;基差 {basis:+.0f}</div>
      </td>
    </tr>

    <!-- 行2：资金费率 vs OI -->
    <tr>
      <td {td}border-right:1px solid #e8ecf0;border-bottom:none;width:50%;">
        <div {label}>资金费率</div>
        <div {val} style="color:{fc};">{fr:+.4f}%</div>
      </td>
      <td {td}border-bottom:none;width:50%;">
        <div {label}>持仓量 OI 24H</div>
        <div {val} style="color:{oc};">{oi_chg:+.2f}%</div>
      </td>
    </tr>

  </table>

  <!-- Coinbase 溢价（全宽） -->
  <div style="padding:12px 18px;background:{cb_bg};
              border-top:1px solid {cb_bd};
              display:flex;align-items:center;flex-wrap:wrap;gap:16px;">
    <div>
      <span style="font-size:11px;color:#888;font-weight:600;text-transform:uppercase;
                   letter-spacing:.04em;margin-right:10px;">Coinbase BTC/USD</span>
      <span style="font-size:17px;font-weight:700;color:#333;">${cb_price:,.0f}</span>
    </div>
    <div style="display:flex;align-items:center;gap:8px;">
      <span style="font-size:11px;color:#888;">溢价指数</span>
      <span style="font-size:17px;font-weight:700;color:{cbc};">{cb_prem:+.0f} USD</span>
      <span style="font-size:13px;color:{cbc};">({cb_pct:+.3f}%)</span>
      <span style="font-size:12px;color:#555;background:rgba(0,0,0,.07);
                   padding:2px 9px;border-radius:4px;">{cb_sig}</span>
    </div>
  </div>

</div>'''


def _to_html(briefing_text, binance_data, extras):
    p      = binance_data.get("price", {})
    price  = p.get("price", 0)
    chg    = p.get("change_pct", 0)
    fr     = binance_data.get("funding", {}).get("rate", 0)
    oi_chg = binance_data.get("oi", {}).get("change_24h_pct", 0)
    ts     = binance_data.get("timestamp", fmt_time())

    header = _header_card(price, chg, fr, oi_chg, ts, extras)
    body   = _body(briefing_text)

    return f'''<div style="font-family:'PingFang SC','Microsoft YaHei',Arial,sans-serif;
                           max-width:880px;margin:0 auto;color:#212121;">
  {header}
  <!-- 颜色图例 -->
  <div style="font-size:12px;color:#aaa;text-align:right;margin-bottom:10px;">
    <span style="color:{C_UP};">■ 绿涨</span>&nbsp;
    <span style="color:{C_DOWN};">■ 红跌</span>&nbsp;
    <span style="color:{C_KEY};">■ 关键价位</span>&nbsp;
    <span style="color:{C_IB};">■ IB数据</span>
  </div>
  <!-- 正文 -->
  <div style="background:#fff;border:1px solid #e3e8f0;border-radius:12px;
              padding:28px 32px;line-height:1.9;">
    {body}
  </div>
  <div style="margin-top:16px;text-align:center;font-size:12px;color:#bbb;">
    Binance USDT永续合约&nbsp;·&nbsp;BTC AI 交易系统&nbsp;·&nbsp;仅供参考
  </div>
</div>'''


def _get_or_create_category(name):
    try:
        r = subprocess.run(['wp','term','list','category',f'--name={name}',
             '--field=term_id',f'--path={WP_PATH}','--allow-root'],
            capture_output=True, text=True, timeout=10)
        tid = r.stdout.strip()
        if tid and tid.isdigit(): return tid
        r2 = subprocess.run(['wp','term','create','category',name,
             '--porcelain',f'--path={WP_PATH}','--allow-root'],
            capture_output=True, text=True, timeout=10)
        return r2.stdout.strip() or "1"
    except Exception: return "1"


def publish_briefing(briefing_text, binance_data, extras=None):
    try:
        now     = now_sgt()
        # 标题不含 SGT，保持简洁
        title   = f"BTC 交易简报 · {now.strftime('%Y-%m-%d %H:%M')}"
        content = _to_html(briefing_text, binance_data, extras or {})
        cat_id  = _get_or_create_category("每日简报")
        logger.info(f"发布: {title}")
        result  = subprocess.run(
            ['wp','post','create',
             f'--post_title={title}',f'--post_content={content}',
             '--post_status=publish',f'--post_category={cat_id}',
             '--porcelain',f'--path={WP_PATH}','--allow-root'],
            capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            logger.error(f"WP-CLI 失败: {result.stderr[:200]}")
            return ""
        post_id = result.stdout.strip()
        if not post_id.isdigit(): return ""
        link_r = subprocess.run(['wp','post','get',post_id,'--field=guid',
             f'--path={WP_PATH}','--allow-root'],
            capture_output=True, text=True, timeout=10)
        link = link_r.stdout.strip() or f"{WP_BASE_URL}/?p={post_id}"
        logger.info(f"发布成功 | {title} | {link}")
        return link
    except subprocess.TimeoutExpired:
        logger.error("WP-CLI 超时"); return ""
    except Exception as e:
        logger.error(f"异常: {e}"); return ""
