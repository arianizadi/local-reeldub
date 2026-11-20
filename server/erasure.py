"""Retryable account erasure and the minimum ledger needed to restore App Store purchases."""
from datetime import timedelta
import hashlib
import os
from pathlib import Path

import gridfs
import jwt
import requests

from .db import media_db, db, now


def billing_id(user):
    return "retired:" + hashlib.sha256(("reeldub/billing/" + user).encode()).hexdigest()


def closing(user):
    return db().account_deletions.find_one({"_id": user}, {"_id": 1}) is not None


def restore_billing(user):
    retired = billing_id(user)
    link = db().billing_bindings.find_one({"_id": retired})
    if link:
        db().accounts.update_one({"_id": user}, {"$set": {"store_token": link["token"]}})
        db().purchases.update_many({"owner": retired}, {"$set": {"owner": user}})
    db().jobs.update_many({"owner": retired}, {"$set": {"owner": user}})


def erase(user):
    database = db()
    # Repeated cleanup also catches an old request that passed authorization immediately before deletion.
    database.sessions.delete_many({"owner": user})
    database.playback.delete_many({"owner": user})
    database.purchase_intents.delete_many({"owner": user})
    account = database.accounts.find_one({"_id": user}, {"store_token": 1}) or {}
    retired = billing_id(user)
    if account.get("store_token"):
        database.billing_bindings.update_one({"_id": retired}, {"$set": {"token": account["store_token"]}}, upsert=True)
    database.purchases.update_many({"owner": user}, {"$set": {"owner": retired}})
    database.jobs.update_many({"owner": user}, {"$set": {"deleted": True}, "$unset": {"lease": "", "lease_end": ""}})
    bucket = gridfs.GridFS(media_db())
    database.quotes.delete_many({"owner":user})
    for source in database.sources.find({"owner":user},{"_id":1}):
        bucket.delete(source["_id"])
        database.sources.delete_one({"_id":source["_id"]})
    for job in database.jobs.find({"owner": user}, {"artifact": 1, "extra_ticket": 1, "skip_ticket": 1, "minute_grants":1, "minute_units":1,"state":1}):
        if "artifact" in job:
            bucket.delete(job["artifact"])
        if job.get("state") == "done" and (job.get("minute_units") or job.get("extra_ticket") or job.get("skip_ticket")):
            database.jobs.update_one({"_id": job["_id"], "owner": user}, [{"$project": {
                "owner": {"$literal": retired}, "request_id": "$_id", "deleted": {"$literal": True},
                "state": {"$literal": "done"}, "extra_ticket": 1, "skip_ticket": 1, "minute_grants":1, "minute_units":1}}])
        else:
            database.jobs.delete_one({"_id": job["_id"], "owner": user})
    database.beta_allowances.delete_many({"owner": user})
    database.accounts.delete_one({"_id": user})
    stamp = now()
    # This also prevents erasing and creating an account repeatedly to replenish today's allowance.
    until = max(stamp + timedelta(minutes=15), stamp.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1))
    database.account_deletions.update_one({"_id": user, "expires": {"$exists": False}},
                                           {"$set": {"expires": until, "retry_until": stamp + timedelta(minutes=5)}})


def resume():
    for marker in db().account_deletions.find({"$or": [{"expires": {"$exists": False}},
                                                       {"retry_until": {"$gt": now()}}]}, {"_id": 1}):
        erase(marker["_id"])


def revoke_apple(code, user, keys):
    secret_file = os.getenv("APPLE_SIGNIN_CLIENT_SECRET_FILE")
    if not secret_file or not code:
        return False
    audience = os.environ["APPLE_CLIENT_ID"]
    try:
        form = {"client_id": audience, "client_secret": Path(secret_file).read_text().strip()}
        response = requests.post("https://appleid.apple.com/auth/token",
                                 data={**form, "code": code, "grant_type": "authorization_code"},
                                 timeout=(3, 6), allow_redirects=False)
        response.raise_for_status()
        tokens = response.json()
        identity = tokens["id_token"]
        claims = jwt.decode(identity, keys.get_signing_key_from_jwt(identity).key, algorithms=["RS256"],
                            audience=audience, issuer="https://appleid.apple.com", options={"require":["exp","sub"]})
        if "apple:" + claims["sub"] != user:
            return False
        field = "refresh_token" if tokens.get("refresh_token") else "access_token"
        revoked = requests.post("https://appleid.apple.com/auth/revoke",
                                data={**form, "token": tokens[field], "token_type_hint": field},
                                timeout=(3, 6), allow_redirects=False)
        return revoked.status_code == 200
    except (OSError, ValueError, KeyError, jwt.PyJWTError, requests.RequestException):
        return False
