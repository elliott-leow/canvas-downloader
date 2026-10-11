import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

import requests

from canvas_sync import AuthenticationExpired, Sync, course_catalog, select_courses, enrollment_roles


def response(data, code=200, next_url=None):
    result = requests.Response()
    result.status_code = code
    result._content = json.dumps(data).encode()
    if next_url:
        result.headers["Link"] = f'<{next_url}>; rel="next"'
    return result


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.session = Mock()
        self.sync = Sync(self.session, "https://canvas.example", self.root)

    def test_pagination_and_host_boundary(self):
        self.session.get.side_effect = [response([{"id": 1}], next_url="https://canvas.example/page2"),
                                        response([{"id": 2}])]
        self.assertEqual([x["id"] for x in self.sync.listing("/api/v1/courses")], [1, 2])
        self.session.get.side_effect = None
        self.session.get.return_value = response([], next_url="https://elsewhere.example/courses")
        with self.assertRaises(ValueError):
            self.sync.listing("/api/v1/courses")

    def test_past_courses_default_hidden_and_user_override(self):
        current = {"id": 1, "name": "Current", "term": {"name": "Current term", "end_at": "2099-01-01T00:00:00Z"}}
        past = {"id": 2, "name": "Past", "term": {"name": "Past term", "end_at": "2020-01-01T00:00:00Z"}}
        def get(url, **kwargs):
            if url.endswith("/courses"):
                return response([current, past] if kwargs["params"]["enrollment_state"] == "active" else [])
            return response([])
        self.session.get.side_effect = get
        status = self.sync.run()
        self.assertEqual(status["state"], "complete")
        self.assertEqual(status["selected_courses"], 1)
        self.assertEqual([c["id"] for c in status["courses"]], ["1"])
        catalog = course_catalog(self.root)
        self.assertEqual([c["id"] for c in catalog["courses"] if c["hidden"]], ["2"])
        select_courses(self.root, {"1": False, "2": True})
        status = Sync(self.session, "https://canvas.example", self.root).run()
        self.assertEqual([c["id"] for c in status["courses"]], ["2"])
        with self.assertRaises(ValueError):
            select_courses(self.root, {"999": True})

    def test_login_failure_keeps_archive_and_reports_status(self):
        old = self.root / "existing.txt"
        old.write_text("existing data")
        self.session.get.return_value = response({}, code=401)
        status = self.sync.run()
        self.assertEqual(status["state"], "login_required")
        self.assertEqual(old.read_text(), "existing data")
        self.assertEqual(json.loads((self.root / ".sync-status.json").read_text())["state"], "login_required")

    def test_incremental_download_and_atomic_failure(self):
        item = {"id": 42, "display_name": "lecture.txt", "updated_at": "v1", "size": 3,
                "url": "https://cdn.example/download?secret=DO_NOT_LOG"}
        stream = Mock()
        stream.__enter__ = Mock(return_value=stream)
        stream.__exit__ = Mock(return_value=False)
        stream.status_code = 200
        stream.iter_content.return_value = iter([b"old"])
        self.session.get.return_value = stream
        self.sync.file("1", "Course", item)
        path = self.root / "Course/Canvas Sync/Files/42/lecture.txt"
        self.assertEqual(path.read_bytes(), b"old")
        self.sync.file("1", "Course", item)
        self.assertEqual(self.session.get.call_count, 1)
        def broken_chunks(*args):
            yield b"partial"
            raise requests.ConnectionError("https://cdn.example/download?secret=DO_NOT_LOG")
        stream.iter_content.side_effect = broken_chunks
        item["updated_at"] = "v2"
        self.sync.attempt("1", "file:42", lambda: self.sync.file("1", "Course", item))
        self.assertEqual(path.read_bytes(), b"old")
        self.assertNotIn("DO_NOT_LOG", json.dumps(self.sync.status))
        self.assertEqual(self.sync.state["items"]["1:file:42"]["version"][0], "v1")

    def test_symlink_write_is_rejected(self):
        with tempfile.TemporaryDirectory() as elsewhere:
            (self.root / "Course").symlink_to(elsewhere)
            with self.assertRaises(ValueError):
                self.sync.write_text("item", "Course/notes.txt", "secret")

    def test_roles_include_observer_ta_and_custom_without_roster_fields(self):
        role_data = enrollment_roles({"enrollments": [
            {"type": "student", "role": "StudentEnrollment", "enrollment_state": "active", "user_id": 123, "grades": {"score": 100}},
            {"type": "ta", "role": "Lab Assistant", "role_id": 42, "enrollment_state": "active"},
            {"type": "observer", "role": "ObserverEnrollment", "enrollment_state": "active"},
            {"type": "teacher", "role": "TeacherEnrollment", "enrollment_state": "active"},
            {"type": "designer", "role": "DesignerEnrollment", "enrollment_state": "active"}]})
        self.assertEqual(role_data["roles"], ["designer", "observer", "student", "ta", "teacher"])
        self.assertEqual(role_data["enrollments"][1]["role"], "Lab Assistant")
        self.assertNotIn("grades", role_data["enrollments"][0])
        self.assertNotIn("user_id", role_data["enrollments"][0])

    def test_multiple_enrollment_states_do_not_hide_active_role(self):
        active = {"id": 1, "name": "Course", "enrollments": [{"type": "ta", "role": "TaEnrollment", "enrollment_state": "active"}]}
        completed = {"id": 1, "name": "Course", "enrollments": [{"type": "student", "role": "StudentEnrollment", "enrollment_state": "completed"}]}
        self.session.get.side_effect = [response([active]), response([completed])]
        self.sync.discover()
        course = course_catalog(self.root)["courses"][0]
        self.assertFalse(course["past"])
        self.assertTrue(course["sync_enabled"])
        self.assertEqual(course["roles"], ["student", "ta"])

    def test_announcement_and_nested_discussion_with_fresh_entries(self):
        announcement = {"id": 10, "title": "Exam update", "message": "<p>Exam moved to Friday.</p>", "user_name": "Instructor", "is_announcement": True}
        discussion = {"id": 20, "title": "Reading discussion", "message": "<p>Explain the argument.</p>"}
        tree = {"participants": [{"id": 1, "display_name": "Student"}, {"id": 2, "display_name": "TA"}],
                "view": [{"id": 100, "user_id": 1, "message": "<p>Old answer</p>", "replies": [
                    {"id": 101, "user_id": 2, "message": "<p>A nested response</p>"}]}],
                "new_entries": [{"id": 100, "user_id": 1, "message": "<p>Edited answer</p>"},
                                {"id": 102, "user_id": 1, "parent_id": 101, "message": "<p>Newest reply</p>"}]}
        def get(url, **kwargs):
            if url.endswith("/discussion_topics"):
                self.assertNotEqual(kwargs["params"].get("only_announcements"), "false")
                return response([announcement] if kwargs["params"].get("only_announcements") == "true" else [discussion, announcement])
            return response({"view": [], "participants": []} if "/10/view" in url else tree)
        self.session.get.side_effect = get
        self.sync.sync_topics("1", "Course")
        announcement_file = self.root / "Course/Canvas Sync/Announcements/10-Exam update.txt"
        thread_file = self.root / "Course/Canvas Sync/Discussions/20-Reading discussion.txt"
        self.assertIn("Exam moved to Friday.", announcement_file.read_text())
        thread = thread_file.read_text()
        self.assertIn("Edited answer", thread)
        self.assertNotIn("Old answer", thread)
        self.assertEqual(thread.count("Entry 100"), 1)
        self.assertIn("Entry 101 (reply to 100)", thread)
        self.assertIn("Entry 102 (reply to 101)", thread)
        self.assertIn("Author: TA", thread)
        self.sync.sync_topics("1", "Course")
        self.assertEqual(self.sync.status["updated"], 2)
        self.assertEqual(self.sync.status["unchanged"], 2)

    def test_post_first_restriction_and_unavailable_replies(self):
        topic = {"id": 20, "title": "Discussion", "message": "Visible prompt", "user_can_see_posts": False}
        self.sync.discussion("1", "Course", topic)
        self.session.get.assert_not_called()
        path = self.root / "Course/Canvas Sync/Discussions/20-Discussion.txt"
        self.assertIn("Visible prompt", path.read_text())
        self.assertIn("not visible", path.read_text())
        topic["user_can_see_posts"] = True
        self.session.get.return_value = response({}, code=403)
        self.sync.discussion("1", "Course", topic)
        self.assertIn("Replies are unavailable", path.read_text())
        self.assertEqual(self.sync.status["errors"][0]["error"], "HTTP 403")

    def test_transient_thread_failure_preserves_complete_archive(self):
        topic = {"id": 20, "title": "Discussion", "message": "Prompt"}
        self.session.get.return_value = response({"view": [{"id": 100, "message": "Existing complete reply"}]})
        self.sync.discussion("1", "Course", topic)
        path = self.root / "Course/Canvas Sync/Discussions/20-Discussion.txt"
        previous = path.read_text()
        self.session.get.return_value = response({}, code=503)
        self.sync.attempt("1", "discussion", lambda: self.sync.discussion("1", "Course", topic))
        self.assertEqual(path.read_text(), previous)
        self.assertEqual(self.sync.status["errors"][0]["error"], "HTTP 503")

    def test_group_child_and_deleted_reply(self):
        topic = {"id": 20, "title": "Group topic", "message": "Parent", "group_topic_children": [{"id": 30, "group_id": 5}]}
        def get(url, **kwargs):
            if url.endswith("/discussion_topics"):
                return response([] if kwargs["params"].get("only_announcements") == "true" else [topic])
            if url.endswith("/discussion_topics/30"):
                return response({"id": 30, "title": "Group topic", "message": "Group prompt"})
            return response({"view": [{"id": 10, "deleted": True, "message": "Removed body"}]})
        self.session.get.side_effect = get
        self.sync.sync_topics("1", "Course")
        path = self.root / "Course/Canvas Sync/Discussions/30-Group topic-group-5.txt"
        self.assertIn("Group prompt", path.read_text())
        self.assertIn("[Deleted entry]", path.read_text())
        self.assertNotIn("Removed body", path.read_text())
        self.assertTrue(any("/api/v1/groups/5/discussion_topics/30/view" in call.args[0] for call in self.session.get.call_args_list))

    def test_missing_topic_body_fetches_detail_and_keeps_publication_state(self):
        self.session.get.side_effect = [
            response({"id": 20, "title": "Draft", "message": "Full prompt", "published": False, "delayed_post_at": "2099-01-01"}),
            response({"view": []})]
        self.sync.discussion("1", "Course", {"id": 20, "title": "Draft"})
        text = (self.root / "Course/Canvas Sync/Discussions/20-Draft.txt").read_text()
        self.assertIn("Full prompt", text)
        self.assertIn("Published: False", text)
        self.assertIn("Scheduled posting: 2099-01-01", text)
        self.assertTrue(self.session.get.call_args_list[0].args[0].endswith("/discussion_topics/20"))


if __name__ == "__main__":
    unittest.main()
