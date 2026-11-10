"""Tool schemas: what the model reads to decide when to use Reflexes."""

REFLEX_ADD = {
    "name": "reflex_add",
    "description": (
        "Arm a reflex: a standing rule that starts a new turn in THIS chat whenever something happens "
        "on the user's computer, without the user typing anything. Three kinds of trigger (set 'kind'): "
        "'folder' (default): files created, modified or deleted in a folder that match a pattern; use it for "
        "'whenever a PDF lands in Downloads, ...' or 'watch this folder and ...'. "
        "'drive': a USB stick, SD card or external disk is connected (or removed); use it for 'when I plug in "
        "my camera card, ...'. "
        "'log': new lines matching some text or a regular expression are appended to one log file; use it for "
        "'tell me when build.log gets an ERROR' or 'watch this log for failures'. "
        "The user is asked to approve each new reflex. When it fires you receive what happened plus the saved "
        "instruction, and you act with your normal tools. Write the instruction so it stands alone later. Have "
        "it put outputs somewhere the trigger will not see (a different folder, or a file the log reflex is "
        "not watching), so the reflex does not react to its own results."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "kind": {
                "type": "string", "enum": ["folder", "drive", "log"],
                "description": "What to watch: 'folder' (default), 'drive' (storage volumes appearing) or 'log' (a file's new lines).",
            },
            "name": {"type": "string", "description": "Short label, e.g. 'receipts'."},
            "folder": {"type": "string", "description": "kind=folder: absolute path of the folder to watch (~ is expanded)."},
            "file": {"type": "string", "description": "kind=log: absolute path of the log file (it, or at least its folder, must exist)."},
            "pattern": {
                "type": "string",
                "description": (
                    "kind=folder: file-name glob(s), comma separated, case-insensitive; default '*', e.g. '*.pdf, *.png'. "
                    "kind=drive: glob(s) matched against the drive label and path, e.g. 'KINGSTON*'; default '*' (any drive). "
                    "kind=log: REQUIRED. Plain text is a case-insensitive substring ('error'); wrap in slashes for a "
                    "regular expression ('/ERROR|FATAL/')."
                ),
            },
            "events": {
                "type": "array",
                "items": {"type": "string", "enum": ["created", "modified", "deleted", "connected", "removed"]},
                "description": (
                    "kind=folder: created (default), modified, deleted. kind=drive: connected (default), removed. "
                    "Ignored for log."
                ),
            },
            "recursive": {"type": "boolean", "description": "kind=folder: also watch subfolders. Default false."},
            "instruction": {
                "type": "string",
                "description": "What to do each time it fires, written as a complete standalone instruction.",
            },
        },
        "required": ["instruction"],
    },
}

REFLEX_MANAGE = {
    "name": "reflex_manage",
    "description": (
        "List, pause, resume, remove, test or inspect the log of reflexes (standing rules armed with "
        "reflex_add that start a turn when files change in a folder, a drive is connected, or a log file gets matching lines). Use 'list' to see what is armed and "
        "whether each is healthy, 'log' to see recent firings and whether they were delivered, 'test' to "
        "send one fake firing to check delivery."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "pause", "resume", "remove", "test", "log"]},
            "name": {"type": "string", "description": "Reflex name; required for pause, resume, remove and test."},
            "limit": {"type": "integer", "description": "For 'log': how many recent firings (default 10)."},
        },
        "required": ["action"],
    },
}
