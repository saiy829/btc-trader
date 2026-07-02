"""
startup_guard.py — 启动授权验证
=================================
保护方式：
  1. 密钥验证：.env 中的 BTC_TRADER_KEY 必须存在且长度 ≥ 32
  2. 主机绑定：hostname 必须包含 VPS 标识，防止代码搬到别处运行

密钥生成：
  venv/bin/python3 -c "import secrets; print(secrets.token_hex(32))"

密钥写入：
  echo 'BTC_TRADER_KEY=生成的密钥' >> /opt/btc-trader/.env

注意：.env 已在 .gitignore 中排除，密钥不会上传 GitHub。
"""

import os
import socket
from pathlib import Path

# ── 配置 ──────────────────────────────────────────────────────────────────
KEY_ENV_VAR   = "BTC_TRADER_KEY"    # .env 中的变量名
MIN_KEY_LEN   = 32                  # 密钥最小长度
EXPECTED_HOST = "206507"            # VPS hostname 中的唯一标识


def verify():
    """
    启动授权验证入口。
    在 scheduler.py 的 main() 最顶部调用：
        from startup_guard import verify
        verify()
    验证失败时直接退出进程，不抛异常（防止被 try/except 绕过）。
    """
    # utils/helpers.py 在 import 时已执行 load_dotenv，
    # 所以此处 os.getenv 能直接读到 .env 中的变量。
    # 若 startup_guard 比 helpers 更早被导入，手动补加载：
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).parent / '.env')
    except ImportError:
        pass   # dotenv 未安装时跳过（helpers 已处理）

    errors = []

    # ── 验证 1：密钥是否存在 ──────────────────────────────────────
    key = os.getenv(KEY_ENV_VAR, "").strip()
    if len(key) < MIN_KEY_LEN:
        errors.append(
            f"  ✗ 启动密钥无效（{KEY_ENV_VAR} 未设置或长度不足 {MIN_KEY_LEN} 位）\n"
            f"    操作：在 .env 文件添加 {KEY_ENV_VAR}=<32位以上随机字符串>"
        )

    # ── 验证 2：运行主机是否匹配 ────────────────────────────────────
    hostname = socket.gethostname()
    if EXPECTED_HOST not in hostname:
        errors.append(
            f"  ✗ 运行环境不匹配\n"
            f"    当前主机: {hostname}\n"
            f"    期望含有: {EXPECTED_HOST}"
        )

    # ── 结果 ────────────────────────────────────────────────────────
    if errors:
        print("\n" + "═" * 45)
        print("  BTC AI 系统：授权验证失败，拒绝启动")
        print("═" * 45)
        for e in errors:
            print(e)
        print("═" * 45 + "\n")
        os._exit(1)   # 直接退出，不可被 try/except 捕获

    # 验证通过（不打印密钥内容，只打印主机标识）
    print(f"  ✓ 授权验证通过 | host={hostname} | key={'*' * 8}...{key[-4:]}")
