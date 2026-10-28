import pathlib,shutil,subprocess,os,time,sys
root=pathlib.Path('research-runs/b5-p2-three-year-20261028')
syms=['BU0','C0','CF0','CJ0','EB0','EG0','FG0','JD0','L0','MA0','PB0','PP0','RB0','SA0','SF0','SH0','SI0','SM0','SR0','SS0','UR0','V0']
source=pathlib.Path('scripts/backtest_b5_p2_three_year_local.py').read_text(encoding='utf-8-sig')
source=source[:source.index("spec=importlib.util.spec_from_file_location('controller'")]
source=source.replace("out=pathlib.Path('research-runs/b5-p2-three-year-20261028');out.mkdir(exist_ok=True)","out=pathlib.Path(os.environ['THREE_YEAR_PART']);out.mkdir(exist_ok=True)")
source=source.replace('import sys,sqlite3','import os,sys,sqlite3')
source=source.replace('for sym in y.NAMES:',"for sym in json.loads(os.environ['THREE_YEAR_SYMBOLS']):")
source+="\nimport pickle\nwith (out/'partial.pkl').open('wb') as f:pickle.dump((dict(allrows),events,ends,audit),f,protocol=pickle.HIGHEST_PROTOCOL)\nprint('PART COMPLETED',flush=True)\n"
worker=pathlib.Path('scripts/backtest_b5_p2_three_year_worker.py');worker.write_text(source,encoding='utf-8')
procs=[]
for i in range(3):
 part=root/('part'+str(i));part.mkdir(exist_ok=True)
 assigned=syms[i::3]
 for s in assigned:
  if (root/s).exists() and not (part/s).exists():shutil.copytree(root/s,part/s)
 env=os.environ.copy();env['THREE_YEAR_PART']=str(part);env['THREE_YEAR_SYMBOLS']=__import__('json').dumps(assigned)
 log=(part/'run.log').open('w',encoding='utf8');procs.append((subprocess.Popen([sys.executable,str(worker)],env=env,stdout=log,stderr=subprocess.STDOUT),log,assigned))
 print('Started',i,assigned,flush=True)
while any(p.poll() is None for p,_,_ in procs):time.sleep(5)
for p,log,assigned in procs:
 log.close();print('Finished',p.returncode,assigned,flush=True)
if any(p.returncode for p,_,_ in procs):sys.exit(1)
import pickle,collections,json,importlib.util
rows=collections.defaultdict(list);events={};ends={};audit={}
for i in range(3):
 with (root/('part'+str(i))/'partial.pkl').open('rb') as f:r,e,en,a=pickle.load(f)
 for k,v in r.items():rows[k].extend(v)
 events.update(e);ends.update(en);audit.update(a)
(root/'audit.json').write_text(json.dumps(audit,ensure_ascii=False,indent=2),encoding='utf8')
spec=importlib.util.spec_from_file_location('controller','research-strategies/best-a-portfolio-observation-v1/controller.py');ctl=importlib.util.module_from_spec(spec);spec.loader.exec_module(ctl)
sector=json.loads(pathlib.Path('research-strategies/best-a-portfolio-observation-v1/protocol.json').read_text(encoding='utf8'))['sector_map']
with (root/'accepted.jsonl').open('w',encoding='utf8') as f:
 for case,rs in sorted(rows.items()):
  accepted,ledger,risk=ctl.select(rs,events,ends,sector,'sector2_counter_stops' if case.startswith('B5/') else 'base','original')
  for row in accepted:f.write(json.dumps(dict(case=case,row=row),ensure_ascii=False)+'\n')
  print('RESULT',case,len(accepted),collections.Counter(r['result']['evaluation']['status'] for r in accepted),flush=True)
(root/'complete.json').write_text(json.dumps(dict(complete=True,period=['2023-10-30','2026-10-27'],warmup='unavailable',holiday='inferred from observed trading dates'),ensure_ascii=False),encoding='utf8')
