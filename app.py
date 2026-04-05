#!/usr/bin/env python3
"""Flask web UI for Canvas Course Downloader."""

import os
import shutil
import tempfile
import zipfile
from urllib.parse import urlparse

from flask import Flask, render_template, request, send_file

from canvas_downloader import (
    SUPPORTED_BROWSERS,
    create_session,
    fetch_course_name,
    fetch_module_items,
    fetch_modules,
    load_cookies,
    load_cookies_from_browser,
    parse_course_url,
    process_module_items,
    sanitize_filename,
    validate_canvas_url,
)

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024  # 2MB max upload


@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'GET':
        return render_template('index.html', browsers=SUPPORTED_BROWSERS)

    # Validate inputs
    url = request.form.get('url', '').strip()
    auth_method = request.form.get('auth_method', 'browser')
    cookie_file = request.files.get('cookies')
    browser = request.form.get('browser', '')

    if not url:
        return render_template('index.html', error="Please enter a Canvas course URL.", browsers=SUPPORTED_BROWSERS)
    if not validate_canvas_url(url):
        return render_template(
            'index.html',
            error="URL doesn't look like a Canvas course URL. Expected format: https://canvas.example.edu/courses/12345",
            url=url,
            browsers=SUPPORTED_BROWSERS,
        )

    if auth_method == 'file' and (not cookie_file or cookie_file.filename == ''):
        return render_template('index.html', error="Please upload a cookies.txt file.", url=url, browsers=SUPPORTED_BROWSERS)
    if auth_method == 'browser' and browser not in SUPPORTED_BROWSERS:
        return render_template('index.html', error="Please select a browser.", url=url, browsers=SUPPORTED_BROWSERS)

    tmpdir = tempfile.mkdtemp(prefix='canvas_dl_')
    try:
        return _process_download(url, auth_method, browser, cookie_file, tmpdir)
    except Exception:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise


def _process_download(url, auth_method, browser, cookie_file, tmpdir):
    output_dir = os.path.join(tmpdir, 'output')
    os.makedirs(output_dir)

    if auth_method == 'browser':
        domain = urlparse(url).netloc
        try:
            cookie_jar = load_cookies_from_browser(browser, domain)
        except SystemExit:
            shutil.rmtree(tmpdir, ignore_errors=True)
            return render_template(
                'index.html',
                error=f"Failed to load cookies from {browser}. Make sure you're logged into Canvas and try closing the browser first.",
                url=url,
                browsers=SUPPORTED_BROWSERS,
            )
    else:
        cookie_path = os.path.join(tmpdir, 'cookies.txt')
        cookie_file.save(cookie_path)
        try:
            cookie_jar = load_cookies(cookie_path)
        except SystemExit:
            shutil.rmtree(tmpdir, ignore_errors=True)
            return render_template(
                'index.html',
                error="Failed to load cookies. Make sure you uploaded a valid Netscape-format cookies.txt file.",
                url=url,
                browsers=SUPPORTED_BROWSERS,
            )

    session = create_session(cookie_jar)
    base_url, course_id = parse_course_url(url)

    # Fetch course name
    try:
        course_name = sanitize_filename(fetch_course_name(session, base_url, course_id))
    except SystemExit:
        shutil.rmtree(tmpdir, ignore_errors=True)
        return render_template(
            'index.html',
            error="Authentication failed. Your cookies may have expired — log into Canvas again and retry.",
            url=url,
            browsers=SUPPORTED_BROWSERS,
        )

    # Fetch modules
    try:
        modules = fetch_modules(session, base_url, course_id)
    except SystemExit:
        shutil.rmtree(tmpdir, ignore_errors=True)
        return render_template(
            'index.html',
            error="Failed to fetch modules. Check that you have access to this course.",
            url=url,
            browsers=SUPPORTED_BROWSERS,
        )

    if not modules:
        shutil.rmtree(tmpdir, ignore_errors=True)
        return render_template('index.html', error="No modules found in this course.", url=url, browsers=SUPPORTED_BROWSERS)

    # Process all modules
    total_stats = {'files_ok': 0, 'files_fail': 0, 'pages_ok': 0, 'pages_fail': 0}
    all_links = []

    for module in modules:
        module_name = sanitize_filename(module.get('name', 'Unnamed Module'))
        module_id = module['id']
        module_dir = os.path.join(output_dir, module_name)
        os.makedirs(module_dir, exist_ok=True)

        try:
            items = fetch_module_items(session, base_url, course_id, module_id)
        except (SystemExit, Exception):
            continue

        module_links = []
        stats = process_module_items(session, base_url, course_id, items, module_dir, module_links)

        for link in module_links:
            all_links.append(f"({module_name}) {link}")
        for key in total_stats:
            total_stats[key] += stats[key]

    # Save links
    if all_links:
        links_path = os.path.join(output_dir, 'links.txt')
        with open(links_path, 'w', encoding='utf-8') as f:
            for link in all_links:
                f.write(link + '\n')

    # Check if anything was saved
    has_content = False
    for root, dirs, files in os.walk(output_dir):
        if files:
            has_content = True
            break

    if not has_content:
        shutil.rmtree(tmpdir, ignore_errors=True)
        return render_template('index.html', error="No downloadable content found in this course.", url=url, browsers=SUPPORTED_BROWSERS)

    # Zip everything
    zip_path = os.path.join(tmpdir, f"{course_name}.zip")
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for root, _, files in os.walk(output_dir):
            for fname in files:
                fpath = os.path.join(root, fname)
                zf.write(fpath, os.path.relpath(fpath, output_dir))

    results = {
        'page_title': course_name,
        'files_ok': [f"{total_stats['files_ok']} files downloaded"],
        'files_fail': [f"{total_stats['files_fail']} files failed"] if total_stats['files_fail'] else [],
        'pages_ok': [f"{total_stats['pages_ok']} pages saved"],
        'pages_fail': [f"{total_stats['pages_fail']} pages failed"] if total_stats['pages_fail'] else [],
        'external_links': all_links,
        'has_main_page': True,
    }

    return render_template('index.html', results=results, zip_ready=True, url=url, tmpdir=tmpdir, browsers=SUPPORTED_BROWSERS)


@app.route('/download')
def download_zip():
    tmpdir = request.args.get('dir', '')
    if not tmpdir or not os.path.isdir(tmpdir) or not tmpdir.startswith(tempfile.gettempdir()):
        return "Invalid download link.", 400

    zip_files = [f for f in os.listdir(tmpdir) if f.endswith('.zip')]
    if not zip_files:
        return "Download not found.", 404

    zip_path = os.path.join(tmpdir, zip_files[0])

    response = send_file(zip_path, as_attachment=True, download_name=zip_files[0])
    response.call_on_close(lambda: shutil.rmtree(tmpdir, ignore_errors=True))
    return response


if __name__ == '__main__':
    app.run(debug=os.environ.get('FLASK_DEBUG', '0') == '1', port=5000)
