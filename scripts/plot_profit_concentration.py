"""Plot historical holiday-flat profit concentration, no new strategy decisions."""
import argparse
import json
import pathlib


def run(report,output):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.ticker import PercentFormatter
    report,output=map(pathlib.Path,(report,output))
    if output.exists():raise FileExistsError(output)
    summary=json.loads((report/'summary.json').read_text(encoding='utf-8'))
    cases={}
    for line in (report/'trade_attribution.jsonl').read_text(encoding='utf-8').splitlines():
        r=json.loads(line)
        cases.setdefault(r['scenario'],[]).append(r)
    plt.rcParams.update({'font.family':'Microsoft YaHei','axes.unicode_minus':False,'font.size':10})
    fig,axes=plt.subplots(2,3,figsize=(14,7.2))
    fig.patch.set_facecolor('#f7f9fc')
    names=[('net_s1','净空间'),('score3_friction20','净空间＋评分≥3＋成本过滤'),
        ('score3_confirm_no_follow_off','收盘确认＋取消无跟随退出')]
    colors={1:'#087f8c',2:'#cb6417'}
    for col,(base,label) in enumerate(names):
        for slip in (1,2):
            name=f'{base}_holiday_flat/slip{slip}'
            p=summary['cases'][name]['profit_audit'];rows=cases[name]
            winners=sorted([r['net_amount'] for r in rows if r['net_amount']>0],reverse=True)
            net=p['cash']['net']
            remaining=[net-sum(winners[:k]) for k in range(11)]
            ax=axes[0,col]
            ax.plot(range(11),remaining,color=colors[slip],marker='o',markersize=3,label=f'{slip} tick')
            ax.scatter([5],[remaining[5]],color=colors[slip],s=40,zorder=5)
            ax.annotate(f'{remaining[5]:,.0f}',(5,remaining[5]),xytext=(5,8 if slip==1 else -16),
                textcoords='offset points',fontsize=9,color=colors[slip])
            ax=axes[1,col]
            total=sum(winners);running=0.;ys=[0.]
            for x in winners:running+=x;ys.append(running/total)
            ax.plot([i/len(winners) for i in range(len(winners)+1)],ys,color=colors[slip],label=f'{slip} tick')
        ax=axes[0,col]
        ax.set_title(label,pad=12,fontweight='bold',fontsize=11)
        ax.axhline(0,color='#374151',linewidth=1)
        ax.set_ylim(-15000,14500);ax.set_xticks([0,1,3,5,10]);ax.set_xlabel('删除最大的盈利交易数')
        if col==0:ax.set_ylabel('剩余净收益（元）')
        ax.legend(frameon=False,fontsize=9)
        ax=axes[1,col]
        ax.plot([0,1],[0,1],color='#9ca3af',linestyle='--',linewidth=1,label='等额盈利参考')
        ax.set_xlim(0,1);ax.set_ylim(0,1);ax.xaxis.set_major_formatter(PercentFormatter(1));ax.yaxis.set_major_formatter(PercentFormatter(1))
        ax.set_xlabel('盈利交易比例（从大到小）')
        if col==0:ax.set_ylabel('累计正盈利占比（未抵扣亏损）')
        ax.legend(frameon=False,fontsize=9,loc='lower right')
    for ax in axes.flat:
        ax.set_facecolor('white');ax.grid(alpha=.16);ax.spines[['top','right']].set_visible(False)
    fig.suptitle('长假前退出后：净利润仍怕删大单，正盈利本身并非只来自一两笔',fontsize=16,fontweight='bold',y=.985)
    fig.text(.5,.015,'历史开发样本｜一手、同品种单机会、无资金上限｜长假前14:45退出｜不含未知敞口；删单是敏感性诊断',
        ha='center',fontsize=10,color='#475569')
    fig.tight_layout(rect=[0,.045,1,.945],h_pad=2.2)
    fig.savefig(output,dpi=150,facecolor=fig.get_facecolor())
    plt.close(fig)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('report');p.add_argument('output')
    args=p.parse_args();run(args.report,args.output)
