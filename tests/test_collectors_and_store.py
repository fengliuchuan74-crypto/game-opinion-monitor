from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock

import pandas as pd
import requests

from collectors.app_store import AppStoreCollector
from modules.data_loader import prepare_dataframe
from modules.filters import apply_filters
from modules.issue_classifier import analyze_issues
from modules.issue_classifier import classify_issue
from modules.review_store import clear_reviews, list_game_profiles, read_reviews, save_reviews
from modules.sentiment import add_sentiment_analysis
from modules.sentiment import analyze_sentiment


FIXTURE = Path(__file__).parent / "fixtures" / "appstore_reviews.json"


def response_with_payload(payload: dict, status_code: int = 200) -> Mock:
    response = Mock()
    response.status_code = status_code
    response.json.return_value = payload
    return response


class AppStoreCollectorTests(unittest.TestCase):
    def test_search_apps_maps_itunes_search_results(self) -> None:
        session = Mock()
        response = Mock()
        response.status_code = 200
        response.json.return_value = {
            "results": [
                {
                    "trackId": 123456,
                    "trackName": "Goose Goose Duck",
                    "sellerName": "Gaggle Studios",
                    "bundleId": "com.example.goose",
                    "averageUserRating": 4.6,
                    "userRatingCount": 99,
                    "trackViewUrl": "https://apps.apple.com/app/id123456",
                }
            ]
        }
        session.get.return_value = response
        collector = AppStoreCollector(log_dir=Path(tempfile.mkdtemp()), session=session)

        results, errors = collector.search_apps("Goose Goose Duck", country="us")

        self.assertEqual(errors, [])
        self.assertEqual(results[0]["app_id"], "123456")
        self.assertEqual(results[0]["name"], "Goose Goose Duck")
        session.get.assert_called_once()

    def test_parse_reviews_maps_public_feed_fields(self) -> None:
        payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        collector = AppStoreCollector(log_dir=Path(tempfile.mkdtemp()))

        reviews = collector.parse_reviews(payload, app_id="123456", country="cn")

        self.assertEqual(len(reviews), 2)
        self.assertEqual(reviews[0].platform, "App Store")
        self.assertEqual(reviews[0].external_id, "review-001")
        self.assertEqual(reviews[0].rating, 5.0)
        self.assertEqual(reviews[0].likes, 12)
        self.assertEqual(reviews[0].data_source, "app_store_public_feed")

    def test_collect_continues_after_single_empty_page(self) -> None:
        payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        empty_payload = {"feed": {"entry": []}}
        session = Mock()
        session.get.side_effect = [
            response_with_payload(payload),
            response_with_payload(empty_payload),
            response_with_payload(payload),
        ]
        collector = AppStoreCollector(log_dir=Path(tempfile.mkdtemp()), session=session)

        result = collector.collect(app_id="123456", country="cn", max_pages=3, delay_seconds=0)

        self.assertEqual(result.fetched_count, 4)
        self.assertEqual(result.failed_count, 0)
        self.assertEqual(len(result.warnings), 1)

    def test_collect_stops_when_target_review_count_is_reached(self) -> None:
        payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        session = Mock()
        session.get.side_effect = [
            response_with_payload(payload),
            response_with_payload(payload),
            response_with_payload(payload),
        ]
        collector = AppStoreCollector(log_dir=Path(tempfile.mkdtemp()), session=session)

        result = collector.collect(
            app_id="123456",
            country="cn",
            max_pages=3,
            target_reviews=3,
            delay_seconds=0,
        )

        self.assertEqual(result.fetched_count, 3)
        self.assertEqual(session.get.call_count, 2)

    def test_collect_without_page_limit_can_continue_past_ten_pages(self) -> None:
        payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        session = Mock()
        session.get.side_effect = [response_with_payload(payload) for _ in range(13)]
        collector = AppStoreCollector(log_dir=Path(tempfile.mkdtemp()), session=session)

        result = collector.collect(
            app_id="123456",
            country="cn",
            target_reviews=25,
            delay_seconds=0,
        )

        self.assertEqual(result.fetched_count, 25)
        self.assertEqual(session.get.call_count, 13)
        self.assertEqual(result.requested, 13)

    def test_collect_network_timeout_returns_friendly_error(self) -> None:
        session = Mock()
        session.get.side_effect = requests.Timeout()
        collector = AppStoreCollector(log_dir=Path(tempfile.mkdtemp()), session=session)

        result = collector.collect(app_id="123456", country="cn", max_pages=1, delay_seconds=0)

        self.assertEqual(result.fetched_count, 0)
        self.assertEqual(result.failed_count, 1)
        self.assertIn("超时", result.errors[0])


class ReviewStoreTests(unittest.TestCase):
    def test_sqlite_save_dedupes_and_feeds_analysis_pipeline(self) -> None:
        payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        reviews = AppStoreCollector(log_dir=Path(tempfile.mkdtemp())).parse_reviews(
            payload, app_id="123456", country="cn"
        )
        db_path = Path(tempfile.mkdtemp()) / "reviews.sqlite3"

        first = save_reviews(db_path, reviews)
        second = save_reviews(db_path, reviews)
        stored = read_reviews(db_path)
        package = prepare_dataframe(stored, "本地已采集评论")
        analyzed = analyze_issues(add_sentiment_analysis(package.data))

        self.assertEqual(first.inserted, 2)
        self.assertEqual(second.duplicates, 2)
        self.assertEqual(len(stored), 2)
        self.assertIn("data_source", analyzed.columns)
        self.assertIn("operation_suggestion", analyzed.columns)
        self.assertIn("差评", set(analyzed["sentiment_label"]))

    def test_sqlite_reads_and_clears_reviews_by_game_profile(self) -> None:
        payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        collector = AppStoreCollector(log_dir=Path(tempfile.mkdtemp()))
        reviews_a = collector.parse_reviews(payload, app_id="111", country="cn")
        reviews_b = collector.parse_reviews(payload, app_id="222", country="us")
        db_path = Path(tempfile.mkdtemp()) / "reviews.sqlite3"

        save_reviews(db_path, reviews_a + reviews_b)
        only_a = read_reviews(db_path, app_id="111", country="cn")
        only_b = read_reviews(db_path, app_id="222", country="us")
        all_profiles = list_game_profiles(db_path)
        removed = clear_reviews(db_path, app_id="111", country="cn")
        after_clear_a = read_reviews(db_path, app_id="111", country="cn")
        after_clear_b = read_reviews(db_path, app_id="222", country="us")

        self.assertEqual(len(only_a), 2)
        self.assertEqual(len(only_b), 2)
        self.assertEqual(len(all_profiles), 2)
        self.assertEqual(removed, 2)
        self.assertEqual(len(after_clear_a), 0)
        self.assertEqual(len(after_clear_b), 2)

    def test_timezone_dates_filter_by_whole_day_without_comparison_error(self) -> None:
        raw = [
            {
                "platform": "App Store",
                "date": "2026-06-27T07:02:11-07:00",
                "author": "A",
                "content": "更新后闪退",
                "rating": 1,
                "likes": 0,
                "comments": 0,
                "shares": 0,
            }
        ]
        package = prepare_dataframe(pd.DataFrame(raw), "timezone test")
        analyzed = analyze_issues(add_sentiment_analysis(package.data))

        filtered = apply_filters(
            analyzed,
            ["App Store"],
            list(analyzed["sentiment_label"].unique()),
            list(analyzed["issue_category"].unique()),
            (date(2026, 6, 27), date(2026, 6, 27)),
        )

        self.assertEqual(len(filtered), 1)

    def test_date_range_filter_excludes_undated_rows(self) -> None:
        raw = [
            {
                "platform": "App Store",
                "date": "2026-06-27",
                "author": "A",
                "content": "Server lag after update",
                "rating": 1,
            },
            {
                "platform": "App Store",
                "date": "",
                "author": "B",
                "content": "No date should not enter a date range analysis",
                "rating": 3,
            },
        ]
        package = prepare_dataframe(pd.DataFrame(raw), "date range test")
        analyzed = analyze_issues(add_sentiment_analysis(package.data))

        filtered = apply_filters(
            analyzed,
            ["App Store"],
            list(analyzed["sentiment_label"].unique()),
            list(analyzed["issue_category"].unique()),
            (date(2026, 6, 27), date(2026, 6, 27)),
        )

        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered.iloc[0]["author"], "A")


class EnglishAnalysisTests(unittest.TestCase):
    def test_english_issue_classification_matches_app_store_review_topics(self) -> None:
        self.assertEqual(
            classify_issue("False advertising, the game looks nothing like the ads")[
                "issue_category"
            ],
            "广告/宣传问题",
        )
        self.assertEqual(
            classify_issue("Everything is behind a paywall and feels p2w")[
                "issue_category"
            ],
            "付费/商业化问题",
        )
        self.assertEqual(
            classify_issue("Progression is gated by upgrade materials and afk resources")[
                "issue_category"
            ],
            "养成/进度反馈",
        )
        self.assertEqual(
            classify_issue("想赚女性的钱还不尊重女性消费者")["issue_category"],
            "价值观/性别争议",
        )
        self.assertEqual(
            classify_issue("请回应政治立场问题，文化出口却抹掉汉字")["issue_category"],
            "政治/历史争议",
        )
        self.assertEqual(
            classify_issue("不听玩家意见就倒闭吧，退钱下架")["issue_category"],
            "退款/下架诉求",
        )
        self.assertEqual(
            classify_issue("剧情主线不更，男主人设 ooc")["issue_category"],
            "角色/剧情争议",
        )
        self.assertEqual(
            classify_issue("分化社区吵架，水军骂人带节奏")["issue_category"],
            "官方回应/公关信任",
        )
        self.assertEqual(
            classify_issue("每天一星打卡，继续给你打一星")["issue_category"],
            "评分抗议/集中差评",
        )
        self.assertEqual(
            classify_issue("国服和外服区别对待，中文文案翻译也有问题")["issue_category"],
            "本地化/地区差异",
        )
        self.assertEqual(
            classify_issue("玩法单一而且内容重复，实在没东西玩")["issue_category"],
            "可玩性/内容单薄",
        )
        self.assertEqual(
            classify_issue("每天一星，因为辱华问题不能接受")["issue_category"],
            "政治/历史争议",
        )
        self.assertEqual(
            classify_issue("虚假承诺又背刺玩家，厂商信用已经没了")["issue_category"],
            "厂商信任/经营争议",
        )

    def test_english_sentiment_uses_common_review_words_and_rating(self) -> None:
        self.assertEqual(analyze_sentiment("Amazing and fun mobile game", 5)["sentiment_label"], "好评")
        self.assertEqual(analyze_sentiment("Awful false advertising and boring gameplay", 1)["sentiment_label"], "差评")


if __name__ == "__main__":
    unittest.main()
