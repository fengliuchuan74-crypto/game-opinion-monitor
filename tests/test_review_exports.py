from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd

from collectors.base import RawReview
from modules.review_exports import export_reviews_by_game
from modules.review_store import save_reviews


class ReviewExcelExportTests(unittest.TestCase):
    def test_exports_saved_reviews_into_separate_game_excels(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        db_path = temp_dir / "reviews.sqlite3"
        output_dir = temp_dir / "review_excels"
        reviews = [
            RawReview(
                platform="App Store",
                external_id="a-001",
                date="2026-06-01T08:00:00+00:00",
                author="Player A",
                title="Too laggy",
                content="Server lag and crash after update.",
                rating=1,
                app_id="111",
                country="us",
                data_source="fixture",
            ),
            RawReview(
                platform="App Store",
                external_id="b-001",
                date="2026-06-02T08:00:00+00:00",
                author="Player B",
                title="Fun",
                content="Amazing fun game with friends.",
                rating=5,
                app_id="222",
                country="cn",
                data_source="fixture",
            ),
        ]

        save_reviews(db_path, reviews)
        exported_files = export_reviews_by_game(db_path, output_dir)

        self.assertEqual(len(exported_files), 2)
        self.assertTrue(all(path.exists() for path in exported_files))
        exported_app_ids = set()
        for path in exported_files:
            data = pd.read_excel(path, sheet_name="comments")
            profile = pd.read_excel(path, sheet_name="profile")
            self.assertEqual(data["app_id"].nunique(), 1)
            self.assertIn("operation_suggestion", data.columns)
            self.assertIn("sentiment_label", data.columns)
            self.assertEqual(profile["app_id"].nunique(), 1)
            exported_app_ids.add(str(data["app_id"].iloc[0]))

        self.assertEqual(exported_app_ids, {"111", "222"})


if __name__ == "__main__":
    unittest.main()
