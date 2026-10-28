"""Fixed other-month transfer results, including losses andofflineorderreplay."""
import ast
import collections
import json
import shutil
import subprocess
import validate_swing_jd_reclaim_2025 as v

RUN=v.RUN
R=v.R
T={'E20_ONLY':'E20S1完整对照','BASE':'原双向收回＋计划2R','F2S8':'新双向退出组合'}
def f(z):return '—' if z is None else f'{z:,.2f}'

def offline(data,meta,ends,results,monthly,cases):
    dest=RUN/'offline-order-replay';dest.mkdir();write=lambda n,z:v.old.write(dest/n,z)
    shutil.copy2(v.x.r.SNAP/'controller.py',dest/'controller.py')
    write('data.json',dict(metadata=meta,ends=ends,cases=cases,months=v.MONTHS))
    write('expected-results.json',results);write('expected-monthly.json',monthly)
    with (dest/'individual-decisions.jsonl').open('x',encoding='utf-8') as out:
        for (m,suffix),rs in data.items():
            for q in rs:out.write(json.dumps(dict(model=m,suffix=suffix,row=q),ensure_ascii=False)+'\n')
    # Admission andsummaryfunctions are copied verbatim fromverifiedsnapshot.
    source=(v.x.r.SNAP/'replay.py').read_text(encoding='utf-8');tree=ast.parse(source);helpers='\n\n'.join(ast.get_source_segment(source,n) for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in ['ev','admit','metric','same'])
    code='''"""Read-only stdlib replay, fee-priced raworders, immutable sourcehashes."""
import sys
sys.dont_write_bytecode=True
import collections,copy,hashlib,json
from pathlib import Path
import controller
ROOT=Path(__file__).resolve().parent
UNKNOWN={'rollover','pending','unfinished','ambiguous','missing_data'}
IMMEDIATE={'filtered','filtered_setup','filtered_geometry','filtered_entry_economics','filtered_A_net_body','filtered_net_rr_plan'}
def read(name):return json.loads((ROOT/name).read_text(encoding='utf-8'))
'''+helpers+'''
def main():
    manifest=read('manifest.json')
    for q,h in manifest.items():assert hashlib.sha256((ROOT/q).read_bytes()).hexdigest()==h,q
    spec=read('data.json');data=collections.defaultdict(list)
    for line in (ROOT/'individual-decisions.jsonl').open(encoding='utf-8'):
        z=json.loads(line);data[z['model'],z['suffix']].append(z['row'])
    expected=read('expected-results.json');monthly=read('expected-monthly.json');checks=0
    for case in spec['cases']:
        policy,suffix=case.split('/',1)
        for model,module in [('E20_ONLY',None),('BASE','BASE'),('F2S8','REFAIL2')]:
            meta=dict(spec['metadata']);stream=[]
            for prefix,component in ([('0','E20S1')] if module is None else [('0','E20S1'),('1',module)]):
                for original in data[component,suffix]:
                    q=copy.deepcopy(original);key=prefix+'|'+q['event_key'];meta[key]=meta[q['event_key']];q['event_key']=key;stream.append(q)
            aa=admit(stream,spec['ends'],policy,meta);same(metric(aa,spec['months']),expected[case+'/'+model]);checks+=1
            for month in spec['months']:
                same(metric([q for q in aa if q['warning_ts'].startswith(month)],[month]),monthly[case+'/'+model+'/'+month]);checks+=1
    print(json.dumps(dict(verified_records=checks,files_verified=len(manifest))))
if __name__=='__main__':main()
'''
    (dest/'replay.py').write_text(code,encoding='utf-8');write('manifest.json',{q.name:v.sha(q) for q in dest.iterdir() if q.is_file()})
    result=subprocess.run(['python',str(dest/'replay.py')],capture_output=True,text=True,check=True);return json.loads(result.stdout)

def run():
    p=R(RUN/'protocol.json');assert p['script_sha256']==v.sha(v.__file__)
    assert p['parent_rules_sha256']==v.sha(v.x.RUN/'protocol.json');assert p['combo_rules_sha256']==v.sha(v.x.RUN/'F2S8-followup/protocol.json')
    assert v.sha(p['execution_binary']['path'])==p['execution_binary']['sha256'];assert v.sha(p['generator_binary']['path'])==p['generator_binary']['sha256']
    result=R(RUN/'results.json');monthly=R(RUN/'monthly-results.json');primary='original/corrected_touch/slip1';cases=sorted({q.rsplit('/',1)[0] for q in result});coverage=R(RUN/'actual-contract-coverage.json');generation=R(RUN/'generation-checks.json');audit=R(RUN/'data-audit.json')
    ctl,_,_,_,_=v.x.r.load();meta=R(RUN/'metadata.json');ends={};data=collections.defaultdict(list);fees=R(v.x.r.SNAP/'fees.json');cal=v.b.z.s.TradingDates({});cal.dates=R(RUN/'trading-dates.json');nativehash={};nativecount=0;funnel=collections.defaultdict(collections.Counter)
    for task in ['E20','reclaim']:
        ends.update(R(RUN/task/'executions/manifest.json')['contract_ends']);path=RUN/task/'executions/evaluations.jsonl';nativehash[path.relative_to(RUN).as_posix()]=v.sha(path)
        for line in path.open(encoding='utf-8'):
            q=v.b.z.s.reprice(json.loads(line),fees,cal);m,suffix=q['scenario'].split('/',1);data[m,suffix].append(q);nativecount+=1
            for fill in q['result']['fills']:assert fill['ts']>q['warning_ts'];assert abs(fill['price']/q['tick_size']-round(fill['price']/q['tick_size']))<1e-7
            if suffix=='corrected_touch/slip1':funnel[m][v.x.EV(q)['status']]+=1
            if v.x.EV(q)['status']=='closed':
                e=v.x.EV(q);sign=1 if q['direction']=='up' else -1;assert abs((e['fill']-q['stop'])*sign-e['risk'])<1e-7
                assert abs(e['net_amount']+q['cost_repricing']['commission']-q['cost_repricing']['gross_amount_after_slippage'])<1e-6
    verification=offline(data,meta,ends,result,monthly,cases);v.write('offline-order-verification.json',verification);v.write('funnel.json',{k:dict(z) for k,z in funnel.items()})
    accepted=collections.defaultdict(dict)
    for line in (RUN/'accepted-primary.jsonl').open(encoding='utf-8'):
        z=json.loads(line);q=z['row']
        if v.x.EV(q)['status']=='closed':accepted[z['model']][(q['module'],q['original_event_key'])]=q
    attribution={};base=accepted['BASE'];cash=lambda q:v.x.EV(q)['net_amount']
    for model in ['BASE','F2S8']:
        aa=accepted[model];ref=accepted['E20_ONLY'];new=aa.keys()-ref.keys();removed=ref.keys()-aa.keys();attribution[model]=dict(kept_reference_n=len(aa.keys()&ref.keys()),new_module_n=len(new),new_module_net=sum(cash(aa[k]) for k in new),removed_reference_n=len(removed),removed_reference_net=sum(cash(ref[k]) for k in removed))
    common=accepted['F2S8'].keys()&base.keys();changes=[dict(key=k[1],module=k[0],warning_ts=accepted['F2S8'][k]['warning_ts'],direction=accepted['F2S8'][k]['direction'],old_exit=v.x.EV(base[k])['exit_reason'],new_exit=v.x.EV(accepted['F2S8'][k])['exit_reason'],net_delta=cash(accepted['F2S8'][k])-cash(base[k])) for k in common if abs(cash(accepted['F2S8'][k])-cash(base[k]))>1e-7]
    standalone={}
    for case in cases:
        policy,suffix=case.split('/',1);aa,_=v.b.admit(data['REFAIL2',suffix],ends,policy,ctl,meta);standalone[case]=v.b.z.metric(aa,v.MONTHS)
    v.write('module-standalone-results.json',standalone);v.write('incremental-attribution.json',dict(versus_E20=attribution,F2S8_versus_BASE_changes=changes))
    summary={m:dict(primary=result[primary+'/'+m],min16cash=min(result[case+'/'+m]['net'] for case in cases),min16normalized=min(result[case+'/'+m]['normalized_net'] for case in cases),max16unknown=max(result[case+'/'+m]['unknown'] for case in cases)) for m in T};v.write('comparison-summary.json',summary)
    lines=['# 双向收回A起点＋计划2R：2025年其他月份验证','',
        '日期：2026-10-27。固定规则在重新读取行情前冻结，本轮只验证，不因新结果修改入场、方向、止损、退出或目标。结果未支持直接推广。','',
        '## 原开发测试和本次新增测试的时间','',
        '- 原主要开发/优化评分：2026年1月1日—6月30日、7月1—10日、8月10日—10月23日11:05。中间有缺口。',
        '- 2026年7月13—24日另有已看过的固定JD2608复核，不是新独立样本。',
        '- **本次固定评分：2025年9月1日—12月31日15:00，逐月列出9、10、11、12月。**另取2025年8月预热和2026年1月实际退出尾部；连续跑四个月，不每月清仓或重置。','',
        '这四个月此前做过B2/B5回测，旧结果已被看过；它们未用于选择当前“收回＋计划2R＋F2S8”规则。本轮是额外月份的迁移检验，不宣称整段历史从未见过或具有统计独立性。','',
        '## 原始行情重新读取与一致性','',
        '仓库中的旧回测明确记录raw_prices_saved=false，项目数据库没有该期间1分钟实际成员行情。本次使用本机既有账号的普通TqBacktest/TqSim读取实际合约，沿用此前已成功的期货行情与静态元数据通道；没有交易、修改权限或调用批量导出接口。',
        f"共读取{sum(q['minutes'] for q in coverage['contracts'].values()):,}根实际合约分钟记录，覆盖{len(coverage['contracts'])}个合约，普通回测完整结束。全部7个合约的分钟序列哈希与旧回测记录一致；与现有5分钟归档重叠{audit['overlap_existing5m']}根，OHLCV及持仓量不同为0。",
        f"重新生成{generation['qualified_seeds']}个合格ABC预警，键和几何锚点与旧102个JD预警完全一致。在不按原接受名单筛选的前提下，生成{generation['reclaim_decisions']}个S0失守后收回决策。首次失守、首次收回均按当时前缀验证。",
        '原始分钟流只在内存传入执行器；保存了行情覆盖、哈希、预警、全部个体成交/拒绝及订单离线重算包。后续若改变执行规则仍需重新读取分钟流，不能把逐笔结果当成完整原始行情。','',
        '## 同规则整体结果','',
        '一手、冻结手续费、实际分钟成交；1—4tick/次成交、正常与延迟一分钟入场、两种未知占仓政策，共16场景。目标和退出参数均与2026实验相同。计划风险300元归一仅供研究比较，非账户收益。','',
        '|方案|平仓数|主净额|16场景最低净额|最低统一风险净额|最多未知|','|---|---:|---:|---:|---:|---:|']
    for m,q in summary.items():lines.append(f"|{T[m]}|{q['primary']['n']}|{f(q['primary']['net'])}|{f(q['min16cash'])}|{f(q['min16normalized'])}|{q['max16unknown']}|")
    lines+=['','三组未知均为0，有足够实际退出尾部；没有把未知按0元补齐。新退出组合比原收回组合少亏约120元，但自身仍亏，且不及E20对照。','',
        '## 分月结果','',
        '|评分预警月份|E20对照：笔数／净额|原收回组合：笔数／净额|新退出组合：笔数／净额|收回决策数|','|---|---:|---:|---:|---:|']
    for month in v.MONTHS:
        vals=[monthly[primary+'/'+m+'/'+month] for m in T];lines.append('|'+month+'|'+'|'.join(f"{q['n']} / {f(q['net'])}" for q in vals)+'|'+str(generation['reclaim_months'].get(month,0))+'|')
    lines+=['','12月虽整体盈利，但该月没有新增收回决策，利润全部来自原E20对照，不能作为收回模块通过验证的证据。9月、10月组合亏损；11月组合改善，未扭转四个月总体结果。','',
        '## 信号、推进与占仓交互','',
        '102个ABC预警中，E20主场景独立回放平仓10笔，大部分被风险/费用或净盈亏比拒绝。新增10个收回决策中，2个因风险/摩擦不合格、8个独立完成；与E20共用一仓后，仅5笔新增交易被接受。',
        '原收回组合新增5笔合计亏437元，新退出组合对应5笔合计亏317元；原E20的10笔全部保留，没有移除原对照交易。新退出组合改变3笔退出：9月一笔原周末盈利变小亏、另一笔止损提前减亏，11月一笔止损提前减亏，净改善约120元。',
        '这5笔新增组合交易没有一笔达到计划2R，两个盈利机会依赖周末退出，另外几笔最大有利推进很小。新退出组合扣佣金前、已计滑点的毛额也亏140元，不能把失败全部归因于手续费。',
        '新增模块独立一仓连续运行为7笔、净额约184元；合并时只接受其中5笔、净贡献−317元，说明时序占仓交互也影响结果。不能把独立模块小幅盈利直接加到E20上，也不能据此立刻改优先级来挽救本段表现。','',
        '## 本次泛化判断','',
        '**这四个月没有通过稳定盈利的迁移检验。**2026覆盖段的正收益不能外推成跨年份、跨行情均有效。退出改进在这段样本中仍减少了一些亏损，但尚未使收回入场与合并组合具备稳定正收益。',
        '样本仍少，不能由15笔或5笔新增成交断言整个ABC框架完全无效；也不能因为单个月盈利、独立模块盈利或新退出优于旧退出就宣布泛化通过。',
        '原冻结版本及2026正负证据继续保存；本轮不改参数、不筛掉亏损月份、不覆盖旧结果、不升级部署。下一步应先分清“收回信号的原始推进不足”与“共用占仓导致机会冲突”的贡献，再提出单独、可冻结的新假设。','',
        '## 离线复核','',
        '运行本目录`offline-order-replay/replay.py`，仅用Python标准库即可重新核对全部个体决策的连续占仓及240项整体/分月统计，不依赖行情服务和当前研究脚本。它使用已重计费用的个体成交结果，不能替代分钟行情重新生成。',
        '证据：[测试前协议](protocol.json) · [行情覆盖](actual-contract-coverage.json) · [数据一致性](data-audit.json) · [预警核对](generation-checks.json) · [全部场景](results.json) · [分月场景](monthly-results.json) · [增量归因](incremental-attribution.json) · [离线核验](offline-order-verification.json) · [完成核验](completion.json)。','']
    (RUN/'summary.md').write_text('\n'.join(lines),encoding='utf-8');plot(monthly,primary)
    for q in result.values():assert abs(sum(q['month_cash'].values())-q['net'])<1e-6
    finish(p,verification,nativecount,nativehash)
    print(json.dumps(dict(result={m:dict(n=q['primary']['n'],net=q['primary']['net'],min16=q['min16cash']) for m,q in summary.items()},offline_verification=verification),ensure_ascii=False))

def finish(p,verification,nativecount,nativehash):
    assert all(v.sha(v.b.ROOT/q)==h for q,h in p['production_before'].items())
    for q,h in R(v.x.r.SNAP/'manifest.json')['files'].items():assert v.sha(v.x.r.SNAP/q)==h
    v.write('completion.json',dict(completed=True,transfer_positive_profit_screen_pass=False,production_changed=False,frozen_candidate_unchanged=True,raw_prices_saved=False,
        native_rows=nativecount,native_hashes=nativehash,offline_verification=verification,report_sha256=v.sha(RUN/'summary.md'),report_source_sha256=v.sha(__file__),
        artifacts={q.relative_to(RUN).as_posix():v.sha(q) for q in RUN.rglob('*.json') if q.name!='completion.json'}))

def plot(monthly,primary):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np
    plt.rcParams['font.sans-serif']=['Microsoft YaHei'];plt.rcParams['axes.unicode_minus']=False
    fig,ax=plt.subplots(figsize=(11,5));loc=np.arange(len(v.MONTHS));width=.24
    for i,(model,label) in enumerate(T.items()):
        ys=[monthly[primary+'/'+model+'/'+month]['net'] for month in v.MONTHS];bars=ax.bar(loc+(i-1)*width,ys,width,label=label)
        for rect,value in zip(bars,ys):ax.text(rect.get_x()+rect.get_width()/2,value+(12 if value>=0 else -15),f'{value:.0f}',ha='center',va='bottom' if value>=0 else 'top',fontsize=9)
    ax.axhline(0,c='#777777',lw=.8);ax.set_xticks(loc,v.MONTHS);ax.set_ylabel('一手已平仓净额（元）');ax.set_title('固定规则迁移到2025年9—12月，连续占仓、不按月重置');ax.legend(fontsize=9);fig.tight_layout();fig.savefig(RUN/'monthly-comparison.png',dpi=160);plt.close(fig)

if __name__=='__main__':
    import sys
    if '--finalize-existing' in sys.argv:
        # Reporting-onlynamefix: preserveallwrittenresults/report/image.
        p=R(RUN/'protocol.json');result=subprocess.run(['python',str(RUN/'offline-order-replay/replay.py')],capture_output=True,text=True,check=True);hashes={};count=0
        for task in ['E20','reclaim']:
            path=RUN/task/'executions/evaluations.jsonl';hashes[path.relative_to(RUN).as_posix()]=v.sha(path);count+=sum(1 for _ in path.open(encoding='utf-8'))
        finish(p,json.loads(result.stdout),count,hashes);print(result.stdout)
    else:run()
