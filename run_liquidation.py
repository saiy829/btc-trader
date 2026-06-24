"""
清算监控启动入口（由 Supervisor 管理）
手动测试：python run_liquidation.py
"""
import asyncio
from monitor.liquidation_monitor import run

if __name__ == "__main__":
    asyncio.run(run())
