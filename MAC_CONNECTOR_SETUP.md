# Connect your Canvas courses to ChatGPT or Claude

Run your own private connector on your computer. It discovers your Canvas courses,
keeps selected materials up to date, and lets ChatGPT or Claude search them without
repeated uploads. No browser app is needed.

## What is free?

The project, local Python server, GitHub OAuth app, and temporary Cloudflare tunnel
have no hosting subscription or model API requirement. This route needs no domain,
Cloudflare account, OpenAI API key, or Anthropic API key. You still need your own
Canvas account and a ChatGPT/Claude account that offers custom MCP connections;
account and workspace restrictions can apply. The scripts do not unlock missing
ChatGPT/Claude features.

A temporary URL changes when restarted and has no uptime guarantee. A permanent
Cloudflare tunnel is an optional alternative for people who already own a domain
on Cloudflare. Domain registration can cost money. Neither option works when your
computer is asleep or offline.

Sources: [Cloudflare Quick Tunnels](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/),
[ChatGPT custom MCP servers](https://developers.openai.com/api/docs/guides/custom-mcp-server),
[Claude custom connectors](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp).

## Free quick setup: one script

**Supported:** macOS and Linux, using your own signed-in desktop browser. Firefox
is recommended. Automatic login services are available on macOS; the free quick
mode runs in Terminal on both systems. The original single-course downloader has
its own platform support; this MCP setup does not support native Windows yet.

1. Log into your school's Canvas website in Firefox. Keep this login current.
2. Install the free [uv](https://docs.astral.sh/uv/getting-started/installation/)
   tool. On a Mac with Homebrew, the setup script installs uv and cloudflared if
   needed. Without Homebrew, install uv and
   [cloudflared](https://developers.cloudflare.com/tunnel/downloads/) using their
   official instructions first.
3. Clone the repository and run:

   ```sh
   git clone https://github.com/elliott-leow/canvas-downloader.git
   cd canvas-downloader
   ./setup.sh
   ```

   The script installs Python 3.12 and dependencies in its own environment, asks
   for your Canvas URL (such as `https://canvas.example.edu`) and browser, then
   starts a free HTTPS tunnel. Choose **github** authentication for both web apps.
   It does not ask for your Canvas password.
4. The script prints a temporary hostname and the exact GitHub OAuth callback
   URL. Open [GitHub's new OAuth app page](https://github.com/settings/applications/new)
   and enter the fields printed by the script:

   | GitHub field | What to enter |
   | --- | --- |
   | Application name | `Canvas Files` |
   | Homepage URL | The printed HTTPS origin |
   | Authorization callback URL | The printed origin followed by `/auth/callback` |

   Click **Register application**, then **Generate a new client secret**. Enter
   your GitHub username, Client ID, and Client Secret at the script's prompts.
   Secret input is hidden. **Only the configured GitHub username can access your
   installation**, even if someone else knows its URL.
5. After setup, copy the printed connector URL ending in `/mcp`. Leave Terminal
   open. The script runs the MCP server, the tunnel, and a sync every 30 minutes.
   Press Control-C to stop its processes.

GitHub app creation and approving the connection in your chat account are manual
account steps. Everything else above is handled by the script.
[GitHub OAuth app instructions](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/creating-an-oauth-app),
[FastMCP GitHub OAuth](https://gofastmcp.com/integrations/github).

## Connect to Claude

1. Open **Customize → Connectors → + Add → Add custom connector**.
2. Name it **Canvas Files** and paste your own printed HTTPS URL ending in `/mcp`.
3. Choose **Sign in now** and **Register automatically** under OAuth client.
4. Leave Claude's OAuth Client ID and Client Secret fields blank. The GitHub app
   credentials belong in the local setup script, not in Claude.
5. Complete consent and GitHub sign-in using the username configured during setup.
6. Enable Canvas Files in your conversation's connector menu.

Test with: **“List all my available Canvas courses, including hidden courses.”**

On Team/Enterprise, an organization owner may need to add the connector first.
Menu wording can vary. If Claude cannot register, verify that authentication is
configured as **github**, that the server is running, and that GitHub's callback
matches the current public URL exactly. [Claude instructions](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp).

## Connect to ChatGPT

1. Open [ChatGPT Plugins](https://chatgpt.com/plugins), select **+ → Add custom
   MCP server**. Some accounts expose this under **Settings → Apps**, with a
   developer/custom-app option instead.
2. Name it **Canvas Files**, choose the **Server URL** connection, and enter your
   printed HTTPS URL ending in `/mcp`.
3. Choose **OAuth** and automatic/dynamic client registration when offered. Leave
   ChatGPT's OAuth Client ID and Client Secret fields blank.
4. Create and install the connection, complete consent and GitHub sign-in, then
   select Canvas Files with `@` in a new conversation. If your surface requires
   Work mode, start the conversation there.
5. Ask: **“Search my selected Canvas courses for the latest assignment.”**

Custom-server access depends on your account/workspace. If the add-custom option
is missing, check your workspace permissions and current account availability.
No OpenAI API key is needed for this public HTTPS + OAuth route.
[OpenAI connection instructions](https://developers.openai.com/api/docs/guides/custom-mcp-server),
[plugin quickstart](https://developers.openai.com/plugins/quickstart),
[OAuth requirements](https://developers.openai.com/plugins/build/auth).

## Pick courses and refresh from either chat app

Current courses are included by default; courses whose term/course has ended or
whose enrollment is completed are hidden. Undated courses default to included.
Selections are separate from Canvas dashboard favorites and do not modify Canvas.

Use these prompts with Canvas Files enabled:

- “Show all my courses and which ones are hidden.”
- “Which courses am I a student, instructor, TA, or observer in?”
- “Hide these courses from syncing and assistant access.”
- “Include my past calculus course, then sync selected courses now.”
- “Refresh the course list without downloading files.”
- “Show the last sync status and any failures.”
- “Summarize the latest announcements and discussion replies in my selected courses.”

Hidden courses are excluded from search, file lists, direct fetch, and subsequent
downloads. Hiding a course does not delete existing local files. A download already
in progress may finish. Course names remain visible in the selection list so you
can include them again. New enrollments are discovered on each refresh.

The tools are `list_available_courses`, `get_course_roles`, `set_course_sync`, `sync_courses`,
`sync_status`, `list_courses`, `list_files`, `search`, and `fetch`. Course selections
and refresh requests are marked as actions; reading materials remains read-only.
After a code update that adds or changes tools, refresh ChatGPT's tool metadata
or remove/re-add the Claude connector. Ordinary course updates need no reset.

`get_course_roles` reports your per-course enrollment types, role labels (including
custom roles), role IDs, and enrollment states from Canvas. A person can have
multiple roles in one course and different roles across courses. Observer and
designer enrollments are included. Roles are context, not a permission grant;
the connector still obeys Canvas access rules and provides no grading, submission,
or administration tools. Missing role data is reported as unknown rather than
assuming every person is a student. The role snapshot updates during discovery.

## Permanent URL and automatic Mac startup

Use this optional path if you already have a domain on Cloudflare and want the
services to start at login without keeping Terminal open.

1. In the [Cloudflare dashboard](https://dash.cloudflare.com/), open **Networking
   → Tunnels** and create a Cloudflared tunnel named **canvas-files**. Choose your
   Mac's operating system and architecture.
2. Under **Routes → Add route → Published application**, set:

   | Field | Example |
   | --- | --- |
   | Subdomain | `canvas` |
   | Domain | Your own domain, such as `example.com` |
   | Path | Leave blank |
   | Service type | HTTP |
   | Service URL | `127.0.0.1:8765` |

   Keep the hostname unused by other applications. Cloudflare creates the DNS
   record. If you choose another connector port, use that port here too.
3. Configure your own values:

   ```sh
   ./setup.sh configure --canvas-url https://canvas.example.edu --browser firefox \
     --auth github --public-url https://canvas.example.com
   ./setup.sh oauth
   ```

   The OAuth wizard prints `https://canvas.example.com/auth/callback`. Create or
   update your GitHub OAuth app with that callback, then enter its credentials.
4. Install the connector and scheduled sync:

   ```sh
   ./setup.sh services
   ./setup.sh tunnel
   ```

   At the tunnel prompt, paste **only the Cloudflare tunnel token** from its setup
   screen. Do not paste the entire install command. This is different from the
   GitHub Client Secret and the optional MCP bearer token. You do not need to run
   Cloudflare's separate service-install command.
5. Check **Healthy** in Cloudflare and run:

   ```sh
   ./setup.sh status
   ```

   Add `https://canvas.example.com/mcp` to both chat apps using OAuth as above.

Both services start at Mac login and restart after exiting. Sync runs at login
and every 30 minutes while awake. This does not bypass FileVault, run before login,
or keep your computer awake. Login/reboot persistence needs checking on your own
Mac. [Cloudflare permanent tunnel setup](https://developers.cloudflare.com/tunnel/get-started/).

## Useful commands

```sh
./setup.sh quick        # Free temporary URL; keep Terminal open
./setup.sh sync         # Refresh selected courses immediately
./setup.sh status       # Mac login-service and last-sync status
./setup.sh run          # Run the HTTP MCP server in the foreground
./setup.sh uninstall    # Remove Mac login services; retain files and credentials
```

`./setup.sh configure --port 8976` chooses another local port. Update your permanent
Cloudflare route to match. Do not move the repository after installing login
services; if you do, remove the old services first and reinstall from the new path.

### Optional Claude-only bearer-token mode

If you do not want GitHub OAuth and only need Claude, choose **token** during
setup. In Claude choose **No sign in**, then add the request header
`Authorization: Bearer <token>` using your `.mcp-token` file. Copy the complete
header value on macOS without printing it:

```sh
.venv-mcp/bin/python -c 'from pathlib import Path; import subprocess; subprocess.run(["pbcopy"], input="Bearer " + Path(".mcp-token").read_text().strip(), text=True, check=True)'
```

Choose **No sign in** even if sign-in is tagged Detected; the header performs
authentication. Never use the Cloudflare tunnel token here. ChatGPT's public MCP
OAuth route cannot use this token-only configuration. Its alternative private
[Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)
requires a separate OpenAI runtime API key and is not the default free guide.

## Privacy, limits, and troubleshooting

Each person runs a separate installation connected to their own Canvas/browser
account. This is not a hosted multi-user service. Your machine stores course files
in `downloads/`, account settings in `.connector-config.json`, secrets in
`.connector-secrets.json` and `.mcp-token`, and encrypted OAuth state in
`.connector-oauth/`. These paths are ignored by Git. Secrets use owner-only file
permissions. Course content reaches ChatGPT/Claude only when its tools retrieve it.

- **Temporary URL changed:** update the GitHub OAuth app's Homepage and callback
  URL to those printed by `./setup.sh quick`, then re-add the chat connector. A
  permanent domain avoids this step.
- **Login required:** log into Canvas again in the configured browser, then ask
  the connector to sync. Firefox is recommended; Chromium-based browsers may
  require operating-system keychain permission. The setup never asks for your
  Canvas password. Session expiry means this is not unattended forever.
- **403/404 or partial sync:** some courses/endpoints are unavailable to your
  enrollment. The status records failures and keeps earlier files. No script can
  bypass Canvas permissions. Files linked in accessible modules are also tried.
- **Port in use:** stop the previous foreground setup with Control-C, or choose
  another port. Do not run quick mode and a login service on the same port.
- **Cloudflare Access sign-in page:** an interactive email/PIN gate prevents MCP
  clients reaching OAuth discovery. The connector has its own authenticated OAuth
  and owner check; route it directly through the tunnel.
- **GitHub sign-in denied:** sign in as the exact GitHub owner configured locally.
- **Linux:** quick/foreground mode works; `services`, `tunnel` installation,
  `status`, and `uninstall` login-service commands are macOS-only. Linux users can
  supervise the foreground commands with their own service manager.

Sync archives accessible Canvas files, page text, assignment descriptions, syllabi,
module outlines, announcements, and discussion content. Announcements and discussions
are saved in their own folders with topic bodies, authors, timestamps, source URLs,
nested reply relationships, and available group-specific threads. The sync checks
fresh replies on each run, including entries newer than Canvas's cached thread view.
Canvas post-first and group-access restrictions are respected; unavailable replies
are labeled, and temporary failures preserve the last complete thread. Attachment
links are recorded; attachments are also available when the ordinary file sync can
retrieve them. Unchanged file timestamps and text hashes avoid repeated
writes. Downloads replace files atomically. Deleted/renamed remote items do not
remove old local copies. External websites, submissions, and quiz
question bodies are not synchronized. Media files may download as Canvas files,
but the assistant reads only TXT, Markdown, PDF, DOCX, and PPTX; diagrams, images,
and scanned text are not extracted. Extraction excludes files over 25 MB and caps
text at one million characters.

### Developer checks

```sh
uv pip install --python .venv-mcp/bin/python -r requirements-mcp.txt
.venv-mcp/bin/python -m unittest discover -v
```

Tests cover protected HTTP and stdio tools, course visibility, incremental sync,
pagination, login failure, atomic writes, portable setup, OAuth discovery/client
registration/persistence, the owner gate, announcements, nested/group replies,
fresh-entry updates, deleted posts, and reply-access restrictions. A new GitHub app and browser consent
still need an account-level check; unit/integration tests do not prove those steps.
