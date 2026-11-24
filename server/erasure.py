"""Retryable account erasure and the minimum ledger needed to restore App Store purchases."""
from datetime import timedelta
import hashlib
import os
from pathlib import Path

import gridfs
import jwt
import requests

from .db import media_db, db, now
from . import erasure_journal


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


def erase_data(database, user, delete_blob=None):
    """Scrub an account. Isolated metadata restores never touch a media store."""
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
    database.quotes.delete_many({"owner":user})
    for source in database.sources.find({"owner":user},{"_id":1}):
        if delete_blob:
            delete_blob(source["_id"])
        database.sources.delete_one({"_id":source["_id"]})
    for job in database.jobs.find({"owner": user}, {"artifact": 1, "extra_ticket": 1, "skip_ticket": 1, "minute_grants":1, "minute_units":1,"state":1}):
        if delete_blob and "artifact" in job:
            delete_blob(job["artifact"])
        if job.get("state") == "done" and (job.get("minute_units") or job.get("extra_ticket") or job.get("skip_ticket")):
            database.jobs.update_one({"_id": job["_id"], "owner": user}, [{"$project": {
                "owner": {"$literal": retired}, "request_id": "$_id", "deleted": {"$literal": True},
                "state": {"$literal": "done"}, "extra_ticket": 1, "skip_ticket": 1, "minute_grants":1, "minute_units":1}}])
        else:
            database.jobs.delete_one({"_id": job["_id"], "owner": user})
    database.beta_allowances.delete_many({"owner": user})
    # Erase personal data while retaining the moderation decision for this Apple subject.
    database.accounts.delete_one({"_id":user,"banned":{"$ne":True}})
    database.accounts.update_one({"_id":user,"banned":True}, [{"$project":{"banned":1,"moderated_at":1,"erased":{"$literal":True}}}])


def erase(user):
    database = db()
    erase_data(database, user, gridfs.GridFS(media_db()).delete)
    stamp = now()
    # This also prevents erasing and creating an account repeatedly to replenish today's allowance.
    until = max(stamp + timedelta(minutes=15), stamp.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1))
    database.account_deletions.update_one({"_id": user, "expires": {"$exists": False}},
                                           {"$set": {"expires": until, "retry_until": stamp + timedelta(minutes=5)}})


def resume():
    for marker in db().account_deletions.find({"$or": [{"expires": {"$exists": False}},
                                                       {"retry_until": {"$gt": now()}}]}, {"_id": 1}):
        erase(marker["_id"])
    erasure_journal.prune(now())


def apple_client_secret(audience):
    # Generate a short-lived client secret on demand using the app-specific Apple signing key.
    key_path = os.getenv("APPLE_SIGNIN_PRIVATE_KEY_FILE")
    if key_path:
        team = os.environ["APPLE_SIGNIN_TEAM_ID"]
        key = os.environ["APPLE_SIGNIN_KEY_ID"]
        stamp = now()
        return jwt.encode({"iss": team, "iat": stamp, "exp": stamp + timedelta(minutes=5),
                           "aud": "https://appleid.apple.com", "sub": audience},
                          Path(key_path).read_bytes(), algorithm="ES256", headers={"kid": key})
    secret_file = os.getenv("APPLE_SIGNIN_CLIENT_SECRET_FILE")
    return Path(secret_file).read_text().strip() if secret_file else None


def revoke_apple(code, user, keys):
    if not code:
        return False
    try:
        audience = os.environ["APPLE_CLIENT_ID"]
        secret = apple_client_secret(audience)
        if not secret:
            return False
        form = {"client_id": audience, "client_secret": secret}
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
