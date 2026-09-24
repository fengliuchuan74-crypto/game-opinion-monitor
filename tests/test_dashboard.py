import unittest
from io import BytesIO
import pandas as pd

from modules.dashboard import (category_counts, category_timeline, keyword_counts, negative_rows,
    ratings, region_comparison, sentiments, timeline, versions,day_reviews)
from modules.review_cards import card_html,featured_reviews
from modules.operations import analyze_reviews, snapshot
from modules.deliverables import build_report, workbook


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.start=pd.Timestamp('2026-09-20',tz='Asia/Shanghai')
        self.end=pd.Timestamp('2026-09-23',tz='Asia/Shanghai')
        self.data=pd.DataFrame([
            dict(review_id=1,date='2026-09-19T16:01Z',rating=1,sentiment_label='差评',
                 issue_category='Bug/闪退问题',issue_categories='Bug/闪退问题；服务器/网络问题；Bug/闪退问题',
                 title='crash',content='crash crash TestTitle',topic='version:2.0',app_id='111',country='cn',needs_review=False),
            dict(review_id=2,date='2026-09-20T15:59Z',rating=5,sentiment_label='好评',
                 issue_category='正向口碑/泛好评',issue_categories='',title='story',content='great story',
                 topic='版本：3.0',app_id='111',country='cn',needs_review=False),
            dict(review_id=3,date='2026-09-21T16:01Z',rating=None,sentiment_label='待复核',
                 issue_category='未归类/待复核',issue_categories='',title='unknown',content='unknown',
                 topic='',app_id='111',country='us',needs_review=True)])
        self.data['date']=pd.to_datetime(self.data['date'],utc=True)

    def test_distribution_denominators_and_pending_are_explicit(self):
        distribution=sentiments(self.data).set_index('情绪')
        self.assertEqual(distribution.loc['待复核','评论数'],1)
        self.assertEqual(distribution.loc['中评','评论数'],0)
        self.assertAlmostEqual(distribution['占比'].sum(),100)
        stars=ratings(self.data)
        self.assertEqual(stars['评论数'].sum(),2)
        self.assertEqual(stars.iloc[0]['占比'],50)

    def test_china_dates_missing_day_and_missing_rates(self):
        series=timeline(self.data,self.start,self.end).set_index('时间')
        self.assertEqual(series['评论数'].tolist(),[2,0,1])
        self.assertEqual(series.loc['2026-09-20','平均星级'],3)
        self.assertEqual(series.loc['2026-09-20','低星占比'],50)
        self.assertTrue(pd.isna(series.loc['2026-09-21','平均星级']))
        self.assertTrue(pd.isna(series.loc['2026-09-22','低星占比']))
        self.assertEqual(timeline(self.data,self.start,self.end,'按周')['评论数'].tolist(),[2,1])
        self.assertEqual(timeline(self.data,self.start,self.end,'按月')['评论数'].tolist(),[3])

    def test_end_exclusive_and_outside_dates_excluded(self):
        extra=self.data.iloc[[0]].assign(date=self.end)
        series=timeline(pd.concat([self.data,extra],ignore_index=True),self.start,self.end)
        self.assertEqual(series['评论数'].sum(),3)

    def test_multilabel_deduplicated_and_unclassified_retained(self):
        table=category_counts(self.data,self.data.iloc[:0]).set_index('类别')
        self.assertEqual(table.loc['Bug/闪退问题','本期'],1)
        self.assertEqual(table.loc['服务器/网络问题','本期'],1)
        self.assertEqual(table.loc['未归类/待复核','本期'],1)
        prior_only=category_counts(self.data.iloc[:0],self.data)
        self.assertEqual(prior_only['本期'].sum(),0)
        self.assertTrue(prior_only['本期占比'].isna().all())
        trend=category_timeline(self.data,self.start,self.end,['Bug/闪退问题','服务器/网络问题'])
        self.assertEqual(trend['评论数'].sum(),2)

    def test_regions_do_not_mix_apps_or_turn_no_sample_into_zero_percent(self):
        foreign=self.data.iloc[[0]].assign(app_id='222')
        table=region_comparison(pd.concat([self.data,foreign]),'111',['cn','us','jp'],self.start,self.end).set_index('地区')
        self.assertEqual(table.loc['CN','评论数'],2)
        self.assertEqual(table.loc['CN','低星占比'],50)
        self.assertEqual(table.loc['US','评论数'],1)
        self.assertTrue(pd.isna(table.loc['US','低星占比']))
        self.assertEqual(table.loc['JP','评论数'],0)
        self.assertTrue(pd.isna(table.loc['JP','低星占比']))

    def test_keywords_count_reviews_not_repetitions_or_metadata(self):
        words=keyword_counts(self.data,'TestTitle').set_index('关键词')
        self.assertEqual(words.loc['crash','提及评论数'],1)
        self.assertNotIn('testtitle',words.index)
        self.assertNotIn('version',words.index)
        self.assertNotIn('2.0',words.index)
        self.assertEqual(words.loc['story','提及评论数'],1)

    def test_version_and_negative_subset(self):
        table=versions(self.data).set_index('版本')
        self.assertEqual(set(table.index),{'2.0','3.0','版本未知'})
        self.assertEqual(table['来源提供'].sum(),2)
        self.assertEqual(table['按日期推定'].sum(),0)
        self.assertEqual(table.loc['版本未知','来源提供'],0)
        self.assertEqual(len(negative_rows(self.data)),1)
        # A high-star negative text is still negative-related; zero stars are not low stars.
        self.assertEqual(len(negative_rows(self.data.assign(rating=[5,0,None]))),1)

    def test_attributed_version_groups_keep_source_and_inference_counts_distinct(self):
        attributed=self.data.assign(source_version=['2.0','3.0',''],inferred_version=['','','3.0'],
            version_basis=['source','source','date_inferred'],version_display=['2.0','3.0','3.0'])
        unknown=self.data.iloc[[2]].assign(review_id=4,source_version='',inferred_version='',
            version_basis='unknown',version_display='')
        data=pd.concat([attributed,unknown],ignore_index=True)
        table=versions(data).set_index('版本')
        self.assertEqual(table.loc['3.0','评论数'],2)
        self.assertEqual(table.loc['3.0','来源提供'],1)
        self.assertEqual(table.loc['3.0','按日期推定'],1)
        self.assertEqual(table.loc['3.0','有效星级'],1)
        self.assertEqual(table.loc['3.0','平均星级'],5)
        self.assertEqual(table.loc['版本未知','评论数'],1)
        self.assertEqual(table.loc['版本未知',['来源提供','按日期推定']].sum(),0)
        self.assertEqual(table['评论数'].sum(),4)
        self.assertIn('来源提供',card_html(attributed.iloc[0].to_dict()))
        self.assertIn('按日期推定',card_html(attributed.iloc[2].to_dict()))
        self.assertNotIn('来源提供',card_html(attributed.iloc[2].to_dict()))
        profile=dict(app_id='111',country='cn',app_name='版本统计验收')
        view=snapshot(data,now=self.end,start=self.start,end=self.end)
        book=pd.ExcelFile(BytesIO(workbook(profile,view,[],[],[],include_workflow=False)))
        exported=pd.read_excel(book,sheet_name='版本分析').set_index('版本')
        self.assertEqual(exported.loc['3.0','按日期推定'],1)
        evidence=pd.read_excel(book,sheet_name='评论证据')
        self.assertIn('source_version',evidence.columns)
        self.assertIn('inferred_version',evidence.columns)
        self.assertEqual(evidence.loc[evidence['review_id'].eq(3),'version_basis'].iloc[0],'date_inferred')

    def test_empty_data_keeps_missing_metrics_and_all_charts_safe(self):
        empty=analyze_reviews(pd.DataFrame())
        self.assertTrue(sentiments(empty)['占比'].isna().all())
        self.assertEqual(timeline(empty,self.start,self.end)['评论数'].sum(),0)
        self.assertTrue(versions(empty).empty)
        self.assertTrue(keyword_counts(empty).empty)
        self.assertEqual(region_comparison(empty,'111',['cn'],self.start,self.end).iloc[0]['评论数'],0)

    def test_dashboard_exports_have_visuals_and_no_workflow_sections(self):
        view=snapshot(self.data,now=self.end,start=self.start,end=self.end)
        profile=dict(app_id='111',country='cn',app_name='<script>alert(1)</script>')
        md,html,_=build_report(profile,view,[],[],include_workflow=False)
        self.assertNotIn('<script>',html)
        self.assertIn('情绪分布',html)
        self.assertNotIn('## 处理台账',md)
        book=pd.ExcelFile(BytesIO(workbook(profile,view,[],[],[],include_workflow=False)))
        self.assertIn('每日趋势',book.sheet_names)
        self.assertIn('每日问题类别',book.sheet_names)
        self.assertIn('关键词',book.sheet_names)
        self.assertNotIn('处理台账',book.sheet_names)
        self.assertEqual(pd.read_excel(book,sheet_name='情绪分布')['评论数'].sum(),3)

    def test_day_detail_respects_midnight_and_partial_window(self):
        day=day_reviews(self.data,'2026-09-20',self.start,self.end)
        self.assertEqual(day['review_id'].tolist(),[1,2])
        self.assertTrue(day_reviews(self.data,'2026-09-21',self.start,self.end).empty)
        clipped=day_reviews(self.data,'2026-09-20',self.start+pd.Timedelta(hours=8),self.start+pd.Timedelta(hours=16))
        self.assertTrue(clipped.empty)
        self.assertTrue(day_reviews(self.data,'2026-09-22',self.start,self.start+pd.Timedelta(days=2)).empty)

    def test_full_category_timeline_does_not_discard_rare_topics(self):
        rows=self.data.iloc[[0]*18].reset_index(drop=True)
        rows['issue_category']=[f'主题{i}' for i in range(18)]
        rows['issue_categories']=rows['issue_category']
        categories=category_counts(rows,rows.iloc[:0])['类别'].tolist()
        trend=category_timeline(rows,self.start,self.end,categories)
        self.assertEqual(trend['类别'].nunique(),18)
        self.assertEqual(trend['评论数'].sum(),18)

    def test_cards_escape_source_content_and_reject_unsafe_urls(self):
        row=self.data.iloc[0].to_dict()
        row.update(author='<script>bad()</script>',content='<img src=x onerror=alert(1)>'*20,
            title='<b>title</b>',url='javascript:alert(1)')
        html=card_html(row)
        self.assertNotIn('<script>',html)
        self.assertNotIn('<img',html)
        self.assertNotIn('javascript:',html)
        self.assertIn('&lt;b&gt;title&lt;/b&gt;',html)
        self.assertIn('展开完整原文',html)
        self.assertNotIn('赞 ',html)
        row['url']='https://apps.apple.com/cn/app/id111'
        self.assertIn('查看商店来源',card_html(row))

    def test_typical_cards_keep_sentiment_groups_and_unique_reviews(self):
        cards=featured_reviews(self.data)
        self.assertEqual(cards['典型好评 Top 5']['review_id'].tolist(),[2])
        self.assertEqual(cards['典型差评 Top 5']['review_id'].tolist(),[1])
        unsure=self.data.iloc[[1]].assign(review_id=99,needs_review=True,title='手机卡死')
        safe_cards=featured_reviews(pd.concat([self.data,unsure],ignore_index=True))
        self.assertNotIn(99,safe_cards['典型好评 Top 5']['review_id'].tolist())
        for rows in cards.values(): self.assertEqual(rows['review_id'].nunique(),len(rows))
        empty=featured_reviews(analyze_reviews(pd.DataFrame()))
        self.assertTrue(all(rows.empty for rows in empty.values()))


if __name__=='__main__': unittest.main()
