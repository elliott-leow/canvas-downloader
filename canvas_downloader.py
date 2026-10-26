#!/usr/bin/env python3
"""
Canvas Course Downloader

Downloads all files and pages from a Canvas LMS course using the REST API.
Organizes content by module into subdirectories.
"""

from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlparse, unquote, urljoin

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from archive_io import atomic_write, available_path, DownloadIndex, write_download, course_lock
from canvas_content import rich_text, references, fidelity_html, offline_html, file_id


class CookieError(ValueError):
    """The selected cookie source could not be read."""


SUPPORTED_BROWSERS = ['chrome', 'firefox', 'opera', 'edge', 'chromium', 'brave', 'vivaldi', 'safari']


def load_cookies_from_browser(browser_name: str, domain: str = '') -> http.cookiejar.CookieJar:
    """Load cookies directly from an installed browser."""
    import browser_cookie3

    browser_funcs = {
        'chrome': browser_cookie3.chrome,
        'firefox': browser_cookie3.firefox,
        'opera': browser_cookie3.opera,
        'edge': browser_cookie3.edge,
        'chromium': browser_cookie3.chromium,
        'brave': browser_cookie3.brave,
        'vivaldi': browser_cookie3.vivaldi,
        'safari': browser_cookie3.safari,
    }

    func = browser_funcs.get(browser_name)
    if not func:
        raise CookieError("Select a supported browser")

    try:
        cookie_jar = func(domain_name=domain) if domain else func()
        return cookie_jar
    except Exception as error:
        raise CookieError(f"Could not read {browser_name} cookies. Sign in and check browser access.") from error


def load_cookies(cookie_file: str) -> http.cookiejar.MozillaCookieJar:
    """Load cookies from a Netscape format cookie file."""
    cookie_jar = http.cookiejar.MozillaCookieJar(cookie_file)
    try:
        cookie_jar.load(ignore_discard=True, ignore_expires=True)
    except Exception as e:
        raise CookieError("Could not read cookies.txt. Supply a Netscape-format cookie export.") from e
    return cookie_jar


def create_session(cookie_jar: http.cookiejar.CookieJar) -> requests.Session:
    """Create a requests session with cookies loaded."""
    session = requests.Session()
    session.cookies = cookie_jar
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    })
    retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504],
                  allowed_methods=["GET"], respect_retry_after_header=True)
    for scheme in ("https://", "http://"):
        session.mount(scheme, HTTPAdapter(max_retries=retry))
    return session


WINDOWS_RESERVED_NAMES = {
    'CON', 'PRN', 'AUX', 'NUL',
    *(f'COM{i}' for i in range(1, 10)),
    *(f'LPT{i}' for i in range(1, 10)),
}


def sanitize_filename(name: str) -> str:
    """Remove characters that are invalid on Windows, macOS, or Linux."""
    # Strip control characters (0x00-0x1F)
    name = re.sub(r'[\x00-\x1f]', '', name)
    # Replace characters invalid on Windows
    invalid_chars = '<>:"/\\|?*'
    for char in invalid_chars:
        name = name.replace(char, '_')
    # Collapse multiple underscores/spaces
    name = re.sub(r'_+', '_', name)
    name = name.strip().strip('.')[:200].rstrip('. ') or 'Untitled'
    # Avoid Windows reserved names (CON, PRN, NUL, COM1, etc.)
    if name.split('.')[0].upper() in WINDOWS_RESERVED_NAMES:
        name = f"_{name}"
    return name


def parse_course_url(url: str) -> tuple[str, str]:
    """Extract base_url and course_id from a Canvas course URL.

    Returns (base_url, course_id).
    """
    if not validate_canvas_url(url):
        raise ValueError("Expected a Canvas course URL, such as https://canvas.example.edu/courses/12345")
    parsed = urlparse(url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    match = re.match(r'^/courses/(\d+)(?:/|$)', parsed.path)
    return base_url, match.group(1)


def api_get_paginated(session: requests.Session, url: str, params: dict = None, get=None) -> list:
    """GET a Canvas API endpoint, handling pagination via Link headers.

    Returns the combined list of JSON results across all pages.
    """
    params = dict(params or {})
    params.setdefault('per_page', 100)

    results = []
    next_url = url

    seen = set()
    origin = urlparse(url)
    while next_url:
        target = urlparse(next_url)
        if (target.scheme, target.netloc.lower()) != (origin.scheme, origin.netloc.lower()):
            raise ValueError("Canvas pagination left the original server")
        if next_url in seen:
            raise ValueError("Canvas returned a pagination loop")
        seen.add(next_url)
        resp = get(next_url, params=params) if get else session.get(next_url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            if not all(isinstance(item, dict) for item in data):
                raise ValueError('Canvas returned malformed list items')
            results.extend(data)
        else:
            raise ValueError("Canvas returned an unexpected list response")

        # Follow pagination
        next_url = resp.links.get('next', {}).get('url') if isinstance(resp.links, dict) else None
        params = {}  # params are baked into the Link URL
        link_header = resp.headers.get('Link', '')
        for link in requests.utils.parse_header_links(link_header if isinstance(link_header, str) else ''):
            if link.get('rel') == 'next':
                next_url = urljoin(url, link.get('url', ''))
                break
        if next_url:
            next_url = urljoin(url, next_url)

    return results


def fetch_modules(session: requests.Session, base_url: str, course_id: str) -> list[dict]:
    """Fetch all modules for a course."""
    url = f"{base_url}/api/v1/courses/{course_id}/modules"
    return api_get_paginated(session, url)


def fetch_module_items(session: requests.Session, base_url: str, course_id: str, module_id: int) -> list[dict]:
    """Fetch all items within a module."""
    url = f"{base_url}/api/v1/courses/{course_id}/modules/{module_id}/items"
    return api_get_paginated(session, url)


def fetch_course_name(session: requests.Session, base_url: str, course_id: str) -> str:
    """Fetch the course name."""
    url = f"{base_url}/api/v1/courses/{course_id}"
    resp = session.get(url, timeout=30)
    resp.raise_for_status()
    return resp.json().get('name', f'Course {course_id}')


def download_index(session, output_dir):
    index = vars(session).get('_canvas_archive') if hasattr(session, '__dict__') else None
    return index if isinstance(index, DownloadIndex) else DownloadIndex(output_dir)


def download_error(error):
    status = getattr(getattr(error, 'response', None), 'status_code', None)
    return f"HTTP {status}" if isinstance(status, int) else str(error) if isinstance(error, ValueError) else type(error).__name__


def save_file_info(session, info, output_dir, overwrite=False, identifier=None):
    """Shared identity, validation and streaming for every file collection path."""
    identifier = identifier or str(info['id'])
    index = download_index(session, output_dir)
    key = f'file:{identifier}'
    filename = sanitize_filename(info.get('display_name') or info.get('filename') or f'file-{identifier}')
    version = [info.get('updated_at'), info.get('modified_at'), info.get('size')]
    if index.valid(key, version, force=overwrite):
        index.seen.add(key)
        if index.items[key].get('version') != version:
            index.items[key]['version'] = version
            index.dirty += 1
        path = index.root / index.items[key]['path']
        return path, index.items[key], False
    url = info.get('url')
    if not url or urlparse(url).scheme not in {'https', 'http'}:
        raise ValueError('No download URL')
    path = index.target(key, output_dir, filename)
    with session.get(url, stream=True, timeout=120) as response:
        response.raise_for_status()
        integrity = write_download(path, response, info.get('size'))
    index.record(key, path, version, integrity)
    if vars(session).get('_canvas_archive') is not index:
        index.flush()
    return path, index.items[key], True


def download_file_from_api(session: requests.Session, file_api_url: str, output_dir: str,
                            overwrite: bool = False) -> tuple[bool, str]:
    """Download a file given its Canvas API URL (e.g. /api/v1/files/:id).

    The API returns JSON with a `url` field containing a temporary download link.
    If `overwrite` is True, an existing file at the target path is replaced.
    """
    try:
        index = download_index(session, output_dir)
        key = 'file:' + urlparse(file_api_url).path.rstrip('/').rsplit('/', 1)[-1]
        if not overwrite and key in index.seen and index.valid(key):
            return True, os.path.relpath(index.root / index.items[key]['path'], Path(output_dir).resolve())
        resp = session.get(file_api_url, timeout=30)
        resp.raise_for_status()
        info = resp.json()
        if not isinstance(info, dict):
            raise ValueError('Canvas returned malformed file metadata')
        identifier = str(info.get('id') or file_api_url.rstrip('/').rsplit('/', 1)[-1])
        path, _, _ = save_file_info(session, info, output_dir, overwrite, identifier)
        return True, os.path.relpath(path, Path(output_dir).resolve())
    except (requests.RequestException, ValueError, KeyError, OSError) as error:
        return False, download_error(error)


def download_file_by_url(session: requests.Session, url: str, output_dir: str,
                          overwrite: bool = False) -> tuple[bool, str]:
    """Download a file by direct URL (for embedded file links in pages)."""
    resp = None
    try:
        index = download_index(session, output_dir)
        identifier = file_id(url, url) or hashlib.sha256(url.encode()).hexdigest()
        key = f'file:{identifier}'
        if index.valid(key, force=overwrite):
            return True, os.path.relpath(index.root / index.items[key]['path'], Path(output_dir).resolve())
        # A later run can validate Canvas's version metadata without fetching the
        # same embedded bytes again. Verifier-only links may still need the URL.
        if not overwrite and key in index.items and key not in index.seen and file_id(url, url):
            parsed = urlparse(url)
            ok, saved = download_file_from_api(session,
                f'{parsed.scheme}://{parsed.netloc}/api/v1/files/{identifier}', output_dir)
            if ok:
                return ok, saved
        resp = session.get(url, stream=True, timeout=120, allow_redirects=True)
        if resp.status_code in (401, 403):
            return False, "Authentication failed"
        resp.raise_for_status()

        # Try Content-Disposition header for filename
        filename = None
        cd = resp.headers.get('Content-Disposition', '')
        match = re.search(r'filename[*]?=["\']?(?:UTF-8\'\')?([^"\';]+)', cd)
        if match:
            filename = unquote(match.group(1))

        if not filename:
            parsed = urlparse(url)
            filename = os.path.basename(unquote(parsed.path))

        if not filename or filename in ('download', ''):
            filename = f"file_{hashlib.sha256(url.encode()).hexdigest()[:12]}"

        filename = sanitize_filename(filename)
        filepath = index.target(key, output_dir, filename)
        expected = resp.headers.get('Content-Length')
        expected = int(expected) if isinstance(expected, str) and expected.isdigit() and not resp.headers.get('Content-Encoding') else None
        integrity = write_download(filepath, resp, expected)
        index.record(key, filepath, None, integrity)
        if vars(session).get('_canvas_archive') is not index:
            index.flush()
        filename = os.path.relpath(filepath, Path(output_dir).resolve())

        return True, filename

    except (requests.RequestException, ValueError, OSError) as e:
        return False, download_error(e)
    finally:
        if resp is not None:
            resp.close()


def rewrite_embedded_file_link(href: str, base_url: str) -> str | None:
    """Return the canonical download URL for a Canvas file link, or None.

    Only links pointing at a Canvas file (/files/<id>) on this instance's host
    are rewritten; external links are ignored so they aren't mangled. Links that
    already point at the download endpoint are returned unchanged.
    """
    # urljoin resolves relative and protocol-relative ("//host/...") hrefs, which
    # plain concatenation would mangle into "<base>//host/...".
    href = urljoin(base_url, href)

    parsed = urlparse(href)
    # Only Canvas files on this instance. This guards against external URLs whose
    # path happens to contain "/files/<digits>" (e.g. a government PDF host).
    # Hostnames are case-insensitive, so compare them lowercased.
    if parsed.netloc.lower() != urlparse(base_url).netloc.lower():
        return None
    match = re.search(r'/files/\d+', parsed.path)
    if not match:
        return None

    # Already a download link: leave it (and any verifier query) untouched.
    if re.search(r'/files/\d+/download', parsed.path):
        return href

    # Truncate the path at the file id before appending the download endpoint.
    # Truncating (rather than appending to the whole path) also normalises viewer
    # suffixes such as "/files/<id>/preview", which would otherwise turn into
    # ".../preview/download". Dropping the query avoids a doubled "/download".
    query = parsed.query if 'verifier=' in parsed.query else ''
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path[:match.end()]}/download" + (f'?{query}' if query else '')


def external_link_target(href: str, base_url: str) -> str | None:
    """Return an absolute off-instance http(s) link, or None.

    Links on this Canvas instance are skipped: module items already record those,
    and in-page anchors aren't references worth keeping.
    """
    absolute = urljoin(base_url, href)
    parsed = urlparse(absolute)
    if parsed.scheme not in ('http', 'https'):
        return None
    if parsed.netloc.lower() == urlparse(base_url).netloc.lower():
        return None
    return absolute


def process_page(session: requests.Session, base_url: str, course_id: str,
                 page_url_slug: str, output_dir: str,
                 overwrite: bool = False) -> tuple[bool, str, list[str], list[str]]:
    try:
        return _process_page(session, base_url, course_id, page_url_slug, output_dir, overwrite)
    except (requests.RequestException, ValueError, KeyError, OSError, TypeError) as error:
        return False, download_error(error), [], []


def _process_page(session: requests.Session, base_url: str, course_id: str,
                 page_url_slug: str, output_dir: str,
                 overwrite: bool = False) -> tuple[bool, str, list[str], list[str]]:
    """Fetch a Canvas page via API, save its text, and return its outbound links.

    Returns (success, filename_or_error, embedded_file_urls, external_links).
    """
    url = f"{base_url}/api/v1/courses/{course_id}/pages/{page_url_slug}"
    try:
        resp = session.get(url, timeout=30)
        if resp.status_code in (401, 403):
            return False, "Authentication failed", [], []
        if resp.status_code == 404:
            return False, "Page not found", [], []
        resp.raise_for_status()
    except requests.RequestException as e:
        return False, str(e), [], []

    page_data = resp.json()
    if not isinstance(page_data, dict):
        raise ValueError('Canvas returned malformed page metadata')
    cache = vars(session).setdefault('_canvas_pages', {})
    cache[(str(course_id), page_url_slug)] = page_data
    title = sanitize_filename(page_data.get('title', 'Untitled Page'))
    body_html = page_data.get('body', '')

    if not body_html or not body_html.strip():
        return False, "No content", [], []

    assets, refs = references(body_html, base_url)
    embedded_files = [rewrite_embedded_file_link(url, base_url) for url in assets]
    external_links = [url for url in refs if external_link_target(url, base_url)]
    text = rich_text(body_html, base_url)
    if not text.strip() and not assets:
        return False, "No text content", embedded_files, external_links
    index = download_index(session, output_dir)
    key = f'page:{course_id}:{page_url_slug}'
    filepath = index.target(key, output_dir, f"{title}.txt")
    source = page_data.get('html_url') or f'{base_url}/courses/{course_id}/pages/{page_url_slug}'
    content = f"{page_data.get('title', 'Page')}\nSource: {source}\n\n{text}\n".encode()
    atomic_write(filepath, [content])
    index.record(key, filepath, None, {'size': len(content), 'sha256': hashlib.sha256(content).hexdigest()})
    if fidelity_html(body_html):
        companion = index.target(key + ':html', filepath.parent, filepath.with_suffix('.html').name)
        html = offline_html(body_html, base_url, {}).encode()
        atomic_write(companion, [html])
        index.record(key + ':html', companion, None, {'size': len(html), 'sha256': hashlib.sha256(html).hexdigest()})
    index.flush()
    filename = os.path.relpath(filepath, Path(output_dir).resolve())

    return True, filename, embedded_files, external_links


def process_module_items(session: requests.Session, base_url: str, course_id: str,
                         items: list[dict], module_dir: str, links: list[str],
                         overwrite: bool = False, failed: list | None = None) -> dict:
    """Process all items in a module. Returns stats dict."""
    stats = {'files_ok': 0, 'files_fail': 0, 'pages_ok': 0, 'pages_fail': 0, 'items_skipped': 0}

    for item in items:
        item_type = item.get('type', '')
        title = item.get('title', 'Unknown')

        if item_type == 'File':
            content_id = item.get('content_id')
            if content_id:
                file_api_url = f"{base_url}/api/v1/files/{content_id}"
                success, result = download_file_from_api(session, file_api_url, module_dir, overwrite=overwrite)
                status = "ok" if success else "FAIL"
                print(f"      [File] {result} ({status})")
                if success:
                    stats['files_ok'] += 1
                else:
                    stats['files_fail'] += 1
                    if failed is not None:
                        failed.append(dict(item))
            else:
                stats['files_fail'] += 1
                if failed is not None:
                    failed.append(dict(item))

        elif item_type == 'EmbeddedFile':
            success, _ = download_file_by_url(session, item['url'], module_dir, overwrite=overwrite)
            stats['files_ok' if success else 'files_fail'] += 1
            if not success and failed is not None:
                failed.append(dict(item))

        elif item_type == 'Page':
            page_url_slug = item.get('page_url')
            if page_url_slug:
                success, result, embedded, external = process_page(
                    session, base_url, course_id, page_url_slug, module_dir, overwrite=overwrite)
                status = "ok" if success else "FAIL"
                print(f"      [Page] {result} ({status})")
                if success:
                    stats['pages_ok'] += 1
                else:
                    stats['pages_fail'] += 1
                    if failed is not None:
                        failed.append(dict(item, _skip_embedded=list(set(item.get('_skip_embedded', [])) | set(embedded))))
                # External references in the page body go to links.txt
                for ext in external:
                    links.append(f"[{title}] {ext}")
                # Download embedded files from the page
                for file_url in dict.fromkeys(embedded):
                    if file_url in item.get('_skip_embedded', []):
                        continue
                    fs, fr = download_file_by_url(session, file_url, module_dir, overwrite=overwrite)
                    fstatus = "ok" if fs else "FAIL"
                    print(f"        [Embedded] {fr} ({fstatus})")
                    if fs:
                        stats['files_ok'] += 1
                    else:
                        stats['files_fail'] += 1
                        if failed is not None:
                            failed.append({'type': 'EmbeddedFile', 'url': file_url, 'title': title})
                index = vars(session).get('_canvas_archive') if hasattr(session, '__dict__') else None
                if success and isinstance(index, DownloadIndex):
                    body = vars(session).get('_canvas_pages', {}).get((str(course_id), page_url_slug), {}).get('body', '')
                    if fidelity_html(body):
                        html_key = f'page:{course_id}:{page_url_slug}:html'
                        companion = index.root / index.items[html_key]['path']
                        local = {key[5:]: os.path.relpath(index.root / record['path'], companion.parent)
                                 for key, record in index.items.items() if key.startswith('file:')}
                        html = offline_html(body, base_url, local).encode()
                        atomic_write(companion, [html])
                        index.record(html_key, companion, None, {'size': len(html), 'sha256': hashlib.sha256(html).hexdigest()})
            else:
                stats['pages_fail'] += 1
                if failed is not None:
                    failed.append(dict(item))

        elif item_type == 'ExternalUrl':
            ext_url = item.get('external_url', '')
            if ext_url:
                links.append(f"[{title}] {ext_url}")

        elif item_type == 'ExternalTool':
            ext_url = item.get('external_url', '') or item.get('html_url', '')
            if ext_url:
                links.append(f"[{title}] {ext_url}")

        elif item_type in ('Assignment', 'Discussion', 'Quiz'):
            html_url = item.get('html_url', '')
            if html_url:
                links.append(f"[{title}] {html_url}")

        elif item_type == 'SubHeader':
            pass  # Organizational only

        else:
            stats['items_skipped'] += 1
            html_url = item.get('html_url', '')
            if html_url:
                links.append(f"[{title}] ({item_type}) {html_url}")

    return stats


def validate_canvas_url(url: str) -> bool:
    """Validate that URL looks like a Canvas course URL."""
    try:
        parsed = urlparse(url)
        return bool(parsed.scheme in ('http', 'https') and parsed.hostname
                    and not parsed.username and not parsed.password
                    and parsed.port != 0
                    and re.match(r'^/courses/\d+(?:/|$)', parsed.path))
    except ValueError:
        return False


def main():
    parser = argparse.ArgumentParser(
        description='Download all files and pages from a Canvas LMS course using the REST API.'
    )
    parser.add_argument(
        '--url', '-u',
        required=True,
        help='Canvas course URL (e.g. https://canvas.example.edu/courses/12345)'
    )

    cookie_group = parser.add_mutually_exclusive_group(required=True)
    cookie_group.add_argument(
        '--cookies', '-c',
        help='Path to Netscape format cookie file'
    )
    cookie_group.add_argument(
        '--browser', '-b',
        choices=SUPPORTED_BROWSERS,
        help='Browser to extract cookies from (e.g. firefox, chrome)'
    )

    parser.add_argument(
        '--output', '-o',
        default='./downloads',
        help='Output directory (default: ./downloads)'
    )

    parser.add_argument(
        '--overwrite', '-w',
        action='store_true',
        help='Force refresh of files already tracked by Canvas ID; unrelated files are preserved'
    )

    parser.add_argument('--include-personal', action='store_true', help='Include your own submissions, grades and feedback')
    parser.add_argument('--include-groups', action='store_true', help='Include your groups’ files and pages')
    args = parser.parse_args()

    if not validate_canvas_url(args.url):
        print("Error: URL doesn't look like a Canvas course URL.")
        print("Expected format: https://canvas.example.edu/courses/12345")
        sys.exit(1)

    # Parse URL
    base_url, course_id = parse_course_url(args.url)

    try:
        if args.browser:
            cookie_jar = load_cookies_from_browser(args.browser, urlparse(args.url).hostname)
        else:
            cookie_jar = load_cookies(args.cookies)
        with create_session(cookie_jar) as session:
            # Fetch course name
            print("Fetching course info...")
            course_name = sanitize_filename(fetch_course_name(session, base_url, course_id))
            print(f"Course: {course_name}")

            # Create output directory
            course_dir = os.path.join(args.output, course_name)
            import json
            marker = Path(course_dir) / '.sync-state.json'
            if marker.is_file():
                stored = json.loads(marker.read_text())
                if stored.get('course_id', course_id) != course_id or stored.get('origin', base_url) != base_url:
                    course_dir += f' [Canvas {course_id}-{hashlib.sha256(base_url.encode()).hexdigest()[:6]}]'
            os.makedirs(course_dir, exist_ok=True)
            from canvas_sync import Sync
            guard = Sync(session, base_url, course_dir)
            guard.bind_account()
            guard.state['course_id'] = course_id
            guard.save()

            # Fetch all modules
            print("Fetching modules...")
            module_list_failed = False
            try:
                modules = fetch_modules(session, base_url, course_id)
            except (requests.RequestException, ValueError):
                modules, module_list_failed = [], True
            print(f"Found {len(modules)} modules")

            from course_archive import archive_modules, complete_course
            with course_lock(session, course_dir):
                result = archive_modules(session, base_url, course_id, modules, course_dir,
                                         overwrite=args.overwrite)
                if module_list_failed:
                    result.module_errors.append('Module list')
                    result.pending.append({'module_list': True})
                result = complete_course(session, base_url, course_id, course_dir, result, course_name,
                                         args.include_personal, args.include_groups)
            print("\nFinished with some errors." if result.partial else "\nDone!")
            print(f"  Files downloaded: {result.files_ok}")
            print(f"  Files failed:     {result.files_fail}")
            print(f"  Pages saved:      {result.pages_ok}")
            print(f"  Pages failed:     {result.pages_fail}")
            print(f"  Modules failed:   {len(result.module_errors)}")
            print(f"  Links collected:  {len(result.links)}")
            print(f"  Output:           {course_dir}")
            if result.partial:
                raise SystemExit(1)
    except (requests.RequestException, ValueError, OSError) as error:
        print(f"Error: Download could not finish ({type(error).__name__}). Check your connection, access, and output folder.")
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
