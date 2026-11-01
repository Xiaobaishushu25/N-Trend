"""Reproducible offline factor study. Never writes source DBs, broker config or live scores."""
import argparse
import collections
import json
import pathlib
import subprocess
import sys

from factor_study_data import ROOT,read,rows,write,new_output,seal,verify_seal,digest,prepare
from factor_study_execution import replay_stage,audit_label,metrics
from factor_study_analysis import diagnose_stage,fit_stage,load_context
from factor_study_quote_audit import audit_quotes


def scan_stage(prepared,output,scanner=None):
    prepared=pathlib.Path(prepared).resolve();verify_seal(prepared)
    output=pathlib.Path(output).resolve()
    if output.exists():raise FileExistsError(output)
    if not output.is_relative_to(ROOT):raise ValueError('output outside repository')
    scanner=pathlib.Path(scanner) if scanner else ROOT/'target/debug/signal-quality.exe'
    if not scanner.exists():raise FileNotFoundError('Build scanner first: cargo build -p n-core --bin signal-quality --offline')
    command=[str(scanner),'factor-scan','--input',str(prepared/'scan-input.jsonl'),'--out',str(output)]
    subprocess.run(command,cwd=ROOT,check=True)
    write(output/'invocation.json',dict(prepared=str(prepared),scanner_sha256=digest(scanner),command=command,
                                      prepared_manifest_sha256=digest(prepared/'manifest.json'),
                                      snapshots_sha256=digest(output/'snapshots.jsonl')))
    return output


def verify_stage(replayed,output,fit=None,diagnosis=None):
    prepared,scan,snapshots,maps,protocol,coverage=load_context(replayed)
    out=new_output(output)
    invocation=read(scan/'invocation.json')
    if digest(scan/'snapshots.jsonl')!=invocation['snapshots_sha256']:
        raise ValueError('scan snapshots changed')
    byid={s['candidate_id']:s for s in snapshots}
    if len(byid)!=len(snapshots):raise ValueError('duplicate candidate identity')
    expected=set(byid)
    counts=collections.Counter()
    for scenario,labels in maps.items():
        if set(labels)!=expected:raise ValueError('scenario population mismatch')
        for key,label in labels.items():
            audit_label(byid[key],label)
            if label['status']=='closed':counts['reconciled_closed']+=1
            counts[label['status']]+=1
    for item in read(scan/'manifest.json')['contracts']:
        if item['prefix_mismatches']!=0:raise ValueError('prefix snapshot mismatch')
    quote_audit=audit_quotes(prepared,snapshots,maps)
    write(out/'quote-audit.json',quote_audit)
    checked_ledgers=0
    if fit:
        fit=pathlib.Path(fit);verify_seal(fit)
        for ledger_path in fit.glob('*.ledger.jsonl'):
            active={}
            for row in rows(ledger_path):
                if row['decision']!='accepted':continue
                label=row['label'];symbol=row['symbol']
                if active.get(symbol,'')>row['warning_ts']:raise ValueError('product occupancy overlaps')
                active[symbol]=label.get('release_ts') or (label['exit_ts'] if label['status'] in {'closed','untriggered','invalidated','invalid_target'} else '9999')
            checked_ledgers+=1
        for record in read(fit/'artifacts.json'):
            a=record['artifact']
            if a:
                start=read(fit/'summary.json')['folds'][record['fold']]['start']
                if a['training_last_exit']>=start:raise ValueError('training used incomplete future outcome')
    if diagnosis:verify_seal(diagnosis)
    result=dict(status='passed',snapshots=len(snapshots),scenarios=len(maps),counts=dict(counts),
                checked_ledgers=checked_ledgers,prefix_mismatches=0,production_mutations=False,
                source_quote_and_fee_checks=quote_audit['closed_quote_and_fee_checks'],
                conservative_target_gaps=quote_audit['conservative_target_gap_count'],
                limitations=coverage['limitations'])
    write(out/'verification.json',result)
    inputs=[pathlib.Path(replayed)/'manifest.json',prepared/'manifest.json',scan/'manifest.json']
    if fit:inputs.append(pathlib.Path(fit)/'manifest.json')
    if diagnosis:inputs.append(pathlib.Path(diagnosis)/'manifest.json')
    seal(out,'verify',inputs)
    return out


def format_value(v):
    return '—' if v is None else f'{v:.2f}'


def report_stage(replayed,fit,diagnosis,verification,output):
    prepared,scan,snapshots,maps,protocol,coverage=load_context(replayed)
    fit,diagnosis,verification=map(pathlib.Path,(fit,diagnosis,verification))
    for p in (fit,diagnosis,verification):verify_seal(p)
    if read(verification/'verification.json')['status']!='passed':raise ValueError('verification not passed')
    out=new_output(output);summary=read(fit/'summary.json');candidate=read(fit/'frozen-candidate.json')
    invocation=read(scan/'invocation.json')
    executable=pathlib.Path(invocation['command'][0])
    executable=executable if executable.is_absolute() else ROOT/executable
    if executable.exists() and digest(executable)==invocation['scanner_sha256']:
        (out/'scanner.exe').write_bytes(executable.read_bytes())
    verification_result=read(verification/'verification.json')
    write(out/'summary.json',summary)
    stratified={}
    for ledger_path in sorted(fit.glob('*.ledger.jsonl')):
        cohort_start=(summary['folds'][0]['start'] if summary['folds'] and 'ge70' not in ledger_path.name else protocol['start'])
        accepted=[r['label'] for r in rows(ledger_path) if r['decision']=='accepted' and r['warning_ts']>=cohort_start]
        groups={'cohort_start':cohort_start}
        for dimension,key in [('month',lambda r:r['warning_ts'][:7]),('symbol',lambda r:r['symbol'])]:
            grouped=collections.defaultdict(list)
            for label in accepted:grouped[key(label)].append(label)
            groups[dimension]={k:metrics(v) for k,v in grouped.items()}
        groups['recent_three_months']=metrics(r for r in accepted if r['warning_ts']>='2026-07-31 00:00:00')
        closed=sorted((r for r in accepted if r['status']=='closed'),key=lambda r:r['net_yuan'],reverse=True)
        groups['without_best_five']=metrics(closed[5:])
        groups['leave_one_symbol_out']={symbol:metrics(r for r in accepted if r['symbol']!=symbol)
                                        for symbol in sorted({r['symbol'] for r in accepted})}
        stratified[ledger_path.stem]=groups
    write(out/'stratified-metrics.json',stratified)
    lines=['# 评分参数与交互研究结果','',
           '**性质：历史开发研究。正式评分与交易配置未修改；前瞻尚未开始。**','',
           f'请求窗口：{protocol["start"]} 至 {protocol["end"]}。实际行情最晚：{coverage["actual_last"]}。',
           f'实际合约：{len(coverage["contracts"])}；主窗口候选：{sum(s.get("in_window",True) for s in snapshots)}；另含预热候选：{sum(not s.get("in_window",True) for s in snapshots)}。',
           f'最终状态：`{candidate["status"]}`。共同前缀核验、金额/R及源行情与费用检查通过。','',
           '主实验：预警收盘后突破入场、一手、计划损失≤500元、净200元固定目标、第5根无跟随及60根期限退出。',
           '500元为计划风险；跳空后实际风险与亏损可能超过该值。费用为冻结快照，合约映射原时点可知性未验证。','',
           '## 一手执行比较','',
           '下表为可确认已完成部分。未知、未结束和占用影响分别保留，不能当作完整账户收益。','',
           'adaptive/v1/equal/space_cost是初始训练后的滚动评估；ge70是全窗口固定阈值对照，时间覆盖与筛选力度不同。','',
           '|版本|完成|未知|净元|平均净元|平均净R|净胜率|已平仓回撤元|',
           '|---|---:|---:|---:|---:|---:|---:|---:|']
    for name,value in summary['metrics'].items():
        lines.append('|'+name+'|'+str(value['closed'])+'|'+str(value['unknown'])+'|'+
                     '|'.join([format_value(value['net_yuan']),format_value(value['mean_net_yuan']),
                               '—' if value['mean_net_r'] is None else f'{value["mean_net_r"]:.4f}',
                               '—' if value['win_rate'] is None else f'{100*value["win_rate"]:.2f}%',
                               format_value(value['max_drawdown_yuan'])])+'|')
    lines+=['','## 选模状态','',f'滚动折数：{len(summary["folds"])}；内部验证选中非默认规则的折数：{summary["selected_nonbaseline_folds"]}。',
            f'最终研究候选状态：`{candidate["status"]}`。',
            '无候选通过时沿用基线，不降低样本或覆盖门槛。权重与映射的历史改善不构成前瞻验证。','',
            '|窗口|训练结构组|结果|采用配置|','|---|---:|---|---|']
    for fold in summary['folds']:
        lines.append(f'|{fold["start"][:10]}—{fold["end"][:10]}|{fold["training_groups"]}|{fold["selection"]["reason"]}|{fold["chosen"]["id"]}|')
    lines+=['','## 固定模块消融','',
            '固定映射移除与重新拟合消融独立输出；移除一个模块后的均值变化不能直接解释为因果。','',
            '本表使用首训练期确定的固定门槛；主表滚动基线按每折之前的分布更新门槛，因此两个original/v1汇总不完全相同。','',
            '|移除模块|完成|净元|平均净R|未知|','|---|---:|---:|---:|---:|']
    for r in read(diagnosis/'fixed-ablations.json'):
        m=r['metrics']['1'];lines.append(f'|{r["removed"]}|{m["closed"]}|{format_value(m["net_yuan"])}|{format_value(m["mean_net_r"])}|{m["unknown"]}|')
    boundary_text=('最初训练期（至少6个月）' if protocol['validation'].get('schedule')=='monthly' else '最初30个交易日')
    lines+=['','## 参数与交互证据','',
            f'单项五分位和交互三分位边界均来自{boundary_text}；每格少于30个独立结构组标为不足。',
            '原始Spearman检验及BH校正仅为探索描述，不能替代时间外消融与新信号确认。','',
            '详见同批次 diagnose 目录：特征字典、单项曲线、交互条件面、相关分组、固定与重新拟合消融、权重敏感性。','',
            '## 数据限制与后续','']
    lines += ['- '+s for s in coverage['limitations']]
    lines+=['',f'独立源行情与费用核验：{verification_result.get("source_quote_and_fee_checks","未在此版本执行")}条情景成交记录。',
            f'有利跳空的保守目标价约定：{verification_result.get("conservative_target_gaps","未单列")}条，逐条保存在verify/quote-audit.json；不计开盘价格改善，不把该参考价冒充本分钟实际观察成交。',
            '', '本批次不自动发布正式权重、不启动实盘或前瞻采集。冻结研究候选必须在新信号上满足登记的60个交易日及200笔完成交易后复核。','',
            '当前批次包含的每个阶段均有输入、输出及代码哈希；复现入口见运行目录 invocation 和各 manifest。']
    (out/'report.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    make_plots(diagnosis,out)
    seal(out,'report',[fit/'manifest.json',diagnosis/'manifest.json',verification/'manifest.json'])
    return out


def make_plots(diagnosis,out):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.colors import TwoSlopeNorm
    curves=read(diagnosis/'feature-curves.json')
    selected=[c for c in curves if c['feature'] in {'retracement','a_core_efficiency','a_move_atr','ma20_slope_pre'}]
    fig,axes=plt.subplots(2,2,figsize=(11,7),layout='constrained')
    for ax,curve in zip(axes.flat,selected):
        points=sorted((int(k),v) for k,v in curve['bins'].items() if k!='missing')
        ax.axhline(0,color='gray',linewidth=.7)
        for x,item in points:
            m=item['metrics'];y=m['mean_net_yuan']
            if y is not None:
                interval=m.get('mean_net_yuan_interval95')
                if interval:ax.vlines(x,*interval,color='#aaa',linewidth=1)
                ax.scatter(x,y,c='#16784b' if item['sufficient'] else '#aaa',s=45)
                ax.annotate(str(item['groups'])+' groups',(x,y),xytext=(0,5),textcoords='offset points',fontsize=8)
        ax.set_title(curve['feature']);ax.set_xlabel('Training-defined bin');ax.set_ylabel('Mean net CNY / lot')
    fig.suptitle('Development diagnostics; gray = insufficient independent groups')
    fig.savefig(out/'factor-curves.png',dpi=160);plt.close(fig)
    surfaces=read(diagnosis/'interaction-surfaces.json')
    fig,axes=plt.subplots(1,2,figsize=(11,4),layout='constrained')
    import numpy as np
    for ax,surface in zip(axes,surfaces[:2]):
        array=np.full((3,3),np.nan)
        for cell,item in surface['cells'].items():
            if cell=='missing':continue
            i,j=map(int,cell.split(','));value=item['metrics']['mean_net_yuan']
            if value is not None:array[j,i]=value
            ax.text(i,j,f'{value:.0f}\nn={item["groups"]}' if value is not None else 'missing',ha='center',va='center',fontsize=9)
        finite=array[np.isfinite(array)]
        norm=TwoSlopeNorm(vmin=min(-1.,float(finite.min())) if len(finite) else -1.,vcenter=0.,
                          vmax=max(1.,float(finite.max())) if len(finite) else 1.)
        image=ax.imshow(array,cmap='RdYlGn',norm=norm,origin='lower');fig.colorbar(image,ax=ax,label='Mean net CNY')
        ax.set_xlabel(surface['features'][0]);ax.set_ylabel(surface['features'][1]);ax.set_xticks(range(3));ax.set_yticks(range(3))
        for axis,name in ((ax.xaxis,surface['features'][0]),(ax.yaxis,surface['features'][1])):
            cuts=surface['training_bounds'][name]
            if len(cuts)==2:axis.set_ticklabels([f'<{cuts[0]:.3g}',f'{cuts[0]:.3g}-{cuts[1]:.3g}',f'>{cuts[1]:.3g}'])
    fig.suptitle('A x B development surfaces; small cells do not establish healthy ranges')
    fig.savefig(out/'a-b-surfaces.png',dpi=160);plt.close(fig)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage',choices=['prepare','scan','replay','diagnose','fit','verify','report','run'])
    parser.add_argument('--protocol',default=str(pathlib.Path(__file__).with_name('factor-study-v1.json')))
    parser.add_argument('--input');parser.add_argument('--prepared');parser.add_argument('--fit');parser.add_argument('--diagnosis')
    parser.add_argument('--verification');parser.add_argument('--scanner');parser.add_argument('--out',required=True)
    args=parser.parse_args()
    if args.stage=='run':
        root=new_output(args.out)
        prepared=prepare(args.protocol,root/'prepare')
        scan=scan_stage(prepared,root/'scan',args.scanner)
        replay=replay_stage(prepared,scan,root/'replay')
        diagnosis=diagnose_stage(replay,root/'diagnose')
        fit=fit_stage(replay,root/'fit')
        verification=verify_stage(replay,root/'verify',fit,diagnosis)
        report_stage(replay,fit,diagnosis,verification,root/'report')
        print('Completed: '+str(root/'report/report.md'),flush=True)
    elif args.stage=='prepare':prepare(args.protocol,args.out)
    elif not args.input:parser.error('--input is required')
    elif args.stage=='scan':scan_stage(args.input,args.out,args.scanner)
    elif args.stage=='replay':
        prepared=args.prepared or read(pathlib.Path(args.input)/'invocation.json')['prepared']
        replay_stage(prepared,args.input,args.out)
    elif args.stage=='diagnose':diagnose_stage(args.input,args.out)
    elif args.stage=='fit':fit_stage(args.input,args.out)
    elif args.stage=='verify':verify_stage(args.input,args.out,args.fit,args.diagnosis)
    elif args.stage=='report':
        if not all((args.fit,args.diagnosis,args.verification)):parser.error('report requires --fit, --diagnosis, --verification')
        report_stage(args.input,args.fit,args.diagnosis,args.verification,args.out)


if __name__=='__main__':main()
