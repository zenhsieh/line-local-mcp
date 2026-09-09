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

`line-local-case-cockpit` is the reusable interactive composition extracted from
two independently operated case dashboards. Its `run` command performs one read-only watch,
creates exactly one durable task for every new inbox event, and then invokes the
same optional fail-closed injection contract. Its `dashboard --watch` command shows
pending/completed tasks and recent LINE input in a two-column terminal view. Project
labels, contacts, paths and agent targets remain private profile configuration; they
are never compiled into the shared tool.

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

## If you fork this to add sending

This project stops at notification on purpose: nothing in `line-local-mcp` or this
pipeline can send, mark-read, or otherwise mutate LINE, so the trust boundary is easy
to state and easy to audit. Closing the loop from "agent drafted a reply" to "LINE
received it" is a materially different risk and belongs in your fork, not here—but if
you build it, the shape of the risk is the same regardless of which archive or send API
you're driving:

- **This is account automation, not a business integration.** A personal LINE account
  sending programmatically, through any channel, risks the account itself—no test
  suite catches "LINE decided this looks like a bot."
- **A draft is not a send.** Wire the agent's output to a queue a human confirms, the
  same way `resolver-command` must resolve to exactly one target or fail closed
  (`_resolve_target` above)—never let agent output reach a send call unreviewed.
- **Rate-limit and de-duplicate independently of the agent.** An agent that retries,
  loops, or gets asked twice should not be able to cause two sends; use the same
  fingerprint-before-append discipline `watch()` already uses for the inbox.
- **Log every send attempt with an idempotency key**, durably, before the network call—
  not after—so a crash mid-send is a replay-safe retry, not a silent double-send or a
  silent loss.
- **A send-capable credential is a different asset than a read-only one.** Do not reuse
  the archive's bearer token or its storage; scope and rotate it separately, and assume
  its compromise is worse.
- **Keep it out of this package.** A send path changes what every consumer of
  `line-local-mcp` needs to audit, even ones who never enable it. Ship it as a separate,
  clearly-labeled module in your fork.

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
