"""
WordPress 自动发布模块 v7
新增颜色规则：
  - 评级 A/B/C/D → 彩色大字
  - 市场状态（三因子分类）→ 按风险着色
  - 做多/观望/做空 段落 → 绿/灰/红左侧色条
  - 止损价 → 红色粗体  目标价 → 绿色粗体  入场价 → 蓝色粗体
  - Z-score 数值 → 按极端度着色
  - BSL/SSL → 橙色警示
  - Header Card 新增：Z-score 行 + 市场状态行
"""
import subprocess, re
from utils.helpers import setup_logger, fmt_time, now_sgt

logger = setup_logger()
WP_PATH     = "/www/wwwroot/jianbao.661688.xyz"
WP_BASE_URL = "https://jianbao.661688.xyz"

# ── 颜色常量 ──────────────────────────────────────────────────────────────
C_UP     = "#1e8449"   # 绿：涨价、做多、目标
C_DOWN   = "#c0392b"   # 红：跌价、做空、止损
C_KEY    = "#d35400"   # 橙：关键价位、BSL/SSL
C_IB     = "#6a1b9a"   # 紫：IB / Market Profile 数据
C_HEAD   = "#1565c0"   # 蓝：标题、中性价格
C_CAUTION= "#e67e22"   # 橙黄：谨慎状态
C_NEUTRAL= "#555555"   # 深灰：中性文字
C_GRAY   = "#9e9e9e"   # 灰：次要信息、观望
C_REGIME = {           # 市场状态配色
    "red":    C_DOWN,
    "orange": C_CAUTION,
    "green":  C_UP,
    "blue":   C_HEAD,
}
GRADE_COLOR = {        # 评级配色
    "A": C_UP,
    "B": C_HEAD,
    "C": C_CAUTION,
    "D": C_DOWN,
}


# ── 风险分级辅助 ──────────────────────────────────────────────────────────
def _regime_risk(label: str) -> str:
    """根据市场状态标签返回风险等级 red/orange/green/blue"""
    red_kws    = ["过热", "顶部风险", "拥挤承压", "被迫平仓", "横盘多头拥挤"]
    orange_kws = ["去杠杆", "清洗中", "混合信号", "空头拥挤承托", "横盘空头拥挤"]
    green_kws  = ["健康趋势", "趋势延续", "真实趋势建仓"]
    blue_kws   = ["挤压酝酿"]
    if any(k in label for k in red_kws):    return "red"
    if any(k in label for k in orange_kws): return "orange"
    if any(k in label for k in green_kws):  return "green"
    if any(k in label for k in blue_kws):   return "blue"
    return "orange"


# ── 内联颜色工具 ──────────────────────────────────────────────────────────
def _sp(text, color, bold=True):
    w = "font-weight:700;" if bold else ""
    return f'<span style="color:{color};{w}">{text}</span>'


# ── 文本颜色处理（行内 pattern 替换）──────────────────────────────────────
def _colorize(text: str) -> str:
    """
    两阶段着色：先用 marker 标记高优先级元素（止损/目标/入场价），
    再应用通用规则，最后还原 marker，避免嵌套 span。
    """
    markers = {}

    def mark(html):
        key = f"\x00M{len(markers)}\x00"
        markers[key] = html
        return key

    # ── 阶段1：高优先级元素 → marker ────────────────────────────────────
    # 止损价 → 红
    text = re.sub(r'(止损[：:\s]*)(\$[\d,]+)',
        lambda m: m.group(1) + mark(_sp(m.group(2), C_DOWN)), text)
    # 目标价 → 绿
    text = re.sub(r'(目标[1-5]?[：:\s]*)(\$[\d,]+)',
        lambda m: m.group(1) + mark(_sp(m.group(2), C_UP)), text)
    # 入场/触发价 → 蓝
    text = re.sub(r'((?:入场区间|触发|入场)[：:\s]*)(\$[\d,]+(?:\s*[-~至到]\s*\$[\d,]+)?)',
        lambda m: m.group(1) + mark(_sp(m.group(2), C_HEAD)), text)

    # ── 阶段2：通用规则（marker 不含 $，不会被误匹配）────────────────────
    # 通用价格 → 橙
    text = re.sub(r'(?<!\w)(\$\d{1,3}(?:,\d{3})+(?:\.\d+)?)',
        lambda m: _sp(m.group(1), C_KEY), text)
    # 正/负百分比
    text = re.sub(r'(\+[\d.]+%)', lambda m: _sp(m.group(1), C_UP), text)
    text = re.sub(r'(-[\d.]+%)',   lambda m: _sp(m.group(1), C_DOWN), text)
    # 评级字母
    text = re.sub(r'(评级[：:]\s*)([ABCD])\b',
        lambda m: m.group(1) + _sp(m.group(2), GRADE_COLOR.get(m.group(2), C_HEAD)), text)
    # Z-score 数值
    def _zcolor(m):
        try:
            v = float(m.group(2))
            c = C_DOWN if abs(v) > 2 else (C_CAUTION if abs(v) > 1 else C_UP)
        except ValueError:
            c = C_NEUTRAL
        return m.group(1) + _sp(m.group(2), c)
    text = re.sub(r'(Z(?:-|\s)?score[：:]\s*)([+-]?\d+\.\d+)', _zcolor, text)
    # BSL/SSL
    text = re.sub(r'\b(BSL|SSL)\b', lambda m: _sp(m.group(1), C_KEY), text)
    # IB/MP 关键词 → 紫
    for kw in ["IB High","IB Low","IB Mid","IB区间","IB中点","IB宽度",
               "PDH","PDL","PDC","VAH","VAL","POC","HVN","LVN"]:
        text = text.replace(kw, _sp(kw, C_IB))

    # ── 阶段3：还原 marker ───────────────────────────────────────────────
    for key, html in markers.items():
        text = text.replace(key, html)

    return text


# ── 段落类型判断 ──────────────────────────────────────────────────────────
def _section_style(line: str):
    """
    返回 (left_border_color, text_hint) 用于 做多/做空/观望 段落。
    None 表示普通段落。
    """
    s = line.strip()
    if re.match(r'^做多', s):    return C_UP,    "long"
    if re.match(r'^做空', s):    return C_DOWN,  "short"
    if re.match(r'^观望', s):    return C_GRAY,  "watch"
    return None, None


def _body(text: str) -> str:
    """将纯文本简报转换为带样式的 HTML"""
    out = []
    lines = text.split("\n")
    in_long = in_short = in_watch = False   # 段落状态追踪

    for raw in lines:
        s = raw.strip()

        # ── 空行 ────────────────────────────────────────────────────────
        if not s:
            in_long = in_short = in_watch = False
            out.append('<div style="height:8px;"></div>')
            continue

        # ── 纯分隔线 ────────────────────────────────────────────────────
        if set(s) <= set("=-") and len(s) > 3:
            in_long = in_short = in_watch = False
            out.append('<hr style="border:none;border-top:1px solid #e0e0e0;margin:16px 0;">')
            continue

        # ── 节标题 【...】 ───────────────────────────────────────────────
        if s.startswith("【") and "】" in s:
            in_long = in_short = in_watch = False
            out.append(
                f'<h3 style="color:{C_HEAD};font-size:16px;font-weight:700;'
                f'border-left:4px solid {C_HEAD};padding-left:12px;'
                f'margin:24px 0 10px;">{_colorize(s)}</h3>'
            )
            continue

        # ── 市场状态 / 三因子状态行 ─────────────────────────────────────
        if re.search(r'(当前状态|市场状态)[：:\s]', s):
            risk = _regime_risk(s)
            rc   = C_REGIME[risk]
            bg   = {"red":"#fdecea","orange":"#fff3e0",
                    "green":"#e8f5e9","blue":"#e3f2fd"}.get(risk, "#f5f5f5")
            out.append(
                f'<div style="background:{bg};border-left:4px solid {rc};'
                f'padding:10px 16px;border-radius:4px;margin:8px 0;'
                f'font-weight:600;color:{rc};">{_colorize(s)}</div>'
            )
            continue

        # ── 做多 / 做空 / 观望 ─────────────────────────────────────────
        border_c, hint = _section_style(s)
        if border_c:
            in_long  = (hint == "long")
            in_short = (hint == "short")
            in_watch = (hint == "watch")
            out.append(
                f'<p style="margin:6px 0;padding:8px 14px;line-height:1.9;'
                f'border-left:4px solid {border_c};'
                f'background:{"#f1f8f1" if hint=="long" else "#fdf3f2" if hint=="short" else "#f9f9f9"};'
                f'border-radius:3px;color:{border_c};font-weight:700;'
                f'font-size:15px;">{_colorize(s)}</p>'
            )
            continue

        # ── 做多/做空/观望段落内的子条目 ───────────────────────────────
        if (in_long or in_short or in_watch) and (s.startswith("-") or s.startswith("·")):
            bc = C_UP if in_long else (C_DOWN if in_short else C_GRAY)
            out.append(
                f'<p style="margin:3px 0 3px 16px;line-height:1.9;'
                f'color:{C_NEUTRAL};font-size:14px;">{_colorize(s)}</p>'
            )
            continue

        # ── > 引用行 ───────────────────────────────────────────────────
        if s.startswith(">") or s.startswith("▸"):
            out.append(
                f'<p style="color:#546e7a;padding:4px 14px;margin:4px 0;'
                f'font-size:14px;line-height:1.9;'
                f'border-left:3px solid #b0bec5;">{_colorize(s)}</p>'
            )
            continue

        # ── 普通段落 ───────────────────────────────────────────────────
        out.append(
            f'<p style="margin:6px 0;line-height:1.9;color:#212121;'
            f'font-size:15px;">{_colorize(s)}</p>'
        )

    return "\n".join(out)


# ── Header Card（含 Z-score + 市场状态）──────────────────────────────────
def _header_card(p, chg, fr, oi_chg, ts, extras, market_meta=None):
    meta = market_meta or {}

    spot      = extras.get("spot_price", 0)
    spot_chg  = extras.get("spot_chg_pct", 0)
    spot_vol  = extras.get("spot_vol_str", "-")
    perp_vol  = extras.get("perp_vol_str", "-")
    cb_price  = extras.get("cb_price", 0)
    cb_prem   = extras.get("cb_premium", 0)
    cb_pct    = extras.get("cb_premium_pct", 0)
    cb_sig    = extras.get("cb_signal", "-")
    basis     = p - spot if spot and p else 0

    # Z-score
    zscore      = meta.get("fr_zscore")
    z_label     = meta.get("fr_zscore_label", "")
    z_str       = f"{zscore:+.2f}" if zscore is not None else "—"
    z_color     = (C_DOWN if zscore and abs(zscore)>2
                   else C_CAUTION if zscore and abs(zscore)>1
                   else C_UP) if zscore is not None else C_NEUTRAL

    # 市场状态
    regime      = meta.get("regime", "")
    regime_act  = meta.get("regime_action", "")
    regime_risk = _regime_risk(regime)
    rc          = C_REGIME[regime_risk]
    regime_bg   = {"red":"#fdecea","orange":"#fff3e0",
                   "green":"#e8f5e9","blue":"#e3f2fd"}.get(regime_risk,"#f5f5f5")

    arrow = "▲" if chg > 0 else "▼" if chg < 0 else "━"
    pc  = C_UP if chg > 0    else C_DOWN if chg < 0    else "#757575"
    sc  = C_UP if spot_chg>0 else C_DOWN if spot_chg<0 else "#757575"
    fc  = C_DOWN if fr<-0.01 else C_UP if fr>0.01 else "#555"
    oc  = C_UP if oi_chg>0   else C_DOWN if oi_chg<0  else "#555"
    cbc = C_UP if cb_prem>0  else C_DOWN if cb_prem<0 else "#555"
    cb_bg = "#f1f8e9" if cb_prem>0 else "#ffebee" if cb_prem<0 else "#f5f5f5"
    cb_bd = "#a5d6a7" if cb_prem>0 else "#ef9a9a" if cb_prem<0 else "#e0e0e0"

    td    = 'style="padding:13px 18px;vertical-align:top;border-bottom:1px solid #e8ecf0;'
    label = 'style="font-size:11px;color:#999;font-weight:600;text-transform:uppercase;letter-spacing:.04em;margin-bottom:6px;"'
    val   = 'style="font-size:17px;font-weight:700;margin-bottom:3px;"'
    sub   = 'style="font-size:12px;color:#888;"'

    regime_row = ""
    if regime:
        regime_row = f'''
    <!-- 行3：Z-score + 市场状态 -->
    <tr>
      <td {td}border-right:1px solid #e8ecf0;border-bottom:none;width:50%;">
        <div {label}>资金费率 Z-score</div>
        <div {val} style="color:{z_color};">{z_str}</div>
        <div {sub}>{z_label}</div>
      </td>
      <td {td}border-bottom:none;background:{regime_bg};width:50%;">
        <div {label}>市场状态</div>
        <div style="font-size:15px;font-weight:700;color:{rc};margin-bottom:4px;">{regime}</div>
        <div {sub} style="color:{rc};opacity:.85;">{regime_act}</div>
      </td>
    </tr>'''

    return f'''<div style="border:1px solid #d0d7de;border-radius:12px;overflow:hidden;
                           margin-bottom:22px;background:#fff;
                           font-family:'PingFang SC','Microsoft YaHei',Arial,sans-serif;">
  <div style="background:#f6f8fa;padding:9px 18px;border-bottom:1px solid #e8ecf0;
              display:flex;justify-content:space-between;flex-wrap:wrap;gap:4px;">
    <span style="font-size:12px;color:#656d76;">{ts}</span>
    <span style="font-size:12px;color:#656d76;">Binance USDT永续合约 · BTCUSDT</span>
  </div>
  <div style="padding:16px 20px;border-bottom:1px solid #e8ecf0;">
    <div style="display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;">
      <span style="font-size:15px;font-weight:600;color:#444;">BTC</span>
      <span style="font-size:34px;font-weight:800;color:{pc};">{arrow} ${p:,.0f}</span>
      <span style="font-size:18px;font-weight:700;color:{pc};">{chg:+.2f}%</span>
    </div>
  </div>
  <table style="width:100%;border-collapse:collapse;">
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
    <tr>
      <td {td}border-right:1px solid #e8ecf0;width:50%;">
        <div {label}>资金费率</div>
        <div {val} style="color:{fc};">{fr:+.4f}%</div>
      </td>
      <td {td}width:50%;">
        <div {label}>持仓量 OI 24H</div>
        <div {val} style="color:{oc};">{oi_chg:+.2f}%</div>
      </td>
    </tr>
    {regime_row}
  </table>
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


# ── 完整 HTML 组装 ────────────────────────────────────────────────────────
def _to_html(briefing_text, binance_data, extras):
    p      = binance_data.get("price", {})
    price  = p.get("price", 0)
    chg    = p.get("change_pct", 0)
    fr     = binance_data.get("funding", {}).get("rate", 0)
    oi_chg = binance_data.get("oi", {}).get("change_24h_pct", 0)
    ts     = binance_data.get("timestamp", fmt_time())
    meta   = binance_data.get("market_meta", {})

    header = _header_card(price, chg, fr, oi_chg, ts, extras, meta)
    body   = _body(briefing_text)

    legend = (
        f'<span style="color:{C_UP};">■ 做多/目标</span>&nbsp;'
        f'<span style="color:{C_DOWN};">■ 做空/止损</span>&nbsp;'
        f'<span style="color:{C_KEY};">■ 关键价位</span>&nbsp;'
        f'<span style="color:{C_IB};">■ IB/MP数据</span>&nbsp;'
        f'<span style="color:{C_HEAD};">■ 入场触发</span>'
    )

    return f'''<div style="font-family:'PingFang SC','Microsoft YaHei',Arial,sans-serif;
                           max-width:880px;margin:0 auto;color:#212121;">
  {header}
  <div style="font-size:12px;color:#aaa;text-align:right;margin-bottom:10px;">{legend}</div>
  <div style="background:#fff;border:1px solid #e3e8f0;border-radius:12px;
              padding:28px 32px;line-height:1.9;">
    {body}
  </div>
  <div style="margin-top:16px;text-align:center;font-size:12px;color:#bbb;">
    Binance USDT永续合约&nbsp;·&nbsp;BTC AI 交易系统&nbsp;·&nbsp;仅供参考
  </div>
</div>'''


# ── WP 辅助函数 ───────────────────────────────────────────────────────────
def _get_or_create_category(name):
    try:
        r = subprocess.run(
            ['wp','term','list','category',f'--name={name}',
             '--field=term_id',f'--path={WP_PATH}','--allow-root'],
            capture_output=True, text=True, timeout=10)
        tid = r.stdout.strip()
        if tid and tid.isdigit(): return tid
        r2 = subprocess.run(
            ['wp','term','create','category',name,
             '--porcelain',f'--path={WP_PATH}','--allow-root'],
            capture_output=True, text=True, timeout=10)
        return r2.stdout.strip() or "1"
    except Exception: return "1"


def publish_briefing(briefing_text, binance_data, extras=None):
    try:
        now     = now_sgt()
        title   = f"BTC 交易简报 · {now.strftime('%Y-%m-%d %H:%M')}"
        content = _to_html(briefing_text, binance_data, extras or {})
        cat_id  = _get_or_create_category("每日简报")
        logger.info(f"发布: {title}")
        result  = subprocess.run(
            ['wp','post','create',
             f'--post_title={title}', f'--post_content={content}',
             '--post_status=publish', f'--post_category={cat_id}',
             '--porcelain', f'--path={WP_PATH}', '--allow-root'],
            capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            logger.error(f"WP-CLI 失败: {result.stderr[:200]}")
            return ""
        post_id = result.stdout.strip()
        if not post_id.isdigit(): return ""
        link_r = subprocess.run(
            ['wp','post','get', post_id, '--field=guid',
             f'--path={WP_PATH}', '--allow-root'],
            capture_output=True, text=True, timeout=10)
        link = link_r.stdout.strip() or f"{WP_BASE_URL}/?p={post_id}"
        logger.info(f"发布成功 | {title} | {link}")
        return link
    except subprocess.TimeoutExpired:
        logger.error("WP-CLI 超时"); return ""
    except Exception as e:
        logger.error(f"异常: {e}"); return ""
