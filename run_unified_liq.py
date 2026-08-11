"""
统一爆仓采集器启动入口（由 Supervisor 管理：btc-liq-unified）
手动测试：/opt/btc-trader/venv/bin/python run_unified_liq.py
"""
import asyncio

from monitor.unified_liq_collector import run

if __name__ == "__main__":
    asyncio.run(run())
