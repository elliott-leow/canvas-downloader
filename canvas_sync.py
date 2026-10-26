"""Incrementally refresh all accessible active and completed Canvas courses."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
import os
from urllib.parse import quote, urlparse

import requests
from archive_io import atomic_write, DownloadIndex, safe_path, lock_file, trim_connector_logs, course_lock
from canvas_downloader import (create_session, load_cookies_from_browser, sanitize_filename,
                               api_get_paginated, save_file_info, CookieError)
from canvas_content import rich_text, references, fidelity_html, offline_html, file_id
from connector_config import load_config, https_origin

PROJECT = Path(__file__).resolve().parent


class AuthenticationExpired(requests.HTTPError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def name(value):
    return sanitize_filename(str(value)).strip(". ") or "Untitled"


def html_text(value):
    return rich_text(value)


def past_course(course):
    end = (course.get("term") or {}).get("end_at") or course.get("end_at")
    if end:
        try:
            if datetime.fromisoformat(end.replace("Z", "+00:00")) < datetime.now(timezone.utc):
                return True
        except (ValueError, TypeError):
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
    state_path = root / '.sync-state.json'
    state = json.loads(state_path.read_text()) if state_path.is_file() else {}
    known = {str(c['id']) for c in catalog['courses']}
    # Recover visibility protection even for courses dropped by an older discovery.
    catalog['courses'].extend({'id': str(cid), 'folder': folder, 'name': folder, 'past': True, 'available': False}
                              for cid, folder in state.get('courses', {}).items() if str(cid) not in known)
    preferences = root / ".sync-preferences.json"
    overrides = json.loads(preferences.read_text()).get("overrides", {}) if preferences.is_file() else {}
    for course in catalog["courses"]:
        wanted = overrides.get(str(course["id"]), not course["past"])
        course["sync_enabled"] = wanted and course.get('available', True)
        course["hidden"] = not wanted
    return catalog


def select_courses(root, selections):
    root = Path(root)
    known = {str(c["id"]) for c in course_catalog(root)["courses"]}
    if not selections or not set(selections).issubset(known):
        raise ValueError("Use course IDs returned by the course list")
    if any(type(value) is not bool for value in selections.values()):
        raise ValueError("Course selection values must be true or false")
    with (root / ".preferences.lock").open("w") as lock:
        lock_file(lock)
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
            lock_file(lock, blocking=False)
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


class Sync:
    def __init__(self, session, base_url, root, include_personal=False, include_groups=False):
        self.session = session
        self.base = base_url.rstrip("/")
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        state = self.root / ".sync-state.json"
        self.state = json.loads(state.read_text()) if state.exists() else {"courses": {}, "items": {}}
        self.status = {"started_at": now(), "state": "running", "courses": [],
                       "updated": 0, "unchanged": 0, "skipped_locked": 0, "errors": [], 'unavailable_sources': []}
        self.failed = {}
        self.include_personal, self.include_groups = include_personal, include_groups
        self.downloads = None
        self._checkpoint = time.monotonic()
        self._pending_writes = 0
        self._selection = (0, set())

    def safe_path(self, relative):
        return safe_path(self.root, relative)

    def save(self):
        for filename, data in [(".sync-state.json", self.state), (".sync-status.json", self.status)]:
            atomic_write(self.safe_path(filename), [json.dumps(data, indent=2).encode()])
        self._pending_writes = 0
        self._checkpoint = time.monotonic()

    def checkpoint(self):
        self._pending_writes += 1
        if self._pending_writes >= 25 or time.monotonic() - self._checkpoint >= 5:
            self.save()

    def selected(self, course_id):
        if not (self.root / '.sync-catalog.json').is_file():
            return True
        if time.monotonic() - self._selection[0] >= 1:
            self._selection = (time.monotonic(), {c['id'] for c in course_catalog(self.root)['courses'] if c['hidden']})
        return str(course_id) not in self._selection[1]

    def get(self, url, **kwargs):
        target, origin = urlparse(url), urlparse(self.base)
        if (target.scheme, target.netloc.lower()) != (origin.scheme, origin.netloc.lower()):
            raise ValueError("Canvas API pagination left the configured host")
        response = self.session.get(url, timeout=60, **kwargs)
        if response.status_code == 401:
            raise AuthenticationExpired("Canvas session expired; sign in again in your configured browser", response=response)
        response.raise_for_status()
        return response

    def listing(self, endpoint, params=None):
        return api_get_paginated(self.session, self.base + endpoint, params, get=self.get)

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
            if candidate.casefold() in {value.casefold() for value in self.state["courses"].values()}:
                candidate += f" [Canvas {key}]"
            self.state["courses"][key] = candidate
        return self.state["courses"][key]

    def write_text(self, key, relative, text):
        digest = hashlib.sha256(text.encode()).hexdigest()
        path = self.safe_path(relative)
        previous = self.state['items'].get(key, {})
        # Retain the original stable path when an item is renamed.
        if previous.get('path'):
            old = self.safe_path(previous['path'])
            if old.is_file() and hashlib.sha256(old.read_bytes()).hexdigest() == previous.get('version'):
                path, relative = old, previous['path']
        if previous.get('version') == digest and path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == digest:
            self.status["unchanged"] += 1
        else:
            if path.is_file() and (previous.get('path') != relative or hashlib.sha256(path.read_bytes()).hexdigest() != previous.get('version')):
                from archive_io import available_path
                path = available_path(path.parent, path.name)
                relative = path.relative_to(self.root).as_posix()
            atomic_write(path, [text.encode()])
            self.status["updated"] += 1
        self.state["items"][key] = {"version": digest, "path": relative, 'available': True, 'last_seen': now()}
        self.checkpoint()
        return path

    def file(self, course_id, folder, item):
        if not self.selected(course_id):
            return
        file_id = str(int(item['id']))
        key = f'{course_id}:file:{file_id}'
        if item.get('locked_for_user') or item.get('hidden_for_user'):
            self.status['skipped_locked'] += 1
            if key in self.state['items']:
                self.state['items'][key]['available'] = False
            return
        relative = f"{folder}/Canvas Sync/Files/{file_id}/{name(item.get('display_name') or item.get('filename'))}"
        previous = self.state['items'].get(key, {})
        # One index per course also lets module, page and discussion attachments reuse bytes.
        if self.downloads is None or not self.downloads.root == self.safe_path(folder).resolve():
            self.downloads = DownloadIndex(self.safe_path(folder))
        self.session._canvas_archive = self.downloads
        index_key = f'file:{file_id}'
        if index_key not in self.downloads.items and previous.get('path'):
            old = self.safe_path(previous['path'])
            if old.is_file():
                expected = previous.get('size', (previous.get('version') or [None, None, None])[-1])
                self.downloads.items[index_key] = {'path': old.relative_to(self.downloads.root).as_posix(),
                    'version': previous.get('version'), 'size': expected}
        if not item.get('url'):
            item = dict(item, **self.get(f'{self.base}/api/v1/files/{file_id}').json())
        path, record, updated = save_file_info(self.session, item, self.safe_path(relative).parent)
        self.state['items'][key] = dict(record, path=path.relative_to(self.root).as_posix(), available=True, last_seen=now(),
                                        folder_id=item.get('folder_id'))
        self.status['updated' if updated else 'unchanged'] += 1
        self.checkpoint()
        return path

    def rich(self, course_id, folder, key, relative, header, body, text=None):
        assets, links = references(body, self.base)
        local = {}
        for url in assets:
            identifier = file_id(url, self.base)
            def asset(identifier=identifier):
                info = self.get(f'{self.base}/api/v1/files/{identifier}').json()
                self.file(course_id, folder, info)
            # Already downloaded assets need no repeated metadata request in this run.
            if self.downloads and f'file:{identifier}' in self.downloads.seen:
                continue
            self.attempt(course_id, f'asset:{identifier}', asset)
        text = rich_text(body, self.base) if text is None else text
        path = self.write_text(key, relative, header + '\n\n' + text + '\n')
        if self.downloads:
            for identifier, record in self.downloads.items.items():
                if identifier.startswith('file:'):
                    local[identifier[5:]] = os.path.relpath(self.downloads.root / record['path'], path.parent)
        if fidelity_html(body):
            html_key = key + ':html'
            self.write_text(html_key, str(path.with_suffix('.html').relative_to(self.root)), offline_html(body, self.base, local))
            self.state['items'][html_key]['companion_of'] = key
        elif key + ':html' in self.state['items']:
            self.state['items'][key + ':html']['available'] = False
        return path

    def record_error(self, course_id, kind, error):
        # Do not expose signed download URLs or raw exception messages in logs/status.
        code = getattr(getattr(error, "response", None), "status_code", None)
        self.status["errors"].append({"course_id": str(course_id), "item": kind,
                                      "error": f"HTTP {code}" if code else type(error).__name__})

    def attempt(self, course_id, kind, action):
        key = f'{course_id}:{kind}'
        try:
            if action() is not False:
                self.failed.pop(key, None)
        except AuthenticationExpired:
            raise
        except (requests.RequestException, ValueError, KeyError, OSError, TypeError) as error:
            self.failed[key] = (str(course_id), kind, action)
            self.record_error(course_id, kind, error)
            for item_key, record in self.state['items'].items():
                if item_key == key:
                    record['available'] = False

    def retry(self):
        self.status['errors'] = []
        for course_id, kind, action in list(self.failed.values()):
            self.attempt(course_id, kind, action)
        self.finish()

    def finish(self):
        self.status['state'] = 'partial' if self.failed or self.status['errors'] else 'complete'
        self.status['finished_at'] = now()
        if self.downloads:
            self.downloads.flush()
        self.save()

    def discussion(self, course_id, folder, topic, announcement=False, group_id=None):
        topic_id = str(int(topic["id"]))
        category = "Announcements" if announcement else "Discussions"
        context = f"/api/v1/groups/{int(group_id)}" if group_id else f"/api/v1/courses/{course_id}"
        if "message" not in topic and not topic.get("locked_for_user"):
            topic = dict(topic, **self.get(self.base + context + f"/discussion_topics/{topic_id}").json())
        title = topic.get("title") or category[:-1]
        source = topic.get("html_url") or (
            f"{self.base}/groups/{group_id}/discussion_topics/{topic_id}" if group_id else
            f"{self.base}/courses/{course_id}/discussion_topics/{topic_id}")
        suffix = f":group:{group_id}" if group_id else ""
        key = f"{course_id}:{category.lower()}:{topic_id}{suffix}"
        filename = f"{topic_id}-{name(title)}" + (f"-group-{group_id}" if group_id else "") + ".txt"
        previous = self.state['items'].get(key, {})
        old = self.safe_path(previous['path']) if previous.get('path') else None
        complete_before = previous.get('complete', False) or bool(old and old.is_file() and '\nReplies\n' in old.read_text())
        complete = False
        bodies = []
        lines = [title, f"Source: {source}", f"Author: {topic.get('user_name') or (topic.get('author') or {}).get('display_name') or 'Unknown'}",
                 f"Posted: {topic.get('posted_at') or 'Not specified'}",
                 f"Last reply: {topic.get('last_reply_at') or 'Not specified'}",
                 f"Published: {topic.get('published', 'Unknown')}",
                 f"Scheduled posting: {topic.get('delayed_post_at') or 'Not specified'}"]
        if topic.get("locked_for_user"):
            self.status["skipped_locked"] += 1
            if old and old.is_file():
                previous['available'] = False
                return
            lines.append("\nThis topic is not currently available to your Canvas account.")
        else:
            bodies.append(topic.get('message') or '')
            lines.extend(["\nTopic", rich_text(topic.get("message"), self.base)])
            for attachment in topic.get("attachments") or []:
                lines.append(f"Attachment: {attachment.get('display_name') or attachment.get('filename') or 'File'} — {attachment.get('url', '')}")
                self.attempt(course_id, f'{category}:attachment:{topic_id}:{attachment.get("id", attachment.get("filename"))}',
                             lambda attachment=attachment: self.attachment(course_id, folder, attachment))
            if topic.get("user_can_see_posts") is False:
                lines.append("\nReplies are not visible to your Canvas account; Canvas may require an initial post.")
            else:
                try:
                    view = self.get(self.base + context + f"/discussion_topics/{topic_id}/view",
                                    params={"include_new_entries": 1}).json()
                except requests.HTTPError as error:
                    if error.response.status_code not in (403, 404):
                        raise  # Transient failures preserve the last complete thread.
                    self.record_error(course_id, f"{category.lower()}-replies:{topic_id}{suffix}", error)
                    retry_key = f'{course_id}:{category.lower()}:{topic_id}{suffix}'
                    self.failed[retry_key] = (str(course_id), f'{category.lower()}:{topic_id}{suffix}',
                        lambda: self.discussion(course_id, folder, topic, announcement, group_id))
                    if complete_before:
                        previous['available'] = False
                        return False
                    lines.append("\nReplies are unavailable to your Canvas account.")
                else:
                    complete = True
                    if not isinstance(view, dict) or not isinstance(view.get("view"), list):
                        raise ValueError("Canvas returned an unexpected discussion view")
                    authors = {str(p["id"]): p.get("display_name") or "Unknown"
                               for p in view.get("participants") or []}
                    entries = {}
                    stack = [(entry, None) for entry in reversed(view.get("view") or [])]
                    while stack:
                        entry, parent = stack.pop()
                        entry_id = str(entry["id"])
                        entries[entry_id] = dict(entry, parent_id=entry.get("parent_id", parent))
                        stack.extend((reply, entry["id"]) for reply in reversed(entry.get("replies") or []))
                    # New entries are a flat, fresher overlay on the eventually consistent view.
                    for entry in view.get("new_entries") or []:
                        entry_id = str(entry["id"])
                        entries[entry_id] = dict(entries.get(entry_id, {}), **entry)
                    lines.append("\nReplies")
                    if not entries:
                        lines.append("No visible replies.")
                    for entry_id, entry in sorted(entries.items(), key=lambda pair: (pair[1].get("created_at") or "", int(pair[0]))):
                        author = authors.get(str(entry.get("user_id")), "Unknown")
                        parent = entry.get("parent_id")
                        lines.extend([f"\nEntry {entry_id}" + (f" (reply to {parent})" if parent is not None else ""),
                                      f"Author: {author}", f"Posted: {entry.get('created_at') or 'Not specified'}",
                                      "[Deleted entry]" if entry.get("deleted") else rich_text(entry.get("message"), self.base)])
                        if not entry.get('deleted'):
                            bodies.append(entry.get('message') or '')
                        attachment = entry.get("attachment")
                        if attachment and not entry.get("deleted"):
                            lines.append(f"Attachment: {attachment.get('display_name') or attachment.get('filename') or 'File'} — {attachment.get('url', '')}")
                            self.attempt(course_id, f'{category}:attachment:{topic_id}:{entry_id}',
                                         lambda attachment=attachment: self.attachment(course_id, folder, attachment))
        self.rich(course_id, folder, key, f"{folder}/Canvas Sync/{category}/{filename}",
                  '\n'.join(lines), '\n'.join(bodies), text='')
        self.state['items'][key]['complete'] = complete
        return complete or topic.get('user_can_see_posts') is False

    def attachment(self, course_id, folder, attachment):
        identifier = attachment.get('id') or file_id(attachment.get('url', ''), self.base)
        if identifier:
            info = self.get(f'{self.base}/api/v1/files/{int(identifier)}').json()
            return self.file(course_id, folder, info)
        identifier = hashlib.sha256((attachment.get('filename') or attachment.get('url') or '').encode()).hexdigest()[:20]
        if self.downloads is None:
            self.downloads = DownloadIndex(self.safe_path(folder))
        self.session._canvas_archive = self.downloads
        path, record, updated = save_file_info(self.session, attachment,
            self.safe_path(f'{folder}/Canvas Sync/Files/attachments/{identifier}'), identifier=identifier)
        self.state['items'][f'{course_id}:attachment:{identifier}'] = dict(record,
            path=path.relative_to(self.root).as_posix(), available=True, last_seen=now())
        self.status['updated' if updated else 'unchanged'] += 1
        return path

    def collect(self, course_id, label, endpoint, action, params=None, prefix=None, optional=False):
        def load():
            try:
                items = self.listing(endpoint, params)
            except requests.HTTPError as error:
                if not optional or error.response is None or error.response.status_code not in (403, 404):
                    raise
                self.status['unavailable_sources'].append({'course_id': str(course_id), 'source': label,
                    'reason': f'HTTP {error.response.status_code}: optional source unavailable to this account'})
                for key, record in self.state['items'].items():
                    if prefix and key.startswith(prefix):
                        record['available'] = False
                return
            seen = set()
            for item in items:
                if not self.selected(course_id):
                    return
                identifier = str(item.get('page_id') if label == 'pages' else item.get('id'))
                seen.add(identifier)
                kind = prefix[:-1].split(':', 1)[1] if prefix else label
                self.attempt(course_id, f'{kind}:{identifier}', lambda item=item: action(item))
            if prefix:
                for key, record in self.state['items'].items():
                    if key.startswith(prefix) and ':group:' not in key and key[len(prefix):].split(':')[0] not in seen:
                        record['available'] = False
            self.save()
        self.attempt(course_id, label, load)

    def sync_topics(self, course_id, folder):
        endpoint = f'/api/v1/courses/{course_id}/discussion_topics'
        for announcement in (True, False):
            category = 'announcements' if announcement else 'discussions'
            def topic(data, announcement=announcement, category=category):
                if not announcement and data.get('is_announcement') is True:
                    return
                result = self.discussion(course_id, folder, data, announcement)
                for child in ([] if data.get('locked_for_user') else data.get('group_topic_children') or []):
                    def group_topic(child=child):
                        group_id, child_id = int(child['group_id']), int(child['id'])
                        detail = self.get(f'{self.base}/api/v1/groups/{group_id}/discussion_topics/{child_id}').json()
                        return self.discussion(course_id, folder, detail, announcement, group_id)
                    self.attempt(course_id, f'{category}-group:{child.get("id")}', group_topic)
                return result
            self.collect(course_id, category, endpoint, topic,
                         {'only_announcements': 'true'} if announcement else None,
                         prefix=f'{course_id}:{category}:')

    def page(self, course_id, folder, item, endpoint=None, relative=None, prefix=None):
        key = f'{prefix or str(course_id) + ":page"}:{int(item["page_id"])}'
        if item.get('locked_for_user'):
            self.status['skipped_locked'] += 1
            if key in self.state['items']:
                self.state['items'][key]['available'] = False
            return
        cache = vars(self.session).get('_canvas_pages', {})
        data = cache.get((str(course_id), item.get('url')), item) if endpoint is None else item
        if 'body' not in data:
            slug = quote(str(data['url']), safe='')
            data = self.get(self.base + (endpoint or f'/api/v1/courses/{course_id}/pages') + '/' + slug).json()
        title = data.get('title', 'Untitled')
        index_key = f'{prefix or "page:" + str(course_id)}:{data.get("url") or item.get("url")}'
        if self.downloads and index_key in self.downloads.items:
            saved = self.downloads.items[index_key]
            path = self.downloads.root / saved['path']
            if path.is_file():
                relative = path.relative_to(self.root).as_posix()
                self.state['items'][key] = {'path': relative, 'version': saved.get('sha256')}
                companion_record = self.downloads.items.get(index_key + ':html')
                if companion_record:
                    companion = self.downloads.root / companion_record['path']
                    self.state['items'][key + ':html'] = {'path': companion.relative_to(self.root).as_posix(),
                        'version': companion_record.get('sha256')}
        path = self.rich(course_id, folder, key,
            relative or f'{folder}/Canvas Sync/Pages/{data["page_id"]}-{name(title)}.txt',
            f'{title}\nSource: {data.get("html_url", "")}', data.get('body'))
        if self.downloads:
            self.downloads.record(index_key, path, None, {'size': path.stat().st_size,
                'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})

    def assignment(self, course_id, folder, item):
        identifier = int(item['id'])
        key = f'{course_id}:assignment:{identifier}'
        if item.get('locked_for_user'):
            self.status['skipped_locked'] += 1
            if key in self.state['items']:
                self.state['items'][key]['available'] = False
            return
        title = item.get('name', 'Assignment')
        lines = [title, f'Source: {item.get("html_url", "")}', f'Due: {item.get("due_at") or "Not specified"}']
        for field in ('points_possible', 'grading_type', 'submission_types', 'allowed_extensions', 'unlock_at',
                      'lock_at', 'published', 'allowed_attempts', 'all_dates'):
            if field in item:
                lines.append(f'{field}: {json.dumps(item[field], ensure_ascii=False)}')
        for criterion in item.get('rubric') or []:
            lines.append(f'Rubric: {criterion.get("description", "")} ({criterion.get("points", "")} points)')
            lines.append(criterion.get('long_description') or '')
            for rating in criterion.get('ratings') or []:
                lines.append(f'  {rating.get("points", "")}: {rating.get("description", "")} {rating.get("long_description") or ""}')
        self.rich(course_id, folder, key, f'{folder}/Canvas Sync/Assignments/{identifier}-{name(title)}.txt',
                  '\n'.join(lines), item.get('description'))
        if self.include_personal:
            self.attempt(course_id, f'submission:{identifier}', lambda: self.submission(course_id, folder, identifier))

    def submission(self, course_id, folder, assignment_id):
        data = self.get(f'{self.base}/api/v1/courses/{course_id}/assignments/{assignment_id}/submissions/self',
                        params={'include[]': ['submission_comments', 'submission_history', 'rubric_assessment']}).json()
        lines, bodies = [f'Your submission: assignment {assignment_id}'], []
        attempts = [dict(entry, **data) if entry.get('attempt') == data.get('attempt') else entry
                    for entry in data.get('submission_history') or [data]]
        if data.get('attempt') not in {a.get('attempt') for a in attempts}:
            attempts.append(data)
        for entry in attempts:
            for field in ('attempt', 'submitted_at', 'workflow_state', 'score', 'grade', 'late', 'missing', 'url', 'html_url', 'rubric_assessment'):
                if field in entry:
                    lines.append(f'{field}: {json.dumps(entry[field], ensure_ascii=False)}')
            bodies.append(entry.get('body') or '')
            for attachment in entry.get('attachments') or []:
                self.attempt(course_id, f'submission-attachment:{assignment_id}:{attachment.get("id")}',
                    lambda attachment=attachment: self.attachment(course_id, folder, attachment))
            for comment in entry.get('submission_comments') or []:
                lines.append(f'{comment.get("created_at", "")} — {comment.get("author_name", "Unknown")}: {comment.get("comment", "")}')
                for attachment in comment.get('attachments') or []:
                    self.attempt(course_id, f'feedback-attachment:{assignment_id}:{attachment.get("id")}',
                        lambda attachment=attachment: self.attachment(course_id, folder, attachment))
        self.rich(course_id, folder, f'{course_id}:submission:{assignment_id}',
                  f'{folder}/Canvas Sync/Your submissions/{assignment_id}.txt', '\n'.join(lines), '\n'.join(bodies))

    def module(self, course_id, folder, item):
        contents = self.listing(f'/api/v1/courses/{course_id}/modules/{int(item["id"])}/items')
        lines = [item.get('name', 'Module'), f'Position: {item.get("position", "Unknown")}']
        for position, entry in enumerate(contents, 1):
            lines.append(f'{position}. {entry.get("type", "")}: {entry.get("title", "")}\n'
                         f'{entry.get("external_url") or entry.get("html_url", "")}')
            if entry.get('type') == 'File' and entry.get('content_id'):
                def module_file(entry=entry):
                    metadata = self.get(f'{self.base}/api/v1/files/{int(entry["content_id"])}').json()
                    self.file(course_id, folder, metadata)
                self.attempt(course_id, f'module-file:{entry["content_id"]}', module_file)
        self.write_text(f'{course_id}:module:{item["id"]}',
            f'{folder}/Canvas Sync/Modules/{item["id"]}-{name(item.get("name"))}.txt', '\n\n'.join(lines))

    def quiz(self, course_id, folder, item, new=False):
        identifier = int(item['id'])
        title = item.get('title', 'Quiz')
        lines = [title, f'Source: {item.get("html_url") or self.base + "/courses/" + str(course_id)}',
                 'Only available quiz instructions and metadata are archived; questions and answers are excluded.']
        for field in ('quiz_type', 'time_limit', 'due_at', 'unlock_at', 'lock_at', 'allowed_attempts', 'points_possible', 'published', 'quiz_settings'):
            if field in item:
                lines.append(f'{field}: {json.dumps(item[field], ensure_ascii=False)}')
        category = 'new-quiz' if new else 'quiz'
        self.rich(course_id, folder, f'{course_id}:{category}:{identifier}',
                  f'{folder}/Canvas Sync/Quizzes/{category}-{identifier}-{name(title)}.txt',
                  '\n'.join(lines), item.get('instructions') if new else item.get('description'))

    def event(self, course_id, folder, item):
        identifier = int(item['id'])
        title = item.get('title', 'Event')
        lines = [title, f'Source: {item.get("html_url", "")}']
        for field in ('start_at', 'end_at', 'location_name', 'location_address', 'all_day', 'workflow_state'):
            if field in item:
                lines.append(f'{field}: {item[field]}')
        self.rich(course_id, folder, f'{course_id}:event:{identifier}',
            f'{folder}/Canvas Sync/Calendar/{identifier}-{name(title)}.txt', '\n'.join(lines), item.get('description'))

    def groups(self, course_id, folder):
        def group(item):
            gid = int(item['id'])
            endpoint = f'/api/v1/groups/{gid}'
            self.collect(course_id, f'group:{gid}:files', endpoint + '/files', lambda data: self.file(course_id, folder, data))
            def page(data):
                return self.page(course_id, folder, data, endpoint + '/pages',
                    f'{folder}/Canvas Sync/Groups/{gid}/Pages/{int(data["page_id"])}-{name(data.get("title"))}.txt',
                    f'{course_id}:group:{gid}:page')
            self.collect(course_id, f'group:{gid}:pages', endpoint + '/pages', page, {'include[]': 'body'})
        self.collect(course_id, 'groups', f'/api/v1/courses/{course_id}/groups', group, {'only_own_groups': 'true'})

    def course(self, course, folder=None, modules=True):
        folder = self.folder(course) if folder is None else folder
        with course_lock(self.session, self.safe_path(folder)):
            return self._course(course, folder, modules)

    def _course(self, course, folder, modules):
        course_id = str(int(course['id']))
        folder = self.folder(course) if folder is None else folder
        if self.downloads is None or self.downloads.root != self.safe_path(folder).resolve():
            self.downloads = DownloadIndex(self.safe_path(folder))
        self.session._canvas_archive = self.downloads
        self.status['courses'].append({'id': course_id, 'name': course['name']})
        self.status['current_course'] = {'id': course_id, 'name': course['name']}
        self.save()
        endpoint = f'/api/v1/courses/{course_id}'
        if course.get('syllabus_body'):
            self.rich(course_id, folder, f'{course_id}:syllabus', f'{folder}/Canvas Sync/syllabus.txt',
                f'{course["name"]}\nSource: {self.base}/courses/{course_id}/assignments/syllabus', course['syllabus_body'])
        self.collect(course_id, 'files', endpoint + '/files', lambda item: self.file(course_id, folder, item), prefix=f'{course_id}:file:')
        self.collect(course_id, 'pages', endpoint + '/pages', lambda item: self.page(course_id, folder, item),
                     {'include[]': 'body'}, prefix=f'{course_id}:page:')
        self.collect(course_id, 'assignments', endpoint + '/assignments', lambda item: self.assignment(course_id, folder, item),
                     {'include[]': 'all_dates'}, prefix=f'{course_id}:assignment:')
        if modules:
            self.collect(course_id, 'modules', endpoint + '/modules', lambda item: self.module(course_id, folder, item), prefix=f'{course_id}:module:')
        self.sync_topics(course_id, folder)
        self.collect(course_id, 'quizzes', endpoint + '/quizzes', lambda item: self.quiz(course_id, folder, item), prefix=f'{course_id}:quiz:')
        self.collect(course_id, 'new-quizzes', f'/api/quiz/v1/courses/{course_id}/quizzes',
                     lambda item: self.quiz(course_id, folder, item, new=True), prefix=f'{course_id}:new-quiz:', optional=True)
        self.collect(course_id, 'calendar', '/api/v1/calendar_events', lambda item: self.event(course_id, folder, item),
                     {'context_codes[]': f'course_{course_id}', 'all_events': 'true', 'type': 'event'}, prefix=f'{course_id}:event:')
        if self.include_groups:
            self.groups(course_id, folder)
        self.downloads.flush()
        self.save()

    def discover(self):
        courses = self.courses()
        previous = course_catalog(self.root)['courses']
        catalog = {"discovered_at": now(), "courses": [
            {"id": str(c["id"]), "name": c["name"], "term": (c.get("term") or {}).get("name", "Unknown term"),
             "past": past_course(c), "folder": self.folder(c),
             **enrollment_roles(c),
             "available": True, "canvas_url": f"{self.base}/courses/{c['id']}"} for c in courses]}
        current_ids = {str(c['id']) for c in courses}
        catalog['courses'].extend(dict(c, available=False) for c in previous if str(c['id']) not in current_ids)
        atomic_write(self.safe_path(".sync-catalog.json"), [json.dumps(catalog, indent=2).encode()])
        atomic_write(self.safe_path(".sync-state.json"), [json.dumps(self.state, indent=2).encode()])
        return courses

    def bind_account(self):
        """Refuse cross-host/account reuse before writing archive content."""
        origin = self.state.get('origin')
        if origin and origin != self.base:
            raise ValueError('Use a separate archive for a different Canvas host')
        user = vars(self.session).get('_canvas_owner')
        if user is None:
            user = self.get(self.base + '/api/v1/users/self').json()
            self.session._canvas_owner = user
        user_id = str(int(user['id']))
        if self.state.get('owner_id') and self.state['owner_id'] != user_id:
            raise ValueError('Use a separate archive for a different Canvas account')
        self.state.update(origin=self.base, owner_id=user_id)

    def run(self, discover_only=False):
        if not discover_only:
            self.save()
        try:
            self.bind_account()
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
                    self.attempt(course["id"], "course", lambda course=course: self.course(course))
            self.status["state"] = "partial" if self.status["errors"] else "complete"
            self.status.pop("current_course", None)
        except AuthenticationExpired:
            self.status["state"] = "login_required"
            self.status["message"] = "Sign into Canvas in Firefox, then sync again."
        except (requests.RequestException, ValueError, KeyError, OSError, TypeError) as error:
            self.status["state"] = "failed"
            self.record_error("all", "course_discovery", error)
        finally:
            if not discover_only:
                self.status["finished_at"] = now()
                self.save()
        return self.status


def main():
    trim_connector_logs(PROJECT)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url")
    parser.add_argument("--browser")
    parser.add_argument("--output", type=Path, default=PROJECT / "downloads")
    parser.add_argument("--discover-only", action="store_true")
    parser.add_argument('--include-personal', action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument('--include-groups', action=argparse.BooleanOptionalAction, default=None)
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
            lock_file(lock, blocking=False)
        except BlockingIOError:
            print("Another sync is running; skipped.")
            return
        try:
            session = create_session(load_cookies_from_browser(args.browser, host.hostname))
        except (SystemExit, CookieError):
            atomic_write(args.output / ".sync-status.json", [json.dumps({
                "state": "login_required", "finished_at": now(),
                "message": "Could not load Canvas cookies. Sign in and check browser access."}).encode()])
            raise SystemExit(1) from None
        with session:
            result = Sync(session, args.base_url, args.output,
                include_personal=args.include_personal if args.include_personal is not None else config.get('include_personal', False),
                include_groups=args.include_groups if args.include_groups is not None else config.get('include_groups', False)).run(args.discover_only)
        print(json.dumps(result, indent=2))
        if result.get("state") in ("partial", "failed", "login_required"):
            raise SystemExit(1)


if __name__ == "__main__":
    main()
