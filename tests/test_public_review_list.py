import json
import tempfile
import unittest
from datetime import datetime,timedelta,timezone
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

from collectors.app_store import AppStoreCollector
from collectors.app_store_public import ROWS_URL,public_context,public_page
from collectors.base import RawReview
from modules.review_store import read_reviews,save_reviews
from modules.storage import database


def metadata(app='111',country='cn'):
    return {'adamId':int(app),'writeUserReviewUrl':f'https://userpub.itunes.apple.com/writeUserReview?cc={country}',
            'kindId':11,'userReviewsRowUrl':ROWS_URL,'userReviewsSortOptions':[{'sortId':4,'name':'最新发表'}],
            'totalNumberOfReviews':10000}


def rows(start,count,country='cn'):
    newest=datetime(2026,9,20,10,tzinfo=timezone.utc)
    return {'userReviewList':[{'userReviewId':str(1000+i),'body':'更新后闪退','date':(newest-timedelta(minutes=i)).isoformat(),
        'name':'玩家','rating':1,'title':'反馈','viewUsersUserReviewsUrl':f'https://itunes.apple.com/{country}/reviews?userProfileId=1'}
        for i in range(start,start+count)]}


def response(data,status=200):
    r=Mock(status_code=status,text=json.dumps(data)); r.json.return_value=data
    return r


class PublicReviewTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.folder=Path(temp.name); self.session=Mock()
        self.collector=AppStoreCollector(self.folder,session=self.session)

    def collect(self,responses,**kwargs):
        self.session.get.side_effect=[response(p) for p in responses]
        return self.collector.collect('111','cn',max_pages=kwargs.pop('max_pages',10),delay_seconds=0,
            follow_feed_links=True,prefer_public_reviews=True,**kwargs)

    def test_public_list_works_when_rss_is_unavailable_and_pages_have_no_offset_gap(self):
        result=self.collect([metadata(),rows(0,50),rows(50,50)],max_pages=2)
        self.assertEqual(result.fetched_count,100)
        self.assertEqual([r.external_id for r in result.reviews],[str(i) for i in range(1000,1100)])
        self.assertEqual([call.kwargs['params']['startIndex'] for call in self.session.get.call_args_list[1:]],[0,50])
        self.assertEqual([call.kwargs['params']['endIndex'] for call in self.session.get.call_args_list[1:]],[50,100])
        self.assertTrue(all(call.kwargs['params']['sort']=='4' for call in self.session.get.call_args_list[1:]))
        self.assertEqual(result.metadata['source_kind'],'public_review_list')
        self.assertTrue(result.metadata['sequence_verified'])
        self.assertTrue(all(r.data_source=='app_store_public_review_list' for r in result.reviews))
        self.assertTrue(all(r.topic=='' for r in result.reviews))
        self.assertEqual(result.stop_reason,'页数上限')

    def test_fixed_range_stops_only_after_reading_past_start(self):
        result=self.collect([metadata(),rows(0,50),rows(50,50)],start_at='2026-09-20T09:15:00Z',end_at='2026-09-20T09:50:00Z')
        self.assertEqual(result.requested,1)
        self.assertEqual(result.fetched_count,35)
        self.assertTrue(result.metadata['boundary_reached'])
        self.assertEqual(result.metadata['coverage_start'],'2026-09-20T09:15:00+00:00')

    def test_context_refuses_other_game_country_or_endpoint(self):
        cases=[metadata('222'),metadata(country='us'),{**metadata(),'userReviewsRowUrl':'https://example.org/reviews'},
               {**metadata(),'userReviewsSortOptions':[{'sortId':1}]}]
        for data in cases:
            with self.subTest(data=data):
                self.session.get.return_value=response(data)
                context,error=public_context(self.collector,'111','cn',{'request_attempts':0},[])
                self.assertIsNone(context); self.assertTrue(error)

    def test_country_not_in_compatibility_map_does_not_guess_storefront(self):
        context,error=public_context(self.collector,'111','zz',{'request_attempts':0},[])
        self.assertIsNone(context); self.assertTrue(error); self.session.get.assert_not_called()

    def test_mismatched_region_row_or_invalid_id_rejects_entire_batch(self):
        wrong_id=rows(0,2); wrong_id['userReviewList'][1]['userReviewId']=''
        for data in [rows(0,2,country='us'),wrong_id]:
            self.session.get.return_value=response(data)
            _,reviews,_,error=public_page(self.collector,'111','cn',1,{'headers':{}},{'request_attempts':0},[])
            self.assertFalse(reviews); self.assertTrue(error)

    def test_first_list_failure_can_recover_using_rss(self):
        rss={'feed':{'entry':[{'id':{'label':'1'},'content':{'label':'闪退'},'updated':{'label':'2026-09-20T10:00:00Z'},'im:rating':{'label':'1'}}]}}
        result=self.collect([metadata(),{'userReviewList':[]},rss])
        self.assertEqual(result.fetched_count,1)
        self.assertEqual(result.metadata['source_kind'],'public_rss')
        self.assertFalse(result.errors)

    def test_middle_empty_page_keeps_data_without_switching_or_skipping(self):
        result=self.collect([metadata(),rows(0,50),{'userReviewList':[]}])
        self.assertEqual(result.fetched_count,50); self.assertEqual(result.requested,2)
        self.assertEqual(self.session.get.call_count,3)
        self.assertTrue(result.errors)
        self.assertEqual(result.metadata['source_kind'],'public_review_list')

    def test_rate_limit_stops_without_fallback_requests(self):
        self.session.get.return_value=response({},429)
        result=self.collector.collect('111',follow_feed_links=True,prefer_public_reviews=True)
        self.assertTrue(result.errors); self.session.get.assert_called_once()

    def test_repeated_public_pages_do_not_claim_continuity(self):
        result=self.collect([metadata(),rows(0,50),rows(0,50)])
        self.assertEqual(result.fetched_count,50)
        self.assertFalse(result.metadata['sequence_verified'])
        self.assertIsNone(result.metadata['coverage_start'])

    def test_same_revision_retains_version_and_hash_across_sources(self):
        db=self.folder/'reviews.sqlite3'
        old=RawReview(platform='App Store',external_id='1000',date='2026-09-20T03:00:00-07:00',author='玩家',title='反馈',
            content='更新后闪退',rating=1,app_id='111',country='cn',topic='version:6.0.0',data_source='app_store_public_feed')
        save_reviews(db,[old]); before=read_reviews(db).iloc[0]
        result=self.collect([metadata(),rows(0,1)])
        saved=save_reviews(db,result.reviews); after=read_reviews(db).iloc[0]
        self.assertEqual(saved.duplicates,1); self.assertEqual(saved.updated,0)
        self.assertEqual(after.topic,'version:6.0.0'); self.assertEqual(after.content_hash,before.content_hash)
        self.assertEqual(after.date,before.date)
        with database(db) as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM review_revisions').fetchone()[0],0)

    def test_new_revision_without_version_is_unknown_not_old_version(self):
        db=self.folder/'reviews.sqlite3'
        old=RawReview(platform='App Store',external_id='1000',date='2026-09-19T10:00:00Z',author='玩家',title='反馈',
            content='旧评论',rating=1,app_id='111',country='cn',topic='version:5.0')
        save_reviews(db,[old])
        result=self.collect([metadata(),rows(0,1)])
        self.assertEqual(save_reviews(db,result.reviews).updated,1)
        self.assertEqual(read_reviews(db).iloc[0].topic,'')

    def test_transport_control_noise_retains_metadata_without_rewriting_body(self):
        old=RawReview(platform='App Store',external_id='1000',date='2026-09-20T03:00:00-07:00',
            author='玩家',title='反馈',content='更新后闪退',rating=1,app_id='111',country='cn',
            topic='version:6.0.0',likes=7,comments=2,shares=1,data_source='app_store_public_feed')
        for index,(rss_body,list_body) in enumerate([
                ('更新后闪退','更新后\x14闪退'),
                ('更新后\x14闪退','更新后闪退'),
                ('更新后闪退','\x00更新后闪退\x7f\x9f')]):
            with self.subTest(rss_body=rss_body,list_body=list_body):
                db=self.folder/f'controls-{index}.sqlite3'
                save_reviews(db,[replace(old,content=rss_body)])
                before=read_reviews(db).iloc[0]
                incoming=replace(old,date='2026-09-20T10:00:00Z',content=list_body,
                    topic='',likes=0,comments=0,shares=0,data_source='app_store_public_review_list')
                self.assertEqual(save_reviews(db,[incoming]).updated,1)
                after=read_reviews(db).iloc[0]
                self.assertEqual(after.topic,'version:6.0.0')
                self.assertEqual((after.likes,after.comments,after.shares),(7,2,1))
                self.assertEqual(after.content,list_body)
                self.assertNotEqual(after.content_hash,before.content_hash)
                with database(db) as connection:
                    previous=json.loads(connection.execute(
                        'SELECT previous_json FROM review_revisions').fetchone()[0])
                self.assertEqual(previous['content'],rss_body)

    def test_changed_revision_does_not_inherit_metadata_even_with_control_noise(self):
        old=RawReview(platform='App Store',external_id='1000',date='2026-09-20T10:00:00Z',
            author='玩家',title='反馈',content='更新后闪退',rating=1,app_id='111',country='cn',
            topic='version:6.0.0',likes=7,data_source='app_store_public_feed')
        changes=[{'content':'更新后\x14不闪退'}, {'content':'更新后\n闪退'},
                 {'content':'更新后\t闪退'}, {'content':'更新后\u200d闪退'},
                 {'date':'2026-09-20T10:00:01Z'}, {'title':'新反馈'}, {'rating':2}]
        for index,change in enumerate(changes):
            with self.subTest(change=change):
                db=self.folder/f'changed-{index}.sqlite3'
                save_reviews(db,[old])
                incoming=replace(old,topic='',likes=0,data_source='app_store_public_review_list')
                self.assertEqual(save_reviews(db,[replace(incoming,**change)]).updated,1)
                after=read_reviews(db).iloc[0]
                self.assertEqual(after.topic,'')
                self.assertEqual(after.likes,0)
