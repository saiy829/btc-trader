"""
Coinglass 数据采集模块
获取：多交所 Funding Rate 对比、OI 汇总、清算数据
"""
import requests
from utils.helpers import setup_logger, get_env

logger = setup_logger()

BASE_URL = "https://open-api-v3.coinglass.com"

def _get(endpoint: str, params: dict = None) -> dict:
    """带认证的 Coinglass API 请求"""
    headers = {
        "accept": "application/json",
        "coinglassSecret": get_env("COINGLASS_API_KEY")
    }
    url = f"{BASE_URL}{endpoint}"
    resp = requests.get(url, headers=headers, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != "0":
        raise ValueError(f"Coinglass API 错误: {data.get('msg', '未知错误')}")
    return data.get("data", {})


def get_multi_exchange_funding() -> list:
    """
    获取多交所实时 Funding Rate
    返回：[{"exchange": "Binance", "rate": 0.01, ...}, ...]
    """
    try:
        data = _get("/api/futures/fundingRate/exchange-list",
                    params={"symbol": "BTC"})

        results = []
        # 主要关注的交易所
        target = {"Binance", "OKX", "Bybit", "dYdX", "Bitget", "Gate"}

        for item in (data if isinstance(data, list) else []):
            ex = item.get("exchangeName", "")
            if ex in target:
                rate = float(item.get("rate", 0)) * 100
                results.append({
                    "exchange": ex,
                    "rate":     rate,
                    "rate_str": f"{rate:+.4f}%"
                })

        # 按交易所名称排序
        results.sort(key=lambda x: x["exchange"])
        logger.info(f"Coinglass Funding: 获取到 {len(results)} 个交易所数据")
        return results

    except Exception as e:
        logger.error(f"get_multi_exchange_funding 失败: {e}")
        return []


def get_exchange_oi() -> list:
    """
    获取多交所 OI（持仓量）汇总
    返回：[{"exchange": "Binance", "oi_usd": 12345678, ...}, ...]
    """
    try:
        data = _get("/api/futures/openInterest/exchange-list",
                    params={"symbol": "BTC"})

        results = []
        for item in (data if isinstance(data, list) else []):
            ex = item.get("exchangeName", "")
            if not ex:
                continue
            oi_usd = float(item.get("openInterest", 0))
            if oi_usd > 0:
                results.append({
                    "exchange": ex,
                    "oi_usd":   oi_usd,
                    "oi_str":   f"${oi_usd/1e9:.2f}B" if oi_usd > 1e9
                                else f"${oi_usd/1e6:.0f}M"
                })

        # 按 OI 大小排序
        results.sort(key=lambda x: x["oi_usd"], reverse=True)
        logger.info(f"Coinglass OI: 获取到 {len(results)} 个交易所数据")
        return results[:8]  # 只取前8大

    except Exception as e:
        logger.error(f"get_exchange_oi 失败: {e}")
        return []


def get_liquidation_map_levels() -> dict:
    """
    获取清算热力图关键价位
    返回：{
        "upper_zones": [{"price": 70000, "intensity": "high"}, ...],
        "lower_zones": [{"price": 60000, "intensity": "medium"}, ...],
    }
    """
    try:
        # 尝试获取清算热力图数据
        data = _get("/api/futures/liquidation/detail/chart",
                    params={"exName": "Binance",
                            "symbol": "BTCUSDT",
                            "range":  "12h"})

        upper_zones, lower_zones = [], []
        current_price_ref = 0

        if isinstance(data, dict):
            price_list = data.get("priceList", [])
            liq_list   = data.get("liquidationList", [])
            current_price_ref = float(data.get("currentPrice", 0))

            if price_list and liq_list:
                pairs = list(zip(price_list, liq_list))
                # 按清算量排序找出密集区
                pairs_sorted = sorted(pairs,
                                      key=lambda x: float(x[1]),
                                      reverse=True)

                for price, liq in pairs_sorted[:10]:
                    p   = float(price)
                    val = float(liq)
                    if val <= 0:
                        continue
                    level = {"price": p, "liq_usd": val,
                             "liq_str": f"${val/1e6:.1f}M"}
                    if p > current_price_ref:
                        upper_zones.append(level)
                    else:
                        lower_zones.append(level)

        upper_zones.sort(key=lambda x: x["price"])
        lower_zones.sort(key=lambda x: x["price"], reverse=True)

        logger.info(f"清算热力图: 上方{len(upper_zones)}个区域，下方{len(lower_zones)}个区域")
        return {
            "upper_zones":   upper_zones[:4],
            "lower_zones":   lower_zones[:4],
            "current_price": current_price_ref
        }

    except Exception as e:
        logger.error(f"get_liquidation_map_levels 失败（可能是免费版限制）: {e}")
        return {"upper_zones": [], "lower_zones": [], "current_price": 0}


def collect_all() -> dict:
    """汇总采集所有 Coinglass 数据（主入口）"""
    logger.info("开始采集 Coinglass 数据...")

    result = {
        "funding_multi": get_multi_exchange_funding(),
        "oi_exchanges":  get_exchange_oi(),
        "liq_map":       get_liquidation_map_levels(),
    }

    logger.info("Coinglass 数据采集完成")
    return result
