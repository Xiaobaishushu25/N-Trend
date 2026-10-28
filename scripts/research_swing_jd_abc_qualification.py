"""One sequential control: S1 entry qualification, farther A-leg exit target."""
import collections
import copy
import datetime as dt
import json
import shutil
import subprocess
import research_swing_jd_abc_execution as b
RUN=b.RUN/'qualification-split'
READ=b.READ
EV=b.EV
def write(n,x):b.z.s.m.old.write(RUN/n,x)

def run():
    RUN.mkdir(exist_ok=False)
    p=READ(b.RUN/'protocol.json');t=copy.deepcopy(p['templates']['TOUCH_BTRUE']);t['name']='S1QUAL_AEXIT'
    write('protocol.json',dict(created_at=dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(),
        purpose='After target attribution revealed losses concentrated in newly admitted plans, freeze one entry/exit qualification split before viewing its results. Sequential exploratory, not independent OOS.',
        template=t,entry='TOUCH_BTRUE unchanged; grade/body,risk,cost-space and both nominal/actual fill netRR use ORIGINAL S1 target.',
        exit='max directionally(originalS1, entireB extreme+direction*A_move), known at original warning. No no-follow,60bucket maxhold and original guards.',
        no_new_grid=True,primary=p['primary'],script_sha256=b.z.s.m.old.sha(__file__)))
    fork=RUN/'fork';shutil.copytree(b.RUN/'fork',fork);lib=fork/'crates/n-core/src/v3/ablation.rs';s=lib.read_text(encoding='utf-8')
    replace=b.z.s.m.old.replace
    s=replace(s,'pub struct ResearchRules {','pub struct ResearchRules {\n    pub entry_qualification_target: Option<f64>,')
    s=replace(s,'    fn expected_entry(self, ev: &ReplayEvent, slip: f64, sign: f64) -> f64 {','    fn entry_target(self,ev:&ReplayEvent,fill:f64,sign:f64)->f64{self.entry_qualification_target.unwrap_or_else(||self.target(ev,fill,sign))}\n    fn expected_entry(self, ev: &ReplayEvent, slip: f64, sign: f64) -> f64 {')
    s=replace(s,'g.allows_target(ev, fill, self.rules.target(ev, fill, sign), self.multiplier)','g.allows_target(ev, fill, self.rules.entry_target(ev, fill, sign), self.multiplier)')
    s=replace(s,'g.allows_target(ev, proposed, rules.target(ev, proposed, sign), multiplier)','g.allows_target(ev, proposed, rules.entry_target(ev, proposed, sign), multiplier)')
    tests='''
    fn qualification_case(target:Option<f64>,bars:&[Kline])->ResultRow{
        let mut e=event(false,3.);e.target=140.;
        evaluate_research(&e,bars,&[],&ExecutionConfig::default(),Experiment{policy:Policy::OriginalS1,no_follow_bar:None,slip_ticks:1.,fee_ticks:0.},10.,Some(guard()),ResearchRules{entry_qualification_target:target,..Default::default()})
    }
    #[test]fn qualification_original_space_rejects_even_with_distant_exit(){let r=qualification_case(Some(112.),&[bar(46,100.,101.,99.,100.)]);assert_eq!(r.evaluation.status,"filtered_net_rr_plan");assert!(r.evaluation.entry_ts.is_none());}
    #[test]fn qualification_actual_fill_rechecks_original_space(){let r=qualification_case(Some(116.),&[bar(46,101.,103.,100.,102.)]);assert_eq!(r.evaluation.status,"filtered_net_rr_fill");assert!(r.evaluation.entry_ts.is_none());}
    #[test]fn qualification_does_not_cap_the_farther_exit(){let r=qualification_case(Some(120.),&[bar(46,100.,101.,99.,100.),bar(47,101.,141.,100.,140.)]);assert_eq!(r.evaluation.status,"closed");assert_eq!(r.evaluation.final_price,Some(139.));}
'''
    s=replace(s,'mod tests {\n    use super::*;','mod tests {\n    use super::*;\n'+tests);lib.write_text(s,encoding='utf-8')
    runner=fork/'crates/n-core/src/bin/swing_jd_abc_execution.rs';s=runner.read_text(encoding='utf-8')
    s=replace(s,'room_covers_cost_limit(planning_entry,ev.target,','room_covers_cost_limit(planning_entry,snap["mechanism"]["original_S1"].as_f64().unwrap_or(ev.target),')
    s=replace(s,'                rules.min_actual_risk_ticks=Some(min_risk);','                rules.min_actual_risk_ticks=Some(min_risk);\n                rules.entry_qualification_target=snap["mechanism"]["original_S1"].as_f64();')
    (fork/'crates/n-core/src/bin/swing_jd_abc_qualification.rs').write_text(s,encoding='utf-8')
    args=['--release','--offline','--manifest-path',str(fork/'Cargo.toml'),'--target-dir',str(b.ROOT/'target/swing-jd-abc-execution-isolated'),'-p','n-core']
    subprocess.run(['cargo','test',*args,'--lib','v3::ablation::tests::qualification_'],check=True)
    subprocess.run(['cargo','build',*args,'--bin','swing_jd_abc_qualification'],check=True)
    exe=b.ROOT/'target/swing-jd-abc-execution-isolated/release/swing_jd_abc_qualification.exe';write('binary.json',dict(path=str(exe),sha256=b.z.s.m.old.sha(exe)))
    meta=READ(b.RUN/'signal-metadata.json');data=collections.defaultdict(list);fees=READ(b.ROOT/'scripts/exchange_fees_20261022.json')
    for period in ['early','recent']:
        dest=RUN/period;dest.mkdir();pool=copy.deepcopy(READ(b.RUN/period/'seed'/(period+'-pool.json')))
        for snap in pool:
            e=snap['event'];sign=1 if e['direction']=='up' else -1;old=e['target'];projection=snap['entry_quality']['b_true_extreme']+sign*e['a_move'];target=max(old,projection) if sign>0 else min(old,projection)
            e['target']=b.z.grid(target,e['tick_size'],sign<0);e['rr']=abs(e['target']-e['entry'])/abs(e['risk']) if e['risk'] else 0.;snap['mechanism']['original_S1']=old
        ep=READ(b.RUN/period/'seed/execution.protocol.json');ep['templates']=[t]
        b.z.s.m.old.write(dest/(period+'-pool.json'),pool);b.z.s.m.old.write(dest/'execution.protocol.json',ep)
        subprocess.run([str(exe),str(dest/'execution.protocol.json'),period,'replay',str(dest/'executions')],check=True)
        cal=b.z.s.TradingDates({});cal.dates=READ(b.RUN/period/'seed/trading-dates.json')
        for line in (dest/'executions/evaluations.jsonl').open(encoding='utf-8'):
            r=b.z.s.reprice(json.loads(line),fees,cal);r['period']=period;sf=r['scenario'].split('/',1)[1];data[sf].append(r)
    c,_,_,ends,_,_,_=b.load();cases=[pol+'/'+sf for pol in p['policies'] for sf in p['scenarios']];results={};accepted={}
    for case in cases:
        pol,sf=case.split('/',1);aa,ll=b.admit(data[sf],ends,pol,c,meta);q=b.z.metric(aa)
        q['blocks']={f['label']:b.z.metric([r for r in aa if f['start']<=r['warning_ts']<f['end']]) for f in b.z.x.blocks()};results[case]=q;accepted[case]=aa
    write('results.json',results)
    with (RUN/'accepted-primary.jsonl').open('x',encoding='utf-8') as f:
        for r in accepted[p['primary']]:f.write(json.dumps(r,ensure_ascii=False)+'\n')
    checks=entry_checks(data)
    write('entry-equivalence.json',checks)
    q=results[p['primary']]
    lines=['# 原S1审核资格，A段等幅管理退出','',
        '这是看过A投射目标归因后固定的单一对照，属于顺序探索。没有把旧成交/盈利标签拿来筛选：警报时成本空间、计划净盈亏比及实际成交净盈亏比均用原S1，退出允许到预警时已知的更远目标。','',
        '|主场景平仓|净额|8场景最低净额|归一净额|8场景最低归一|最多未知|','|---:|---:|---:|---:|---:|---:|',
        f"|{q['n']}|{q['net']:.2f}|{min(v['net'] for v in results.values()):.2f}|{q['normalized_net']:.2f}|{min(v['normalized_net'] for v in results.values()):.2f}|{max(v['unknown'] for v in results.values())}|",'',
        '|Q1训练|Q2|Q3覆盖段|10月部分|删除最佳3笔后归一|','|---:|---:|---:|---:|---:|',
        '|'+ '|'.join(f"{q['blocks'][n]['net']:.2f}" for n in ['Q1_train','Q2','Q3','October_partial'])+f"|{q['normalized_ex_top3']:.2f}|",'',
        '同一套警报上的个别入场与原S1组合核验，具体见[entry-equivalence.json](entry-equivalence.json)。退出更远会延长占用、改变后续竞争，不能把个别交易增量简单加到原组合上。旧日期都已研究过，不可称作独立OOS或保证未来收益。','']
    (RUN/'summary.md').write_text('\n'.join(lines),encoding='utf-8')
    for n,h in p['production_before'].items():assert b.z.s.m.old.sha(b.ROOT/n)==h
    write('completion.json',dict(completed=True,production_changed=False,not_independent_OOS=True,artifacts={q.name:b.z.s.m.old.sha(q) for q in RUN.glob('*.json') if q.name!='completion.json'},report_sha256=b.z.s.m.old.sha(RUN/'summary.md')))
    print(json.dumps(dict(primary=q,worst=min(v['net'] for v in results.values()),checks=checks),ensure_ascii=False),flush=True)

def entry_checks(data):
    compared=0;no_entry=0;entries=0
    for period in ['early','recent']:
        old={}
        for line in (b.RUN/period/'seed/executions/evaluations.jsonl').open(encoding='utf-8'):
            r=json.loads(line)
            if r['scenario'].startswith('TOUCH_BTRUE/'):old[r['event_key'],r['scenario'].split('/',1)[1]]=r
        for sf,rows in data.items():
            for r in rows:
                if r['period']!=period:continue
                q=old[r['event_key'],sf];qe=EV(q);re=EV(r)
                def fill(e,row):
                    if e['entry_ts'] is not None:return e['entry_ts'],e['fill'],e['risk']
                    d=row['result']['disagreement']
                    if d:
                        ps=[d['open_high_low_close'],d['open_low_high_close']]
                        vals=[(p['entry_ts'],p['fill'],p['risk']) for p in ps]
                        if vals[0]==vals[1] and vals[0][0] is not None:return vals[0]
                    return None
                assert fill(qe,q)==fill(re,r),(r['event_key'],sf,fill(qe,q),fill(re,r))
                if fill(qe,q) is None:no_entry+=1
                else:entries+=1
                compared+=1
    return dict(exact_same_individual_entries=compared,entry_cases=entries,no_entry_cases=no_entry,
        no_outcome_labels_used_for_qualification=True,entry_gate_target_S1_exit_target_separate=True)

if __name__=='__main__':run()
