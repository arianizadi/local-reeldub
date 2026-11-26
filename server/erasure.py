"""Apply historical account deletion receipts during an isolated backup restore."""
import hashlib


def billing_id(user):
    return "retired:" + hashlib.sha256(("reeldub/billing/" + user).encode()).hexdigest()


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
