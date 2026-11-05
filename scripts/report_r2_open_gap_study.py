"""Report the preregistered gap mechanism study, including unsupported cells."""
import json
import shutil
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import r2_open_gap_study as r
from report_r1_diagnostics import table


def main():
    out=r.BASE/'development'
    q=r.pd.read_csv(out/'mechanism_comparison.csv');coverage=r.pd.read_csv(out/'coverage.csv')
    events=r.pd.read_parquet(out/'events.parquet')
    ver=json.loads((out/'verification.json').read_text(encoding='utf8'))
    freeze=json.loads((r.BASE/'freeze.json').read_text(encoding='utf8'))
    decisions=r.pd.DataFrame(ver['decisions']);passed=decisions.loc[decisions.passed_both_periods,'mechanism'].tolist()
    main=q.loc[q.primary].copy()
    md='# R2：日盘开盘跳空的延续与回补机制研究\n\n'
    if passed:
        md+=f'**机制筛查通过：{passed}。只能据此另行预注册可执行假设，尚无资金策略或保留期验收。**\n\n'
    else:
        md+='**延续与回补都未满足跨 D1/D2 的预注册证据门槛。当前没有依据把本轮机制推进为新策略。保留期仍未读取。**\n\n'
    md+='这是继 R1 日内方向结构失败后，另立的市场机制问题。只使用 D1=2023-11-01至2024-11-01、D2=2024-11-01至2025-11-01（结束不含），两段均为多轮已暴露开发历史。新结果不具有独立样本外身份。\n\n'
    md+='## 事前定义与搜索预算\n\n'
    md+='跳空=当日09:00开盘−前一已观察交易日15:00收盘；要求前一日盘45根完整有效bar、同真实合约/连续历史段、前日振幅>0，且不能跳过其他品种共同观察到的交易日。大跳空定义固定为绝对跳空≥前日盘振幅50%，不按收益调整。\n\n'
    md+='最早在09:00–09:05完整bar结束时登记，参考入场为下一实际09:05 bar开盘，扣每边1tick和双边适用交易所费用；2tick另算。分别观察同跳空方向延续和反向回补，H=6/12/24个活跃5m bar，对应30/60/120活跃分钟，H12是唯一主期限。休息时间不计活跃分钟。\n\n'
    md+='对有夜盘品种，15:00→次日09:00的变化包含夜盘交易；它不是纯无交易隔夜新闻效应，也不是结算价收益。没有持有隔夜仓位。品种夜盘属性采用既有静态时段配置代理。\n\n'
    md+='预算固定为2个方向探针×3期限×2时期，共12个机制结果单元；4个主期限方向×时期检验一起做Holm修正。累计82个已有策略模板/260次策略×窗口评估保留，本轮12个新结果单元单独计入反馈史，没有借“事件研究”重置搜索史或称没有新增收益反馈。未生成可执行策略模板。\n\n'
    md+='## 主期限结果\n\n'+table(main,['period','mechanism','event_count','complete_events','active_dates','gross_yuan_date_equal','cost_yuan_date_equal','net_yuan_date_equal','net_ci_lower','net_ci_upper','net_2tick_yuan_date_equal'])
    md+='\n单元是“完整事件普通均值”与“每日期内平均、再日期等权”的人民币方向位移。既非账户日收益，也没有止损/止盈成交。H12主检验并列报告全部样本净值与匹配子集：\n\n'
    md+=table(main,['period','mechanism','matched_events','matched_fraction','matched_signal_net_date_equal','matched_effect','effect_ci_lower','effect_ci_upper','effect_p','holm_primary_p'])
    md+='\n机械对照只使用该事件之前60个已观察交易日，低跳空绝对值<其前日振幅10%，其前日振幅/事件前日振幅∈[.67,1.5]，同品种同09:05参考时刻，并复制事件方向。选最近符合特征的一日；不检查对照收益是否完整来回补匹配。没有对照时留缺失，不填零。对照可重复使用，control_reuse.csv披露每个源日期的复用次数。\n\n'
    md+='置信区间采用5日移动块、1000次联合来源日期抽样；事件和重复对照贡献各自归回原始日期，使用冻结日期权重。不能将匹配对数视为独立样本数，也不能从观察性对照推断因果。较早事件缺乏足够过去控制日期会降低覆盖，未事后放宽。\n\n'
    md+='## 全部期限结果\n\n'+table(q,['period','mechanism','horizon','complete_events','gross_yuan','cost_yuan','net_yuan','net_yuan_date_equal','net_2tick_yuan_date_equal','matched_fraction'])
    md+='\n上表完整保留两种方向、两个时期和三个期限，不只呈现正格子。额外期限是描述性敏感性结果，不能从中挑最优替代H12。逐品种CSV全部保留，零大跳空品种可由coverage.csv识别，未事后剔除。\n\n'
    md+='## 机会覆盖与可交易性边界\n\n'+table(coverage,['period','product','observed_days','usable_day_pairs','large_gap_events','low_gap_control_dates','night_session_proxy'])
    md+='\n以第一根5m极值外1tick作为事前技术止损参考，输出400/600元一手计划风险可容纳比例，只作几何诊断；主样本没有根据预算或未来是否触及该止损过滤：\n\n'
    md+=table(main,['period','mechanism','forecast_budget400_fraction','forecast_budget600_fraction','forecast_stop_touch_fraction','calendar_known_fraction'])
    md+='\n固定期限路径可能先穿过参考止损再反弹；上表因此不能写成有止损策略收益，也不能宣称实际单笔亏损已被400/600元限制。没有共享账户、保证金、同时在险900元或20%最大回撤认证。\n\n'
    md+='## 决策\n\n'+table(decisions,list(decisions.columns))
    md+='\n门槛固定要求同一方向在D1/D2主期限均达到：≥100完整事件、≥60活跃日期、对照覆盖≥80%、自身净日期均值及其95%下界>0、2tick仍正、匹配优势95%下界>0且主检验Holm p≤.05。只有通过才值得另写一个交易假设；本轮不是部署或资金验收。\n\n'
    if not passed:
        md+='本轮不追加25%/75%跳空门槛、不改成其他开盘分钟、不按盈利品种组重选、不把次期限重新指定为主期限。该结论限于本次明确的开盘跳空定义及观察方式，不能推出任何跳空交易都必然无效。\n\n'
    md+='## 来源与复验\n\n'
    md+=f"git HEAD `{freeze['git_sha']}`；主库 SHA256 `{freeze['source_sha256']}`。源码、输入和规则已在收益产生前复制封存，见freeze.json与snapshot目录。\n\n"
    md+='费用仍为2026-10-22固定交易所快照，非逐月历史核验；BU/JD/SH规格来源、历史日历/保证金、主力映射可得性及盘口执行缺口继续保留。本轮保留原日历已知标记，未编造早期官方交易日。\n\n'
    md+='六项合成回归及C/JD/PB×D1/D2六组真实SQL前缀与历史对照一致性检查已通过。只读复验：`python scripts/r2_open_gap_study.py verify`；合成回归：`python -m unittest discover -s tests -p test_r2_open_gap_study.py -v`。不要重复run增加所谓独立证据；已有输出拒绝覆盖。\n'
    (out/'R2_open_gap_report.md').write_text(md,encoding='utf8')
    shutil.copy2(Path(__file__),out/'report_source.py.txt')
    print('R2 report generated')


if __name__=='__main__':main()
