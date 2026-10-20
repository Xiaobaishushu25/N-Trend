"""
Comprehensive backtest of high win-rate pattern events over the recent month.
Execution logic:
  - Entry: Close price of the trigger K-line (15m) in the signal direction (long for 'up', short for 'down').
  - Exit: Close price of the immediately following 15m K-line.
  - Position size: 1 lot (一手) per trade.
  - Metrics: Win Rate (胜率), Profit/Loss Ratio (盈亏比), Total Profit Amount (收益金额).
"""
import sys
import os
import sqlite3
import re
import pandas as pd
import numpy as np
from pathlib import Path

if sys.stdout.encoding != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

DB_PATH = Path("data/server_backup.sqlite")

multipliers = {
    'RB': 10, 'HC': 10, 'WR': 10, 'I': 100, 'J': 100, 'JM': 60, 'SF': 5, 'SM': 5, 'SS': 5,
    'CU': 5, 'AL': 5, 'ZN': 5, 'PB': 5, 'NI': 1, 'SN': 1, 'AO': 20, 'AU': 1000, 'AG': 15,
    'SC': 1000, 'FU': 10, 'LU': 10, 'BU': 10, 'PG': 20, 'RU': 10, 'BR': 5, 'NR': 10, 'SP': 10,
    'L': 5, 'PP': 5, 'V': 5, 'EB': 5, 'EG': 10, 'PF': 5, 'TA': 5, 'PX': 5, 'SH': 30, 'MA': 10,
    'FG': 20, 'SA': 20, 'UR': 20, 'M': 10, 'Y': 10, 'P': 10, 'RM': 10, 'OI': 10, 'SR': 10,
    'CF': 5, 'CY': 5, 'C': 10, 'CS': 10, 'JD': 5, 'LH': 16, 'AP': 10, 'CJ': 5, 'PK': 5,
    'SI': 5, 'LC': 1, 'EC': 50
}

tick_sizes = {
    'RB': 1.0, 'HC': 1.0, 'WR': 1.0, 'I': 0.5, 'J': 0.5, 'JM': 0.5, 'SF': 2.0, 'SM': 2.0, 'SS': 5.0,
    'CU': 10.0, 'AL': 5.0, 'ZN': 5.0, 'PB': 5.0, 'NI': 10.0, 'SN': 10.0, 'AO': 1.0, 'AU': 0.02, 'AG': 1.0,
    'SC': 0.1, 'FU': 1.0, 'LU': 1.0, 'BU': 1.0, 'PG': 1.0, 'RU': 5.0, 'BR': 5.0, 'NR': 5.0, 'SP': 2.0,
    'L': 1.0, 'PP': 1.0, 'V': 1.0, 'EB': 1.0, 'EG': 1.0, 'PF': 2.0, 'TA': 2.0, 'PX': 2.0, 'SH': 1.0, 'MA': 1.0,
    'FG': 1.0, 'SA': 1.0, 'UR': 1.0, 'M': 1.0, 'Y': 2.0, 'P': 2.0, 'RM': 1.0, 'OI': 1.0, 'SR': 1.0,
    'CF': 5.0, 'CY': 5.0, 'C': 1.0, 'CS': 1.0, 'JD': 1.0, 'LH': 5.0, 'AP': 1.0, 'CJ': 5.0, 'PK': 2.0,
    'SI': 5.0, 'LC': 50.0, 'EC': 0.1
}

def get_prefix(sym):
    m = re.match(r'^[A-Za-z]+', sym)
    return m.group(0).upper() if m else sym.upper()

def get_mult(sym):
    return multipliers.get(get_prefix(sym), 10.0)

def get_tick(sym):
    return tick_sizes.get(get_prefix(sym), 1.0)

def load_data():
    conn = sqlite3.connect(DB_PATH)
    query = """
    WITH sorted_klines AS (
        SELECT symbol, timeframe, ts, open, high, low, close,
               LEAD(open) OVER (PARTITION BY symbol, timeframe ORDER BY ts) as next_open,
               LEAD(close) OVER (PARTITION BY symbol, timeframe ORDER BY ts) as next_close,
               LEAD(ts) OVER (PARTITION BY symbol, timeframe ORDER BY ts) as next_ts
        FROM klines
        WHERE timeframe = '15m'
    )
    SELECT p.id, p.symbol, p.direction, p.grade, p.entry_score, p.trigger_ts, p.trigger_bar_ts, p.trigger_price,
           k.open as trigger_open, k.high as trigger_high, k.low as trigger_low, k.close as trigger_close,
           k.next_open, k.next_close, k.next_ts,
           m.p_win
    FROM pattern_events p
    JOIN sorted_klines k ON k.symbol = p.symbol AND k.ts = p.trigger_bar_ts
    LEFT JOIN v2_model_predictions m ON m.event_id = p.id AND m.model_id = 'logistic-v1-2972140d'
    WHERE p.trigger_ts >= '2026-09-20'
    ORDER BY p.trigger_ts ASC
    """
    df = pd.read_sql_query(query, conn)
    df['multiplier'] = df['symbol'].apply(get_mult)
    df['tick'] = df['symbol'].apply(get_tick)
    
    # 策略执行：收盘价按照方向买入（多头up买入，空头down卖空），下一根K线收盘平仓
    # 多头点数差 = next_close - trigger_close
    # 空头点数差 = trigger_close - next_close
    df['points_diff'] = np.where(df['direction'] == 'up',
                                 df['next_close'] - df['trigger_close'],
                                 df['trigger_close'] - df['next_close'])
    
    # 毛收益金额（一手）
    df['pnl_gross'] = df['points_diff'] * df['multiplier']
    df['is_win'] = df['pnl_gross'] > 0
    df['is_loss'] = df['pnl_gross'] < 0
    df['is_flat'] = df['pnl_gross'] == 0
    
    # 交易成本评估（双边手续费估计：一手开平按 6元 计算；1跳滑点双边成本 = 2 * tick * multiplier）
    fee_per_trade = 6.0
    slippage_1tick = 2.0 * df['tick'] * df['multiplier']
    df['pnl_net_fee_only'] = df['pnl_gross'] - fee_per_trade
    df['pnl_net_with_slippage'] = df['pnl_gross'] - fee_per_trade - slippage_1tick
    
    # 备用对比1：下一根开盘价成交（Next Open）
    df['points_diff_next_open'] = np.where(df['direction'] == 'up',
                                           df['next_close'] - df['next_open'],
                                           df['next_open'] - df['next_close'])
    df['pnl_next_open'] = df['points_diff_next_open'] * df['multiplier']
    
    # 备用对比2：触发瞬间价格成交（Trigger Price）
    df['points_diff_trig_price'] = np.where(df['direction'] == 'up',
                                            df['next_close'] - df['trigger_price'],
                                            df['trigger_price'] - df['next_close'])
    df['pnl_trig_price'] = df['points_diff_trig_price'] * df['multiplier']
    
    # 时间维度衍生
    df['trigger_dt'] = pd.to_datetime(df['trigger_ts'])
    df['date'] = df['trigger_dt'].dt.date
    df['week'] = df['trigger_dt'].dt.isocalendar().week
    
    return df

def calc_stats(sub, label=""):
    n = len(sub)
    if n == 0:
        return {'label': label, 'count': 0}
    wins = int(sub['is_win'].sum())
    losses = int(sub['is_loss'].sum())
    flats = int(sub['is_flat'].sum())
    win_rate = wins / n
    win_rate_no_flat = wins / (wins + losses) if (wins + losses) > 0 else 0.0
    total_gross = float(sub['pnl_gross'].sum())
    gross_win = float(sub[sub['is_win']]['pnl_gross'].sum())
    gross_loss = float(abs(sub[sub['is_loss']]['pnl_gross'].sum()))
    profit_factor = gross_win / gross_loss if gross_loss > 0 else float('inf')
    avg_win = float(sub[sub['is_win']]['pnl_gross'].mean()) if wins > 0 else 0.0
    avg_loss = float(abs(sub[sub['is_loss']]['pnl_gross'].mean())) if losses > 0 else 0.0
    avg_win_loss_ratio = avg_win / avg_loss if avg_loss > 0 else float('inf')
    avg_pnl = float(sub['pnl_gross'].mean())
    max_win = float(sub['pnl_gross'].max()) if n > 0 else 0.0
    max_loss = float(sub['pnl_gross'].min()) if n > 0 else 0.0
    
    # 累计收益与回撤
    cum_pnl = sub['pnl_gross'].cumsum()
    running_max = cum_pnl.cummax()
    drawdown = running_max - cum_pnl
    max_dd = float(drawdown.max())
    
    # 连胜与连亏
    win_seq = (sub['is_win'] != sub['is_win'].shift()).cumsum()
    max_consec_wins = int(sub[sub['is_win']].groupby(win_seq).size().max()) if wins > 0 else 0
    loss_seq = (sub['is_loss'] != sub['is_loss'].shift()).cumsum()
    max_consec_losses = int(sub[sub['is_loss']].groupby(loss_seq).size().max()) if losses > 0 else 0
    
    total_net_fee = float(sub['pnl_net_fee_only'].sum())
    total_net_slip = float(sub['pnl_net_with_slippage'].sum())

    return {
        'label': label,
        'count': n,
        'wins': wins,
        'losses': losses,
        'flats': flats,
        'win_rate': win_rate,
        'win_rate_no_flat': win_rate_no_flat,
        'total_gross': total_gross,
        'total_net_fee': total_net_fee,
        'total_net_slip': total_net_slip,
        'gross_win': gross_win,
        'gross_loss': gross_loss,
        'profit_factor': profit_factor,
        'avg_win': avg_win,
        'avg_loss': avg_loss,
        'avg_win_loss_ratio': avg_win_loss_ratio,
        'avg_pnl': avg_pnl,
        'max_win': max_win,
        'max_loss': max_loss,
        'max_dd': max_dd,
        'max_consec_wins': max_consec_wins,
        'max_consec_losses': max_consec_losses
    }

def print_section(m):
    print(f"==================================================")
    print(f"【{m['label']}】")
    print(f"--------------------------------------------------")
    print(f"  总交易笔数: {m['count']} 笔 (盈利: {m['wins']} 笔, 亏损: {m['losses']} 笔, 平局: {m['flats']} 笔)")
    print(f"  ★ 策略胜率 (全量笔数): {m['win_rate']*100:.2f}%")
    print(f"  ★ 策略胜率 (剔除平局): {m['win_rate_no_flat']*100:.2f}%")
    print(f"  ★ 总盈亏比 (Profit Factor 获利倍数): {m['profit_factor']:.2f}")
    print(f"  ★ 单笔平均盈亏比 (Avg Win / Avg Loss): {m['avg_win_loss_ratio']:.2f}")
    print(f"  ★ 毛收益总金额 (一手算): {m['total_gross']:+,.1f} 元")
    print(f"  - 扣除交易所基础手续费(单笔6元)后净额: {m['total_net_fee']:+,.1f} 元")
    print(f"  - 扣除手续费 + 双边各1跳滑点压力测试后净额: {m['total_net_slip']:+,.1f} 元")
    print(f"  单笔平均毛利: {m['avg_pnl']:+,.1f} 元/笔")
    print(f"  累计盈利额: +{m['gross_win']:,.1f} 元 (平均单笔盈利: +{m['avg_win']:,.1f} 元)")
    print(f"  累计亏损额: -{m['gross_loss']:,.1f} 元 (平均单笔亏损: -{m['avg_loss']:,.1f} 元)")
    print(f"  单笔最大盈利: +{m['max_win']:,.1f} 元 | 单笔最大亏损: -{abs(m['max_loss']):,.1f} 元")
    print(f"  期间最大回撤 (一手累计): {m['max_dd']:,.1f} 元")
    print(f"  最大连胜笔数: {m['max_consec_wins']} 笔 | 最大连亏笔数: {m['max_consec_losses']} 笔")
    print()

def main():
    df = load_data()
    print("==================================================")
    print(f"回测区间: {df['trigger_ts'].min()} 至 {df['trigger_ts'].max()} (近一个月完整交易日)")
    print(f"总计匹配K线并触发形态笔数: {len(df)} 笔")
    print("==================================================\n")
    
    # 1. 核心高胜率形态 (p_win >= 0.5)
    hw = df[df['p_win'] >= 0.5]
    print_section(calc_stats(hw, "系统高胜率形态总览 (p_win >= 0.50)"))
    
    # 2. 多空拆分
    print_section(calc_stats(hw[hw['direction'] == 'up'], "高胜率形态 - 多头 (Long)"))
    print_section(calc_stats(hw[hw['direction'] == 'down'], "高胜率形态 - 空头 (Short)"))
    
    # 3. 概率梯度对比
    print_section(calc_stats(df[df['p_win'] >= 0.55], "高胜率形态增强档 (p_win >= 0.55)"))
    print_section(calc_stats(df[df['p_win'] >= 0.60], "高胜率形态极高档 (p_win >= 0.60)"))
    
    # 4. 全量基准与对照
    print_section(calc_stats(df[df['p_win'] < 0.5], "对照组：普通/未达标形态 (p_win < 0.50)"))
    print_section(calc_stats(df, "全市场基准组：全部触发形态 (共521笔)"))
    
    # 5. 周度统计表现
    print("==================================================")
    print("【高胜率形态 (p_win >= 0.5) 按自然周表现统计】")
    print("--------------------------------------------------")
    week_stats = []
    for w, g in hw.groupby('week'):
        dt_start = g['trigger_ts'].min()[:10]
        dt_end = g['trigger_ts'].max()[:10]
        s = calc_stats(g, f"第{w}周 ({dt_start}~{dt_end})")
        week_stats.append({
            '周别与日期': s['label'],
            '交易笔数': s['count'],
            '胜笔': s['wins'],
            '负笔': s['losses'],
            '平笔': s['flats'],
            '胜率': f"{s['win_rate']*100:.1f}%",
            '盈亏比': f"{s['profit_factor']:.2f}",
            '收益金额': f"{s['total_gross']:+,.0f} 元",
            '扣费后': f"{s['total_net_fee']:+,.0f} 元"
        })
    print(pd.DataFrame(week_stats).to_string(index=False))
    print()

    # 6. 品种表现全景榜
    print("==================================================")
    print("【高胜率形态 (p_win >= 0.5) 分品种盈亏全景 (共22个品种)】")
    print("--------------------------------------------------")
    sym_rows = []
    for sym, g in hw.groupby('symbol'):
        s = calc_stats(g, sym)
        sym_rows.append({
            '代码': sym,
            '笔数': s['count'],
            '胜': s['wins'],
            '负': s['losses'],
            '平': s['flats'],
            '胜率': f"{s['win_rate']*100:.1f}%",
            '盈亏比': f"{s['profit_factor']:.2f}" if s['losses']>0 else '99.9',
            '毛收益(元)': s['total_gross'],
            '扣费净额(元)': s['total_net_fee'],
            '单笔均利(元)': f"{s['avg_pnl']:+,.1f}"
        })
    sym_df = pd.DataFrame(sym_rows).sort_values('毛收益(元)', ascending=False)
    print(sym_df.to_string(index=False))
    print()

if __name__ == '__main__':
    main()
