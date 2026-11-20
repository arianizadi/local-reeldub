import argparse
import json
from .db import db

p = argparse.ArgumentParser(description="ReelDub operator tools. Requires database credentials.")
sub = p.add_subparsers(dest="command", required=True)
sub.add_parser("reports")
sub.add_parser("accounts", help="List Apple subjects that have signed in to this instance")
account = sub.add_parser("account")
account.add_argument("apple_subject")
account.add_argument("tier", choices=["free", "paid", "admin"])
beta = sub.add_parser("beta-allowance", help="Reserve daily dubs for a verified Apple sign-in email")
beta.add_argument("email")
beta.add_argument("daily_limit", type=int)
args = p.parse_args()
if args.command == "reports":
    for job in db().jobs.find({"report": {"$exists": True}}, {"url": 1, "report": 1, "state": 1,
                              "stage": 1, "attempt": 1, "error": 1, "created": 1, "finished": 1}):
        print(json.dumps(job, default=str, ensure_ascii=False))
elif args.command == "accounts":
    for owner in db().sessions.distinct("owner", {"owner": {"$regex": "^apple:"}}):
        account = db().accounts.find_one({"_id": owner}) or {}
        print(owner.removeprefix("apple:"), account.get("tier", "free"))
elif args.command == "beta-allowance":
    from .api import beta_invite_id
    if args.email.count("@") != 1 or not 1 <= args.daily_limit <= 1000:
        p.error("Provide one email address and a daily limit from 1 through 1000.")
    db().beta_allowances.update_one({"_id": beta_invite_id(args.email)},
                                   {"$set": {"limit": args.daily_limit}}, upsert=True)
    print(f"Reserved {args.daily_limit} daily dubs for this verified email.")
else:
    db().accounts.update_one({"_id": "apple:" + args.apple_subject}, {"$set": {"tier": args.tier}}, upsert=True)
    print("Account tier updated.")
