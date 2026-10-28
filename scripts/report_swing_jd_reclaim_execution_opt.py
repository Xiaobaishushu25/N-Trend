"""Bidirectional planned2R execution optimization, attribution andfloatingrisk."""
import collections
import json
import research_swing_jd_reclaim_f2s8 as y

x=y.x
b=x.b
R=x.R
RUN=x.RUN
T={'BASE':'冻结双向合并版','ENTRY_HOLD':'成交前保持S0','REFAIL1':'一次收盘再失守退出','REFAIL2':'两次收盘再失守退出','STALE8':'第8桶无推进退出','BE1':'达到1R后保护','F2S8':'两次再失守＋第8桶无推进','E20_ONLY':'完整E20对照'}
def f(v):return '—' if v is None else f'{v:,.2f}'

def equity(rows,price_maps,fees):
    charge=lambda leg,price:fees[leg]['fixed_yuan_per_lot']+fees[leg]['notional_rate']*price*10
    cash=norm=peak=npeak=dd=ndd=0.;points=[];last_end='';openrecord=None
    def mark(ts,value,nvalue,kind):
        nonlocal peak,npeak,dd,ndd
        peak=max(peak,value);npeak=max(npeak,nvalue);dd=max(dd,peak-value);ndd=max(ndd,npeak-nvalue);points.append(dict(ts=ts,equity=value,normalized_equity=nvalue,kind=kind))
    entered=sorted([r for r in rows if x.EV(r).get('entry_ts')],key=lambda r:(x.EV(r)['entry_ts'],r['event_key']))
    for row in entered:
        e=x.EV(row);assert e['status'] in ['closed','unfinished'];assert not last_end or last_end<e['entry_ts'];sign=1 if row['direction']=='up' else -1;qty=300/(row['planned_risk']*10);entry=e['fill'];raw=price_maps[row['period']][row['execution_contract']]
        selected=[q for q in raw if e['entry_ts']<=q['datetime'] and (e['exit_ts'] is None or q['datetime']<e['exit_ts'])]
        for q in selected:
            close=q['close']-sign*row['tick_size']*row['slip'];floating=(close-entry)*sign*10-charge('open',entry)-max(charge('close_today',close),charge('close_yesterday',close));mark(q['datetime'],cash+floating,norm+floating*qty,'marked')
        if e['status']=='closed':cash+=e['net_amount'];norm+=e['net_amount']*qty;mark(e['exit_ts'],cash,norm,'settled');last_end=e['exit_ts']
        else:
            assert selected;last_end=raw[-1]['datetime'];openrecord=dict(key=row['original_event_key'],last_quote=selected[-1]['datetime'],floating=floating,normalized_floating=floating*qty)
    return dict(closed_net=cash,closed_normalized_net=norm,cash_minute_close_dd=dd,normalized_minute_close_dd=ndd,open_position=openrecord),points

def run():
    p=R(RUN/'protocol.json');primary=p['primary'];main=R(RUN/'main-results.json');combo=R(y.RUN/'main-results.json');main.update(combo);cases=sorted({k.rsplit('/',1)[0] for k in main});models=x.MODELS[:-1]+['F2S8','E20_ONLY'];rows=collections.defaultdict(list)
    for root in [RUN,y.RUN]:
        for line in (root/'accepted-main-primary.jsonl').open(encoding='utf-8'):
            z=json.loads(line);rows[z['model']].append(z['row'])
    summary={}
    for m in models:
        q=main[primary+'/'+m];xs=[main[case+'/'+m] for case in cases];summary[m]=dict(primary=q,min16cash=min(v['net'] for v in xs),min16normalized=min(v['normalized_net'] for v in xs),max16unknown=max(v['unknown'] for v in xs))
    # Descriptive same-opportunity attribution, notfilteringhistoricalwinners.
    base={(r['module'],r['original_event_key']):r for r in rows['BASE'] if x.EV(r)['status']=='closed'};cash=lambda r:x.EV(r)['net_amount'];attribution={}
    for m in models:
        if m in ['BASE','E20_ONLY']:continue
        current={(r['module'],r['original_event_key']):r for r in rows[m] if x.EV(r)['status']=='closed'};common=current.keys()&base.keys();added=current.keys()-base.keys();removed=base.keys()-current.keys();changes=[]
        for key in sorted(common):
            old,new=base[key],current[key];delta=cash(new)-cash(old)
            if abs(delta)>1e-7:changes.append(dict(parent_key=key[1],module=key[0],warning_ts=new['warning_ts'],direction=new['direction'],old_exit=x.EV(old)['exit_reason'],new_exit=x.EV(new)['exit_reason'],old_exit_ts=x.EV(old)['exit_ts'],new_exit_ts=x.EV(new)['exit_ts'],old_net=cash(old),new_net=cash(new),net_delta=delta))
        q=dict(common_n=len(common),added_n=len(added),added_net=sum(cash(current[k]) for k in added),removed_n=len(removed),removed_old_net=sum(cash(base[k]) for k in removed),changed_common=changes,common_delta=sum(z['net_delta'] for z in changes),gain_by_direction={d:sum(z['net_delta'] for z in changes if z['direction']==d) for d in ['up','down']})
        delta=summary[m]['primary']['net']-summary['BASE']['primary']['net'];assert abs(delta-q['common_delta']-q['added_net']+q['removed_old_net'])<1e-6;attribution[m]=q
    x.write('opportunity-attribution.json',attribution)
    # Same minute-close liquidation-equity convention asprevioussavedrisk audit.
    x.write('mtm-protocol.json',dict(method='Actualmember1mclose fromentrytoexit-exclusive, actualsettlednetatexits; subtractpaidopenfee andhypotheticalexitfee/adverse1tick. Noentryminutepriorwicks. Fractional300plannedriskalsoreported. NotfulltickDD/accountROI.',
        original_method_sha256=x.sha(x.r.RUN/'mtm/protocol.json'),models=models,source_script_sha256=x.sha(__file__)))
    maps={};sources={};external=R(x.r.SNAP/'external-sources.json')
    for per in ['early','recent']:
        path=x.r.f.t.period_paths(per)[0]['contracts'];assert x.sha(path)==external[path];maps[per]={k:v['minutes'] for k,v in R(path).items() if k.startswith('DCE.jd')};sources[path]=x.sha(path)
    fees=R(x.r.SNAP/'fees.json')['products']['JD'];mtm={};curves=[]
    for m in models:
        result,points=equity(rows[m],maps,fees);mtm[m]=result
        assert abs(result['closed_net']-summary[m]['primary']['net'])<1e-6;assert abs(result['closed_normalized_net']-summary[m]['primary']['normalized_net'])<1e-6
        for q in points:curves.append(dict(model=m,**q))
    with (RUN/'mtm-equity.jsonl').open('x',encoding='utf-8') as out:
        for q in curves:out.write(json.dumps(q,ensure_ascii=False)+'\n')
    x.write('mtm-results.json',mtm);x.write('mtm-sources.json',sources)
    # ParentAclustersarethetailunit, notnecessarilyindependentindividualtrades.
    _,meta,_,_,_=x.load();concentration={};direction={}
    for m in models:
        closed=[r for r in rows[m] if x.EV(r)['status']=='closed'];groups=collections.defaultdict(lambda:dict(cash=0.,normalized=0.,n=0))
        for row in closed:
            group=groups[meta[row['original_event_key']]['cluster']];group['n']+=1;group['cash']+=cash(row);group['normalized']+=cash(row)*300/(row['planned_risk']*row['multiplier'])
        q=summary[m]['primary'];concentration[m]=dict(ex_best3_A_cash=q['net']-sum(sorted([max(0,v['cash']) for v in groups.values()],reverse=True)[:3]),ex_best3_A_normalized=q['normalized_net']-sum(sorted([max(0,v['normalized']) for v in groups.values()],reverse=True)[:3]))
        for d in ['up','down']:direction[m+'/'+d]=b.z.metric([v for v in rows[m] if v['module']=='S0_RECLAIM_R2' and v['direction']==d])
    x.write('concentration.json',concentration);x.write('module-direction-results.json',direction)
    x.write('comparison-summary.json',summary)
    lines=['# 双向收回A起点＋计划2R：入场与退出优化','',
        '2026-10-27。遵照本轮方向：停止固定做多探索，完整双向合并版为基线，原E20规则保持完整。已保存的jd-abc-reclaim-observation-v2快照未改，生产规则未改。','',
        '## 固定实验','',
        '|方案|唯一改变|','|---|---|',
        '|成交前保持S0|从信号到成交，若实际观察价回到S0或越过S0失效侧，取消该订单；延迟成交前分钟内触及也取消|',
        '|一次／两次再失守退出|只统计成交后完整15分钟柱，收盘越过S0失效侧至少1tick；一次或连续两次触发后，下一实际分钟开盘退出。两次要求相邻15分钟，休市间隔重置|',
        '|第8桶无推进退出|原生规则：持仓第8桶收盘，最大浮盈不足0.5实际R且收盘弱于原计划入场价，按观察收盘扣滑点退出；入场桶可含部分柱，沿用既有时间退出执行假设|',
        '|达到1R后保护|达到1实际R后将保护止损提高到毛入场价，计划2R目标不变；滑点与费用保留，不能称为零成本保本|','',
        '五个单项方案在回放前固定，没有固定方向限制或新趋势筛选。目标始终是原决策时的计划2R，初始结构止损和全部费用、风险、节假日、最长持有条件保持。每项改变仅作用于新增模块。','',
        '观察单项结果后，追加唯一一个组合：两次再失守＋第8桶无推进。两种失效触发先到先退出，没有加入1R保护，也没有扫描所有组合。这是明确的顺序探索，不是未触碰样本外。','',
        '## 主历史连续占仓结果','',
        '仍为2026年1—6月、7月1—10日、8月10日—10月23日11:05的已有覆盖段，缺失区间不补造机会。以下金额是一手扣冻结手续费和滑点后的已平仓元。16场景为1—4tick/次成交、正常与额外一分钟入场延迟、两种未知占仓政策。各方案合并全部原始订单后重新竞争一单/一仓，同A两次已实现止损限制共享。','',
        '|方案|平仓数|主净额|16场景最低净额|最低统一风险净额|最多未知|','|---|---:|---:|---:|---:|---:|']
    for m in models:
        q=summary[m];lines.append(f"|{T[m]}|{q['primary']['n']}|{f(q['primary']['net'])}|{f(q['min16cash'])}|{f(q['min16normalized'])}|{q['max16unknown']}|")
    lines+=['','统一风险是假设每笔计划风险300元的研究归一，非账户收益或可执行小数手。未知不按0元盈利填入；各主要候选仍有1笔数据结束未平仓。','',
        '|方案|Q1|Q2|Q3覆盖段|10月部分|删最佳3个A后现金|删最佳3个A后统一风险|','|---|---:|---:|---:|---:|---:|---:|']
    for m in models:
        q=summary[m]['primary'];z=concentration[m];lines.append('|'+T[m]+'|'+'|'.join(f(q['blocks'][k]['net']) for k in ['Q1_train','Q2','Q3','October_partial'])+'|'+f(z['ex_best3_A_cash'])+'|'+f(z['ex_best3_A_normalized'])+'|')
    lines+=['','## 改善来自哪些交易','',
        '|方案|共同交易中改变盈亏的笔数|共同交易净额变化|新增平仓数|移除原平仓数|','|---|---:|---:|---:|---:|']
    for m,q in attribution.items():lines.append(f"|{T[m]}|{len(q['changed_common'])}|{f(q['common_delta'])}|{q['added_n']}|{q['removed_n']}|")
    d=attribution['F2S8']['gain_by_direction'];lines+=['',
        '一次失守就退出过于敏感：它减少部分亏损，也提前截断了1月、5月、9月的原盈利交易，整体净额降至2,779元。两次确认保留了这些主要推进机会，代价是部分亏损仍退出较晚。',
        '1R保护仅改变4月22日的一笔原亏损交易，改善约140元；毛入场价保护遇到次日跳空仍亏约90元。不能从这一笔外推稳定保护效果。',
        f"组合F2S8相对冻结版主净额增加约530元；共同机会里做多改善{f(d['up'])}元、做空改善{f(d['down'])}元。双向规则均执行，但做空模块自身并未因此成为稳定盈利策略。共同交易、失去机会与新增机会的完整逐笔归因保存为opportunity-attribution.json。",'',
        '## 浮动权益与风险取舍','',
        '与此前相同，以实际合约一分钟收盘价估计含退出费用与滑点的浮动权益，实际退出使用原生成交净额。仅计入成交后观察；不使用入场前分钟极值。分钟收盘采样不是完整tick最大回撤，未包含保证金强平。','',
        '|方案|平仓统一风险回撤|浮动统一风险回撤|浮动一手现金回撤|','|---|---:|---:|---:|']
    for m in models:
        q=mtm[m];lines.append(f"|{T[m]}|{f(summary[m]['primary']['normalized_realized_dd'])}|{f(q['normalized_minute_close_dd'])}|{f(q['cash_minute_close_dd'])}|")
    lines+=['','## 时间选择与局限','',
        '沿用此前严格的截止日前已平仓训练条件：20笔、每覆盖日0.35笔、16压力归一为正、去最佳笔后为正、每场景不低于冻结合并版，再按最差归一收益及回撤排序。三个季度截止点的选择器均回退BASE；组合F2S8同样未获得采用资格，主要因训练频率及部分训练期不及冻结版。',
        '因此全历史固定方案的改善不能改称为选择器已经取得的独立样本外收益。两次确认和组合在Q1/Q2也有收益牺牲；结果改善仍依赖少数被改变的交易及已有市场阶段。此前新增模块Q2集中、下跌环境样本不足的限制没有被消除。',
        '7月13—24日仍作为已知历史复核单列，不是新独立窗口。初始32个主历史收回决策没有扩充，交易频率未提高到60笔；本轮优化的是失效后退出和风险路径，不通过改方向或凑机会增加笔数。','',
        '## 本轮保留的研究分支','',
        '保留“两次再失守”“第8桶无推进”及其组合为双向计划2R备选；1R保护只留诊断。原完整冻结候选继续作对照，当前不覆盖或部署。后续应在冻结后的新增行情、独立下跌区间及更多原合约预热上，验证改善是否持续，而非进一步扫退出阈值。','',
        '证据：[单项冻结协议](protocol.json) · [全部16场景](main-results.json) · [组合追加协议](F2S8-followup/protocol.json) · [组合场景](F2S8-followup/main-results.json) · [逐机会归因](opportunity-attribution.json) · [浮动权益](mtm-results.json) · [时间选择](chronological-selection.json) · [核验](completion.json)。','']
    (RUN/'summary.md').write_text('\n'.join(lines),encoding='utf-8');plot(summary,mtm)
    # Validate immutablebaseline andallcompiledstudy sourcesbeforecompletion.
    for q,h in R(x.r.SNAP/'manifest.json')['files'].items():assert x.sha(x.r.SNAP/q)==h
    assert p['script_sha256']==x.sha(x.__file__)
    for q,h in R(RUN/'implementation-manifest.json').items():assert x.sha(RUN/'fork'/q)==h
    cp=R(y.RUN/'protocol.json');assert cp['parent_results_sha256']==x.sha(RUN/'main-results.json');assert cp['script_sha256']==x.sha(y.__file__)
    assert all(x.sha(b.ROOT/q)==h for q,h in p['production_before'].items())
    native={};count=0
    for root in [RUN,y.RUN]:
        for per in ['early','recent','july']:
            path=root/per/'executions/evaluations.jsonl';native[path.relative_to(RUN).as_posix()]=x.sha(path)
            for line in path.open(encoding='utf-8'):
                row=json.loads(line);count+=1
                for fill in row['result']['fills']:assert fill['ts']>row['warning_ts'];assert abs(fill['price']/row['tick_size']-round(fill['price']/row['tick_size']))<1e-7
    x.write('research-candidates.json',dict(reference='jd-abc-reclaim-observation-v2',retained=['REFAIL2','STALE8','F2S8'],diagnostic_only=['ENTRY_HOLD','REFAIL1','BE1'],direction='bidirectional',target='unchangedplanned2R',status='research_not_promoted',definitions_sha256=x.sha(RUN/'protocol.json'),combo_sha256=x.sha(y.RUN/'protocol.json')))
    x.write('completion.json',dict(completed=True,production_changed=False,frozen_reference_unchanged=True,untouched_OOS=False,native_rows=count,native_hashes=native,rust_focused_tests=9,
        report_sha256=x.sha(RUN/'summary.md'),source_sha256=x.sha(__file__),artifacts={q.relative_to(RUN).as_posix():x.sha(q) for q in RUN.rglob('*.json') if 'fork' not in q.parts and q.name!='completion.json'}))
    print(json.dumps(dict(main={m:dict(n=summary[m]['primary']['n'],net=summary[m]['primary']['net'],min16=summary[m]['min16cash'],mtmDD=mtm[m]['normalized_minute_close_dd']) for m in ['BASE','REFAIL2','STALE8','BE1','F2S8']},combo_attribution=attribution['F2S8']['gain_by_direction']),ensure_ascii=False))

def plot(summary,mtm):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    plt.rcParams['font.sans-serif']=['Microsoft YaHei'];plt.rcParams['axes.unicode_minus']=False
    fig,axes=plt.subplots(1,2,figsize=(15,6));models=['BASE','ENTRY_HOLD','REFAIL1','REFAIL2','STALE8','BE1','F2S8']
    for i,m in enumerate(models[::-1]):
        q=summary[m];axes[0].plot([q['min16cash'],q['primary']['net']],[i,i],c='#9caec1');axes[0].scatter(q['primary']['net'],i,c='#21816e',s=35)
    axes[0].axvline(0,c='#999999',lw=.8);axes[0].set_yticks(range(len(models)),[T[m] for m in models[::-1]],fontsize=9);axes[0].set_title('双向＋计划2R：一手已平仓净额');axes[0].set_xlabel('元；线段左端为16场景最低值，未知另计')
    for m in ['BASE','REFAIL2','STALE8','F2S8']:
        q=summary[m]['primary'];months=list(q['month_cash']);ys=[];total=0
        for mo in months:total+=q['month_cash'][mo];ys.append(total)
        axes[1].plot(months,ys,marker='o',ms=3,label=T[m])
    axes[1].axhline(0,c='#999999',lw=.8);axes[1].tick_params(axis='x',rotation=55);axes[1].set_title('按决策月份归属的累计已平仓净额');axes[1].legend(fontsize=8);axes[1].set_ylabel('元');fig.tight_layout();fig.savefig(RUN/'comparison.png',dpi=160);plt.close(fig)

if __name__=='__main__':run()
