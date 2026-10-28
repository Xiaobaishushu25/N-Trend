import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'scripts'))
import run_market_backfill as runner


class MergeTests(unittest.TestCase):
    def test_conflicting_overlap_does_not_modify_main(self):
        old_stage,old_main,old_status,old_missing=runner.STAGE,runner.store.DB,runner.STATUS,runner.missing_windows
        try:
            with tempfile.TemporaryDirectory() as directory:
                root=pathlib.Path(directory);runner.STAGE=root/'stage.sqlite';runner.store.DB=root/'main.sqlite';runner.STATUS=root/'status.json'
                runner.missing_windows=lambda db:[]
                main=runner.store.connect(runner.store.DB);stage=runner.store.connect(runner.STAGE)
                stage.execute('CREATE TABLE backfill_batches(start_date TEXT,end_date TEXT,batch TEXT,status TEXT,details TEXT)')
                row=('DCE.eg2401',60,0,60000000000,0,10.,11.,9.,10.,1,2,3,1,1,'test','v1')
                main.execute('INSERT INTO bars VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',row);main.commit()
                altered=list(row);altered[5]=10.5
                stage.execute('INSERT INTO bars VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',altered);stage.commit();main.close();stage.close()
                with self.assertRaisesRegex(RuntimeError,'conflict'):runner.merge()
                check=runner.store.connect(runner.store.DB)
                self.assertEqual(check.execute('SELECT open FROM bars').fetchone()[0],10.);check.close()
        finally:
            runner.STAGE,runner.store.DB,runner.STATUS,runner.missing_windows=old_stage,old_main,old_status,old_missing

if __name__=='__main__':unittest.main()
