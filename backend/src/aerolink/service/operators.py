"""ATC operator accounts (G2): the CLI that creates and manages the logins for the operations dashboard.

    python -m aerolink.service.operators add alice --name "Alice Kim"    # asks for the password (twice, no echo)
    python -m aerolink.service.operators add alice --password-stdin < pw.txt   # scripts: one line on stdin
    python -m aerolink.service.operators passwd alice                    # new password; ends alice's sessions
    python -m aerolink.service.operators disable alice                   # cannot sign in; sessions end now
    python -m aerolink.service.operators enable alice
    python -m aerolink.service.operators list

Inside the running stack: ``docker compose exec backend python -m aerolink.service.operators add alice``.

The password is never taken from the command line (it would sit in shell history and ``ps``). Usernames are 2-32
characters (a-z, 0-9, ``.``, ``_``, ``-``), stored in lower case; passwords are 10-256 characters and stored only
as a scrypt hash with a per-user salt. The database must already be migrated (``alembic upgrade head``).
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys

from aerolink.service import auth
from aerolink.service.settings import Settings
from aerolink.service.store import Store


def read_password(from_stdin: bool) -> str:
    """One line from stdin, or a hidden prompt asked twice."""
    if from_stdin:
        return sys.stdin.readline().rstrip("\r\n")
    first = getpass.getpass("Password: ")
    if first != getpass.getpass("Repeat password: "):
        raise SystemExit("error: the two passwords differ")
    return first


async def _main(a: argparse.Namespace) -> None:
    store = Store.connect(Settings.from_env().database_url)
    try:
        if a.cmd == "add":
            try:
                u = auth.check_username(a.username)
                password = auth.check_password(read_password(a.password_stdin))
                await auth.create_operator(store, u, password, a.name)
            except ValueError as e:
                raise SystemExit(f"error: {e}") from e
            print(f"operator {u} created; they can sign in at /login")
        elif a.cmd == "passwd":
            u = auth.normalise_username(a.username)
            try:
                password = auth.check_password(read_password(a.password_stdin))
            except ValueError as e:
                raise SystemExit(f"error: {e}") from e
            h, salt = await asyncio.to_thread(auth.make_hash, password)
            if not await store.set_operator_password(u, h, salt):
                raise SystemExit(f"error: no operator {u!r}")
            print(f"password changed for {u}; their sessions were ended")
        elif a.cmd in ("disable", "enable"):
            u = auth.normalise_username(a.username)
            if not await store.set_operator_disabled(u, a.cmd == "disable"):
                raise SystemExit(f"error: no operator {u!r}")
            print(f"{u} {'disabled; their sessions were ended' if a.cmd == 'disable' else 'enabled'}")
        else:
            for o in await store.list_operators():
                last = o["last_login"].strftime("%Y-%m-%d %H:%M") if o["last_login"] else "never"
                state = "disabled" if o["disabled"] else "active"
                print(f"{o['username']:<20} {o['display_name']:<24} {state:<9} last login: {last}")
    finally:
        await store.close()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    add = sub.add_parser("add", help="create an operator")
    add.add_argument("username")
    add.add_argument("--name", help="display name shown in the dashboard header (default: the username)")
    pw = sub.add_parser("passwd", help="change a password; ends the operator's sessions")
    pw.add_argument("username")
    for p in (add, pw):
        p.add_argument("--password-stdin", action="store_true", help="read the password from one line of stdin")
    for name in ("disable", "enable"):
        sub.add_parser(name).add_argument("username")
    sub.add_parser("list")
    asyncio.run(_main(ap.parse_args(argv)))


if __name__ == "__main__":
    main()
