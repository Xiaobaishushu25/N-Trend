"""Independently reconcile followup tables, risk gates and serialized forecasts."""
import argparse
import collections
from decimal import Decimal
import pathlib

import numpy as np

from factor_study_data import read,rows,new_output,write,seal,verify_seal
from factor_study_models import target_samples,predict


def verify(run,output):
    run=pathlib.Path(run)
    for name in ('replay','evaluate','reassess','report'):verify_seal(run/name)
    summary=read(run/'evaluate/summary.json')
    protocol=read(run/'evaluate/protocol.json')
    context=read(run/'replay/context.json')
    original=pathlib.Path(context['original_replay'])
    verify_seal(original)
    old_context=read(original/'context.json');verify_seal(old_context['prepared'])
    snapshots={s['candidate_id']:s for s in rows(pathlib.Path(old_context['scan'])/'snapshots.jsonl')}
    checked=0
    for path in (run/'evaluate').glob('*.ledger.jsonl'):
        name=path.name.removesuffix('.ledger.jsonl');strategy,slip=name.rsplit('_',1)
        slip=slip.removesuffix('tick')
        if strategy in summary['structures']['variants']:
            item=summary['structures']['variants'][strategy]
            expected=item['metrics'][slip]['post_training'];cap=item['variant']['cap']
            nf=item['variant']['no_follow'];start=summary['structures']['evaluation_start']
        else:
            expected=summary['b_models']['strategies'][strategy]['metrics'][slip]
            cap=500.;nf=5;start=summary['b_models']['evaluation_start']
        chosen=[];active={}
        for row in rows(path):
            if row['decision']!='accepted':continue
            label=row['label'];s=snapshots[row['candidate_id']]
            if label['plan']['planned_loss_yuan']>cap+1e-8:raise ValueError('planned cap violated')
            if s['quality']['quality_score']<protocol['fixed_score_min']:raise ValueError('shape admission changed')
            if active.get(row['symbol'],'')>row['warning_ts']:raise ValueError('overlapping product trades')
            active[row['symbol']]=label.get('release_ts') or (label['exit_ts'] if label['status'] in
                {'closed','invalidated','untriggered','invalid_target'} else '9999-12-31 23:59:59')
            if row['warning_ts']<start:continue
            if strategy.endswith('_positive_ev') and row['score']<=0:raise ValueError('nonpositive EV admitted')
            if nf is None and label['reason']=='no_follow':raise ValueError('disabled exit still used')
            chosen.append(label)
        closed=[l for l in chosen if l['status']=='closed']
        cash=sum((Decimal(str(l['net_yuan'])) for l in closed),Decimal(0))
        if len(closed)!=expected['closed'] or abs(float(cash)-expected['net_yuan'])>1e-5:
            raise ValueError('table and ledger do not reconcile: '+strategy)
        checked+=1
    if checked!=32:raise ValueError('incomplete preregistered strategy ledgers')
    artifacts={(a['model'],a['start']):a for a in read(run/'evaluate/model-artifacts.json') if a['status']=='fit'}
    samples=collections.defaultdict(list)
    for record in rows(run/'evaluate/forecasts.jsonl'):
        key=record['model'],record['fold_start']
        a=artifacts[key]
        if not a['training_last_exit']<record['fold_start']<=snapshots[record['candidate_id']]['warning_ts']<a['end']:
            raise ValueError('forecast chronology violated')
        if len(samples[key])<10:samples[key].append(record)
    forecasts_checked=0
    for key,records in samples.items():
        features=target_samples([snapshots[r['candidate_id']] for r in records],
                                {r['candidate_id']:r['label'] for r in records})
        predicted=predict(features,artifacts[key]['artifact'])
        if not np.allclose(predicted,[r['predicted_net_yuan'] for r in records],rtol=1e-10,atol=1e-6):
            raise ValueError('serialized model does not reproduce forecasts')
        forecasts_checked+=len(records)
    for row in read(run/'reassess/refitted-ablations.json'):
        if 'training_last_exit' in row and row['training_last_exit']>=row['start']:
            raise ValueError('maturity contaminated training cutoff')
    out=new_output(output)
    result=dict(status='passed',ledgers_reconciled=checked,forecasts_reproduced=forecasts_checked,
                planned_risk_and_shape_gates=True,no_follow_switch=True,training_cutoffs=True,
                scope='Cash is conditional on priced outcomes; unresolved labels remain unresolved.')
    write(out/'verification.json',result)
    seal(out,'followup-independent-verification',[run/name/'manifest.json' for name in ('replay','evaluate','reassess','report')])
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',required=True);parser.add_argument('--out',required=True)
    args=parser.parse_args();print(verify(args.run,args.out))
