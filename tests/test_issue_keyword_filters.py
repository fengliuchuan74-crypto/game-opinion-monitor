"""Version-scoped issue and keyword evidence, without previous-period leakage."""
from __future__ import annotations

import shutil
import unittest
import uuid
from pathlib import Path

import pandas as pd
from streamlit.testing.v1 import AppTest


class IssueKeywordFilterTests(unittest.TestCase):
    def setUp(self):
        self.folder=Path(__file__).resolve().parents[1]/'.test-tmp'/('issue-filter-'+uuid.uuid4().hex)
        self.folder.mkdir(parents=True)
        self.addCleanup(shutil.rmtree,self.folder)
        self.current=[
            dict(review_id=1,date='2026-09-20T01:00:00Z',rating=1,sentiment_label='差评',
                 issue_category='Bug/闪退问题',issue_categories='Bug/闪退问题',
                 title='crash',content='crash',topic='version:2.0',
                 version_display='2.0',version_basis='source'),
            dict(review_id=2,date='2026-09-20T02:00:00Z',rating=1,sentiment_label='差评',
                 issue_category='Bug/闪退问题',issue_categories='Bug/闪退问题',
                 title='crash',content='crash crash',topic='',
                 version_display='2.0',version_basis='date_inferred'),
            dict(review_id=3,date='2026-09-20T03:00:00Z',rating=5,sentiment_label='好评',
                 issue_category='内容/玩法反馈',issue_categories='内容/玩法反馈',
                 title='story',content='story',topic='version:3.0',
                 version_display='3.0',version_basis='source'),
            dict(review_id=4,date='2026-09-20T04:00:00Z',rating=2,sentiment_label='差评',
                 issue_category='服务器/网络问题',issue_categories='服务器/网络问题',
                 title='network',content='network',topic='',
                 version_display='版本未知',version_basis='unknown'),
        ]
        self.previous=[{**self.current[0],'review_id':99,'title':'payment','content':'payment',
            'issue_category':'充值/付费问题','issue_categories':'充值/付费问题'}]
        self.entry=self.folder/'issue_keyword_app.py'
        self.entry.write_text('''import pandas as pd
import streamlit as st
from unittest.mock import patch
import dashboard_ui as ui

def capture_chart(fig,key,*args,**kwargs):
    st.session_state['fig_'+key] = {
        'hover': '\\n'.join(str(trace.hovertemplate or '') for trace in fig.data),
        'labels': [str(label) for trace in fig.data for label in trace.y],
    }

current=pd.DataFrame(st.session_state['fixture_current'])
previous=pd.DataFrame(st.session_state['fixture_previous'])
current['date']=pd.to_datetime(current['date'],utc=True)
previous['date']=pd.to_datetime(previous['date'],utc=True)
with patch.object(ui,'chart',side_effect=capture_chart):
    ui.issue_keyword_charts({'app_name':'FixtureGame'}, {'current':current,'previous':previous})
''',encoding='utf-8')

    def app(self,*,legacy=False,selection=None):
        app=AppTest.from_file(str(self.entry),default_timeout=20)
        app.session_state['fixture_current']=[
            {key:value for key,value in row.items() if not(legacy and key in ('version_display','version_basis'))}
            for row in self.current]
        app.session_state['fixture_previous']=self.previous
        if selection is not None: app.session_state['issue_keyword_version']=selection
        app.run()
        self.assert_no_error(app)
        return app

    def assert_no_error(self,app):
        self.assertFalse(app.exception,[item.message for item in app.exception])

    def category_table(self,app):
        return next(item.value for item in app.dataframe if '类别' in item.value.columns)

    def evidence_table(self,app):
        return next(item.value for item in app.dataframe if 'content' in item.value.columns)

    def test_current_only_category_counts_and_hover_ignore_previous_reviews(self):
        app=self.app()
        table=self.category_table(app)
        self.assertEqual(table.columns.tolist(),['类别','评论数','占比'])
        self.assertEqual(table.set_index('类别').loc['Bug/闪退问题','评论数'],2)
        self.assertEqual(table.set_index('类别').loc['Bug/闪退问题','占比'],50)
        self.assertNotIn('充值/付费问题',table['类别'].tolist())
        self.assertFalse(any(item.key=='category_display' for item in app.radio))
        figure=app.session_state['fig_category_comparison']
        self.assertNotIn('本期',figure['hover'])
        self.assertNotIn('上期',figure['hover'])
        self.assertIn('评论数',figure['hover'])
        app.session_state['fixture_previous']=self.previous*100
        app.run()
        self.assert_no_error(app)
        pd.testing.assert_frame_equal(self.category_table(app),table)
        self.assertEqual(app.session_state['fig_category_comparison'],figure)

    def test_version_filter_keeps_inferred_and_source_rows_and_switches_evidence(self):
        app=self.app(selection='2.0')
        self.assertEqual(app.selectbox(key='issue_keyword_version').options,
            ['全部版本','2.0','3.0','版本未知'])
        self.assertEqual(self.category_table(app)['类别'].tolist(),['Bug/闪退问题'])
        self.assertEqual(self.category_table(app)['评论数'].tolist(),[2])
        self.assertEqual(self.category_table(app)['占比'].tolist(),[100])
        self.assertEqual(app.session_state['fig_keyword_ranking']['labels'],['crash'])
        self.assertEqual(len(self.evidence_table(app)),2)
        self.assertTrue(self.evidence_table(app)['content'].str.contains('crash').all())
        app.selectbox(key='issue_keyword_version').set_value('3.0').run()
        self.assert_no_error(app)
        self.assertEqual(self.category_table(app)['类别'].tolist(),['内容/玩法反馈'])
        self.assertEqual(app.session_state['fig_keyword_ranking']['labels'],['story'])
        self.assertEqual(app.selectbox(key='keyword_evidence').value,'story')
        self.assertEqual(self.evidence_table(app)['content'].tolist(),['story'])
        app.selectbox(key='issue_keyword_version').set_value('版本未知').run()
        self.assert_no_error(app)
        self.assertEqual(self.category_table(app)['类别'].tolist(),['服务器/网络问题'])
        self.assertEqual(app.session_state['fig_keyword_ranking']['labels'],['network'])
        self.assertEqual(self.evidence_table(app)['content'].tolist(),['network'])

    def test_invalid_version_selection_resets_and_legacy_topics_remain_filterable(self):
        app=self.app(selection='不存在的版本')
        self.assertEqual(app.selectbox(key='issue_keyword_version').value,'全部版本')
        self.assertEqual(self.category_table(app)['评论数'].sum(),4)
        legacy=self.app(legacy=True,selection='2.0')
        self.assertEqual(self.category_table(legacy)['评论数'].tolist(),[1])
        legacy.selectbox(key='issue_keyword_version').set_value('版本未知').run()
        self.assert_no_error(legacy)
        self.assertEqual(self.category_table(legacy)['评论数'].sum(),2)
        self.assertEqual(set(legacy.selectbox(key='keyword_evidence').options),{'crash','network'})


if __name__=='__main__': unittest.main()
