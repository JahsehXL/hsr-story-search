import base64
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.app import create_app
import backend.feedback as feedback_module


TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


class FeedbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hsr-feedback-test-")
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_public_visitor_can_submit_feedback_with_image_and_admin_can_manage(self):
        with patch.dict("os.environ", {"HSR_ADMIN_CODE": "admin-secret-123456", "HSR_ACCESS_CODE": ""}):
            app = create_app(self.root, mode="server")
            with TestClient(app, base_url="https://127.0.0.1", client=("127.0.0.1", 50000)) as client:
                res = client.post(
                    "/api/v1/feedback",
                    data={
                        "category": "missing",
                        "content": "安灵布奠这段少了一句台词",
                        "context": '{"mission_id":"1021702","mission_name":"安灵布奠，天清路远","line_id":"abc"}',
                    },
                    files=[("files", ("shot.png", TINY_PNG, "image/png"))],
                    headers={"CF-Connecting-IP": "203.0.113.42"},
                )
                self.assertEqual(res.status_code, 200, res.text)
                body = res.json()
                self.assertTrue(body["ok"])
                fid = body["id"]
                self.assertEqual(body["attachment_count"], 1)

                # Unauthenticated visitor cannot read admin feedback list or attachments
                self.assertEqual(client.get("/api/v1/admin/feedback").status_code, 401)

                # Log in as admin
                login = client.post("/api/v1/auth/login", json={"code": "admin-secret-123456", "admin": True})
                self.assertEqual(login.status_code, 200)

                listed = client.get("/api/v1/admin/feedback").json()
                self.assertEqual(listed["total"], 1)
                self.assertEqual(listed["storage"]["pending_count"], 1)
                self.assertGreater(listed["storage"]["used_bytes"], len(TINY_PNG))
                item = listed["items"][0]
                self.assertEqual(item["id"], fid)
                self.assertEqual(item["category"], "missing")
                self.assertEqual(item["ip_masked"], "203.0.*.*")
                self.assertEqual(item["context"]["mission_id"], "1021702")
                self.assertEqual(len(item["attachments"]), 1)
                att_id = item["attachments"][0]["id"]

                # Fetch attachment image
                att_res = client.get(f"/api/v1/admin/feedback/{fid}/attachments/{att_id}")
                self.assertEqual(att_res.status_code, 200)
                self.assertEqual(att_res.content, TINY_PNG)
                self.assertIn("no-store", att_res.headers["Cache-Control"])

                # Mark resolved and clear resolved
                st_res = client.post(f"/api/v1/admin/feedback/{fid}/status", json={"status": "resolved"})
                self.assertEqual(st_res.status_code, 200)
                self.assertEqual(st_res.json()["storage"]["resolved_count"], 1)

                clr_res = client.post("/api/v1/admin/feedback/clear-resolved")
                self.assertEqual(clr_res.status_code, 200)
                self.assertEqual(clr_res.json()["deleted"], 1)
                self.assertEqual(clr_res.json()["storage"]["used_bytes"], 0)
                self.assertEqual(clr_res.json()["storage"]["total_count"], 0)

    def test_rate_limit_enforces_5_submissions_per_ip_per_hour(self):
        app = create_app(self.root, mode="local")
        with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
            for i in range(5):
                res = client.post(
                    "/api/v1/feedback",
                    json={"category": "other", "content": f"测试反馈 {i + 1}"},
                    headers={"CF-Connecting-IP": "198.51.100.9"},
                )
                self.assertEqual(res.status_code, 200, res.text)
            sixth = client.post(
                "/api/v1/feedback",
                json={"category": "other", "content": "第 6 次应该被限流"},
                headers={"CF-Connecting-IP": "198.51.100.9"},
            )
            self.assertEqual(sixth.status_code, 429)
            self.assertIn("5 次", sixth.json()["detail"])

            # Another IP is not blocked
            other_ip = client.post(
                "/api/v1/feedback",
                json={"category": "other", "content": "另一个 IP 正常发送"},
                headers={"CF-Connecting-IP": "198.51.100.10"},
            )
            self.assertEqual(other_ip.status_code, 200)

    def test_attachment_size_limit_5mb_and_total_storage_cap_1gb(self):
        app = create_app(self.root, mode="local")
        with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
            # Invalid non-image attachment
            bad_img = client.post(
                "/api/v1/feedback",
                data={"category": "display", "content": "伪装附件"},
                files=[("files", ("fake.png", b"not-an-image", "image/png"))],
            )
            self.assertEqual(bad_img.status_code, 400)
            self.assertIn("PNG", bad_img.json()["detail"])

            # Attachment exceeding 5 MB
            oversized = TINY_PNG + b"\x00" * (5 * 1024 * 1024)
            too_big = client.post(
                "/api/v1/feedback",
                data={"category": "display", "content": "超大图片"},
                files=[("files", ("huge.png", oversized, "image/png"))],
            )
            self.assertEqual(too_big.status_code, 400)
            self.assertIn("5 MB", too_big.json()["detail"])

            # Simulate storage cap reached
            with patch.object(feedback_module, "MAX_STORAGE_BYTES", 50):
                full_res = client.post(
                    "/api/v1/feedback",
                    json={
                        "category": "feature",
                        "content": "这是一段超过 50 字节模拟容量上限的反馈文本，用来验证 1GB 满容提示语。",
                        "attachments": [{"filename": "a.png", "data": base64.b64encode(TINY_PNG).decode()}],
                    },
                )
                self.assertEqual(full_res.status_code, 507)
                self.assertIn("目前反馈存储空间已满", full_res.json()["detail"])
                self.assertIn("联系作者", full_res.json()["detail"])


if __name__ == "__main__":
    unittest.main()
