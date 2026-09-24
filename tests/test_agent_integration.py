"""Cross-module checks for provenance and effective review classifications."""
from __future__ import annotations

import json
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from collectors.base import RawReview
from modules.agent_analysis import (_process_next, analysis_status, configure_analysis,
    latest_report, recent_analysis_runs, request_analysis)
from modules.dashboard import sentiments
from modules.deliverables import build_report, workbook
from modules.operations import analyze_reviews, issue_plans, review_override, snapshot
from modules.review_cards import card_html
from modules.review_store import init_db, read_reviews, save_reviews


class AgentEffectiveAnalysisTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.db = self.folder/'reviews.sqlite3'
        self.start = '2026-09-23T00:00:00+08:00'
        self.end = '2026-09-24T00:00:00+08:00'
        self.profile = dict(app_id='111',country='cn',app_name='集成验收游戏')
        init_db(self.db)
        configure_analysis(self.db,'111','cn',model='integration-fixture-model')
        save_reviews(self.db,[self.review()])

    def review(self, identity='first', **changes):
        values = dict(platform='App Store',external_id=identity,app_id='111',country='cn',
            date='2026-09-23T01:00:00+00:00',author='集成测试玩家',title='',
            content='希望增加一张新地图',rating=1)
        values.update(changes)
        return RawReview(**values)

    def effective(self):
        return analyze_reviews(read_reviews(self.db,app_id='111',country='cn'),self.db)

    def process(self):
        def runner(prompt, schema, work_dir, **kwargs):
            directory = Path(work_dir)
            if 'reviews' in schema['properties']:
                rows = json.loads((directory/'reviews.json').read_text('utf-8'))
                output = {'reviews':[dict(review_id=row['review_id'],content_hash=row['content_hash'],
                    sentiment_label='中评',issue_category='内容/玩法反馈',
                    issue_categories=['内容/玩法反馈'],needs_review=False,
                    reason='玩家提出地图内容需求，没有明确情绪词。',demand='新增地图',
                    target='游戏内容',evidence_quote=row['content']) for row in rows]}
            elif 'requests' in schema['properties']:
                output = {'requests':[]}
            else:
                context = json.loads((directory/'report_context.json').read_text('utf-8'))
                evidence = context['evidence'][0]
                category = evidence['analysis']['issue_category']
                output = dict(summary='发现具体内容需求，需结合产品计划核实。',
                    findings=[dict(title='核实具体玩家诉求',category=category,
                        observation='原文包含具体反馈。',hypothesis='影响范围尚未确认。',
                        actions=['核对原文并整理需求候选。'],owner='内容策划',
                        validation='确认是否进入需求评审。',evidence_ids=[evidence['review_id']])],
                    positive=[],limitations=['测试结果仅用于隔离验收。'])
            return {'output':output,'model':'integration-fixture-model','usage':{}}
        request_analysis(self.db,'111','cn',start_at=self.start,end_at=self.end,
            model='integration-fixture-model')
        self.assertTrue(_process_next(self.db,self.folder/'agent',runner=runner))
        run = recent_analysis_runs(self.db,'111','cn')[0]
        self.assertEqual(run['status'],'completed',run.get('error'))

    def test_valid_model_overlay_keeps_rule_provenance_available(self):
        before = self.effective().iloc[0]
        self.assertEqual(before.analysis_source,'规则初筛')
        self.assertFalse(before.agent_analyzed)
        self.assertEqual(before.sentiment_label,'差评')
        self.process()
        row = self.effective().iloc[0]
        self.assertEqual(row.analysis_source,'Agent')
        self.assertTrue(row.agent_analyzed)
        self.assertEqual(row.sentiment_label,'中评')
        self.assertEqual(row.issue_category,'内容/玩法反馈')
        self.assertEqual(row.rule_sentiment_label,'差评')
        self.assertIn('玩家提出地图',row.agent_reason)

    def test_valid_human_override_wins_over_saved_model(self):
        self.process()
        row = self.effective().iloc[0]
        review_override(self.db,int(row.review_id),row.content_hash,'差评','Bug/闪退问题',
            '验收QA','另行核对上下文后确认')
        current = self.effective().iloc[0]
        self.assertTrue(current.manual_reviewed)
        self.assertEqual(current.analysis_source,'人工复核')
        self.assertEqual(current.sentiment_label,'差评')
        self.assertEqual(current.issue_category,'Bug/闪退问题')
        self.assertIn('人工复核',current.analysis_basis)

    def test_source_update_invalidates_model_and_human_results(self):
        self.process()
        row = self.effective().iloc[0]
        review_override(self.db,int(row.review_id),row.content_hash,'差评','Bug/闪退问题',
            '验收QA','验收旧版本')
        save_reviews(self.db,[self.review(content='修复后很好玩，很稳定',rating=5,
            date='2026-09-23T02:00:00+00:00')])
        current = self.effective().iloc[0]
        self.assertNotEqual(current.content_hash,row.content_hash)
        self.assertFalse(current.agent_analyzed)
        self.assertFalse(current.manual_reviewed)
        self.assertTrue(current.override_stale)
        self.assertEqual(current.analysis_source,'规则初筛')
        self.assertEqual(current.agent_reason,'')
        status = analysis_status(self.db,'111','cn',self.start,self.end)
        self.assertEqual(status['pending'],1)
        report = latest_report(self.db,'111','cn',self.start,self.end)
        self.assertTrue(report['stale'])

    def test_agent_only_and_rule_views_keep_exports_on_the_selected_basis(self):
        from agent_ui import analysis_data
        self.process()
        save_reviews(self.db,[self.review('pending',content='非常好玩，很稳定',rating=5)])
        effective = self.effective()
        agent = analysis_data(effective,'Agent分析')
        pending = agent.loc[agent.external_id.eq('pending')].iloc[0]
        self.assertEqual(pending.analysis_source,'待分析')
        self.assertEqual(pending.sentiment_label,'待复核')
        self.assertFalse(pending.agent_analyzed)
        rules = analysis_data(effective,'规则初筛')
        self.assertTrue(rules.analysis_source.eq('规则初筛').all())
        self.assertEqual(rules.loc[rules.external_id.eq('first'),'sentiment_label'].iloc[0],'差评')
        view = snapshot(agent,start=self.start,end=self.end,now=self.end)
        view.update(analysis_mode='Agent分析',agent_report=latest_report(self.db,'111','cn',self.start,self.end))
        book = pd.ExcelFile(BytesIO(workbook(self.profile,view,[],[],[],include_workflow=False)))
        exported = pd.read_excel(book,sheet_name='评论证据').set_index('external_id')
        self.assertEqual(exported.loc['first','analysis_source'],'Agent')
        self.assertEqual(exported.loc['pending','analysis_source'],'待分析')
        self.assertEqual(exported.loc['pending','sentiment_label'],'待复核')
        expected = sentiments(agent).set_index('情绪')['评论数'].to_dict()
        actual = pd.read_excel(book,sheet_name='情绪分布').set_index('情绪')['评论数'].to_dict()
        self.assertEqual(actual,expected)

    def test_model_change_invalidates_old_results_but_keeps_human_corrections(self):
        from agent_ui import analysis_data
        self.process()
        previous = self.effective().iloc[0]
        self.assertIsNotNone(latest_report(self.db,'111','cn',self.start,self.end))
        configure_analysis(self.db,'111','cn',model='different-fixture-model')
        current = self.effective().iloc[0]
        self.assertFalse(current.agent_analyzed)
        self.assertEqual(current.agent_reason,'')
        self.assertEqual(current.analysis_source,'规则初筛')
        self.assertIsNone(latest_report(self.db,'111','cn',self.start,self.end))
        status = analysis_status(self.db,'111','cn',self.start,self.end)
        self.assertEqual((status['analyzed'],status['pending']),(0,1))
        pending = analysis_data(self.effective(),'Agent分析').iloc[0]
        self.assertEqual(pending.analysis_source,'待分析')
        self.assertEqual(pending.sentiment_label,'待复核')
        review_override(self.db,int(previous.review_id),previous.content_hash,
            '中评','内容/玩法反馈','验收QA','保留有效的人工作品需求判断')
        for mode in ('Agent分析','规则初筛'):
            with self.subTest(mode=mode):
                human = analysis_data(self.effective(),mode).iloc[0]
                self.assertTrue(human.manual_reviewed)
                self.assertEqual(human.analysis_source,'人工复核')
                self.assertEqual(human.sentiment_label,'中评')
                self.assertEqual(human.issue_category,'内容/玩法反馈')

    def test_rule_analysis_is_offline_without_cancelling_existing_jobs(self):
        from agent_ui import analysis_data
        from modules.monitoring import configure_target, request_collection, target_state
        self.process()
        save_reviews(self.db,[self.review('pending',content='更新后闪退，无法登录')])
        configure_analysis(self.db,'111','cn',auto_enabled=True,model='integration-fixture-model')
        request_analysis(self.db,'111','cn',start_at=self.start,end_at=self.end,
            model='integration-fixture-model')
        configure_target(self.db,'111','cn',enabled=True)
        self.assertTrue(request_collection(self.db,'111','cn'))
        runs_before = recent_analysis_runs(self.db,'111','cn')
        monitoring_before = target_state(self.db,'111','cn')
        settings_before = analysis_status(self.db,'111','cn',self.start,self.end)['settings']
        offline_error = AssertionError('Rule analysis must not invoke network or a model')
        with patch('requests.sessions.Session.request',side_effect=offline_error), \
                patch('socket.create_connection',side_effect=offline_error), \
                patch('subprocess.Popen',side_effect=offline_error), \
                patch('modules.codex_runner.run_codex',side_effect=offline_error):
            effective = self.effective()
            rules = analysis_data(effective,'规则初筛')
            view = snapshot(rules,start=self.start,end=self.end,now=self.end)
            view['analysis_mode'] = '规则初筛'
            plans = issue_plans(view)
            markdown, report, meta = build_report(self.profile,view,plans,[],include_workflow=False)
            book = pd.ExcelFile(BytesIO(workbook(self.profile,view,plans,[],[],include_workflow=False)))
            self.assertEqual(meta['analysis_mode'],'规则初筛')
            self.assertEqual(meta['agent'],{})
            self.assertNotIn('Agent深度报告',book.sheet_names)
            self.assertNotIn('## Agent 深度分析',markdown)
            self.assertIn('规则初筛',report)
            exported = pd.read_excel(book,sheet_name='评论证据')
            self.assertTrue(exported.analysis_source.eq('规则初筛').all())
            self.assertFalse(exported.agent_analyzed.any())
            for field in ['agent_reason','agent_demand','agent_target','agent_quote','agent_model']:
                self.assertTrue(exported[field].fillna('').eq('').all(),field)
            for row in rules.to_dict('records'):
                self.assertNotIn('Agent 解读',card_html(row))
            # Selecting either local view is presentation, not a queue command.
            analysis_data(effective,'Agent分析')
            self.assertEqual(recent_analysis_runs(self.db,'111','cn'),runs_before)
            self.assertEqual(target_state(self.db,'111','cn'),monitoring_before)
            self.assertEqual(analysis_status(self.db,'111','cn',self.start,self.end)['settings'],settings_before)


class AgentCardTests(unittest.TestCase):
    def row(self, **changes):
        values = dict(review_id=1, date='2026-09-23T01:00:00+00:00', author='玩家',
            title='反馈', content='更新后闪退', rating=1, topic='version:2.0',
            sentiment_label='差评', issue_category='Bug/闪退问题',
            issue_categories='Bug/闪退问题', needs_review=False)
        values.update(changes)
        return values

    def test_model_card_uses_escaped_model_reason_and_demand(self):
        markup = card_html(self.row(analysis_source='Agent',
            agent_reason='<script>reason()</script>定位到更新后的闪退',
            agent_demand='<img src=x onerror=alert(1)>希望恢复登录'))
        self.assertIn('判断来源：Agent', markup)
        self.assertIn('Agent 解读', markup)
        self.assertIn('玩家诉求：', markup)
        self.assertIn('&lt;script&gt;', markup)
        self.assertIn('&lt;img', markup)
        self.assertNotIn('<script>', markup)
        self.assertNotIn('<img', markup)
        self.assertNotIn('规则处理参考', markup)
        self.assertNotIn('收集版本、机型', markup)

    def test_rule_and_human_cards_do_not_reuse_a_cached_model_interpretation(self):
        for source in ('规则初筛', '人工复核'):
            with self.subTest(source=source):
                markup = card_html(self.row(analysis_source=source,
                    analysis_basis='人工复核：QA', agent_reason='旧模型解读'))
                self.assertIn('判断来源：'+source, markup)
                self.assertIn('规则处理参考', markup)
                self.assertNotIn('旧模型解读', markup)
                self.assertNotIn('Agent 解读', markup)
                if source=='人工复核': self.assertIn('人工复核依据', markup)

    def test_pending_card_does_not_present_fallback_as_an_agent_judgment(self):
        markup = card_html(self.row(analysis_source='待分析', sentiment_label='好评',
            agent_reason='过期模型判断', needs_review=True))
        self.assertIn('待 Agent 分析', markup)
        self.assertIn('判断来源：待Agent分析', markup)
        self.assertNotIn('过期模型判断', markup)
        self.assertNotIn('规则处理参考', markup)
        self.assertNotIn('Bug/闪退问题', markup)
        self.assertNotIn('review-card good', markup)
        self.assertNotIn('>好评<', markup)
        self.assertIn('更新后闪退', markup)


if __name__ == '__main__':
    unittest.main()
