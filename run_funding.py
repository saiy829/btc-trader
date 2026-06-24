"""
Funding Rate 监控启动入口（由 Supervisor 管理）
手动测试：python run_funding.py
"""
from monitor.funding_monitor import run

if __name__ == "__main__":
    run()
