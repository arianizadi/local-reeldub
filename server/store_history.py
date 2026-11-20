"""Recover missed V2 callbacks with the same verifier used by the live endpoint."""
import asyncio
import hashlib
from fastapi import HTTPException
from datetime import timedelta
import logging
import os
from pathlib import Path
from uuid import uuid4

from pymongo import ReturnDocument

from . import store
from .db import db, now


def configured():
    return store.enabled() and all(os.getenv(k) for k in ("STORE_KEY_PATH", "STORE_KEY_ID", "STORE_ISSUER_ID"))


def client():
    from appstoreserverlibrary.api_client import AppStoreServerAPIClient
    from appstoreserverlibrary.models.Environment import Environment
    return AppStoreServerAPIClient(Path(os.environ["STORE_KEY_PATH"]).read_bytes(), os.environ["STORE_KEY_ID"],
                                   os.environ["STORE_ISSUER_ID"], os.environ["APPLE_CLIENT_ID"],
                                   Environment(os.environ["STORE_ENVIRONMENT"]))


def recover():
    from appstoreserverlibrary.models.NotificationHistoryRequest import NotificationHistoryRequest
    stamp = now()
    key = os.environ["STORE_ENVIRONMENT"]
    token = str(uuid4())
    states = db().store_history
    states.update_one({"_id": key}, {"$setOnInsert": {"due": stamp}}, upsert=True)
    state = states.find_one_and_update({"_id": key, "due": {"$lte": stamp}},
        {"$set": {"lease": token, "due": stamp + timedelta(minutes=10)}}, return_document=ReturnDocument.AFTER)
    if not state:
        return
    fence = {"_id": key, "lease": token}
    # Leave one minute for newly arriving events. Re-read ten minutes at window boundaries.
    end = state.get("end") or stamp - timedelta(minutes=1)
    start = state.get("start") or max(stamp - timedelta(days=29),
                                      state.get("through", stamp - timedelta(days=29)) - timedelta(minutes=10))
    page_token = state.get("page")
    request = NotificationHistoryRequest(startDate=int(start.timestamp()*1000), endDate=int(end.timestamp()*1000))
    service = client()
    for _ in range(5):
        page = service.get_notification_history(page_token, request)
        for item in page.notificationHistory or []:
            if item.signedPayload:
                try:
                    store.handle_notification(item.signedPayload)
                except HTTPException as error:
                    if error.status_code not in (400,403,409):
                        raise
                    digest = hashlib.sha256(item.signedPayload.encode()).hexdigest()
                    db().store_quarantine.update_one({"_id":digest},{"$set":{"status":error.status_code,"seen":now()}},upsert=True)
                    logging.warning("Skipped invalid Apple history item %s (%d)",digest[:12],error.status_code)
        if not page.hasMore:
            states.update_one(fence, {"$set": {"through": end, "due": now() + timedelta(minutes=30)},
                                       "$unset": {"lease": "", "page": "", "start": "", "end": ""}})
            return
        page_token = page.paginationToken
        if not page_token:
            raise RuntimeError("Apple did not supply the next notification-history page.")
        if not states.update_one(fence, {"$set": {"start": start, "end": end, "page": page_token,
                                                  "due": now() + timedelta(minutes=10)}}).matched_count:
            return
    states.update_one(fence, {"$set": {"due": now()}, "$unset": {"lease": ""}})


async def run():
    while True:
        try:
            await asyncio.to_thread(recover)
        except Exception as error:
            logging.warning("Apple notification history will retry (%s)", type(error).__name__)
        await asyncio.sleep(1800 if os.getenv("REELDUB_POSTGRES_DSN_FILE") or os.getenv("REELDUB_POSTGRES_DSN") else 60)
