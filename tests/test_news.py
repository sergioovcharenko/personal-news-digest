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
                                  "https://first.example/a", "БпЛА та дрони"))
        self.assertTrue(main.add(self.db, "New Ukrainian robotics system released - Site B",
                                  "https://second.example/b", "БпЛА та дрони"))
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
                                  "https://site.example/a", "БпЛА та дрони"))
        self.assertTrue(main.add(self.db, "Cybersecurity firm discovers a browser exploit",
                                  "https://site.example/b", "Кібербезпека"))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 2)

    def test_balanced_topics(self):
        for i in range(4):
            main.add(self.db, f"Drone manufacturer releases aircraft model {i} with improvements",
                     f"https://site.example/d{i}", "БпЛА та дрони")
        main.add(self.db, "Cybersecurity experts publish detailed advisory",
                 "https://site.example/c", "Кібербезпека")
        self.db.commit()
        chosen = main._candidates(self.db)
        self.assertTrue(any(x["topic"] == "Кібербезпека" for x in chosen))


    def test_digest_uses_single_telegram_message(self):
        main.add(self.db, "Robotics team reveals upgraded aircraft model",
                 "https://example.com/drone", "БпЛА та дрони")
        main.add(self.db, "Security researchers discover an important vulnerability",
                 "https://example.com/security", "Кібербезпека")
        self.db.commit()
        with patch.object(main, "CHAT_ID", "test-chat"), patch.object(main, "translate_title", side_effect=lambda db, title: (title, True)), patch.object(main, "send") as mock_send:
            sent = main.digest("test-one-message")
        self.assertEqual(sent, 2)
        mock_send.assert_called_once()
        self.assertIn("Кібербезпека", mock_send.call_args.args[0])
        self.assertEqual(mock_send.call_args.kwargs["parse_mode"], "HTML")
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
        self.assertIn("залишено на наступний огляд", message)


    def test_digest_groups_topics_and_escapes_html(self):
        items = [
            {"id": 1, "title": "AI & machine <learning> advances",
             "topic": "Штучний інтелект", "url": "https://a.example/?x=1&y=2", "sources": 1},
            {"id": 2, "title": "Another AI article about models",
             "topic": "Штучний інтелект", "url": "https://b.example/a", "sources": 2},
        ]
        msg, ids = main.compose_digest(items, dt.datetime(2026,9,27,20))
        self.assertEqual(ids, [1, 2])
        self.assertEqual(msg.count("<b>Штучний інтелект</b>"), 1)
        self.assertIn("AI &amp; machine &lt;learning&gt;", msg)
        self.assertIn("x=1&amp;y=2", msg)
        self.assertIn("Читати джерело", msg)

    def test_structured_description(self):
        items = [
            {"id": 1, "title": "New policy measures announced", "summary": "Officials published a detailed proposal.",
             "topic": "Політика та дипломатія", "url": "https://news.example/story", "sources": 1},
        ]
        msg, ids = main.compose_digest(items, dt.datetime(2026,9,27,20),
            title_transform=lambda x: ({"New policy measures announced": "Оголошено нові заходи політики",
                                        "Officials published a detailed proposal.": "Посадовці опублікували докладну пропозицію."}.get(x,x), True))
        self.assertEqual(ids, [1])
        self.assertIn("🏛️ <b>Політика та дипломатія</b>", msg)
        self.assertIn("Оголошено нові заходи політики", msg)
        self.assertIn("Коротко: Посадовці опублікували", msg)

    def test_translation_uses_cache(self):
        self.db.execute(
            "INSERT INTO translated_titles(source,ukrainian,translated_at) VALUES(?,?,?)",
            ("Artificial intelligence development", "Розвиток штучного інтелекту", "2026-09-27"))
        self.db.commit()
        with patch.object(main, "GoogleTranslator") as translator:
            title, ok = main.translate_title(self.db, "Artificial intelligence development")
        self.assertTrue(ok)
        self.assertEqual(title, "Розвиток штучного інтелекту")
        translator.assert_not_called()

    def test_foreign_title_translation(self):
        with patch.object(main, "detect", return_value="en"), patch.object(main, "GoogleTranslator") as client:
            client.return_value.translate.return_value = "Новий прорив у робототехніці"
            title, ok = main.translate_title(self.db, "New breakthrough in robotics")
        self.assertTrue(ok)
        self.assertEqual(title, "Новий прорив у робототехніці")


    def test_telegram_menu_has_working_actions(self):
        keyboard = main.menu_keyboard()
        actions = [button["callback_data"] for row in keyboard["inline_keyboard"] for button in row]
        self.assertEqual(actions, ["news", "topics", "sources", "settings", "help"])
        with patch.object(main, "send") as mock_send:
            main.handle_action("start", 123, 1)
            mock_send.assert_called_once()
            self.assertEqual(mock_send.call_args.kwargs["reply_markup"], main.persistent_keyboard())
            self.assertIn("\n", mock_send.call_args.args[0])

    def test_persistent_keyboard_and_startup_announcement(self):
        keys = main.persistent_keyboard()
        self.assertTrue(keys["is_persistent"])
        self.assertEqual(keys["keyboard"][0][0]["text"], "📰 Новини зараз")
        with patch.object(main, "CHAT_ID", "555"), patch.object(main, "show_menu") as show_menu:
            main.announce_menu_once()
            main.announce_menu_once()
            show_menu.assert_called_once_with("555")

    def test_news_button_rejects_other_chats(self):
        with patch.object(main, "CHAT_ID", "owner-test-id"), patch.object(main, "collect") as collect, patch.object(main, "digest") as digest, patch.object(main, "send") as mock_send:
            main.handle_action("news", 555, 2)
            collect.assert_not_called()
            digest.assert_not_called()
            mock_send.assert_called_once()

    def test_news_button_runs_for_owner(self):
        with patch.object(main, "CHAT_ID", "555"), patch.object(main, "collect") as collect, patch.object(main, "digest") as digest:
            main.handle_action("news", 555, 5)
            collect.assert_called_once()
            digest.assert_called_once_with("manual-5")


    def test_selected_filters_exactly_match_user_choice(self):
        self.assertEqual(list(main.TOPICS), [
            "БпЛА та дрони",
            "Робототехніка",
            "Штучний інтелект",
            "Україна",
            "Війна в Україні",
            "Військові технології",
            "Кібербезпека",
            "Технології та електроніка",
            "Космос і супутники",
            "Радіозв’язок та SDR",
            "ArduPilot / PX4 / QGroundControl",
            "Акумулятори та енергетика",
            "Політика та дипломатія",
            "Наука та дослідження",
        ])

    def test_untranslated_foreign_headline_is_not_sent(self):
        items = [{"id": 1, "title": "Foreign headline", "summary": "",
                  "topic": "Штучний інтелект", "url": "https://example.com/a", "sources": 1}]
        msg, ids = main.compose_digest(
            items, dt.datetime(2026, 9, 28, 20),
            title_transform=lambda text: (text, False))
        self.assertEqual(ids, [])
        self.assertNotIn("Foreign headline", msg)


    def test_styled_digest_and_settings(self):
        items = [{"id": 1, "title": "Новий український дрон", "summary": "Короткий опис події.",
                  "topic": "БпЛА та дрони", "url": "https://example.com/a", "sources": 3}]
        msg, ids = main.compose_digest(items, dt.datetime(2026, 9, 28, 8))
        self.assertEqual(ids, [1])
        self.assertIn("━━━━━━━━━━━━━━", msg)
        self.assertIn("🔎 3", msg)
        self.assertIn("<b>1. Новий український дрон</b>", msg)
        with patch.object(main, "send") as mock_send:
            main.show_settings(123)
            mock_send.assert_called_once()
            self.assertIn("Налаштування", mock_send.call_args.args[0])
            self.assertEqual(mock_send.call_args.kwargs["parse_mode"], "HTML")

if __name__ == "__main__":
    unittest.main()
