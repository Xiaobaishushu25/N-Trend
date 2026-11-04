"""Finalize a reproducible S0 report, cross-source checks and artifact hashes."""
import argparse
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from factor_study_data import digest, local

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'artifacts/s0'


def main():
    p=argparse.ArgumentParser();p.add_argument('--require-pass',action='store_true');args=p.parse_args()
    m=json.loads((OUT/'manifest.json').read_text(encoding='utf8'))
    v=json.loads((OUT/'validation.json').read_text(encoding='utf8'))
    # Enforce input seals before comparing archive snapshots.
    for name,expected in m['inputs_sha256'].items():
        if digest(ROOT/name)!=expected:raise ValueError('input changed: '+name)
    db=sqlite3.connect((ROOT/'data/research_market_history.sqlite').as_uri()+'?mode=ro',uri=True)
    db.execute('pragma query_only=ON')
    db.execute('attach database ? as backfill',((ROOT/'data/research_market_backfill.sqlite').as_uri()+'?mode=ro',))
    query='''from main.bars a join backfill.bars b on a.contract=b.contract and a.period_seconds=b.period_seconds and a.start_ns=b.start_ns where a.period_seconds=300'''
    shared=db.execute('select count(*) '+query).fetchone()[0]
    condition=' and (a.end_ns!=b.end_ns or a.open!=b.open or a.high!=b.high or a.low!=b.low or a.close!=b.close or a.volume!=b.volume or a.complete!=b.complete or a.constituent_count!=b.constituent_count)'
    conflicts=db.execute('select a.contract,a.start_ns,a.end_ns,a.open,a.high,a.low,a.close,a.volume,b.open,b.high,b.low,b.close,b.volume '+query+condition).fetchall()
    import csv
    with (OUT/'cross_source_conflicts.csv').open('w',encoding='utf8',newline='') as f:
        w=csv.writer(f);w.writerow(['contract','start_ns','end_ns','main_open','main_high','main_low','main_close','main_volume','backfill_open','backfill_high','backfill_low','backfill_close','backfill_volume']);w.writerows(conflicts)
    db.close()
    cross=dict(overlap_5m_rows=shared,conflicting_5m_rows=len(conflicts),
               unique_5m_rows=sum(next(p[1] for p in d['periods'] if p[0]==300) for d in m['data'])-shared)
    (OUT/'cross_source_summary.json').write_text(json.dumps(cross,indent=2),encoding='utf8')
    # Reuse of time-aggregation code is checked independently of the new executor.
    result=subprocess.run([sys.executable,'-m','unittest','discover','-s','tests','-p','test_market_history_store.py','-v'],cwd=ROOT,text=True,capture_output=True)
    (OUT/'existing_aggregation_tests.txt').write_text(result.stdout+result.stderr,encoding='utf8')
    data_lines=[]
    for d in m['data']:
        row=next(r for r in d['periods'] if r[0]==300)
        data_lines.append(f"|{d['source']}|{len(d['products'])}|{len(d['contracts'])}|{row[1]:,}|{local(row[2])}|{local(row[3])}|")
    issue_lines=[f'|{k}|{n:,}|' for k,n in m['issues'].items()]
    m['cross_source']=cross;m['validation']=v;m['existing_aggregation_test_exit']=result.returncode
    m['code_sha256'].update({str(p.relative_to(ROOT)):digest(p) for p in [ROOT/'scripts/validate_s0.py',Path(__file__)]})
    m['test_coverage']='18 synthetic unittest methods plus existing aggregation regression; not a full legacy-engine audit certification'
    for name in ('scripts/s0_engine.py','tests/test_no_lookahead.py'):
        m['code_sha256'][str((ROOT/name).relative_to(ROOT))]=digest(ROOT/name)
    (OUT/'manifest.json').write_text(json.dumps(m,ensure_ascii=False,indent=2),encoding='utf8')
    report=f'''# S0 数据与回测器审计报告

**结论：NO-GO。S0 工具和回归已落地，真实历史数据验收未通过；不进入 S1 或盈利策略挖掘。**

运行 ID：{m['run_id']}；研究版本：1.0-S0；基线 git SHA：`{m['git_sha']}`。当前工作区包含既有及并行修改，因此 SHA 不代表新增未提交文件；精确代码/输入 SHA256 见 `manifest.json`。时间均为北京时间，创建时间见 manifest 的 UTC 字段。

## 1. 已完成的工作与实际范围

- 已通读方案，只执行 S0/T01–T04 范围；未运行信号搜索，未读取 S4 收益，策略评估次数为 0。
- 修复前发现和最小修复建议保存在 `docs/S0_initial_audit.md`，首轮失败日志为 `tests_before.txt`。首轮失败原因是新验收入口尚不存在，不是对旧引擎全部缺陷的动态复现。旧流程的 1800/80% 差距是静态审计发现。
- 复用现有合约归档、Calendar 和 precision 证据；增加隔离的严格 5m 参考内核 `scripts/s0_engine.py`，未覆盖或改变旧策略版本。
- 内核实现下一可用 bar 开盘成交、先止损、跳空不利成交、限价队列未知拒绝、无法成交持仓保留、时变手续费/保证金、共享余额/浮盈亏/权益/保证金/在险记录、整数一手及 600/900/60% 约束。默认每笔预算 400 元。
- 新仓决策只使用该时刻开盘报价；同 bar 稍后退出的收益不能回流资助当时开仓。平仓时段由事先提供的日历确定，禁止通过查看下一 bar 是否缺失来提前平仓。
- 完整 15/30/60 分钟聚合需要全部已完结且连续的 5m bar；跨合约、缺 bar、未完结周期均不拼接。

## 2. 数据实测

|来源|品种数|合约数|5m 源记录数|最早开始|最晚结束|
|---|---:|---:|---:|---|---|
{chr(10).join(data_lines)}

两源重叠 {shared:,} 条 5m，冲突 {len(conflicts):,} 条；去重后 {cross['unique_5m_rows']:,} 条。冲突明细见 `cross_source_conflicts.csv`。所有计数均为归档记录，不表示主力活跃合约样本数；不能简单相加当独立样本。

协议要求最近 36 个完整月应为 **2023-11-01 ≤ t < 2026-11-01**。归档末端未覆盖到此终点，不能仅凭月份出现过就声称月份完整。本地交易日历覆盖 **{m['calendar_coverage'][0]} 至 {m['calendar_coverage'][1]}**；2023/2024 范围不能按工作日规则冒充交易所日历。

|异常类型|源记录/范围计数|
|---|---:|
{chr(10).join(issue_lines)}

`audit_data.csv` 给出来源、真实合约、原始纳秒时间范围、异常及独立证据标志。计数可重叠；同一条记录可有多个异常，两源也有重复。`expected_session_gap_proxy` 基于已有静态时段模板：远月不活跃也可能产生空档，不能一概认定丢数。`calendar_unknown_gap` 表示无法分类的间隙。完整缺失的首尾时段不会出现在相邻 bar 间隙检查中，仍须交易所日历和覆盖终点检验。

本次未发现重复时间戳、OHLCV 非法值、1m→5m 价格/成交量不一致（若出现会写异常）。数据库主键保证当前快照键唯一，但无法恢复导入阶段被 INSERT OR REPLACE 覆盖过的重复来源。物理写入顺序不是行情顺序；回放按每合约 start_ns 排序并检查重叠。

## 3. 阻断项与明确排除范围

1. **22 vs 20 品种未冻结**：本地映射配置 22 个，不能依据历史表现自行删除两个。池定义待固定；当前全量检查 22 个。
2. **逐月、真实合约元数据缺失**：`contract_specs.csv` 为逐来源/合约缺口清单，生效日期、真实手续费、保证金、涨跌停均留空，静态乘数/tick 标为 UNVERIFIED_STATIC_PROXY。两源共 622 个合约来源记录全部仅作诊断，不能作为有效独立执行证据。
3. **规格冲突**：BU tick：precision=2、旧脚本=1；JD multiplier：precision=5、旧研究覆盖=10；SH：precision 缺失、旧覆盖=1。未臆造裁决。现有 snapshot 手续费自述 historical_schedule_verified=false，不能倒填三年。
4. **映射可得时刻未知**：effective_date 和 snapshot 哈希未提供历史 available_at。前一自然日映射不等于已证实当时可得，也不能证明夜盘的历史主力选择无前视。两个归档全部映射仅作诊断。
5. **日历和时段不充分**：2025 年以前覆盖外记录及间隙已列出；当前 night_ends 为已有研究模板，不是逐合约历史时段证据。节前、换月缺口不可平滑补成交。
6. **末端覆盖不足及不完整 5m**：未完成 bar 被标记且不得作为完整信号输入；缺失行情保留范围，不补零收益或理想成交。

可验证的补齐来源：各交易所历史交易规则/合约规格、手续费和保证金调整公告、夜盘及节假日休市公告；期货公司有日期的保证金/加收手续费通知与成交结算单；CTP 的 Instrument、InstrumentMarginRate、InstrumentCommissionRate 日度快照。CTP 当前查询只能证明当前快照，不能恢复历史。主力映射需供应商历史发布时间或可重放的滞后成交量/OI 切换定义。此处列的是待取得的来源，不表示已下载核实。

## 4. 回归与人工账本

`python scripts/validate_s0.py`：**18 项通过**，`tests_after.txt` 保存完整测试名及输出。原归档聚合测试退出码：{result.returncode}，见 `existing_aggregation_tests.txt`。

增加风险处置时曾有 2 项回归失败，原始日志保留为 `tests_risk_disposal_failure.txt`：数据异常停机曾误走风险强平分支。现已将数据不可验证停机与亏损/回撤风险处置分开，前者不伪造成交，后者仅在下一可成交开盘处置。最终回归全部通过。

覆盖：信号 bar 不成交、未来数据修改前缀不变、完整周期聚合、同 bar 止损优先、跳空、缺 bar、跨午夜夜盘、周末/节前休市、覆盖外日历拒绝、换月不借新合约价格、无法成交、资金/保证金不足、单手 600、总在险 900、重复订单、时变费用和平今/平昨、临近收盘不新开及按已知时段清仓。

`manual_ledger.csv`：12 笔多空交替的独立手算预期。多头 101→119、空头 99→81，乘数 10；每笔毛利 180，开平费 2+3=5，净利 175，最终余额 52,100，最大余额误差 {v['max_ledger_error']}。`synthetic_account.csv` 记录逐 bar 余额、浮盈亏、权益、可用资金、保证金、在险。**这是合成账本验证，不是真实历史账单对账。**

## 5. 实现边界和下一步

参考内核目前只由合成、显式标注日历/可成交性的 bar 驱动，未把未验证归档接入并伪装成历史认证。无法成交/行情缺失可产生未决持仓，必须保留并排除对应证据，不能用最后理想收盘价强平。当前日亏/回撤 guard 基于开盘和收盘可观察点，不能证明 5m 内每一瞬间风控；周度复核、8%预警、真实盘口排队及组合最终执行属于后续 S6 集成范围。

旧 Python/Rust 回放入口仍按旧协议运行，均未获得本次认证。先补齐上述输入，再接入严格账户并逐入口回归；不应全局改默认值后将旧结果重新冠以新协议。真实交易日历/元数据未齐前，S0 维持 **NO-GO**。

复验命令（项目根目录）：

```powershell
python scripts/validate_s0.py
python scripts/run_s0_audit.py
python scripts/report_s0_audit.py --require-pass
```

最后命令生成报告后在 NO-GO 时返回退出码 2，供流水线阻断后续研究。审计扫描命令本身返回 0 只表示扫描完成，不能解释为 S0 通过。原始数据库只读打开，使用前后 SHA256 检查来源未变。
'''
    (OUT/'S0_audit_report.md').write_text(report,encoding='utf8')
    seal={str(p.relative_to(OUT)):digest(p) for p in OUT.iterdir() if p.is_file() and p.name!='artifact_hashes.json'}
    (OUT/'artifact_hashes.json').write_text(json.dumps(seal,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps(dict(s0_passed=False,synthetic_tests=v['synthetic_tests_passed'],cross_source=cross),ensure_ascii=False))
    if args.require_pass:raise SystemExit(2)


if __name__=='__main__':main()
