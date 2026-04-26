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
    u = os.environ.get("TELEGRAM_USER_ID", "").strip()
    return f"suri-user-{u}" if u else "suri-user-default"


def _get_or_create_plaid_api_user_id(client) -> tuple[str | None, str | None]:
    """Plaid `user_id` from /user/create — required when using multi-item Link (Dec 2025+).
    Persisted in user_facts. Returns (user_id, error_message)."""
    existing = db.user_facts().get("plaid_user_id", "").strip()
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
        db.set_fact("plaid_user_id", str(uid))
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


def _serialize_tx(t) -> dict:
    if isinstance(t, dict):
        amt = t.get("amount")
        dts = t.get("date") or t.get("authorized_date")
        if dts is not None and not isinstance(dts, str) and hasattr(dts, "isoformat"):
            dts = dts.isoformat()
        elif dts is not None:
            dts = str(dts)
        name = t.get("name") or ""
        mch = t.get("merchant_name") or ""
    else:
        amt = getattr(t, "amount", None)
        dt = getattr(t, "date", None) or getattr(t, "authorized_date", None)
        if hasattr(dt, "isoformat"):
            dts = dt.isoformat()
        else:
            dts = str(dt) if dt is not None else None
        name = getattr(t, "name", None) or ""
        mch = getattr(t, "merchant_name", None) or ""
    return {
        "date": dts,
        "amount": float(amt) if amt is not None else None,
        "name": str(name)[:200],
        "merchant_name": str(mch)[:200] if mch else None,
    }


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


def sync_transactions(item_id: str | None) -> dict:
    """Run /transactions/sync for one or all items; returns added slice + cursor progress."""
    client = _get_client()
    if client is None:
        return {"ok": False, "error": "plaid not configured"}
    from plaid.model.transactions_sync_request import TransactionsSyncRequest

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
        cur = row.get("cursor")
        try:
            added: list[dict] = []
            has_more = True
            n_cursor: str | None = cur
            while has_more:
                req = TransactionsSyncRequest(
                    access_token=at, cursor=n_cursor, count=200
                )
                resp = client.transactions_sync(req)
                d = (
                    resp.to_dict()
                    if hasattr(resp, "to_dict")
                    else (dict(resp) if resp is not None else {})
                )
                for t in d.get("added") or []:
                    added.append(_serialize_tx(t))
                has_more = bool(d.get("has_more"))
                n_cursor = d.get("next_cursor")
            db.set_plaid_cursor(iid, n_cursor)
            per_item.append(
                {
                    "item_id": iid,
                    "institution_name": row.get("institution_name"),
                    "new_transactions_fetched": len(added),
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
