#!/usr/bin/env python
"""Store a secret without it appearing on screen, in shell history, or in a log.

    ./jobhunt/venv/bin/python -m jobhunt.scripts.set_secret slack.bot_token

The value is read with getpass, so it is never echoed, never becomes a command
argument, and never lands in ~/.zsh_history. Passing a token as a CLI argument
would put it in the process list and your shell history — this script exists
specifically to avoid that, so it refuses to accept one that way.
"""

import getpass
import sys

from .. import config
from ..db import DB_PATH, connect, init_db

KNOWN = {
    "slack.bot_token": ("Slack bot token", "xoxb-"),
    "slack.app_token": ("Slack app-level token", "xapp-"),
    "anthropic.api_key": ("Anthropic API key", "sk-ant-"),
}


def main(argv):
    if len(argv) != 2 or argv[1] not in KNOWN:
        print(__doc__)
        print("Known keys:")
        for key, (label, prefix) in sorted(KNOWN.items()):
            print("  {0:<20} {1} (starts with {2})".format(key, label, prefix))
        return 2

    key = argv[1]
    label, prefix = KNOWN[key]

    value = getpass.getpass("Paste the {0} (input hidden): ".format(label)).strip()
    if not value:
        print("Nothing entered — leaving {0} unchanged.".format(key))
        return 1
    if not value.startswith(prefix):
        print("That does not look like a {0}: expected it to start with '{1}'."
              .format(label, prefix))
        print("Nothing was stored.")
        return 1

    init_db()
    conn = connect()
    try:
        config.set_secret(conn, key, value)
        is_set, source, masked = config.secret_status(conn, key)
    finally:
        conn.close()

    print("Stored {0} = {1} ({2}).".format(key, masked, source))
    print("Database: {0}".format(DB_PATH))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
