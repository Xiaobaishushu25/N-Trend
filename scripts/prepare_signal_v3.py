"""Archive and prepare a database for the offline signal V3 upgrade.
Never opens the source with the application migration layer. Standard library only.
"""
from contextlib import closing
import argparse, datetime as dt, gzip, hashlib, json, os, sqlite3, tempfile, urllib.request, uuid
from pathlib import Path

DERIVED = ("v2_model_predictions", "v2_trigger_features", "v2_setup_features", "v2_trade_outcomes", "v2_trade_events", "v2_model_registry", "signal_annotations", "signal_decisions", "preclose_signals", "preclose_candidates", "scan_watermarks", "notification_history", "pattern_events")
def ro(path):
    return closing(sqlite3.connect(Path(path).resolve().as_uri()+"?mode=ro", uri=True))
def digest(path):
    h=hashlib.sha256()
    with open(path,"rb") as f:
        for b in iter(lambda:f.read(1024*1024),b""): h.update(b)
    return h.hexdigest()
def raw_hash(db):
    h=hashlib.sha256()
    for row in db.execute("SELECT * FROM klines WHERE timeframe='5m' AND source='raw' ORDER BY symbol,ts"):
        h.update(json.dumps(row,ensure_ascii=False,separators=(",",":")).encode()); h.update(b"\n")
    return h.hexdigest()
def summary(db):
    tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    result={name: db.execute('SELECT count(*) FROM "'+name+'"').fetchone()[0] for name in (*DERIVED,"klines") if name in tables}
    result["raw_5m"]=db.execute("SELECT count(*),min(ts),max(ts),count(DISTINCT symbol) FROM klines WHERE timeframe='5m' AND source='raw'").fetchone()
    return result
def request(base,token,path):
    req=urllib.request.Request(base.rstrip("/")+"/api/v1"+path,headers={"Authorization":"Bearer "+token})
    return urllib.request.urlopen(req,timeout=120)
def prepare(source, archive_dir, data_dir, origin="local-explicit-snapshot", expected_sha=None, compressed=False):
    archive_dir=Path(archive_dir).resolve(); data_dir=Path(data_dir).resolve(); source=Path(source).resolve()
    if archive_dir==data_dir or archive_dir in data_dir.parents or data_dir in archive_dir.parents: raise ValueError("archive and work directories must be disjoint")
    if (data_dir/"ntrend.db").exists() or (data_dir/"manifest.json").exists(): raise ValueError("destination exists; choose a new staging directory")
    archive_dir.mkdir(parents=True,exist_ok=False); data_dir.mkdir(parents=True,exist_ok=True)
    snapshot=archive_dir/"source.sqlite"
    if expected_sha and digest(source)!=expected_sha.lower(): raise ValueError("download checksum mismatch")
    if compressed:
        import shutil
        with gzip.open(source,"rb") as r, snapshot.open("xb") as w: shutil.copyfileobj(r,w)
    else:
        with ro(source) as src, closing(sqlite3.connect(snapshot)) as dst: src.backup(dst)
    with ro(snapshot) as db:
        if db.execute("PRAGMA integrity_check").fetchone()[0]!="ok": raise ValueError("source integrity failed")
        before=summary(db); raw_before=raw_hash(db)
        old_max=db.execute("SELECT coalesce(max(id),0) FROM pattern_events").fetchone()[0]
    archive_sha=digest(snapshot)
    manifest={"generation":str(uuid.uuid4()),"created_at":dt.datetime.now(dt.timezone.utc).isoformat(),"origin":origin,"source_sha256":archive_sha,"raw_sha256":raw_before,"before":before,"old_signal_max_id":old_max,"archive":str(snapshot),"status":"archived"}
    (archive_dir/"manifest.json").write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding="utf-8")
    work=data_dir/"ntrend.db"
    with ro(snapshot) as src, closing(sqlite3.connect(work)) as dst: src.backup(dst)
    with closing(sqlite3.connect(work)) as db:
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        db.execute("PRAGMA foreign_keys=OFF")
        with db:
            for table in DERIVED:
                if table in tables: db.execute('DELETE FROM "'+table+'"')
            db.execute("DELETE FROM klines WHERE source='derived'")
            db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES ('signal_generation',?)",(manifest["generation"],))
            db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES ('signal_origin','historical_rebuild')")
            db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES ('signal_v3_prepared','1')")
            # DELETE preserves AUTOINCREMENT; also cover older schemas without an existing sequence row.
            db.execute("UPDATE sqlite_sequence SET seq=max(seq,?) WHERE name='pattern_events'",(old_max,))
        if raw_hash(db)!=raw_before: raise ValueError("raw data changed during cleanup")
        manifest["after"]=summary(db)
        if db.execute("PRAGMA integrity_check").fetchone()[0]!="ok": raise ValueError("working integrity failed")
    if (source.parent/"execution-snapshot.json").exists(): manifest["execution_config_archive"]=str(source.parent/"execution-snapshot.json")
    manifest["status"]="prepared"
    (data_dir/"manifest.json").write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding="utf-8")
    # Restore verification is the working copy's read-only integrity/hash comparison above.
    os.chmod(snapshot,0o444)
    return manifest

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    choice=ap.add_mutually_exclusive_group(required=True); choice.add_argument("--source",type=Path); choice.add_argument("--auth-file",type=Path)
    ap.add_argument("--archive-dir",required=True,type=Path); ap.add_argument("--data-dir",required=True,type=Path)
    args=ap.parse_args()
    if args.auth_file:
        auth=json.loads(args.auth_file.read_text(encoding="utf-8-sig")); base=auth["server_url"]; token=auth["device_token"]
        with tempfile.TemporaryDirectory(prefix="ntrend-download-") as temp:
            downloaded=Path(temp)/"snapshot.gz"
            with request(base,token,"/admin/database-backup") as response:
                expected=response.headers.get("X-Database-SHA256") or response.headers.get("X-Backup-Sha256")
                if not expected: raise ValueError("server did not provide snapshot checksum")
                downloaded.write_bytes(response.read())
            result=prepare(downloaded,args.archive_dir,args.data_dir,origin="server-consistent-snapshot",expected_sha=expected,compressed=True)
            # Read-only exports preserve execution evidence alongside the database archive.
            for name,path in [("trade-history","/trader/history"),("execution-snapshot","/trader/snapshot")]:
                try:
                    with request(base,token,path) as response: value=json.load(response)
                    (args.archive_dir/(name+".json")).write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding="utf-8")
                except Exception as error:
                    raise RuntimeError("database archived, but execution export incomplete: "+name) from error
    else: result=prepare(args.source,args.archive_dir,args.data_dir)
    if args.auth_file:
        result["execution_config_archive"]=str((args.archive_dir/"execution-snapshot.json").resolve())
        (args.data_dir/"manifest.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({k:result[k] for k in ("generation","status","before","after","source_sha256","raw_sha256")},ensure_ascii=False,indent=2))
if __name__=="__main__": main()
