from __future__ import annotations

import unittest

import pandas as pd

from modules.insight_generator import build_operational_insights
from modules.report_generator import generate_operational_summary


class OperationalSummaryTests(unittest.TestCase):
    def test_social_opportunity_fallback_does_not_claim_missing_keyword_as_topic(self) -> None:
        data = pd.DataFrame(
            [
                {
                    "platform": "App Store",
                    "content": "Server lag after update",
                    "sentiment_label": "差评",
                    "issue_category": "服务器/网络问题",
                    "interaction_count": 3,
                    "high_interaction": False,
                },
                {
                    "platform": "App Store",
                    "content": "Fun with friends",
                    "sentiment_label": "好评",
                    "issue_category": "内容/玩法反馈",
                    "interaction_count": 5,
                    "high_interaction": False,
                },
            ]
        )

        summary = generate_operational_summary(data, game_name="Test Game")

        self.assertIn("未覆盖小红书、B站或微博", summary)
        self.assertNotIn("暂无明显关键词 等可二创话题", summary)

    def test_other_is_reported_as_review_queue_not_a_top_issue(self) -> None:
        data = pd.DataFrame(
            [
                {
                    "platform": "App Store",
                    "content": "不满意",
                    "sentiment_label": "差评",
                    "issue_category": "其他",
                    "interaction_count": 0,
                },
                {
                    "platform": "App Store",
                    "content": "服务器一直掉线",
                    "sentiment_label": "差评",
                    "issue_category": "服务器/网络问题",
                    "interaction_count": 0,
                },
            ]
        )

        insights = build_operational_insights(data, game_name="Test Game")

        self.assertEqual(insights.top_issues, [("服务器/网络问题", 1)])
        self.assertIn("待人工复核", insights.negative_analysis)
        self.assertNotIn("主要明确问题为：其他", insights.negative_analysis)

    def test_actions_are_sequential_and_change_with_top_issues(self) -> None:
        server_data = pd.DataFrame(
            [
                {
                    "platform": "App Store",
                    "content": "服务器掉线",
                    "sentiment_label": "差评",
                    "issue_category": "服务器/网络问题",
                    "interaction_count": 0,
                }
            ]
        )
        payment_data = pd.DataFrame(
            [
                {
                    "platform": "App Store",
                    "content": "卡池太贵",
                    "sentiment_label": "差评",
                    "issue_category": "付费/商业化问题",
                    "interaction_count": 0,
                }
            ]
        )

        server_insights = build_operational_insights(server_data)
        payment_insights = build_operational_insights(payment_data)
        summary = server_insights.to_text()

        self.assertIn("1. 【P1｜服务器/网络问题】", summary)
        self.assertIn("2. 【P2｜数据补充】", summary)
        self.assertNotIn("3. 【", summary)
        self.assertNotEqual(server_insights.actions[0], payment_insights.actions[0])


if __name__ == "__main__":
    unittest.main()
