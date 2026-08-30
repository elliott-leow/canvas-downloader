#!/usr/bin/env python3
"""
Canvas Course Downloader

Downloads all files and pages from a Canvas LMS course using the REST API.
Organizes content by module into subdirectories.
"""

import argparse
import http.cookiejar
import os
import re
import sys
from urllib.parse import urlparse, unquote

import requests
from bs4 import BeautifulSoup

try:
    import lxml  # noqa: F401
    HTML_PARSER = 'lxml'
except ImportError:
    HTML_PARSER = 'html.parser'


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
        print(f"Error: Unsupported browser '{browser_name}'.")
        print(f"Supported browsers: {', '.join(SUPPORTED_BROWSERS)}")
        sys.exit(1)

    try:
        cookie_jar = func(domain_name=domain) if domain else func()
        return cookie_jar
    except Exception as e:
        print(f"Error loading cookies from {browser_name}: {e}")
        print("\nMake sure the browser is installed and you're logged into Canvas.")
        sys.exit(1)


def load_cookies(cookie_file: str) -> http.cookiejar.MozillaCookieJar:
    """Load cookies from a Netscape format cookie file."""
    cookie_jar = http.cookiejar.MozillaCookieJar(cookie_file)
    try:
        cookie_jar.load(ignore_discard=True, ignore_expires=True)
    except FileNotFoundError:
        print(f"Error: Cookie file '{cookie_file}' not found.")
        print("\nTo get your cookies:")
        print("1. Install a browser extension like 'Get cookies.txt LOCALLY'")
        print("2. Log into Canvas")
        print("3. Export cookies for the Canvas domain")
        print("4. Save to a file and pass it with --cookies")
        sys.exit(1)
    except Exception as e:
        print(f"Error loading cookies: {e}")
        sys.exit(1)
    return cookie_jar


def create_session(cookie_jar: http.cookiejar.CookieJar) -> requests.Session:
    """Create a requests session with cookies loaded."""
    session = requests.Session()
    session.cookies = cookie_jar
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    })
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
    name = name.strip().strip('.')[:200]
    # Avoid Windows reserved names (CON, PRN, NUL, COM1, etc.)
    if name.split('.')[0].upper() in WINDOWS_RESERVED_NAMES:
        name = f"_{name}"
    return name


def parse_course_url(url: str) -> tuple[str, str]:
    """Extract base_url and course_id from a Canvas course URL.

    Returns (base_url, course_id).
    """
    parsed = urlparse(url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    match = re.search(r'/courses/(\d+)', parsed.path)
    if not match:
        print(f"Error: Could not extract course ID from URL: {url}")
        print("Expected format: https://canvas.example.edu/courses/12345")
        sys.exit(1)
    return base_url, match.group(1)


def api_get_paginated(session: requests.Session, url: str, params: dict = None) -> list:
    """GET a Canvas API endpoint, handling pagination via Link headers.

    Returns the combined list of JSON results across all pages.
    """
    if params is None:
        params = {}
    params.setdefault('per_page', 100)

    results = []
    next_url = url

    while next_url:
        resp = session.get(next_url, params=params, timeout=30)
        if resp.status_code in (401, 403):
            print(f"Error: Authentication failed (HTTP {resp.status_code}).")
            print("Your session may have expired. Please export fresh cookies.")
            sys.exit(1)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            results.extend(data)
        else:
            results.append(data)

        # Follow pagination
        next_url = None
        params = {}  # params are baked into the Link URL
        link_header = resp.headers.get('Link', '')
        for part in link_header.split(','):
            if 'rel="next"' in part:
                next_url = part.split('<')[1].split('>')[0].strip()
                break

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
    if resp.status_code in (401, 403):
        print(f"Error: Authentication failed (HTTP {resp.status_code}).")
        sys.exit(1)
    resp.raise_for_status()
    return resp.json().get('name', f'Course {course_id}')


def download_file_from_api(session: requests.Session, file_api_url: str, output_dir: str,
                            overwrite: bool = False) -> tuple[bool, str]:
    """Download a file given its Canvas API URL (e.g. /api/v1/files/:id).

    The API returns JSON with a `url` field containing a temporary download link.
    If `overwrite` is True, an existing file at the target path is replaced.
    """
    try:
        resp = session.get(file_api_url, timeout=30)
        if resp.status_code in (401, 403):
            return False, "Authentication failed"
        resp.raise_for_status()
        file_info = resp.json()

        download_url = file_info.get('url')
        filename = sanitize_filename(file_info.get('display_name') or file_info.get('filename', 'unknown'))

        if not download_url:
            return False, f"No download URL for {filename}"

        filepath = os.path.join(output_dir, filename)

        if not overwrite:
            base, ext = os.path.splitext(filename)
            counter = 1
            while os.path.exists(filepath):
                filename = f"{base}_{counter}{ext}"
                filepath = os.path.join(output_dir, filename)
                counter += 1

        dl_resp = session.get(download_url, stream=True, timeout=120)
        dl_resp.raise_for_status()
        with open(filepath, 'wb') as f:
            for chunk in dl_resp.iter_content(chunk_size=8192):
                f.write(chunk)

        return True, filename

    except requests.RequestException as e:
        return False, str(e)


def download_file_by_url(session: requests.Session, url: str, output_dir: str,
                          overwrite: bool = False) -> tuple[bool, str]:
    """Download a file by direct URL (for embedded file links in pages)."""
    try:
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
            filename = f"file_{hash(url) % 10000}"

        filename = sanitize_filename(filename)
        filepath = os.path.join(output_dir, filename)

        if not overwrite:
            base, ext = os.path.splitext(filename)
            counter = 1
            while os.path.exists(filepath):
                filename = f"{base}_{counter}{ext}"
                filepath = os.path.join(output_dir, filename)
                counter += 1

        with open(filepath, 'wb') as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)

        return True, filename

    except requests.RequestException as e:
        return False, str(e)


def rewrite_embedded_file_link(href: str, base_url: str) -> str | None:
    """Return the canonical download URL for a Canvas file link, or None.

    Only links pointing at a Canvas file (/files/<id>) on this instance's host
    are rewritten; external links are ignored so they aren't mangled. Links that
    already point at the download endpoint are returned unchanged.
    """
    if href.startswith('/'):
        href = base_url + href

    parsed = urlparse(href)
    # Only Canvas files on this instance. This guards against external URLs whose
    # path happens to contain "/files/<digits>" (e.g. a government PDF host).
    if parsed.netloc != urlparse(base_url).netloc:
        return None
    if not re.search(r'/files/\d+', parsed.path):
        return None

    # Already a download link: leave it (and any verifier query) untouched.
    if re.search(r'/files/\d+/download', parsed.path):
        return href

    # Append the download endpoint, dropping any query string so we don't end up
    # with a doubled "/download/download".
    path = parsed.path.rstrip('/')
    return f"{parsed.scheme}://{parsed.netloc}{path}/download"


def process_page(session: requests.Session, base_url: str, course_id: str,
                 page_url_slug: str, output_dir: str,
                 overwrite: bool = False) -> tuple[bool, str, list[str]]:
    """Fetch a Canvas page via API, save its text, and return embedded file URLs.

    Returns (success, filename_or_error, list_of_embedded_file_urls).
    """
    url = f"{base_url}/api/v1/courses/{course_id}/pages/{page_url_slug}"
    try:
        resp = session.get(url, timeout=30)
        if resp.status_code in (401, 403):
            return False, "Authentication failed", []
        if resp.status_code == 404:
            return False, "Page not found", []
        resp.raise_for_status()
    except requests.RequestException as e:
        return False, str(e), []

    page_data = resp.json()
    title = sanitize_filename(page_data.get('title', 'Untitled Page'))
    body_html = page_data.get('body', '')

    if not body_html or not body_html.strip():
        return False, "No content", []

    # Parse HTML body
    soup = BeautifulSoup(body_html, HTML_PARSER)

    # Extract embedded file links
    embedded_files = []
    for a_tag in soup.find_all('a', href=True):
        download_url = rewrite_embedded_file_link(a_tag['href'], base_url)
        if download_url:
            embedded_files.append(download_url)

    # Convert to text
    for elem in soup.find_all(['script', 'style']):
        elem.decompose()
    text = soup.get_text(separator='\n', strip=True)

    if not text.strip():
        return False, "No text content", embedded_files

    filename = f"{title}.txt"
    filepath = os.path.join(output_dir, filename)

    if not overwrite:
        counter = 1
        while os.path.exists(filepath):
            filename = f"{title}_{counter}.txt"
            filepath = os.path.join(output_dir, filename)
            counter += 1

    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(text)

    return True, filename, embedded_files


def process_module_items(session: requests.Session, base_url: str, course_id: str,
                         items: list[dict], module_dir: str, links: list[str],
                         overwrite: bool = False) -> dict:
    """Process all items in a module. Returns stats dict."""
    stats = {'files_ok': 0, 'files_fail': 0, 'pages_ok': 0, 'pages_fail': 0}

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

        elif item_type == 'Page':
            page_url_slug = item.get('page_url')
            if page_url_slug:
                success, result, embedded = process_page(
                    session, base_url, course_id, page_url_slug, module_dir, overwrite=overwrite)
                status = "ok" if success else "FAIL"
                print(f"      [Page] {result} ({status})")
                if success:
                    stats['pages_ok'] += 1
                else:
                    stats['pages_fail'] += 1
                # Download embedded files from the page
                for file_url in embedded:
                    fs, fr = download_file_by_url(session, file_url, module_dir, overwrite=overwrite)
                    fstatus = "ok" if fs else "FAIL"
                    print(f"        [Embedded] {fr} ({fstatus})")
                    if fs:
                        stats['files_ok'] += 1
                    else:
                        stats['files_fail'] += 1

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
            html_url = item.get('html_url', '')
            if html_url:
                links.append(f"[{title}] ({item_type}) {html_url}")

    return stats


def validate_canvas_url(url: str) -> bool:
    """Validate that URL looks like a Canvas course URL."""
    return bool(re.search(r'/courses/\d+', url))


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
        help='Overwrite existing files instead of appending _1, _2, ... suffixes'
    )

    args = parser.parse_args()

    if not validate_canvas_url(args.url):
        print("Error: URL doesn't look like a Canvas course URL.")
        print("Expected format: https://canvas.example.edu/courses/12345")
        sys.exit(1)

    # Parse URL
    base_url, course_id = parse_course_url(args.url)

    # Load cookies
    if args.browser:
        domain = urlparse(args.url).netloc
        print(f"Loading cookies from {args.browser} (domain: {domain})...")
        cookie_jar = load_cookies_from_browser(args.browser, domain)
    else:
        print(f"Loading cookies from {args.cookies}...")
        cookie_jar = load_cookies(args.cookies)

    session = create_session(cookie_jar)

    # Fetch course name
    print(f"Fetching course info...")
    course_name = sanitize_filename(fetch_course_name(session, base_url, course_id))
    print(f"Course: {course_name}")

    # Create output directory
    course_dir = os.path.join(args.output, course_name)
    os.makedirs(course_dir, exist_ok=True)

    # Fetch all modules
    print(f"Fetching modules...")
    modules = fetch_modules(session, base_url, course_id)
    print(f"Found {len(modules)} modules")

    if not modules:
        print("No modules found. The course may be empty or you may not have access.")
        sys.exit(0)

    # Track everything
    total_stats = {'files_ok': 0, 'files_fail': 0, 'pages_ok': 0, 'pages_fail': 0}
    all_links = []

    for module in modules:
        module_name = sanitize_filename(module.get('name', 'Unnamed Module'))
        module_id = module['id']
        print(f"\n  [{module_name}]")

        module_dir = os.path.join(course_dir, module_name)
        os.makedirs(module_dir, exist_ok=True)

        items = fetch_module_items(session, base_url, course_id, module_id)
        print(f"    {len(items)} items")

        if not items:
            continue

        # Track which module links came from
        module_links = []
        stats = process_module_items(session, base_url, course_id, items, module_dir, module_links, overwrite=args.overwrite)

        # Add module context to links
        for link in module_links:
            all_links.append(f"({module_name}) {link}")

        for key in total_stats:
            total_stats[key] += stats[key]

    # Save links
    if all_links:
        links_path = os.path.join(course_dir, 'links.txt')
        with open(links_path, 'w', encoding='utf-8') as f:
            for link in all_links:
                f.write(link + '\n')
        print(f"\nSaved {len(all_links)} links to links.txt")

    # Summary
    print(f"\nDone!")
    print(f"  Files downloaded: {total_stats['files_ok']}")
    print(f"  Files failed:     {total_stats['files_fail']}")
    print(f"  Pages saved:      {total_stats['pages_ok']}")
    print(f"  Pages failed:     {total_stats['pages_fail']}")
    print(f"  Links collected:  {len(all_links)}")
    print(f"  Output:           {course_dir}")


if __name__ == '__main__':
    main()
