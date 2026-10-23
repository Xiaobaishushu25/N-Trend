"""Compare fixed product ordering and both-sided trading-day diagnostics."""
import argparse
import json
import pathlib


def run(report,output):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np
    report,output=map(pathlib.Path,(report,output))
    if output.exists():raise FileExistsError(output)
    data=json.loads((report/'summary.json').read_text(encoding='utf-8'))
    names=[('score3_friction20','原成本过滤＋长假退出'),
        ('fric_atr_weekend','加入ATR筛选＋周末退出'),
        ('fric_atr_cap20_weekend','分布候选：再限制目标至2R')]
    get=lambda n,i:data['cases'][f'{n}/slip{i}']['admissions']['symbol_slot']
    reference=get(names[0][0],1)
    order=sorted(reference['products'],key=lambda k:reference['products'][k]['stats']['net_amount'],reverse=True)
    plt.rcParams.update({'font.family':'Microsoft YaHei','axes.unicode_minus':False,'font.size':10})
    fig,axes=plt.subplots(2,3,figsize=(15,10),gridspec_kw={'height_ratios':[2.3,1]})
    fig.patch.set_facecolor('#f7f9fc')
    for col,(name,label) in enumerate(names):
        v=get(name,1);ax=axes[0,col]
        for i,symbol in enumerate(order):
            p=v['products'][symbol]['stats'];cash=p['net_amount'];n=p['n']
            color='#0b8b7b' if cash>=0 else '#c65346'
            ax.barh(i,cash,color=color,alpha=1 if n>=10 else .35,height=.66)
            ax.text(cash+(75 if cash>=0 else -75),i,f'{cash:,.0f}  (n={n})',
                ha='left' if cash>=0 else 'right',va='center',fontsize=8.5,color='#334155')
        ax.set_yticks(range(len(order)),order);ax.invert_yaxis();ax.axvline(0,color='#475569',linewidth=.8)
        ax.set_xlim(-4300,7300);ax.set_xlabel('1 tick已定价净收益（元）')
        ax.set_title(f"{label}\n盈利{v['breadth']['positive_products']}/{v['breadth']['active_products']}品种；前3占正品种净额{v['breadth']['top3_positive_product_net_share']:.1%}",fontsize=11,pad=12)
        ax=axes[1,col];x=np.arange(4);width=.35
        for slip,color in [(1,'#087f8c'),(2,'#cc721d')]:
            v=get(name,slip);t=v['symmetric']['trading_day']['cash']['trim']['3']
            amounts=[v['stats']['net_amount'],*[t[k]['net_amount'] for k in ('without_best','without_worst','without_both')]]
            bars=ax.bar(x+(slip-1.5)*width,amounts,width,color=color,label=f'{slip} tick')
            for bar,cash in zip(bars,amounts):
                ax.text(bar.get_x()+bar.get_width()/2,cash+(300 if cash>=0 else -300),f'{cash/1000:.1f}k',
                    ha='center',va='bottom' if cash>=0 else 'top',fontsize=8,color=color)
        ax.set_xticks(x,['全部','删最好\n3交易日','删最差\n3交易日','两端各删\n3交易日'])
        ax.set_ylim(-1500,25500);ax.axhline(0,color='#475569',linewidth=.8);ax.legend(frameon=False,fontsize=9)
        if col==0:ax.set_ylabel('剩余净收益（元）')
    for ax in axes.flat:
        ax.set_facecolor('white');ax.spines[['top','right']].set_visible(False);ax.grid(axis='x' if ax in axes[0] else 'y',alpha=.12)
        ax.set_axisbelow(True)
    fig.suptitle('品种利润广度与最好／最差交易日的对称诊断',fontsize=17,fontweight='bold',y=.985)
    fig.text(.5,.018,'固定原方案品种顺序；浅色为少于10笔，n=0表示未有可定价交易｜一手、无资金上限｜夜盘归下一交易日（归档推断）；不含未知敞口',
        ha='center',fontsize=10,color='#475569')
    fig.tight_layout(rect=[0,.04,1,.95],h_pad=2)
    fig.savefig(output,dpi=150,facecolor=fig.get_facecolor());plt.close(fig)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('report');p.add_argument('output');a=p.parse_args();run(a.report,a.output)
