"""Answer direction-overfit question withpast-onlyregimes, no selectednewgate."""
import collections
import datetime as dt
import json
import audit_swing_jd_reclaim_roll_neutral as n

a=n.a
r=n.r
R=n.R
RUN=a.RUN
def f(x):return f'{x:,.2f}'

def run():
    p=R(r.RUN/'protocol.json');primary=p['primary'];cases=[pol+'/'+mode+'/slip'+str(s) for pol in p['policies'] for mode in ['corrected_touch','next_minute_open'] for s in p['slips']]
    ctl,meta,data,ends,checks=r.load();same={q['key']:q['daily10']['state'] for q in R(RUN/'opportunities.json')};neutral={q['key']:q['state'] for q in R(n.RUN/'opportunities.json')};stress={}
    for case in cases:
        policy,suffix=case.split('/',1)
        for variant in ['FULL','LONG_ONLY','SHORT_ONLY']:
            aa,_=r.admit_variant(data,suffix,variant,ctl,meta,ends,policy,False)
            for definition,lookup in [('same_contract_daily10',same),('roll_neutral_daily10',neutral)]:
                for state in ['UP','DOWN','RANGE','UNKNOWN']:
                    rows=[q for q in aa if q['module']=='S0_RECLAIM_R2' and lookup[q['original_event_key']]==state];stress[case+'/'+variant+'/'+definition+'/'+state]=r.b.z.metric(rows)
    a.write('regime-stress-results.json',stress)
    results=R(RUN/'regime-results.json');roll=R(n.RUN/'regime-results.json');time=R(RUN/'time-direction-results.json');coverage=R(RUN/'market-state-coverage.json');rollcov=R(n.RUN/'market-coverage.json')
    longs=[time['FULL/'+q+'/up'] for q in ['Q1_train','Q2','Q3','October_partial']];without_q2=sum(q['net'] for i,q in enumerate(longs) if i!=1)
    down={}
    for definition in ['same_contract_daily10','roll_neutral_daily10']:
        key=lambda case:case+'/LONG_ONLY/'+definition+'/DOWN';down[definition]=dict(primary=stress[key(primary)],min16cash=min(stress[key(case)]['net'] for case in cases),min16normalized=min(stress[key(case)]['normalized_net'] for case in cases),max16unknown=max(stress[key(case)]['unknown'] for case in cases))
    a.write('comparison-summary.json',dict(long_new_ex_Q2_net=without_q2,down_long=down,market_known_same_contract=sum(q['daily10']['state']!='UNKNOWN' for q in coverage),market_known_roll_neutral=sum(q['roll_neutral_daily']['state']!='UNKNOWN' for q in rollcov)))
    # A reviewable next-step protocol, not scheduled task orproductionchanges.
    a.write('next-validation-protocol.json',dict(created_at=dt.datetime.now().isoformat(),status='research_protocol_only',
        frozen_reference=str(r.SNAP),snapshot_manifest_sha256=r.sha(r.SNAP/'manifest.json'),
        comparisons=['E20_ONLY','FULL','LONG_ONLY'],direction_rule='Do notpromoteLONG_ONLYorcreateUP-long/DOWN-short switch fromthishistory. All3definitionsfrozen; E20componentalwaysfullunchanged.',
        sampling='Firstscoreafterthealreadyrecordedcandidatefreeze; prospectivewarningsandminutequotesmustbeavailableonlyasofdecision. Existingpostfreeze-butretrospectivelysupplieddata separatelylabelednewunseenhistorical, notliveprospective. Olderindependentdowntrendsneedactualsamecontractminutes andfullwarmup, notsignals-onlyarchives.',
        records=['originalABCseed_andscore','actualcontract_androllover','firstS0failure_orcancel','firstreclaim_ornoreclaim','decision_clock','planned_stop_andtarget','economics_rejection','raw_native_fill_orunknown','shared_occupancy_admission','parentAcluster','past_only_regimes'],
        market_labels='Freeze10dayrollneutralstateasbroadcoverageprimarylabelandsamecontract10day+30quarterlabelassensitivity. Labelsareforreportingonly, nevernewtradegate. RetainUNKNOWN; no choosingfavorablelabelafteroutcomes.',
        evaluation='Reportdirection x UP/DOWN/RANGE/UNKNOWN: ALLproposals,economicallyeligible,admitted,closed,unknown,cash,plannedrisk-normalized,16executionstresses,minutecloseMTMDD,exbestA/week. Compare incrementalFULLandLONG_ONLYagainstE20withcontinuousreadmission, notsumofstandalones.',
        independent_direction_control='Atmatchedcalendar/member/session/volatility/prior-momentum hosts, replayownfixed2Rgeometrythroughnativeengine, notonlyforwardmean. Freezehostalgorithmandsamplingseedbeforeseeingnewperiod; distinguishriskgeometryfromtimingselection.',
        adoption='Noadoptionwithoutmaterialindependentdeclinecoverage. Apositiveaggregateoronewinningdirectioncellisnotpass. Sparsecellsremaininconclusive; noadaptivecutofforparametergridrescue. Frequency60andunknownrequirementsfrompriorprotocolremain.',
        operational='Noautomationstarted,nonotificationtask,noliveorders,noquotesinvented; productionrulesunchanged.'))
    lines=['# 鸡蛋仅做多：行情依赖与下一步验证','',
        '2026-10-26。结论：仅做多存在明显的数据选择和行情阶段依赖风险，不能从本段历史总收益更高就升级为固定方向规则。当前保存的完整双向候选保持不变，单多只是诊断分支。','',
        '## 先区分组合收益与新增模块收益','',
        '此前31笔、净额4,945元是“完整E20S1对照＋新增模块仅做多”的组合；它不等于新增模块自身有31笔稳定盈利。新增做多模块实际12笔已平仓净额2,186元，另1笔未平仓。','',
        '|时间段|新增做多已平仓数|净额|统一风险净额|','|---|---:|---:|---:|']
    for label,q in zip(['Q1','Q2','Q3覆盖段','10月部分'],longs):lines.append(f"|{label}|{q['n']}|{f(q['net'])}|{f(q['normalized_net'])}|")
    lines+=['',f"第二季度4笔赚约2,432元，其余时间合计{f(without_q2)}元。组合四段盈利同时受原E20对照和占仓变化影响，不能证明新增单多模块跨时段稳定。仅做多方向是在观察多空结果后获得关注的，存在选择偏差；即使本轮方向诊断预先列出，也不等于重新获得独立样本外。",'',
        '## 下跌环境里的实际表现','',
        '状态全部按订单决策前已知价格计算，独立于ABC自身A段方向。主要口径使用同一实际合约的前11个完整交易日，计算过去10日净推进/10日平均真实波幅；≥1为上涨推进、≤−1为下跌推进，其余区间。当天尚未完成的日线不使用。另用前30根已完成15分钟柱作较短尺度敏感性。','',
        '同合约日线口径避免了换月跳价，但173个覆盖交易日中有82天的同合约预热不足。看到这个限制后，追加一个描述性换月中性日线指数：换合约时剔除旧收盘到新开盘的整段跳跃，当天只保留新合约真实日内变动，其余日期保留真实同合约收盘变化；缺失交易日重置，不跨归档缺口连接。它不是交易收益，也不是唯一正确的趋势定义。','',
        '|状态口径|上涨日|下跌日|区间日|无法判断日|','|---|---:|---:|---:|---:|']
    for key,qs,field in [('同合约日线10日',coverage,'daily10'),('换月中性日线10日',rollcov,'roll_neutral_daily')]:
        counts=collections.Counter(q[field]['state'] for q in qs);lines.append('|'+key+'|'+'|'.join(str(counts[s]) for s in ['UP','DOWN','RANGE','UNKNOWN'])+'|')
    lines+=['','以下仅分组已有新增做多成交，没有按状态删单再优化。金额为一手扣费已平仓净额；统一风险每笔计划300元是研究比较值。','',
        '|日线口径|状态|新增做多平仓数|净额|统一风险净额|未知|','|---|---|---:|---:|---:|---:|']
    for name,source,prefix in [('同合约',results,'LONG_ONLY/daily10/'),('换月中性',roll,'LONG_ONLY/')]:
        for state in ['UP','DOWN','RANGE','UNKNOWN']:
            q=source[prefix+state+'/S0_RECLAIM_R2'];lines.append(f"|{name}|{a.NAMES[state]}|{q['n']}|{f(q['net'])}|{f(q['normalized_net'])}|{q['unknown']}|")
    lines+=['','两种口径都没有提供可信的下跌行情盈利证据：同合约口径3笔亏872元；换月中性口径4笔现金赚554元，但统一风险只赚26元，仍有1笔未平仓。结果对状态定义和预热可用性敏感，不能挑其中一个口径宣称已经适应下跌。',
        '同样不能机械切换成“下跌只做空”：换月中性下跌环境里的新增做空分支只有2笔，合计亏987元。样本太少，当前规则的多空镜像并未获得对称验证。','',
        '|新增做多处于下跌状态|主场景已平仓净额|16场景最低净额|16场景最低统一风险净额|','|---|---:|---:|---:|']
    for definition,zh in [('same_contract_daily10','同合约日线'),('roll_neutral_daily10','换月中性日线')]:
        q=down[definition];lines.append(f"|{zh}|{f(q['primary']['net'])}|{f(q['min16cash'])}|{f(q['min16normalized'])}|")
    lines+=['','这些仍是旧历史中的少数条件样本，无法推断未来下跌必亏或必赚。S0失守后收回的做多形态是一次反弹/回归尝试；在持续下跌里，短暂收回也可能随后再次破底。形态出现不意味着大环境转为上涨。','',
        '## 下一步执行顺序','',
        '1. **保持完整候选冻结，暂停把“仅做多”当成采用结论。**保留E20对照、完整合并版、单多诊断版三条固定观察，不再根据这段历史切方向或扫趋势阈值。',
        '2. **补独立的下跌数据及换月前预热。**需要原合约分钟数据，沿用15分钟识别、实际分钟成交；既有历史再分组只能做诊断。新增未看过的历史段和冻结后前瞻样本分别标记。未覆盖到的状态不能按0亏损当作通过。',
        '3. **按“方向×上涨/下跌/区间”验增量。**记录全部失守、收回、经济性拒绝、成交及未知；同时比较现金、统一风险、浮动权益回撤、费用压力、去最佳A结构和周。先看是否持续优于完整E20，再看频率。',
        '4. **补原生成交的匹配基准。**前轮只有15个严格匹配信号，未来16柱均值0.79ATR、对照0.74ATR，尚未体现明显额外方向优势。下一阶段应把固定几何和费用放入同状态匹配时点，区分择时增益与风险/退出增益，不能只看收盘方向均值。',
        '5. **下跌覆盖不足时保持研究观察。**当前没有足够证据承诺下跌仍盈利、固定只做多、或达到60笔及EG一半收益；也不把“上涨只多、下跌只空”作为现成答案。','',
        '已把下一阶段需要固定的比较版本、数据记录、状态标签和核验项写入[next-validation-protocol.json](next-validation-protocol.json)。这是研究协议文件，没有启动定时任务或实盘交易。','',
        '证据：[原始状态定义](protocol.json) · [状态分组](regime-results.json) · [时段与方向](time-direction-results.json) · [辅助指数定义](roll-neutral-supplement/protocol.json) · [辅助状态结果](roll-neutral-supplement/regime-results.json) · [下跌场景压力](regime-stress-results.json) · [因果核验](verification.json)。','']
    (RUN/'summary.md').write_text('\n'.join(lines),encoding='utf-8')
    # Frozenreferenceandallsourcehashes remainverified, no strategy mutation.
    for q,h in R(r.SNAP/'manifest.json')['files'].items():assert r.sha(r.SNAP/q)==h
    for paths in R(RUN/'sources.json').values():
        for q,h in paths.items():assert r.sha(q)==h
    assert R(RUN/'protocol.json')['script_sha256']==r.sha(a.__file__)
    assert R(n.RUN/'protocol.json')['script_sha256']==r.sha(n.__file__)
    a.write('completion.json',dict(completed=True,production_changed=False,frozen_candidate_unchanged=True,untouched_OOS=False,source_script_sha256=r.sha(__file__),report_sha256=r.sha(RUN/'summary.md'),
        artifacts={q.relative_to(RUN).as_posix():r.sha(q) for q in RUN.rglob('*.json') if q.name!='completion.json'}))
    print(json.dumps(dict(long_module_ex_Q2_net=without_q2,down_long_stress={k:{name:q[name] for name in ['min16cash','min16normalized','max16unknown']} for k,q in down.items()}),ensure_ascii=False))

if __name__=='__main__':run()
