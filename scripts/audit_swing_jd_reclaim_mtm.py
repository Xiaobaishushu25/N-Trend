"""Minute-close liquidation-equity audit, frozen decisions, no strategy change."""
import collections
import importlib.util
import json
import sys
import research_swing_jd_reclaim_robustness as r

RUN=r.RUN/'mtm'
R=r.R

def run():
    RUN.mkdir(exist_ok=False);write=lambda n,x:r.b.z.s.m.old.write(RUN/n,x)
    write('protocol.json',dict(purpose='Riskdiagnostic only; comparefrozenE20andFULL mainhistory withoutchangingorders.',
        prices='Actualmember1m CLOSE afterentrythroughbarbeforeexit; exitsuseactualsettledPnL. No pre-entryintraminutewicks, no fabricatedmissingquotes. Availableminuteclose samplingisnot tickequityorfullintraminuteworstloss.',
        equity='Priorclosednet + currentunrealized atminuteclose minuspaidopenfee andhypotheticalclosingfee/adverse1tickexit slip. Realsettlednetalreadyincludesbothfees/slip. Fractional300plannedrisk normalizationalso shown, notexecutablefractionallots.',
        unfinished='Marklastactualquote andreportfloatingcomponentseparately; neverreplacebaseunknownPnLwith0orclaimmarkedpositionclosed. No margin/forcedliquidation/accountreturn.',script_sha256=r.sha(__file__)))
    spec=R(r.SNAP/'portfolio-data.json');fees=R(r.SNAP/'fees.json')['products']['JD'];external=R(r.SNAP/'external-sources.json');maps={};sources={}
    for per in ['early','recent']:
        paths=r.f.t.period_paths(per)[0];path=paths['contracts'];assert r.sha(path)==external[path];maps[per]={k:v['minutes'] for k,v in R(path).items() if k.startswith('DCE.jd')};sources[path]=r.sha(path)
    # Load savedstandaloneadmission only, independentofmutableportfoliohelpers.
    sys.path.insert(0,str(r.SNAP));loader=importlib.util.spec_from_file_location('frozen_jd_reclaim_admission',r.SNAP/'replay.py');frozen=importlib.util.module_from_spec(loader);loader.loader.exec_module(frozen)
    rows=[]
    for line in (r.SNAP/'individual-decisions.jsonl').open(encoding='utf-8'):
        x=json.loads(line)
        if x['model']=='E20S1' and x['suffix']=='corrected_touch/slip1' and x['row']['period']!='july':rows.append(x['row'])
    base=frozen.admit(rows,spec['ends'],'original',spec['metadata'])
    full=[json.loads(line)['row'] for line in (r.SNAP/'evidence/reclaim-followup/overlay/accepted-main-primary.jsonl').open(encoding='utf-8') if json.loads(line)['model']=='E20_PLUS_RECLAIM_R2']
    charge=lambda leg,price:fees[leg]['fixed_yuan_per_lot']+fees[leg]['notional_rate']*price*10
    result={};curves=[]
    for name,admitted in [('E20S1',base),('FULL',full)]:
        trades=sorted([x for x in admitted if r.f.EV(x).get('entry_ts')],key=lambda x:(r.f.EV(x)['entry_ts'],x['event_key']));cash=norm=0.;peak=normpeak=0.;dd=normdd=0.;last_end='';openrecord=None;count=0
        def mark(ts,equity,equitynorm,kind):
            nonlocal peak,normpeak,dd,normdd,count
            peak=max(peak,equity);normpeak=max(normpeak,equitynorm);dd=max(dd,peak-equity);normdd=max(normdd,normpeak-equitynorm);count+=1
            curves.append(dict(model=name,ts=ts,equity=equity,normalized_equity=equitynorm,kind=kind))
        for row in trades:
            e=r.f.EV(row);assert not last_end or last_end<e['entry_ts'];sign=r.f.direction(row);qty=300/(row['planned_risk']*10);entry=e['fill'];openfee=charge('open',entry)
            minute=maps[row['period']][row['execution_contract']];selected=[q for q in minute if q['datetime']>=e['entry_ts'] and (e['exit_ts'] is None or q['datetime']<e['exit_ts'])]
            assert e['status'] in ['closed','unfinished'],e['status']
            for q in selected:
                assert q['close']>0;hypothetical_exit=q['close']-sign*row['tick_size']*row['slip'];closefee=max(charge('close_today',hypothetical_exit),charge('close_yesterday',hypothetical_exit));floating=(hypothetical_exit-entry)*sign*10-openfee-closefee
                mark(q['datetime'],cash+floating,norm+floating*qty,'marked')
            if e['status']=='closed':cash+=e['net_amount'];norm+=e['net_amount']*qty;mark(e['exit_ts'],cash,norm,'settled');last_end=e['exit_ts']
            else:
                assert selected;last=selected[-1];openrecord=dict(event_key=row['event_key'],last_quote_ts=last['datetime'],last_quote_close=last['close'],marked_floating_after_costs=floating,marked_normalized_floating_after_costs=floating*qty,entry_price=entry,stop=row['stop'],planned_cash_risk=row['planned_risk']*10);last_end=minute[-1]['datetime']
        expected=R(r.SNAP/'expected-main.json')['original/corrected_touch/slip1/'+('E20S1' if name=='E20S1' else 'E20_PLUS_RECLAIM_R2')]
        assert abs(cash-expected['net'])<1e-7;assert abs(norm-expected['normalized_net'])<1e-7
        result[name]=dict(closed_net=cash,closed_normalized_net=norm,minute_close_cash_dd=dd,minute_close_normalized_dd=normdd,closed_only_normalized_dd=expected['normalized_realized_dd'],marked_points=count,open_position=openrecord,
            last_marked_equity=cash+(openrecord['marked_floating_after_costs'] if openrecord else 0),last_marked_normalized_equity=norm+(openrecord['marked_normalized_floating_after_costs'] if openrecord else 0))
    with (RUN/'equity.jsonl').open('x',encoding='utf-8') as out:
        for q in curves:out.write(json.dumps(q,ensure_ascii=False)+'\n')
    write('results.json',result);write('sources.json',sources)
    print(json.dumps(result,ensure_ascii=False),flush=True)

if __name__=='__main__':run()
