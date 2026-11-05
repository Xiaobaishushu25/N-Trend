"""Render all R1 cohorts; no candidate rankings or downstream strategy search."""
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import r1_diagnostics as r


def table(df, cols):
    def fmt(x):
        if r.pd.isna(x):return '—'
        if isinstance(x,(float,r.np.floating)):return f'{x:.4f}'
        return str(x)
    return '| '+' | '.join(cols)+' |\n| '+' | '.join(['---']*len(cols))+' |\n'+ '\n'.join(
        '| '+' | '.join(fmt(row[c]) for c in cols)+' |' for row in df.to_dict('records'))+'\n'


def main():
    out=r.BASE/'diagnostics'
    family=r.pd.read_csv(out/'R1_family_overview.csv')
    funnel=r.pd.read_csv(out/'R1_funnel.csv')
    state=r.pd.read_csv(out/'R1_regime_scale_audit.csv')
    market=r.pd.read_csv(out/'R1_market_cost_map.csv')
    fe=r.pd.read_parquet(out/'R1_funnel_event_audit.parquet')
    reasons=r.pd.read_csv(out/'R1_funnel_reasons.csv')
    recon=json.loads((out/'Q_C01_reconciliation.json').read_text(encoding='utf8'))
    seal=json.loads((r.BASE/'R1_freeze_manifest.json').read_text(encoding='utf8'))
    # Predefined descriptive summaries, all periods, no sorting by profitability.
    state_summary=state.groupby(['period','feature']).agg(slot_product_cells=('bars','size'),
        coverage_mean=('coverage','mean'),median_abs_q99=('q99',lambda x:x.abs().median()),
        missing_fraction_mean=('missing_fraction','mean'),peers_mean=('effective_peer_count_mean','mean')).reset_index()
    slot=state.groupby(['period','feature','minute']).agg(coverage=('coverage','mean'),q90=('q90','median'),q99=('q99','median')).reset_index()
    slot.to_csv(out/'R1_regime_slot_overview.csv',index=False)
    # Supplement: paired fixed-H outcomes for the base mechanism; doesn't change any rule.
    base_summary=r.summarize_events(fe.assign(signal_group='BASE12',horizon=12),['signal_group','period','horizon'])
    base_summary.to_csv(out/'R1_base_economics.csv',index=False)
    md='# R1 止损诊断与研究重启：第一轮结果\n\n'
    md+='本轮只完成 R1-0 与 R1-1 的 T1/T2/T3，停止于诊断。没有新增策略模板，没有进入 R1-2/3，没有读取 2025-11-01 起的策略收益。D1/D2 都是已暴露开发区间，本报告不是独立样本外检验、WFO 或实盘收益证明。\n\n'
    md+='**建议：暂停现有 5m 假突破/多条件机制的扩展；下一轮仅考虑路线 A 的有限可行性研究，不采用路线 B 直接简化后晋级。** 基础形态 D1 日期等权每次毛位移 13.26 元、摩擦 35.03 元，净 -21.76 元；D2 毛位移 -7.81 元、摩擦 35.15 元，净 -42.96 元。过滤确实减少机会，却没有替基础全集创造稳定毛优势。路线 A 只是后续研究方向，尚未证明盈利，也未在本轮执行。\n\n'
    md+='## T1：经济可行性\n\n'
    md+='下表覆盖全部既有信号族和四个期限，先按族内品种/时间/方向去重；V1 重叠发现窗去重，避免重复窗口夸大机会数。不同族之间仍可能共享机会。`gross` 由下一实际开盘未加滑点的参考价开始；`cost` 包含双边手续费和每边 1 tick，`net=gross-cost`。与旧报告单边滑点已计入 entry 的 gross 定义不同，不直接拼接。\n\n'
    md+=table(family,['signal_group','period','horizon','event_count','complete_events','complete_days','censored_fraction','gross_R_date_equal','cost_R_date_equal','net_R_date_equal','net_2tick_yuan_date_equal'])
    md+='\n`R` 始终为统一重建的信号时 ATR5×乘数；ATR30/60 只是波动尺度，未充当可捕获收益。表 A 主表另按品种给出所有格子，候选表保留所有原模板，时段年份表保留全部时段。没有删除亏损或样本不足的格子。\n\n'
    md+='MFE/MAE 从未加滑点的开盘参考价计算，属于事后路径边界，不能按 MFE 理想平仓。H=6/12/24/36 均按可交易 bar 数计数，允许日盘休息间隔，但不跨日、不跨合约/缺口/无效 bar；不完整路径没有部分收益。高期限完整样本减少具有日内时段选择效应。\n\n'
    md+='市场成本地图逐品种×D1/D2×年份×5m 时刻给出 tick 元值、名义金额、开仓/平今/平昨费用、1/2 tick 成本、ATR5/30/60 分位和缺失率。高周期仅用完整时钟桶，12 个桶预热，不填缺失。每个同一时刻一般每天一条，因此分位是跨日分布；全体合并不是品种等权。\n\n'
    md+='盘口点差与历史保证金均 unknown；BU/JD/SH 标为规格冲突/覆盖未核验，其他规格也只是现有表代理。费用是 2026-10-22 固定交易所快照，不含期货公司加收，不声称还原历史费率。当前只研究日盘，标签一律使用平今；平昨费率仅并列披露。`stop_budget_pass` 是信号时费用下的计划风险诊断，下一开盘跳空仍可能不合格；`cost_to_stop` 在逐事件路径表中，不是风险认证。\n\n'
    md+='## T2：机会漏斗\n\n基础全集为固定 12 根区间、0.25 前 ATR 突破、收回区间且反向实体/收盘的形态；先不施加经济/活跃度、低 ER/真正平静和冷却。不能构成前一完整 bar、同日连续信号的情况不属于基础事件，指标缺失不能凭空造形态。\n\n'
    md+=table(base_summary,['period','event_count','complete_events','complete_days','gross_yuan','cost_yuan','net_yuan','gross_yuan_date_equal','cost_yuan_date_equal','net_yuan_date_equal'])
    md+='\n固定五步并行报告 400/600 两种预算，不择优。表中 `retained` 为累计留存；CSV 同时包含当步剔除和累计剔除。各行均保持同一原始事件 ID，收益比较使用统一 H12 标签。补充结构收益复用原 V4 固定目标、止损、12 根/日终退出；未新增退出规则。预算漏斗按信号时可计划风险，实际下一开盘的预算重检与目标成本检查仍在结构撮合内部。\n\n'
    md+=table(funnel.loc[funnel.cohort=='retained'],['period','budget','step','condition','event_count','unique_days','complete_events','gross_yuan_date_equal','cost_yuan_date_equal','net_yuan_date_equal','structural_trade_count','structural_net_yuan'])
    md+='\nQ-C01 是形态收盘立即确认；没有“再等待一根”的额外确认条件。最后一步恢复原 24 个活跃 bar 冷却，按原有所有警报计算，不能因为预算拒绝就重新触发。完整分支的结构交易数量和现金总额与原 V4 一致：\n\n'
    md+=table(r.pd.DataFrame(recon),['period','complete_trades','archive_complete_trades','net_sum_yuan','exact_aggregate_match'])
    md+='\n各失败原因非互斥，不能相加当作总剔除数：\n\n'+table(reasons,['period','reason','events','unique_days'])
    md+='\nS1 69.5% 未分类没有被当作 V4 的交易门槛：S1 market_state 并不进入该信号函数。另输出 R1_s1_classification_overlap.csv，给出本次基础事件与 S1 分类的重叠，不能把重叠的未分类数称为实际被 S1 排除的信号。\n\n'
    md+='各层样本构成、日期、时段、品种均随过滤改变，均值变化不是过滤条件的因果效应。400/600 仅执行诊断，未据此放宽或选择风险预算。没有同时运行账户资金分配，结构事件之间可重叠。\n\n'
    md+='## T3：无收益状态尺度审计\n\n原始同组累计位移除以当时 ATR5，与一个固定过去 30 分钟的方向尺度对比。候选仅作特征审计，不设新状态阈值，不关联哪个状态赚钱。其分母为 6 个完整 5m true range 的平方和平方根，分子为这 30 分钟的 close-open；窗口必须墙钟连续，不能跨午休或小节休息。true range 依赖已知前收盘，并非统计意义的收益标准差。\n\n'
    md+=table(state_summary,list(state_summary.columns))
    md+='\n上表为品种×时刻单元的等权摘要，精确分布见逐单元 CSV；不同特征数值没有统一概率尺度，不能仅凭绝对值较小断言候选更有效。原状态从 09:00 起累计，观察期限随时段变化是定义上确定的；30m 候选的缺失包括不足 6 根、休息边界、对手品种不足两只。两者均剔除目标品种，仅精确相同完成时间，不前填。C/JD/PB 在 D1/D2 各做一次从 SQL 重读的实际前缀重算，共六组通过。前缀检查涉及原/新本地与同组特征及 ATR30/60。\n\n'
    md+='可预注册的修复方向是固定观察长度与对应波动尺度，并明确休息后预热和最低有效品种数；本轮没有把候选阈值应用到信号或测试新策略收益。\n\n'
    md+='## 决策与边界\n\n'
    md+='路线判断应结合上表毛空间/成本、完整日期数与漏斗留存率。当前结果只支持诊断建议；不能因为某行净值较高选它作为新策略。30m/60m ATR 大于成本仅说明幅度，不证明更大周期有可捕获方向优势。若建议路线 A，也只是下一轮先验证机会空间，尚未启动两个新规则或使用保留期。\n\n'
    md+='具体证据结论另见 R1_verdict.md；本轮结束，不自动进入搜索。当前没有可部署策略。\n\n'
    md+='## 复验与来源\n\n'
    md+=f"git HEAD：`{seal['git_sha']}`；SQLite SHA256：`{seal['source_sha256']}`。\n\n"
    md+='冻结清单包含旧冻结文件、代码逐字节快照、未提交工作树记录和历史 verify 日志；未自动 git 提交其他工作。合成测试浮点精确比较修正为近似比较；首次运行的 V1 文件名错误在读取真实结果前修复；随后 Pandas product 方法与列名冲突使事件连接为空，在生成收益汇总前改为显式列索引。初始清单、修订记录和失败产物均保留。没有为改善收益修改规则。\n\n'
    md+='继承的 V4 state_at 在非有限同组数值相加时仍有 RuntimeWarning，沿用冻结规则拒绝这些状态；未修改旧函数。描述性缺失统计对非有限值留空，未填成零。运行过程记录见 R1_runtime_notes.md。\n\n'
    md+='`python -m unittest discover -s tests -p test_r1_diagnostics.py -v`\n\n`python scripts/r1_diagnostics.py verify`\n\n不要再次执行 freeze/run：已有目录会拒绝覆盖；重复历史计算不增加独立证据。报告脚本仅读取本轮产物，不计算新策略。全部数值均来自已暴露 D1/D2；没有匹配子集日期等权新估计，其字段明确为 not_computed。\n'
    (out/'R1_diagnostic_report.md').write_text(md,encoding='utf8')
    # Register this diagnostic feedback separately from the 256 actual strategy/window evaluations.
    exp=[]
    for task in ['T1_cost_paths','T2_funnel','T3_feature_only']:
        for period,start,end in r.PERIODS:
            exp.append(dict(version='R1',candidate_id='none_diagnostic',period=period,start=start,end_exclusive=end,
                exposure_type=task,result_seen=task!='T3_feature_only',classification='EXPOSED_DEVELOPMENT',
                reason='User requested R1 first round; fixed diagnostics, zero new strategy templates; no promotion',source='artifacts/r1/diagnostics/R1_diagnostic_report.md'))
    e=r.pd.DataFrame(exp);e.to_csv(out/'evaluation_exposures.csv',index=False)
    registry=r.ROOT/'registry/evaluation_exposures.csv';old=r.pd.read_csv(registry)
    if not (old.version=='R1').any():r.pd.concat([old,e],ignore_index=True).to_csv(registry,index=False)
    print('R1 report generated')


if __name__=='__main__':main()
