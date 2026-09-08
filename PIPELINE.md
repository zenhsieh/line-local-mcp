# Read-only event pipeline

The event pipeline is an optional layer around `line-local-mcp`. It does not scrape
LINE, acknowledge messages, mark them read, or send replies. `line_sync` means “ask
the user-owned archive to refresh”; every LINE-facing operation remains read-only.

## Components and failure boundaries

1. `watch` asks the MCP server to sync, reads one canonical contact (including the
   configured incoming display-name aliases), fingerprints messages and attachments,
   and appends only unseen incoming events to a profile-local JSONL inbox.
2. Inbox append happens before the cursor is replaced. Both files use private modes;
   a profile lock prevents overlapping timer runs.
3. `todo` maintains a human-editable Markdown file. The `next-task-id` marker is the
   high-water mark, so a number is not reused when a completed row is removed.
4. `dashboard` reads the task file, inbox and watcher state. It is presentation only.
5. `inject` is optional and disabled by default. A failed notification remains unseen
   and is retried. Commands are argument arrays, never shell strings.

## Profile schema

See `pipeline.example.toml`. Required profile keys are `project_label`, `contact`,
`state_dir`, and `todo_file`. `incoming_aliases` should include every sender display
name which represents that contact. Files from different customers must never share a
state directory.

The MCP subprocess can be defined directly with `mcp.command` and `mcp.env`, or read
from one named server in a Claude-compatible JSON file with `mcp.config_file` and
`mcp.server_name`. `$VARS` and `~` are expanded. Keep bearer tokens out of tracked
configuration.

Agent delivery supports only these target strategies:

- `disabled`: safe default; collection and dashboard continue without an agent.
- `explicit`: use a stable agent name supplied by the deployment, not a pane id.
- `resolver-command`: run a site-owned helper which must resolve exactly one target
  and print it as exactly one non-empty line. Zero, multiple, timeout, or non-zero exit
  fails closed.

`agent_command` is an argv array with optional `{target}`, `{prompt}`, `{profile}` and
`{project_label}` placeholders. It is executed without a shell. Never configure it to
call a LINE-send command.

## Migrating an existing project watcher

Do not switch a live watcher in place. This sequence prevents replay and preserves an
easy rollback:

1. Create a new private TOML profile with a new, empty `state_dir`; point its
   `todo_file` at a copied task file first.
2. Run `watch PROFILE --bootstrap`. Confirm it emits zero historical events and inspect
   `watch-state.json` permissions and contact aliases.
3. Stop the old timer. Run the new `run PROFILE` manually twice; the second run must
   report zero new events and must not append to `inbox.jsonl`.
4. Add one synthetic archive event or wait for a harmless real event. Confirm exactly
   one inbox row. Keep injection disabled for this test.
5. If agent delivery is desired, configure a stable target or fail-closed resolver,
   enable it, then run `inject PROFILE`. Confirm exactly one delivery and a no-op on the
   second invocation.
6. Install and enable `line-local-pipeline@PROFILE.timer`. Leave the old state and unit
   files intact but disabled for rollback. Do not merge state directories between
   customers.
7. Only after an observation period should the copied task file become canonical.

The project-specific watcher, dashboard, systemd units, and current LINE data do not
need to be modified during development or migration planning.
