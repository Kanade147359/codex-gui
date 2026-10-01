"""Manage login accounts from the shell:  python -m app.users add|passwd|delete|list|count  [username]

Passwords are read from the terminal (never from the command line, so they stay out of shell history).
CODEX_GUI_NEW_PASSWORD may supply one for scripted setups.
"""
import argparse
import getpass
import os
import sys

from .auth import AuthError, AuthService
from .config import Settings
from .database import Database


def read_password(confirm: bool = True) -> str:
    env = os.environ.get("CODEX_GUI_NEW_PASSWORD")
    if env:
        return env
    if not sys.stdin.isatty():
        raise AuthError("no terminal to read a password from (set CODEX_GUI_NEW_PASSWORD for scripted use)")
    password = getpass.getpass("Password: ")
    if confirm and getpass.getpass("Password (again): ") != password:
        raise AuthError("passwords do not match")
    return password


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.users", description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["add", "passwd", "delete", "list", "count"])
    parser.add_argument("username", nargs="?")
    args = parser.parse_args(argv)
    if args.command in ("add", "passwd", "delete") and not args.username:
        parser.error(f"{args.command} needs a username")

    settings = Settings.from_env()
    settings.ensure_dirs()
    db = Database(settings.db_path)
    auth = AuthService(db)
    try:
        if args.command == "add":
            user = auth.create_user(args.username, read_password())
            print(f"created user {user['username']}")
        elif args.command == "passwd":
            auth.set_password(args.username, read_password())
            print(f"password changed for {args.username}; its browser sessions were signed out")
        elif args.command == "delete":
            if not db.delete_user(args.username):
                raise AuthError(f"no such user: {args.username}")
            print(f"deleted user {args.username}")
        elif args.command == "list":
            for u in db.list_users():
                print(f"{u['username']}\t{u['created_at']}")
        else:
            print(db.count_users())
    except AuthError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
