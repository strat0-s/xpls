from __future__ import annotations

import argparse
import getpass
from pathlib import Path

from didac_app import auth
from didac_app.config import load_config
from didac_app.storage import LeadStore


def main() -> None:
    parser = argparse.ArgumentParser(description="DIDAC WhatsApp lead app utilities")
    parser.add_argument("command", choices=["export", "summary", "reset-sample", "create-admin"])
    parser.add_argument("--output", default="outputs/didac/didac_leads.xlsx")
    parser.add_argument("--name", help="create-admin: full name")
    parser.add_argument("--phone", help="create-admin: phone number, also used for the WhatsApp internal menu")
    parser.add_argument("--email", default="", help="create-admin: optional email")
    args = parser.parse_args()

    config = load_config()
    store = LeadStore(config.database_url)
    if args.command == "export":
        path = store.export_report_excel(Path(args.output))
        print(path)
    elif args.command == "summary":
        leads = store.list_leads()
        print(f"Total leads: {len(leads)}")
        for priority, count in store.count_by_priority().items():
            print(f"{priority}: {count}")
    elif args.command == "create-admin":
        password = getpass.getpass("Password: ")
        if password != getpass.getpass("Repeat password: "):
            raise SystemExit("Passwords do not match")
        try:
            user_id = auth.add_user(store, args.name or "", args.phone or "", args.email, password, is_admin=True)
        except ValueError as exc:
            raise SystemExit(str(exc))
        print(f"Created admin #{user_id}")
    elif args.command == "reset-sample":
        store.clear()
        print("Cleared leads, media, feedback and inbound events")


if __name__ == "__main__":
    main()
