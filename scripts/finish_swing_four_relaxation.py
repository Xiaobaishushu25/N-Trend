"""Finish2026 report, then reuse executed base candidates for full-window E20."""
import collections
import json
import pathlib
import sys
import datetime as dt

sys.dont_write_bytecode=True
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parent))
import report_swing_four_relaxation as a

m=a.m
r=a.r
ROOT=r.RUN
OUT=ROOT/'full-cost-space20'


def write(name,obj):m.old.write(OUT/name,obj)


def finalize_2026():
    out=ROOT/'2026-only'
    a.RUN=out;a.EVALUATION_PERIODS=['early','recent'];a.SPAN='2026年1月—10月23日（仅已完成回放的2026年）'
    m.MONTHS=m.report.OLD_MONTHS
    p=m.old.read(ROOT/'protocol.json');results=m.old.read(out/'results.json');attribution=m.old.read(out/'attribution.json')
    if not (out/'protocol.json').exists():
        m.old.write(out/'protocol.json',dict(p,evaluation_periods=a.EVALUATION_PERIODS,source_protocol=str(ROOT/'protocol.json'),source_protocol_sha256=m.old.sha(ROOT/'protocol.json')))
    if not (out/'product-stress.json').exists():
        st={name:{s:dict(positive_cases=sum(attribution[name][pol][sf]['products'][s]['net_delta']>1e-7 for pol in p['policies'] for sf in m.SUFFIXES),cases=8,
                min_net_delta=min(attribution[name][pol][sf]['products'][s]['net_delta'] for pol in p['policies'] for sf in m.SUFFIXES),
                max_net_delta=max(attribution[name][pol][sf]['products'][s]['net_delta'] for pol in p['policies'] for sf in m.SUFFIXES),
                min_known_net=min(results[pol][name][sf]['products'][s]['net'] for pol in p['policies'] for sf in m.SUFFIXES)) for s in r.SYMS} for name in results['original']}
        m.old.write(out/'product-stress.json',st)
    a.report(results,m.old.read(out/'rejected-outcomes.json'),attribution,m.old.read(out/'stress-summary.json'))
    completion=dict(files={q.name:m.old.sha(q) for q in out.iterdir() if q.is_file() and q.name!='completion.json'},
                    scripts={str(q):m.old.sha(q) for q in [pathlib.Path(a.__file__),pathlib.Path(r.__file__),pathlib.Path(__file__)]},production_changed=False)
    (out/'completion.json').write_text(json.dumps(completion,ensure_ascii=False,indent=2),encoding='utf-8')
    m.MONTHS=m.report.MONTHS+m.report.OLD_MONTHS


def allowed(row,feature,limit=.20):
    # Only warning-time geometry and features. No fills, outcomes or future state.
    sign=1 if row['direction']=='up' else -1
    space=(row['target']-row['entry'])*sign
    cost=row['new_friction']*row['planned_risk']
    return space>0 and cost>=0 and cost/space<=limit+1e-12 and feature['a_body_net_ex_s0']>0


def run():
    OUT.mkdir(exist_ok=False)
    protocol=m.old.read(ROOT/'protocol.json')
    write('protocol.json',dict(created_at=dt.datetime.now().astimezone().isoformat(),
        fixed_experiment='E_cost_space20 from parent protocol; no new variant or new threshold',
        parent_protocol=str(ROOT/'protocol.json'),parent_sha256=m.old.sha(ROOT/'protocol.json'),
        threshold=.20,products=r.SYMS,period='2025-09 through 2026-10-23',
        method='Reuse unmerged/base individual executions, then impose warning-only cost/target<=20% and A-net-body>0 before portfolio selection. Original geometry/netRR/entry/exit/fees unchanged. Verify every surviving2026 individual result and every2026 portfolio against new native E20 replay before using2025 outcomes.',
        caution='Not all rejected signals can be labeled this way. Geometry/netRR-rejected2025 signals still have no counterfactual execution. Full diagnostic and other relaxations remain2026-only. This is exploratory repeatedly researched history.',production_changed=False))
    controller,pp,events,ends,data,sources,_=m.load()
    features={}
    for path in [m.old.RUN/'unmerged/older-pool.json',m.REPO/'research-runs/swing-entry-economics-20261025-v1/native-verification/early-pool.json',m.REPO/'research-runs/swing-entry-economics-20261025-v1/native-verification/recent-pool.json']:
        sources[str(path)]=m.old.sha(path)
        for z in m.old.read(path):features[z['event_key']]=z['entry_quality']
    mask={sf:[z for z in data['unmerged']['base',sf] if z['symbol'] in r.SYMS and allowed(z,features[z['event_key']])] for sf in m.SUFFIXES}
    native={}
    for period in ['early','recent']:
        calendar=a.TradingDates(m.old.read(protocol['periods'][period]['input'])['symbols'])
        fees=m.old.read(protocol['fees_path'])
        path=ROOT/period/'executions/evaluations.jsonl';sources[str(path)]=m.old.sha(path)
        for line in path.open(encoding='utf-8'):
            z=json.loads(line);name,sf=z['scenario'].split('/',1)
            if name!='E_cost_space20':continue
            z=a.reprice(z,fees,calendar);z['period']=period
            if m.report.s.ev(z)['status']=='closed':z['exit_trading_day']=calendar.day(m.report.s.ev(z)['exit_ts'])
            native[z['event_key'],sf]=z
    qa=collections.Counter()
    for sf,rs in mask.items():
        kept={z['event_key'] for z in rs if z['period']!='older'}
        for z in rs:
            if z['period']=='older':continue
            expected=native[z['event_key'],sf]
            for k in ['result','entry','stop','target','planned_risk','new_friction','cost_repricing']:
                assert z.get(k)==expected.get(k),(z['event_key'],sf,k)
            qa['exact_surviving2026_individual_rows']+=1
        for (key,case),z in native.items():
            if case==sf and key not in kept:
                assert controller.reservation(z,ends['unmerged'],'original') is None,(key,'missed reservation')
                qa['excluded2026_rows_have_no_reservation']+=1
    results={};attribution={};accepted={};evs=events['unmerged'];checks=[]
    native_results=m.old.read(ROOT/'2026-only/results.json')
    for pol in ['original','two_paths_flat']:
        results[pol]={};attribution[pol]={}
        for sf in m.SUFFIXES:
            allbase=data['unmerged']['budget_body',sf]
            aa0,ll0,_=controller.select(allbase,evs,ends['unmerged'],pp['sector_map'],'sector2_counter_stops',pol)
            rr=[z for z in allbase if z['symbol'] not in r.SYMS]+mask[sf]
            aa1,ll1,_=controller.select(rr,evs,ends['unmerged'],pp['sector_map'],'sector2_counter_stops',pol)
            m.report.audit(ll1,rr)
            #2026 clean-start comparison exactly matches the native new engine.
            check,_l,_risk=controller.select([z for z in rr if z['period']!='older'],evs,ends['unmerged'],pp['sector_map'],'sector2_counter_stops',pol)
            for group,rows in [('all',check),('focus',[z for z in check if z['symbol'] in r.SYMS])]:
                metric=a.metric(rows,evs)
                expected=native_results[pol]['E_cost_space20'][sf][group]
                for k in ['n','net','wins','losses','realized_cash_drawdown','unknown']:
                    assert m.equal(metric[k],expected[k]),(pol,sf,group,k,metric[k],expected[k])
            checks.append(dict(policy=pol,scenario=sf,exact_native2026_portfolio=True))
            results[pol][sf]={};attribution[pol][sf]={}
            for name,aa in [('C_control',aa0),('E_cost_space20',aa1)]:
                focus=[z for z in aa if z['symbol'] in r.SYMS]
                results[pol][sf][name]=dict(all=a.metric(aa,evs),focus=a.metric(focus,evs),
                    products={s:a.metric([z for z in focus if z['symbol']==s],evs) for s in r.SYMS},
                    periods={q:a.metric([z for z in focus if z['period']==q],evs) for q in ['older','early','recent']})
                accepted[pol,sf,name]=aa
            expected=m.old.read(m.old.RUN/'results.json')[pol]['combined']['B5/'+sf]
            for k,v in results[pol][sf]['C_control']['all'].items():
                if k in expected:assert m.equal(v,expected[k]),(pol,sf,k)
            for s in ['all','focus']+r.SYMS:
                keep=lambda z:s=='all' or (z['symbol'] in r.SYMS if s=='focus' else z['symbol']==s)
                attribution[pol][sf][s]=a.delta([z for z in aa1 if keep(z)],[z for z in aa0 if keep(z)],evs)
    stress={}
    for s in ['all','focus']+r.SYMS:
        changes=[attribution[pol][sf][s]['net_delta'] for pol in results for sf in m.SUFFIXES]
        stress[s]=dict(positive_cases=sum(x>1e-7 for x in changes),cases=8,min_delta=min(changes),max_delta=max(changes))
    write('results.json',results);write('attribution.json',attribution);write('stress.json',stress)
    write('checks.json',dict(qa=dict(qa),native_portfolio_checks=checks,original_full_B5_controls=8,source_hashes=sources,production_changed=False))
    with (OUT/'accepted.jsonl').open('x',encoding='utf-8') as f:
        for name in ['C_control','E_cost_space20']:
            for z in accepted['original',m.PRIMARY,name]:f.write(json.dumps(dict(variant=name,row=z),ensure_ascii=False)+'\n')
    summary(results,attribution,stress)
    write('completion.json',dict(files={p.name:m.old.sha(p) for p in OUT.iterdir() if p.is_file()},script_sha256=m.old.sha(__file__),production_changed=False))
    print(json.dumps(dict(primary=results['original'][m.PRIMARY],stress=stress)),flush=True)


def summary(results,attribution,stress):
    z=results['original'][m.PRIMARY];delta=attribution['original'][m.PRIMARY]
    lines=['# 四品种目标成本比放宽到20%：全历史对照','',
        '范围：2025年9月至2026年10月23日，沿用原回测实际覆盖。此处完整比较已冻结的E方案：只把目标成本比10%放宽至20%；仍保持止损≥10 tick、风险成本≤0.2R、净盈亏比≥1和A段净实体筛选。四品种同参数，单手；其他18品种不改，组合按时间重新竞争。',
        '', '该方案只改变预警时是否接纳，可以复用已经保存的较宽候选执行；已验证2026年全部幸存个别结果与本次新引擎相同，并完整复现8组2026年组合比较和8组全历史B5控制。它不需要重新取得2025年分钟价格。',
        '', '|品种|原笔数|放宽后笔数|原净额/元|放宽后净额/元|净增益|压力改善|原回撤→新回撤|','|---|---:|---:|---:|---:|---:|---:|---:|']
    for s in r.SYMS:
        old=z['C_control']['products'][s];new=z['E_cost_space20']['products'][s];st=stress[s]
        lines.append(f"|{m.NAMES[s]}|{old['n']}|{new['n']}|{old['net']:,.2f}|{new['net']:,.2f}|{new['net']-old['net']:+,.2f}|{st['positive_cases']}/8|{old['realized_cash_drawdown']:,.2f} → {new['realized_cash_drawdown']:,.2f}|")
    lines+=['','金额为已扣模型手续费及成交滑点的已知平仓净额；回撤为已实现回撤，未含浮亏和保证金。未知结果单列于results.json。',
        '', '## 新增／替代交易','', '|品种|新增已平仓|新增盈利|新增亏损|新增净额|被替代已平仓|被替代净额|','|---|---:|---:|---:|---:|---:|---:|']
    for s in r.SYMS:
        d=delta[s];v=d['added'];b=d['removed']
        lines.append(f"|{m.NAMES[s]}|{v['n']}|{v['wins']}|{v['losses']}|{v['net']:,.2f}|{b['n']}|{b['net']:,.2f}|")
    lines+=['','## 四品种合计','']
    for name in ['C_control','E_cost_space20']:
        v=z[name]['focus'];lines.append(f"- {name}：{v['n']}笔，净额{v['net']:,.2f}元，已实现回撤{v['realized_cash_drawdown']:,.2f}元，未知{v['unknown']}个。")
    lines+=['','## 时段对照','', '|方案|2025年9—12月|2026年1—6月|2026年近期段|','|---|---:|---:|---:|']
    for name in ['C_control','E_cost_space20']:
        lines.append('|'+name+'|'+'|'.join(f"{z[name]['periods'][q]['net']:,.2f}" for q in ['older','early','recent'])+'|')
    lines+=['','限制：较早四个月中，止损／风险成本或净盈亏比原本不合格的候选没有执行结果，无法用本方法给它们补标签。因此“全部被挡候选多数赚还是亏”的全面诊断仍仅覆盖2026年，详见[2026年完整诊断](../2026-only/summary.md)。不能把这批少量新增交易的胜率代表全部被挡信号，更不能把已反复研究的历史当成独立验证。',
        '', '此处没有改线上策略，也没有按各品种另挑阈值。各压力场景及逐笔新增／替代见results.json、attribution.json与stress.json。']
    (OUT/'summary.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')


if __name__=='__main__':
    finalize_2026();run()
