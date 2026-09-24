from __future__ import annotations

import unittest

import pandas as pd

from modules.keyword_extractor import extract_keywords


class KeywordExtractorTests(unittest.TestCase):
    def test_app_store_version_metadata_is_not_counted_as_keyword(self) -> None:
        data = pd.DataFrame(
            [
                {
                    "title": "Great support",
                    "content": "Support is great and story is fun",
                    "topic": "version:1.0.1",
                },
                {
                    "title": "Need support",
                    "content": "Support team fixed my issue",
                    "topic": "version:1.0.2",
                },
            ]
        )

        keywords = extract_keywords(data, top_n=10)
        keyword_values = set(keywords["keyword"].tolist())

        self.assertNotIn("version", keyword_values)
        self.assertNotIn("the", keyword_values)
        self.assertNotIn("game", keyword_values)
        self.assertIn("support", keyword_values)


if __name__ == "__main__":
    unittest.main()
