# Canvas Course Downloader

Create a local archive of the Canvas material your account can access. The CLI,
local web app, background sync, and MCP connector share the same course-wide
collector: module folders remain useful for navigation, while a course with no
modules is still archived.

The archive includes accessible files and embedded assets, page text, syllabus,
assignments and rubrics, quiz metadata/instructions, calendar events,
announcements, and discussions. Rich Canvas bodies retain links, table rows, and
image/equation alt text; content that needs layout also receives an inert HTML
companion. It does not retrieve restricted quiz answers, revision history,
Inbox, OCR/transcription, or crawl media providers and other external sites.

## Choose an interface

- **CLI:** make a single course archive on demand.
- **Local web UI:** make a temporary ZIP with a form and failed-only retry.
- **MCP connector:** keep selected courses available to ChatGPT or Claude, with
  periodic local sync. It retains its nine tool names and read-only archive access.

## Install and authentication

Python 3.10+ supports the CLI and web app. The connector requires Python 3.11+.
Use a private environment appropriate to the interface:

```sh
python3 -m venv venv
. venv/bin/activate
python -m pip install -r requirements.txt          # CLI + local web app
# or: python -m pip install -r requirements-mcp.txt # CLI + sync + MCP connector
```

`requirements-core.txt` contains the CLI/sync dependencies; `requirements.txt`
adds Flask; `requirements-mcp.txt` adds FastMCP and PDF extraction without Flask.
If you use both the web app and connector, reuse the full `.venv-mcp` environment
created by `./setup.sh`, then add the web requirements to that same environment.
Do not remove legacy environments or existing user archives automatically.

Canvas authentication uses either browser cookies or a Netscape-format
`cookies.txt` export. Supported browsers are Chrome, Firefox, Opera, Edge,
Chromium, Brave, Vivaldi, and Safari. If browser-cookie access fails, close the
browser or use a fresh cookie export.

## CLI

```sh
python canvas_downloader.py --url "https://canvas.example.edu/courses/12345" --browser firefox
python canvas_downloader.py --url "https://canvas.example.edu/courses/12345" --cookies cookies.txt
python canvas_downloader.py --url "https://canvas.example.edu/courses/12345" --browser chrome --output ./my-course
python canvas_downloader.py --url "https://canvas.example.edu/courses/12345" --browser firefox --overwrite
```

Use `--include-personal` to add your own submissions, grades, and feedback, and
`--include-groups` to add your groups’ files and pages. Both are opt-in:

```sh
python canvas_downloader.py --url "https://canvas.example.edu/courses/12345" \
  --browser firefox --include-personal --include-groups
```

Files have stable Canvas identities. Repeated references reuse one saved copy;
renames retain their established path; local text edits and failed streaming
downloads are preserved rather than silently replaced.
`--overwrite` forces a refresh of tracked files. Partial archives return a nonzero
CLI exit status; already saved material is retained.

## Local web app

```sh
python app.py
```

Open <http://127.0.0.1:5000>, choose browser cookies or upload `cookies.txt`, and
optionally expand **Additional course material** for personal or group content.
Uploaded cookie files are removed after parsing. ZIP URLs are opaque, local, and
available for one hour while the process runs.

Partial results retain saved content and can retry only recorded failures. Retry
state is in memory, bound to the originating browser session, and expires after
one hour; cookie-file retries require another upload. A restart loses that retry
queue but does not delete user archives.

## Archive contents and coverage

Course-wide material is stored alongside module folders, for example:

```text
Course Name/
  Week 1/
    lecture.pdf
    Intro.txt
    Intro.html
  Canvas Sync/
    Assignments/
    Announcements/
    Discussions/
    Files/
    Pages/
    Quizzes/
  links.txt
  archive-status.json
```

`archive-status.json` records scope, partial failures, unavailable modules, and
locked or unsupported content. Missing/locked Canvas items remain represented by
availability metadata; previously saved bytes are not deleted. External links are
references in `links.txt`, not downloaded websites.

## ChatGPT and Claude connector

Run the guided setup after logging into Canvas in Firefox:

```sh
git clone https://github.com/elliott-leow/canvas-downloader.git
cd canvas-downloader
./setup.sh
```

It creates `.venv-mcp`, stores private local settings, and guides Canvas URL,
browser, OAuth/token mode, and an optional temporary Cloudflare HTTPS endpoint.
For durable connector, OAuth, tunnel, and macOS login-service instructions, see
[MAC_CONNECTOR_SETUP.md](MAC_CONNECTOR_SETUP.md). Token mode is for Claude/local
testing; the public ChatGPT route uses OAuth.

Connector sync keeps active courses visible and past courses hidden by default.
Missing courses remain in catalog metadata instead of being erased. Configure
optional personal/group collection during setup or later:

```sh
./setup.sh configure --include-personal --include-groups
```

The MCP archive interface searches extracted text only. `list_files` retains its
string file IDs and adds a readable inventory: unsupported files and files over
25 MiB are discoverable but cannot be fetched or searched. Extraction includes
ordinary text/code/CSV/JSON/HTML, PDFs, DOCX headers/footers/footnotes/endnotes/
comments, and PPTX speaker notes. Diagrams and scanned text may still be absent.

## Development

Install both requirement sets before the full suite:

```sh
.venv-mcp/bin/python -m pip install -r requirements.txt -r requirements-mcp.txt
.venv-mcp/bin/python -m unittest discover -v
```

There is no `lxml` dependency or frontend build step. See
[docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) for the source map and verification
boundaries.

## License

MIT
