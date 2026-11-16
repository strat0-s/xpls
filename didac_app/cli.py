from __future__ import annotations

import argparse
import getpass
from pathlib import Path

from didac_app import auth
from didac_app.config import default_brochures, load_config
from didac_app.storage import LeadStore


def main() -> None:
    parser = argparse.ArgumentParser(description="Footfall WhatsApp lead app utilities")
    parser.add_argument("command", choices=["init-db", "export", "summary", "reset-sample", "create-admin", "events", "new-event", "start-event", "stop-event"])
    parser.add_argument("--output", default="outputs/didac/didac_leads.xlsx")
    parser.add_argument("--id", type=int, help="start-event, stop-event: the event number")
    parser.add_argument("--event-name", help="new-event: the event name")
    parser.add_argument("--copy-from", type=int, help="new-event: copy document types and brochures from this event number")
    parser.add_argument("--name", help="create-admin: full name")
    parser.add_argument("--phone", help="create-admin: phone number (used to sign in)")
    parser.add_argument("--email", default="", help="create-admin: optional email")
    args = parser.parse_args()

    config = load_config()
    store = LeadStore(config.database_url)
    store.init_db()  # the web app no longer creates tables on every start; this is where they get created
    if args.command == "init-db":
        first = store.ensure_default_event(default_brochures(config))
        print("Tables are up to date" + (f". Created the first event (#{first}) and made it live" if first else ""))
    elif args.command == "events":
        for event in store.list_events():
            print(f"#{event['id']}  {event['status']:<6}  {event['name']}  ({event['lead_count']} leads)")
    elif args.command == "new-event":
        if not args.event_name:
            raise SystemExit("--event-name is required")
        event_id = store.create_event(args.event_name)
        if args.copy_from:
            store.copy_setup(args.copy_from, event_id)
        print(f"Created event #{event_id} as a draft. Make it live with: start-event --id {event_id}")
    elif args.command in ("start-event", "stop-event"):
        event = store.get_event(args.id) if args.id else None
        if not event:
            raise SystemExit("--id must be an existing event number (see: events)")
        (store.start_event if args.command == "start-event" else store.stop_event)(event["id"])
        print(f"{event['name']} is now {'live' if args.command == 'start-event' else 'stopped'}")
    elif args.command == "export":
        path = store.export_report_excel(Path(args.output))
        print(path)
    elif args.command == "summary":
        leads = store.list_leads()
        print(f"Total leads: {len(leads)}")
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
