"""One disclosed post-main interaction: economic budget AND positive A net bodies."""
import datetime as dt,shutil
import research_swing_entry_economics as base
from swing_entry_economics import allows as original_allows
w=base.w;SNAP=base.SNAP;FAMILIES=base.FAMILIES;RUN=base.RUN/'body-interaction';MODELS=['base','budget10','body_net','budget_body']
def allows(name,f,p):
    return original_allows('budget10',f,p) and original_allows('body_net',f,p) if name=='budget_body' else original_allows(name,f,p)
def prepare():
    RUN.mkdir(exist_ok=False);p=w.read(base.RUN/'protocol.json');p.update(id=RUN.name,created_at=dt.datetime.now(dt.timezone.utc).isoformat(),models=MODELS,script_sha256=w.sha(__file__),parent_results_sha256=w.sha(base.RUN/'results.json'),
        disclosure='Exploratory AFTER main8-rule results. budget10 improved total in both A families/all stresses; positive-body standalone slightly improved main total but worsened some distribution metrics. Test only their conjunction, no new threshold/product/month rule; do not add failed gap/body-dominance filters.',
        objective='Unchanged: total net primary, month/product distribution secondary, no80% gate. Preserve3 repeated controls and all parent failures; this is not independent validation.')
    w.write(RUN/'protocol.json',p);shutil.copy2(base.RUN/'warning-features.json',RUN/'warning-features.json');print('Frozen single economic+positive-body interaction after main results',flush=True)
if __name__=='__main__':prepare()
