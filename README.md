# line-local-mcp

A privacy-first, read-only [Model Context Protocol](https://modelcontextprotocol.io/)
gateway for a user-owned, locally synchronized LINE archive.

It lets Claude Code, Claude Desktop, Codex, and other MCP hosts search chat history
without automating the LINE UI or granting the model permission to send messages.

> This project is independent and is not affiliated with or endorsed by LY
> Corporation or LINE. LINE is a trademark of LY Corporation and is used here only
> to identify compatibility. This repository contains no LINE source code, SDK code,
> logos, icons, or chat data. It does not provide a LINE scraper. You supply a
> compatible local archive API and are responsible for complying with applicable
> terms and laws.

## Try it in two minutes (synthetic demo data)

No archive, no account, no setup. `examples/demo_archive.py` is a zero-dependency
stdlib script that serves the full archive API contract below over invented
contacts, messages, and attachments—an image, a text note, a PDF, and a video clip
with a MIME type the default allow list refuses. Nothing here is real LINE data.

```bash
python3 examples/demo_archive.py
```

It prints a bearer token and a ready-to-run `claude mcp add` command; paste that
command, then ask Claude to list your LINE chats, read the demo image, or read the
text note (its fake password will come back redacted). Loopback-only, and nothing
is written to disk. Ctrl-C stops it.

## Why this exists

Existing LINE MCP projects generally target LINE Official Accounts or automate the
desktop application. This project deliberately has a narrower trust boundary:

- read-only archive access;
- local `stdio` MCP transport—no MCP network listener;
- HTTPS required for non-loopback archive APIs;
- response-size and request-time limits;
- built-in or external fail-closed redaction;
- contact aliases for renamed LINE contacts;
- attachment reading gated by a MIME allow list, with no files written to disk;
- no send, retract, mark-read, or account-management tools.

## Archive API contract

The server is an adapter. Your archive API must accept a Bearer token and return JSON
on every endpoint except `/media`, which returns raw bytes:

| Method | Path | Query | Purpose |
|---|---|---|---|
| GET | `/health` | — | Database health |
| GET | `/stats` | — | Counts and date range |
| POST | `/sync` | — | Trigger incremental synchronization |
| GET | `/chats` | `limit` | Recent chats |
| GET | `/recent` | `limit` | Recent messages |
| GET | `/unread` | — | Unread messages |
| GET | `/search` | `q`, `limit` | Full-text search |
| GET | `/chat` | `name`, `limit` | One contact/group history |
| GET | `/attachments` | `name`, `kind`, `limit` | Attachment metadata |
| GET | `/media` | `id` | One attachment's raw bytes |

The API may return `{"error": "..."}` when a contact is not found. All other JSON
shapes pass through unchanged after redaction.

`/attachments` should return `{"attachments": [...]}`. Each entry needs a `media_id`
that `/media` accepts; `mime`, `filename`, `kind`, `timestamp`, and sender fields pass
through if present. An empty list is treated as "this display name has nothing", so the
server keeps trying the remaining alias candidates.

`/media` is the one binary endpoint. It must answer `404` for an unknown id—an
`{"error": ...}` body with status `200` would be returned to the model as a JSON
attachment. `Content-Type` types the payload; `Content-Disposition` supplies the
filename and, when the type is `application/octet-stream`, is used to guess the type
from the extension.

## Install for Claude Code

The safest token setup uses a command that prints a short-lived or locally stored
token to stdout:

```bash
claude mcp add --scope user --transport stdio line-local \
  --env LINE_API_BASE=https://your-private-archive.example \
  --env 'LINE_API_TOKEN_COMMAND=your-password-manager read line-archive-token' \
  -- uvx --from git+https://github.com/zenhsieh/line-local-mcp line-local-mcp
```

For a quick local setup, `LINE_API_TOKEN` is also accepted, but the value may be
stored in the MCP host's configuration:

```bash
claude mcp add --scope user --transport stdio line-local \
  --env LINE_API_BASE=http://127.0.0.1:8765 \
  --env LINE_API_TOKEN=replace-me \
  -- uvx --from git+https://github.com/zenhsieh/line-local-mcp line-local-mcp
```

Verify with:

```bash
claude mcp get line-local
```

## Claude Desktop / generic MCP host

```json
{
  "mcpServers": {
    "line-local": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/zenhsieh/line-local-mcp",
        "line-local-mcp"
      ],
      "env": {
        "LINE_API_BASE": "http://127.0.0.1:8765",
        "LINE_API_TOKEN": "replace-me"
      }
    }
  }
}
```

## Contact aliases

Copy `aliases.example.json` outside the repository, edit it, and configure its path:

```bash
export LINE_MCP_ALIASES_FILE="$HOME/.config/line-local-mcp/aliases.json"
```

The keys are canonical people; each list contains former display names or alternate
accounts. The alias file itself is ignored by Git when named `aliases.json`.

## Redaction

`LINE_MCP_REDACTION_MODE` supports:

- `basic` (default): masks common password, token, private-key, and API-key patterns;
- `external`: pipes every response string through your own scanner and refuses to
  return data if the scanner fails;
- `off`: explicitly disables redaction. Avoid this for real chat history.

External scanner example:

```bash
export LINE_MCP_REDACTION_MODE=external
export 'LINE_MCP_REDACTOR_COMMAND=python3 /path/to/scanner.py --redact'
```

The command receives newline-delimited text on stdin and must preserve the exact
number of lines on stdout. Exit status 0 or 1 is accepted; all other outcomes fail
closed.

## Attachments

`LINE_MCP_MEDIA_MODE` controls how much of an attachment the model can reach:

- `full` (default): `line_list_attachments` and `line_read_media` are both registered;
- `metadata`: only `line_list_attachments`—the model can see that a file exists, and
  its name and type, but cannot load its content;
- `off`: neither tool is registered.

`line_read_media` returns the attachment inline, chosen by MIME type:

| Type | Returned as |
|---|---|
| `image/*` | image content the model can actually look at |
| `text/*`, `application/json`, `+json`, `+xml` | UTF-8 text, passed through the redactor |
| anything else on the allow list | an opaque `line-archive://media/<id>` embedded resource |

Nothing is written to disk. `LINE_MCP_MAX_MEDIA_BYTES` (default 4 MiB) caps one
attachment independently of `LINE_MCP_MAX_RESPONSE_BYTES`, and oversized attachments
are refused rather than truncated.

`LINE_MCP_MEDIA_MIME_ALLOW` is a comma-separated allow list of exact types and
`type/*` wildcards. It defaults to `image/*,text/*,application/json,application/pdf,application/xml`—
types a model can do something useful with. A type outside it is refused by name, so
widen it deliberately:

```bash
export LINE_MCP_MEDIA_MIME_ALLOW='image/*,text/*,application/pdf,application/zip'
```

`*` allows every type. Only the text branch passes through the redactor; see
[SECURITY.md](SECURITY.md).

## Tools

- `line_health`
- `line_stats`
- `line_sync`
- `line_list_chats`
- `line_list_recent_messages`
- `line_list_unread`
- `line_search_messages`
- `line_resolve_contact`
- `line_get_messages`
- `line_list_attachments` (media mode `metadata` or `full`)
- `line_read_media` (media mode `full`)

## Optional event pipeline

`line-local-pipeline` turns the same read-only archive into a small operations
pipeline without adding any LINE write capability:

```text
archive/MCP -> watcher + deduplication -> per-profile JSONL inbox
                                         |-> permanent-number Markdown tasks
                                         |-> terminal dashboard
                                         `-> optional agent notification
```

Each configured profile has its own contact aliases, state directory, inbox, task
file, project label, and agent-target policy. Copy `pipeline.example.toml` outside
the repository, keep secrets in that private file or its environment, then bootstrap
the current archive position before enabling a timer:

```bash
line-local-pipeline --config ~/.config/line-local-mcp/pipeline.toml \
  watch example --bootstrap
line-local-pipeline --config ~/.config/line-local-mcp/pipeline.toml run example
line-local-pipeline --config ~/.config/line-local-mcp/pipeline.toml \
  todo example add "Confirm the delivery rule" --status 待問
line-local-pipeline --config ~/.config/line-local-mcp/pipeline.toml dashboard example --watch
```

For the full Case Cockpit composition used beside a working agent, run one
read-only collection/reconciliation cycle and the interactive two-column view:

```bash
line-local-case-cockpit --config ~/.config/line-local-mcp/pipeline.toml run example
line-local-case-cockpit --config ~/.config/line-local-mcp/pipeline.toml \
  dashboard example --watch
```

The cockpit turns each new inbox event into exactly one durable Markdown task,
even after retries or manual task deletion. It is a presentation and routing
composition: the task file and inbox remain the durable records, and agent
delivery still follows the disabled-by-default, fail-closed pipeline policy.

Task numbers are monotonically allocated and never reused after completion or
deletion. The JSONL inbox is durable: an agent notification is marked delivered only
after its configured command succeeds. Notification is disabled by default. There is
no built-in Herdr pane discovery and no pane id is persisted; a deployment may name a
stable agent explicitly or provide a resolver command that must return exactly one
target. The prompt always receives an archive event, never authority to reply to LINE
or mutate a live system.

User-level systemd templates are included in `systemd/`. Install them in
`~/.config/systemd/user/`, ensure `line-local-pipeline` is on the service PATH, and
enable one isolated timer per profile:

```bash
systemctl --user enable --now line-local-pipeline@example.timer
journalctl --user -u line-local-pipeline@example.service
```

See [PIPELINE.md](PIPELINE.md) for the profile schema, deployment checklist, and a
safe migration procedure from a project-specific watcher.

## Configuration

| Variable | Required | Default |
|---|---:|---|
| `LINE_API_BASE` | yes | — |
| `LINE_API_TOKEN` or `LINE_API_TOKEN_COMMAND` | yes | — |
| `LINE_MCP_ALIASES_FILE` | no | — |
| `LINE_MCP_REDACTION_MODE` | no | `basic` |
| `LINE_MCP_REDACTOR_COMMAND` | external mode | — |
| `LINE_MCP_TIMEOUT` | no | `30` seconds |
| `LINE_MCP_MAX_RESPONSE_BYTES` | no | `5242880` |
| `LINE_MCP_MEDIA_MODE` | no | `full` |
| `LINE_MCP_MAX_MEDIA_BYTES` | no | `4194304` |
| `LINE_MCP_MEDIA_MIME_ALLOW` | no | readable types |
| `LINE_MCP_ALLOW_INSECURE_HTTP` | no | false |

## Development

```bash
git clone https://github.com/zenhsieh/line-local-mcp
cd line-local-mcp
uv sync --extra dev
uv run pytest
uv run ruff check .
```

Run the MCP Inspector:

```bash
LINE_API_BASE=http://127.0.0.1:8765 LINE_API_TOKEN=test \
  uv run mcp dev src/line_local_mcp/server.py
```

## License

MIT
