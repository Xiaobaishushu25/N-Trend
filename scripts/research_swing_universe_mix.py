"""Four fixed universe comparisons; original B2/B5, no strategy retuning."""
import collections
import datetime as dt
import json
import pathlib
import sys

sys.dont_write_bytecode=True
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parent))
import research_swing_monthly_products as m

RUN=m.REPO/'research-runs/swing-universe-mix-20261026-v1'
UNIVERSES={
    'six_original':['EG0','PP0','V0','JD0','SR0','CJ0'],
    'five_without_CJ':['EG0','PP0','V0','JD0','SR0'],
    'four_EG_V_JD_SR':['EG0','V0','JD0','SR0'],
    'four_EG_PP_JD_SR':['EG0','PP0','JD0','SR0'],
}
LABELS={'six_original':'原六品种','five_without_CJ':'五品种：去CJ',
        'four_EG_V_JD_SR':'四品种：EG/PVC/JD/SR','four_EG_PP_JD_SR':'四品种：EG/PP/JD/SR'}


def write(name,obj):m.old.write(RUN/name,obj)


def compact(z):
    keys=['n','wins','losses','net','win_rate','mean_r','realized_cash_drawdown','unknown',
          'positive_warning_months','negative_warning_months','worst_warning_month','warning_month_net','bins']
    return {k:z[k] for k in keys}


def run():
    RUN.mkdir(exist_ok=False)
    protocol=dict(created_at=dt.datetime.now().astimezone().isoformat(),universes=UNIVERSES,
        versions=['B2','B5'],policies=['original','two_paths_flat'],scenarios=m.SUFFIXES,
        primary='B5/original/corrected_touch/slip1',
        purpose='Answer product mix tradeoffs, not exhaustive subset search; four lists declared before this replay. All histories already researched; exploratory in-sample comparison.',
        execution='Original frozen strategy/fees/position sizing; do not mix in latest JD20 observation. Re-select all potential opportunities, do not sum old accepted rows.',
        risk='Single lot is NOT equal risk. Realized DD excludes floating PnL and margins. Lower exposure/number of trades can lower DD without better predictive edge. Price correlations are historical daily returns, not intraday strategy correlations.',
        sources={'six_results':m.old.sha(m.REPO/'research-runs/swing-requested-six-products-20261026-v1/results.json'),
                 'correlations':m.old.sha(m.REPO/'research-runs/swing-requested-six-products-20261026-v1/market-return-correlations.json')},
        production_before={str(p):m.old.sha(p) for p in ['crates/n-core/src/trader/candidates.rs','src/stores/liveTrader.ts','src/types.ts','src/views/LiveTraderView.vue']},
        production_changed=False)
    write('protocol.json',protocol)
    controller,pp,events,ends,data,sources,coverage=m.load()
    ref=m.old.read(m.REPO/'research-runs/swing-requested-six-products-20261026-v1/results.json')
    results={};controls=[];accepted={};ledgers={}
    for pol in protocol['policies']:
        results[pol]={}
        for model in protocol['versions']:
            family,profile,rule=m.report.MODELS[model]
            results[pol][model]={}
            for sf in m.SUFFIXES:
                results[pol][model][sf]={}
                for name,syms in UNIVERSES.items():
                    rows=[z for z in data[family][profile,sf] if z['symbol'] in syms]
                    aa,ll,risk=controller.select(rows,events[family],ends[family],pp['sector_map'],rule,pol)
                    m.report.audit(ll,rows)
                    z=m.report.distribution(aa,events[family],m.MONTHS)
                    z.update({k:v for k,v in risk.items() if k!='realized_settlements'})
                    if name=='six_original':
                        expect=ref[pol][model][sf]['six']
                        for k,v in expect.items():
                            if k in z:assert m.equal(z[k],v),(pol,model,sf,k,z[k],v)
                        controls.append(dict(policy=pol,model=model,scenario=sf,exact=True))
                    byproduct={s:compact(m.report.distribution([r for r in aa if r['symbol']==s],events[family],m.MONTHS)) for s in syms}
                    z['product_metrics']=byproduct
                    z['EG_share_of_known_net']=byproduct['EG0']['net']/z['net'] if z['net']>0 else None
                    z['chemical_share_of_known_net']=sum(byproduct[s]['net'] for s in syms if s in ['EG0','PP0','V0'])/z['net'] if z['net']>0 else None
                    results[pol][model][sf][name]=z
                    if pol=='original' and sf==m.PRIMARY:
                        accepted[model+'/'+name]=aa;ledgers[model+'/'+name]=ll
    stress={}
    for model in protocol['versions']:
        stress[model]={}
        for name in UNIVERSES:
            cases=[results[pol][model][sf][name] for pol in protocol['policies'] for sf in m.SUFFIXES]
            stress[model][name]=dict(min_net=min(x['net'] for x in cases),max_drawdown=max(x['realized_cash_drawdown'] for x in cases),
                min_n=min(x['n'] for x in cases),max_n=max(x['n'] for x in cases),
                cases_net_positive=sum(x['net']>0 for x in cases),max_unknown=max(x['unknown'] for x in cases))
    write('results.json',results);write('stress.json',stress);write('opportunity-ledgers.json',ledgers)
    write('checks.json',dict(exact_six_controls=controls,source_hashes=sources,
        production_changed=False,production_hash_differences=[p for p,h in protocol['production_before'].items() if m.old.sha(p)!=h]))
    with (RUN/'accepted.jsonl').open('x',encoding='utf-8') as f:
        for case,rows in accepted.items():
            for row in rows:f.write(json.dumps(dict(case=case,row=row),ensure_ascii=False)+'\n')
    report(results,stress)
    write('completion.json',dict(artifacts={p.name:m.old.sha(p) for p in RUN.iterdir() if p.is_file()},script_sha256=m.old.sha(__file__),production_changed=False))
    print(json.dumps({model:{name:dict(primary={k:z[k] for k in ['n','net','realized_cash_drawdown','positive_warning_months','unknown','EG_share_of_known_net','chemical_share_of_known_net']},stress=stress[model][name]) for name,z in results['original'][model][m.PRIMARY].items()} for model in protocol['versions']}),flush=True)


def report(results,stress):
    lines=['# 固定品种搭配的收益、回撤与集中度取舍','',
        '2026-10-26。沿用2025年9月至2026年10月23日原窗口，7月与10月覆盖不完整。只比较四个事前写入本批协议的名单；这是已被反复研究历史上的探索，不是全组合扫描或独立样本外验证。',
        '', '原B2、B5、单手、冻结费用模型及四种成交压力／两种未知政策；每组都重新竞争完整潜在机会。没有使用最新鸡蛋20%放松观察版，没有修改线上。',
        '', '## B5主场景','', '|名单|平仓笔数|净额/元|已实现最大回撤/元|正收益月|最严压力净额/元|EG占净额|化工占净额|','|---|---:|---:|---:|---:|---:|---:|---:|']
    for name,z in results['original']['B5'][m.PRIMARY].items():
        lines.append(f"|{LABELS[name]}|{z['n']}|{z['net']:,.2f}|{z['realized_cash_drawdown']:,.2f}|{z['positive_warning_months']}/14|{stress['B5'][name]['min_net']:,.2f}|{z['EG_share_of_known_net']:.1%}|{z['chemical_share_of_known_net']:.1%}|")
    lines+=['','占比为本批单手已知净额贡献占比，不是资金权重，也不是风险贡献或保证金占比。','',
        '## 简单B2对照','', '|名单|平仓笔数|净额/元|已实现最大回撤/元|最严压力净额/元|','|---|---:|---:|---:|---:|']
    for name,z in results['original']['B2'][m.PRIMARY].items():
        lines.append(f"|{LABELS[name]}|{z['n']}|{z['net']:,.2f}|{z['realized_cash_drawdown']:,.2f}|{stress['B2'][name]['min_net']:,.2f}|")
    lines+=['','## 已有日收益相关性','',
        '以下沿用前次2026年实际主力收盘对数收益率统计，剔除跨换月及缺失相邻交易日，不能直接视为15分钟策略或持仓浮动盈亏相关。',
        '', '|组合|历史相关系数|','|---|---:|']
    corr=m.old.read(m.REPO/'research-runs/swing-requested-six-products-20261026-v1/market-return-correlations.json')['pairs']
    for key in ['EG0/PP0','EG0/V0','PP0/V0','EG0/JD0','EG0/SR0','EG0/CJ0']:
        lines.append(f"|{key}|{corr[key]['all2026']['pearson']:.3f}|")
    lines+=['','## 如何使用这些比较','',
        '- 非化工当前回测不强，表示这套策略尚未充分证明其优势，不能推断品种长期不适合交易。交易数受过滤门槛影响，不能直接充当波动率排名。',
        '- 降低EG/PP同时出现可以降低某种共同敞口，但删掉PP也可能让剩余利润更依赖EG；利润集中、市场相关性和同时持仓风险应分开看。',
        '- JD/SR/CJ交易少，名单中保留它们并不代表每天都有非化工风险来分散化工持仓；没有信号时不能为了分散而强行入场。',
        '- 本批单手不是等风险。小组合更低的已实现回撤不能证明等风险或等资金条件更优。没有浮动权益、保证金、极端跳空和账户级风险验证，不据此指定实际资金比例。',
        '- 前次EG/PP同向硬性二选一实验全部8组降低收益且没有改善已实现回撤；因此风险预算应另作前瞻验证，不把高相关直接转成该硬限制。',
        '', '## 本批搭配建议','',
        '如果优先减少化工共同风险，先把EG＋PVC＋JD＋SR作为固定的前瞻观察组合，PP保留备选，CJ保留影子记录。这里的JD/SR仍须满足原规则，不为了凑非化工仓位而放宽或强制交易。这个建议是上述有限历史对照后的选择，不是新的独立验证。',
        '', '该四品种B5相对原六品种主场景净额下降约29%，已实现回撤下降约42%；最严压力净额7,456.47元对原六品种7,770.47元。但EG占该组合净额86.2%，化工贡献92.7%，不能称为已得到多个独立且均衡的收益来源。较低回撤主要应理解为减少了部分共同机会和敞口，尚未证明JD/SR在化工亏损时起到了对冲作用。',
        '', '若更重视保留主场景收入，则五品种EG＋PP＋PVC＋JD＋SR可作为另一观察对照：净额20,150.01元，回撤5,975.25元，最严压力净额9,136.47元。它没有降低主场景回撤，化工利润占比94.2%，不应把它包装成更分散的组合。',
        '', '同时不要将EG/PP/PVC各一手当成三份独立风险。下一步更值得验证的是按计划止损距离×合约乘数×手数计量化工组的合计风险预算；名单可以保留多个化工机会，实际持仓共同风险应整体看待。预算数值及其对收益的影响尚未回测，不能宣称一定改善。分散目标是控制共同敞口，见[CME关于组合分散与行业暴露的说明](https://www.cmegroup.com/education/courses/understanding-the-benefits-of-futures/the-benefits-of-portfolio-diversification)。',
        '', '当前22品种中尚未充分验证一个高频、稳定盈利、同时与化工独立的非化工主力。不能因为非化工当前回测弱就断言它们没有机会，也不宜为填满板块配置而增加未验证交易。若希望真正降低对化工行情的依赖，需要继续验证非化工策略适配或不同策略来源，单靠改名单不足以保证做到。',
        '', '完整逐月、各品种与8场景结果见[results.json](results.json)，控制核验见[checks.json](checks.json)。']
    (RUN/'summary.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')


if __name__=='__main__':run()
