# Canvas Course Downloader

Back up your entire Canvas LMS course locally -- all files, pages, and links, organized by module.

Built for students who want to preserve class materials and build a local context bank for AI tools like [Claude Code](https://docs.anthropic.com/en/docs/claude-code) to help with assignments, studying, and review.

Works with any Canvas LMS instance.

## Features

- Downloads all files from every module (PDFs, slides, documents, etc.)
- Saves Canvas page text content as `.txt` files
- Extracts embedded file links from pages
- Collects external links (assignments, quizzes, external tools) into `links.txt`
- Organizes everything by module folder -- ready to use as context for AI assistants
- Two interfaces: **CLI** and **Web UI**

## Setup

```bash
git clone https://github.com/elliott-leow/canvas-downloader.git
cd canvas-downloader
python -m venv venv
source venv/bin/activate  # Linux/macOS
# venv\Scripts\activate   # Windows
pip install -r requirements.txt
```

## Authentication

Canvas requires authentication. There are two options:

### Option A: Auto-fetch from browser (easiest)

Just log into Canvas in your browser, then use the `--browser` flag. The tool reads cookies directly from your browser -- no extensions or extra steps needed.

```bash
python canvas_downloader.py --url "https://canvas.example.edu/courses/12345" --browser firefox
```

**Supported browsers:** chrome, firefox, opera, edge, chromium, brave, vivaldi, safari

> You may need to close the browser first -- some browsers lock their cookie database while running.

### Option B: Export a cookies file

If auto-fetch doesn't work for your setup, you can export cookies manually:

1. Install [Get cookies.txt LOCALLY](https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc) (Chrome) or a similar extension
2. Log into your Canvas site
3. Click the extension icon on the Canvas page and export cookies
4. Save as `cookies.txt`

```bash
python canvas_downloader.py --url "https://canvas.example.edu/courses/12345" --cookies cookies.txt
```

## Usage

### CLI

```bash
# Auto-fetch cookies from browser
python canvas_downloader.py --url "https://canvas.example.edu/courses/12345" --browser firefox

# Using a cookies file
python canvas_downloader.py --url "https://canvas.example.edu/courses/12345" --cookies cookies.txt

# Custom output directory
python canvas_downloader.py --url "https://canvas.example.edu/courses/12345" --browser chrome --output ./my-course
```

### Web UI

```bash
python app.py
```

Open http://localhost:5000 in your browser. Paste the course URL, pick your browser (or upload a cookies file), and hit Download. Everything gets packaged into a ZIP.

## Output Structure

```
downloads/
  Course Name/
    Module 1/
      lecture-slides.pdf
      reading-list.txt
      assignment.docx
    Module 2/
      lab-instructions.pdf
      ...
    links.txt
```

- **Files** are downloaded with their original names
- **Canvas pages** are saved as `.txt` files
- **External links** (assignments, quizzes, external tools) are collected in `links.txt`

## Requirements

- Python 3.10+
- An active Canvas LMS session (you must be logged in)

## Troubleshooting

**"Authentication failed (HTTP 401/403)"**
Your cookies have expired. Log into Canvas again and re-export your cookies.

**"No modules found"**
The course may be empty, unpublished, or you may not have access.

**`browser_cookie3` errors with `--browser`**
Some browsers lock their cookie databases while running. Try closing the browser first, or use the `--cookies` file method instead.

## Using as a Context Bank for Claude Code

Once you've downloaded a course, you can point Claude Code at the materials:

```bash
# From your assignment/project directory, reference the downloaded course
claude "Using the course materials in ~/downloads/Intro_to_CS/ as context, help me with this assignment"
```

Or add the course path to your project's `CLAUDE.md`:

```markdown
# Course Context
Course materials are in ~/downloads/Course_Name/
Reference these for assignment help, studying, and review.
```

The module-based folder structure makes it easy to reference specific topics:

```
downloads/Intro_to_CS/
  Module_1_Fundamentals/
  Module_2_Data_Structures/
  Module_3_Algorithms/
  links.txt
```

## License

MIT
