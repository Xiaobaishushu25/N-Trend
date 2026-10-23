"""Inspect the same 19 original live signals under P2/P3, including concentration.

Compatible with installed Python3.6/Matplotlib2.2 for static chart export.
"""
import argparse
import collections
import csv
import hashlib
import json
import math
import os
import pathlib
import statistics
import tempfile


def money(x):return format(x, ',.2f')


def table(headers,rows):
    return ['|'+'|'.join(headers)+'|','|'+'|'.join('---' for _ in headers)+'|']+[
        '|'+'|'.join(str(x) for x in row)+'|' for row in rows]


def run(folder):
    folder=pathlib.Path(folder)
    lanes={'P2':{},'P3':{}}
    with (folder/'accepted-replays.jsonl').open(encoding='utf-8') as f:
        for line in f:
            r=json.loads(line)
            if r['cohort']=='live_all_original' and r['lane'] in ('P2/slip1','P3/slip1') and r['result']['evaluation']['status']=='closed':
                lanes[r['lane'][:2]][r['event_key']]=r
    assert set(lanes['P2'])==set(lanes['P3']) and len(lanes['P2'])==19
    paired=[]
    for key,r in sorted(lanes['P2'].items(),key=lambda x:(x[1]['warning_ts'],x[0])):
        a=r['result']['evaluation'];b=lanes['P3'][key]['result']['evaluation']
        assert a['entry_ts']==b['entry_ts'] and abs(a['fill']-b['fill'])<1e-7
        paired.append(dict(symbol=r['symbol'],warning_ts=r['warning_ts'],entry_ts=a['entry_ts'],
                           p2_exit_ts=a['exit_ts'],p3_exit_ts=b['exit_ts'],p2_net=a['net_amount'],p3_net=b['net_amount'],
                           delta_p3_minus_p2=b['net_amount']-a['net_amount'],p2_reason=a['exit_reason'],p3_reason=b['exit_reason'],event_key=key))
    with (folder/'recent-19-paired.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,list(paired[0]));w.writeheader();w.writerows(paired)
    summaries={};bins={};products={};days={}
    bounds=[(-math.inf,-500),(-500,-200),(-200,0),(0,200),(200,500),(500,1000),(1000,math.inf)]
    for p in lanes:
        rows=list(lanes[p].values());cash=[r['result']['evaluation']['net_amount'] for r in rows]
        pos=sorted((x for x in cash if x>0),reverse=True);neg=[x for x in cash if x<0]
        total=math.fsum(cash);profit=math.fsum(pos);loss=math.fsum(neg)
        summaries[p]=dict(n=len(cash),wins=len(pos),losses=len(neg),net=total,profit=profit,loss=loss,
            average_win=statistics.mean(pos),average_loss=statistics.mean(neg),median=statistics.median(cash),
            best=max(cash),worst=min(cash),top3_positive_share=math.fsum(pos[:3])/profit,
            top5_positive_share=math.fsum(pos[:5])/profit,
            without_best1=total-math.fsum(pos[:1]),without_best3=total-math.fsum(pos[:3]),without_best5=total-math.fsum(pos[:5]))
        bins[p]=[dict(n=sum(a<=x<b for x in cash),net=math.fsum(x for x in cash if a<=x<b)) for a,b in bounds]
        assert sum(x['n'] for x in bins[p])==19 and abs(math.fsum(x['net'] for x in bins[p])-total)<1e-7
        products[p]=collections.defaultdict(list);days[p]=collections.defaultdict(list)
        for r in rows:
            products[p][r['symbol']].append(r['result']['evaluation']['net_amount'])
            days[p][r['exit_trading_day']].append(r['result']['evaluation']['net_amount'])
    lines=['# P2/P3近期19笔利润分布（2026-10-23）','',
        '范围：服务器10月16日至23日原始189条条件信号筛选后的19笔可定价平仓。两组恰好是相同19个信号，入场时刻与入场价也一致，退出可能不同。固定一手、每侧1 tick、已扣冻结手续费；这是策略回放，不是实际账户成交。','']
    labels=[('净利润','net'),('盈利合计','profit'),('亏损合计','loss'),('平均盈利','average_win'),('平均亏损','average_loss'),
            ('单笔中位数','median'),('最好单笔','best'),('最差单笔','worst'),('删最大1笔盈利后净额','without_best1'),
            ('删最大3笔盈利后净额','without_best3'),('删最大5笔盈利后净额','without_best5')]
    lines+=table(['指标','P2','P3'],[[label]+[money(summaries[p][field]) for p in ('P2','P3')] for label,field in labels])+['',
        'P2为9盈10亏，P3为10盈9亏；两组都没有平局。前三笔盈利占全部正盈利之和分别为52.13%/46.59%，前五笔占73.51%/67.19%。金额贡献的分母是盈利交易的正净额合计，不是总净利润。删交易只是事后依赖诊断，不是可事前实现的规则。','',
        '## 金额区间','', '区间左闭右开，单位元；低于-500和1000及以上是开口端。','']
    names=['低于−500','[−500,−200)','[−200,0)','[0,200)','[200,500)','[500,1000)','1000及以上']
    lines+=table(['单笔净额区间','P2笔数','P3笔数'],[[label,bins['P2'][i]['n'],bins['P3'][i]['n']] for i,label in enumerate(names)])+['',
        '## 分品种净额','']
    lines+=table(['品种','共同信号数','P2净额','P3净额'],[[s,len(products['P2'][s]),money(math.fsum(products['P2'][s])),money(math.fsum(products['P3'][s]))] for s in sorted(products['P2'])])+['',
        '## 退出交易日分布','', '交易日按归档日盘日期推断，夜盘归下一日盘交易日。','']
    lines+=table(['退出交易日','P2净额','P3净额'],[[d,money(math.fsum(days['P2'][d])),money(math.fsum(days['P3'][d]))] for d in sorted(days['P2'])])+['',
        '## 同一19笔逐笔对照','', '按预警时间排列。完整入场、退出时间、退出原因及事件键见同目录recent-19-paired.csv。','']
    lines+=table(['品种','预警时刻','P2净额','P3净额','P3−P2'],[[r['symbol'],r['warning_ts'],money(r['p2_net']),money(r['p3_net']),money(r['delta_p3_minus_p2'])] for r in paired])+['',
        'P3多赚140.27元的主要来源是一笔L空单（10月21日14:00预警）：P2为−249.45元，P3为+477.90元，单笔差727.35元；其余四笔被2R目标上限削减盈利，合计少赚587.08元。因此这19笔并不能证明P3在每类交易上都更好。','',
        'P2剔除最大五笔盈利后净额−424.04元，P3剩+133.84元，说明两者都较依赖大额盈利交易；P3在这19笔中对最大几笔的依赖稍低。各品种只有1～4笔，不能据此建立品种白名单。','',
        '![同一19个信号的逐笔净额]('+str((folder/'recent-19-distribution.png').resolve()).replace('\\','/')+')','']
    (folder/'recent-19-distribution.md').write_text('\n'.join(lines),encoding='utf-8')
    (folder/'recent-19-distribution.json').write_text(json.dumps(dict(summary=summaries,bins=bins,
        source_sha256=hashlib.sha256((folder/'accepted-replays.jsonl').read_bytes()).hexdigest(),
        same_event_keys=True,same_entries=True),ensure_ascii=False,indent=2),encoding='utf-8')
    plot(folder,paired)
    print('Wrote recent-19 paired CSV, report, JSON and PNG; same 19 keys and entry fills verified.')


def plot(folder,paired):
    os.environ.setdefault('MPLCONFIGDIR',os.path.join(tempfile.gettempdir(),'ntrend-paper-history-mpl'))
    import matplotlib
    matplotlib.use('Agg')
    from matplotlib import pyplot as plt,font_manager
    import numpy as np
    font_path='C:/Windows/Fonts/msyh.ttc'
    font_manager.fontManager.ttflist.extend(font_manager.createFontList([font_path]))
    plt.rcParams['font.family']=font_manager.FontProperties(fname=font_path).get_name()
    plt.rcParams['axes.unicode_minus']=False
    plt.rcParams['font.size']=11
    fig,ax=plt.subplots(figsize=(13,10))
    y=np.arange(len(paired))
    for p,offset,color in [('p2_net',-.18,'#087e8b'),('p3_net',.18,'#7c3aed')]:
        ax.barh(y+offset,[r[p] for r in paired],height=.32,color=color,label=p[:2].upper())
    ax.set_yticks(y);ax.set_yticklabels([r['symbol']+'  '+r['warning_ts'][5:16] for r in paired]);ax.invert_yaxis()
    ax.axvline(0,color='#64748b',lw=1)
    ax.set_xlim(-480,1510);ax.set_xlabel('单笔净盈亏（元）')
    ax.grid(axis='x',alpha=.2);ax.set_axisbelow(True)
    ax.spines['top'].set_visible(False);ax.spines['right'].set_visible(False)
    ax.legend(frameon=False,ncol=2,loc='upper right')
    fig.suptitle('P2 / P3：同一19个信号的利润分布',x=.22,y=.98,ha='left',fontsize=20)
    fig.text(.22,.935,'按预警时间排列 · 一手 · 每侧1 tick · 已扣冻结手续费',fontsize=11,color='#475569')
    fig.text(.22,.025,'19笔小样本；P3主要靠一笔L由亏转盈，多赚140.27元。模拟回放，非实际账户成交。',fontsize=10,color='#475569')
    fig.subplots_adjust(left=.22,right=.97,top=.89,bottom=.09)
    fig.savefig(str(folder/'recent-19-distribution.png'),dpi=150,facecolor='white');plt.close(fig)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--folder',required=True)
    run(p.parse_args().folder)
