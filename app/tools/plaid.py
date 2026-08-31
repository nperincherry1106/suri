"""Plaid: read-only access to bank/card transactions (via Transactions product only).

Suri never gets login credentials: you sign in through Plaid's own Link UI in the
browser. We only store Plaid-issued item tokens to pull transaction history and
Plaid's recurring stream signals. No moving money, no payments.

Secrets: PLAID_CLIENT_ID, PLAID_SECRET, PLAID_ENV (sandbox | production).
Public URL: SURI_PUBLIC_URL must be set for the hosted Link page + webhook.
"""
import json
import os
import secrets
import sys
from typing import Any

from app import db

_plaid: Any = None


def _plaid_error(e: Exception) -> str:
    if hasattr(e, "body") and e.body is not None:
        try:
            b = e.body
            if isinstance(b, (bytes, bytearray)):
                b = b.decode("utf-8", errors="replace")
            d = json.loads(b) if isinstance(b, str) else {}
            if isinstance(d, dict) and d.get("error_message"):
                return f"{d.get('error_type', 'plaid')}: {d.get('error_message')}"
        except (json.JSONDecodeError, TypeError, AttributeError, UnicodeDecodeError):
            pass
    return f"{type(e).__name__}: {e}"


def _get_client():
    global _plaid
    if _plaid is not None:
        return _plaid
    cid = os.environ.get("PLAID_CLIENT_ID", "").strip()
    secret = os.environ.get("PLAID_SECRET", "").strip()
    if not cid or not secret:
        return None
    from plaid import Configuration, Environment
    from plaid.api import plaid_api
    from plaid import ApiClient

    env = (os.environ.get("PLAID_ENV") or "sandbox").lower().strip()
    if env == "production":
        host = Environment.Production
    elif env in ("development", "dev"):
        host = Environment.Development
    else:
        host = Environment.Sandbox
    conf = Configuration(host=host, api_key={"clientId": cid, "secret": secret})
    _plaid = plaid_api.PlaidApi(ApiClient(conf))
    return _plaid


def _client_user_id() -> str:
    """Stable per-user ID handed to Plaid /user/create. Single-user app, so a
    fixed string is fine; pinning Namrita's Apple sub here once it's known
    would also work but isn't worth churning the existing Plaid user record."""
    return "suri-user-default"


def _plaid_user_fact_key() -> str:
    """Plaid /user/create IDs are per-environment — sandbox IDs break in production."""
    env = _plaid_env_label()
    if env == "production":
        return "plaid_user_id_production"
    if env in ("development", "dev"):
        return "plaid_user_id_development"
    return "plaid_user_id_sandbox"


def _get_or_create_plaid_api_user_id(client) -> tuple[str | None, str | None]:
    """Plaid `user_id` from /user/create — required when using multi-item Link (Dec 2025+).
    Persisted in user_facts per PLAID_ENV. Returns (user_id, error_message)."""
    key = _plaid_user_fact_key()
    existing = db.user_facts().get(key, "").strip()
    if not existing:
        legacy = db.user_facts().get("plaid_user_id", "").strip()
        if legacy and key == "plaid_user_id_sandbox":
            existing = legacy
        elif legacy:
            db.forget_fact("plaid_user_id")
    if existing:
        return existing, None
    from plaid.model.user_create_request import UserCreateRequest

    try:
        r = client.user_create(
            UserCreateRequest(client_user_id=_client_user_id())
        )
        d = r.to_dict() if hasattr(r, "to_dict") else {}
        uid = d.get("user_id") or (getattr(r, "user_id", None) if not d else None)
        if not uid:
            return None, "plaid /user/create returned no user_id (check Plaid dashboard / API access)"
        db.set_fact(key, str(uid))
        if key != "plaid_user_id":
            db.forget_fact("plaid_user_id")
        return str(uid), None
    except Exception as e:
        err = _plaid_error(e)
        print(f"[plaid] user_create failed: {err}", file=sys.stderr, flush=True)
        return None, f"plaid /user/create failed: {err}"


def public_base_url() -> str | None:
    u = os.environ.get("SURI_PUBLIC_URL", "").rstrip("/")
    return u or None


def _plaid_env_label() -> str:
    return (os.environ.get("PLAID_ENV") or "sandbox").strip().lower() or "sandbox"


def read_only_promise() -> str:
    return (
        "Suri only requests read-only access to transaction data (and recurring "
        "charge signals) through Plaid. She cannot move money or see your bank "
        "password; Plaid handles login."
    )


def user_facing_steps() -> list[str]:
    """Short instructions for the host page and for the agent to paste in chat."""
    if _plaid_env_label() in ("production", "development", "dev"):
        return [
            "Tap 'open plaid' and sign in to your bank like any banking app. "
            "You can add more than one bank in the same session when Plaid offers it — "
            "link everything you want Suri to read.",
            "Suri only sees your transactions (read-only) — not a way to send money or spend.",
        ]
    return [
        "This is Plaid TEST mode (sandbox). Do not use your real bank password — it won't work.",
        "Search for 'First Platypus Bank' (or 'ins_109508') and log in with "
        "username: user_good  /  password: pass_good  (Plaid's official test account).",
        "If a phone number is required: use 415-555-0010  (and if asked for a code, try 123456). "
        "Real phone numbers are rejected in sandbox on purpose.",
        "In one session you can add multiple test banks when Plaid offers 'add another' — link all you need.",
        "In production, you'd use your real bank; here we're just proving the pipe works. "
        + read_only_promise(),
    ]


def is_configured() -> bool:
    c = _get_client()
    return c is not None and bool(public_base_url())


def _config_status() -> dict:
    """Why Plaid may be unavailable (no secret values; safe to log in tool results)."""
    cid = bool(os.environ.get("PLAID_CLIENT_ID", "").strip())
    sec = bool(os.environ.get("PLAID_SECRET", "").strip())
    suri = bool(public_base_url())
    return {
        "plaid_client_id_set": cid,
        "plaid_secret_set": sec,
        "suri_public_url_set": suri,
        "suri_public_url_len": len(os.environ.get("SURI_PUBLIC_URL", "")),
        "plaid_env": (os.environ.get("PLAID_ENV") or "sandbox").strip().lower()
        or "sandbox",
    }


def _to_dict(t) -> dict:
    """Coerce a Plaid SDK model OR dict into a plain dict so downstream
    extractors don't have to fork on type. SDK objects expose to_dict()."""
    if isinstance(t, dict):
        return t
    if hasattr(t, "to_dict"):
        try:
            return t.to_dict()
        except Exception:
            pass
    return {k: getattr(t, k) for k in dir(t) if not k.startswith("_")}


def _iso(d) -> str | None:
    if d is None:
        return None
    if hasattr(d, "isoformat"):
        return d.isoformat()
    return str(d)


def _serialize_tx(t) -> dict:
    """Compact representation used in the agent's tool-output sample (kept
    short on purpose — the model doesn't need 30 fields per tx)."""
    d = _to_dict(t)
    amt = d.get("amount")
    return {
        "date": _iso(d.get("date") or d.get("authorized_date")),
        "amount": float(amt) if amt is not None else None,
        "name": str(d.get("name") or "")[:200],
        "merchant_name": str(d.get("merchant_name") or "")[:200] or None,
    }


def _persist_tx(t, item_id: str) -> str | None:
    """Upsert a single Plaid transaction into the local DB. Returns the
    transaction_id on success, or None if the payload was missing the
    required fields (we log + skip rather than crash the sync loop)."""
    d = _to_dict(t)
    txid = d.get("transaction_id")
    date = _iso(d.get("date") or d.get("authorized_date"))
    amt = d.get("amount")
    if not txid or not date or amt is None:
        print(
            f"[plaid] skipping tx with missing required fields: "
            f"id={txid!r} date={date!r} amount={amt!r}",
            file=sys.stderr,
            flush=True,
        )
        return None
    pcc = d.get("personal_finance_category") or {}
    if not isinstance(pcc, dict):
        pcc = _to_dict(pcc)
    raw_min = {
        k: d.get(k)
        for k in (
            "transaction_id", "account_id", "amount", "iso_currency_code",
            "date", "authorized_date", "name", "merchant_name", "pending",
            "personal_finance_category", "payment_channel", "category",
            "category_id", "transaction_type",
        )
        if k in d
    }
    db.upsert_plaid_transaction(
        transaction_id=str(txid),
        item_id=item_id,
        account_id=d.get("account_id"),
        amount=float(amt),
        iso_currency_code=d.get("iso_currency_code"),
        date=date,
        authorized_date=_iso(d.get("authorized_date")),
        name=(d.get("name") or "")[:300] or None,
        merchant_name=(d.get("merchant_name") or "")[:300] or None,
        pending=bool(d.get("pending")),
        category_primary=(pcc.get("primary") if isinstance(pcc, dict) else None),
        category_detailed=(pcc.get("detailed") if isinstance(pcc, dict) else None),
        payment_channel=d.get("payment_channel"),
        raw_json=json.dumps(raw_min, default=str),
    )
    return str(txid)


def _transactions_sync_request(access_token: str, cursor: str | None, count: int = 200):
    """Plaid's SDK rejects cursor=None (must be str); omit the field for first-time sync."""
    from plaid.model.transactions_sync_request import TransactionsSyncRequest

    c = (cursor or "").strip()
    if c:
        return TransactionsSyncRequest(
            access_token=access_token, cursor=c, count=count
        )
    return TransactionsSyncRequest(access_token=access_token, count=count)


def start_link() -> dict:
    """Create a one-time Plaid Link session; return a URL to open in the browser."""
    status = _config_status()
    if not status["plaid_client_id_set"] or not status["plaid_secret_set"]:
        return {
            "ok": False,
            "error": (
                "Missing PLAID_CLIENT_ID and/or PLAID_SECRET in the *running* app env "
                "(e.g. fly secrets set on app suri, then wait for restart). "
                "Local .env does not apply to Fly."
            ),
            "config": status,
        }
    if not public_base_url():
        return {
            "ok": False,
            "error": (
                "SURI_PUBLIC_URL is empty in the running process. In fly.toml it should be "
                "set under [env], OR set with fly secrets set SURI_PUBLIC_URL=https://suri.fly.dev "
                "— if you set it as a secret, make sure the value is not empty (empty overrides fly.toml)."
            ),
            "config": status,
        }
    from plaid.model.country_code import CountryCode
    from plaid.model.link_token_create_request import LinkTokenCreateRequest
    from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
    from plaid.model.products import Products

    client = _get_client()
    if client is None:
        return {"ok": False, "error": "plaid client unavailable"}

    base = public_base_url()
    webhook = f"{base}/plaid/webhook" if base else None

    try:
        # Multi-item Link requires a Plaid user from /user/create (see
        # https://plaid.com/docs/link/multi-item-link/ ).
        puid, uerr = _get_or_create_plaid_api_user_id(client)
        if uerr or not puid:
            return {
                "ok": False,
                "error": uerr or "could not create Plaid user",
                "config": status,
                "hint": "Plaid now requires /user/create before link when multi-bank in one session is enabled. This should be automatic — if it failed, the message above is from Plaid's API.",
            }
        req = LinkTokenCreateRequest(
            user_id=puid,
            user=LinkTokenCreateRequestUser(client_user_id=_client_user_id()),
            client_name="Suri",
            products=[Products("transactions")],
            country_codes=[CountryCode("US")],
            language="en",
            webhook=webhook,
            enable_multi_item_link=True,
        )
        resp = client.link_token_create(req)
        if isinstance(resp, dict):
            link_token = resp.get("link_token")
        else:
            d = resp.to_dict() if hasattr(resp, "to_dict") else {}
            link_token = d.get("link_token") or getattr(resp, "link_token", None)
        if not link_token:
            return {"ok": False, "error": "plaid did not return link_token"}
    except Exception as e:
        print(f"[plaid] link_token_create failed: {_plaid_error(e)}", file=sys.stderr, flush=True)
        return {"ok": False, "error": _plaid_error(e)}

    db.prune_stale_plaid_link_sessions(older_than_minutes=30)
    sid = secrets.token_hex(8)
    db.create_plaid_link_session(sid, link_token)
    connect_url = f"{base}/plaid/link/{sid}"
    return {
        "ok": True,
        "connect_url": connect_url,
        "read_only": True,
        "plaid_mode": _plaid_env_label(),
        "what_suri_gets": read_only_promise(),
        "say_this_to_namrita": user_facing_steps(),
        "note": (
            "Paste connect_url. After she finishes in the browser, run plaid_list_items — "
            "then plaid_sync_transactions and plaid_recurring for card-side recurring charges."
        ),
    }


def list_items() -> dict:
    """List linked Plaid items (no raw tokens)."""
    status = _config_status()
    rows = db.list_plaid_items_public()
    if not rows:
        return {
            "ok": True,
            "items": [],
            "config": status,
            "note": (
                "No bank accounts in the database yet. If tools said Plaid is not configured, "
                "use the config flags above. Call plaid_start_link for a connect URL once "
                "PLAID_* and SURI_PUBLIC_URL are set in the *deployed* environment."
            ),
        }
    return {"ok": True, "items": rows, "config": status}


def exchange_public_token(public_token: str) -> dict:
    """Server-side: exchange public_token, persist item + access token."""
    try:
        return _exchange_public_token_impl(public_token)
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        print(f"[plaid] exchange_public_token fatal: {err}", file=sys.stderr, flush=True)
        return {"ok": False, "error": err}


def _exchange_public_token_impl(public_token: str) -> dict:
    client = _get_client()
    if client is None:
        return {"ok": False, "error": "plaid not configured"}
    from plaid.model.item_get_request import ItemGetRequest
    from plaid.model.item_public_token_exchange_request import (
        ItemPublicTokenExchangeRequest,
    )

    try:
        ex = client.item_public_token_exchange(
            ItemPublicTokenExchangeRequest(public_token=public_token)
        )
        access = getattr(ex, "access_token", None)
        item_id = getattr(ex, "item_id", None)
        d: dict = {}
        if (not access or not item_id) and hasattr(ex, "to_dict"):
            d = ex.to_dict() or {}
            access = access or d.get("access_token")
            item_id = item_id or d.get("item_id")
        if not access or not item_id:
            return {
                "ok": False,
                "error": "public_token exchange returned no access_token or item_id",
                "raw_keys": list(d.keys()) if d else "no to_dict on response",
            }
    except Exception as e:
        return {"ok": False, "error": _plaid_error(e)}

    inst_id = None
    inst_name = None
    try:
        igr = client.item_get(ItemGetRequest(access_token=access))
        d2 = igr.to_dict() if hasattr(igr, "to_dict") else {}
        item = d2.get("item")
        if item:
            iid = item.get("institution_id")
            if iid:
                inst_id = str(iid)
            iname = item.get("institution_name")
            if not iname and inst_id:
                from plaid.model.institutions_get_by_id_request import (
                    InstitutionsGetByIdRequest,
                )
                from plaid.model.country_code import CountryCode

                inst_resp = client.institutions_get_by_id(
                    InstitutionsGetByIdRequest(
                        institution_id=inst_id,
                        country_codes=[CountryCode("US")],
                    )
                )
                inst_d = inst_resp.to_dict() if hasattr(inst_resp, "to_dict") else {}
                ins = inst_d.get("institution") or {}
                iname = ins.get("name")
            inst_name = iname
    except Exception as e:
        print(f"[plaid] item_get/inst name after exchange: {e}", file=sys.stderr, flush=True)

    try:
        db.upsert_plaid_item(
            str(item_id),
            str(access),
            inst_id,
            str(inst_name) if inst_name else None,
        )
    except Exception as e:
        return {
            "ok": False,
            "error": f"sqlite (saving item): {type(e).__name__}: {e}. Is /data volume writable?",
        }

    return {
        "ok": True,
        "item_id": str(item_id),
        "institution_id": inst_id,
        "institution_name": inst_name,
    }


def unlink_item(item_id: str | None = None, all_items: bool = False) -> dict:
    """Remove one Plaid item (or all of them) from the DB and revoke the
    access_token via Plaid /item/remove. Idempotent — Plaid 4xx errors do not
    block the local delete (e.g. if the env was switched and the token is now
    invalid for the new env). Used to clean up sandbox items before cutting
    over to production Plaid."""
    if not item_id and not all_items:
        return {
            "ok": False,
            "error": "pass item_id, or set all_items=true to remove every linked item",
        }

    targets: list[dict] = []
    if all_items:
        for p in db.list_plaid_items_public():
            row = db.get_plaid_item(p["item_id"])
            if row:
                targets.append(row)
    else:
        row = db.get_plaid_item(str(item_id))
        if row is None:
            return {"ok": False, "error": f"unknown item_id: {item_id}"}
        targets.append(row)

    if not targets:
        return {"ok": True, "removed": [], "note": "no linked items to remove"}

    client = _get_client()
    removed: list[dict] = []
    plaid_errors: list[dict] = []
    for row in targets:
        iid = row["item_id"]
        at = row["access_token"]
        nm = row.get("institution_name") or iid
        if client is not None:
            try:
                from plaid.model.item_remove_request import ItemRemoveRequest

                client.item_remove(ItemRemoveRequest(access_token=at))
            except Exception as e:
                plaid_errors.append({"item_id": iid, "error": _plaid_error(e)})
                print(
                    f"[plaid] item_remove non-fatal: item_id={iid} err={_plaid_error(e)}",
                    file=sys.stderr,
                    flush=True,
                )
        try:
            db.delete_plaid_item(iid)
        except Exception as e:
            plaid_errors.append(
                {"item_id": iid, "error": f"sqlite delete failed: {e}"}
            )
            continue
        removed.append({"item_id": iid, "institution_name": nm})

    return {
        "ok": True,
        "removed": removed,
        "plaid_errors": plaid_errors,
        "note": (
            "access_token revoked at Plaid (best-effort) and row deleted locally. "
            "if you switched PLAID_ENV, plaid_errors are expected — local rows are gone either way."
        ),
    }


def _duplicate_public_token_error(err: str) -> bool:
    u = (err or "").upper()
    if "INVALID_PUBLIC_TOKEN" in u:
        return True
    if "INVALID" in u and "PUBLIC" in u and "TOKEN" in u:
        return True
    return False


def process_link_webhook(body: dict) -> dict:
    """Multi-Item Link does not call the browser onSuccess with a public_token; Plaid
    sends public_token(s) in LINK webhooks (SESSION_FINISHED, ITEM_ADD_RESULT)."""
    wtype = (body.get("webhook_type") or "").strip().upper()
    wcode = body.get("webhook_code")
    if wtype and wtype != "LINK":
        return {"ok": True, "ignored": True, "webhook_type": wtype}
    if wcode not in ("SESSION_FINISHED", "ITEM_ADD_RESULT"):
        return {"ok": True, "ignored": True, "webhook_code": wcode}
    tokens: list[str] = []
    if wcode == "SESSION_FINISHED":
        if (body.get("status") or "").strip().upper() != "SUCCESS":
            return {
                "ok": True,
                "skipped": True,
                "reason": "status_not_success",
                "status": body.get("status"),
            }
        for t in body.get("public_tokens") or []:
            if isinstance(t, str) and t:
                tokens.append(t)
        leg = body.get("public_token")
        if isinstance(leg, str) and leg and leg not in tokens:
            tokens.append(leg)
    else:
        pt = body.get("public_token")
        if isinstance(pt, str) and pt:
            tokens.append(pt)
    tokens = list(dict.fromkeys(tokens))
    if not tokens:
        return {"ok": True, "exchanged_item_ids": [], "note": "no public_token(s) in payload"}
    exchanged: list[str] = []
    errors: list[dict] = []
    for pt in tokens:
        r = exchange_public_token(pt)
        if r.get("ok"):
            eid = r.get("item_id")
            if eid:
                exchanged.append(str(eid))
            print(
                f"[plaid] LINK webhook item linked item_id={eid}",
                file=sys.stderr,
                flush=True,
            )
            continue
        emsg = r.get("error") or ""
        if _duplicate_public_token_error(emsg):
            print(
                f"[plaid] LINK webhook skip already-used public_token: {emsg[:160]}",
                file=sys.stderr,
                flush=True,
            )
        else:
            errors.append(
                {
                    "error": emsg,
                    "token_tail": pt[-12:] if len(pt) > 12 else pt,
                }
            )
    return {
        "exchanged_item_ids": exchanged,
        "errors": errors,
    }


def sync_transactions(item_id: str | None, reset_cursor: bool = False) -> dict:
    """Run /transactions/sync for one or all items; persists added/modified
    transactions and removes deletions. Returns per-item counts + a small
    sample of newly-added rows. If reset_cursor=True, wipes the cursor
    first so we re-pull the entire history (useful for backfill after
    adding the local plaid_transactions table)."""
    client = _get_client()
    if client is None:
        return {"ok": False, "error": "plaid not configured"}

    rows = []
    if item_id:
        r = db.get_plaid_item(item_id)
        if r is None:
            return {"ok": False, "error": f"unknown item_id: {item_id}"}
        rows = [r]
    else:
        allp = db.list_plaid_items_public()
        for p in allp:
            r = db.get_plaid_item(p["item_id"])
            if r:
                rows.append(r)

    if not rows:
        return {
            "ok": True,
            "message": "no linked items. use plaid_start_link first.",
            "per_item": [],
        }

    per_item: list[dict] = []
    for row in rows:
        iid = row["item_id"]
        at = row["access_token"]
        cur = None if reset_cursor else row.get("cursor")
        if reset_cursor:
            db.set_plaid_cursor(iid, None)
        try:
            added: list[dict] = []
            n_added = 0
            n_modified = 0
            n_removed = 0
            has_more = True
            n_cursor: str | None = cur
            while has_more:
                req = _transactions_sync_request(at, n_cursor, 200)
                resp = client.transactions_sync(req)
                d = (
                    resp.to_dict()
                    if hasattr(resp, "to_dict")
                    else (dict(resp) if resp is not None else {})
                )
                for t in d.get("added") or []:
                    added.append(_serialize_tx(t))
                    if _persist_tx(t, iid):
                        n_added += 1
                for t in d.get("modified") or []:
                    if _persist_tx(t, iid):
                        n_modified += 1
                removed_ids = []
                for r in d.get("removed") or []:
                    if isinstance(r, dict):
                        rid = r.get("transaction_id")
                    else:
                        rid = getattr(r, "transaction_id", None)
                    if rid:
                        removed_ids.append(str(rid))
                if removed_ids:
                    n_removed += db.delete_plaid_transactions(removed_ids)
                has_more = bool(d.get("has_more"))
                n_cursor = d.get("next_cursor")
            db.set_plaid_cursor(iid, n_cursor)
            per_item.append(
                {
                    "item_id": iid,
                    "institution_name": row.get("institution_name"),
                    "new_transactions_fetched": len(added),
                    "persisted_added": n_added,
                    "persisted_modified": n_modified,
                    "persisted_removed": n_removed,
                    "sample": added[:15],
                }
            )
        except Exception as e:
            per_item.append(
                {
                    "item_id": iid,
                    "ok": False,
                    "error": _plaid_error(e),
                }
            )

    ok_all = not any("error" in p for p in per_item)
    return {"ok": ok_all, "per_item": per_item}


def fetch_recurring() -> dict:
    """Call /transactions/recurring/get per linked item. May be empty until enough tx history."""
    client = _get_client()
    if client is None:
        return {"ok": False, "error": "plaid not configured"}
    from plaid.model.transactions_recurring_get_request import (
        TransactionsRecurringGetRequest,
    )

    out_in = []
    out_out = []
    errors: list[str] = []
    for p in db.list_plaid_items_public():
        r = db.get_plaid_item(p["item_id"])
        if r is None:
            continue
        at = r["access_token"]
        iid = r["item_id"]
        iname = r.get("institution_name")
        try:
            req = TransactionsRecurringGetRequest(access_token=at)
            resp = client.transactions_recurring_get(req)
            d = resp.to_dict() if hasattr(resp, "to_dict") else {}
            for stream in d.get("inflow_streams") or []:
                out_in.append(
                    {
                        "item_id": iid,
                        "institution_name": iname,
                        "stream": _stream_summary(stream, "inflow"),
                    }
                )
            for stream in d.get("outflow_streams") or []:
                out_out.append(
                    {
                        "item_id": iid,
                        "institution_name": iname,
                        "stream": _stream_summary(stream, "outflow"),
                    }
                )
        except Exception as e:
            errors.append(f"{iid}: {_plaid_error(e)}")

    if errors and not out_in and not out_out:
        return {
            "ok": False,
            "error": "; ".join(errors),
            "note": "For sandbox, add transactions via /sandbox/transactions/create in Plaid, or use a test institution with history.",
        }
    return {
        "ok": True,
        "inflow_recurring": out_in,
        "outflow_recurring": out_out,
        "inflow_count": len(out_in),
        "outflow_count": len(out_out),
    }


def _stream_summary(stream, direction: str) -> dict:
    if isinstance(stream, dict):
        return {
            "direction": direction,
            "name": (stream.get("description") or stream.get("merchant_name") or "")[:200],
            "frequency": str(stream.get("frequency") or ""),
            "status": str(stream.get("status") or ""),
            "last_amount": stream.get("last_amount")
            and (
                (stream["last_amount"].get("amount") if isinstance(stream.get("last_amount"), dict) else str(stream.get("last_amount")))
            ),
        }
    d = {
        "direction": direction,
        "name": (getattr(stream, "description", None) or getattr(stream, "merchant_name", None) or "") or "",
    }
    if hasattr(stream, "frequency") and stream.frequency is not None:
        d["frequency"] = str(stream.frequency)
    if hasattr(stream, "status") and stream.status is not None:
        d["status"] = str(stream.status)
    if hasattr(stream, "last_amount") and stream.last_amount is not None:
        la = stream.last_amount
        if hasattr(la, "amount"):
            d["last_amount"] = la.amount
        else:
            d["last_amount"] = str(la)
    return d
