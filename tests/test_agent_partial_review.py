"""Independent edge checks for partial Agent result validation and repair."""
import json
import tempfile
import unittest
from pathlib import Path

from collectors.base import RawReview
from modules.agent_analysis import (BatchValidationError, _process_next, _validate_batch,
    analysis_status, request_analysis)
from modules.review_store import init_db, save_reviews
from modules.storage import database
from test_agent_analysis import FixtureRunner, START, END


class AgentPartialReviewTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        self.db=self.root/'reviews.sqlite3'
        init_db(self.db)

    def review(self,identity,content='更新后闪退，无法进入游戏'):
        return RawReview(platform='App Store',app_id='111',country='cn',external_id=str(identity),
            date='2026-09-22T01:00:00+00:00',author='独立验收',title='启动问题',content=content,rating=1)

    def request(self):
        return request_analysis(self.db,'111','cn',start_at=START,end_at=END,max_reviews=None,model='test-model')

    def test_source_revision_changes_during_correction_do_not_publish_old_interpretation(self):
        save_reviews(self.db,[self.review(i) for i in range(15)])
        base=FixtureRunner()
        invalid_id=None
        original_hash=None
        correction_seen=False
        def runner(prompt,schema,directory,**kwargs):
            nonlocal invalid_id,original_hash,correction_seen
            response=base(prompt,schema,directory,**kwargs)
            if 'reviews' in schema['properties']:
                records=response['output']['reviews']
                if directory.name!='correction' and invalid_id is None:
                    invalid_id=records[0]['review_id']
                    original_hash=records[0]['content_hash']
                    records[0]['evidence_quote']='改写的总结，原文不存在'
                elif directory.name=='correction':
                    correction_seen=True
                    self.assertEqual(len(records),1)
                    with database(self.db) as connection:
                        self.assertEqual(connection.execute('SELECT COUNT(*) FROM agent_review_results').fetchone()[0],14)
                        identity=connection.execute('SELECT external_id FROM review_records WHERE id=?',(invalid_id,)).fetchone()[0]
                    save_reviews(self.db,[self.review(identity,'更新后恢复正常，现在可以进入游戏')])
            return response
        self.request()
        self.assertTrue(_process_next(self.db,self.root/'runs',runner=runner))
        status=analysis_status(self.db,'111','cn',START,END)
        self.assertEqual((status['analyzed'],status['pending']),(14,1))
        with database(self.db) as connection:
            self.assertNotEqual(connection.execute('SELECT content_hash FROM review_records WHERE id=?',(invalid_id,)).fetchone()[0],original_hash)
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM agent_review_results WHERE review_id=?',(invalid_id,)).fetchone()[0],0)
        retry=FixtureRunner()
        self.request()
        self.assertTrue(_process_next(self.db,self.root/'runs',runner=retry))
        self.assertEqual([size for kind,size in retry.calls if kind=='reviews'],[1])
        self.assertEqual(analysis_status(self.db,'111','cn',START,END)['pending'],0)

    def test_duplicate_id_rejects_both_variants_but_retains_unambiguous_neighbor(self):
        rows=[dict(review_id=i,content_hash='hash-'+str(i),title='',content='原文'+str(i)) for i in [1,2]]
        def record(row):
            return dict(review_id=row['review_id'],content_hash=row['content_hash'],sentiment_label='差评',
                issue_category='Bug/闪退问题',issue_categories=['Bug/闪退问题'],needs_review=False,
                reason='玩家描述启动失败',demand='恢复游戏',target='游戏',evidence_quote=row['content'])
        first,second=map(record,rows)
        duplicate=dict(first,sentiment_label='好评',reason='相互矛盾的重复输出')
        with self.assertRaises(BatchValidationError) as caught:
            _validate_batch({'reviews':[first,second,duplicate]},rows)
        self.assertEqual(set(caught.exception.valid_records),{2})
        self.assertEqual([row['review_id'] for row in caught.exception.retry_rows],[1])


if __name__=='__main__': unittest.main()
