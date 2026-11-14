import contextlib
import io
import logging
import os
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from pathlib import Path

DB_DIR = tempfile.TemporaryDirectory()
logging.disable(logging.CRITICAL)
os.environ["DATABASE_URL"] = "sqlite:///" + Path(DB_DIR.name, "test.db").as_posix()
os.environ["KITE_SECRET_KEY"] = "test-only-secret-key-with-at-least-32-characters"
os.environ["RUNNER_SHARED_SECRET"] = "test-runner-secret"

from fastapi.testclient import TestClient

from backend.app import (
    Activity, Base, CHALLENGES, LESSONS, LessonProgress, PlaygroundError, PlaygroundExecution,
    MODULES, QUIZZES, QUIZ_METADATA, QuizAttempt, SessionLocal, User, app, engine,
)
from backend.curriculum import NEW_CHALLENGE_DATA, NEW_QUIZ_DATA


class KiteApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.create_all(engine)
        cls.client_context = TestClient(app)
        cls.client = cls.client_context.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client_context.__exit__(None, None, None)
        Base.metadata.drop_all(engine)
        engine.dispose()
        DB_DIR.cleanup()

    def setUp(self):
        self.email = f"user-{uuid.uuid4().hex}@example.com"
        response = self.client.post("/api/auth/register", json={
            "name": "Kite Learner",
            "email": self.email,
            "password": "secure-example-password",
        })
        self.assertEqual(response.status_code, 201, response.text)
        self.token = response.json()["access_token"]
        self.headers = {"Authorization": "Bearer " + self.token}

    def test_registration_login_and_persisted_profile(self):
        response = self.client.get("/api/me", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["name"], "Kite Learner")
        login = self.client.post("/api/auth/login", json={
            "email": self.email,
            "password": "secure-example-password",
        })
        self.assertEqual(login.status_code, 200)
        self.assertEqual(login.json()["user"]["email"], self.email)

    def test_logout_revokes_local_access_token(self):
        response = self.client.post("/api/auth/logout", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get("/api/me", headers=self.headers).status_code, 401)

    def test_profile_email_password_and_settings_changes_persist(self):
        profile = self.client.patch("/api/me", headers=self.headers, json={"name": "Updated Kite Learner"})
        self.assertEqual(profile.status_code, 200, profile.text)
        self.assertEqual(profile.json()["name"], "Updated Kite Learner")

        email = f"updated-{uuid.uuid4().hex}@example.com"
        changed_email = self.client.patch(
            "/api/me/email", headers=self.headers,
            json={"email": email, "password": "secure-example-password"},
        )
        self.assertEqual(changed_email.status_code, 200, changed_email.text)
        self.assertEqual(changed_email.json()["email"], email)

        changed_password = self.client.patch(
            "/api/me/password", headers=self.headers,
            json={"current_password": "secure-example-password", "new_password": "another-secure-password"},
        )
        self.assertEqual(changed_password.status_code, 200, changed_password.text)
        self.assertEqual(self.client.post("/api/auth/login", json={
            "email": email, "password": "secure-example-password",
        }).status_code, 401)
        login = self.client.post("/api/auth/login", json={
            "email": email, "password": "another-secure-password",
        })
        self.assertEqual(login.status_code, 200, login.text)

        settings = self.client.patch(
            "/api/settings", headers=self.headers, json={"theme": "dark", "notifications": False},
        )
        self.assertEqual(settings.status_code, 200, settings.text)
        me = self.client.get("/api/me", headers=self.headers).json()
        self.assertEqual(me["settings"]["theme"], "dark")
        self.assertFalse(me["settings"]["notifications"])

    def test_notification_read_clear_and_ownership_flows(self):
        notifications = self.client.get("/api/notifications", headers=self.headers)
        self.assertEqual(notifications.status_code, 200, notifications.text)
        self.assertEqual(len(notifications.json()), 1)
        notice_id = notifications.json()[0]["id"]

        marked = self.client.post(f"/api/notifications/{notice_id}/read", headers=self.headers)
        self.assertEqual(marked.status_code, 200)
        self.assertTrue(self.client.get("/api/notifications", headers=self.headers).json()[0]["read"])
        self.assertEqual(self.client.post("/api/notifications/read-all", headers=self.headers).status_code, 200)
        self.assertEqual(self.client.delete("/api/notifications", headers=self.headers).status_code, 200)
        self.assertEqual(self.client.get("/api/notifications", headers=self.headers).json(), [])
        self.assertEqual(self.client.post("/api/notifications/999999/read", headers=self.headers).status_code, 404)

    def test_activity_notifications_are_real_deduplicated_and_preference_aware(self):
        complete_url = "/api/lessons/python-variables/complete"
        self.assertEqual(self.client.post(complete_url, headers=self.headers).status_code, 200)
        self.assertEqual(self.client.post(complete_url, headers=self.headers).status_code, 200)
        notices = self.client.get("/api/notifications", headers=self.headers).json()
        lesson_notices = [
            item for item in notices
            if item["title"] == "Lesson completed: " + LESSONS[0][1]
        ]
        self.assertEqual(len(lesson_notices), 1)
        self.assertEqual(lesson_notices[0]["category"], "learning")
        self.assertEqual(lesson_notices[0]["target_route"], "learn")
        self.assertEqual(
            self.client.get("/api/notifications/unread-count", headers=self.headers).json()["count"],
            len(notices),
        )

        self.client.patch("/api/settings", headers=self.headers, json={"notifications": False})
        self.assertEqual(
            self.client.post("/api/lessons/python-types/complete", headers=self.headers).status_code,
            200,
        )
        notices = self.client.get("/api/notifications", headers=self.headers).json()
        self.assertFalse(any(item["title"] == "Lesson completed: Data types" for item in notices))
        self.assertEqual(
            self.client.get("/api/notifications/unread-count", headers=self.headers).json()["count"],
            len(notices),
        )

    def test_progress_statistics_are_derived_from_persisted_records(self):
        initial = self.client.get("/api/progress", headers=self.headers).json()
        self.assertEqual(initial["total_lessons"], len(LESSONS))
        self.assertEqual(initial["completion_percent"], 0)
        self.assertEqual(initial["quiz_count"], 0)
        modules = self.client.get("/api/modules", headers=self.headers).json()
        self.assertEqual(
            initial["total_quizzes"],
            sum(module["quiz_count"] for module in modules if not module["locked"]),
        )
        self.assertEqual(initial["challenge_count"], 0)
        self.assertEqual(initial["total_challenges"], len(CHALLENGES))
        self.assertEqual(len(initial["weekly_activity"]), 7)
        self.assertEqual(initial["active_days_7"], 0)

        self.client.post("/api/lessons/python-variables/complete", headers=self.headers)
        result = self.client.get("/api/progress", headers=self.headers).json()
        self.assertEqual(result["completion_percent"], round(1 / len(LESSONS) * 100))
        self.assertEqual(result["xp"], 20)
        self.assertEqual(result["streak"], 1)
        self.assertEqual(result["active_days_7"], 1)
        self.assertEqual(sum(day["xp"] for day in result["weekly_activity"]), 20)

    def test_curriculum_has_twelve_modules_and_preserves_existing_lessons(self):
        response = self.client.get("/api/modules", headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        modules = response.json()
        self.assertEqual(len(modules), 12)
        self.assertEqual([module["order"] for module in modules], list(range(1, 13)))
        self.assertEqual(
            [module["lessons"][0]["id"] for module in modules[:6]],
            [row[0] for row in LESSONS[:6]],
        )
        self.assertTrue(all(module["lesson_count"] == 8 for module in modules[:11]))
        self.assertEqual(modules[11]["lesson_count"], 13)
        self.assertEqual(sum(module["lesson_count"] for module in modules), 101)
        self.assertTrue(all(module["quiz_count"] == 2 for module in modules[:11]))
        self.assertEqual(modules[11]["quiz_count"], 3)
        self.assertEqual([module["challenge_count"] for module in modules[:6]], [2, 3, 3, 4, 3, 3])
        self.assertTrue(all(module["challenge_count"] == 10 for module in modules[6:11]))
        self.assertEqual(modules[11]["challenge_count"], 12)
        lesson_ids = [
            lesson["id"] for module in modules for lesson in module["lessons"]
        ]
        self.assertEqual(len(lesson_ids), len(set(lesson_ids)))
        self.assertTrue(all(lesson["locked"] for module in modules[6:] for lesson in module["lessons"]))
        quizzes = self.client.get("/api/quizzes", headers=self.headers).json()
        challenges = self.client.get("/api/challenges", headers=self.headers).json()
        self.assertEqual(len(quizzes), len(QUIZZES))
        self.assertEqual(len(challenges), len(CHALLENGES))
        self.assertTrue(all(item["locked"] for item in quizzes if item["module_id"] == modules[6]["id"]))
        self.assertTrue(all(item["locked"] for item in challenges if item["module_id"] == modules[6]["id"]))

    def test_new_curriculum_examples_and_assessments_are_complete(self):
        required_sections = (
            "Learning objectives\n", "Core concept\n", "Why it matters\n", "How it works\n",
            "Syntax\n", "Worked example\n", "Step-by-step walkthrough\n", "Expected output\n",
            "Practical application\n", "Common mistakes\n", "Practice exercises\n", "Knowledge check\n",
        )
        for lesson in LESSONS:
            self.assertGreater(len(lesson[3]), 1800, lesson[0])
            for section in required_sections:
                self.assertIn(section, lesson[3], lesson[0])
            example = lesson[3].split("Worked example\n", 1)[1].split(
                "\n\nStep-by-step walkthrough\n", 1,
            )[0]
            output = io.StringIO()
            disabled_logging = logging.root.manager.disable
            logging.disable(logging.NOTSET)
            try:
                with contextlib.redirect_stdout(output):
                    exec(compile(example, lesson[0], "exec"), {})
            finally:
                logging.disable(disabled_logging)
            expected = lesson[3].split("\n\nExpected output\n", 1)[1].split(
                "\n\nUnderstand the result\n", 1,
            )[0]
            self.assertEqual(output.getvalue().rstrip("\n"), expected, lesson[0])
        for quiz in NEW_QUIZ_DATA.values():
            self.assertEqual(len(quiz["questions"]), 5)
            for question in quiz["questions"]:
                self.assertTrue(question["explanation"])
                self.assertIn(question["type"], {
                    "multiple_choice", "true_false", "output_prediction",
                    "code_interpretation", "debugging", "error_identification",
                })
                self.assertGreaterEqual(question["answer"], 0)
                self.assertLess(question["answer"], len(question["options"]))
        self.assertIn("true_false", {
            question.get("type", "multiple_choice")
            for questions in QUIZZES.values() for question in questions
        })
        self.assertIn("output_prediction", {
            question["type"] for quiz in NEW_QUIZ_DATA.values() for question in quiz["questions"]
        })
        for quiz in QUIZZES.values():
            self.assertTrue(all(question.get("explanation") for question in quiz))
        self.assertEqual(len(QUIZZES), 25)
        self.assertEqual(len(CHALLENGES), 80)
        for challenge in NEW_CHALLENGE_DATA.values():
            self.assertTrue(challenge["description"])
            self.assertTrue(challenge["hint"])
            self.assertGreaterEqual(len(challenge["tests"]), 1)

    def test_new_lesson_completion_updates_persisted_progress(self):
        with patch("backend.app.premium_entitled", return_value=True):
            completed = self.client.post(
                "/api/lessons/python-strings-literals/complete", headers=self.headers,
            )
        self.assertEqual(completed.status_code, 200, completed.text)
        progress = self.client.get("/api/progress", headers=self.headers).json()
        self.assertIn("python-strings-literals", progress["completed_lessons"])
        self.assertEqual(progress["xp"], 30)
        self.assertEqual(progress["total_lessons"], len(LESSONS))

    def test_new_quiz_submission_returns_explanations_and_persists_score(self):
        with patch("backend.app.premium_entitled", return_value=True):
            quiz = self.client.get("/api/quizzes/strings-reading", headers=self.headers)
            self.assertEqual(quiz.status_code, 200, quiz.text)
            self.assertEqual(len(quiz.json()["questions"]), 5)
            result = self.client.post(
                "/api/quizzes/strings-reading/submit",
                headers=self.headers,
                json={"answers": [0, 0, 1, 0, 1]},
            )
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["score"], 5)
        self.assertTrue(all(item["correct"] for item in result.json()["feedback"]))
        self.assertTrue(all(item["explanation"] for item in result.json()["feedback"]))
        with patch("backend.app.premium_entitled", return_value=True):
            progress = self.client.get("/api/progress", headers=self.headers).json()
            modules = self.client.get("/api/modules", headers=self.headers).json()
        self.assertEqual(progress["quiz_count"], 1)
        self.assertEqual(progress["quiz_attempt_count"], 1)
        self.assertEqual(progress["average_quiz_score"], 100)
        module = next(
            item for item in modules
            if item["id"] == next(
                quiz["module_id"] for quiz in QUIZ_METADATA if quiz["id"] == "strings-reading"
            )
        )
        self.assertEqual(module["completed_quizzes"], 1)
        self.assertEqual(module["quiz_attempt_count"], 1)
        self.assertEqual(module["average_quiz_score"], 100)
        strings_quiz = next(item for item in module["quizzes"] if item["id"] == "strings-reading")
        self.assertTrue(strings_quiz["completed"])
        self.assertEqual(strings_quiz["attempt_count"], 1)

    def test_guest_can_submit_free_quizzes_without_account_or_server_history(self):
        catalog = self.client.get("/api/quizzes")
        self.assertEqual(catalog.status_code, 200, catalog.text)
        quiz = self.client.get("/api/quizzes/python-basics")
        self.assertEqual(quiz.status_code, 200, quiz.text)
        self.assertIn("type", quiz.json()["questions"][0])
        self.assertNotIn("answer", quiz.json()["questions"][0])
        self.assertNotIn("explanation", quiz.json()["questions"][0])

        locked = self.client.get("/api/quizzes/advanced-iteration")
        self.assertEqual(locked.status_code, 402)
        locked_submit = self.client.post(
            "/api/quizzes/advanced-iteration/submit",
            json={"answers": [0, 0, 0, 0, 0]},
        )
        self.assertEqual(locked_submit.status_code, 402)

        answers = [0, 1, 1, 2, 1, 1, 2, 2, 1, 1]
        result = self.client.post("/api/quizzes/python-basics/submit", json={"answers": answers})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertTrue(result.json()["guest"])
        self.assertEqual(result.json()["score"], 10)
        self.assertEqual(result.json()["percentage"], 100)
        self.assertTrue(result.json()["passed"])
        self.assertEqual(result.json()["xp_awarded"], 0)
        self.assertIsNone(result.json()["total_xp"])
        after = self.client.get("/api/quizzes").json()
        basics = next(item for item in after if item["id"] == "python-basics")
        self.assertFalse(basics["completed"])
        self.assertEqual(basics["attempts"], [])

        invalid = self.client.post(
            "/api/quizzes/python-basics/submit",
            json={"answers": [99] + answers[1:]},
        )
        self.assertEqual(invalid.status_code, 400)

    def test_quiz_catalog_maps_topics_to_their_module_lessons_and_challenges(self):
        catalog = self.client.get("/api/quizzes").json()
        modules = self.client.get("/api/modules").json()
        challenges = self.client.get("/api/challenges").json()
        lessons_by_module = {
            module["id"]: {lesson["id"] for lesson in module["lessons"]}
            for module in modules
        }
        challenges_by_id = {challenge["id"]: challenge for challenge in challenges}
        self.assertEqual(len(catalog), len(QUIZ_METADATA))
        for quiz in catalog:
            self.assertTrue(quiz["topic"])
            self.assertTrue(quiz["difficulty"])
            if not quiz["locked"]:
                detail = self.client.get("/api/quizzes/" + quiz["id"])
                self.assertEqual(detail.status_code, 200, detail.text)
                self.assertEqual(quiz["question_count"], len(detail.json()["questions"]))
            self.assertTrue(set(quiz["lesson_ids"]).issubset(lessons_by_module[quiz["module_id"]]))
            challenge = challenges_by_id.get(quiz["practice_challenge_id"])
            self.assertIsNotNone(challenge)
            self.assertEqual(challenge["module_id"], quiz["module_id"])

    def test_quiz_attempt_history_is_persisted_per_account(self):
        answers = [0, 1, 1, 2, 1, 1, 2, 2, 1, 1]
        for _ in range(2):
            result = self.client.post(
                "/api/quizzes/python-basics/submit",
                headers=self.headers,
                json={"answers": answers},
            )
            self.assertEqual(result.status_code, 200, result.text)
        quiz = next(
            item for item in self.client.get("/api/quizzes", headers=self.headers).json()
            if item["id"] == "python-basics"
        )
        self.assertEqual(quiz["attempt_count"], 2)
        self.assertEqual(len(quiz["attempts"]), 2)
        self.assertEqual(quiz["best_score"], 10)
        with SessionLocal() as db:
            user = db.query(User).filter(User.email == self.email).one()
            self.assertEqual(
                db.query(QuizAttempt).filter(
                    QuizAttempt.user_id == user.id,
                    QuizAttempt.quiz_id == "python-basics",
                ).count(),
                2,
            )

        other_email = f"other-{uuid.uuid4().hex}@example.com"
        other = self.client.post("/api/auth/register", json={
            "name": "Other Learner",
            "email": other_email,
            "password": "secure-example-password",
        })
        other_headers = {"Authorization": "Bearer " + other.json()["access_token"]}
        other_quiz = next(
            item for item in self.client.get("/api/quizzes", headers=other_headers).json()
            if item["id"] == "python-basics"
        )
        self.assertEqual(other_quiz["attempt_count"], 0)
        self.assertEqual(other_quiz["attempts"], [])

    def test_quiz_submission_idempotency_replays_saved_result_without_duplicate_attempt(self):
        answers = [0, 1, 1, 2, 1, 1, 2, 2, 1, 1]
        payload = {"answers": answers, "submission_id": uuid.uuid4().hex}
        first = self.client.post(
            "/api/quizzes/python-basics/submit", headers=self.headers, json=payload,
        )
        replay = self.client.post(
            "/api/quizzes/python-basics/submit", headers=self.headers, json=payload,
        )
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(replay.json(), first.json())
        self.assertEqual(first.json()["attempt_number"], 1)
        self.assertEqual(first.json()["submission_id"], payload["submission_id"])

        changed_answers = self.client.post(
            "/api/quizzes/python-basics/submit",
            headers=self.headers,
            json={"answers": [-1] + answers[1:], "submission_id": payload["submission_id"]},
        )
        self.assertEqual(changed_answers.status_code, 409)

        quiz = next(
            item for item in self.client.get("/api/quizzes", headers=self.headers).json()
            if item["id"] == "python-basics"
        )
        self.assertEqual(quiz["attempt_count"], 1)
        notices = [
            item for item in self.client.get("/api/notifications", headers=self.headers).json()
            if item["category"] == "learning" and item["target_route"] == "quiz"
        ]
        self.assertEqual(len(notices), 1)

    def test_new_challenge_uses_runner_tests_and_awards_xp_once(self):
        with patch("backend.app.premium_entitled", return_value=True), \
                patch("backend.app.call_runner", new_callable=AsyncMock) as run:
            run.side_effect = [
                {"stdout": "Kite learns Python\n", "stderr": "", "error": None},
                {"stdout": "one two\n", "stderr": "", "error": None},
            ]
            result = self.client.post(
                "/api/challenges/python-strings-normalize-space/submit",
                headers=self.headers,
                json={"code": 'print(" ".join(text.split()))'},
            )
        self.assertEqual(result.status_code, 200, result.text)
        self.assertTrue(result.json()["passed"])
        self.assertEqual(result.json()["xp_awarded"], 100)
        self.assertEqual(run.await_count, 2)

    def test_leaderboard_periods_and_invalid_period(self):
        for period in ("week", "month", "all"):
            result = self.client.get(f"/api/leaderboard?period={period}", headers=self.headers)
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json()["period"], period)
            self.assertIn("rank", result.json()["current"])
        invalid = self.client.get("/api/leaderboard?period=year", headers=self.headers)
        self.assertEqual(invalid.status_code, 400)

    def test_python_pro_and_top_ten_achievements_use_saved_activity(self):
        with SessionLocal() as db:
            user = db.query(User).filter(User.email == self.email).one()
            for lesson in LESSONS:
                db.add(LessonProgress(user_id=user.id, lesson_id=lesson[0], completed=True, score=100))
            for challenge_id in CHALLENGES:
                db.add(Activity(
                    user_id=user.id, kind="challenge", item_id=challenge_id, xp=10000,
                ))
            db.commit()
        result = self.client.get("/api/progress", headers=self.headers)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertIn("Python Pro", result.json()["achievements"])
        self.assertIn("Top 10", result.json()["achievements"])

    def test_bug_hunter_tracks_resolved_playground_errors(self):
        error_result = {"stdout": "", "stderr": "Traceback", "error": "Code exited with an error."}
        success_result = {"stdout": "2\n", "stderr": "", "error": None}
        with patch("backend.app.call_runner", new_callable=AsyncMock) as run:
            run.side_effect = [result for _ in range(10) for result in (error_result, success_result)]
            for _ in range(10):
                failed = self.client.post("/api/execution/run", headers=self.headers, json={"code": "bad code"})
                fixed = self.client.post("/api/execution/run", headers=self.headers, json={"code": "print(2)"})
                self.assertEqual(failed.status_code, 200, failed.text)
                self.assertEqual(fixed.status_code, 200, fixed.text)
        with SessionLocal() as db:
            fixed_count = db.query(PlaygroundError).filter(
                PlaygroundError.user_id == self.client.get("/api/me", headers=self.headers).json()["id"],
                PlaygroundError.fixed_at.is_not(None),
            ).count()
        self.assertEqual(fixed_count, 10)
        progress = self.client.get("/api/progress", headers=self.headers).json()
        self.assertIn("Bug Hunter", progress["achievements"])

    def test_subscription_verification_and_refresh_update_entitlement(self):
        product_id = "kite_premium_monthly"
        purchase_token = "test-google-play-purchase-token"
        expires_at = datetime.now(timezone.utc) + timedelta(days=30)
        active = {
            "state": "SUBSCRIPTION_STATE_ACTIVE",
            "expires_at": expires_at,
            "auto_renew": True,
        }
        with patch.dict(os.environ, {"KITE_PLAY_MONTHLY_PRODUCT_ID": product_id}), \
                patch("backend.app.fetch_google_purchase", return_value=active) as verify:
            response = self.client.post(
                "/api/subscription/google/verify",
                headers=self.headers,
                json={"product_id": product_id, "purchase_token": purchase_token},
            )
            duplicate = self.client.post(
                "/api/subscription/google/verify",
                headers=self.headers,
                json={"product_id": product_id, "purchase_token": purchase_token},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(duplicate.status_code, 200, duplicate.text)
        self.assertTrue(response.json()["premium"])
        self.assertEqual(verify.call_count, 2)
        verify.assert_called_with(product_id, purchase_token)
        confirmed = [
            item for item in self.client.get("/api/notifications", headers=self.headers).json()
            if item["category"] == "subscription"
        ]
        self.assertEqual(len(confirmed), 1)
        self.assertTrue(self.client.get("/api/subscription", headers=self.headers).json()["premium"])
        lessons = {lesson["id"]: lesson for lesson in self.client.get("/api/lessons", headers=self.headers).json()}
        self.assertFalse(lessons["python-loops"]["locked"])
        unlocked_quizzes = {
            item["id"]: item for item in self.client.get("/api/quizzes", headers=self.headers).json()
        }
        self.assertFalse(unlocked_quizzes["strings-reading"]["locked"])
        self.assertEqual(
            self.client.get("/api/quizzes/strings-reading", headers=self.headers).status_code, 200,
        )

        expired = {
            "state": "SUBSCRIPTION_STATE_EXPIRED",
            "expires_at": datetime.now(timezone.utc) - timedelta(days=1),
            "auto_renew": False,
        }
        with patch.dict(os.environ, {"KITE_PLAY_MONTHLY_PRODUCT_ID": product_id}), \
                patch("backend.app.fetch_google_purchase", return_value=expired):
            refreshed = self.client.post("/api/subscription/refresh", headers=self.headers)
        self.assertEqual(refreshed.status_code, 200, refreshed.text)
        self.assertFalse(refreshed.json()["premium"])
        relocked_quizzes = {
            item["id"]: item for item in self.client.get("/api/quizzes", headers=self.headers).json()
        }
        self.assertTrue(relocked_quizzes["strings-reading"]["locked"])
        self.assertEqual(
            self.client.get("/api/quizzes/strings-reading", headers=self.headers).status_code, 402,
        )
        self.assertTrue(any(
            item["title"] == "Kite Premium has expired"
            for item in self.client.get("/api/notifications", headers=self.headers).json()
        ))

    def test_registration_rejects_password_shorter_than_eight_characters(self):
        response = self.client.post("/api/auth/register", json={
            "name": "Short Password",
            "email": f"short-{uuid.uuid4().hex}@example.com",
            "password": "short7",
        })
        self.assertEqual(response.status_code, 422)

    def test_registration_rate_limit_returns_retry_after(self):
        email = f"limited-{uuid.uuid4().hex}@example.com"
        payload = {"name": "Rate Limited", "email": email, "password": "secure-example-password"}
        for _ in range(5):
            self.client.post("/api/auth/register", json=payload)
        limited = self.client.post("/api/auth/register", json=payload)
        self.assertEqual(limited.status_code, 429, limited.text)
        self.assertGreater(int(limited.headers["Retry-After"]), 0)

    def test_unverified_supabase_registration_does_not_create_local_account(self):
        email = f"verify-{uuid.uuid4().hex}@example.com"
        provider_user = {"id": "unverified-provider-user", "email": email}
        with patch.dict(os.environ, {
            "SUPABASE_URL": "https://kite.example",
            "SUPABASE_ANON_KEY": "test-anon-key",
        }), patch("backend.app.supabase_request", return_value={"user": provider_user}):
            response = self.client.post("/api/auth/register", json={
                "name": "Needs Verification",
                "email": email,
                "password": "secure-example-password",
            })
        self.assertEqual(response.status_code, 201, response.text)
        self.assertNotIn("access_token", response.json())
        with SessionLocal() as db:
            self.assertIsNone(db.query(User).filter_by(email=email).one_or_none())

    def test_unverified_provider_session_cannot_be_linked(self):
        email = f"unverified-{uuid.uuid4().hex}@example.com"
        with patch.dict(os.environ, {
            "SUPABASE_URL": "https://kite.example",
            "SUPABASE_ANON_KEY": "test-anon-key",
        }), patch("backend.app.supabase_request", return_value={"id": "unverified-session", "email": email}):
            response = self.client.post("/api/auth/session", json={"access_token": "provider-token"})
        self.assertEqual(response.status_code, 403, response.text)
        with SessionLocal() as db:
            self.assertIsNone(db.query(User).filter_by(email=email).one_or_none())

    def test_verified_provider_session_links_once_without_replacing_identity(self):
        env = {
            "SUPABASE_URL": "https://kite.example",
            "SUPABASE_ANON_KEY": "test-anon-key",
        }
        verified_user = {
            "id": "verified-provider-user",
            "email": self.email,
            "email_confirmed_at": "2025-01-01T00:00:00Z",
            "user_metadata": {"name": "Kite Learner"},
        }
        with patch.dict(os.environ, env), patch(
            "backend.app.supabase_request", return_value=verified_user,
        ):
            linked = self.client.post("/api/auth/session", json={"access_token": "provider-token"})
        self.assertEqual(linked.status_code, 200, linked.text)
        self.assertEqual(linked.json()["access_token"], "provider-token")

        conflicting_user = dict(verified_user, id="different-provider-user")
        with patch.dict(os.environ, env), patch(
            "backend.app.supabase_request", return_value=conflicting_user,
        ):
            conflict = self.client.post("/api/auth/session", json={"access_token": "other-provider-token"})
        self.assertEqual(conflict.status_code, 409, conflict.text)
        self.assertNotIn(self.email, conflict.text)

    def test_verification_resend_uses_provider_and_generic_response(self):
        env = {
            "SUPABASE_URL": "https://kite.example",
            "SUPABASE_ANON_KEY": "test-anon-key",
            "PUBLIC_APP_URL": "https://kite.example.app",
        }
        with patch.dict(os.environ, env), patch("backend.app.supabase_request", return_value={}) as request:
            response = self.client.post("/api/auth/resend-verification", json={"email": self.email})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.json()["message"],
            "If the address needs verification, an email will be sent shortly.",
        )
        self.assertEqual(request.call_args.args[0], "/resend?redirect_to=https%3A%2F%2Fkite.example.app%2F")
        self.assertEqual(request.call_args.kwargs["data"]["type"], "signup")

    def test_local_password_recovery_fails_closed_without_smtp(self):
        with patch.dict(os.environ, {
            "SUPABASE_URL": "",
            "SUPABASE_ANON_KEY": "",
            "SMTP_HOST": "",
        }):
            response = self.client.post("/api/auth/password-reset", json={"email": self.email})
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(self.email, response.text)

    def test_local_password_reset_token_is_one_time_and_changes_password(self):
        env = {
            "SUPABASE_URL": "",
            "SUPABASE_ANON_KEY": "",
            "SMTP_HOST": "smtp.example",
        }
        with patch.dict(os.environ, env), patch("backend.app.send_reset_email") as send_email:
            requested = self.client.post("/api/auth/password-reset", json={"email": self.email})
            self.assertEqual(requested.status_code, 200, requested.text)
            reset_token = send_email.call_args.args[1]
            new_password = "different-secure-Password-92!"
            completed = self.client.post("/api/auth/password-reset/complete", json={
                "token": reset_token,
                "password": new_password,
            })
            self.assertEqual(completed.status_code, 200, completed.text)
            reused = self.client.post("/api/auth/password-reset/complete", json={
                "token": reset_token,
                "password": new_password,
            })
            self.assertEqual(reused.status_code, 400)
            old_login = self.client.post("/api/auth/login", json={
                "email": self.email,
                "password": "secure-example-password",
            })
            new_login = self.client.post("/api/auth/login", json={
                "email": self.email,
                "password": new_password,
            })
        self.assertEqual(old_login.status_code, 401)
        self.assertEqual(new_login.status_code, 200, new_login.text)

    def test_lesson_completion_persists_and_awards_xp_once(self):
        first = self.client.post("/api/lessons/python-variables/complete", headers=self.headers)
        second = self.client.post("/api/lessons/python-variables/complete", headers=self.headers)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(first.json()["xp"], 20)
        self.assertEqual(second.json()["xp"], 20)
        progress = self.client.get("/api/progress", headers=self.headers).json()
        self.assertIn("python-variables", progress["completed_lessons"])

    def test_guest_lesson_progress_import_is_free_only_and_idempotent(self):
        payload = {
            "started_lessons": ["python-types"],
            "completed_lessons": ["python-variables"],
        }
        imported = self.client.post("/api/progress/import", headers=self.headers, json=payload)
        self.assertEqual(imported.status_code, 200, imported.text)
        self.assertEqual(imported.json()["imported_started"], 1)
        self.assertEqual(imported.json()["imported_completed"], 1)
        self.assertEqual(imported.json()["xp_awarded"], 20)

        lessons = {lesson["id"]: lesson for lesson in self.client.get(
            "/api/lessons", headers=self.headers,
        ).json()}
        self.assertTrue(lessons["python-variables"]["completed"])
        self.assertTrue(lessons["python-types"]["started"])
        progress = self.client.get("/api/progress", headers=self.headers).json()
        self.assertIn("python-variables", progress["completed_lessons"])
        self.assertIn("python-types", progress["started_lessons"])

        repeated = self.client.post("/api/progress/import", headers=self.headers, json=payload)
        self.assertEqual(repeated.status_code, 200, repeated.text)
        self.assertEqual(repeated.json()["imported_started"], 0)
        self.assertEqual(repeated.json()["imported_completed"], 0)
        self.assertEqual(repeated.json()["xp_awarded"], 0)

        premium = self.client.post(
            "/api/progress/import", headers=self.headers,
            json={"completed_lessons": ["python-loops"]},
        )
        self.assertEqual(premium.status_code, 402)

    def test_guest_progress_import_requires_authentication(self):
        response = self.client.post(
            "/api/progress/import",
            json={"completed_lessons": ["python-variables"]},
        )
        self.assertEqual(response.status_code, 401)

    def test_premium_content_is_locked_for_free_account(self):
        listing = self.client.get("/api/lessons", headers=self.headers)
        premium = next(item for item in listing.json() if item["id"] == "python-loops")
        self.assertTrue(premium["locked"])
        locked = self.client.get("/api/lessons/python-loops", headers=self.headers)
        self.assertEqual(locked.status_code, 402)

    def test_authenticated_runner_is_unavailable_without_runner_service(self):
        response = self.client.post("/api/execution/run", headers=self.headers, json={"code": "print('hi')"})
        self.assertEqual(response.status_code, 503)

    def test_execution_cancellation_is_limited_to_the_owning_user(self):
        other_email = f"other-{uuid.uuid4().hex}@example.com"
        other = self.client.post("/api/auth/register", json={
            "name": "Other Learner",
            "email": other_email,
            "password": "secure-example-password",
        })
        self.assertEqual(other.status_code, 201, other.text)
        execution_id = uuid.uuid4().hex
        other_id = other.json()["user"]["id"]
        with SessionLocal() as db:
            db.add(PlaygroundExecution(execution_id=execution_id, user_id=other_id))
            db.commit()
        with patch("backend.app.cancel_runner_execution", new_callable=AsyncMock) as cancel:
            cancel.return_value = {"cancelled": True}
            rejected = self.client.post(
                "/api/execution/cancel",
                headers=self.headers,
                json={"execution_id": execution_id},
            )
            accepted = self.client.post(
                "/api/execution/cancel",
                headers={"Authorization": "Bearer " + other.json()["access_token"]},
                json={"execution_id": execution_id},
            )
        self.assertEqual(rejected.status_code, 404)
        self.assertEqual(accepted.status_code, 200, accepted.text)
        cancel.assert_awaited_once_with(execution_id)
        with SessionLocal() as db:
            db.delete(db.get(PlaygroundExecution, execution_id))
            db.commit()

    def test_cancelled_execution_does_not_count_as_a_playground_error(self):
        execution_id = uuid.uuid4().hex
        with patch("backend.app.call_runner", new_callable=AsyncMock) as run:
            run.return_value = {"stdout": "", "stderr": "", "error": "Execution cancelled."}
            response = self.client.post(
                "/api/execution/run",
                headers=self.headers,
                json={"code": "while True: pass", "execution_id": execution_id},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["error"], "Execution cancelled.")
        run.assert_awaited_once_with("while True: pass", execution_id=execution_id)
        with SessionLocal() as db:
            self.assertIsNone(db.get(PlaygroundExecution, execution_id))
            self.assertEqual(db.query(PlaygroundError).filter(
                PlaygroundError.user_id == self.client.get("/api/me", headers=self.headers).json()["id"],
                PlaygroundError.fixed_at.is_(None),
            ).count(), 0)

    def test_unauthenticated_progress_is_rejected(self):
        response = self.client.get("/api/progress")
        self.assertEqual(response.status_code, 401)

    def test_guest_progress_inputs_are_public_without_exposing_account_progress(self):
        modules = self.client.get("/api/modules")
        self.assertEqual(modules.status_code, 200, modules.text)
        self.assertEqual(len(modules.json()), 12)

        lesson = self.client.get("/api/lessons/python-variables")
        self.assertEqual(lesson.status_code, 200, lesson.text)
        self.assertEqual(lesson.json()["xp_reward"], 20)
        self.assertEqual(self.client.get("/api/progress").status_code, 401)

    def test_subscription_plans_show_requested_prices_and_annual_value(self):
        with patch.dict(os.environ, {}, clear=True):
            response = self.client.get("/api/plans")
        self.assertEqual(response.status_code, 200)
        plans = {plan["id"]: plan for plan in response.json()["plans"]}
        self.assertEqual(plans["premium_monthly"]["price"], "K19.99")
        self.assertEqual(plans["premium_monthly"]["period"], "month")
        self.assertEqual(plans["premium_monthly"]["product_id"], "kite_premium_monthly")
        self.assertEqual(plans["premium_annual"]["price"], "K99.99")
        self.assertEqual(plans["premium_annual"]["period"], "year")
        self.assertEqual(plans["premium_annual"]["product_id"], "kite_premium_annual")
        self.assertEqual(plans["premium_annual"]["savings"], "K139.89 annually")
        self.assertEqual(plans["premium_annual"]["percentage_savings"], "58.3%")
        self.assertTrue(plans["premium_annual"]["best_value"])

    def test_google_sign_in_requires_supabase_configuration(self):
        with patch.dict(os.environ, {}, clear=True):
            response = self.client.get("/api/auth/google/authorize", params={"code_challenge": "c" * 43})
        self.assertEqual(response.status_code, 503)

    def test_google_sign_in_uses_pkce_authorization_and_exchange(self):
        env = {
            "SUPABASE_URL": "https://kite.example",
            "SUPABASE_ANON_KEY": "test-anon-key",
            "PUBLIC_APP_URL": "https://kite.example.app",
        }
        with patch.dict(os.environ, env), patch("backend.app.supabase_request") as auth_request:
            authorize = self.client.get("/api/auth/google/authorize", params={"code_challenge": "c" * 43})
            auth_request.return_value = {
                "access_token": "google-access",
                "refresh_token": "google-refresh",
                "user": {
                    "id": "google-user-id",
                    "email": self.email,
                    "email_confirmed_at": "2025-01-01T00:00:00Z",
                    "user_metadata": {"name": "Kite Learner"},
                },
            }
            exchange = self.client.post("/api/auth/google/exchange", json={
                "code": "authorization-code",
                "code_verifier": "v" * 43,
            })
        self.assertEqual(authorize.status_code, 200, authorize.text)
        authorization_url = authorize.json()["authorization_url"]
        self.assertIn("provider=google", authorization_url)
        self.assertIn("code_challenge_method=s256", authorization_url)
        self.assertEqual(exchange.status_code, 200, exchange.text)
        self.assertEqual(exchange.json()["access_token"], "google-access")
        self.assertEqual(auth_request.call_args.args[0], "/token?grant_type=pkce")
        self.assertEqual(auth_request.call_args.kwargs["data"]["code_verifier"], "v" * 43)

    def test_quiz_xp_is_awarded_only_once(self):
        questions = self.client.get("/api/quizzes/python-basics", headers=self.headers).json()["questions"]
        self.assertEqual(len(questions), 10)
        incomplete = self.client.post("/api/quizzes/python-basics/submit", headers=self.headers,
                                      json={"answers": [0, 1, 1, 2, 1, 1, 2, 2, 1]})
        self.assertEqual(incomplete.status_code, 400)
        answers = [0, 1, 1, 2, 1, 1, 2, 2, 1, 1]
        first = self.client.post("/api/quizzes/python-basics/submit", headers=self.headers,
                                 json={"answers": answers})
        second = self.client.post("/api/quizzes/python-basics/submit", headers=self.headers,
                                  json={"answers": answers})
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["score"], 10)
        self.assertEqual(first.json()["xp_awarded"], 50)
        self.assertEqual(second.json()["xp_awarded"], 0)
        quiz_notices = [
            item for item in self.client.get("/api/notifications", headers=self.headers).json()
            if item["category"] == "learning" and item["target_route"] == "quiz"
        ]
        self.assertEqual(len(quiz_notices), 2)

    def test_quiz_best_score_updates_on_retry_without_extra_xp(self):
        correct = [0, 1, 1, 2, 1, 1, 2, 2, 1, 1]
        partial = [-1, -1, -1] + correct[3:]
        first = self.client.post(
            "/api/quizzes/python-basics/submit", headers=self.headers, json={"answers": partial},
        )
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["score"], 7)
        self.assertIsNone(first.json()["feedback"][0]["your_answer"])
        self.assertFalse(first.json()["feedback"][0]["correct"])
        self.assertNotIn(
            "Quiz Master", self.client.get("/api/progress", headers=self.headers).json()["achievements"],
        )

        improved = self.client.post(
            "/api/quizzes/python-basics/submit", headers=self.headers, json={"answers": correct},
        )
        self.assertEqual(improved.status_code, 200, improved.text)
        self.assertEqual(improved.json()["score"], 10)
        self.assertEqual(improved.json()["xp_awarded"], 0)
        self.assertIn("Quiz Master", self.client.get("/api/progress", headers=self.headers).json()["achievements"])

    def test_challenge_is_server_checked_and_xp_is_idempotent(self):
        with patch("backend.app.call_runner", new_callable=AsyncMock) as run:
            run.return_value = {"stdout": "Hello, World!\n", "stderr": "", "error": None}
            first = self.client.post("/api/challenges/hello-world/submit", headers=self.headers,
                                     json={"code": 'print("Hello, World!")'})
            second = self.client.post("/api/challenges/hello-world/submit", headers=self.headers,
                                      json={"code": 'print("Hello, World!")'})
        self.assertEqual(first.status_code, 200, first.text)
        self.assertTrue(first.json()["passed"])
        self.assertEqual(first.json()["xp_awarded"], 100)
        self.assertEqual(second.json()["xp_awarded"], 0)


if __name__ == "__main__":
    unittest.main()
