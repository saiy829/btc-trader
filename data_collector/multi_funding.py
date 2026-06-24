"""
多交所 Funding Rate 聚合模块
直接调各交所公开 API，无需任何 Key
覆盖：Binance / OKX / Bybit / Bitget / Gate.io
"""
import requests
from utils.helpers import setup_logger

logger = setup_logger()
TIMEOUT = 8


def _binance() -> dict:
    """
    v2修复：原接口 /fapi/v1/fundingRate 是历史已结算记录（每8小时一次），
    limit=1取到的可能是几小时前的旧费率，与其余4所的"实时滚动费率"口径不一致。
    改用 /fapi/v1/premiumIndex 的 lastFundingRate，与其余4所口径统一。
    """
    try:
        r = requests.get(
            "https://fapi.binance.com/fapi/v1/premiumIndex",
            params={"symbol": "BTCUSDT"}, timeout=TIMEOUT)
        d = r.json()
        rate = float(d["lastFundingRate"]) * 100
        return {"exchange": "Binance", "rate": rate}
    except Exception as e:
        logger.warning(f"Binance funding 失败: {e}")
        return None


def _okx() -> dict:
    try:
        r = requests.get(
            "https://www.okx.com/api/v5/public/funding-rate",
            params={"instId": "BTC-USDT-SWAP"}, timeout=TIMEOUT)
        d = r.json()
        rate = float(d["data"][0]["fundingRate"]) * 100
        return {"exchange": "OKX", "rate": rate}
    except Exception as e:
        logger.warning(f"OKX funding 失败: {e}")
        return None


def _bybit() -> dict:
    try:
        r = requests.get(
            "https://api.bybit.com/v5/market/tickers",
            params={"category": "linear", "symbol": "BTCUSDT"}, timeout=TIMEOUT)
        d = r.json()
        rate = float(d["result"]["list"][0]["fundingRate"]) * 100
        return {"exchange": "Bybit", "rate": rate}
    except Exception as e:
        logger.warning(f"Bybit funding 失败: {e}")
        return None


def _bitget() -> dict:
    try:
        r = requests.get(
            "https://api.bitget.com/api/v2/mix/market/current-fund-rate",
            params={"symbol": "BTCUSDT", "productType": "USDT-FUTURES"},
            timeout=TIMEOUT)
        d = r.json()
        rate = float(d["data"][0]["fundingRate"]) * 100
        return {"exchange": "Bitget", "rate": rate}
    except Exception as e:
        logger.warning(f"Bitget funding 失败: {e}")
        return None


def _gate() -> dict:
    try:
        r = requests.get(
            "https://api.gateio.ws/api/v4/futures/usdt/contracts/BTC_USDT",
            timeout=TIMEOUT)
        rate = float(r.json()["funding_rate"]) * 100
        return {"exchange": "Gate.io", "rate": rate}
    except Exception as e:
        logger.warning(f"Gate.io funding 失败: {e}")
        return None


def _signal(rate: float) -> str:
    if rate > 0.05:    return "极度偏多 警惕挤多"
    elif rate > 0.01:  return "偏多"
    elif rate >= -0.01: return "中性 最干净"
    elif rate >= -0.05: return "偏空"
    else:              return "极度偏空 警惕挤空"


def collect_multi_funding() -> dict:
    """
    采集所有交所 Funding Rate
    返回 {
        "exchanges": [{"exchange": "Binance", "rate": 0.01, "signal": "..."}, ...],
        "avg_rate": 0.008,
        "consensus": "偏多",
        "extreme_count": 0,
        "summary": "五大交所平均 +0.008%，整体偏多..."
    }
    """
    logger.info("采集多交所 Funding Rate...")

    fetchers = [_binance, _okx, _bybit, _bitget, _gate]
    exchanges = []

    for fn in fetchers:
        result = fn()
        if result:
            result["signal"] = _signal(result["rate"])
            result["rate_str"] = f"{result['rate']:+.4f}%"
            exchanges.append(result)

    if not exchanges:
        return {"exchanges": [], "avg_rate": 0, "consensus": "未知",
                "extreme_count": 0, "summary": "Funding 数据获取失败"}

    avg  = sum(e["rate"] for e in exchanges) / len(exchanges)
    ext  = sum(1 for e in exchanges if abs(e["rate"]) > 0.05)
    cons = _signal(avg)

    # 检查各所是否一致
    positives = sum(1 for e in exchanges if e["rate"] > 0)
    negatives = len(exchanges) - positives
    alignment = "方向一致" if (positives == len(exchanges) or negatives == len(exchanges)) \
                else f"方向分歧（{positives}所偏多/{negatives}所偏空）"

    summary = (
        f"{len(exchanges)} 家交所平均 {avg:+.4f}%，整体{cons}，{alignment}"
        + (f"，{ext} 家达到极端值" if ext > 0 else "")
    )

    logger.info(f"Funding 采集完成: 均值 {avg:+.4f}%，{len(exchanges)} 所")

    return {
        "exchanges":     exchanges,
        "avg_rate":      avg,
        "consensus":     cons,
        "extreme_count": ext,
        "alignment":     alignment,
        "summary":       summary,
    }
