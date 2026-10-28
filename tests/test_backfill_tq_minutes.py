import datetime as dt
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'scripts'))
import backfill_tq_minutes as b


class BackfillTests(unittest.TestCase):
    def test_friday_cutoff_uses_tuesday_anchor(self):
        self.assertEqual(b.anchor_for_end(dt.date(2023,11,10)),dt.date(2023,11,14))

    def test_rollover_selects_both_members(self):
        table={'DCE.eg':[['20231001','DCE.eg2401'],['20231102','DCE.eg2405']]}
        result=b.selections(table,dt.date(2023,10,28),dt.date(2023,11,3))
        self.assertEqual(result['DCE.eg'],['DCE.eg2401','DCE.eg2405'])

    def test_conflict_does_not_commit_batch(self):
        with tempfile.TemporaryDirectory() as d:
            db=b.prepare(pathlib.Path(d)/'b.sqlite')
            row=('DCE.eg2401',60,0,b.STEP,0,10.,11.,9.,10.,1,2,3,1,1,'test','v1')
            db.execute('INSERT INTO bars VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',row);db.commit()
            altered=list(row);altered[5]=10.5
            with self.assertRaises(ValueError):
                b.persist(db,dt.date(2023,1,1),dt.date(2023,1,2),'DCE.eg',{'DCE.eg':['DCE.eg2401']},{'DCE.eg2401':[tuple(altered)]},1,None)
            self.assertEqual(db.execute('SELECT count(*) FROM backfill_batches').fetchone()[0],0)
            db.close()

if __name__=='__main__':unittest.main()
