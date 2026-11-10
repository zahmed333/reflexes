# Reflexes

Reflexes gives Hermes senses for your computer: when something happens, Hermes starts a turn in your own chat and acts with its normal tools and approvals. Nobody types anything.

<!-- demo.gif here -->

```
/reflex add ~/Downloads *.pdf :: Each new file is a receipt. Rename it to date_vendor_amount.pdf,
move it to ~/Documents/Receipts/<year> and add a row to receipts.csv.

/reflex add drive :: A card or stick was plugged in. Tell me what is on it and, if it holds
photos, copy them into ~/Pictures/Imports/<date>.

/reflex add log ~/code/app/build.log /error|failed/ :: The build just failed. Read the lines,
find the likely cause in the project and explain it in two sentences.
```

You can also ask in plain words ("whenever a screenshot lands on my Desktop, file it into Screenshots and name it after what it shows"). Hermes arms the reflex with the `reflex_add` tool and you approve it once.

## What it senses

**Folder.** Files created (default), modified or deleted that match a glob such as `*.pdf, *.png`.
- Fires once per finished file, not while it is still being written.
- Files that arrive together come as one firing.
- Ignored: partial downloads (`.crdownload`, `.part`), hidden files, lock files, renames inside the folder, and files the agent itself wrote in the last two minutes.

**Drive.** A USB stick, SD card or external disk is connected (optionally `removed`), with an optional name filter.
- Drives already connected when Hermes starts never fire.
- The firing lists the drive's label, size and top-level entries.

**Log.** New lines containing a text, or matching `/regex/`, are appended to a file.
- Existing content never fires.
- Matches are batched; up to 20 lines, 300 characters each, are sent.

## Why it is safe to leave on

- **Delivers to the chat that armed it.** Not to a new session somewhere else.
- **Survives restarts.** Firings go into a durable SQLite queue.
- **Never fires twice.** One detector runs across several Hermes processes (a lease), so two open Hermes windows do not both report the same event.
- **Stops loops.** A reflex that fires more than 30 times an hour pauses itself and says why.
- **Never drops silently.** A firing that cannot be delivered within 15 minutes is logged as missed, with the reason.
- **Cannot approve anything.** Injected text is ordinary input. It cannot approve a tool call, run a slash command or answer a confirmation prompt, so Hermes's normal approvals still apply.
- **Asks before the model arms one.** `reflex_add` asks you to approve by default.

## Commands

| Command | What it does |
|---|---|
| `/reflex` | List reflexes, their state and any problem |
| `/reflex add <folder> [pattern] :: <what to do>` | Arm a folder reflex in this chat |
| `/reflex add drive [name] :: <what to do>` | Arm a drive reflex |
| `/reflex add log <file> <text> :: <what to do>` | Arm a log reflex (`<text>` may be `/regex/`) |
| `/reflex pause <name>` / `resume <name>` / `remove <name>` | Manage one reflex |
| `/reflex test <name>` | Send one fake firing to check delivery |
| `/reflex here [name]` | Deliver to this chat from now on |
| `/reflex log [n]` | Recent firings and what happened to them |

The model has two tools, `reflex_add` and `reflex_manage`, and a bundled skill, `reflexes:authoring`, on writing an instruction that still makes sense weeks later.

## Hermes Desktop

Turn the plugin on in **Capabilities → Plugins** to get:

- a **Reflexes page**: pick the trigger kind, start from one of four one-click starters (Receipts, Screenshots, Camera card, Build errors), pause or resume with a switch, Test, Remove, and read recent firings with the reason for anything not delivered
- a **status-bar chip** showing how many reflexes are armed
- **toasts** when a reflex fires or pauses itself

## Setup

```
hermes plugins install reflexes
hermes config set plugins.entries.reflexes.allow_gateway_injection true
```

Hermes lets a plugin start turns only after you allow it, per plugin; the second command is that permission. Until you set it, reflexes are detected and logged but not delivered.

Turns show live in Hermes Desktop, `hermes --tui` and messaging chats. The classic `hermes chat` terminal runs them without displaying them.

Reflexes run only while Hermes runs.

## Settings

Under `plugins.entries.reflexes.settings`:

| Key | Default | Meaning |
|---|---|---|
| `poll_interval` | `1.0` | Seconds between looks at watched folders |
| `event_ttl_seconds` | `900` | How long a firing waits for its chat before it is recorded as missed |
| `max_fires_per_hour` | `30` | Above this a reflex pauses itself |
| `approve_new_reflexes` | `true` | Ask before the model arms a reflex |

## Disclosure

- **Starts agent turns nobody typed**, through `ctx.inject_message`. That is the purpose of the plugin.
- **Lists the folders you name** about once a second (names, sizes and times), and opens new files briefly only to check they are complete.
- **Log reflexes read the bytes appended** to the one file you name and send matching lines to your model.
- **Enumerates connected drives** and lists the top level of a newly connected one.
- **Runs a background thread** inside Hermes while it runs.
- **Stores locally** in `<HERMES_HOME>/plugin-data/reflexes/reflexes.db`.
- **No network, no telemetry, no shell commands, no subprocesses, no credentials** and no dependencies beyond the Python standard library (on Windows it uses `ctypes` to list drives).

## Nearest neighbours

- **Dropstation** gives each file in a folder inbox a fresh session and writes a Markdown report to an outbox (macOS and Linux). Reflexes acts on your real folders, in your existing chat, with tools, on Windows too, and senses more than files.
- **heartbeat2** has the agent work between your messages on a budget and check things on a timer. Reflexes is event-driven and costs nothing while nothing happens.
- **wakeshift** lets remote MCP servers wake the agent. Reflexes senses the local machine.

## How it works

One Hermes process holds a lease and runs the detectors. A folder detector compares each folder with the files it already knows and waits until a file stops changing before reporting it. A drive detector compares the set of connected drives with the previous one. A log detector remembers how far it has read in the file and looks only at bytes added after that point. Each event goes into a small SQLite queue. Whichever Hermes process hosts the reflex's chat takes the event from the queue and injects it as a turn. Detection and delivery are separate so that two Hermes processes never report the same event twice and an event survives a restart. Events still undelivered after `event_ttl_seconds` are recorded as missed.

## Development

```
python -m unittest discover -s tests
hermes plugins doctor ./reflexes --ci
hermes plugins validate ./reflexes --install-deps
```

Tested live end to end on Windows 11 with Hermes 0.22.6 and 0.23.1. The macOS and Linux code paths are covered by unit tests only. Requires Hermes 0.22.0 or later.

## Licence

MIT, 2026, Zul (zahmed333). See `LICENSE`.
