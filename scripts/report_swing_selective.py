"""Final comparison and raw-engine regression for the selective research round."""
import collections,datetime as dt,json,math,pathlib,statistics
import research_swing_selective as s
from research_swing_combination import weekly_delta
RUN=s.RUN;STRUCT=RUN/'structural-supplement';SESSION=RUN/'session-supplement';INTER=RUN/'execution-interaction';AUDIT=RUN/'flat-ambiguity-audit'
NAME='strong_kind_session'

def raw_checks():
    events={x['event_key']:x for q in ('early','recent') for x in s.read(s.PARENT/(q+'-pool.json'))};checks=collections.Counter()
    for q in ('early','recent'):
        control={}
        for line in (s.PARENT/q/'evaluations.jsonl').open(encoding='utf-8'):
            r=json.loads(line)
            if r['scenario'].startswith('P2_cycle_base/'):control[(r['event_key'],r['scenario'].split('/',1)[1])]=r
        for root,prefix in [(STRUCT,'P2_failure_'),(SESSION,'P2_session_')]:
            for line in (root/q/'evaluations.jsonl').open(encoding='utf-8'):
                r=json.loads(line);name,suffix=r['scenario'].split('/',1);name=name.removeprefix(prefix);e=s.ev(r)
                if name=='base':
                    old=control[(r['event_key'],suffix)];assert r['result']==old['result'];checks['new_engine_raw_baseline_exact']+=1
                if name=='half' or name=='half_loss':
                    snap=events[r['event_key']];original=snap['event'];w=snap['warning_candle'];tick=r['tick_size'];stop=original['stop'];up=r['direction']=='up'
                    edge=min(w['open'],w['close'])-tick if up else max(w['open'],w['close'])+tick
                    raw=(stop+(max(stop,edge) if up else min(stop,edge)))*.5
                    expected=(math.floor(raw/tick+1e-9) if up else math.ceil(raw/tick-1e-9))*tick
                    assert abs(r['stop']-expected)<1e-7;checks['half_stop_exact']+=1
                for f in r['result']['fills']:
                    assert f['ts']>r['warning_ts'];assert abs(f['price']/r['tick_size']-round(f['price']/r['tick_size']))<1e-6;checks['fill_tick_time']+=1
                if e['status']=='closed':
                    fs=r['result']['fills'];assert len(fs)==2 and fs[0]['volume']==fs[1]['volume']==1
                    gross=(fs[1]['price']-fs[0]['price'])*(1 if r['direction']=='up' else -1)
                    assert abs(gross-e['gross_points'])<1e-6;assert abs(e['risk']-abs(fs[0]['price']-r['stop']))<1e-6
                    assert abs(e['net_amount']-gross*r['multiplier'])<1e-6;assert not r['zero_volume_execution_unknown'];checks['raw_gross_risk_reconciled']+=1
    return dict(checks)

def run():
    checks=raw_checks();r=s.read(INTER/'results.json');leave=s.read(INTER/'leave-product-out.json');accepted=collections.defaultdict(list)
    for line in (INTER/'accepted.jsonl').open(encoding='utf-8'):
        z=json.loads(line)
        if z['case'].split('/')[0] in ('base',NAME):accepted[(z['policy'],z['period'],z['case'])].append(z['row'])
    comparisons={};paired={};fullflags={};samples={};intervals={}
    for policy in r:
        comparisons[policy]={};paired[policy]={};fullflags[policy]={}
        for q in ('early','recent'):
            comparisons[policy][q]={};paired[policy][q]={};fullflags[policy][q]={}
            for mode in ('corrected_touch','next_minute_open'):
                for slip in (1,2):
                    suffix=f'{mode}/slip{slip}';case=NAME+'/'+suffix;m=r[policy][q][case];b=r[policy][q]['base/'+suffix]
                    comparisons[policy][q][suffix]=dict(n=m['n'],net=m['net'],mean_r=m['mean_r'],win_rate=m['win_rate'],unknown=m['unknown'],
                        baseline_net=b['net'],baseline_mean_r=b['mean_r'],cash_retention=m['net']/b['net'] if b['net']>0 else None,
                        baseline_win_rate=b['win_rate'],drawdown=m['realized_cash_drawdown'],baseline_drawdown=b['realized_cash_drawdown'],
                        without_best3products=m['without_best3products'],without_best3days=m['without_best3days'],tail10_cash=m['tail10_cash'],baseline_tail10_cash=b['tail10_cash'])
                    fullflags[policy][q][suffix]=dict(cash_positive=m['net']>0,r_positive=m['mean_r']>0,retention=q!='recent' or m['net']>=.8*b['net'],
                        win_rate=q!='recent' or m['win_rate']>=b['win_rate'],drawdown=m['realized_cash_drawdown']<=b['realized_cash_drawdown'],
                        tail=m['tail10_cash']>=b['tail10_cash'],unknown=m['unknown']<=b['unknown'])
                    aa={x['event_key']:x for x in accepted[(policy,q,case)] if s.ev(x)['status']=='closed'};bb={x['event_key']:x for x in accepted[(policy,q,'base/'+suffix)] if s.ev(x)['status']=='closed'}
                    common=aa.keys()&bb.keys();dcommon=sum(s.ev(aa[k])['net_amount']-s.ev(bb[k])['net_amount'] for k in common)
                    added=sum(s.ev(aa[k])['net_amount'] for k in aa.keys()-bb.keys());removed=sum(s.ev(bb[k])['net_amount'] for k in bb.keys()-aa.keys())
                    assert abs(m['net']-b['net']-dcommon-added+removed)<1e-6
                    paired[policy][q][suffix]=dict(common=len(common),common_delta=dcommon,candidate_only=len(aa.keys()-bb.keys()),candidate_only_net=added,baseline_only=len(bb.keys()-aa.keys()),baseline_only_net=removed)
    for q in ('early','recent'):
        suffix='corrected_touch/slip1';rows=accepted[('two_paths_flat',q,NAME+'/'+suffix)];base=accepted[('two_paths_flat',q,'base/'+suffix)]
        intervals[q]=weekly_delta(base,rows);bins=collections.defaultdict(list)
        for row in rows:
            if s.ev(row)['status']=='closed':bins[row['warning_ts'][:7]].append(row)
        samples[q]={month:s.metrics(zz) for month,zz in sorted(bins.items())}
    candidate=dict(id=NAME,status='research_observation_only',qualified_for_trading=False,production_changed=False,independent_holdout=False,
        definition=dict(reference='P2_local_wide',aligned_or_missing='Original touch entry and S2 stop/nearby structural target.',
            counter='Warning-time side*(closed60m EMA20-EMA60)/ATR20<=0: cumulative warnings touch, other warnings completed1m close confirms and enter next minute open.',
            strong_counter='Alignment<=-1: additionally reject new entry in known final10min before a long session break; held gross PnL<0 in that window queues exit at next minute open. Profitable holds continue.',
            preserved='No new A breakout, product/month whitelist, position-size increase, tightened body stop, or target cap. All existing min10tick/cost/netRR/chase/dedup/calendar guards stay.'),
        results=comparisons,complete_gates_passed=False,remaining=['Early averageR and cash negative in all4 scenarios','Recent winrate lower in delayed-entry pressure modes','All periods repeatedly researched; data coverage incomplete','Unknown PnL remains, exposure audited separately'],
        primary_uncertainty=intervals,protocol_sha256=s.sha(INTER/'protocol.json'),rules_frozen_before_this_batch_outcomes=True)
    s.write(RUN/'observation-candidate.json',candidate)
    s.write(RUN/'final-analysis.json',dict(comparisons=comparisons,gate_checks=fullflags,paired_cash_decomposition=paired,warning_month_results=samples,weekly_delta_intervals=intervals,raw_checks=checks))
    audit=s.read(AUDIT/'results.json');primary='corrected_touch/slip1'
    lines=['# 保留近期收益的选择性适配：研究汇总','',
        '本轮45个不同规则模板（含参考），没有方案达到全周期正期望和完整稳定门槛。找到一个更好的观察折中：近期四场景均保留80%以上现金收益，同时上半年现金亏损、平均净R和已实现现金回撤均改善。生产代码、实盘参数未改。',
        '', '## 优先观察的规则','',
        '方向环境只用预警时已完成60分钟数据。小时结构同向或上下文缺失时，维持参考策略；小时结构逆向时，累积覆盖警报保留原触发，其他警报等分钟收盘确认后在下一分钟开盘成交；小时均线反向差至少1ATR时，另限制临近长休市的新开仓，只将当时尚亏损的持仓排队到下一分钟开盘退出。继续用原S2硬止损和附近结构目标。',
        '', '没有逐品种/月选参，没有增加仓位，没有新增A段突破要求。−1ATR与0R阈值均为批前固定；四种场景是每边1/2tick及额外一分钟入场延迟。数据均已反复研究，不是独立保留验证。',
        '', '## 与熟悉的原占用口径比较','',
        '|场景|上半年参考净额|候选净额|参考平均R|候选平均R|近期参考净额|候选净额|保留比例|',
        '|---|---:|---:|---:|---:|---:|---:|---:|']
    for suffix,e in comparisons['original']['early'].items():
        n=comparisons['original']['recent'][suffix]
        lines.append(f"|{suffix}|{e['baseline_net']:.2f}|{e['net']:.2f}|{e['baseline_mean_r']:.4f}|{e['mean_r']:.4f}|{n['baseline_net']:.2f}|{n['net']:.2f}|{n['cash_retention']:.1%}|")
    lines+=['', '## 未知收益与未知敞口必须分开','',
        '旧保守口径把所有歧义单保留占用品种直到合约数据结束。但部分单的两条模拟路径都是结束后空仓（未进场，或已进场并止损），只有盈亏/是否成交未知。新敏感性只在两条冻结路径均已空仓时释放占用；只要有一条仍可能持仓，就继续保留占用。未知盈亏没有被改成0，也没有被计作盈利。',
        '', '这个差别确实影响了前面看起来很好的实体止损组合：上半年已知净额由旧口径−11404.82变为释放确定空仓占用后的−15465.82，少亏的一部分来自少做了后续亏损单。不能把全部改善归于止损有效。本轮优先候选不收紧实体止损，上半年没有新增未知单，其改善不依赖这两个空仓歧义单的长时间占用。',
        '', '|释放两路径均空仓后，1tick|上半年笔数|净额|平均R|近期笔数|净额|平均R|近期收益保留|',
        '|---|---:|---:|---:|---:|---:|---:|---:|']
    rr=r['two_paths_flat'];be=rr['early']['base/'+primary];br=rr['recent']['base/'+primary];ce=rr['early'][NAME+'/'+primary];cr=rr['recent'][NAME+'/'+primary]
    for name,e,n in [('参考',be,br),('观察候选',ce,cr)]:lines.append(f"|{name}|{e['n']}|{e['net']:.2f}|{e['mean_r']:.4f}|{n['n']}|{n['net']:.2f}|{n['mean_r']:.4f}|{n['net']/br['net']:.1%}|")
    lines+=['', '这些仍是已知平仓净额；释放占用后参考近期仍有2笔、候选1笔盈亏未知。两条单调OHLC路径不等于真实逐笔行情的全部可能性；敏感性范围及未知单逐笔记录单独保留，不据此升级实盘。原口径和新敏感性都保留，以便核对。',
        '', '## 改善与剩余问题','',
        f"- 1tick上半年亏损减少约44.5%；平均R从−0.1342改善至−0.0600；已实现现金回撤从{be['realized_cash_drawdown']:.2f}降至{ce['realized_cash_drawdown']:.2f}。这些不是含浮盈亏的账户最大回撤。",
        f"- 同一口径近期1tick胜率由{br['win_rate']:.2%}到{cr['win_rate']:.2%}；入场额外延迟的两种场景胜率下降，仍未满足保持胜率的完整要求。",
        '- 四场景上半年现金和平均R仍负，不能称稳定策略。近期保留80%以上也没有证明跨期泛化。',
        '- 更强的全逆向休市减仓可把上半年1tick平均R改善到约−0.036，但压力场景近期收益保留降到约77%—80%，胜率下降更多，未优先采用。',
        '- 留品种、去掉最好3天/品种及预警周重采样诊断全部保留；区间没有校正本轮及此前的重复研究，不能据历史排名晋升。',
        '', '## 交易笔数口径','',
        '上一轮281笔是过滤后候选，上半年基线为501笔；近期同样候选97笔、基线191笔。上半年4843个预警，经风险/成本/净空间筛选剩769个，逐信号回放平仓562笔，机会竞争后501笔；近期对应2293→307→214→191。月份按预警归属，退出月另列，详见trade-count-audit.json。',
        '', '近期各品种输入起点不齐：BU从8月25日开始、EB/SH从9月16日开始，多数品种从7月14日开始。不能把窗口名称“7—10月”当作完整四个月的统一品种样本；本轮没有补齐数据，下一步必须补足覆盖和因果预热，再用新增时期验证。',
        '', '## 产物与核验','',
        '18个选择性模板、16个结构退出/分层模板、11个会话模板和3个新增交互模板，去掉重复参考与counter_body后共45个不同策略模板。歧义占用审计不算新的独立策略或独立数据。新增执行器评估313984行，不是独立交易笔数。',
        '', '新增6项Rust退出测试、13项Python路由/预算/歧义因果测试及原6项Rust入场回归通过。两份新执行器的原始参考逐行回归、半实体止损、有效tick/成交时间、现金和初始风险校验见final-verification.json。共169个生产文件哈希未变。',
        '', '本轮冻结观察候选见observation-candidate.json。四场景、两个占用口径、逐笔现金变化归因、按预警月统计和描述性周区间见final-analysis.json。全部失败方案保留在各子批protocol/results/gates/accepted文件中。',
        '', '结构/会话子批的通用自动报告沿用“18个模板”等旧说明；实际模板数、退出规则以各批冻结protocol.json及此汇总为准。结构批最后控制台打印曾因GBK编码失败，所有结果在打印前已完整保存；后续UTF8读取、逐行回归和哈希核验均通过，未重跑选参。',
        '', '没有启动自动交易或后台采集，也没有把已研究历史称为新验证。']
    (RUN/'summary.md').write_text('\n'.join(lines),encoding='utf-8')
    assert all(s.sha(k)==h for k,h in s.read(RUN/'protocol.json')['production_before'].items())
    for root in (STRUCT,SESSION):assert all(s.sha(root/'fork'/k)==h for k,h in s.read(root/'implementation-manifest.json').items())
    s.write(RUN/'final-verification.json',dict(raw_checks=checks,production_files_unchanged=169,new_rust_tests=6,new_python_tests=13,entry_regression_tests=6,
        complete_stability_achieved=False,qualified_for_trading=False,production_changed=False,
        files={str(p.relative_to(RUN)):s.sha(p) for p in [RUN/'summary.md',RUN/'final-analysis.json',RUN/'observation-candidate.json',RUN/'trade-count-audit.json']},
        source_scripts={str(p.relative_to(s.REPO)):s.sha(p) for p in [pathlib.Path(__file__),s.REPO/'scripts/research_swing_selective.py',s.REPO/'scripts/research_swing_failure.py',s.REPO/'scripts/research_swing_session.py',s.REPO/'scripts/audit_swing_flat_ambiguity.py',s.REPO/'scripts/research_swing_execution_interaction.py']}))
    print(json.dumps(dict(checks=checks,candidate=NAME,complete_stability_achieved=False,original=comparisons['original'],two_paths_flat=comparisons['two_paths_flat']),ensure_ascii=False,indent=2),flush=True)

if __name__=='__main__':run()
