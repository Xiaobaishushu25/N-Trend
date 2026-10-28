"""Finalize interpretation and audit same-time order sensitivity of the pair cap."""
import json
import pathlib
import sys

sys.dont_write_bytecode=True
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parent))
import research_swing_requested_six as r


def write_matching(path,value):
    if path.exists():assert r.m.old.read(path)==value
    else:r.m.old.write(path,value)


def run():
    m=r.m;root=r.RUN;results=m.old.read(root/'results.json');cells=m.old.read(root/'monthly-product.json');stats=m.old.read(root/'product-statistics.json')
    market=m.old.read(root/'market-return-correlations.json');checks=m.old.read(root/'pair-overlap-audit.json');attr=m.old.read(root/'attribution.json')
    controller,pp,events,ends,data,sources,coverage=m.load();pair=r.pair_selector(controller)
    order=[]
    for pol in ['original','two_paths_flat']:
        for sf in m.SUFFIXES:
            rows=[z for z in data['unmerged'][('budget_body',sf)] if z['symbol'] in r.SYMBOLS]
            values={}
            for name,select in [('six',controller.select),('six_EGPP_one_direction',pair)]:
                aa,ll,risk=select(rows,events['unmerged'],ends['unmerged'],pp['sector_map'],'sector2_counter_stops',pol,True)
                m.report.audit(ll,rows)
                if name!='six':assert r.pair_audit(aa,ll)['same_direction_pairs']==0
                met=m.report.distribution(aa,events['unmerged'],m.MONTHS)
                values[name]={k:met[k] for k in ['net','n','unknown','realized_cash_drawdown']}
            order.append(dict(policy=pol,scenario=sf,reverse_tie=values,reverse_pair_net_delta=values['six_EGPP_one_direction']['net']-values['six']['net']))
    write_matching(root/'pair-order-sensitivity.json',order)
    r.report(results,cells,stats,market['pairs'],market['coverage'],checks,attr)
    text=(root/'summary.md').read_text(encoding='utf-8')
    mark=text.index('## 原逐月筛选到底用了什么')
    b5=results['original']['B5'][m.PRIMARY]['six'];limited=results['original']['B5'][m.PRIMARY]['six_EGPP_one_direction'];strict=results['original']['B5'][m.SUFFIXES[-1]]['six']
    notes=['## 结果解读','',
        f"六品种B5主场景净额{b5['net']:.2f}元，{b5['positive_warning_months']}个正收益月、{b5['negative_warning_months']}个负收益月；最差预警月2026-04为{b5['warning_month_net']['2026-04']:.2f}元。最大回撤{b5['realized_cash_drawdown']:.2f}元是按逐笔已实现累计净额计算，不是最大单月亏损，也不含浮动持仓。",'',
        'EG/PP同向共同风险确实存在，但“同向最多一个”的先到先得限制在本批8个执行/未知政策场景下都降低净额，且回撤没有改善。当前结果不支持把相关性直接转换为硬性二选一；也不能由此否定共同风险，后续应以实际初始现金风险/资金约束讨论合计风险预算，而不是把两个一手当作两份分散风险。','',
        f"鸡蛋只有{stats['B5/six']['JD0']['n']}笔，主场景{stats['B5/six']['JD0']['net']:.2f}元，但触价2tick为{results['original']['B5']['corrected_touch/slip2']['six']['bins']['product']['JD0']:.2f}元，样本小、结论弱。白糖28笔主场景714元，最严压力−1164元，摩擦敏感。PVC81笔主场景1033元，最严压力仍为304元，改善较温和，但它仍是化工品种，不是与能化完全独立的风险来源。",'',
        f"六品种组合最严压力净额{strict['net']:.2f}元，其中EG贡献{strict['bins']['product']['EG0']:.2f}元，而白糖−1164元、红枣−1366元。组合为正不代表每个品种都有稳健优势。",'',
        '主场景相对原全品种组合，不再接受397笔原净亏2048.12元的已知交易，并因机会重竞争新增2笔合计131元；共同291笔完整结果和费用不变。增加的2笔分别为PP−12元、PVC+143元，说明此次确实不是只对旧成交求和。','']
    text=text[:mark]+'\n'.join(notes)+'\n'+text[mark:]
    periods={}
    for model in ['B2','B5']:
        periods[model]={}
        for pol in results:
            periods[model][pol]={}
            for sf in m.SUFFIXES:
                months=results[pol][model][sf]['six']['warning_month_net']
                periods[model][pol][sf]={label:sum(v for k,v in months.items() if fun(k)) for label,fun in [
                    ('2025-09_to_12',lambda k:k<'2026'),('2026-01_to_06',lambda k:'2026-01'<=k<='2026-06'),('2026-07_to_10',lambda k:k>='2026-07')]}
    write_matching(root/'period-results.json',periods)
    block=['## 三个原有时期分别看','', '|B5主政策场景|2025年9—12月|2026年1—6月|2026年7—10月|','|---|---:|---:|---:|']
    for sf in m.SUFFIXES:block.append('|'+sf+'|'+'|'.join(f'{v:.2f}' for v in periods['B5']['original'][sf].values())+'|')
    block+=['','三期已知净额在四场景均为正，说明本次组合并非仅靠近期合计拉正；但前两期在最严压力下仅约207/960元，不能忽略剩余未知、成本模型误差及事后选品种偏差。各期笔数、长度和行情覆盖不同。','']
    text=text.replace('## EG和PP同向最多一个：独立风险对照','\n'.join(block)+'\n## EG和PP同向最多一个：独立风险对照')
    byproduct=['## 六个品种各自的压力敏感性','', '|B5六品种中的贡献|触价1tick|触价2tick|延迟1tick|延迟2tick|','|---|---:|---:|---:|---:|']
    for sym in r.SYMBOLS:byproduct.append('|'+m.NAMES[sym]+'|'+'|'.join(f"{results['original']['B5'][sf]['six']['bins']['product'].get(sym,0):.2f}" for sf in m.SUFFIXES)+'|')
    byproduct+=['','四场景会改变成交、资格守卫和机会竞争，不能把它们理解为同一组交易简单加减成本，也不能把场景收益相加。','']
    text=text.replace('## 四压力与两种未知占用政策：六品种净额','\n'.join(byproduct)+'\n## 四压力与两种未知占用政策：六品种净额')
    text+='\n## 同触发时间反向排序敏感性\n\n|政策/场景|反序六品种净额|反序加EG/PP限制净额|净额差|\n|---|---:|---:|---:|\n'
    for z in order:
        a=z['reverse_tie']['six'];b=z['reverse_tie']['six_EGPP_one_direction']
        text+=f"|{z['policy']}/{z['scenario']}|{a['net']:.2f}|{b['net']:.2f}|{z['reverse_pair_net_delta']:+.2f}|\n"
    text+='\n仅改变同触发/预警时间下的代码并列顺序，未按未来盈亏排序。它是内部机会排序敏感性，不是完整逐报价或账户浮动权益验证。\n'
    (root/'summary.md').write_text(text,encoding='utf-8')
    # All monthly rows must reconcile to full known cash and trade counts.
    for case,months in cells.items():
        model,variant=case.split('/')
        met=results['original'][model][m.PRIMARY][variant]
        assert abs(sum(z['net'] for month in months.values() for z in month.values())-met['net'])<1e-6
        assert sum(z['n'] for month in months.values() for z in month.values())==met['n']
    done=m.old.read(root/'completion.json')
    done['sources'][str(pathlib.Path(__file__).resolve())]=m.old.sha(__file__)
    done['artifacts']={p.name:m.old.sha(p) for p in root.iterdir() if p.is_file() and p.name!='completion.json'}
    (root/'completion.json').write_text(json.dumps(done,ensure_ascii=False,indent=2),encoding='utf-8')
    assert all(m.old.sha(root/k)==h for k,h in done['artifacts'].items())
    print(json.dumps(order,ensure_ascii=False),flush=True)


if __name__=='__main__':run()
