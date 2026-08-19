"""Companion API + entitlement gate tests."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path


class CompanionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        root = Path(self._tmpdir.name)
        os.environ["SAAS_DATABASE_URL"] = f"sqlite:///{root / 'saas.db'}"
        os.environ["SAAS_UPLOAD_DIR"] = str(root / "uploads")
        os.environ["SAAS_SECRET_KEY"] = "test-secret"
        os.environ["SAAS_FORCE_SQLITE"] = "1"
        os.environ.pop("MONGO_URI", None)
        import jobsearch_saas.config as cfg
        import jobsearch_saas.db as dbmod

        cfg.DATABASE_URL = os.environ["SAAS_DATABASE_URL"]
        cfg.UPLOAD_DIR = Path(os.environ["SAAS_UPLOAD_DIR"])
        cfg.MONGO_URI = ""
        cfg.FORCE_SQLITE = True
        dbmod._initialized = False
        dbmod.init_db()

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_login_upload_quota_and_source(self) -> None:
        from jobsearch_saas.auth import create_user
        from jobsearch_saas import companion as companion_svc
        from jobsearch_saas.entitlements import active_plan, grant_beta_pass
        from jobsearch_saas.jobs.sources import assert_permitted
        from fastapi.testclient import TestClient
        from jobsearch_saas.api.app import app

        assert_permitted("linkedin_companion")
        user = create_user(email="comp@example.com", password="password123", full_name="Comp")
        token_payload = companion_svc.issue_companion_token(
            email="comp@example.com",
            password="password123",
            device_id="device-a",
            device_name="Test Mac",
        )
        self.assertIn("token", token_payload)
        plan = active_plan(user["id"])
        self.assertEqual(plan["companion_uploads_per_week"], 5)

        # Exhaust free companion uploads
        posts = [
            {
                "url": f"https://linkedin.com/feed/update/{i}",
                "post_text": f"Hiring Python developer in India apply jobs{i}@co.test",
                "author": "Recruiter",
                "keyword": "Python hiring",
            }
            for i in range(6)
        ]
        result = companion_svc.ingest_companion_posts(user["id"], posts)
        self.assertEqual(result["accepted"], 5)
        self.assertGreaterEqual(result["skipped"], 1)
        self.assertTrue(result["blocked_reason"])

        grant_beta_pass(user["id"])
        plan = active_plan(user["id"])
        self.assertGreater(plan["companion_uploads_remaining"], 50)

        client = TestClient(app)
        r = client.post(
            "/api/companion/login",
            json={
                "email": "comp@example.com",
                "password": "password123",
                "device_id": "device-b",
                "device_name": "Second",
            },
        )
        # Pro allows 2 devices — should work after beta grant
        self.assertEqual(r.status_code, 200, r.text)
        token = r.json()["token"]
        r = client.get("/api/companion/me", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["user"]["email"], "comp@example.com")

        r = client.post(
            "/api/companion/posts",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "posts": [
                    {
                        "url": "https://www.linkedin.com/feed/update/urn:li:activity:999",
                        "title": "Backend Engineer",
                        "company": "Acme",
                        "post_text": "Hiring Backend Engineer in Bengaluru. Email hr@acme.test",
                        "apply_email": "hr@acme.test",
                    }
                ]
            },
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["accepted"], 1)

        r = client.get("/download", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertTrue(r.headers["location"].startswith("/login"))

    def test_device_limit_on_free(self) -> None:
        from jobsearch_saas.auth import create_user
        from jobsearch_saas import companion as companion_svc

        create_user(email="lim@example.com", password="password123")
        companion_svc.issue_companion_token(
            email="lim@example.com",
            password="password123",
            device_id="d1",
            device_name="One",
        )
        with self.assertRaises(PermissionError):
            companion_svc.issue_companion_token(
                email="lim@example.com",
                password="password123",
                device_id="d2",
                device_name="Two",
            )


if __name__ == "__main__":
    unittest.main()
