#!/usr/bin/env python3
"""Local Flask interface for creating temporary Canvas course archives."""
from __future__ import annotations

import atexit
import os
from pathlib import Path
import secrets
import shutil
import tempfile
import threading
import time
from urllib.parse import urlparse
import zipfile

from flask import Flask, render_template, request, send_file, session
import requests

from canvas_downloader import (
    SUPPORTED_BROWSERS, create_session, fetch_course_name, fetch_modules, CookieError,
    load_cookies, load_cookies_from_browser, parse_course_url,
    sanitize_filename, validate_canvas_url,
)
from course_archive import archive_modules, retry_failed, complete_course
from archive_io import course_lock


class DownloadStore:
    """Issue opaque, expiring download IDs without accepting filesystem paths."""
    def __init__(self, ttl=3600):
        self.root = Path(tempfile.mkdtemp(prefix='canvas_web_'))
        self.ttl = ttl
        self.lock = threading.Lock()
        self.entries = {}
        self.jobs = {}
        self.busy = set()
        atexit.register(shutil.rmtree, self.root, ignore_errors=True)

    def prune(self):
        with self.lock:
            for token, (path, created) in list(self.entries.items()):
                if token not in self.busy and time.monotonic() - created >= self.ttl:
                    shutil.rmtree(path.parent, ignore_errors=True)
                    del self.entries[token]
                    self.jobs.pop(token, None)

    def add(self, path):
        token = secrets.token_urlsafe(32)
        with self.lock:
            self.entries[token] = (Path(path), time.monotonic())
        return token

    def get(self, token):
        self.prune()
        with self.lock:
            entry = self.entries.get(token)
            return entry[0] if entry and entry[0].is_file() else None


def create_app(config=None):
    application = Flask(__name__)
    application.config.update(
        MAX_CONTENT_LENGTH=2 * 1024 * 1024,
        SECRET_KEY=secrets.token_hex(32),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Strict',
        DOWNLOAD_TTL=3600,
    )
    application.config.update(config or {})
    store = DownloadStore(application.config['DOWNLOAD_TTL'])
    application.extensions['downloads'] = store

    def page(error=None, status=200, **context):
        if 'csrf_token' not in session:
            session['csrf_token'] = secrets.token_urlsafe(32)
        form = {} if status == 413 else request.form
        values = dict(browsers=SUPPORTED_BROWSERS,
                      url=form.get('url', ''),
                      auth_method=form.get('auth_method', 'browser'),
                      browser=form.get('browser', 'firefox'),
                      csrf_token=session['csrf_token'], error=error)
        retry_id = form.get('retry_id', '')
        job = store.jobs.get(retry_id)
        if job and job['owner'] == session['csrf_token']:
            values.update(result=job['result'], course_title=job['title'], download_id=retry_id,
                          retry_method=job['method'], retry_browser=job['browser'],
                          retry_url=job['url'], has_content=job['has_content'],
                          expiry_minutes=max(1, application.config['DOWNLOAD_TTL'] // 60))
        values.update(context)
        return render_template('index.html', **values), status

    @application.after_request
    def response_headers(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; style-src 'self'; script-src 'self'; "
            "img-src 'self' data:; object-src 'none'; frame-ancestors 'none'; "
            "base-uri 'self'; form-action 'self'")
        return response

    @application.errorhandler(413)
    def too_large(_error):
        return page('The cookies file is too large. Upload a cookies.txt file smaller than 2 MB.', 413,
                    url='', auth_method='file', browser='firefox')

    @application.route('/', methods=['GET', 'POST'])
    def index():
        store.prune()
        if request.method == 'GET':
            return page()
        token = request.form.get('csrf_token', '')
        if not token or not secrets.compare_digest(token, session.get('csrf_token', '')):
            return page('This form expired. Please try again.', 400)
        retry_id = request.form.get('retry_id', '')
        job = store.jobs.get(retry_id) if retry_id else None
        if retry_id and (not job or job['owner'] != session['csrf_token']):
            return page('This retry has expired or is unavailable. Create the archive again.', 404)
        url = job['url'] if job else request.form.get('url', '').strip()
        method = request.form.get('auth_method', 'browser')
        browser = request.form.get('browser', '')
        cookie_file = request.files.get('cookies')
        if not validate_canvas_url(url):
            return page('Enter a Canvas course URL, such as https://canvas.example.edu/courses/12345.', 400)
        if method not in ('browser', 'file'):
            return page('Choose browser cookies or a cookies file.', 400)
        if method == 'browser' and browser not in SUPPORTED_BROWSERS:
            return page('Please select a supported browser.', 400)
        if method == 'file' and (not cookie_file or not cookie_file.filename):
            return page('Please upload a cookies.txt file.', 400)

        if job:
            with store.lock:
                if retry_id in store.busy:
                    return page('A retry is already running for this archive.', 409)
                store.busy.add(retry_id)
            work = job['work']
        else:
            work = Path(tempfile.mkdtemp(dir=store.root))
        keep = bool(job)
        try:
            try:
                if method == 'browser':
                    cookies = load_cookies_from_browser(browser, urlparse(url).hostname)
                else:
                    cookie_path = work / 'cookies.txt'
                    cookie_file.save(cookie_path)
                    try:
                        cookies = load_cookies(str(cookie_path))
                    finally:
                        cookie_path.unlink(missing_ok=True)
            except (SystemExit, CookieError):
                return page('Could not read your cookies. Sign into Canvas in the selected browser, or upload a fresh Netscape-format cookies.txt file.', 400)
            base_url, course_id = parse_course_url(url)
            with create_session(cookies) as client:
                output = work / 'output'
                if job:
                    title = job['title']
                    result = retry_failed(client, base_url, course_id, output, job['result'])
                else:
                    title = sanitize_filename(fetch_course_name(client, base_url, course_id))
                    module_list_failed = False
                    try:
                        modules = fetch_modules(client, base_url, course_id)
                    except (requests.RequestException, ValueError):
                        modules, module_list_failed = [], True
                    with course_lock(client, output):
                        result = archive_modules(client, base_url, course_id, modules, output)
                        if module_list_failed:
                            result.module_errors.append('Module list')
                            result.pending.append({'module_list': True})
                        result = complete_course(client, base_url, course_id, output, result, title,
                            include_personal=request.form.get('include_personal') == 'on',
                            include_groups=request.form.get('include_groups') == 'on')
            has_content = any(file.is_file() and file.name != 'archive-status.json'
                              and not any(part.startswith('.') for part in file.relative_to(output).parts)
                              for file in output.rglob('*'))
            if not has_content and not result.pending:
                return page('No downloadable content found in this course.')
            path = work / f'{title}.zip'
            temporary_zip = work / 'archive.part'
            try:
                with zipfile.ZipFile(temporary_zip, 'w', zipfile.ZIP_DEFLATED) as archive:
                    for file in sorted(output.rglob('*')):
                        if file.is_file() and not any(part.startswith('.') for part in file.relative_to(output).parts):
                            archive.write(file, file.relative_to(output))
                temporary_zip.replace(path)
            finally:
                temporary_zip.unlink(missing_ok=True)
            download_id = retry_id or store.add(path)
            store.jobs[download_id] = dict(work=work, result=result, title=title, url=url,
                                          method=method, browser=browser, has_content=has_content,
                                          owner=session['csrf_token'])
            keep = True
            return page(result=result, course_title=title, download_id=download_id,
                        retry_method=method, retry_browser=browser, retry_url=url,
                        has_content=has_content,
                        expiry_minutes=max(1, application.config['DOWNLOAD_TTL'] // 60))
        except SystemExit:
            return page('Canvas rejected the session. Sign in again and retry.', 401)
        except requests.HTTPError as error:
            code = error.response.status_code if error.response is not None else 502
            return page('Canvas rejected this request. Sign in again for HTTP 401; HTTP 403 means this account lacks access.', code if code in (401, 403) else 502)
        except (requests.RequestException, ValueError):
            return page('Canvas could not be reached or returned an unexpected response. Check your connection and course access, then retry.', 502)
        except OSError:
            return page('The archive could not be saved. Check available disk space and folder permissions, then retry.', 500)
        finally:
            if job:
                with store.lock:
                    store.busy.discard(retry_id)
            if not keep:
                shutil.rmtree(work, ignore_errors=True)

    @application.get('/download')
    def download_zip():
        path = store.get(request.args.get('id', ''))
        if path is None:
            return page('This download has expired or is unavailable. Create the archive again.', 404)
        return send_file(path, as_attachment=True, download_name=path.name)

    return application


app = create_app()

if __name__ == '__main__':
    app.run(host='127.0.0.1', debug=os.environ.get('FLASK_DEBUG', '0') == '1', port=5000)
