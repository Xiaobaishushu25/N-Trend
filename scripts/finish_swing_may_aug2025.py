"""Independent artifact checks, plot and final status for the earlier four months."""
import collections
import datetime as dt
import json
import math
import pathlib
import sys

sys.dont_write_bytecode=True
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parent))
import report_swing_may_aug2025 as a
r=a.r
RUN=r.RUN


def read(name):return r.old.read(RUN/name)
def write(name,obj):r.old.write(RUN/name,obj)


def run():
    p=read('protocol.json');results=read('results.json');combined=read('combined18-results.json')
    qa=collections.Counter()
    assert r.old.sha(p['fees_path'])==p['fees_sha256']
    assert r.old.sha(p['continuous_table_path'])==p['continuous_table_sha256']
    for n,h in p['engine_hashes'].items():assert r.old.sha(r.old.TARGET/'release'/n)==h
    for root,sha in p['frozen_references'].items():
        root=pathlib.Path(root);assert r.old.sha(root/'manifest.json')==sha
        assert all(r.old.sha(root/name)==h for name,h in r.old.read(root/'manifest.json').items())
    for obj in [results,combined]:
        for pol,models in obj.items():
            for model,cases in models.items():
                for sf,universes in cases.items():
                    for name,z in universes.items():
                        assert sum(x['n'] for x in z['products'].values())==z['n']
                        assert abs(sum(x['net'] for x in z['products'].values())-z['net'])<1e-7
                        assert abs(sum(z['warning_month_net'].values())-z['net'])<1e-7
                        for month,cash in z['warning_month_net'].items():
                            assert abs(sum(x['warning_month_net'][month] for x in z['products'].values())-cash)<1e-7
                        qa['portfolio_and_product_month_sums']+=1
    ev=a.m.report.s.ev
    count=0;keys=collections.Counter()
    for line in (RUN/'older/evaluations.jsonl').open(encoding='utf-8'):
        row=json.loads(line);e=ev(row);count+=1;keys[row['event_key']]+=1
        if e['status']!='closed':continue
        fills=row['result']['fills'];assert len(fills)==2 and all(f['volume']==1 for f in fills)
        assert fills[0]['ts']>row['warning_ts'] and fills[1]['ts']>=fills[0]['ts']
        assert e['risk']>0 and e['volume']==1
        for f in fills:assert abs(f['price']/row['tick_size']-round(f['price']/row['tick_size']))<1e-6
        points=(fills[1]['price']-fills[0]['price'])*(1 if row['direction']=='up' else -1)
        assert abs(points-e['gross_points'])<1e-7
        qa['individual_closed_fills_time_price_cash']+=1
    pool=read('older-pool.json');assert count==len(pool)*8==read('older/manifest.json')['evaluations']
    assert all(n==8 for n in keys.values())
    assert all(z['event']['evaluation'] is None and z['event']['outcome'] is None and z['event']['entry_ts'] is None for z in pool)
    overlap=read('prior-acquisition-overlap.json');assert overlap['compared']==136 and not overlap['mismatches']
    coverage=read('data-audit.json');assert all(all(v['scored_month_minutes'].get(m,0)>0 for m in a.MONTHS) for v in coverage.values())
    source=read('metadata-cache-source.json');assert r.old.sha(source['source'])==source['source_sha256']
    assert r.old.sha(RUN/'metadata-cache.json')==source['cache_sha256']
    metadata=read('metadata-cache.json');assert all(not any(k in z for k in ['last_price','open','close','settlement_price','open_interest']) for z in metadata.values())
    accepted=collections.defaultdict(list)
    for line in (RUN/'accepted.jsonl').open(encoding='utf-8'):
        z=json.loads(line);accepted[z['case']].append(z['row'])
    same=True
    for model in ['B2','B5']:
        full=accepted[model+'/six_original']
        for name,syms in r.UNIVERSES.items():
            chosen=accepted[model+'/'+name]
            for s in syms:
                get=lambda rr:sorted((z['event_key'],z['result'],z.get('cost_repricing')) for z in rr if z['symbol']==s and ev(z)['status']=='closed')
                same=same and get(full)==get(chosen)
    prior=r.old.read(r.ROOT/'research-runs/swing-universe-mix-20261026-v1/results.json')
    boundaries=[]
    for pol,models in results.items():
        for model,cases in models.items():
            for sf,universes in cases.items():
                for name,z in universes.items():
                    joined=combined[pol][model][sf][name];old=prior[pol][model][sf][name]
                    boundaries.append(dict(policy=pol,model=model,scenario=sf,universe=name,
                        net_delta=joined['net']-old['net']-z['net'],n_delta=joined['n']-old['n']-z['n']))
    write('boundary-effects.json',boundaries)
    checks=dict(qa=dict(qa),individual_rows=count,unlabelled_warning_pool=len(pool),all_scenarios_per_warning=8,
        overlap_exact_candles=136,complete_backtest=read('actual-contract-coverage.json')['complete_backtest'],
        primary_common_closed_trades_identical=same,
        metadata_contract_definitions_only=True,fee_table_and_native_engine_hashes_verified=True,
        source_strategy_manifests_verified=True,
        production_hash_differences=[k for k,h in p['production_before'].items() if r.old.sha(k)!=h],
        historical_controls=read('existing-control-verification.json')['exact_cases'])
    assert not checks['production_hash_differences']
    write('final-verification.json',checks)
    plot(results,accepted)
    enrich(results,combined)
    status=dict(completed_at=dt.datetime.now().astimezone().isoformat(),execution_complete=True,new_window=[r.START,r.END],
        new_evaluation_rows=count,new_warning_candidates=len(pool),models=p['versions'],universes=p['universes'],
        transport_repair='Authenticated futures route + cached official contract definitions; unchanged SDK readiness/backtest and frozen native strategy.',
        raw_prices_saved=False,production_changed=False,checks=checks,
        scripts={str(path):r.old.sha(path) for path in [pathlib.Path(r.__file__),pathlib.Path(a.__file__),pathlib.Path(__file__)]})
    (RUN/'run-status.json').write_text(json.dumps(status,ensure_ascii=False,indent=2),encoding='utf-8')
    old_status=(RUN/'status.md').read_text(encoding='utf-8')
    (RUN/'status.md').write_text('# 已完成：2025年5—8月向前回测\n\n2026-10-26已修复连接并完成新四个月及18个月接纳回放。结果见[完整报告](summary.md)，核验见[final-verification.json](final-verification.json)。\n\n以下保留完成前的连接排查记录，原先的“尚未完成”已被本次完成状态取代。\n\n'+old_status,encoding='utf-8')
    completion=dict(complete=True,completed_at=status['completed_at'],new_window=[r.START,r.END],production_changed=False,
        scripts=status['scripts'],artifacts={q.name:r.old.sha(q) for q in RUN.iterdir() if q.is_file() and q.name!='completion.json'})
    (RUN/'completion.json').write_text(json.dumps(completion,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(dict(verification=checks,B5={name:{k:z[k] for k in ['n','net','realized_cash_drawdown','unknown']} for name,z in results['original']['B5'][a.m.PRIMARY].items()})),flush=True)


def plot(results,accepted):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np
    plt.rcParams.update({'font.sans-serif':['Microsoft YaHei','SimHei','DejaVu Sans'],'axes.unicode_minus':False})
    symbols=r.SYMS;rs=accepted['B5/six_original'];ev=a.m.report.s.ev
    cash=np.zeros((4,6));n=np.zeros((4,6),dtype=int)
    for i,month in enumerate(a.MONTHS):
        for j,s in enumerate(symbols):
            known=[z for z in rs if z['warning_ts'][:7]==month and z['symbol']==s and ev(z)['status']=='closed']
            cash[i,j]=sum(ev(z)['net_amount'] for z in known);n[i,j]=len(known)
    fig,axes=plt.subplots(2,1,figsize=(13,9),layout='constrained',gridspec_kw={'height_ratios':[1.15,1]})
    bound=max(abs(cash.min()),abs(cash.max()));im=axes[0].imshow(cash,cmap='RdBu',vmin=-bound,vmax=bound,aspect='auto')
    axes[0].set_xticks(range(6),[a.m.NAMES[s]+' '+s.rstrip('0') for s in symbols]);axes[0].set_yticks(range(4),a.MONTHS)
    for i in range(4):
        for j in range(6):axes[0].text(j,i,f'{cash[i,j]:,.2f}\n{n[i,j]}笔',ha='center',va='center',fontsize=10,color='white' if abs(cash[i,j])>.65*bound else '#111827')
    axes[0].set_title('原六品种：各月各品种已知净额 / 笔数',loc='left');fig.colorbar(im,ax=axes[0],label='净额 / 元',shrink=.85)
    x=np.arange(4);width=.19;colors=['#64748b','#3b82f6','#0d9488','#f59e0b']
    for j,(name,label) in enumerate(r.LABELS.items()):
        vals=[results['original']['B5'][a.m.PRIMARY][name]['warning_month_net'][month] for month in a.MONTHS]
        axes[1].bar(x+(j-1.5)*width,vals,width,color=colors[j],label=label)
    axes[1].set_xticks(x,a.MONTHS);axes[1].set_ylabel('每月已知净额 / 元');axes[1].axhline(0,color='#64748b',lw=.8);axes[1].grid(axis='y',alpha=.2);axes[1].legend(ncol=2,frameon=False,fontsize=9)
    axes[1].set_title('四组固定搭配：组合逐月对照',loc='left')
    fig.suptitle('向前四个月 · 2025年5—8月 · 冻结B5\n单手，已扣模型费用和每边1tick滑点；月份按预警归属',fontsize=14)
    fig.savefig(RUN/'comparison.png',dpi=160);plt.close(fig)


def enrich(results,combined):
    path=RUN/'summary.md';text=path.read_text(encoding='utf-8')
    explanation='''## 本轮结果解读

新增四个月的主场景，EG/PVC/JD/SR四品种净额最高、已实现回撤最低；该组与EG/PP/JD/SR组在两类成交、两档滑点及两种未知政策下均保持已知净额为正。两种未知政策在本窗口没有接纳未知机会，不能把这些压力场景视为独立统计样本。

鸡蛋6笔净赚1,248.27元，超过EG的1,176元，并且8组压力均为正；这说明“非化工在当前策略下都没有价值”的判断过于宽泛。但仅6笔，仍不足以证明可稳定增加仓位或应进一步放松规则。

PP、PVC、SR在新四个月的8组压力下净额均为负；CJ主场景为负，其他成交假设可转正，执行敏感。EG/PVC/JD/SR组合主场景的利润来自EG和JD覆盖了PVC、SR的亏损，不等于四个品种各自都获得了验证。

五月四组均亏损。原六品种新增期主场景仅179.27元，较严压力可转亏，不能将后期收益直接外推到更早月份。B2与B5在本期差异很小，不据此宣称更复杂的B5具有明确优势。

本批只比较已固定的四组，不再根据新结果删掉亏损品种另找最优名单，也不修改生产策略。

'''
    text=text.replace('## 新四个月B5主场景合计',explanation+'## 新四个月B5主场景合计',1)
    text+='''
## 数据与结果校验

普通TqBacktest已完整走完4月预热到9月退出尾部，22个实际合约共279,548根分钟记录，只在内存处理。6个品种在5—8月均有覆盖。新数据与前次采集重叠的136根9月预警K线OHLC完全一致。

967个未标记未来结果的候选，经两条冻结执行模板、两类成交和两档滑点得到7,736条个别评估。新增期64组组合与18个月64组接纳回放，均核对品种、月份和组合净额及笔数合计。原64组历史控制已复现；成交时间、价格网格和单手现金计算另有校验。

连接修复只涉及账号正常服务发现的期货路由，以及已有官方静态合约定义缓存。缓存不含市场成交价格，未改账号权限、SDK数据就绪检查、策略规则或成交模型。前期超时是连接及元数据路径问题，本次已实际取得历史数据，不能沿用“仍然无法获取”的旧状态。

18个月结果是各冻结分段候选合并后重新计算占用和止损记录，并非本次新取18个月原始行情一次性重新生成全部信号。分段回撤不能相加；未知处理和跨期差异见完整JSON与boundary-effects.json。

![月度和品种对照](comparison.png)

核验：[final-verification.json](final-verification.json)；行情覆盖：[data-audit.json](data-audit.json)；重叠行情：[prior-acquisition-overlap.json](prior-acquisition-overlap.json)；连接修复：[transport-repair.json](transport-repair.json)。
'''
    path.write_text(text,encoding='utf-8')


if __name__=='__main__':run()
