"""Last bounded search: range swings, 12 models x two fixed risk budgets.

Shares chronological selection/reporting with daily study. No asset selection.
"""
import research_swing_daily_20261104 as d
import research_swing_year_20261104 as s


def main():
    models=[dict(id=f'range_{f}_{n}_{k}',family=f,n=n,k=k) for f in ['falsebreak','meanrevert'] for n in [64,128,256] for k in [4,6]]
    d.main('swing-range-20261104',models,daily=False)


if __name__=='__main__':main()
