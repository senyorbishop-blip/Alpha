"""
server/twitch_ext/routes.py — EBS HTTP API for the Twitch Extension.

Endpoints (all under /api/twitch/ext):
  POST /bind-session  — broadcaster binds channel -> live game session
  GET  /catalog       — viewer fetches purchasable known powers
  POST /transaction   — viewer's verified Bits receipt -> grant the named power
  POST /sub-claim     — subscriber claims a free known power (server-verified)

Security: every call verifies the Extension JWT signature + exp. Identity, sku,
and price are taken only from signed claims, never from the client body.
Compliance: Bits buy a single KNOWN power per SKU — there is no random path.
"""
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from server.twitch_ext import config as ext_config
from server.twitch_ext.jwt_verify import verify_ext_jwt, verify_bits_receipt, ExtJWTError
from server.twitch_ext import granting
from server.twitch_ext.granting import (
    SKU_TO_POWER,
    ResolveError,
    resolve_target,
    grant_known_power,
    bind_session,
    lookup_bound_session,
    lookup_binding_config,
    is_transaction_processed,
    mark_transaction_processed,
    check_rate_limit,
    sub_claim_remaining,
    record_sub_claim,
)
from server.handlers.viewer_powers import _viewer_power_defs, VIEWER_POWER_DEFS
from server.session import get_session

_logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/twitch/ext", tags=["twitch-ext"])


# ── helpers ────────────────────────────────────────────────────────────────────

def _cors_headers(request: Request) -> dict:
    # The Extension iframe is hosted on a Twitch domain; allow the calling origin.
    origin = request.headers.get("origin") or "*"
    return {
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Authorization, Content-Type",
        "Access-Control-Max-Age": "600",
        "Vary": "Origin",
    }


def _json(request: Request, payload: dict, status: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status, headers=_cors_headers(request))


def _not_configured(request: Request) -> JSONResponse:
    return _json(
        request,
        {
            "ok": False,
            "error": "extension_not_configured",
            "message": "The Twitch Extension is not configured on this server.",
            "missing": ext_config.missing_core_keys(),
        },
        status=503,
    )


def _bearer(request: Request) -> str:
    auth = request.headers.get("authorization") or request.headers.get("Authorization") or ""
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    return ""


def _catalog_for_session(session, channel_id: str) -> list[dict]:
    """Build the purchasable known-power list for a bound session."""
    defs = _viewer_power_defs(session) if session is not None else dict(VIEWER_POWER_DEFS)
    cfg = lookup_binding_config(channel_id)
    # Optional broadcaster-selected subset + costs.
    allowed = {str(p).strip().lower() for p in (cfg.get("purchasable_powers") or []) if str(p).strip()}
    costs = {str(k).strip().lower(): v for k, v in dict(cfg.get("costs") or {}).items()}
    items = []
    for sku, power_id in SKU_TO_POWER.items():
        if power_id not in defs:
            continue
        if allowed and power_id not in allowed:
            continue
        d = defs[power_id]
        item = {
            "power_id": power_id,
            "sku": sku,
            "name": str(d.get("name") or power_id),
            "description": str(d.get("description") or ""),
            "kind": str(d.get("kind") or ""),
            "cooldown_sec": int(d.get("cooldown_sec", 0) or 0),
            "requires_approval": bool(d.get("approval_default", False)),
        }
        if power_id in costs:
            try:
                item["bits_cost"] = int(costs[power_id])
            except (TypeError, ValueError):
                pass
        items.append(item)
    return items


# ── preflight ──────────────────────────────────────────────────────────────────

@router.options("/{rest_of_path:path}")
async def ext_preflight(rest_of_path: str, request: Request):
    return JSONResponse({"ok": True}, headers=_cors_headers(request))


# ── bind-session (broadcaster) ──────────────────────────────────────────────────

@router.post("/bind-session")
async def bind_session_route(request: Request):
    if not ext_config.ext_configured():
        return _not_configured(request)
    try:
        claims = verify_ext_jwt(_bearer(request))
    except ExtJWTError as exc:
        return _json(request, {"ok": False, "error": "unauthorized", "message": str(exc)}, status=401)
    if claims.get("role") != "broadcaster":
        return _json(request, {"ok": False, "error": "forbidden", "message": "Broadcaster role required."}, status=403)

    try:
        body = await request.json()
    except Exception:
        body = {}
    body = body if isinstance(body, dict) else {}
    session_id = str(body.get("session_id") or "").strip()
    if not session_id:
        return _json(request, {"ok": False, "error": "bad_request", "message": "session_id is required."}, status=400)
    if get_session(session_id) is None:
        return _json(request, {"ok": False, "error": "no_session", "message": "That game session is not live."}, status=404)

    channel_id = claims["channel_id"]  # authoritative — from the signed JWT, not the body
    config_payload = {
        "purchasable_powers": [str(p).strip().lower() for p in (body.get("purchasable_powers") or []) if str(p).strip()],
        "costs": {str(k).strip().lower(): v for k, v in dict(body.get("costs") or {}).items()},
    }
    bind_session(channel_id, session_id, config_payload)
    _logger.info("[twitch_ext] bound channel %s -> session %s", channel_id, session_id)
    return _json(request, {"ok": True, "channel_id": channel_id, "session_id": session_id})


# ── catalog (viewer) ─────────────────────────────────────────────────────────────

@router.get("/catalog")
async def catalog_route(request: Request, channel: str = ""):
    if not ext_config.ext_configured():
        return _not_configured(request)
    try:
        claims = verify_ext_jwt(_bearer(request))
    except ExtJWTError as exc:
        return _json(request, {"ok": False, "error": "unauthorized", "message": str(exc)}, status=401)

    channel_id = claims["channel_id"]  # trust the signed channel over the query param
    session_id = lookup_bound_session(channel_id)
    session = get_session(session_id) if session_id else None
    bound = bool(session_id and session is not None)
    return _json(
        request,
        {
            "ok": True,
            "bound": bound,
            "channel_id": channel_id,
            "powers": _catalog_for_session(session, channel_id),
            "sub_powers": _sub_power_catalog(session),
            "sub_cooldown_sec": ext_config.sub_cooldown_sec(),
        },
    )


def _sub_power_catalog(session) -> list[dict]:
    defs = _viewer_power_defs(session) if session is not None else dict(VIEWER_POWER_DEFS)
    out = []
    for power_id in ext_config.sub_power_ids():
        d = defs.get(power_id)
        if not d:
            continue
        out.append({
            "power_id": power_id,
            "name": str(d.get("name") or power_id),
            "description": str(d.get("description") or ""),
            "cooldown_sec": int(d.get("cooldown_sec", 0) or 0),
            "requires_approval": bool(d.get("approval_default", False)),
        })
    return out


# ── powers (broadcaster config page) ─────────────────────────────────────────────

@router.get("/powers")
async def powers_route(request: Request):
    """All SKU-mapped known powers, unfiltered by the broadcaster's current
    selection — the config page renders its toggle list from this so the
    Extension never hardcodes power names. Names/descriptions come straight
    from the server power defs (session-aware when the channel is bound)."""
    if not ext_config.ext_configured():
        return _not_configured(request)
    try:
        claims = verify_ext_jwt(_bearer(request))
    except ExtJWTError as exc:
        return _json(request, {"ok": False, "error": "unauthorized", "message": str(exc)}, status=401)
    if claims.get("role") != "broadcaster":
        return _json(request, {"ok": False, "error": "forbidden", "message": "Broadcaster role required."}, status=403)

    channel_id = claims["channel_id"]
    session_id = lookup_bound_session(channel_id)
    session = get_session(session_id) if session_id else None
    defs = _viewer_power_defs(session) if session is not None else dict(VIEWER_POWER_DEFS)
    powers = [
        {
            "power_id": power_id,
            "sku": sku,
            "name": str(defs[power_id].get("name") or power_id),
            "description": str(defs[power_id].get("description") or ""),
        }
        for sku, power_id in SKU_TO_POWER.items()
        if power_id in defs
    ]
    return _json(request, {"ok": True, "channel_id": channel_id, "powers": powers})


# ── transaction (viewer Bits purchase) ──────────────────────────────────────────

@router.post("/transaction")
async def transaction_route(request: Request):
    if not ext_config.ext_configured():
        return _not_configured(request)
    try:
        claims = verify_ext_jwt(_bearer(request))
    except ExtJWTError as exc:
        return _json(request, {"ok": False, "error": "unauthorized", "message": str(exc)}, status=401)

    try:
        body = await request.json()
    except Exception:
        body = {}
    body = body if isinstance(body, dict) else {}
    receipt_token = str(body.get("transactionReceipt") or body.get("receipt") or "").strip()
    try:
        receipt = verify_bits_receipt(receipt_token)
    except ExtJWTError as exc:
        return _json(request, {"ok": False, "error": "bad_receipt", "message": str(exc)}, status=400)

    sku = receipt["sku"]
    power_id = SKU_TO_POWER.get(sku)
    if not power_id:
        return _json(request, {"ok": False, "error": "unknown_sku", "message": f"Unknown product: {sku}"}, status=400)

    # Identity: prefer the signed receipt buyer id, fall back to the JWT user id.
    twitch_user_id = receipt.get("user_id") or claims.get("user_id")
    if not twitch_user_id:
        return _json(request, {"ok": False, "error": "no_identity", "message": "Share your Twitch identity to use powers."}, status=403)

    transaction_id = receipt["transaction_id"]
    # Replay/dedupe: reject if we've already processed this transaction id.
    if is_transaction_processed(transaction_id):
        return _json(request, {"ok": True, "duplicate": True, "message": "Already processed."})

    try:
        session, viewer_key, user = resolve_target(claims["channel_id"], twitch_user_id)
    except ResolveError as exc:
        return _json(request, {"ok": False, "error": "unresolved", "message": str(exc)}, status=409)

    ok, reason = check_rate_limit(session.id, viewer_key)
    if not ok:
        return _json(request, {"ok": False, "error": "rate_limited", "message": reason}, status=429)

    # Claim the transaction id atomically BEFORE granting so a concurrent retry
    # cannot double-grant. If another request won the race, treat as duplicate.
    if not mark_transaction_processed(transaction_id, sku=sku, viewer_key=viewer_key, session_id=session.id):
        return _json(request, {"ok": True, "duplicate": True, "message": "Already processed."})

    try:
        result = await grant_known_power(session, viewer_key, user, power_id)
    except ResolveError as exc:
        return _json(request, {"ok": False, "error": "grant_failed", "message": str(exc)}, status=409)

    return _json(request, {"ok": True, "transaction_id": transaction_id, "grant": result})


# ── sub-claim (subscriber free known power) ──────────────────────────────────────

@router.post("/sub-claim")
async def sub_claim_route(request: Request):
    if not ext_config.ext_configured():
        return _not_configured(request)
    if not ext_config.sub_path_configured():
        return _json(
            request,
            {
                "ok": False,
                "error": "sub_not_configured",
                "message": "Subscriber powers require server-side Twitch verification (TWITCH_EXT_CLIENT_SECRET).",
            },
            status=503,
        )
    try:
        claims = verify_ext_jwt(_bearer(request))
    except ExtJWTError as exc:
        return _json(request, {"ok": False, "error": "unauthorized", "message": str(exc)}, status=401)

    twitch_user_id = claims.get("user_id")
    if not twitch_user_id:
        return _json(request, {"ok": False, "error": "no_identity", "message": "Share your Twitch identity to claim sub powers."}, status=403)

    try:
        body = await request.json()
    except Exception:
        body = {}
    body = body if isinstance(body, dict) else {}
    power_id = str(body.get("power_id") or "").strip().lower()
    if power_id not in ext_config.sub_power_ids():
        return _json(request, {"ok": False, "error": "bad_power", "message": "That power is not claimable with a sub."}, status=400)

    channel_id = claims["channel_id"]
    cooldown = ext_config.sub_cooldown_sec()
    remaining = sub_claim_remaining(channel_id, twitch_user_id, cooldown)
    if remaining > 0:
        return _json(request, {"ok": False, "error": "cooldown", "message": f"You can claim again in {remaining}s.", "retry_after": remaining}, status=429)

    # Server-side sub verification — never trust the client claim alone.
    from server.twitch_ext.helix import is_subscribed
    try:
        subscribed = await is_subscribed(channel_id, twitch_user_id)
    except Exception as exc:
        _logger.warning("[twitch_ext] sub verification failed: %s", exc)
        return _json(request, {"ok": False, "error": "verify_failed", "message": "Could not verify your subscription right now. Try again shortly."}, status=502)
    if not subscribed:
        return _json(request, {"ok": False, "error": "not_subscribed", "message": "An active subscription is required to claim this power."}, status=403)

    try:
        session, viewer_key, user = resolve_target(channel_id, twitch_user_id)
    except ResolveError as exc:
        return _json(request, {"ok": False, "error": "unresolved", "message": str(exc)}, status=409)

    ok, reason = check_rate_limit(session.id, viewer_key)
    if not ok:
        return _json(request, {"ok": False, "error": "rate_limited", "message": reason}, status=429)

    try:
        result = await grant_known_power(session, viewer_key, user, power_id)
    except ResolveError as exc:
        return _json(request, {"ok": False, "error": "grant_failed", "message": str(exc)}, status=409)

    record_sub_claim(channel_id, twitch_user_id)
    return _json(request, {"ok": True, "grant": result})
