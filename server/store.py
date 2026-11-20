"""Apple-signed purchases and uniquely spendable units on standalone MongoDB."""
from datetime import datetime, timezone
from functools import lru_cache
import os
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import HTTPException
from pymongo.errors import DuplicateKeyError

from .db import db, now

PREFIX = "com.arianizadi.reeldub."
MONTHLY = PREFIX + "premium.monthly"
CREATOR = PREFIX + "creator.monthly"
MINUTE_PACK = PREFIX + "minutes15"
SUBSCRIPTIONS = (MONTHLY, CREATOR)
# Additional translations, priority passes per purchased unit.
PACKS = {PREFIX + "skip1": (0, 1), PREFIX + "mini": (5, 1), PREFIX + "studio": (30, 4)}


def enabled():
    return os.getenv("STORE_ENVIRONMENT") in ("Production", "Sandbox")


@lru_cache
def verifier():
    from appstoreserverlibrary.models.Environment import Environment
    from appstoreserverlibrary.signed_data_verifier import SignedDataVerifier
    if not enabled():
        raise HTTPException(503, "The store is unavailable on this server.")
    env = Environment(os.environ["STORE_ENVIRONMENT"])
    root = Path(__file__).with_name("certs") / "AppleRootCA-G3.cer"
    return SignedDataVerifier([root.read_bytes()], True, env, os.environ["APPLE_CLIENT_ID"],
                              int(os.environ["STORE_APP_ID"]))


def verify(signed, notification=False, *, renewal=False):
    from appstoreserverlibrary.signed_data_verifier import VerificationException, VerificationStatus
    v = verifier()
    try:
        if notification:
            return v.verify_and_decode_notification(signed)
        if renewal:
            return v.verify_and_decode_renewal_info(signed)
        return v.verify_and_decode_signed_transaction(signed)
    except VerificationException as exc:
        if exc.status == VerificationStatus.RETRYABLE_VERIFICATION_FAILURE:
            raise HTTPException(503, "Apple verification is temporarily unavailable.") from exc
        raise HTTPException(400, "Apple could not verify this purchase.") from exc


def store_token(user):
    account = db().accounts.find_one({"_id": user}, {"store_token": 1}) or {}
    if not account.get("store_token"):
        # Existing Sign in with Apple accounts predate the StoreKit account identifier.
        db().accounts.update_one({"_id": user}, [{"$set": {"store_token": {
            "$ifNull": ["$store_token", str(uuid4())]}}}], upsert=True)
        account = db().accounts.find_one({"_id": user}, {"store_token": 1})
    return account["store_token"]


def record(tx, user=None):
    product = tx.productId
    original = str(getattr(tx, "originalTransactionId", None) or tx.transactionId or "")
    series = {"environment": tx.rawEnvironment, "original": original, "product": product}
    bound = db().purchases.find_one(series, {"owner": 1}) if product in SUBSCRIPTIONS and original else None
    if tx.appAccountToken is None and bound:
        account = {"_id": bound["owner"]}
    else:
        try:
            token = str(UUID(str(tx.appAccountToken)))
        except (ValueError, AttributeError):
            if user is None:
                return None
            raise HTTPException(400, "This purchase needs a verified ReelDub account association.")
        account = db().accounts.find_one({"store_token": token}, {"_id": 1})
        if not account and user is None:
            account = db().billing_bindings.find_one({"token": token}, {"_id": 1})
            if not account:
                return None
        if bound and account and bound["owner"] != account["_id"]:
            raise HTTPException(403, "Subscription belongs to another ReelDub account.")
    if not account or (user is not None and account["_id"] != user):
        raise HTTPException(403, "This purchase belongs to another ReelDub account.")
    if product not in PACKS and product not in (*SUBSCRIPTIONS, MINUTE_PACK):
        raise HTTPException(400, "Unknown ReelDub product.")
    correct_type = "Auto-Renewable Subscription" if product in SUBSCRIPTIONS else "Consumable"
    if (tx.rawType != correct_type or tx.rawInAppOwnershipType != "PURCHASED" or not tx.transactionId
            or not tx.signedDate or not tx.quantity or not 1 <= tx.quantity <= 10
            or (product in SUBSCRIPTIONS and not tx.expiresDate)):
        raise HTTPException(400, "Incomplete Apple transaction.")
    key = f"{tx.rawEnvironment}:{tx.transactionId}"
    credits, skips = PACKS.get(product, (0, 0))
    credits *= tx.quantity
    skips *= tx.quantity
    revoked = tx.revocationDate is not None or bool(tx.isUpgraded)
    # For a prorated pack refund keep the unrefunded fraction, rounded up to whole units.
    seconds = 900 * tx.quantity if product == MINUTE_PACK else 0
    if (product in PACKS or product == MINUTE_PACK) and getattr(tx, "rawRevocationType", None) == "REFUND_PRORATED" and tx.revocationDate is not None:
        percent = tx.revocationPercentage
        if not isinstance(percent, int) or not 0 <= percent <= 100000:
            raise HTTPException(400, "Incomplete prorated refund.")
        credits = (credits * (100000 - percent) + 99999) // 100000
        skips = (skips * (100000 - percent) + 99999) // 100000
        seconds = (seconds * (100000 - percent) + 99999) // 100000
        revoked = percent == 100000
    data = dict(owner=account["_id"], product=product, environment=tx.rawEnvironment, signed=tx.signedDate,
                revoked=revoked, credits=credits, skips=skips, seconds=seconds, original=original)
    if tx.expiresDate:
        data["expires"] = datetime.fromtimestamp(tx.expiresDate / 1000, timezone.utc)
    # Only a newer Apple-signed revision can alter an existing transaction.
    try:
        db().purchases.update_one({"_id": key, "owner": account["_id"], "signed": {"$lt": tx.signedDate}},
                                  {"$set": data}, upsert=True)
    except DuplicateKeyError:
        if not db().purchases.find_one({"_id": key, "owner": account["_id"]}, {"_id": 1}):
            raise HTTPException(409, "Transaction already claimed.")
    if product in SUBSCRIPTIONS and revoked:
        update_grace(series, key, tx.signedDate, None)
    return key


def update_grace(series, purchase, signed, until):
    key = series["environment"] + ":" + series["original"]
    try:
        db().subscription_status.update_one({"_id":key,"signed":{"$lt":signed}},
            {"$set":{**series,"purchase":purchase,"signed":signed,"until":until}},upsert=True)
    except DuplicateKeyError:
        pass  # A newer signed subscription status already won.


def handle_notification(signed):
    event = verify(signed, notification=True)
    # We do not collect consent for consumption-data sharing. A refund request alone changes no access.
    if event.rawNotificationType in ("CONSUMPTION_REQUEST", "TEST") or not event.data or not event.data.signedTransactionInfo:
        return
    tx = verify(event.data.signedTransactionInfo)
    if (tx.productId not in PACKS and tx.productId not in (*SUBSCRIPTIONS, MINUTE_PACK)):
        return
    key = record(tx)
    if not key or tx.productId not in SUBSCRIPTIONS or event.data.rawStatus is None or not event.signedDate:
        return
    grace_until = None
    if event.data.rawStatus == 4:
        if not event.data.signedRenewalInfo:
            raise HTTPException(400, "Missing signed renewal details.")
        renewal = verify(event.data.signedRenewalInfo, renewal=True)
        if (renewal.originalTransactionId != tx.originalTransactionId or renewal.productId != tx.productId
                or not renewal.gracePeriodExpiresDate):
            raise HTTPException(400, "Renewal does not match this subscription.")
        grace_until = datetime.fromtimestamp(renewal.gracePeriodExpiresDate / 1000, timezone.utc)
    series = {"environment":tx.rawEnvironment,"original":str(tx.originalTransactionId),"product":tx.productId}
    update_grace(series, key, event.signedDate, grace_until)


def active_period(user, products):
    stamp = now()
    env = os.getenv("STORE_ENVIRONMENT") or "Production"
    key = {"owner":user,"environment":env,"revoked":False,"product":{"$in":list(products)}}
    fresh = db().purchases.find_one({**key,"expires":{"$gt":stamp}},sort=[("expires",-1)])
    if fresh:
        return fresh
    # Only a signed status for a chain owned by this account can extend its last paid period.
    for purchase in db().purchases.find(key).sort("expires",-1):
        if db().subscription_status.find_one({"purchase":purchase["_id"],"until":{"$gt":stamp}},{"_id":1}):
            return purchase
    return None


def premium(user):
    return active_period(user, SUBSCRIPTIONS) is not None


def legacy_premium(user):
    return active_period(user, [MONTHLY]) is not None


def tickets(user, field):
    grant = {"extra_ticket": "credits", "skip_ticket": "skips"}[field]
    spent = set(db().jobs.distinct(field, {"owner": user, field: {"$exists": True}}))
    spent_counts = {}
    for ticket in spent:
        purchase_id = ticket.rpartition("/")[0]
        spent_counts[purchase_id] = spent_counts.get(purchase_id, 0) + 1
    for purchase in db().purchases.find({"owner": user, "environment": os.getenv("STORE_ENVIRONMENT") or "Production", "revoked": False, grant: {"$gt": 0}}, {grant: 1}).sort("_id", 1):
        available = purchase[grant] - spent_counts.get(purchase["_id"], 0)
        for index in range(purchase[grant]):
            if available <= 0:
                break
            ticket = f"{purchase['_id']}/{index}"
            if ticket not in spent:
                available -= 1
                yield ticket


def balances(user):
    return {name: sum(1 for _ in tickets(user, field)) for name, field in
            (("extra_translations", "extra_ticket"), ("queue_skips", "skip_ticket"))}
