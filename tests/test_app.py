import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import app


class SkinTrackTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        app.DB_PATH = Path(self.temp_dir.name) / "test.db"
        app.initialize_database()
        self.user_a = SimpleNamespace(username="alice")
        self.user_b = SimpleNamespace(username="bob")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_missing_photo_and_consent_do_not_call_api(self):
        with patch.object(app.requests, "post") as post:
            self.assertIn("consent", app.analyze_image(None, False)[0].lower())
            self.assertIn("upload", app.analyze_image(None, True)[0].lower())
            post.assert_not_called()

    def test_missing_api_key_is_explicit(self):
        from PIL import Image

        image = Path(self.temp_dir.name) / "face.png"
        Image.new("RGB", (2, 2)).save(image)
        with patch.dict(os.environ, {"HKBU_API_KEY": ""}):
            status, cleanliness, _, _, parsed = app.analyze_image(str(image), True)
        self.assertIn("Enter your HKBU API key", status)
        self.assertEqual(cleanliness, "—")
        self.assertIsNone(parsed)

    def test_api_key_can_be_supplied_for_one_request(self):
        with patch.dict(os.environ, {"HKBU_API_KEY": ""}):
            key, url = app.get_api_config("  session-only-key  ")
        self.assertEqual(key, "session-only-key")
        self.assertEqual(url, app.API_URL_DEFAULT)

    def test_valid_response_and_missing_score(self):
        response = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "skin_cleanliness": 7,
                                "skin_quality": None,
                                "description": "Some dryness.",
                                "estimated_age": "Unable to read",
                            }
                        )
                    }
                }
            ]
        }
        parsed = app.parse_analysis_response(response)
        self.assertEqual(parsed["cleanliness"], 7.0)
        self.assertIsNone(parsed["quality"])

    def test_image_type_and_size_are_checked(self):
        invalid_image = Path(self.temp_dir.name) / "face.txt"
        invalid_image.write_text("not an image")
        with self.assertRaises(app.SkinTrackError):
            app.validate_image(str(invalid_image))

        from PIL import Image

        image = Path(self.temp_dir.name) / "face.png"
        Image.new("RGB", (2, 2)).save(image)
        with patch.object(app, "MAX_IMAGE_BYTES", 1):
            with self.assertRaisesRegex(app.SkinTrackError, "8 MB"):
                app.validate_image(str(image))

    def test_successful_analysis_sends_multimodal_payload(self):
        from PIL import Image

        image = Path(self.temp_dir.name) / "face.png"
        Image.new("RGB", (2, 2)).save(image)
        api_response = Mock(
            status_code=200,
            json=lambda: {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "skin_cleanliness": 8,
                                    "skin_quality": 6.5,
                                    "description": "A cautious observation.",
                                    "estimated_age": "Unable to read",
                                }
                            )
                        }
                    }
                ]
            },
        )
        with patch.dict(os.environ, {"HKBU_API_KEY": ""}):
            with patch.object(app.requests, "post", return_value=api_response) as post:
                result = app.analyze_image(str(image), True, "test-key")
        self.assertIn("complete", result[0].lower())
        self.assertEqual(result[1], "8 / 10")
        self.assertEqual(result[2], "6.5 / 10")
        request_args = post.call_args.kwargs
        self.assertEqual(request_args["timeout"], app.API_TIMEOUT_SECONDS)
        self.assertEqual(request_args["headers"]["Authorization"], "Bearer test-key")
        self.assertEqual(request_args["json"]["model"], "gemini-2.5-pro")
        self.assertTrue(
            request_args["json"]["messages"][0]["content"][1]["image_url"]["url"].startswith(
                "data:image/png;base64,"
            )
        )

    def test_invalid_score_ranges_and_types_are_rejected(self):
        for score in (11, -1, "7", True):
            with self.subTest(score=score), self.assertRaises(app.SkinTrackError):
                app._score(score, "Skin Quality")

    def test_response_shape_errors_are_rejected(self):
        with self.assertRaises(app.SkinTrackError):
            app.parse_analysis_response({"choices": []})
        with self.assertRaises(app.SkinTrackError):
            app.parse_analysis_response(
                {"choices": [{"message": {"content": "not json"}}]}
            )

    def test_http_error_and_timeout_are_user_facing(self):
        image = Path(self.temp_dir.name) / "face.png"
        from PIL import Image

        Image.new("RGB", (2, 2)).save(image)
        with patch.dict(os.environ, {"HKBU_API_KEY": "test-key"}):
            with patch.object(app.requests, "post", side_effect=app.requests.Timeout):
                result = app.analyze_image(str(image), True)
                self.assertIn("timed out", result[0].lower())
            failed_response = Mock(status_code=503)
            with patch.object(app.requests, "post", return_value=failed_response):
                result = app.analyze_image(str(image), True)
                self.assertIn("HTTP 503", result[0])
                self.assertIsNone(result[4])

    def test_two_user_history_isolation_and_scoped_delete(self):
        analysis_a = {
            "cleanliness": 6.0,
            "quality": 7.5,
            "description": "A profile's observation",
            "estimated_age": "30",
        }
        analysis_b = {
            "cleanliness": 2.0,
            "quality": 3.0,
            "description": "B profile's observation",
            "estimated_age": "40",
        }
        self.assertIn("saved", app.save_analysis(analysis_a, self.user_a).lower())
        self.assertIn("saved", app.save_analysis(analysis_b, self.user_b).lower())
        a_history = app.get_history("alice")
        b_history = app.get_history("bob")
        self.assertEqual(len(a_history), 1)
        self.assertEqual(len(b_history), 1)
        self.assertEqual(a_history.iloc[0]["Description"], "A profile's observation")

        a_record_id = int(a_history.iloc[0]["ID"])
        status, refreshed, _, _ = app.delete_history(a_record_id, self.user_b)
        self.assertIn("not found", status.lower())
        self.assertEqual(len(app.get_history("alice")), 1)
        status, _, _, _ = app.delete_history(a_record_id, self.user_a)
        self.assertEqual(status, "Record deleted.")
        self.assertTrue(app.get_history("alice").empty)
        self.assertEqual(len(app.get_history("bob")), 1)

    def test_profile_persists_and_changes_recommendation(self):
        app.save_profile("Dry", "dryness", "HKD 300", "fragrance", self.user_a)
        self.assertEqual(app._profile_values("alice")[0], "Dry")
        morning, evening = app.refresh_routine(self.user_a)
        self.assertIn("fragrance-free moisturizer", morning)
        self.assertIn("HKD 300", evening)
        changed = app.build_routine("Oily", "breakouts", "", "")
        self.assertIn("lightweight, non-comedogenic", changed[0])
        self.assertNotIn("fragrance-free moisturizer", changed[0])

    def test_repeated_save_clicks_create_consistent_distinct_records(self):
        analysis = {
            "cleanliness": 6.0,
            "quality": 7.0,
            "description": "Same analysis",
            "estimated_age": "Unable to read",
        }
        app.save_analysis(analysis, self.user_a)
        app.save_analysis(analysis, self.user_a)
        self.assertEqual(len(app.get_history("alice")), 2)

    def test_empty_and_single_score_history_render(self):
        empty, figure, note = app.history_view(self.user_a)
        self.assertTrue(empty.empty)
        self.assertIn("Save an analysis", note)
        app.save_analysis(
            {"cleanliness": None, "quality": 8.0, "description": "Observation", "estimated_age": "Unable to read"},
            self.user_a,
        )
        history, figure, note = app.history_view(self.user_a)
        self.assertEqual(len(history), 1)
        self.assertEqual(len(figure.data), 2)
        self.assertIn("cannot establish", note)


if __name__ == "__main__":
    unittest.main()
