#!/usr/bin/env python
"""Check the Slack setup and find the two IDs the app still needs.

    ./jobhunt/venv/bin/python -m jobhunt.scripts.verify_slack

Verifies both tokens, then lists the channels the bot can post to and the
workspace members, so the channel and your own user ID can be picked from a
list rather than hunted for in the Slack UI.

Prints only masked tokens — the real values never reach the terminal.
"""

import sys

from .. import config
from ..db import connect, init_db


def check_bot_token(token):
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError

    client = WebClient(token=token)
    try:
        auth = client.auth_test()
    except SlackApiError as exc:
        return None, "rejected: {0}".format(exc.response.get("error", exc))
    return client, "OK — bot '{0}' in workspace '{1}'".format(
        auth.get("user"), auth.get("team")
    )


def check_app_token(token):
    """apps.connections.open is the call Socket Mode itself makes.

    The app token goes in the `app_token=` keyword, not the client
    constructor — this endpoint authenticates differently from the rest of
    the Web API.
    """
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError

    try:
        WebClient().apps_connections_open(app_token=token)
    except SlackApiError as exc:
        error = exc.response.get("error", str(exc))
        if error == "not_allowed_token_type":
            return False, "wrong token type — this must be the xapp- app-level token"
        if error == "missing_scope":
            return False, "token is missing the connections:write scope"
        return False, "rejected: {0}".format(error)
    return True, "OK — Socket Mode connection accepted"


def list_channels(client):
    from slack_sdk.errors import SlackApiError

    try:
        resp = client.conversations_list(
            types="public_channel,private_channel", limit=200, exclude_archived=True
        )
    except SlackApiError as exc:
        return [], str(exc.response.get("error", exc))
    return resp.get("channels", []), None


def list_members(client):
    from slack_sdk.errors import SlackApiError

    try:
        resp = client.users_list(limit=200)
    except SlackApiError as exc:
        return [], str(exc.response.get("error", exc))
    people = [
        u for u in resp.get("members", [])
        if not u.get("is_bot") and not u.get("deleted") and u.get("id") != "USLACKBOT"
    ]
    return people, None


def main():
    init_db()
    conn = connect()
    try:
        bot_token = config.get_secret(conn, "slack.bot_token")
        app_token = config.get_secret(conn, "slack.app_token")
        _, bot_src, bot_mask = config.secret_status(conn, "slack.bot_token")
        _, app_src, app_mask = config.secret_status(conn, "slack.app_token")
    finally:
        conn.close()

    print("=" * 62)
    print("Slack setup check")
    print("=" * 62)

    if not bot_token:
        print("\n  bot token   NOT SET")
        print("    Run: ./jobhunt/venv/bin/python -m jobhunt.scripts.set_secret "
              "slack.bot_token")
        return 1
    print("\n  bot token   {0}  [{1}]".format(bot_mask, bot_src))

    client, message = check_bot_token(bot_token)
    print("              {0}".format(message))
    if client is None:
        return 1

    if not app_token:
        print("\n  app token   NOT SET")
        print("    Basic Information -> App-Level Tokens -> Generate Token and Scopes")
        print("    Add the connections:write scope, then run:")
        print("    ./jobhunt/venv/bin/python -m jobhunt.scripts.set_secret "
              "slack.app_token")
    else:
        print("\n  app token   {0}  [{1}]".format(app_mask, app_src))
        ok, message = check_app_token(app_token)
        print("              {0}".format(message))

    channels, error = list_channels(client)
    print("\n" + "-" * 62)
    print("Channels the bot can see")
    print("-" * 62)
    if error:
        print("  could not list channels: {0}".format(error))
    elif not channels:
        print("  none found — create a channel, then invite the bot with /invite @JobHunt")
    else:
        for channel in sorted(channels, key=lambda c: c.get("name", "")):
            member = "bot is a member" if channel.get("is_member") else "NOT invited"
            kind = "private" if channel.get("is_private") else "public"
            print("  #{0:<24} {1:<8} {2:<8} {3}".format(
                channel.get("name", "?"), channel.get("id", "?"), kind, member))
        print("\n  Set the target with:  slack.channel = #<name>")
        print("  A private channel needs:  /invite @JobHunt")

    members, error = list_members(client)
    print("\n" + "-" * 62)
    print("Workspace members — your own ID goes on the action allowlist")
    print("-" * 62)
    if error:
        print("  could not list members: {0}".format(error))
    else:
        for user in sorted(members, key=lambda u: u.get("name", "")):
            profile = user.get("profile") or {}
            print("  {0:<13} {1:<20} {2}".format(
                user.get("id", "?"), user.get("name", "?"),
                profile.get("real_name") or ""))
        print("\n  The allowlist stops other workspace members from triggering")
        print("  CV generation or editing the tracker.")

    print("\n" + "=" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(main())
