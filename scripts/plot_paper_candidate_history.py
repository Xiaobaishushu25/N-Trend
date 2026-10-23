"""Static export charts; compatible with the installed Python3.6/Matplotlib2.2."""
import argparse
import datetime as dt
import json
import os
import tempfile

os.environ.setdefault('MPLCONFIGDIR', os.path.join(tempfile.gettempdir(), 'ntrend-paper-history-mpl'))
import matplotlib
matplotlib.use('Agg')
from matplotlib import pyplot as plt
from matplotlib import dates as mdates
from matplotlib import font_manager
import numpy as np


def run(folder):
    with open(os.path.join(folder, 'summary.json'), encoding='utf-8') as f:
        data = json.load(f)
    font_path = 'C:/Windows/Fonts/msyh.ttc'
    font = font_manager.FontProperties(fname=font_path)
    font_manager.fontManager.ttflist.extend(font_manager.createFontList([font_path]))
    plt.rcParams['font.family'] = font.get_name()
    plt.rcParams['axes.unicode_minus'] = False
    plt.rcParams['font.size'] = 11
    plt.rcParams['axes.spines.top'] = False
    plt.rcParams['axes.spines.right'] = False
    colors = ['#64748b', '#ca8a04', '#087e8b', '#7c3aed']
    strategies = ['P0', 'P1', 'P2', 'P3']
    def case(p, slip=1):return data['cases'][p+'/slip'+str(slip)]
    fig, axes = plt.subplots(2, 1, figsize=(15, 10), gridspec_kw={'height_ratios':[1.6,1]})
    fig.suptitle('候选策略历史表现 | 2026-07-03 至 2026-10-22', fontsize=21, x=.08, ha='left', y=.98)
    fig.text(.08,.935,'固定一手 · 同品种单入场槽位 · 已扣手续费 · 每侧 1 tick 滑点',color='#475569',fontsize=12)
    for p,color in zip(strategies,colors):
        bins = case(p)['exit_trading_day']
        days = sorted(bins)
        dates = [dt.datetime.strptime(day,'%Y-%m-%d') for day in days]
        equity = np.cumsum([bins[day]['net'] for day in days])
        axes[0].plot(dates,equity,color=color,lw=2,label=p+'  '+format(equity[-1],',.0f')+' 元')
    axes[0].axhline(0,color='#cbd5e1',lw=1)
    axes[0].set_title('已实现累计净额（按退出交易日）',loc='left',fontsize=14,pad=14)
    axes[0].set_ylabel('元')
    axes[0].xaxis.set_major_locator(mdates.MonthLocator())
    axes[0].xaxis.set_major_formatter(mdates.DateFormatter('%m/%d'))
    axes[0].grid(axis='y',alpha=.2)
    axes[0].legend(loc='upper left',frameon=False,ncol=2)
    months = sorted(case('P0')['warning_month'])
    x = np.arange(len(months))
    for i,(p,color) in enumerate(zip(strategies,colors)):
        values = [case(p)['warning_month'][m]['net'] for m in months]
        bars = axes[1].bar(x+(i-1.5)*.19, values, width=.18,color=color,label=p)
        for bar,val in zip(bars,values):
            axes[1].text(bar.get_x()+bar.get_width()/2,val+130,format(val,',.0f'),ha='center',fontsize=9,color=color)
    axes[1].set_title('月度净盈亏（按预警月，与冻结规格一致）',loc='left',fontsize=14,pad=14)
    axes[1].set_xticks(x)
    axes[1].set_xticklabels(['7月（3日起）','8月','9月','10月（截至22日）'])
    axes[1].set_ylabel('元')
    axes[1].grid(axis='y',alpha=.2)
    axes[1].set_axisbelow(True)
    axes[1].legend(frameon=False,ncol=4)
    fig.text(.08,.025,'仅含可定价已平仓；未知机会 P0/P1 各4条、P2/P3 各3条。曲线不含浮盈亏、资金约束和未知损益。历史反复研究样本，新增独立观察为0。',fontsize=10,color='#475569')
    fig.subplots_adjust(left=.08,right=.97,top=.88,bottom=.09,hspace=.42)
    fig.savefig(os.path.join(folder,'overview.png'),dpi=160,facecolor='white')
    plt.close(fig)

    fig,axes = plt.subplots(1,2,figsize=(16,10),gridspec_kw={'width_ratios':[1.15,1]})
    fig.suptitle('利润来自哪里，以及单笔盈亏如何分布',fontsize=21,x=.07,ha='left',y=.98)
    fig.text(.07,.935,'每侧 1 tick · 净金额已扣手续费 · 品种使用固定字母顺序',color='#475569',fontsize=12)
    symbols = [s for s in data['products'] if any(case(p)['products'][s]['n'] for p in strategies)]
    y = np.arange(len(symbols))
    for i,(p,color) in enumerate(zip(strategies,colors)):
        values = [case(p)['products'][s]['net'] for s in symbols]
        axes[0].barh(y+(i-1.5)*.2,values,height=.18,color=color,label=p)
    axes[0].set_yticks(y)
    axes[0].set_yticklabels(symbols)
    axes[0].invert_yaxis()
    axes[0].axvline(0,color='#94a3b8',lw=1)
    axes[0].grid(axis='x',alpha=.2)
    axes[0].set_axisbelow(True)
    axes[0].set_title('分品种累计净盈亏',loc='left',fontsize=14,pad=14)
    axes[0].set_xlabel('元')
    axes[0].legend(frameon=False,ncol=4,loc='lower right')
    labels = ['< -1000','[-1000,-500)','[-500,-200)','[-200,0)','[0,200)','[200,500)','[500,1000)','>= 1000']
    bins = list(case('P0')['cash_bins'])
    y = np.arange(len(bins))
    for i,(p,color) in enumerate(zip(strategies,colors)):
        axes[1].barh(y+(i-1.5)*.2,[case(p)['cash_bins'][b]['n'] for b in bins],height=.18,color=color,label=p)
    axes[1].set_yticks(y)
    axes[1].set_yticklabels(labels)
    axes[1].invert_yaxis()
    axes[1].set_xlabel('交易笔数')
    axes[1].set_title('单笔净金额固定区间',loc='left',fontsize=14,pad=14)
    axes[1].grid(axis='x',alpha=.2)
    axes[1].set_axisbelow(True)
    fig.text(.07,.035,'分品种样本数与胜率见报告；少于10笔不宜推断品种优势。另有7个品种没有可定价平仓，不计为保本。',fontsize=11,color='#475569')
    fig.subplots_adjust(left=.07,right=.97,top=.88,bottom=.10,wspace=.37)
    fig.savefig(os.path.join(folder,'distribution.png'),dpi=160,facecolor='white')
    plt.close(fig)
    print('Created overview.png and distribution.png')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('folder')
    run(parser.parse_args().folder)
