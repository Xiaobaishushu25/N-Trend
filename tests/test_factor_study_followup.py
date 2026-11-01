import copy
import pathlib
import sys
import unittest

import numpy as np

sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'scripts'))
from factor_study_analysis import (LabelMaps, policy_evaluation, observed_training,
                                  asof_label, parameter_evidence)
from factor_study_data import read
from factor_study_execution import evaluate,capacity,metrics
from factor_study_models import fit_model,predict
from research_factor_study_followup import (variants,scenario_key,admission_labels,positive_threshold,
                                           strategy_candidate,validate_ledger,model_spec)
from test_factor_study import snapshot,bar,calendar,CONFIG,SPEC


PROTOCOL=read(pathlib.Path(__file__).resolve().parents[1]/'scripts/factor-study-followup-v1.json')


class MaturityTests(unittest.TestCase):
    def label(self):
        s=snapshot()
        l=evaluate(s,[bar(15,100,104,99,102),bar(16,103,125,101,124)],[],SPEC,calendar(),CONFIG,'cash200',1)
        l.update(exit_ts='2026-08-06 10:00:00',release_ts='2026-08-06 10:00:00')
        return s,l

    def test_outer_can_mature_without_exposing_future_payoff_to_training(self):
        s,l=self.label();maps=LabelMaps();maps['cash200_1tick']={'a':l};maps['cash200_2tick']={'a':l}
        start,end='2026-08-03 00:00:00','2026-08-05 00:00:00'
        early,_=policy_evaluation([s],maps,{'a':80.},70.,start,end)
        mature,ledger=policy_evaluation([s],maps,{'a':80.},70.,start,end,outcome_end='2026-08-07 00:00:00')
        self.assertEqual(early['1']['statuses'],{'unfinished':1})
        self.assertEqual(mature['1']['closed'],1)
        self.assertEqual(ledger['1'][0]['fold_end_status'],'unfinished')
        self.assertFalse(observed_training([s],{'a':l},end,'2026-08-07 00:00:00'))

    def test_maturity_does_not_admit_future_warning(self):
        s,l=self.label();b=snapshot('b','2026-08-06 09:15:00')
        other=dict(l,candidate_id='b',warning_ts=b['warning_ts'])
        maps=LabelMaps();maps['cash200_1tick']={'a':l,'b':other};maps['cash200_2tick']=maps['cash200_1tick']
        _,ledger=policy_evaluation([s,b],maps,{'a':80.,'b':99.},70.,'2026-08-03','2026-08-05',outcome_end='2026-08-07')
        self.assertEqual([r['candidate_id'] for r in ledger['1']],['a'])

    def test_genuine_ambiguity_stays_unknown_after_maturity(self):
        s=snapshot();l=evaluate(s,[bar(15,99,125,94,101)],[],SPEC,calendar(),CONFIG,'cash200',1)
        self.assertEqual(asof_label(l,'2026-09-01')['status'],'ambiguous')
        self.assertIsNone(asof_label(l,'2026-09-01')['net_yuan'])

    def test_rejected_risk_is_known_at_warning_not_pending(self):
        label=dict(status='filtered_risk',entry=None,entry_ts=None,exit_ts=None)
        self.assertEqual(asof_label(label,'2026-08-05')['status'],'filtered_risk')

    def test_observation_cutoff_cannot_precede_cohort_end(self):
        with self.assertRaises(ValueError):
            policy_evaluation([],{}, {},70.,'2026-08-01','2026-08-10',outcome_end='2026-08-09')

    def test_known_through_final_minute_is_inclusive_only_for_outer_maturity(self):
        _,l=self.label();end=l['exit_ts']
        self.assertEqual(asof_label(l,end)['status'],'unfinished')
        self.assertEqual(asof_label(l,end,inclusive=True)['status'],'closed')

    def test_asof_masks_future_exit_price_fills_and_excursion(self):
        _,l=self.label()
        l['fills'][-1]['ts']=l['exit_ts']
        masked=asof_label(l,'2026-08-05 00:00:00')
        self.assertEqual(len(masked['fills']),1)
        self.assertIsNone(masked['reason']);self.assertIsNone(masked['peak'])
        self.assertIsNone(masked['release_ts']);self.assertNotIn('close_fee',masked)
        self.assertIsNone(masked['gross_yuan'])

    def test_small_unknown_subset_is_reported_without_blanket_veto(self):
        rows=[];days=[]
        for month in (2,3,4):
            start=f'2026-{month:02}-01';end=f'2026-{month+1:02}-01'
            daily={f'2026-{month:02}-{d:02}':10. for d in range(1,11)};days.extend(daily)
            for name,profit in [('none',100.),('feature:retracement',0.)]:
                rows.append(dict(start=start,end=end,removed=name,metrics={'1':dict(net_yuan=profit,closed=100,
                            unknown=1,known_fraction=.99)},daily_yuan=daily if profit else {d:0. for d in daily}))
        protocol=dict(statistics=dict(bootstrap_draws=200,fdr=.1),optimization=dict(seed=17),
                      validation=dict(minimum_known_fraction=.95))
        result=parameter_evidence(rows,['retracement'],protocol,days)[0]
        self.assertEqual(result['status'],'provisional_incremental_value_on_priced_subset')
        self.assertTrue(result['priced_subset_only']);self.assertEqual(result['positive_folds'],3)
        self.assertTrue(all(r['unknown']>0 for r in result['folds']))


class StructureTests(unittest.TestCase):
    def test_exactly_eight_variants_without_parameter_search(self):
        values=variants(PROTOCOL)
        self.assertEqual(len(values),8);self.assertEqual(len({v['id'] for v in values}),8)
        self.assertIn('cash200_nf5_cap500',{v['id'] for v in values})

    def test_source_scenario_mapping_does_not_change_with_risk_cap(self):
        a,b=dict(target='s1',no_follow=None,cap=200),dict(target='s1',no_follow=None,cap=500)
        self.assertEqual(scenario_key(a,2),scenario_key(b,2))
        self.assertEqual(scenario_key(a,2),'s1_nf_off_2tick')

    def test_cap_200_filters_without_moving_stop_or_using_future_loss(self):
        l=dict(plan=dict(planned_risk_ok=True,planned_loss_yuan=300.,stop=95.),net_yuan=1000.)
        small=admission_labels({'a':l},200.)['a'];large=admission_labels({'a':l},500.)['a']
        self.assertFalse(small['plan']['planned_risk_ok']);self.assertTrue(large['plan']['planned_risk_ok'])
        self.assertEqual(small['plan']['stop'],95.);self.assertTrue(l['plan']['planned_risk_ok'])

    def test_no_follow_off_waits_for_later_target(self):
        import datetime as dt
        from factor_study_data import stamp
        s=snapshot();start=dt.datetime(2026,8,3,9,15);bars=[]
        for i in list(range(60))+list(range(75,90)):
            t=start+dt.timedelta(minutes=i)
            bars.append(dict(start=stamp(t),ts=stamp(t+dt.timedelta(minutes=1)),open=100.,high=102.,low=99.,close=100.,volume=1.,hold=0.))
        t=start+dt.timedelta(minutes=90)
        bars.append(dict(start=stamp(t),ts=stamp(t+dt.timedelta(minutes=1)),open=100.,high=125.,low=99.,close=124.,volume=1.,hold=0.))
        on=evaluate(s,bars,[],SPEC,calendar(),CONFIG,'cash200',1)
        off=evaluate(s,bars,[],SPEC,calendar(),dict(CONFIG,no_follow_bucket=None),'cash200',1)
        self.assertEqual(on['reason'],'no_follow');self.assertEqual(off['reason'],'target')
        self.assertEqual(on['entry'],off['entry']);self.assertEqual(on['plan']['stop'],off['plan']['stop'])

    def test_positive_ev_is_strictly_positive(self):
        s=snapshot();l=evaluate(s,[bar(15,100,125,99,124)],[],SPEC,calendar(),CONFIG,'cash200',1)
        for score in (-1.,0.):
            self.assertEqual(capacity([s],{'a':l},{'a':score},{'a':positive_threshold()}),[])
        self.assertEqual(len(capacity([s],{'a':l},{'a':.01},{'a':positive_threshold()})),1)

    def test_no_trade_is_not_declared_a_profitable_candidate(self):
        empty=metrics([])
        self.assertEqual(empty['net_yuan'],0.)
        self.assertEqual(strategy_candidate(empty,empty,{'5':{'interval95':[0.,0.]}},100),'insufficient_trades')

    def test_ledger_validation_retains_unknown_occupancy(self):
        s=snapshot();l=evaluate(s,[],[],SPEC,calendar(),CONFIG,'cash200',1)
        ledger=[dict(decision='accepted',symbol='C0',warning_ts='2026-08-03',label=l),
                dict(decision='accepted',symbol='C0',warning_ts='2026-08-04',label=l)]
        with self.assertRaises(ValueError):validate_ledger(ledger)


class BModelTests(unittest.TestCase):
    def samples(self):
        rng=np.random.default_rng(17);r=rng.uniform(.1,.9,400);a=rng.uniform(-1,1,400)
        return [dict(features={'retracement':float(x),'a':float(y)},symbol='C0',direction='up',group_id=str(i),
                     warning_ts='2026-08-03',exit_ts='2026-08-04',net_yuan=float(500*(x-.5)*y))
                for i,(x,y) in enumerate(zip(r,a))]

    def test_only_b_receives_knots_and_interaction_works(self):
        data=self.samples();train,test=data[:300],data[300:]
        model=fit_model(train,['retracement','a'],'interaction',.1,[('retracement','a')],knot_features=['retracement'])
        self.assertTrue(next(t for t in model['transforms'] if t['name']=='retracement')['knots'])
        self.assertFalse(next(t for t in model['transforms'] if t['name']=='a')['knots'])
        predicted=np.array(predict(test,model));y=np.array([r['net_yuan'] for r in test])
        self.assertLess(np.mean((predicted-y)**2),1.)

    def test_four_frozen_model_definitions(self):
        definitions={name:model_spec(name,PROTOCOL) for name in PROTOCOL['b_models']}
        self.assertEqual(len(definitions),4)
        self.assertNotIn('retracement',definitions['without_retracement']['names'])
        self.assertEqual(definitions['b_conditional']['interactions'],PROTOCOL['b_interactions'])
        self.assertTrue(all(v['alpha']==10. for v in definitions.values()))


if __name__=='__main__':unittest.main()
