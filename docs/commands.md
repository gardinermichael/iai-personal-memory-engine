# Command guide

This page documents the main `iai` and `iai-mcp` command families at an operator level: what each family is for, common examples, side effects, and the files or services it usually touches.

For detailed flags and exact option defaults, treat the CLI help as canonical:

```bash
iai --help
iai <command> --help
iai-mcp --help
iai-mcp <command-family> --help
iai-mcp <command-family> <subcommand> --help
```

## `iai recall`, `iai capture`, `iai ask`, `iai status`, `iai last`

### Purpose

`iai` is the user-facing terminal memory CLI. Use it when you want to read from or write to your personal memory without going through an MCP host.

- `iai recall` retrieves memories by natural-language cue.
- `iai capture` writes one episodic memory from the terminal.
- `iai ask` retrieves relevant memories and asks Claude CLI to synthesize an answer grounded in those memories.
- `iai status` prints a short user-level health summary.
- `iai last` shows recent user-turn records, optionally filtered by session.

### Common examples

```bash
iai status
iai recall "what deployment pattern did we choose?"
iai recall "project naming preferences" --limit 10
iai capture "I prefer terse commit messages for small docs-only changes." --session-id manual-note
iai ask "What should I remember before working on the memory engine?"
iai last --n 10
iai last --session 018f0f2a-example-session-id --json
```

### Side effects

- `iai recall`, `iai status`, and `iai last` are read-oriented and should not add memories.
- `iai capture` writes a new episodic record.
- `iai ask` performs recall and then invokes `claude -p`; it can spend your existing Claude subscription quota and sends the prompt plus selected memory snippets to Claude.
- Commands prefer the local daemon when it is alive and may use an offline/local fallback when it is not.

### Related files/services touched

- Memory store under `~/.iai-mcp/`, including the Hippo store and memory-bank fallback files.
- Daemon socket at `~/.iai-mcp/.daemon.sock` when the local engine is running.
- Encryption key file such as `~/.iai-mcp/.crypto.key` for encrypted records.
- Claude CLI (`claude -p`) for `iai ask` only.

### Detailed option info

Run:

```bash
iai --help
iai recall --help
iai capture --help
iai ask --help
iai status --help
iai last --help
```

## `iai-mcp daemon ...`

### Purpose

`iai-mcp daemon` manages the background local engine. The daemon handles wake/sleep lifecycle work, socket-served operations, idle consolidation, deferred capture draining, and scheduled maintenance.

### Common examples

```bash
iai-mcp daemon install
iai-mcp daemon install --dry-run
iai-mcp daemon status
iai-mcp daemon logs -n 100
iai-mcp daemon logs --follow
iai-mcp daemon pause 600
iai-mcp daemon resume
iai-mcp daemon force-rem
iai-mcp daemon stats
iai-mcp daemon configure set-budget 3000
iai-mcp daemon stop
iai-mcp daemon start
iai-mcp daemon uninstall --yes
```

### Side effects

- `install` writes and loads a user service definition and ensures crypto is initialized on fresh installs.
- `uninstall` unloads and removes the user service definition and daemon state files.
- `start`, `stop`, `pause`, `resume`, `force-rem`, and `configure` change daemon runtime behavior.
- `force-rem` can trigger consolidation outside the normal quiet window.
- `logs`, `status`, and `stats` are mostly read-only.

### Related files/services touched

- macOS launchd service: `~/Library/LaunchAgents/com.iai-mcp.daemon.plist`.
- Linux systemd user service: `~/.config/systemd/user/iai-mcp-daemon.service`.
- Daemon socket, lock, and state under `~/.iai-mcp/`.
- Daemon logs, such as `~/Library/Logs/iai-mcp-daemon.stderr.log` on macOS or `journalctl --user` on Linux.
- Memory store under `~/.iai-mcp/`, especially during deferred capture draining and sleep-cycle work.

### Detailed option info

Run:

```bash
iai-mcp daemon --help
iai-mcp daemon install --help
iai-mcp daemon status --help
iai-mcp daemon logs --help
iai-mcp daemon configure --help
```

## `iai-mcp capture-hooks ...`

### Purpose

`iai-mcp capture-hooks` installs, removes, or checks the host-side hooks that make memory ambient. These hooks capture turns at session time and inject recall at session start for supported hosts.

### Common examples

```bash
iai-mcp capture-hooks install
iai-mcp capture-hooks status
iai-mcp capture-hooks uninstall
```

### Side effects

- `install` copies hook scripts into the host hook directory, makes them executable, and patches supported host settings.
- `uninstall` removes hook registrations and installed hook scripts.
- `status` inspects installed hook state.
- Runtime hook execution appends per-turn transcript data, rolls session buffers into deferred captures, and emits session-start recall context.

### Related files/services touched

- Claude Code hook scripts under `~/.claude/hooks/`.
- Claude Code settings, such as `~/.claude/settings.json`.
- Capture buffers, deferred captures, watermarks, and logs under `~/.iai-mcp/`.
- MCP host configuration for Claude Desktop when available.

### Detailed option info

Run:

```bash
iai-mcp capture-hooks --help
iai-mcp capture-hooks install --help
iai-mcp capture-hooks status --help
iai-mcp capture-hooks uninstall --help
```

## `iai-mcp crypto ...`

### Purpose

`iai-mcp crypto` manages the encryption key used for encrypted-at-rest memory records. Use it for first-run key creation, status checks, key rotation, migration from older Keychain-backed setups, and emergency recovery/redaction workflows.

### Common examples

```bash
iai-mcp crypto status
iai-mcp crypto init
iai-mcp crypto rotate
iai-mcp crypto migrate-to-file --keep-keychain
iai-mcp crypto recover-with-prior-key --prior-key-file /secure/backup/.crypto.key --dry-run
iai-mcp crypto redact-undecryptable
```

### Side effects

- `status` validates key backend, path, permissions, ownership, and length without changing records.
- `init` creates a new file-backed key and refuses to overwrite an existing key.
- `rotate` creates a replacement key and re-encrypts records.
- `migrate-to-file` reads an older macOS Keychain key and writes a file-backed key.
- `recover-with-prior-key` can re-encrypt records that require a prior key.
- `redact-undecryptable` permanently replaces undecryptable literal text with a redacted marker while preserving metadata/index context.

### Related files/services touched

- Key file under `~/.iai-mcp/.crypto.key` or the configured store path.
- Older macOS Keychain entry during `migrate-to-file`.
- Encrypted memory records in the Hippo store under `~/.iai-mcp/hippo`.
- Record metadata, provenance, embeddings, and graph edges during recovery/redaction workflows.

### Detailed option info

Run:

```bash
iai-mcp crypto --help
iai-mcp crypto status --help
iai-mcp crypto init --help
iai-mcp crypto rotate --help
iai-mcp crypto recover-with-prior-key --help
iai-mcp crypto redact-undecryptable --help
```

## `iai-mcp doctor`

### Purpose

`iai-mcp doctor` is the operator health checklist. It diagnoses daemon health, socket freshness, lock state, duplicate binders, store readability, crypto key state, lifecycle state, capture failures, native engine readiness, and related runtime checks.

### Common examples

```bash
iai-mcp doctor
iai-mcp doctor --headless
iai-mcp doctor --apply
iai-mcp doctor --apply --yes
```

### Side effects

- Without `--apply`, it is diagnostic/read-only.
- `--headless` changes how some environment-dependent checks are classified in the report.
- `--apply` may attempt safe repairs such as unlinking stale sockets, killing duplicate binders, cleaning orphan processes, or respawning the daemon.
- `--apply --yes` skips repair confirmations.

### Related files/services touched

- Daemon process, socket, lock, and state under `~/.iai-mcp/`.
- Hippo store, HNSW index, schema version, and compaction freshness.
- Crypto key file and permissions.
- Lifecycle/quarantine state and capture failure queues under `~/.iai-mcp/`.
- Native Rust extension and local runtime dependencies.
- Claude CLI credentials checks for subscription-backed sleep synthesis.

### Detailed option info

Run:

```bash
iai-mcp doctor --help
```

## `iai-mcp maintenance ...`

### Purpose

`iai-mcp maintenance` runs one-shot storage and lifecycle maintenance operations. These are operator tools for compacting the Hippo store, backfilling graph self-loops, and manually running the sleep pipeline.

### Common examples

```bash
iai-mcp maintenance compact-hippo --dry-run
iai-mcp daemon stop
iai-mcp maintenance compact-hippo --apply --yes
iai-mcp daemon start

iai-mcp maintenance symmetrize-self-loops --dry-run
iai-mcp maintenance symmetrize-self-loops --apply --yes
iai-mcp maintenance sleep-cycle
iai-mcp maintenance sleep-cycle --reset-quarantine
iai-mcp maintenance sleep-cycle --force
```

### Side effects

- `compact-hippo --dry-run` reports metrics without optimizing storage.
- `compact-hippo --apply` runs checkpoint/VACUUM/index rebuild work and requires the daemon to be stopped.
- `compact-records` is a deprecated alias for `compact-hippo`.
- `symmetrize-self-loops --apply` writes missing Hebbian self-loop edges and requires the daemon to be stopped.
- `sleep-cycle` runs the sleep pipeline once and may update schemas, procedural knobs, graph weights, optimized storage, compacted records, and quarantine state.

### Related files/services touched

- Hippo storage under `~/.iai-mcp/hippo`, including SQLite/WAL data and HNSW index files.
- Memory graph edges and self-loop records.
- Sleep-cycle state, quarantine state, lifecycle files, and maintenance metrics under `~/.iai-mcp/`.
- Daemon service indirectly: several maintenance operations require it to be stopped before applying changes.

### Detailed option info

Run:

```bash
iai-mcp maintenance --help
iai-mcp maintenance compact-hippo --help
iai-mcp maintenance symmetrize-self-loops --help
iai-mcp maintenance sleep-cycle --help
```
