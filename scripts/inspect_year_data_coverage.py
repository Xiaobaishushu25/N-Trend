import json,pathlib,sqlite3
p=pathlib.Path('data/ntrend.db').resolve()
c=sqlite3.connect(p.as_uri()+'?mode=ro',uri=True)
tables=c.execute("select name from sqlite_master where type='table'").fetchall()
print('Tables:',tables)
for (name,) in tables:
    if 'kline' not in name and 'bar' not in name:continue
    print('Schema:',c.execute('pragma table_info("'+name+'")').fetchall())
paths=[p,pathlib.Path('C:/Users/Xbss/AppData/Roaming/com.ntrend.app/ntrend.db'),pathlib.Path('C:/Users/Xbss/AppData/Roaming/com.ntrend.app.research/ntrend.db')]
paths.extend(pathlib.Path('G:/待处理文件/交易系统/ntrend-worktree-backups').rglob('*.db'))
for path in paths:
    if not path.exists():continue
    db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)
    try:
        print(str(path),db.execute('select timeframe,min(ts),max(ts),count(*),count(distinct symbol) from klines group by timeframe').fetchall())
        print('Actual-month symbols:',db.execute("select distinct symbol from klines where symbol not like '%0' limit 40").fetchall())
    except sqlite3.Error as exc:print(type(exc).__name__)
