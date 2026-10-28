"""Full causal B0--B5 comparison on earlier history and joined archives."""
import bisect
import collections
import importlib.util
import json
import math
import statistics
import pathlib
import sys

sys.path.insert(0,str(pathlib.Path(__file__).resolve().parent))

import research_swing_older_four as r
import research_swing_abstop as s
from report_swing_b_transition import enhance
from report_swing_c_structure import equal, SUFFIXES
from reprice_signal_costs import TradingDates, reprice

RUN, REPO = r.RUN, r.REPO
MODELS = {'B0':('best_a','base','base'), 'B1':('unmerged','base','base'),
          'B2':('unmerged','budget10','base'), 'B3':('unmerged','budget_body','base'),
          'B4':('unmerged','budget_body','sector2_counter'),
          'B5':('unmerged','budget_body','sector2_counter_stops')}
MONTHS = [f'2025-{i:02}' for i in range(9,13)]
OLD_MONTHS = [f'2026-{i:02}' for i in range(1,11)]


def distribution(rows, events, months):
    m=enhance(rows,events);known=[z for z in rows if s.ev(z)['status']=='closed'];bins=collections.defaultdict(float)
    for z in known:bins[z['warning_ts'][:7]]+=s.ev(z)['net_amount']
    warning_months={k:bins[k] for k in months};pos=[max(0,v) for v in m['bins']['product'].values()];den=sum(pos)
    gains=sum(max(0,s.ev(z)['net_amount']) for z in known);loss=-sum(min(0,s.ev(z)['net_amount']) for z in known)
    m.update(warning_month_net=warning_months,positive_warning_months=sum(v>0 for v in warning_months.values()),
        negative_warning_months=sum(v<0 for v in warning_months.values()),worst_warning_month=min(warning_months.values()),
        negative_warning_month_burden=-sum(min(0,v) for v in warning_months.values()),
        warning_month_std=statistics.pstdev(warning_months.values()),positive_product_hhi=sum((v/den)**2 for v in pos) if den else None,
        positive_products=sum(v>0 for v in m['bins']['product'].values()),profit_factor=gains/loss if loss else None,
        realized_gains=gains,realized_losses=loss,commission=sum(z['cost_repricing']['commission'] for z in known))
    return m


def row_cases(path, family, period, fees, calendar):
    out=collections.defaultdict(list)
    for line in path.open(encoding='utf-8'):
        z=json.loads(line);prefix,sf=z['scenario'].split('/',1)
        if family=='best_a' and prefix not in ['P2_wave_adaptive','P2_economics_base']:continue
        model='base' if family=='best_a' else prefix.removeprefix('P2_economics_')
        z=reprice(z,fees,calendar);z['period']=period
        if s.ev(z)['status']=='closed':z['exit_trading_day']=calendar.day(s.ev(z)['exit_ts'])
        out[(model,sf)].append(z)
    return out


def audit(ledger, rows):
    bykey={z['event_key']:z for z in rows};stops=collections.defaultdict(list)
    accepted=[l for l in ledger if l['accepted']]
    for l in accepted:
        e=s.ev(bykey[l['event_key']])
        if e['status']=='closed' and e['exit_reason']=='stop':stops[l['cluster']].append(e['exit_ts'])
    for v in stops.values():v.sort()
    last_symbol={}
    for l in accepted:
        assert l['start']>last_symbol.get(bykey[l['event_key']]['symbol'],'')
        last_symbol[bykey[l['event_key']]['symbol']]=l['end']
    for l in ledger:assert l['visible_cluster_stop_count']==bisect.bisect_left(stops[l['cluster']],l['start'])


def chart(res):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np
    plt.rcParams.update({'font.sans-serif':['Microsoft YaHei','SimHei','DejaVu Sans'],'axes.unicode_minus':False})
    colors=['#64748b','#b0b8c5','#2563eb','#0f9488','#ee9427','#d84857']
    fig,axes=plt.subplots(2,1,figsize=(12,8),layout='constrained',facecolor='#f7f9fc')
    for ax in axes:
        ax.set_facecolor('#f7f9fc');ax.grid(axis='y',alpha=.18);ax.axhline(0,color='#475569',lw=.8)
        ax.spines[['top','right']].set_visible(False)
    for i,model in enumerate(MODELS):
        m=res['original']['older'][model+'/'+SUFFIXES[0]]
        vals=[0]+list(np.cumsum([m['warning_month_net'][k] for k in MONTHS]))
        axes[0].plot(range(5),vals,label=model,color=colors[i],lw=2,ls='--' if i<2 else '-',marker='o',ms=4)
    axes[0].set_xticks(range(5),['起点','9月','10月','11月','12月']);axes[0].set_ylabel('累计已知净额 / 元')
    axes[0].set_title('主场景：按预警月份累计（非浮动权益曲线）',loc='left',fontsize=12);axes[0].legend(ncol=6,frameon=False)
    x=np.arange(4);width=.12
    for i,model in enumerate(MODELS):axes[1].bar(x+(i-2.5)*width,[res['original']['older'][model+'/'+sf]['net'] for sf in SUFFIXES],width,label=model,color=colors[i])
    axes[1].set_xticks(x,['触价 / 每边1tick','触价 / 每边2tick','延迟 / 每边1tick','延迟 / 每边2tick'])
    axes[1].set_ylabel('四个月已知净额 / 元');axes[1].set_title('成交与滑点压力',loc='left',fontsize=12)
    fig.suptitle('冻结基线向前回测 · 2025年9—12月',fontsize=17,fontweight='bold')
    fig.savefig(RUN/'comparison.png',dpi=160);plt.close(fig)


def run():
    p=r.read(RUN/'protocol.json');fees=r.read(p['fees_path'])
    snap=REPO/'research-strategies/best-a-portfolio-observation-v1'
    pp=r.read(snap/'protocol.json')
    spec=importlib.util.spec_from_file_location('older_frozen_controller',snap/'controller.py');controller=importlib.util.module_from_spec(spec);spec.loader.exec_module(controller)
    dates=r.read(RUN/'trading-dates.json');calendar=TradingDates({});calendar.dates=dates
    data={};events={};ends={};sourcehashes={}
    for family in ['best_a','unmerged']:
        root=RUN/family;pool=r.read(root/'older-pool.json');events[family]={z['event_key']:z['event'] for z in pool}
        ends[family]=r.read(root/'older/manifest.json')['contract_ends']
        data[family]=row_cases(root/'older/evaluations.jsonl',family,'older',fees,calendar)
        sourcehashes[str(root/'older/evaluations.jsonl')]=r.sha(root/'older/evaluations.jsonl')
    older_events={f:dict(v) for f,v in events.items()};older_ends={f:dict(v) for f,v in ends.items()}
    prior={f:collections.defaultdict(list) for f in data}
    native=REPO/'research-runs/swing-entry-economics-20261025-v1/native-verification'
    for family in ['best_a','unmerged']:
        root=REPO/'research-runs/swing-retirement-20261025-v1/near_start' if family=='best_a' else native
        for period in ['early','recent']:
            events[family].update({z['event_key']:z['event'] for z in r.read(root/(period+'-pool.json'))})
            cc=TradingDates(r.read(pp['periods'][period]['input'])['symbols'])
            for c,end in r.read(root/period/'manifest.json')['contract_ends'].items():ends[family][c]=max(end,ends[family].get(c,''))
            path=root/period/'evaluations.jsonl';sourcehashes[str(path)]=r.sha(path)
            for key,rs in row_cases(path,family,period,fees,cc).items():prior[family][key].extend(rs)
    refs={}
    economic=r.read(REPO/'research-runs/swing-entry-economics-20261025-v1/results.json')
    body=r.read(REPO/'research-runs/swing-entry-economics-20261025-v1/body-interaction/results.json')
    portfolio=r.read(pp['reference_results'])
    for model in MODELS:
        refs[model]=(economic,{'B0':'best_a/base','B1':'unmerged/base','B2':'unmerged/budget10'}[model]) if model in ['B0','B1','B2'] else (body,'unmerged/budget_body') if model=='B3' else (portfolio,'budget_body/'+MODELS[model][2])
    results={pol:{q:{} for q in ['older','prior_2026','combined']} for pol in p['policies']}
    accepted={};checks=[];attribution={};boundaries=[]
    accepted_file=(RUN/'accepted.jsonl').open('x',encoding='utf-8')
    ledger_file=(RUN/'opportunity-ledgers.jsonl').open('x',encoding='utf-8')
    for pol in p['policies']:
        for model,(family,profile,rule) in MODELS.items():
            for sf in SUFFIXES:
                rows=data[family][(profile,sf)];old=prior[family][(profile,sf)];case=model+'/'+sf
                sets={}
                for period,rr,ee,mm in [('older',rows,older_ends[family],MONTHS),('prior_2026',old,ends[family],OLD_MONTHS),('combined',rows+old,ends[family],MONTHS+OLD_MONTHS)]:
                    aa,ll,risk=controller.select(rr,events[family],ee,pp['sector_map'],rule,pol)
                    rev,_,_=controller.select(rr,events[family],ee,pp['sector_map'],rule,pol,True)
                    audit(ll,rr)
                    m=distribution(aa,events[family],mm);m['reverse_tie_net']=s.metrics(rev)['net']
                    m.update({k:v for k,v in risk.items() if k!='realized_settlements'})
                    results[pol][period][case]=m;sets[period]=aa
                    if period=='prior_2026':
                        ref,key=refs[model];expected=ref[pol]['combined'][key+'/'+sf]
                        # Existing versions differ only in whether peak-risk fields were reported.
                        bad={k:[m[k],v] for k,v in expected.items() if k in m and not equal(m[k],v)}
                        assert not bad,(pol,case,bad)
                        checks.append(dict(policy=pol,case=case,all_existing_metrics_exact=True))
                    if period=='older':
                        accepted[(pol,case)]=aa
                        for z in aa:accepted_file.write(json.dumps(dict(policy=pol,case=case,row=z),ensure_ascii=False)+'\n')
                        for l in ll:ledger_file.write(json.dumps(dict(policy=pol,case=case,ledger=l),ensure_ascii=False)+'\n')
                summed=results[pol]['older'][case]['net']+results[pol]['prior_2026'][case]['net']
                boundaries.append(dict(policy=pol,case=case,naive_sum=summed,continuous_net=results[pol]['combined'][case]['net'],boundary_delta=results[pol]['combined'][case]['net']-summed))
    accepted_file.close();ledger_file.close()
    for pol in p['policies']:
        attribution[pol]={}
        for child,parent in [('B1','B0'),('B2','B1'),('B3','B2'),('B4','B3'),('B5','B4')]:
            attribution[pol][child]={}
            for sf in SUFFIXES:
                old={z['event_key']:z for z in accepted[(pol,parent+'/'+sf)] if s.ev(z)['status']=='closed'}
                new={z['event_key']:z for z in accepted[(pol,child+'/'+sf)] if s.ev(z)['status']=='closed'}
                common=old.keys()&new.keys();removed=old.keys()-new.keys();added=new.keys()-old.keys()
                delta_common=sum(s.ev(new[k])['net_amount']-s.ev(old[k])['net_amount'] for k in common)
                changed_common=sum(new[k]['result']!=old[k]['result'] or new[k]['cost_repricing']!=old[k]['cost_repricing'] for k in common)
                if child!='B1':assert changed_common==0
                removed_net=sum(s.ev(old[k])['net_amount'] for k in removed);added_net=sum(s.ev(new[k])['net_amount'] for k in added)
                diff=results[pol]['older'][child+'/'+sf]['net']-results[pol]['older'][parent+'/'+sf]['net']
                assert abs(diff-(added_net-removed_net+delta_common))<1e-6
                attribution[pol][child][sf]=dict(parent=parent,common_n=len(common),changed_common_execution=changed_common,common_cash_delta=delta_common,removed_n=len(removed),removed_original_net=removed_net,added_n=len(added),added_net=added_net,delta=diff)
    for name,obj in [('results.json',results),('attribution.json',attribution),('boundary-continuity.json',boundaries),('verification.json',dict(archive_controls=checks,source_hashes=sourcehashes,all_causal_interval_audits_passed=True,raw_prices_saved=False,production_changed=False))]:r.write(RUN/name,obj)
    assert all(r.sha(k)==v for k,v in p['production_before'].items())
    for path,h in p['frozen_references'].items():
        root=r.pathlib.Path(path)
        assert r.sha(root/'manifest.json')==h
        assert all(r.sha(root/k)==v for k,v in r.read(root/'manifest.json').items())
    report(results,attribution,boundaries)
    chart(results)
    r.write(RUN/'completion.json',dict(status='complete',window_start=r.START,window_end=r.END,models=list(MODELS),older_cases=48,archive_controls=len(checks),raw_prices_saved=False,production_changed=False,
        sources={str(f):r.sha(f) for f in [r.pathlib.Path(__file__),REPO/'scripts/research_swing_older_four.py',REPO/'scripts/probe_swing_older_tqsdk.py']},
        artifacts={f.name:r.sha(f) for f in RUN.iterdir() if f.is_file() and f.name!='completion.json'}))
    print(json.dumps({model:results['original']['older'][model+'/'+SUFFIXES[0]] for model in MODELS},ensure_ascii=False),flush=True)


def report(res,att,boundaries):
    main={k:res['original']['older'][k+'/'+SUFFIXES[0]] for k in MODELS}
    lines=['# 冻结基线向前四个月回测：2025年9—12月','',
        '记录日期：2026-10-26（北京时间）。B3/B4/B5为原开发期收益领先候选，B2为简单经济性对照，B0原版及B1结构消融同时保留。全部阈值沿用冻结定义，未按本批结果调参。','',
        '## 数据与口径','',
        '22个原品种；TqSDK普通TqBacktest通过本机既有账号读取实际合约1分钟历史。评分预警窗口2025-09-01至2025-12-31日盘收盘；另有8月预热和2026年1月退出数据。主力映射沿用供应商历史表，用实际成员合约拼接5分钟输入，随后沿用冻结15分钟聚合、A选择及服务去重。原始分钟只在内存参与计算，未使用专业版批量下载接口。覆盖和内存输入哈希另存。','',
        '主表为original未知占用、corrected_touch/slip1，每个机会一手，已扣冻结模型手续费和每边1tick滑点。不是收益率或完整账户回测；费用不是2025年逐日实际账单；回撤为已实现现金回撤。未知损益不计零，单列。','',
        '## 新增四个月主场景','', '|版本|已知平仓|净额/元|胜率|平均净R|盈亏因子|已实现回撤/元|未知|','|---|---:|---:|---:|---:|---:|---:|---:|']
    best=max(main,key=lambda k:main[k]['net'])
    b5=main['B5'];strict=res['original']['older']['B5/'+SUFFIXES[-1]]
    conclusion=f"新增四个月主场景最高净额为{best}的{main[best]['net']:.2f}元。原开发期最高B5在新窗口净额{b5['net']:.2f}元，已实现回撤{b5['realized_cash_drawdown']:.2f}元；最严延迟＋每边2tick场景净额{strict['net']:.2f}元。"
    positive=sum(max(0,v) for v in b5['bins']['product'].values())
    leaders=sorted(b5['bins']['product'].items(),key=lambda z:z[1],reverse=True)[:2]
    leader_sum=sum(v for _,v in leaders)
    interpretation=[conclusion,'',
        '主要改善仍来自简单的空间成本过滤：B2相对B1增加1358.72元。B3的A净实体条件仅多87元；B4选择性并发在本窗口完全没有增加净额；B5两次止损停试再增加97元，仅涉及一笔原亏损交易。不能据此把新增复杂规则视为已得到广泛验证。','',
        f"B5只有主场景盈利，另三种成交/成本压力均亏损，两种未知占用政策下结论相同。其平均净R仅{b5['mean_r']:.4f}，9月亏{abs(b5['warning_month_net']['2025-09']):.2f}元，四个月中的三个正收益月不足以证明稳定。",'',
        f"B5利润主要依赖{leaders[0][0]}和{leaders[1][0]}，两者贡献{leader_sum:.2f}元、占全部正贡献的{leader_sum/positive:.2%}；剔除这两个品种的固定成交贡献后，其他品种合计{b5['net']-leader_sum:.2f}元。此为描述性贡献拆分，不是重新竞争后的可执行剔除回测。",'']
    lines[2:2]=interpretation
    def fmt(x,style='.2f'):return '—' if x is None else format(x,style)
    for k,m in main.items():lines.append(f"|{k}|{m['n']}|{m['net']:.2f}|{fmt(m['win_rate'],'.2%')}|{fmt(m['mean_r'],'.4f')}|{fmt(m['profit_factor'])}|{m['realized_cash_drawdown']:.2f}|{m['unknown']}|")
    lines+=['','## 预警月份净额','', '|月份|'+'|'.join(MODELS)+'|','|---|'+'---:|'*len(MODELS)]
    for month in MONTHS:lines.append('|'+month+'|'+'|'.join(f"{main[k]['warning_month_net'][month]:.2f}" for k in MODELS)+'|')
    lines+=['','## 四种成交/成本场景：新增四个月净额','', '|场景|'+'|'.join(MODELS)+'|','|---|'+'---:|'*len(MODELS)]
    for sf in SUFFIXES:lines.append('|'+sf+'|'+'|'.join(f"{res['original']['older'][k+'/'+sf]['net']:.2f}" for k in MODELS)+'|')
    lines+=['','## 两种未知占用政策','', '|版本/政策|触价1tick|触价2tick|延迟1tick|延迟2tick|','|---|---:|---:|---:|---:|']
    for k in MODELS:
        for pol in res:lines.append('|'+k+'/'+pol+'|'+'|'.join(f"{res[pol]['older'][k+'/'+sf]['net']:.2f}" for sf in SUFFIXES)+'|')
    lines+=['','全部48个新增场景的未知笔数均为1。主场景未知机会为PP0，2025-10-17 22:45预警，实际合约DCE.pp2601，在10月20日09:01的分钟内两种OHLC路径分别为入场前取消、入场后止损。其损益保持未知；original保留占用至数据末尾，two_paths_flat只在两条路径都确定空仓时释放。','',
        '|版本/政策：已知平仓笔数|触价1tick|触价2tick|延迟1tick|延迟2tick|','|---|---:|---:|---:|---:|']
    for k in MODELS:
        for pol in res:lines.append('|'+k+'/'+pol+'|'+'|'.join(str(res[pol]['older'][k+'/'+sf]['n']) for sf in SUFFIXES)+'|')
    lines+=['','## 新增窗口分布指标','', '|版本|前三正贡献占比|正贡献HHI|盈利品种数|亏损月份负担/元|','|---|---:|---:|---:|---:|']
    for k,m in main.items():lines.append(f"|{k}|{m['top3_product']:.2%}|{m['positive_product_hhi']:.4f}|{m['positive_products']}|{m['negative_warning_month_burden']:.2f}|")
    lines+=['','## 连续合并新增窗口和已有2026年档案','',
        '将全部个体机会按触发时间重新竞争，不在年末重置占用或A簇止损计数，不直接相加分期回撤。2026年7—10月仍沿用原先不齐的实际覆盖，不能称14个月等覆盖账户业绩。','',
        '|版本|新增四个月|原2026年档案净额|连续合并净额|连续合并已实现回撤|','|---|---:|---:|---:|---:|']
    for k,m in main.items():
        old=res['original']['prior_2026'][k+'/'+SUFFIXES[0]];full=res['original']['combined'][k+'/'+SUFFIXES[0]]
        lines.append(f"|{k}|{m['net']:.2f}|{old['net']:.2f}|{full['net']:.2f}|{full['realized_cash_drawdown']:.2f}|")
    lines+=['','## 新增窗口品种净额','', '|品种|'+'|'.join(MODELS)+'|','|---|'+'---:|'*len(MODELS)]
    symbols=sorted({s for m in main.values() for s in m['bins']['product']})
    for sym in symbols:lines.append('|'+sym+'|'+'|'.join(fmt(main[k]['bins']['product'].get(sym)) for k in MODELS)+'|')
    lines+=['','“—”表示没有该品种已知平仓贡献记录，不把未知损益填零。','',
        '## 相对父版本归因：新增窗口主场景','', '|版本→父版|净额差|共同平仓|共同执行差额|移除笔数|移除原净额|新接纳笔数|新接纳净额|','|---|---:|---:|---:|---:|---:|---:|---:|']
    for child in list(MODELS)[1:]:
        a=att['original'][child][SUFFIXES[0]]
        lines.append(f"|{child} vs {a['parent']}|{a['delta']:+.2f}|{a['common_n']}|{a['common_cash_delta']:+.2f}|{a['removed_n']}|{a['removed_original_net']:.2f}|{a['added_n']}|{a['added_net']:.2f}|")
    lines+=['','B2—B5相对父版所有共同平仓的完整执行及费用核对相同；差额来自预警过滤和机会竞争。B1结构消融另外核对共同执行差额。','',
        '## 核验和证据边界','',
        '新引擎使用独立构建目录；共享旧构建缓存曾错误引用旧selector库，已在生成本批结果前改用隔离构建。经济性守卫及selector前缀因果测试通过。旧EG预警池和完整个体执行按归档复现；48个原2026年组合统计对照复现。','',
        '所有方案、成本场景和未知政策完整保留。未因品种历史盈亏删除品种，未增加仓位、改变阈值或自动修改生产参数。新窗口对固定规则有外推参考价值，但多个版本共享交易，且此前已有大量历史研究，不据此声称独立统计显著或实盘合格。','',
        '数据覆盖见data-audit.json、actual-contract-coverage.json；结果见results.json；完整接受与拒绝记录见accepted.jsonl和opportunity-ledgers.jsonl；父版归因见attribution.json；跨年占用差额见boundary-continuity.json。']
    if (RUN/'sdk-edb-overlap-audit.json').exists():
        overlap=r.read(RUN/'sdk-edb-overlap-audit.json')
        lines += ['',f"SDK与旧EDB档案重叠段逐根比较：共{sum(v['compared_5m_bars'] for v in overlap.values())}根5分钟柱，OHLCV/持仓量不同的有{sum(v['mismatched_bars'] for v in overlap.values())}根。此检查属于同供应商两种接口的一致性检查，不是独立跨供应商核验。"]
    (RUN/'summary.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')


if __name__=='__main__':run()
