import copy
import datetime as dt
import pathlib
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'scripts'))
from factor_study_data import Calendar,aggregate,stamp,write,seal,verify_seal
from factor_study_execution import evaluate,planned,cash_target,round_tick,capacity,audit_label,commission
from factor_study_models import fit_model,predict,weight_candidates
from factor_study_analysis import observed_training,asof_label,fdr_adjust,paired_blocks,outer_windows,inner_windows,LabelMaps,policy_evaluation
from factor_study_quote_audit import check_quote


CONFIG=dict(lots=1,planned_loss_cap_yuan=500.,target_net_yuan=200.,pending_buckets=12,
            holding_buckets=60,no_follow_bucket=5,no_follow_mfe_r=.5,forecast_slip_ticks=1)
SPEC=dict(roundtrip_fixed_yuan_per_lot=2.)


def calendar():
    start=dt.date(2026,7,1)
    records=[dict(date=(start+dt.timedelta(days=i)).isoformat(),trading=(start+dt.timedelta(days=i)).weekday()<5) for i in range(150)]
    # National holiday: last pre-break evening has no trading session.
    for r in records:
        if '2026-10-01'<=r['date']<='2026-10-07':r['trading']=False
    return Calendar(dict(records=records),{'C0':1380,'PB0':60})


def snapshot(key='a',warning='2026-08-03 09:15:00',direction='up'):
    return dict(candidate_id=key,group_id=key,symbol='C0',contract='DCE.c2609',warning_ts=warning,available_at=warning,
                tick=1.,multiplier=10.,quality=dict(status='ok',quality_score=80.),
                event=dict(direction=direction,entry=100.,stop=95. if direction=='up' else 105.,target=110. if direction=='up' else 90.))


def bar(minute,o,h,l,c):
    start=dt.datetime(2026,8,3,9,minute)
    return dict(start=stamp(start),ts=stamp(start+dt.timedelta(minutes=1)),open=o,high=h,low=l,close=c,volume=1.,hold=0.)


class ReplayTests(unittest.TestCase):
    def run_trade(self,s,bars,**kwargs):
        return evaluate(s,bars,kwargs.get('gaps',[]),SPEC,calendar(),kwargs.get('config',CONFIG),kwargs.get('policy','cash200'),kwargs.get('slip',1))

    def test_net_cash_target_covers_fees_and_both_sides_slippage(self):
        s=snapshot();r=self.run_trade(s,[bar(15,100,104,99,102),bar(16,103,125,101,124)])
        self.assertEqual(r['status'],'closed');self.assertGreaterEqual(r['net_yuan'],200)
        self.assertTrue(r['hit200']);audit_label(s,r)

    def test_short_target_rounds_down_and_is_profitable(self):
        s=snapshot(direction='down')
        r=self.run_trade(s,[bar(15,100,101,96,98),bar(16,97,99,74,76)])
        self.assertEqual(r['status'],'closed');self.assertGreaterEqual(r['net_yuan'],200);audit_label(s,r)

    def test_cash_target_not_capped_by_s1(self):
        s=snapshot();bs=[bar(15,100,104,99,102),bar(16,103,125,101,124)]
        cash=self.run_trade(s,bs);s1=self.run_trade(s,bs,policy='s1')
        self.assertGreater(cash['target'],s['event']['target']);self.assertLess(s1['net_yuan'],200)

    def test_intraminute_entry_stop_target_order_is_unknown(self):
        r=self.run_trade(snapshot(),[bar(15,99,125,94,101)])
        self.assertEqual(r['status'],'ambiguous');self.assertIsNone(r['net_yuan']);self.assertTrue(r['possibly_entered'])

    def test_after_stop_recovery_does_not_count_as_win(self):
        r=self.run_trade(snapshot(),[bar(15,100,102,99,101),bar(16,94,125,93,124)])
        self.assertEqual(r['reason'],'stop');self.assertLess(r['net_yuan'],0);self.assertFalse(r['hit200'])

    def test_gap_risk_can_exceed_planned_500_and_is_not_deleted(self):
        r=self.run_trade(snapshot(),[bar(15,160,164,159,162),bar(16,90,92,89,91)])
        self.assertTrue(r['plan']['planned_risk_ok']);self.assertGreater(r['risk_yuan'],500)
        self.assertLess(r['net_yuan'],-500)

    def test_oversized_plan_does_not_enter(self):
        s=snapshot();s['event']['stop']=40
        r=self.run_trade(s,[bar(15,100,125,99,124)])
        self.assertEqual(r['status'],'filtered_risk');self.assertFalse(r['fills'])

    def test_risk_cap_includes_exit_slip_and_accepts_exact_boundary(self):
        s=snapshot();s['event']['stop']=52.
        zero=dict(roundtrip_fixed_yuan_per_lot=0.)
        p=planned(s,zero,CONFIG);self.assertEqual(p['planned_loss_yuan'],500.);self.assertTrue(p['planned_risk_ok'])
        s['event']['stop']=51.
        self.assertFalse(planned(s,zero,CONFIG)['planned_risk_ok'])

    def test_close_today_uses_exchange_day_across_friday_midnight(self):
        fee=lambda x:dict(fixed_yuan_per_lot=x,notional_rate=0.)
        spec=dict(open=fee(1.),close_today=fee(2.),close_yesterday=fee(3.))
        self.assertEqual(commission(spec,100.,101.,10.,'2026-08-07 21:01:00','2026-08-08 00:30:00',calendar()),(1.,2.))
        self.assertEqual(commission(spec,100.,101.,10.,'2026-08-07 21:01:00','2026-08-11 09:30:00',calendar()),(1.,3.))

    def test_cost_pressure_keeps_same_target(self):
        s=snapshot();bs=[bar(15,100,104,99,102),bar(16,103,125,101,124)]
        one,two=self.run_trade(s,bs),self.run_trade(s,bs,slip=2)
        self.assertEqual(one['target'],two['target']);self.assertLess(two['net_yuan'],one['net_yuan'])

    def test_missing_minute_is_not_a_fictitious_fill(self):
        r=self.run_trade(snapshot(),[bar(17,100,125,99,124)],gaps=['2026-08-03 09:16:00'])
        self.assertEqual(r['status'],'missing_data');self.assertFalse(r['fills'])

    def test_warning_bar_cannot_fill(self):
        r=self.run_trade(snapshot(),[bar(14,100,125,99,124)])
        self.assertEqual(r['status'],'pending');self.assertFalse(r['fills'])

    def test_no_follow_at_fifth_bucket_close_only(self):
        s=snapshot();bs=[]
        start=dt.datetime(2026,8,3,9,15)
        for i in range(75):
            t=start+dt.timedelta(minutes=i)
            bs.append(dict(start=stamp(t),ts=stamp(t+dt.timedelta(minutes=1)),open=100.,high=102.,low=99.,close=100.,volume=1.,hold=0.))
        r=self.run_trade(s,bs)
        self.assertEqual(r['reason'],'no_follow');self.assertEqual(r['exit_ts'],'2026-08-03 10:30:00')

    def test_same_minute_exit_reconciles(self):
        s=snapshot();r=self.run_trade(s,[bar(15,100,125,99,124)])
        self.assertEqual(r['entry_ts'],r['exit_ts']);audit_label(s,r)

    def test_lunch_break_does_not_count_as_held_bucket(self):
        s=snapshot();start=dt.datetime(2026,8,3,9,15);bs=[]
        for i in list(range(60))+list(range(75,90)):
            t=start+dt.timedelta(minutes=i)
            bs.append(dict(start=stamp(t),ts=stamp(t+dt.timedelta(minutes=1)),open=100.,high=102.,low=99.,close=100.,volume=1.,hold=0.))
        result=self.run_trade(s,bs)
        self.assertEqual(result['reason'],'no_follow');self.assertEqual(result['exit_ts'],'2026-08-03 10:45:00')

    def test_independent_source_quote_check(self):
        s=snapshot();bs=[bar(15,100,104,99,102),bar(16,103,125,101,124)]
        label=self.run_trade(s,bs)
        self.assertFalse(check_quote(s,label,bs[0],bs[1],SPEC,calendar()))

    def test_favorable_target_gap_is_explicitly_conservative(self):
        s=snapshot();bs=[bar(15,100,104,99,102),bar(16,130,132,129,131)]
        label=self.run_trade(s,bs)
        self.assertTrue(check_quote(s,label,bs[0],bs[1],SPEC,calendar()))
        self.assertLess(label['exit'],bs[1]['low'])

    def test_source_quote_check_rejects_invented_entry(self):
        s=snapshot();bs=[bar(15,100,104,99,102),bar(16,103,125,101,124)]
        label=self.run_trade(s,bs);label['entry']+=1
        with self.assertRaises(ValueError):check_quote(s,label,bs[0],bs[1],SPEC,calendar())

    def test_independent_fee_check_rejects_wrong_charge(self):
        s=snapshot();bs=[bar(15,100,104,99,102),bar(16,103,125,101,124)]
        label=self.run_trade(s,bs);label['fees']+=1
        with self.assertRaises(ValueError):check_quote(s,label,bs[0],bs[1],SPEC,calendar())

    def test_unknown_capacity_reserves_following_opportunity(self):
        a=snapshot();b=snapshot('b','2026-08-03 09:30:00')
        x=self.run_trade(a,[bar(15,100,102,99,101),bar(18,100,102,99,101)],gaps=['2026-08-03 09:17:00'])
        y=copy.deepcopy(x);y['candidate_id']='b'
        ledger=capacity([a,b],{'a':x,'b':y},{'a':80,'b':90},{'a':70,'b':70})
        self.assertEqual([r['decision'] for r in ledger],['accepted','skipped_occupied'])

    def test_ambiguous_profit_but_both_terminal_releases_capacity(self):
        a=snapshot();b=snapshot('b','2026-08-03 09:30:00')
        x=self.run_trade(a,[bar(15,99,125,94,101)]);y=copy.deepcopy(x);y['candidate_id']='b'
        ledger=capacity([a,b],{'a':x,'b':y},{'a':80,'b':90},{'a':70,'b':70})
        self.assertEqual([r['decision'] for r in ledger],['accepted','accepted'])

    def test_null_quality_score_is_not_sorted_as_number(self):
        a=snapshot();r=self.run_trade(a,[])
        self.assertEqual(capacity([a],{'a':r},{'a':None},{'a':70}),[])


class DataTests(unittest.TestCase):
    def test_calendar_friday_night_and_holiday(self):
        c=calendar()
        self.assertEqual(c.day('2026-08-07 21:05:00'),'2026-08-10')
        self.assertEqual(c.day('2026-08-08 00:30:00'),'2026-08-10')
        self.assertFalse(c.expected(dt.datetime(2026,9,30,21,0),'C0'))
        self.assertFalse(c.expected(dt.datetime(2026,8,3,10,15),'C0'))
        self.assertTrue(c.expected(dt.datetime(2026,8,3,10,30),'C0'))

    def test_aggregate_refuses_partial_bucket(self):
        bs=[bar(m,100,101,99,100) for m in range(15,30)]
        full,partial=aggregate(bs);self.assertEqual(len(full),1);self.assertEqual(full[0]['ts'],'2026-08-03 09:30:00')
        full,partial=aggregate(bs[:-1]);self.assertFalse(full);self.assertEqual(len(partial),1)

    def test_decimal_tick_rounding(self):
        self.assertEqual(round_tick(100.01,.2,True),100.2)
        self.assertEqual(round_tick(100.01,.2,False),100.)

    def test_manifest_detects_output_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=pathlib.Path(tmp);write(p/'data.json',{'x':1});seal(p,'test',[]);verify_seal(p)
            (p/'data.json').write_text('{}')
            with self.assertRaises(ValueError):verify_seal(p)


class ModelTests(unittest.TestCase):
    def samples(self,n=400):
        rng=np.random.default_rng(7);x=rng.uniform(-1,1,(n,2))
        return [dict(candidate_id=str(i),group_id=str(i),symbol='C0',direction='up',warning_ts='2026-08-03',exit_ts='2026-08-04',
                     features={'x':a,'y':b},net_yuan=200*a*b) for i,(a,b) in enumerate(x)]

    def test_joint_model_discovers_pure_interaction(self):
        data=self.samples();train,test=data[:300],data[300:]
        linear=fit_model(train,['x','y'],'linear',.1)
        joint=fit_model(train,['x','y'],'interaction',.1,[('x','y')])
        y=np.array([r['net_yuan'] for r in test])
        self.assertLess(np.mean((predict(test,joint)-y)**2),.01*np.mean((predict(test,linear)-y)**2))

    def test_training_transform_does_not_change_with_future_extreme(self):
        data=self.samples();model=fit_model(data,['x'],'spline',1.)
        old=copy.deepcopy(model);new=copy.deepcopy(data[0]);new['features']['x']=1e6
        predict([new],model);self.assertEqual(model,old)

    def test_duplicate_feature_does_not_double_predictive_value(self):
        data=self.samples()
        for r in data:r['features']['copy']=r['features']['x'];r['net_yuan']=100*r['features']['x']
        a=fit_model(data,['x'],'linear',.1);b=fit_model(data,['x','copy'],'linear',.1)
        self.assertLess(np.max(np.abs(np.array(predict(data,a))-predict(data,b))),.1)

    def test_unfinished_label_not_observed_training(self):
        a=snapshot();a['quality']['status']='ok'
        labels={'a':dict(status='closed',exit_ts='2026-08-06 10:00:00',plan=dict(planned_risk_ok=True))}
        self.assertFalse(observed_training([a],labels,'2026-08-05','2026-08-06'))

    def test_group_crossing_boundary_purged(self):
        a=snapshot();b=snapshot('b','2026-08-05 09:15:00');b['group_id']=a['group_id']
        label=dict(status='closed',exit_ts='2026-08-04 10:00:00',plan=dict(planned_risk_ok=True))
        self.assertFalse(observed_training([a,b],{'a':label,'b':label},'2026-08-05','2026-08-06'))

    def test_asof_does_not_use_future_completed_profit(self):
        label=dict(status='closed',entry_ts='2026-08-04',exit_ts='2026-08-06',entry=100.,exit=120.,net_yuan=200.,net_r=2.,fees=2.,hit200=True)
        current=asof_label(label,'2026-08-05')
        self.assertEqual(current['status'],'unfinished');self.assertIsNone(current['net_yuan'])

    def test_sobol_candidates_reproducible_and_normalized(self):
        a=weight_candidates(256,7);self.assertEqual(a,weight_candidates(256,7))
        self.assertTrue(all(abs(sum(w)-1)<1e-12 and min(w)>=0 for _,w in a))

    def test_bh_monotone_adjustment(self):
        adjusted=fdr_adjust([.01,.04,None,.03])
        self.assertEqual(adjusted[0],.03);self.assertEqual(adjusted[2],None)

    def test_paired_blocks_preserve_common_date_shocks(self):
        days=[f'2026-08-{i:02}' for i in range(1,21)]
        ledger=[dict(decision='accepted',label=dict(status='closed',warning_ts=d+' 10:00:00',net_yuan=(-1)**i*200)) for i,d in enumerate(days)]
        result=paired_blocks(ledger,ledger,days,2000,5,7)
        self.assertEqual(result['interval95'],[0.,0.])

    def test_bootstrap_never_silently_drops_saturday_trade(self):
        days=[f'2026-08-{i:02}' for i in (3,4,5,6,7,10)]
        ledger=[dict(decision='accepted',label=dict(status='closed',warning_ts='2026-08-08 00:30:00',
                    warning_trading_day='2026-08-10',net_yuan=200.))]
        result=paired_blocks([],ledger,days,200,5,7)
        self.assertEqual(result['delta_total_yuan'],200.)
        with self.assertRaises(ValueError):paired_blocks([],ledger,days[:-1],200,5,7)

    def test_binary_auxiliary_is_probability_not_clipped_regression(self):
        data=self.samples()
        for r in data:r['hit200']=r['features']['x']>0
        model=fit_model(data,['x'],'linear',1.,target='hit200',task='binary')
        probabilities=np.asarray(predict(data,model))
        self.assertTrue(np.all((probabilities>0)&(probabilities<1)))
        self.assertLess(np.mean((probabilities-np.asarray([r['hit200'] for r in data]))**2),.1)

    def test_monthly_extension_has_six_months_training_and_three_past_inner_windows(self):
        start=dt.date(2025,7,31)
        days=[(start+dt.timedelta(days=i)).isoformat() for i in range(457) if (start+dt.timedelta(days=i)).weekday()<5]
        protocol=dict(start='2025-07-31 00:00:00',end='2026-10-31 00:00:00',
                      validation=dict(schedule='monthly',initial_months=6))
        index,begin,end=next(outer_windows(days,protocol))
        self.assertGreaterEqual(begin,'2026-01-31')
        inner=list(inner_windows(days,index,protocol));self.assertEqual(len(inner),3)
        self.assertTrue(all(a<b<=begin for a,b in inner))

    def test_cached_warm_capacity_equals_full_history_replay(self):
        a=snapshot();b=snapshot('b','2026-08-03 09:30:00');c=snapshot('c','2026-08-03 10:20:00')
        base=evaluate(a,[bar(15,100,104,99,102),bar(16,103,125,101,124)],[],SPEC,calendar(),CONFIG,'cash200',1)
        labels={}
        for s,exit_ts in ((a,'2026-08-03 10:15:00'),(b,'2026-08-03 10:16:00'),(c,'2026-08-03 10:25:00')):
            r=copy.deepcopy(base);r.update(candidate_id=s['candidate_id'],warning_ts=s['warning_ts'],exit_ts=exit_ts,release_ts=exit_ts)
            labels[s['candidate_id']]=r
        maps=LabelMaps();maps['cash200_1tick']=labels;maps['cash200_2tick']=labels
        scores={'b':90.,'c':95.};start,end='2026-08-03 09:30:00','2026-08-03 10:30:00'
        _,fast=policy_evaluation([a,b,c],maps,scores,70.,start,end)
        slow=capacity([a,b,c],labels,{'a':80.,**scores},{'a':70.,'b':70.,'c':70.})
        expected=[(r['candidate_id'],r['decision']) for r in slow if r['warning_ts']>=start]
        self.assertEqual([(r['candidate_id'],r['decision']) for r in fast['1']],expected)
        _,again=policy_evaluation([a,b,c],maps,scores,70.,start,end)
        self.assertEqual(fast,again)


if __name__=='__main__':unittest.main()
