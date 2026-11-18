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
args = p.parse_args()
if args.command == "reports":
    for job in db().jobs.find({"report": {"$exists": True}}, {"url": 1, "report": 1, "state": 1,
                              "stage": 1, "attempt": 1, "error": 1, "created": 1, "finished": 1}):
        print(json.dumps(job, default=str, ensure_ascii=False))
elif args.command == "accounts":
    for owner in db().sessions.distinct("owner", {"owner": {"$regex": "^apple:"}}):
        account = db().accounts.find_one({"_id": owner}) or {}
        print(owner.removeprefix("apple:"), account.get("tier", "free"))
else:
    db().accounts.update_one({"_id": "apple:" + args.apple_subject}, {"$set": {"tier": args.tier}}, upsert=True)
    print("Account tier updated.")
