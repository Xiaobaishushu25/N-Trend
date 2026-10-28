"""Save the user-named 最佳a策略 as an immutable research baseline."""
import copy,datetime as dt,json,shutil
import research_swing_wave_selection as w
SNAP=w.REPO/'research-strategies/best-a-v1'

def run():
    source=w.RUN;p=w.read(source/'protocol.json');candidate=w.read(source/'observation-candidate.json');complete=w.read(source/'completion.json')
    assert candidate['id']=='near_start/adaptive' and not candidate['production_changed']
    assert all(w.sha(source/k)==h for k,h in complete['artifacts'].items())
    assert all(w.sha(k)==h for k,h in p['production_before'].items())
    for q,paths in p['periods'].items():assert all(w.sha(path)==p['hashes'][q][k] for k,path in paths.items())
    SNAP.mkdir(parents=True,exist_ok=False);shutil.copytree(source/'fork',SNAP/'fork')
    for name in ('observation-candidate.json','protocol.json','summary.md','completion.json'):
        shutil.copy2(source/name,SNAP/('source-'+name))
    for q in ('early','recent'):shutil.copy2(source/'near_start'/(q+'-pool.json'),SNAP/(q+'-pool.json'))
    params=copy.deepcopy(p);params.update(id='best-a-v1',display_name='最佳a策略',selector=2,templates=[next(t for t in p['templates'] if t['variant']=='adaptive')],
        source_candidate_sha256=w.sha(source/'observation-candidate.json'),production_changed=False,qualified_for_trading=False)
    w.write(SNAP/'protocol.json',params)
    full=w.read(source/'results.json');results={policy:{q:{case.removeprefix('near_start/adaptive/'):m for case,m in cases.items() if case.startswith('near_start/adaptive/')} for q,cases in periods.items()} for policy,periods in full.items()};w.write(SNAP/'results.json',results)
    with (SNAP/'accepted.jsonl').open('x',encoding='utf-8') as out:
        for line in (source/'accepted.jsonl').open(encoding='utf-8'):
            if json.loads(line)['case'].startswith('near_start/adaptive/'):out.write(line)
    (SNAP/'bin').mkdir()
    for name in ('swing_wave_pool.exe','swing_wave_execution.exe'):
        shutil.copy2(w.REPO/'target/release'/name,SNAP/'bin'/name)
    w.write(SNAP/'definition.json',dict(id='best-a-v1',display_name='最佳a策略',version=1,created_at=dt.datetime.now(dt.timezone.utc).isoformat(),
        user_named=True,status='frozen_research_observation',qualified_for_trading=False,independent_holdout=False,
        source='near_start/adaptive',source_run=str(source),source_candidate_sha256=w.sha(source/'observation-candidate.json'),
        warning='User-assigned name; not a claim of optimality, all-cycle stability or live qualification.',
        rules=dict(A='Known completed15m prefixes from corrected5m aggregation; existing confirmed fine(window2/merge8) and large(window5/merge10) starts. Among ABC/nonmicro/strongA>=1/no-rollover eligible candidates, latestS0 first then original score/ties, same priority across levels. No extra A-breakout.',
            setup='Service forward dedup before score>=2.5; no risk/ATR band; min10tick; nominal round-trip friction<=0.2R; projected and actual guarded net reward/loss>=1.',
            execution='Original warning entry and S2 stop (warning extreme plus original buffer), prior12 complete15m nearest eligible confirmed pivot target capped byS1; no target capR. Hourly alignment<=0: strong/wick completed1m close then next1m open, cumulative touch. Hourly<=-1: block entries in last10min before long break and close gross-losing positions next1m open.',
            lifecycle='Pending12 observed15m buckets; no-follow on fifth held bucket if MFE<0.5R; time horizon60 held buckets. Existing calendar/weekend/holiday guards and actual fees; no new state retirements.',
            capacity='One-symbol capacity at trigger time, both warning periods merged. Unknown PnL separate; original contract-end reservation and both-paths-flat release sensitivity both retained.',
            validation='corrected_touch/next_minute_open x each-side1/2tick; next_minute_open is extra-minute latency, not actual next quote. Single-lot realized cash/R, no account equity/size claim.'),
        excluded='Do not include five failed retirement/owner rules or later exploratory modifications in this named baseline.'))
    manifest={f.relative_to(SNAP).as_posix():w.sha(f) for f in SNAP.rglob('*') if f.is_file()};w.write(SNAP/'manifest.json',manifest)
    lines=['# 最佳a策略','', '用户指定名称；固定版本best-a-v1，对应near_start/adaptive。状态：研究观察版，未改实盘，尚未实现全周期稳定。后续探索另存版本，不覆盖本策略。',
        '', '核心是优先选择最近满足原有结构资格的A，保留原反转警报与strong_kind_session执行。没有加入上一轮失败的五种“失效后禁止旧A”规则。完整规则见definition.json，可执行参数见protocol.json；源码、预警池、逐笔已接纳交易、四场景结果和二进制均已快照，manifest.json锁定文件哈希。',
        '', '|执行压力|上半年净额|平均净R|近期净额|平均净R|','|---|---:|---:|---:|---:|']
    for case,e in results['original']['early'].items():
        n=results['original']['recent'][case];lines.append(f"|{case}|{e['net']:.2f}|{e['mean_r']:.4f}|{n['net']:.2f}|{n['mean_r']:.4f}|")
    lines+=['', '以上为每手已平仓净额、扣实际手续费，未知收益另列。1tick上半年444笔、未知0笔，近期178笔、未知1笔。近期不同品种起点不齐，非完整等覆盖四个月。所有历史均已经反复研究，不能当成独立泛化验证。',
        '', '策略快照：research-strategies/best-a-v1/。重放使用该目录bin/swing_wave_execution.exe，参数为protocol.json、early或recent、replay、新输出目录；原行情按协议路径和哈希引用，没有重复复制大型原始行情。']
    doc=w.REPO/'docs/最佳a策略.md';assert not doc.exists();doc.write_text('\n'.join(lines)+'\n',encoding='utf-8')
    w.write(w.REPO/'research-protocols/best-a-strategy-20261025-v1.json',dict(display_name='最佳a策略',id='best-a-v1',snapshot=str(SNAP),manifest_sha256=w.sha(SNAP/'manifest.json'),definition_sha256=w.sha(SNAP/'definition.json'),documentation=str(doc),documentation_sha256=w.sha(doc),production_changed=False,qualified_for_trading=False))
    print('Frozen 最佳a策略:',SNAP,'files',len(manifest),flush=True)

if __name__=='__main__':run()
