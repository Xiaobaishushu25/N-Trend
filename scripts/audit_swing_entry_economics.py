"""Independent nominal fee and prewarning local-target checks for economic gate."""
import datetime as dt,json,math
import research_swing_entry_economics as r
w=r.w;RUN=r.RUN

def run():
    p=w.read(RUN/'protocol.json');plans=w.read(RUN/'warning-plans.json');count=0;costs=0;targets=0;provenance={}
    for family in r.FAMILIES:
        for q in ('early','recent'):
            fees=w.read(p['periods'][q]['fees']);pool={z['event_key']:z for z in w.read(p['sources'][family][q]['pool'])};provenance[p['periods'][q]['fees']]=w.sha(p['periods'][q]['fees'])
            for line in open(p['sources'][family][q]['raw'],encoding='utf-8'):
                z=json.loads(line)
                if z['scenario']!='P2_wave_adaptive/corrected_touch/slip1':continue
                e=pool[z['event_key']]['event'];f=fees['products'][z['symbol'].rstrip('0123456789').upper()];sign=1 if z['direction']=='up' else -1;tick=z['tick_size'];entry=z['entry']+sign*tick;stop=z['stop']-sign*tick
                if 'roundtrip_fixed_yuan_per_lot' in f:commission=f['roundtrip_fixed_yuan_per_lot']
                else:
                    charge=lambda name,price:f[name]['fixed_yuan_per_lot']+f[name]['notional_rate']*price*z['multiplier']
                    commission=charge('open',entry)+max(charge('close_today',stop),charge('close_yesterday',stop))
                nominal=2*tick+commission/z['multiplier'];pp=plans[family][z['event_key']];assert abs(nominal-pp['planned_roundtrip_cost_points'])<1e-8;assert abs(nominal-z['new_friction']*z['planned_risk'])<1e-8;costs+=1
                target=e['target'];local=None;prior=pool[z['event_key']]['prior'][-12:]
                for a,b,c in zip(prior,prior[1:],prior[2:]):
                    assert c['datetime']<e['warning_ts']
                    if (dt.datetime.fromisoformat(b['datetime'])-dt.datetime.fromisoformat(a['datetime'])).total_seconds()!=900 or (dt.datetime.fromisoformat(c['datetime'])-dt.datetime.fromisoformat(b['datetime'])).total_seconds()!=900:continue
                    pivot=b['high']>a['high'] and b['high']>c['high'] if sign>0 else b['low']<a['low'] and b['low']<c['low'];price=b['high'] if sign>0 else b['low']
                    if pivot and (price-z['entry'])*sign>0 and (target-price)*sign>0:target=price;local=b['datetime']
                target=(math.floor(target/tick+1e-9) if sign>0 else math.ceil(target/tick-1e-9))*tick
                assert abs(target-z['target'])<1e-7 and abs(target-pp['planned_target'])<1e-7;assert local==z['local_level_time'];targets+=1;count+=1
    w.write(RUN/'economic-implementation-audit.json',dict(warnings=count,independent_nominal_fee_slip_checks=costs,independent_pre_warning_local_target_checks=targets,fee_file_hashes=provenance,note='Consistency audit of frozen fee/slippage model, not independent historical daily fee or tick-execution verification. No actual fill/outcome used for nominal gate.'))
    print('Independent nominal-cost/target checks passed:',count,flush=True)
if __name__=='__main__':run()
