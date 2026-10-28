"""Save a candidate snapshot before any further changes or robustness studies."""
import datetime as dt
import json
import shutil
import subprocess
import research_swing_jd_reclaim_overlay as o

c=o.c
f=c.f
b=f.b
DEST=b.ROOT/'research-strategies/jd-abc-reclaim-observation-v2'

def run():
    DEST.mkdir(exist_ok=False);write=lambda n,x:b.z.s.m.old.write(DEST/n,x)
    original=f.R(f.RUN/'completion.json')
    for q,h in original['artifacts'].items():assert f.sha(f.RUN/q)==h
    assert f.sha(f.RUN/'summary.md')==original['report_sha256']
    shutil.copytree(f.RUN,DEST/'evidence')
    sources=DEST/'sources';sources.mkdir()
    for name in ['research_swing_jd_abc_execution.py','research_swing_jd_warning_quality.py','research_swing_jd_btail_confirmation.py','research_swing_jd_abc_failure_reversal.py','research_swing_jd_failure_reclaim.py','research_swing_jd_reclaim_overlay.py','report_swing_jd_failure_reversal.py','freeze_swing_jd_reclaim.py']:
        shutil.copy2(b.ROOT/'scripts'/name,sources/name)
    shutil.copy2(b.ROOT/'scripts/frozen_jd_reclaim_replay.py',DEST/'replay.py')
    shutil.copy2(b.ROOT/'research-strategies/best-a-portfolio-observation-v1/controller.py',DEST/'controller.py')
    shutil.copytree(b.RUN/'fork',DEST/'native-source');(DEST/'bin').mkdir()
    binary=f.R(f.RUN/'protocol.json')['native_binary'];assert f.sha(binary['path'])==binary['sha256'];shutil.copy2(binary['path'],DEST/'bin/swing_jd_abc_execution.exe')
    shutil.copy2(b.ROOT/'scripts/exchange_fees_20261022.json',DEST/'fees.json')
    ctl,meta,data,ends,_=c.load(True);p=f.R(c.RUN/'protocol.json');cases=f.cases(p)
    with (DEST/'individual-decisions.jsonl').open('x',encoding='utf-8') as out:
        for (m,suffix),rows in data.items():
            if m not in ['E20S1','S0_RECLAIM_S1','S0_RECLAIM_R2']:continue
            for row in rows:out.write(json.dumps(dict(model=m,suffix=suffix,row=row),ensure_ascii=False)+'\n')
    write('portfolio-data.json',dict(metadata=meta,ends=ends,cases=cases,primary=p['primary'],months=b.MONTHS,blocks=b.z.x.blocks()))
    for coverage in ['main','combined']:
        expected=f.R(o.RUN/(coverage+'-results.json'));source=f.R(c.RUN/(coverage+'-results.json'))
        for case in cases:expected[case+'/E20S1']=source[case+'/E20S1']
        write('expected-'+coverage+'.json',expected)
    write('definition.json',dict(id=DEST.name,created_at=dt.datetime.now().isoformat(),status='research_observation_not_live',
        primary='E20_PLUS_RECLAIM_R2',alternative='E20_PLUS_RECLAIM_S1',symbol='JD0',bar_minutes=15,
        definition='Preserve full E20S1 JD research reference; add first true S0 failure thenfirstclose reclaim>=1tick within12laterbars, withoriginalABC conditions andsameactualcontract. Stop actualfailure-to-reclaim adverseextreme plusoriginalbuffer. Nextobserved1mOPEN; planned2R primary, originalS1 alternative. Exact inheritedcost/risk/holiday/controller definitions preservedin evidence/protocols andnative-source.',
        shared_occupancy='Merge allrawdecisions,warningtimeoneJDslot,sameparentA2settledstops; E20priorityatsameclock; unknownnotzeroandholdslotthrougharchiveend unlessfrozenbothpathsflatpolicy.',
        evidence_source=str(f.RUN),evidence_completion_sha256=f.sha(f.RUN/'completion.json'),frozen_original_primary_net=4478.255,frozen_original_closed=41,frozen_original_unknown=1,
        limitation='Sequential exploratoryhistory, notuntouchedOOS; below60trades; normalizedDD1261.62vs689.42reference; notaccountreturn. Snapshotdoesnotenableproductiontrading.',
        production_before=p['production_before']))
    external={}
    for per in ['early','recent','july']:external.update(f.R(f.RUN/per/'generation-checks.json')['source_hashes'])
    write('external-sources.json',external)
    (DEST/'README.md').write_text('''# 鸡蛋ABC失守后收回：研究观察冻结版v2

保存日期：2026-10-26。主候选为E20S1研究对照＋收回A起点计划2R；原S1目标合并版一并保留。未修改生产策略。

主历史：41笔已平仓净额4,478.255元，1笔未平仓；8场景最低已平仓净额2,355.357元。统一风险已实现回撤1,261.624元。完整限制见definition.json与evidence/summary.md。

## 离线核验

在本目录运行 `python replay.py`。只需Python标准库，验证manifest并从全部个体决策重新计算两种未知政策、四个执行场景、两个合并版及E20对照，覆盖主历史、含7月窗口历史及四个时间块。它不依赖当前仓库脚本、Rust安装或行情服务，不会写入冻结目录。

## 保存内容

- definition.json、全部原研究证据、逐笔原始决策、预期统计及SHA-256清单。
- 原生执行二进制、对应Rust源码、研究脚本和冻结手续费。
- external-sources.json保留分钟级行情来源及哈希。分钟行情重新生成或原生成命令仍依赖这些外部归档，不能把订单离线重算等同于行情全链路自包含。

此目录作为固定对照保留。后续研究另建目录，不覆盖本版本。未完成未来新增数据验证、账户/保证金验证及实盘资格。v2仅令离线入口禁止写入Python字节码缓存，策略及240项核验结果与v1一致；v1保留。
''',encoding='utf-8')
    files={q.relative_to(DEST).as_posix():f.sha(q) for q in DEST.rglob('*') if q.is_file()}
    write('manifest.json',dict(id=DEST.name,created_at=dt.datetime.now().isoformat(),files=files))
    result=subprocess.run(['python',str(DEST/'replay.py')],check=True,capture_output=True,text=True)
    receipt=b.ROOT/'research-runs/jd-reclaim-freeze-verification-20261026-v2.json'
    b.z.s.m.old.write(receipt,dict(snapshot=str(DEST),manifest_sha256=f.sha(DEST/'manifest.json'),verification=json.loads(result.stdout),production_unchanged=all(f.sha(b.ROOT/q)==h for q,h in p['production_before'].items())))
    print(result.stdout,flush=True);print(str(DEST),flush=True)

if __name__=='__main__':run()
