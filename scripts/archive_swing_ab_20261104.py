"""Complete immutable research archive of the two approved observation candidates."""
import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'research-archives/swing-ab-complete-20261104-v1'


def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(8*1024*1024),b''):h.update(chunk)
    return h.hexdigest()


def main():
    if (OUT/'COMPLETE.json').exists():
        print('Existing completed archive preserved:',OUT);return
    OUT.mkdir(parents=True,exist_ok=True)
    report=ROOT/'reports/swing-expansion-20261104'
    shutil.copytree(report,OUT/'reports/swing-expansion-20261104',dirs_exist_ok=True)
    for folder in ['scripts','tests','data','strategies']:(OUT/folder).mkdir(exist_ok=True)
    for name in ['research_swing_expansion_20261104.py','research_swing_year_20261104.py','audit_swing_expansion_20261104.py','report_swing_expansion_20261104.py','exchange_fees_20261022.json','archive_swing_ab_20261104.py']:
        shutil.copy2(ROOT/'scripts'/name,OUT/'scripts'/name)
    for name in ['test_swing_year_20261104.py','test_swing_expansion_20261104.py']:
        shutil.copy2(ROOT/'tests'/name,OUT/'tests'/name)
    robust=json.loads((report/'robustness.json').read_text(encoding='utf8'))
    for name,row in robust.items():
        (OUT/'strategies'/f'{name}.json').write_text(json.dumps(dict(model=row['model'],status='saved_observation_candidate_not_live',results=row,capital=50000,original_margin_assumption=.15,margin20_results_included=True,drawdown_target=7500),ensure_ascii=False,indent=2),encoding='utf8')
    db=OUT/'data/research_market_history.sqlite'
    if not db.exists():
        src=sqlite3.connect(f'file:{(ROOT/"data/research_market_history.sqlite").as_posix()}?mode=ro',uri=True)
        dst=sqlite3.connect(db);src.backup(dst,pages=8192);dst.close();src.close()
    check=sqlite3.connect(f'file:{db.as_posix()}?mode=ro',uri=True)
    integrity=check.execute('PRAGMA quick_check').fetchone()[0];check.close();assert integrity=='ok'
    (OUT/'README.md').write_text('''# A/B波段候选完整归档

日期：2026-11-04。用户要求完整保存两个候选后继续研究。

- A：pullback_end_60_32_target3_r250，多周期回调结束。
- B：oi_drive_15_32_target3_r500，增仓突破。
- 包含完整规则报告、全部80组合、两个候选的逐笔/权益/压力及分钟复核、源代码、测试、冻结手续费，以及SQLite backup API生成的完整研究行情数据库一致性副本。
- 所有文件由manifest.json记录SHA256；COMPLETE.json标记归档完成。后续研究应使用新目录，不能覆盖此归档。
- 从此目录作为工作目录执行 scripts 下研究和审计脚本可使用此行情副本复现；需安装原环境中的numpy/pandas/matplotlib。复现输出会写入副本内reports目录，请先另复制工作目录以保留归档。
- B的3跳完整账户压力失败；保存不等于批准实盘。原结果保证金假设15%，并包含20%压力。
''',encoding='utf8')
    manifest={p.relative_to(OUT).as_posix():dict(bytes=p.stat().st_size,sha256=sha(p)) for p in sorted(OUT.rglob('*')) if p.is_file() and p.name not in ['manifest.json','COMPLETE.json']}
    (OUT/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf8')
    (OUT/'COMPLETE.json').write_text(json.dumps(dict(complete=True,files=len(manifest),bytes=sum(r['bytes'] for r in manifest.values()),database_quick_check=integrity,manifest_sha256=sha(OUT/'manifest.json')),indent=2),encoding='utf8')
    print('Archived',len(manifest),'files;',sum(r['bytes'] for r in manifest.values()),'bytes;',OUT,flush=True)


if __name__=='__main__':main()
