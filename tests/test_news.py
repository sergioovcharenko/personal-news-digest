import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import datetime as dt

# No bot token or live network is required for unit tests.
os.environ["TELEGRAM_BOT_TOKEN"] = ""
os.environ["TELEGRAM_CHAT_ID"] = ""
import main

class NewsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        main.DB_PATH = Path(self.tmp.name) / "test.db"
        self.db = main.connect()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_normalization(self):
        self.assertEqual(main.normalize("БпЛА: новини! - BBC"), "бпла новини")
        self.assertEqual(main.normalize("AI launches new model"), "ai launches new model")

    def test_url_tracking_removed(self):
        self.assertEqual(main.clean_url("https://Example.com/a/?utm_source=x&id=22&fbclid=y"),
                         "https://example.com/a?id=22")

    def test_identical_headlines_different_url_one_event(self):
        self.assertTrue(main.add(self.db, "New Ukrainian robotics system released",
                                  "https://first.example/a", "БпЛА та робототехніка"))
        self.assertTrue(main.add(self.db, "New Ukrainian robotics system released - Site B",
                                  "https://second.example/b", "БпЛА та робототехніка"))
        self.db.commit()
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM articles").fetchone()[0], 2)

    def test_duplicate_url_ignored(self):
        self.assertTrue(main.add(self.db, "Cybersecurity researchers release findings",
                                 "https://site.example/article", "Кібербезпека"))
        self.assertFalse(main.add(self.db, "Cybersecurity researchers release findings",
                                  "https://site.example/article?utm_source=share", "Кібербезпека"))

    def test_different_events_not_merged(self):
        self.assertTrue(main.add(self.db, "Robotics laboratory announces underwater vehicle",
                                  "https://site.example/a", "БпЛА та робототехніка"))
        self.assertTrue(main.add(self.db, "Cybersecurity firm discovers a browser exploit",
                                  "https://site.example/b", "Кібербезпека"))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 2)

    def test_balanced_topics(self):
        for i in range(4):
            main.add(self.db, f"Drone manufacturer releases aircraft model {i} with improvements",
                     f"https://site.example/d{i}", "БпЛА та робототехніка")
        main.add(self.db, "Cybersecurity experts publish detailed advisory",
                 "https://site.example/c", "Кібербезпека")
        self.db.commit()
        chosen = main._candidates(self.db)
        self.assertTrue(any(x["topic"] == "Кібербезпека" for x in chosen))


    def test_digest_uses_single_telegram_message(self):
        main.add(self.db, "Robotics team reveals upgraded aircraft model",
                 "https://example.com/drone", "БпЛА та робототехніка")
        main.add(self.db, "Security researchers discover an important vulnerability",
                 "https://example.com/security", "Кібербезпека")
        self.db.commit()
        with patch.object(main, "CHAT_ID", "test-chat"), patch.object(main, "send") as mock_send:
            sent = main.digest("test-one-message")
        self.assertEqual(sent, 2)
        mock_send.assert_called_once()
        self.assertIn("Кібербезпека", mock_send.call_args.args[0])
        with patch.object(main, "CHAT_ID", "test-chat"), patch.object(main, "send") as mock_send:
            self.assertEqual(main.digest("test-one-message"), 0)
            mock_send.assert_not_called()

    def test_oversized_digest_preserves_unsent_items(self):
        items = [
            {"id": i, "title": "A news event " + str(i) + " " + "x" * 130,
             "topic": "Кібербезпека", "url": "https://example.com/" + str(i), "sources": 2}
            for i in range(30)
        ]
        message, ids = main.compose_digest(items, dt.datetime(2026, 9, 27, 20), limit=900)
        self.assertLessEqual(len(message), 900)
        self.assertGreater(len(ids), 0)
        self.assertLess(len(ids), len(items))
        self.assertIn("залишено для наступного огляду", message)

if __name__ == "__main__":
    unittest.main()
