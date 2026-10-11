"""Incrementally refresh all accessible active and completed Canvas courses."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from urllib.parse import quote, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from canvas_downloader import create_session, load_cookies_from_browser, sanitize_filename
from connector_config import load_config, https_origin

PROJECT = Path(__file__).resolve().parent


class AuthenticationExpired(Exception):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def name(value):
    return sanitize_filename(str(value)).strip(". ") or "Untitled"


def html_text(value):
    soup = BeautifulSoup(value or "", "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    return soup.get_text("\n", strip=True)


def past_course(course):
    end = (course.get("term") or {}).get("end_at") or course.get("end_at")
    if end:
        try:
            if datetime.fromisoformat(end.replace("Z", "+00:00")) < datetime.now(timezone.utc):
                return True
        except ValueError:
            pass
    return course.get("workflow_state") == "completed" or course.get("enrollment_state") == "completed"


def enrollment_roles(course):
    """Keep only the signed-in user's role context, without grades or roster data."""
    enrollments = []
    for entry in course.get("enrollments") or []:
        enrollment = {"type": entry.get("type"), "role": entry.get("role"),
                      "role_id": entry.get("role_id"), "state": entry.get("enrollment_state")}
        if enrollment not in enrollments:
            enrollments.append(enrollment)
    return {"roles": sorted({e["type"] for e in enrollments if e["type"]}), "enrollments": enrollments}


def course_catalog(root):
    root = Path(root)
    path = root / ".sync-catalog.json"
    catalog = json.loads(path.read_text()) if path.is_file() else {"courses": [], "discovered_at": None}
    preferences = root / ".sync-preferences.json"
    overrides = json.loads(preferences.read_text()).get("overrides", {}) if preferences.is_file() else {}
    for course in catalog["courses"]:
        course["sync_enabled"] = overrides.get(str(course["id"]), not course["past"])
        course["hidden"] = not course["sync_enabled"]
    return catalog


def select_courses(root, selections):
    root = Path(root)
    known = {str(c["id"]) for c in course_catalog(root)["courses"]}
    if not selections or not set(selections).issubset(known):
        raise ValueError("Use course IDs returned by the course list")
    if any(type(value) is not bool for value in selections.values()):
        raise ValueError("Course selection values must be true or false")
    with (root / ".preferences.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = root / ".sync-preferences.json"
        data = json.loads(path.read_text()) if path.is_file() else {"overrides": {}}
        data["overrides"].update(selections)
        atomic_write(path, [json.dumps(data, indent=2).encode()])
    return course_catalog(root)


def request_sync(root, discover_only=False):
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".sync.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"state": "already_running", "message": "A Canvas refresh is already running."}
    log_dir = Path.home() / "Library/Logs/Canvas Connector"
    log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    command = [sys.executable, str(PROJECT / "canvas_sync.py"), "--output", str(root)]
    if discover_only:
        command.append("--discover-only")
    with (log_dir / "manual-sync.log").open("a") as log:
        subprocess.Popen(command, stdout=log, stderr=log, start_new_session=True, cwd=PROJECT)
    return {"state": "requested", "message": "Course list refresh requested." if discover_only else "Selected-course sync requested."}


def atomic_write(path: Path, chunks):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".sync-", delete=False) as stream:
            temporary = Path(stream.name)
            for chunk in chunks:
                stream.write(chunk)
        temporary.replace(path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


class Sync:
    def __init__(self, session, base_url, root):
        self.session = session
        self.base = base_url.rstrip("/")
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        state = self.root / ".sync-state.json"
        self.state = json.loads(state.read_text()) if state.exists() else {"courses": {}, "items": {}}
        self.status = {"started_at": now(), "state": "running", "courses": [],
                       "updated": 0, "unchanged": 0, "skipped_locked": 0, "errors": []}

    def safe_path(self, relative):
        path = self.root / relative
        if not path.resolve().is_relative_to(self.root):
            raise ValueError("Archive path leaves the archive")
        if any(p.is_symlink() for p in [path, *path.parents] if p != self.root):
            raise ValueError("Archive symlinks are excluded")
        return path

    def save(self):
        for filename, data in [(".sync-state.json", self.state), (".sync-status.json", self.status)]:
            atomic_write(self.safe_path(filename), [json.dumps(data, indent=2).encode()])

    def get(self, url, **kwargs):
        if urlparse(url).netloc != urlparse(self.base).netloc:
            raise ValueError("Canvas API pagination left the configured host")
        response = self.session.get(url, timeout=60, **kwargs)
        if response.status_code == 401:
            raise AuthenticationExpired("Canvas session expired; sign in again in Firefox")
        response.raise_for_status()
        return response

    def listing(self, endpoint, params=None):
        url = self.base + endpoint
        params = dict(params or {}, per_page=100)
        results = []
        while url:
            response = self.get(url, params=params)
            data = response.json()
            if not isinstance(data, list):
                raise ValueError("Canvas returned an unexpected list response")
            results.extend(data)
            url = response.links.get("next", {}).get("url")
            params = None
        return results

    def courses(self):
        courses = {}
        for state in ("active", "completed"):
            for course in self.listing("/api/v1/courses", {"enrollment_state": state, "include[]": ["syllabus_body", "term"]}):
                if course.get("id") and course.get("name"):
                    course["enrollment_state"] = state
                    key = str(course["id"])
                    if key in courses:
                        existing = courses[key]
                        course["enrollments"] = (existing.get("enrollments") or []) + (course.get("enrollments") or [])
                        # An active TA/student enrollment must not be hidden just because
                        # another enrollment in the same course is completed.
                        if existing["enrollment_state"] == "active":
                            course["enrollment_state"] = "active"
                    courses[key] = course
        return list(courses.values())

    def folder(self, course):
        key = str(course["id"])
        if key not in self.state["courses"]:
            candidate = name(course["name"])
            if candidate in self.state["courses"].values():
                candidate += f" [Canvas {key}]"
            self.state["courses"][key] = candidate
        return self.state["courses"][key]

    def write_text(self, key, relative, text):
        digest = hashlib.sha256(text.encode()).hexdigest()
        path = self.safe_path(relative)
        if self.state["items"].get(key) == {"version": digest, "path": relative} and path.is_file():
            self.status["unchanged"] += 1
            return
        atomic_write(path, [text.encode()])
        self.state["items"][key] = {"version": digest, "path": relative}
        self.status["updated"] += 1

    def file(self, course_id, folder, item):
        catalog_path = self.root / ".sync-catalog.json"
        if catalog_path.is_file() and any(c["id"] == str(course_id) and c["hidden"]
                                          for c in course_catalog(self.root)["courses"]):
            return
        if item.get("locked_for_user") or item.get("hidden_for_user"):
            self.status["skipped_locked"] += 1
            return
        file_id = str(int(item["id"]))
        relative = f"{folder}/Canvas Sync/Files/{file_id}/{name(item.get('display_name') or item.get('filename'))}"
        path = self.safe_path(relative)
        version = [item.get("updated_at"), item.get("modified_at"), item.get("size")]
        key = f"{course_id}:file:{file_id}"
        if any(version[:2]) and self.state["items"].get(key) == {"version": version, "path": relative} and path.is_file():
            self.status["unchanged"] += 1
            return
        url = item.get("url")
        if not url:
            url = self.get(f"{self.base}/api/v1/files/{file_id}").json().get("url")
        if not url or urlparse(url).scheme != "https":
            raise ValueError("No HTTPS download URL")
        # Browser cookies are scoped by domain. No bearer header is forwarded to a CDN.
        with self.session.get(url, stream=True, timeout=120) as response:
            if response.status_code == 401:
                raise AuthenticationExpired("Canvas session expired")
            response.raise_for_status()
            atomic_write(path, response.iter_content(65536))
        self.state["items"][key] = {"version": version, "path": relative}
        self.status["updated"] += 1
        self.save()

    def record_error(self, course_id, kind, error):
        # Do not expose signed download URLs or raw exception messages in logs/status.
        code = getattr(getattr(error, "response", None), "status_code", None)
        self.status["errors"].append({"course_id": str(course_id), "item": kind,
                                      "error": f"HTTP {code}" if code else type(error).__name__})

    def attempt(self, course_id, kind, action):
        try:
            action()
        except AuthenticationExpired:
            raise
        except (requests.RequestException, ValueError, KeyError, OSError) as error:
            self.record_error(course_id, kind, error)

    def course(self, course):
        course_id = str(int(course["id"]))
        folder = self.folder(course)
        self.status["courses"].append({"id": course_id, "name": course["name"]})
        self.status["current_course"] = {"id": course_id, "name": course["name"]}
        self.save()
        endpoint = f"/api/v1/courses/{course_id}"
        if course.get("syllabus_body"):
            self.write_text(f"{course_id}:syllabus", f"{folder}/Canvas Sync/syllabus.txt",
                            f"{course['name']}\nSource: {self.base}/courses/{course_id}/assignments/syllabus\n\n"
                            + html_text(course["syllabus_body"]))
        for kind in ("files", "pages", "assignments", "modules"):
            try:
                items = self.listing(endpoint + "/" + kind,
                                     {"include[]": "body"} if kind == "pages" else None)
            except AuthenticationExpired:
                raise
            except (requests.RequestException, ValueError) as error:
                self.record_error(course_id, kind, error)
                continue
            for item in items:
                if any(c["id"] == course_id and c["hidden"] for c in course_catalog(self.root)["courses"]):
                    break
                if kind == "files":
                    self.attempt(course_id, f"file:{item.get('id')}", lambda: self.file(course_id, folder, item))
                elif kind == "pages":
                    def page():
                        data = item
                        if "body" not in data:
                            slug = quote(str(data["url"]), safe="")
                            data = self.get(self.base + endpoint + "/pages/" + slug).json()
                        title = data.get("title", "Untitled")
                        key = f"{course_id}:page:{data['page_id']}"
                        text = f"{title}\nSource: {data.get('html_url', '')}\n\n{html_text(data.get('body'))}\n"
                        self.write_text(key, f"{folder}/Canvas Sync/Pages/{data['page_id']}-{name(title)}.txt", text)
                    self.attempt(course_id, f"page:{item.get('page_id')}", page)
                elif kind == "assignments":
                    title = item.get("name", "Assignment")
                    text = (f"{title}\nSource: {item.get('html_url', '')}\nDue: {item.get('due_at') or 'Not specified'}\n\n"
                            + html_text(item.get("description")) + "\n")
                    self.attempt(course_id, f"assignment:{item.get('id')}", lambda: self.write_text(
                        f"{course_id}:assignment:{item['id']}",
                        f"{folder}/Canvas Sync/Assignments/{item['id']}-{name(title)}.txt", text))
                else:
                    def module():
                        contents = self.listing(endpoint + f"/modules/{int(item['id'])}/items")
                        lines = [item.get("name", "Module")]
                        for entry in contents:
                            lines.append(f"{entry.get('type', '')}: {entry.get('title', '')}\n{entry.get('html_url', '')}")
                            if entry.get("type") == "File" and entry.get("content_id"):
                                def module_file():
                                    metadata = self.get(f"{self.base}/api/v1/files/{int(entry['content_id'])}").json()
                                    self.file(course_id, folder, metadata)
                                self.attempt(course_id, f"module-file:{entry['content_id']}", module_file)
                        self.write_text(f"{course_id}:module:{item['id']}",
                                        f"{folder}/Canvas Sync/Modules/{item['id']}-{name(item.get('name'))}.txt",
                                        "\n\n".join(lines))
                    self.attempt(course_id, f"module:{item.get('id')}", module)
            self.save()
        self.save()

    def discover(self):
        courses = self.courses()
        catalog = {"discovered_at": now(), "courses": [
            {"id": str(c["id"]), "name": c["name"], "term": (c.get("term") or {}).get("name", "Unknown term"),
             "past": past_course(c), "folder": self.folder(c),
             **enrollment_roles(c),
             "canvas_url": f"{self.base}/courses/{c['id']}"} for c in courses]}
        atomic_write(self.safe_path(".sync-catalog.json"), [json.dumps(catalog, indent=2).encode()])
        atomic_write(self.safe_path(".sync-state.json"), [json.dumps(self.state, indent=2).encode()])
        return courses

    def run(self, discover_only=False):
        if not discover_only:
            self.save()
        try:
            courses = self.discover()
            if discover_only:
                return course_catalog(self.root)
            selected = {c["id"] for c in course_catalog(self.root)["courses"] if c["sync_enabled"]}
            self.status["discovered_courses"] = len(courses)
            self.status["selected_courses"] = len(selected)
            for course in courses:
                # Recheck selection to honor changes made while a sync is running.
                selected_now = {c["id"] for c in course_catalog(self.root)["courses"] if c["sync_enabled"]}
                if str(course["id"]) in selected_now:
                    self.attempt(course["id"], "course", lambda: self.course(course))
            self.status["state"] = "partial" if self.status["errors"] else "complete"
            self.status.pop("current_course", None)
        except AuthenticationExpired:
            self.status["state"] = "login_required"
            self.status["message"] = "Sign into Canvas in Firefox, then sync again."
        except (requests.RequestException, ValueError, OSError) as error:
            self.status["state"] = "failed"
            self.record_error("all", "course_discovery", error)
        finally:
            if not discover_only:
                self.status["finished_at"] = now()
                self.save()
        return self.status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url")
    parser.add_argument("--browser")
    parser.add_argument("--output", type=Path, default=PROJECT / "downloads")
    parser.add_argument("--discover-only", action="store_true")
    args = parser.parse_args()
    config = load_config()
    args.base_url = args.base_url or config.get("canvas_url")
    args.browser = args.browser or config["browser"]
    if not args.base_url:
        parser.error("Configure your Canvas URL with ./setup.sh first, or pass --base-url")
    try:
        args.base_url = https_origin(args.base_url)
    except ValueError as error:
        parser.error(str(error))
    host = urlparse(args.base_url)
    if host.scheme != "https" or not host.netloc or host.path not in ("", "/"):
        parser.error("Use an HTTPS Canvas host URL without a course path")
    args.output.mkdir(parents=True, exist_ok=True)
    with (args.output / ".sync.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another sync is running; skipped.")
            return
        try:
            session = create_session(load_cookies_from_browser(args.browser, host.netloc))
        except SystemExit:
            atomic_write(args.output / ".sync-status.json", [json.dumps({
                "state": "login_required", "finished_at": now(),
                "message": "Could not load Canvas cookies. Sign in and check browser access."}).encode()])
            raise
        retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504], allowed_methods=["GET"])
        session.mount("https://", HTTPAdapter(max_retries=retry))
        result = Sync(session, args.base_url, args.output).run(args.discover_only)
        print(json.dumps(result, indent=2))
        if isinstance(result, dict) and result.get("state") not in ("complete", "running"):
            raise SystemExit(1)


if __name__ == "__main__":
    main()
