"""Tests for M5 transcript storage and retrieval (transcripts.py)."""

from __future__ import annotations

import unittest
from pathlib import Path

from sma_dashboard.db import init_db
from sma_dashboard.transcripts import (
    Transcript,
    _parse_quarter_label,
    load_transcript,
    search_transcripts,
)


class ParseQuarterLabelTests(unittest.TestCase):
    def test_valid_filename(self) -> None:
        self.assertEqual(_parse_quarter_label("2025_Q1_call.txt"), "2025_Q1")

    def test_valid_filename_q4(self) -> None:
        self.assertEqual(_parse_quarter_label("2024_Q4_call.txt"), "2024_Q4")

    def test_case_insensitive(self) -> None:
        self.assertEqual(_parse_quarter_label("2025_Q2_call.TXT"), "2025_Q2")

    def test_invalid_no_call_suffix(self) -> None:
        with self.assertRaises(ValueError):
            _parse_quarter_label("2025_Q1.txt")

    def test_invalid_wrong_format(self) -> None:
        with self.assertRaises(ValueError):
            _parse_quarter_label("transcript_2025.txt")

    def test_invalid_q5(self) -> None:
        with self.assertRaises(ValueError):
            _parse_quarter_label("2025_Q5_call.txt")


class LoadTranscriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path("data/raw")
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.db_path = Path("data/db/test_transcripts_load.db")
        self.db_path.unlink(missing_ok=True)
        self._created_files: list[Path] = []
        init_db(self.db_path)

    def tearDown(self) -> None:
        self.db_path.unlink(missing_ok=True)
        for path in self._created_files:
            path.unlink(missing_ok=True)

    def _write_transcript_file(self, filename: str, content: str) -> Path:
        path = self.tmp / filename
        path.unlink(missing_ok=True)
        path.write_text(content, encoding="utf-8")
        self._created_files.append(path)
        return path

    def test_load_and_retrieve(self) -> None:
        path = self._write_transcript_file("2025_Q1_call.txt", "Hello world transcript.")
        t = load_transcript(path, db_path=self.db_path, call_date="2025-03-28")
        self.assertIsInstance(t, Transcript)
        self.assertEqual(t.quarter_label, "2025_Q1")
        self.assertEqual(t.date, "2025-03-28")
        self.assertEqual(t.full_text, "Hello world transcript.")

    def test_load_requires_call_date(self) -> None:
        path = self._write_transcript_file("2025_Q2_call.txt", "text")
        with self.assertRaises(ValueError):
            load_transcript(path, db_path=self.db_path, call_date=None)

    def test_load_invalid_filename_raises(self) -> None:
        path = self._write_transcript_file("bad_name.txt", "text")
        with self.assertRaises(ValueError):
            load_transcript(path, db_path=self.db_path, call_date="2025-03-28")

    def test_load_with_notes(self) -> None:
        path = self._write_transcript_file("2025_Q1_call.txt", "text")
        t = load_transcript(path, db_path=self.db_path, call_date="2025-03-28", notes="reviewed")
        self.assertEqual(t.notes, "reviewed")


class SearchTranscriptsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path("data/raw")
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.db_path = Path("data/db/test_transcripts_search.db")
        self.empty_db_path = Path("data/db/test_transcripts_empty.db")
        self.db_path.unlink(missing_ok=True)
        self.empty_db_path.unlink(missing_ok=True)
        self._created_files: list[Path] = []
        init_db(self.db_path)
        # Load two transcripts
        self._load("2025_Q1_call.txt", "Q1 text about banks and tech.", "2025-03-28")
        self._load("2025_Q2_call.txt", "Q2 text about pipelines.", "2025-06-27")

    def tearDown(self) -> None:
        self.db_path.unlink(missing_ok=True)
        self.empty_db_path.unlink(missing_ok=True)
        for path in self._created_files:
            path.unlink(missing_ok=True)

    def _load(self, filename: str, content: str, date: str) -> None:
        path = self.tmp / filename
        path.unlink(missing_ok=True)
        path.write_text(content, encoding="utf-8")
        self._created_files.append(path)
        load_transcript(path, db_path=self.db_path, call_date=date)

    def test_most_recent_default(self) -> None:
        results = search_transcripts(self.db_path)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].quarter_label, "2025_Q2")

    def test_n_most_recent_two(self) -> None:
        results = search_transcripts(self.db_path, n_most_recent=2)
        self.assertEqual(len(results), 2)

    def test_filter_by_quarter_label(self) -> None:
        results = search_transcripts(self.db_path, quarter_label="2025_Q1")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].quarter_label, "2025_Q1")
        self.assertIn("banks", results[0].full_text)

    def test_filter_by_date_range(self) -> None:
        results = search_transcripts(
            self.db_path,
            date_range_start="2025-06-01",
            date_range_end="2025-12-31",
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].quarter_label, "2025_Q2")

    def test_no_results(self) -> None:
        results = search_transcripts(self.db_path, quarter_label="2024_Q4")
        self.assertEqual(results, [])

    def test_empty_db(self) -> None:
        init_db(self.empty_db_path)
        results = search_transcripts(self.empty_db_path)
        self.assertEqual(results, [])


if __name__ == "__main__":
    unittest.main()
