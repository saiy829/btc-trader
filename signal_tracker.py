"""
Phase 5A: ATAS 信号结果追踪服务
每5分钟运行一次，自动记录 Absorption 信号触发时的上下文，
并在4H/24H后回查价格结果，积累胜率统计数据。

2026-07-01 更新：get_latest_bar() 加 exchange/market_type 过滤，固定看
币安永续这一路。原因：ATASBridge 现在同时接了币安/OKX 现货/合约四路数据，
写进了同一张 atas_bars 表；如果这里不过滤，"最新一根K线"可能随手抓到
OKX现货的bar，而当前 Absorption 信号统计的胜率样本一直是针对币安永续
校准的，两者对不上会把 trigger_poc/trigger_delta/trigger_cvd 这些
上下文记错，污染正在积累的胜率数据集。
"""
import sqlite3
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = "/opt/btc-trader/btc_history.db"
SGT     = timezone(timedelta(hours=8))
log     = logging.getLogger("signal_tracker")


def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def get_current_price():
    """
    从 binance_structure 取最新价格。仅供 register_new_signals() 记录触发
    时刻的参考价使用（还有 atas_bars.close 兜底，见调用处）；4H/24H 结果
    回查不用这个，改用下面的 get_price_at_time() 按各自到期时刻查历史价——
    原来这里查的是不存在的 price 列（真实列名是 mark_px），查询直接报错
    被 except Exception 吞掉，check_outcomes() 拿到 None 就整轮跳过，导致
    结果回查从上线起就没有真正跑通过（2026-07-12 发现，见下方修复记录）。
    """
    try:
        conn = get_conn()
        row = conn.execute(
            "SELECT mark_px FROM binance_structure ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        conn.close()
        return float(row["mark_px"]) if row and row["mark_px"] else None
    except Exception:
        return None


def get_price_at_time(target_time_str: str):
    """
    查 atas_bars（固定币安永续，理由同 get_latest_bar）里最接近
    target_time_str（"YYYY-MM-DD HH:MM:SS" 北京时间字符串）的那根K线收盘价。
    回查4H/24H结果必须用"目标到期时刻"的历史价，不能用"现在"的价格——
    否则一次性补跑积压信号时，全部会被错误地按"补跑那一刻"结算，而不是
    各自真正到期的那一刻，统计会完全失真（这正是本次修复要同时解决的
    第二个问题，不只是列名报错那一个）。
    优先找 <= 目标时间里最近的一根；找不到（比如目标时间早于数据起点）
    再退而找 >= 目标时间里最近的一根。
    """
    try:
        conn = get_conn()
        target_iso = target_time_str.replace(" ", "T") + "+08:00"
        row = conn.execute("""
            SELECT close FROM atas_bars
            WHERE exchange='binance' AND market_type='perp' AND timestamp <= ?
            ORDER BY timestamp DESC LIMIT 1
        """, (target_iso,)).fetchone()
        if not row:
            row = conn.execute("""
                SELECT close FROM atas_bars
                WHERE exchange='binance' AND market_type='perp' AND timestamp >= ?
                ORDER BY timestamp ASC LIMIT 1
            """, (target_iso,)).fetchone()
        conn.close()
        return float(row["close"]) if row and row["close"] else None
    except Exception:
        return None


def get_latest_bar():
    """获取最新一根K线的上下文（固定币安永续，理由见文件头注释）"""
    try:
        conn = get_conn()
        row = conn.execute(
            """SELECT poc_price, delta, cumulative_delta, close
               FROM atas_bars
               WHERE exchange='binance' AND market_type='perp'
               ORDER BY id DESC LIMIT 1"""
        ).fetchone()
        conn.close()
        return dict(row) if row else {}
    except Exception:
        return {}


def register_new_signals():
    """把新产生的 Absorption 信号登记到追踪表"""
    try:
        conn = get_conn()
        now_str = datetime.now(SGT).strftime("%Y-%m-%d %H:%M:%S")

        # 找出还没有被追踪的新信号（过去2小时内，indicator=Absorption）
        cutoff = (datetime.now(SGT) - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
        signals = conn.execute("""
            SELECT s.id, s.timestamp
            FROM atas_signals s
            WHERE s.created_at >= ?
              AND s.indicator_name LIKE '%Absorption%'
              AND s.id NOT IN (SELECT signal_id FROM atas_signal_outcomes WHERE signal_id IS NOT NULL)
            ORDER BY s.id ASC
        """, (cutoff,)).fetchall()

        bar = get_latest_bar()
        price = get_current_price()

        for sig in signals:
            poc  = bar.get("poc_price")
            delt = bar.get("delta")
            cvd  = bar.get("cumulative_delta")
            tprice = price or bar.get("close")

            # POC 关系
            poc_rel = "unknown"
            if poc and tprice:
                diff_pct = (tprice - poc) / poc * 100
                if diff_pct > 0.1:
                    poc_rel = "above_poc"
                elif diff_pct < -0.1:
                    poc_rel = "below_poc"
                else:
                    poc_rel = "at_poc"

            check_4h  = (datetime.now(SGT) + timedelta(hours=4)).strftime("%Y-%m-%d %H:%M:%S")
            check_24h = (datetime.now(SGT) + timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")

            conn.execute("""
                INSERT INTO atas_signal_outcomes
                (signal_id, indicator, trigger_time, trigger_price,
                 trigger_poc, trigger_delta, trigger_cvd, poc_relation,
                 check_4h_at, check_24h_at, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, (
                sig["id"], "Absorption",
                sig["timestamp"], tprice,
                poc, delt, cvd, poc_rel,
                check_4h, check_24h, now_str
            ))
            log.info(f"[TRACKER] 新信号登记 id={sig['id']} price={tprice} poc_rel={poc_rel}")

        conn.commit()
        conn.close()
    except Exception as e:
        log.warning(f"[TRACKER] register error: {e}")


def check_outcomes():
    """
    回查已到期的信号，记录价格结果。每条信号用它自己 check_4h_at/
    check_24h_at 那一刻的历史价（get_price_at_time），不是"现在"的价格——
    2026-07-12 修复：原来整轮用同一个 get_current_price() 给所有到期信号
    结算，一旦有积压（本次就是12天、11万+条），全部会被错误地按"补跑
    那一刻"算涨跌，而不是各自真正到期的时刻，统计毫无意义。
    每500条提交一次，避免一次性处理大批量积压时长时间占着写锁，
    影响其它同时在写这个库的服务（main.py/signal_engine.py等）。
    """
    try:
        conn  = get_conn()
        now_s = datetime.now(SGT).strftime("%Y-%m-%d %H:%M:%S")

        due_4h = conn.execute("""
            SELECT id, trigger_price, check_4h_at FROM atas_signal_outcomes
            WHERE checked_4h=0 AND check_4h_at <= ?
        """, (now_s,)).fetchall()

        done = 0
        for row in due_4h:
            price = get_price_at_time(row["check_4h_at"])
            if price is None:
                continue
            chg = (price - row["trigger_price"]) / row["trigger_price"] * 100
            outcome = "up" if chg > 0.5 else ("down" if chg < -0.5 else "flat")
            conn.execute("""
                UPDATE atas_signal_outcomes
                SET price_4h=?, change_4h=?, outcome_4h=?, checked_4h=1
                WHERE id=?
            """, (price, round(chg, 3), outcome, row["id"]))
            done += 1
            if done % 500 == 0:
                conn.commit()
                log.info(f"[TRACKER] 4H结果补算中 {done}/{len(due_4h)}")
        conn.commit()
        if due_4h:
            log.info(f"[TRACKER] 4H结果本轮完成 {done}/{len(due_4h)} 条（跳过{len(due_4h)-done}条，历史价查不到）")

        due_24h = conn.execute("""
            SELECT id, trigger_price, check_24h_at FROM atas_signal_outcomes
            WHERE checked_24h=0 AND check_24h_at <= ?
        """, (now_s,)).fetchall()

        done = 0
        for row in due_24h:
            price = get_price_at_time(row["check_24h_at"])
            if price is None:
                continue
            chg = (price - row["trigger_price"]) / row["trigger_price"] * 100
            outcome = "up" if chg > 1.0 else ("down" if chg < -1.0 else "flat")
            conn.execute("""
                UPDATE atas_signal_outcomes
                SET price_24h=?, change_24h=?, outcome_24h=?, checked_24h=1
                WHERE id=?
            """, (price, round(chg, 3), outcome, row["id"]))
            done += 1
            if done % 500 == 0:
                conn.commit()
                log.info(f"[TRACKER] 24H结果补算中 {done}/{len(due_24h)}")
        conn.commit()
        if due_24h:
            log.info(f"[TRACKER] 24H结果本轮完成 {done}/{len(due_24h)} 条（跳过{len(due_24h)-done}条，历史价查不到）")

        conn.close()
    except Exception as e:
        log.warning(f"[TRACKER] check error: {e}")


def print_stats():
    """输出当前胜率统计（日志里可见）"""
    try:
        conn = get_conn()

        # 4H 胜率
        r4 = conn.execute("""
            SELECT outcome_4h, COUNT(*) as n
            FROM atas_signal_outcomes
            WHERE checked_4h=1
            GROUP BY outcome_4h
        """).fetchall()

        # 24H 胜率
        r24 = conn.execute("""
            SELECT outcome_24h, COUNT(*) as n
            FROM atas_signal_outcomes
            WHERE checked_24h=1
            GROUP BY outcome_24h
        """).fetchall()

        total4  = sum(r["n"] for r in r4)
        total24 = sum(r["n"] for r in r24)

        if total4 > 0:
            stats4 = {r["outcome_4h"]: r["n"] for r in r4}
            log.info(
                f"[STATS] 4H胜率({total4}样本) "
                f"up={stats4.get('up',0)} "
                f"down={stats4.get('down',0)} "
                f"flat={stats4.get('flat',0)}"
            )
        if total24 > 0:
            stats24 = {r["outcome_24h"]: r["n"] for r in r24}
            log.info(
                f"[STATS] 24H胜率({total24}样本) "
                f"up={stats24.get('up',0)} "
                f"down={stats24.get('down',0)} "
                f"flat={stats24.get('flat',0)}"
            )

        conn.close()
    except Exception as e:
        log.warning(f"[TRACKER] stats error: {e}")


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    log.info("[TRACKER] Phase 5A signal tracker started")

    cycle = 0
    while True:
        try:
            register_new_signals()
            check_outcomes()
            if cycle % 12 == 0:   # 每小时输出一次统计
                print_stats()
            cycle += 1
        except Exception as e:
            log.warning(f"[TRACKER] main loop error: {e}")
        time.sleep(300)   # 每5分钟运行一次


if __name__ == "__main__":
    main()
