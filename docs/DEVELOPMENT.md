# Development

Use Python 3.11+ for the full connector. CLI and web paths support Python 3.10+.
The following uses the connector environment for the complete test matrix; it is
also the practical environment to reuse when working on both apps.

```sh
.venv-mcp/bin/python -m pip install -r requirements.txt -r requirements-mcp.txt
.venv-mcp/bin/python -m unittest discover -v
.venv-mcp/bin/python -m compileall -q *.py
bash -n setup.sh
```

`requirements-core.txt` is CLI/sync only. `requirements.txt` is core plus Flask;
`requirements-mcp.txt` is core plus FastMCP/PDF extraction and deliberately does
not install Flask. `lxml` is not required. Do not delete existing environments,
downloads, `output/`, or user archives during cleanup.

The web app listens on `127.0.0.1:5000`; debug mode is opt-in with
`FLASK_DEBUG=1`. No frontend build, CDN, or external font service is required.

## Source map

| File | Responsibility |
| --- | --- |
| `canvas_downloader.py` | Cookies, HTTP/session handling, stable file identity, module CLI flow |
| `course_archive.py` | Shared module flow, course-wide collector handoff, result/retry status |
| `canvas_sync.py` | Incremental catalog, availability metadata, course-wide sync collector |
| `canvas_content.py` | Rich-text conversion, Canvas asset references, inert HTML companions |
| `archive_io.py` | Containment checks, atomic writes, download validation and index state |
| `app.py` | Local form, CSRF/session checks, temporary ZIPs and failed-only retry |
| `canvas_mcp.py` | Nine connector tools, bounded extraction/cache, text-only search/inventory |
| `connector_config.py`, `connector_auth.py` | Private settings and owner-restricted OAuth |
| `connector_setup.py`, `mac_services.py`, `setup.sh` | Guided setup, tunnels, and macOS services |

Keep public entrypoint names, sync-state formats, and MCP tool schemas compatible.

## Verification boundaries

The suite uses temporary archives and mocked Canvas responses. It checks course
coverage with and without modules, identity reuse, local-edit preservation,
download integrity, retry call bounds, archive ZIP contents, form/session
protection, course visibility, rich text, discussion trees, MCP extraction/search,
and cache bounds. CI runs it on Python 3.11–3.13 with compilation and shell checks.

Passing tests do not prove a live Canvas login, account permissions, OAuth consent,
tunnel reachability, browser-cookie access, startup service persistence, or a reboot.

## Data and behavior boundaries

- Archive collection includes accessible files/assets, pages, syllabus, assignments
  with rubrics, quiz metadata/instructions, calendar items, announcements, and
  discussions. Personal submissions/grades/feedback and group files/pages require
  explicit opt-in (`--include-personal`, `--include-groups`, or setup config).
- Do not claim or add restricted quiz answers, historical revisions, Inbox,
  OCR/transcription, or external/media-provider crawling.
- `archive-status.json` records coverage and partial/locked/unavailable sources.
  Missing courses and items retain stale availability metadata and saved files.
- Web retry is failed-only, in memory, session-bound, and expires after one hour.
  It does not survive an app restart. Browser authentication is reacquired; uploaded
  cookie files are supplied again and removed after parsing.
- Connector inventory exposes unsupported and over-25-MiB files without allowing
  extraction. Search/fetch are text-only; DOCX supplementary text and PPTX speaker
  notes are extracted, but diagrams and scanned pages may not be.
- Connector text cache is bounded to 128 entries / 8 million characters. Course
  visibility and path containment are checked before cached content is returned.
