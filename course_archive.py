"""One module archive workflow for the CLI and local web interface."""
from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Callable

import requests

from archive_io import atomic_write, DownloadIndex, safe_path, course_lock
from canvas_downloader import fetch_module_items, fetch_modules, process_module_items, sanitize_filename


@dataclass
class ArchiveResult:
    files_ok: int = 0
    files_fail: int = 0
    pages_ok: int = 0
    pages_fail: int = 0
    modules_ok: int = 0
    module_errors: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    pending: list[dict] = field(default_factory=list)
    items_skipped: int = 0
    content_errors: list[dict] = field(default_factory=list)
    skipped_locked: int = 0
    unavailable_sources: list[dict] = field(default_factory=list)
    collector: object = field(default=None, repr=False)
    module_index: list[dict] = field(default_factory=list)

    @property
    def partial(self):
        return bool(self.files_fail or self.pages_fail or self.module_errors or self.content_errors or self.items_skipped)


def archive_modules(session, base_url, course_id, modules, output_dir,
                    overwrite=False, report: Callable[[str], None] = print):
    with course_lock(session, output_dir):
        return _archive_modules(session, base_url, course_id, modules, output_dir, overwrite, report)


def _archive_modules(session, base_url, course_id, modules, output_dir,
                     overwrite=False, report=print):
    """Archive every accessible module, retaining explicit partial-failure counts."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    result = ArchiveResult()
    if session is not None:
        session._canvas_archive = DownloadIndex(output_dir)
    identity_path = output_dir / '.sync-state.json'
    if identity_path.is_file():
        identity = json.loads(identity_path.read_text())
        if identity.get('origin', base_url) != base_url or str(identity.get('course_id', course_id)) != str(course_id):
            raise ValueError('Use a separate archive for a different Canvas course or host')
    links_path = output_dir / 'links.txt'
    result.links = links_path.read_text().splitlines() if links_path.is_file() else []
    used_names = set()
    for module in modules:
        module_name = sanitize_filename(module.get('name') or 'Unnamed Module')
        # Distinct Canvas modules can have the same display name.
        candidate = module_name
        suffix = 1
        while candidate.casefold() in used_names:
            candidate = f"{module_name} [{module['id']}-{suffix}]"
            suffix += 1
        used_names.add(candidate.casefold())
        directory = safe_path(output_dir, candidate)
        directory.mkdir(exist_ok=True)
        report(f"\n  [{candidate}]")
        try:
            items = fetch_module_items(session, base_url, course_id, module['id'])
        except (SystemExit, requests.RequestException, ValueError):
            result.module_errors.append(candidate)
            result.pending.append({'module': candidate, 'module_id': module['id']})
            report("    Could not fetch module items; continuing with other modules.")
            continue
        links = []
        failed = []
        result.module_index.append({'id': module['id'], 'name': module_name,
                                    'position': module.get('position'), 'items': items})
        stats = process_module_items(session, base_url, course_id, items, str(directory),
                                     links, overwrite=overwrite, failed=failed)
        result.pending.extend({'module': candidate, 'item': item} for item in failed)
        result.modules_ok += 1
        for key, value in stats.items():
            setattr(result, key, getattr(result, key) + value)
        result.links = [link for link in result.links if not link.startswith(f'({candidate}) ')]
        result.links.extend(dict.fromkeys(f"({candidate}) {link}" for link in links))
    if result.links:
        atomic_write(output_dir / 'links.txt', [("\n".join(result.links) + "\n").encode()])
    if session is not None:
        session._canvas_archive.flush()
    return result


def retry_failed(session, base_url, course_id, output_dir, result):
    with course_lock(session, output_dir):
        return _retry_failed(session, base_url, course_id, output_dir, result)


def _retry_failed(session, base_url, course_id, output_dir, result):
    """Retry only recorded failures; successful files/pages are never requested again."""
    output_dir = Path(output_dir)
    if result.collector:
        result.collector.session = session
        result.collector.bind_account()
    if session is not None:
        session._canvas_archive = result.collector.downloads if result.collector else DownloadIndex(output_dir)
    for task in list(result.pending):
        if task.get('collector'):
            continue
        if task.get('module_list'):
            try:
                modules = fetch_modules(session, base_url, course_id)
            except (requests.RequestException, ValueError):
                continue
            recovered = archive_modules(session, base_url, course_id, modules, output_dir)
            result.pending.remove(task)
            result.module_errors.remove('Module list')
            result.pending.extend(recovered.pending)
            result.module_errors.extend(recovered.module_errors)
            result.module_index.extend(recovered.module_index)
            result.links = recovered.links
            result.files_fail += recovered.files_fail
            result.pages_fail += recovered.pages_fail
            if result.collector:
                result.collector.downloads = session._canvas_archive
            continue
        module = task['module']
        if 'module_id' in task:
            try:
                items = fetch_module_items(session, base_url, course_id, task['module_id'])
            except (SystemExit, requests.RequestException, ValueError):
                continue
        else:
            items = [task['item']]
        links, failed = [], []
        stats = process_module_items(session, base_url, course_id, items,
                                     str(output_dir / module), links, failed=failed)
        result.pending.remove(task)
        if 'module_id' in task:
            result.module_errors.remove(module)
            result.modules_ok += 1
        else:
            counter = 'pages_fail' if task['item']['type'] == 'Page' else 'files_fail'
            setattr(result, counter, getattr(result, counter) - 1)
        for key, value in stats.items():
            setattr(result, key, getattr(result, key) + value)
        result.pending.extend({'module': module, 'item': item} for item in failed)
        for link in links:
            contextual = f"({module}) {link}"
            if contextual not in result.links:
                result.links.append(contextual)
    if result.links:
        atomic_write(output_dir / 'links.txt', [("\n".join(result.links) + "\n").encode()])
    if result.collector:
        result.collector.session = session
        session._canvas_archive = result.collector.downloads
        result.collector.retry()
        update_collector_result(result, output_dir)
    if session is not None:
        session._canvas_archive.flush()
    return result


def update_collector_result(result, output_dir):
    collector = result.collector
    result.content_errors = list(collector.status['errors'])
    result.skipped_locked = collector.status['skipped_locked']
    result.unavailable_sources = list(collector.status['unavailable_sources'])
    from canvas_content import file_id
    pending = []
    for task in result.pending:
        if task.get('collector'):
            continue
        item = task.get('item', {})
        key = None
        if item.get('type') == 'File':
            key = f'file:{item.get("content_id")}'
        elif item.get('type') == 'EmbeddedFile':
            key = f'file:{file_id(item.get("url", ""), collector.base)}'
        elif item.get('type') == 'Page':
            key = f'page:{collector.course_id}:{item.get("page_url")}'
        if key in collector.downloads.seen:
            counter = 'pages_fail' if item.get('type') == 'Page' else 'files_fail'
            setattr(result, counter, max(0, getattr(result, counter) - 1))
        else:
            pending.append(task)
    result.pending = pending
    result.pending.extend({'collector': True, 'key': key} for key in collector.failed)
    lines = []
    for position, module in enumerate(result.module_index, 1):
        lines.append(f'{position}. {module["name"]}')
        for index, item in enumerate(module['items'], 1):
            lines.append(f'  {index}. {item.get("type", "Unknown")}: {item.get("title", "Untitled")}\n'
                         f'     {item.get("external_url") or item.get("html_url", "")}')
    if lines:
        atomic_write(Path(output_dir) / 'module-index.txt', [('\n'.join(lines) + '\n').encode()])
    # Count actual saved files, rather than every repeated module reference.
    visible = [path for path in Path(output_dir).rglob('*') if path.is_file()
               and not any(p.startswith('.') for p in path.relative_to(output_dir).parts)
               and path.name not in {'archive-status.json', 'links.txt', 'module-index.txt'}]
    downloaded = {record['path'] for key, record in collector.downloads.items.items() if key.startswith('file:')}
    result.files_ok = sum(path.relative_to(output_dir).as_posix() in downloaded for path in visible)
    result.pages_ok = sum(path.suffix.lower() == '.txt' and path.relative_to(output_dir).as_posix() not in downloaded for path in visible)
    summary = {'scope': 'Accessible course materials; no external-site crawling or restricted quiz answers',
               'partial': result.partial, 'files': result.files_ok, 'text_records': result.pages_ok,
               'skipped_locked': result.skipped_locked, 'unsupported_items': result.items_skipped,
               'unavailable_sources': result.unavailable_sources,
               'errors': result.content_errors, 'unavailable_modules': result.module_errors,
               'finished_at': collector.status.get('finished_at'),
               'limitations': ['Historical revisions, inbox, rosters and administrative settings are excluded.',
                               'External tools and media are references unless exposed as Canvas files.',
                               'Original rich layouts are retained as inert HTML where needed.']}
    atomic_write(Path(output_dir) / 'archive-status.json', [json.dumps(summary, indent=2).encode()])


def complete_course(session, base_url, course_id, output_dir, result, title,
                    include_personal=False, include_groups=False):
    """Add course-wide coverage with exactly the same collector used by background sync."""
    from canvas_sync import Sync, AuthenticationExpired
    collector = Sync(session, base_url, output_dir, include_personal=include_personal,
                     include_groups=include_groups)
    collector.downloads = getattr(session, '_canvas_archive', None)
    if not isinstance(collector.downloads, DownloadIndex):
        collector.downloads = DownloadIndex(output_dir)
    session._canvas_archive = collector.downloads
    result.collector = collector
    collector.course_id = str(course_id)
    def course():
        collector.bind_account()
        data = collector.get(f'{base_url}/api/v1/courses/{course_id}', params={'include[]': 'syllabus_body'}).json()
        collector.course(dict(data, id=int(course_id), name=data.get('name') or title), folder='.', modules=False)
    try:
        collector.attempt(course_id, 'course-details', course)
    except AuthenticationExpired as error:
        collector.record_error(course_id, 'course-details', error)
        collector.failed[f'{course_id}:course-details'] = (str(course_id), 'course-details', course)
    collector.finish()
    update_collector_result(result, output_dir)
    return result
