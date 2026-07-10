"""Live summon runtime handlers (Pass D summon families)."""
from __future__ import annotations

import copy
import logging
import time

from server.character.resolver import resolve_character_runtime
from server.character.summon_runtime import (
    build_summon_runtime_payload,
    build_active_deployment_entry,
    register_active_summon,
    remove_active_summon,
    reconcile_native_summons,
    plan_active_summon_mutations,
    prune_expired_temporary_summons,
    normalize_deployment_ui_entry,
    get_summon_runtime_metrics,
)
from server.character.summon_diagnostics import (
    build_entry_breadcrumb,
    build_failure,
    increment_metric,
    metrics_snapshot,
    record_failure_metric,
)
from server.handlers.common import Session, User, manager, save_campaign_async, _broadcast_token_state_sync, _broadcast_token_event, _sync_combatant_token_state
from server.character.summon_state import normalize_summon_state
from server.handlers.content import _send_char_profiles, _char_profile_bucket_key
from server.session import create_token, build_token_runtime_payload

logger = logging.getLogger(__name__)


def _structured_log(event: str, **fields):
    payload = {"event": event, "ts": time.time()}
    payload.update(fields)
    logger.info("[summon_runtime] %s", payload)


def _request_context(*, payload: dict, user: User, resolved: dict | None = None) -> dict:
    resolved_payload = resolved if isinstance(resolved, dict) else {}
    template = resolved_payload.get("template") if isinstance(resolved_payload.get("template"), dict) else {}
    return {
        "owner_user_id": str(getattr(user, "id", "") or ""),
        "owner_role": str(getattr(user, "role", "") or ""),
        "profile_id": str(resolved_payload.get("profile_id") or payload.get("profile_id") or payload.get("profileId") or ""),
        "summon_template_id": str(template.get("id") or payload.get("summon_template_id") or payload.get("summonTemplateId") or "").strip().lower(),
        "summon_group_id": str(resolved_payload.get("summon_group_id") or payload.get("summon_group_id") or payload.get("summonGroupId") or "").strip().lower(),
        "active_id": str(payload.get("active_id") or payload.get("activeId") or ""),
        "token_id": str(payload.get("token_id") or payload.get("tokenId") or ""),
        "map_context": str(resolved_payload.get("map_context") or payload.get("map_context") or payload.get("mapContext") or ""),
    }


def _attach_summon_breadcrumb(native_document: dict, *, active_id: str, breadcrumb: dict):
    summons = normalize_summon_state(native_document.get("summons"))
    rows = list(summons.get("activeSummons") or [])
    touched = False
    for idx, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        if str(row.get("id") or "") != str(active_id or ""):
            continue
        next_row = copy.deepcopy(row)
        trail = list(next_row.get("breadcrumbs") or []) if isinstance(next_row.get("breadcrumbs"), list) else []
        trail.append(copy.deepcopy(breadcrumb))
        next_row["breadcrumbs"] = trail[-10:]
        if breadcrumb.get("ok"):
            next_row["lastSuccessfulAction"] = copy.deepcopy(breadcrumb)
        else:
            next_row["lastFailedAction"] = copy.deepcopy(breadcrumb)
        next_row["lastAction"] = copy.deepcopy(breadcrumb)
        next_row["updatedAt"] = time.time()
        rows[idx] = next_row
        touched = True
    if touched:
        summons["activeSummons"] = rows
        native_document["summons"] = summons
    return touched


def _mark_quarantined(native_document: dict, *, active_id: str, reason: str, context: dict) -> bool:
    summons = normalize_summon_state(native_document.get("summons"))
    rows = list(summons.get("activeSummons") or [])
    changed = False
    for idx, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        if str(row.get("id") or "") != str(active_id or ""):
            continue
        next_row = copy.deepcopy(row)
        next_row["status"] = "quarantined"
        next_row["quarantineReason"] = str(reason or "unknown")
        next_row["quarantinedAt"] = time.time()
        next_row["quarantineContext"] = copy.deepcopy(context or {})
        next_row["updatedAt"] = time.time()
        rows[idx] = next_row
        changed = True
    if changed:
        summons["activeSummons"] = rows
        native_document["summons"] = summons
    return changed


def _mark_active(native_document: dict, *, active_id: str) -> bool:
    summons = normalize_summon_state(native_document.get("summons"))
    rows = list(summons.get("activeSummons") or [])
    changed = False
    for idx, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        if str(row.get("id") or "") != str(active_id or ""):
            continue
        next_row = copy.deepcopy(row)
        next_row["status"] = "active"
        next_row.pop("quarantineReason", None)
        next_row.pop("quarantinedAt", None)
        next_row.pop("quarantineContext", None)
        next_row["updatedAt"] = time.time()
        rows[idx] = next_row
        changed = True
    if changed:
        summons["activeSummons"] = rows
        native_document["summons"] = summons
    return changed


def _normalize_diagnostic(entry: dict) -> dict:
    actor = entry.get("actor") if isinstance(entry.get("actor"), dict) else {}
    suggestions = []
    if not bool(entry.get("tokenPresent")):
        suggestions.extend(["retry_rebind", "cleanup_stale"])
    if str(entry.get("status") or "") == "quarantined":
        suggestions.append("force_cleanup")
    if not suggestions:
        suggestions.append("inspect")
    return {
        "id": str(entry.get("id") or ""),
        "status": str(entry.get("status") or "active"),
        "entity_kind": str(entry.get("entityKind") or "creature"),
        "is_creature": bool(entry.get("isCreature", True)),
        "template_id": str(entry.get("templateId") or ""),
        "summon_group_id": str(entry.get("summonGroupId") or ""),
        "owner": {
            "user_id": str(entry.get("ownerUserId") or ""),
            "profile_id": str(entry.get("ownerProfileId") or ""),
            "name": str(entry.get("ownerName") or ""),
        },
        "token": {
            "id": str(entry.get("tokenId") or ""),
            "present": bool(entry.get("tokenPresent")),
            "name": str(entry.get("tokenName") or actor.get("name") or ""),
            "map_context": str(entry.get("tokenMapContext") or entry.get("mapContext") or ""),
        },
        "source": copy.deepcopy(entry.get("source") or {}),
        "last_action": copy.deepcopy(entry.get("lastAction") or {}),
        "last_failure": copy.deepcopy(entry.get("lastFailedAction") or {}),
        "recovery_suggestions": suggestions,
    }

def _upsert_profile_runtime(session: Session, *, owner_key: str, profile_index: int, native_doc: dict):
    profiles = dict(getattr(session, "char_profiles", {}) or {})
    bucket = list(profiles.get(owner_key) or []) if isinstance(profiles.get(owner_key), list) else []
    if not (0 <= profile_index < len(bucket)):
        return
    row = bucket[profile_index] if isinstance(bucket[profile_index], dict) else {}
    resolved_runtime = resolve_character_runtime(native_doc)
    row["nativeCharacter"] = resolved_runtime.get("document") if isinstance(resolved_runtime.get("document"), dict) else native_doc
    row["nativeRuntime"] = resolved_runtime.get("runtime") if isinstance(resolved_runtime.get("runtime"), dict) else row.get("nativeRuntime", {})
    bucket[profile_index] = row
    profiles[owner_key] = bucket
    session.char_profiles = profiles


def _collect_valid_map_contexts(session: Session) -> set[str]:
    valid_map_contexts = {"world"}
    valid_map_contexts.update(str(k or "").strip() for k in (getattr(session, "pois", {}) or {}).keys())
    valid_map_contexts.update(str(k or "").strip() for k in (getattr(session, "map_documents", {}) or {}).keys())
    return valid_map_contexts


def _refresh_profiles_for_native_docs(session: Session, touched_profiles: set[tuple[str, int]]) -> bool:
    if not touched_profiles:
        return False
    profiles = dict(getattr(session, "char_profiles", {}) or {})
    changed = False
    for owner_key, profile_index in touched_profiles:
        bucket = list(profiles.get(owner_key) or []) if isinstance(profiles.get(owner_key), list) else []
        if not (0 <= profile_index < len(bucket)):
            continue
        slot = bucket[profile_index] if isinstance(bucket[profile_index], dict) else {}
        native_doc = slot.get("nativeCharacter") if isinstance(slot.get("nativeCharacter"), dict) else {}
        resolved_runtime = resolve_character_runtime(native_doc)
        slot["nativeCharacter"] = resolved_runtime.get("document") if isinstance(resolved_runtime.get("document"), dict) else native_doc
        slot["nativeRuntime"] = resolved_runtime.get("runtime") if isinstance(resolved_runtime.get("runtime"), dict) else slot.get("nativeRuntime", {})
        bucket[profile_index] = slot
        profiles[owner_key] = bucket
        changed = True
    if changed:
        session.char_profiles = profiles
    return changed


async def handle_summon_runtime_request(payload: dict, session: Session, user: User):
    started = time.perf_counter()
    if user.role not in {"player", "dm"}:
        await manager.send_to(
            session.id,
            user.id,
            {"type": "summon_runtime_result", "payload": {"ok": False, "error": "role_not_allowed"}},
        )
        return

    _structured_log("deploy_request", session_id=session.id, **_request_context(payload=payload or {}, user=user))
    resolved = build_summon_runtime_payload(session=session, user=user, payload=payload or {})
    if not bool(resolved.get("ok")):
        failure = resolved.get("failure") if isinstance(resolved.get("failure"), dict) else build_failure(
            code=str(resolved.get("error") or "summon_failed"),
            message="Summon runtime request failed.",
            context=_request_context(payload=payload or {}, user=user, resolved=resolved),
        )
        increment_metric(session, "deploy_failure")
        record_failure_metric(session, failure=failure, family=str((failure.get("context") or {}).get("summon_group_id") or "unknown"))
        _structured_log("deploy_failure", session_id=session.id, failure=failure)
        await manager.send_to(
            session.id,
            user.id,
            {"type": "summon_runtime_result", "payload": {"ok": False, "error": str(resolved.get("error") or "summon_failed"), "failure": failure}},
        )
        return

    actor = copy.deepcopy(resolved.get("actor") or {})
    token_payload = resolved.get("token_payload") if isinstance(resolved.get("token_payload"), dict) else {}
    selected_variant = str(resolved.get("selected_variant") or "")
    profile_id = str(resolved.get("profile_id") or "")

    summon_group_id = str(resolved.get("summon_group_id") or "")
    removed_token_ids: list[str] = []
    native_document = resolved.get("native_document") if isinstance(resolved.get("native_document"), dict) else {}
    expired_rows = prune_expired_temporary_summons(native_document)
    for expired in expired_rows:
        expired_token_id = str(expired.get("tokenId") or "").strip()
        if expired_token_id and expired_token_id in (session.tokens or {}):
            session.tokens.pop(expired_token_id, None)
            removed_token_ids.append(expired_token_id)
            await manager.broadcast(session.id, {"type": "token_deleted", "payload": {"token_id": expired_token_id}})
    reconcile_native_summons(
        native_document,
        existing_token_ids={str(k) for k in (session.tokens or {}).keys()},
        valid_map_contexts=_collect_valid_map_contexts(session),
    )
    mutation_plan = plan_active_summon_mutations(
        native_document,
        active_entry={
            "summonGroupId": summon_group_id,
            "templateId": str((resolved.get("template") or {}).get("id") or ""),
            "ownerProfileId": profile_id,
            "source": copy.deepcopy(actor.get("source") or {}),
        },
        template=(resolved.get("template") if isinstance(resolved.get("template"), dict) else {}),
    )
    for row in (mutation_plan.get("remove_entries") or []):
        if not isinstance(row, dict):
            continue
        removed = remove_active_summon(
            native_document,
            active_id=str(row.get("id") or ""),
            token_id=str(row.get("tokenId") or ""),
            owner_profile_id=profile_id,
        )
        for gone in removed:
            token_id = str(gone.get("tokenId") or "").strip()
            if not token_id:
                continue
            tok = (session.tokens or {}).get(token_id)
            if tok and str(getattr(tok, "owner_id", "") or "") == str(user.id):
                removed_token_ids.append(token_id)
                session.tokens.pop(token_id, None)

    try:
        token = create_token(
            session=session,
            dm_id=user.id,
            name=str(token_payload.get("name") or "🐾 Primal Beast"),
            x=float(token_payload.get("x", 120.0) or 120.0),
            y=float(token_payload.get("y", 120.0) or 120.0),
            color=str(((actor.get("tokenVisual") or {}).get("color") or "#7ad67a")),
            shape=str(((actor.get("tokenVisual") or {}).get("shape") or "circle")),
            width=float(token_payload.get("width", 40.0) or 40.0),
            height=float(token_payload.get("height", 40.0) or 40.0),
            owner_id=user.id,
            hp=int(token_payload.get("hp", 1) or 1),
            max_hp=int(token_payload.get("max_hp", token_payload.get("hp", 1)) or 1),
            temp_hp=0,
            map_context=str(token_payload.get("map_context") or "world"),
            hidden_hp=False,
            hidden=False,
            initiative_mod=0,
            ac=int(token_payload.get("ac", 10) or 10),
            speed=int(token_payload.get("speed", 0) or 0),
            token_type="companion",
            notes=str(token_payload.get("notes") or "")[:2000],
            level=int(((actor.get("levelSource") or {}).get("classLevel") or 1)),
            faction=str(token_payload.get("faction") or "allies"),
            passive_perception=None,
            staged=False,
            image_url=token_payload.get("image_url"),
            creature_id=str(actor.get("id") or "")[:120],
            creature_type=("summon" if bool(actor.get("isCreature", True)) else "deployed_effect"),
            monster_type=str(token_payload.get("monster_type") or (actor.get("entityKind") or actor.get("summonCategory") or "") or "summon"),
            cr="",
        )
    except Exception as exc:
        failure = build_failure(
            code="token_spawn_failed",
            message="Token creation failed during summon deployment.",
            context={**_request_context(payload=payload or {}, user=user, resolved=resolved), "exception": str(exc)},
        )
        increment_metric(session, "deploy_failure")
        record_failure_metric(session, failure=failure, family=summon_group_id or "unknown")
        await manager.send_to(session.id, user.id, {"type": "summon_runtime_result", "payload": {"ok": False, "error": "token_spawn_failed", "failure": failure}})
        return

    if str(getattr(token, "owner_id", "") or "") != str(user.id):
        session.tokens.pop(str(getattr(token, "id", "") or ""), None)
        failure = build_failure(
            code="ownership_assignment_failed",
            message="Spawned summon token ownership did not match the request owner.",
            context={**_request_context(payload=payload or {}, user=user, resolved=resolved), "spawned_token_id": str(getattr(token, "id", "") or "")},
        )
        increment_metric(session, "deploy_failure")
        record_failure_metric(session, failure=failure, family=summon_group_id or "unknown")
        await manager.send_to(session.id, user.id, {"type": "summon_runtime_result", "payload": {"ok": False, "error": "ownership_assignment_failed", "failure": failure}})
        return

    active_entry = build_active_deployment_entry(
        actor=actor,
        template=(resolved.get("template") if isinstance(resolved.get("template"), dict) else {}),
        token_id=str(token.id),
        owner_user=user,
        profile_id=profile_id,
        selected_variant=selected_variant,
        map_context=str(token_payload.get("map_context") or "world"),
    )

    owner_key = str(resolved.get("owner_key") or "")
    profile_index = int(resolved.get("profile_index", -1) or -1)
    try:
        register_active_summon(native_document, active_entry)
        _attach_summon_breadcrumb(
            native_document,
            active_id=str(active_entry.get("id") or ""),
            breadcrumb=build_entry_breadcrumb(action="deploy", ok=True, detail="Summon deployed successfully."),
        )
        _upsert_profile_runtime(session, owner_key=owner_key, profile_index=profile_index, native_doc=native_document)
    except Exception as exc:
        session.tokens.pop(str(getattr(token, "id", "") or ""), None)
        failure = build_failure(
            code="register_active_failed",
            message="Failed to register summon state after token creation. Spawned token was rolled back.",
            context={**_request_context(payload=payload or {}, user=user, resolved=resolved), "spawned_token_id": str(getattr(token, "id", "") or ""), "exception": str(exc)},
        )
        increment_metric(session, "deploy_failure")
        record_failure_metric(session, failure=failure, family=summon_group_id or "unknown")
        await manager.send_to(session.id, user.id, {"type": "summon_runtime_result", "payload": {"ok": False, "error": "register_active_failed", "failure": failure}})
        return

    # Filtered (not unconditional) broadcast: a summon token is normally
    # player-owned and visible, but it can still be staged for another map,
    # so only deliver it to users who can currently see it.
    await _broadcast_token_event(manager, session, "token_created", {
        "token": build_token_runtime_payload(session, token),
        "log": session.add_log(f"{user.name} summoned {actor.get('name', 'Primal Beast')}", "system"),
    }, token)
    if removed_token_ids:
        for token_id in removed_token_ids:
            await manager.broadcast(session.id, {"type": "token_deleted", "payload": {"token_id": token_id}})

    await _broadcast_token_state_sync(session)
    await _send_char_profiles(session, user.id)
    await manager.send_to(
        session.id,
        user.id,
        {
            "type": "summon_runtime_result",
            "payload": {
                "ok": True,
                "profile_id": profile_id,
                "summon": active_entry,
                "token": token.to_dict(),
                "removed_token_ids": removed_token_ids,
                "diagnostics": {
                    "runtime_ms": round((time.perf_counter() - started) * 1000.0, 3),
                },
            },
        },
    )
    increment_metric(session, "deploy_success")
    _structured_log("deploy_success", session_id=session.id, active_id=str(active_entry.get("id") or ""), token_id=str(token.id), summon_group_id=summon_group_id)
    await save_campaign_async(session)


async def handle_summon_runtime_dismiss(payload: dict, session: Session, user: User):
    if user.role not in {"player", "dm"}:
        await manager.send_to(session.id, user.id, {"type": "summon_runtime_dismiss_result", "payload": {"ok": False, "error": "role_not_allowed"}})
        return
    requested_profile_id = str(payload.get("profile_id") or payload.get("profileId") or "").strip()
    owner_key = _char_profile_bucket_key(session, user)
    profiles = dict(getattr(session, "char_profiles", {}) or {})
    bucket = list(profiles.get(owner_key) or []) if isinstance(profiles.get(owner_key), list) else []
    if not requested_profile_id:
        requested_profile_id = str((getattr(session, "active_char_profiles", {}) or {}).get(user.id) or "").strip()
    profile_index = -1
    native_document: dict = {}
    for idx, row in enumerate(bucket):
        if not isinstance(row, dict):
            continue
        if requested_profile_id and str(row.get("id") or "").strip() != requested_profile_id:
            continue
        native_document = row.get("nativeCharacter") if isinstance(row.get("nativeCharacter"), dict) else {}
        profile_index = idx
        requested_profile_id = str(row.get("id") or requested_profile_id or "")
        break
    if profile_index < 0 or not native_document:
        increment_metric(session, "restore_failure")
        await manager.send_to(session.id, user.id, {"type": "summon_runtime_dismiss_result", "payload": {"ok": False, "error": "profile_not_found"}})
        return
    profile_id = requested_profile_id
    active_id = str(payload.get("active_id") or payload.get("activeId") or "").strip()
    token_id = str(payload.get("token_id") or payload.get("tokenId") or "").strip()
    summon_group_id = str(payload.get("summon_group_id") or payload.get("summonGroupId") or "").strip().lower()
    source_feature_id = str(payload.get("source_feature_id") or payload.get("sourceFeatureId") or "").strip().lower()

    removed_rows = remove_active_summon(
        native_document,
        active_id=active_id,
        token_id=token_id,
        summon_group_id=summon_group_id,
        source_feature_id=source_feature_id,
        owner_profile_id=profile_id,
    )
    if not removed_rows:
        await manager.send_to(session.id, user.id, {"type": "summon_runtime_dismiss_result", "payload": {"ok": True, "removed": [], "removed_token_ids": []}})
        return

    removed_token_ids: list[str] = []
    for row in removed_rows:
        _attach_summon_breadcrumb(
            native_document,
            active_id=str(row.get("id") or ""),
            breadcrumb=build_entry_breadcrumb(action="dismiss", ok=True, detail="Summon dismissed."),
        )
        tok_id = str(row.get("tokenId") or "").strip()
        tok = (session.tokens or {}).get(tok_id)
        if tok and str(getattr(tok, "owner_id", "") or "") == str(user.id):
            session.tokens.pop(tok_id, None)
            removed_token_ids.append(tok_id)
            await manager.broadcast(session.id, {"type": "token_deleted", "payload": {"token_id": tok_id}})

    _upsert_profile_runtime(
        session,
        owner_key=owner_key,
        profile_index=profile_index,
        native_doc=native_document,
    )
    await _broadcast_token_state_sync(session)
    await _send_char_profiles(session, user.id)
    await manager.send_to(
        session.id,
        user.id,
        {"type": "summon_runtime_dismiss_result", "payload": {"ok": True, "removed": removed_rows, "removed_token_ids": removed_token_ids}},
    )
    increment_metric(session, "cleanup_count")
    await save_campaign_async(session)


def _iter_active_summons(session: Session):
    profiles = dict(getattr(session, "char_profiles", {}) or {})
    users = dict(getattr(session, "users", {}) or {})
    for owner_key, bucket in profiles.items():
        rows = list(bucket) if isinstance(bucket, list) else []
        for idx, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            profile_id = str(row.get("id") or "").strip()
            native = row.get("nativeCharacter") if isinstance(row.get("nativeCharacter"), dict) else {}
            summons = normalize_summon_state(native.get("summons"))
            for active in (summons.get("activeSummons") or []):
                if not isinstance(active, dict):
                    continue
                token_id = str(active.get("tokenId") or "").strip()
                token = (session.tokens or {}).get(token_id)
                owner_user_id = str(active.get("ownerUserId") or "").strip()
                owner_name = ""
                if owner_user_id and owner_user_id in users:
                    owner_name = str(getattr(users[owner_user_id], "name", "") or "")
                if not owner_name:
                    owner_name = str(((active.get("actor") or {}).get("owner") or {}).get("userName") or "")
                if not owner_name:
                    owner_name = str(owner_key or "")
                enriched = copy.deepcopy(active)
                enriched["ownerBucketKey"] = str(owner_key)
                enriched["profileIndex"] = idx
                enriched["profileId"] = profile_id
                enriched["ownerName"] = owner_name
                enriched["tokenPresent"] = bool(token)
                enriched["tokenName"] = str(getattr(token, "name", "") or (enriched.get("actor") or {}).get("name") or "")
                enriched["tokenMapContext"] = str(getattr(token, "map_context", "") or enriched.get("mapContext") or enriched.get("sceneId") or "")
                enriched["status"] = str(enriched.get("status") or ("active" if token else "stale")).strip().lower()
                yield owner_key, idx, row, native, enriched


async def handle_summon_runtime_admin(payload: dict, session: Session, user: User):
    if user.role != "dm":
        await manager.send_to(session.id, user.id, {"type": "summon_runtime_admin_result", "payload": {"ok": False, "error": "role_not_allowed"}})
        return

    action = str(payload.get("action") or "list").strip().lower()
    active_id = str(payload.get("active_id") or payload.get("activeId") or "").strip()
    token_id = str(payload.get("token_id") or payload.get("tokenId") or "").strip()

    rows = list(_iter_active_summons(session))
    if action in {"list", "refresh", "cleanup_stale", "reconcile"}:
        touched_profiles: set[tuple[str, int]] = set()
        removed_token_ids: list[str] = []
        for owner_key, profile_index, _, native, _ in rows:
            expired_rows = prune_expired_temporary_summons(native)
            if not expired_rows:
                continue
            touched_profiles.add((owner_key, profile_index))
            for expired in expired_rows:
                expired_token_id = str(expired.get("tokenId") or "").strip()
                if expired_token_id and expired_token_id in (session.tokens or {}):
                    session.tokens.pop(expired_token_id, None)
                    removed_token_ids.append(expired_token_id)
                    await manager.broadcast(session.id, {"type": "token_deleted", "payload": {"token_id": expired_token_id}})
        if _refresh_profiles_for_native_docs(session, touched_profiles):
            await _broadcast_token_state_sync(session)
            await save_campaign_async(session)
        rows = list(_iter_active_summons(session))
    if action in {"list", "refresh"}:
        data = [_normalize_diagnostic(entry) for (_, _, _, _, entry) in rows]
        quarantined = sum(1 for d in data if d.get("status") == "quarantined")
        metrics = metrics_snapshot(session)
        metrics["quarantined_count"] = max(int(metrics.get("quarantined_count") or 0), quarantined)
        await manager.send_to(
            session.id,
            user.id,
            {
                "type": "summon_runtime_admin_result",
                "payload": {
                    "ok": True,
                    "action": "list",
                    "summons": data,
                    "metrics": metrics,
                    "diagnostics": {
                        "summon_count": len(data),
                        "runtime_metrics": get_summon_runtime_metrics(),
                    },
                },
            },
        )
        return

    if action == "inspect":
        selected = None
        for _, _, _, _, entry in rows:
            if active_id and str(entry.get("id") or "") == active_id:
                selected = entry
                break
            if token_id and str(entry.get("tokenId") or "") == token_id:
                selected = entry
                break
        await manager.send_to(
            session.id,
            user.id,
            {"type": "summon_runtime_admin_result", "payload": {"ok": True, "action": "inspect", "summon": (_normalize_diagnostic(selected) if isinstance(selected, dict) else None)}},
        )
        return

    if action in {"dismiss", "cleanup_stale", "force_cleanup"}:
        removed: list[dict] = []
        removed_token_ids: list[str] = []
        touched: set[tuple[str, int]] = set()
        for owner_key, profile_index, row, native, entry in rows:
            row_active_id = str(entry.get("id") or "")
            row_token_id = str(entry.get("tokenId") or "")
            if action == "cleanup_stale" and row_token_id and row_token_id in (session.tokens or {}):
                continue
            if action == "cleanup_stale":
                if _mark_quarantined(native, active_id=row_active_id, reason="missing_token", context={"action": "cleanup_stale", "token_id": row_token_id}):
                    _attach_summon_breadcrumb(
                        native,
                        active_id=row_active_id,
                        breadcrumb=build_entry_breadcrumb(action="cleanup_stale", ok=True, detail="Entry quarantined because token is missing."),
                    )
                    touched.add((owner_key, profile_index))
                continue
            if action in {"dismiss", "force_cleanup"} and active_id and row_active_id != active_id:
                continue
            if action in {"dismiss", "force_cleanup"} and token_id and row_token_id != token_id:
                continue
            if action in {"dismiss", "force_cleanup"} and not active_id and not token_id:
                continue
            gone = remove_active_summon(native, active_id=row_active_id, token_id=row_token_id, owner_profile_id=str(entry.get("ownerProfileId") or ""))
            if not gone:
                continue
            removed.extend(gone)
            touched.add((owner_key, profile_index))
            if row_token_id and row_token_id in (session.tokens or {}):
                session.tokens.pop(row_token_id, None)
                removed_token_ids.append(row_token_id)
                await manager.broadcast(session.id, {"type": "token_deleted", "payload": {"token_id": row_token_id}})
            increment_metric(session, "cleanup_count")

        if _refresh_profiles_for_native_docs(session, touched):
            await _broadcast_token_state_sync(session)
            for uid in (session.users or {}).keys():
                await _send_char_profiles(session, uid)
            await save_campaign_async(session)

        await manager.send_to(session.id, user.id, {
            "type": "summon_runtime_admin_result",
            "payload": {"ok": True, "action": action, "removed": removed, "removed_token_ids": removed_token_ids, "metrics": metrics_snapshot(session)},
        })
        return

    if action == "retry_rebind":
        touched: set[tuple[str, int]] = set()
        results: list[dict] = []
        for owner_key, profile_index, _, native, entry in rows:
            row_active_id = str(entry.get("id") or "")
            row_token_id = str(entry.get("tokenId") or "")
            if active_id and row_active_id != active_id:
                continue
            if token_id and row_token_id != token_id:
                continue
            if not active_id and not token_id:
                continue
            if row_token_id in (session.tokens or {}):
                if _mark_active(native, active_id=row_active_id):
                    touched.add((owner_key, profile_index))
                _attach_summon_breadcrumb(native, active_id=row_active_id, breadcrumb=build_entry_breadcrumb(action="retry_rebind", ok=True, detail="Token link verified and restored."))
                increment_metric(session, "restore_success")
                results.append({"active_id": row_active_id, "token_id": row_token_id, "status": "active"})
            else:
                failure = build_failure(
                    code="stale_active_conflict",
                    message="Active summon token is missing and could not be rebound.",
                    context={"active_id": row_active_id, "token_id": row_token_id},
                )
                _mark_quarantined(native, active_id=row_active_id, reason="missing_token", context={"action": "retry_rebind"})
                _attach_summon_breadcrumb(native, active_id=row_active_id, breadcrumb=build_entry_breadcrumb(action="retry_rebind", ok=False, detail="Token missing during rebind.", failure=failure))
                touched.add((owner_key, profile_index))
                increment_metric(session, "restore_failure")
                record_failure_metric(session, failure=failure, family=str(entry.get("summonGroupId") or "unknown"))
                results.append({"active_id": row_active_id, "token_id": row_token_id, "status": "quarantined"})
        if touched:
            profiles = dict(getattr(session, "char_profiles", {}) or {})
            for owner_key, profile_index in touched:
                bucket = list(profiles.get(owner_key) or []) if isinstance(profiles.get(owner_key), list) else []
                if not (0 <= profile_index < len(bucket)):
                    continue
                slot = bucket[profile_index] if isinstance(bucket[profile_index], dict) else {}
                native_doc = slot.get("nativeCharacter") if isinstance(slot.get("nativeCharacter"), dict) else {}
                resolved_runtime = resolve_character_runtime(native_doc)
                slot["nativeCharacter"] = resolved_runtime.get("document") if isinstance(resolved_runtime.get("document"), dict) else native_doc
                slot["nativeRuntime"] = resolved_runtime.get("runtime") if isinstance(resolved_runtime.get("runtime"), dict) else slot.get("nativeRuntime", {})
                bucket[profile_index] = slot
                profiles[owner_key] = bucket
            session.char_profiles = profiles
            await _broadcast_token_state_sync(session)
            await save_campaign_async(session)
        await manager.send_to(session.id, user.id, {"type": "summon_runtime_admin_result", "payload": {"ok": True, "action": "retry_rebind", "results": results, "metrics": metrics_snapshot(session)}})
        return

    await manager.send_to(
        session.id,
        user.id,
        {
            "type": "summon_runtime_admin_result",
            "payload": {
                "ok": False,
                "error": "unknown_action",
                "failure": build_failure(
                    code="unknown_action",
                    message="Unsupported summon admin action.",
                    context={"action": action, "active_id": active_id, "token_id": token_id},
                ),
            },
        },
    )


async def handle_summon_action_use(payload: dict, session: Session, user: User):
    """Apply a summon action (attack, ability) on behalf of a player-owned companion token."""
    token_id = str(payload.get("token_id") or "").strip()
    action_id = str(payload.get("action_id") or "").strip()
    target_id = str(payload.get("target_id") or "").strip()

    token = (session.tokens or {}).get(token_id)
    if not token:
        await manager.send_to(session.id, user.id, {
            "type": "summon_action_result",
            "payload": {"ok": False, "error": "token_not_found", "token_id": token_id},
        })
        return

    # Find active summon entry for this token
    active_entry = None
    for _, _, _, native, entry in _iter_active_summons(session):
        if str(entry.get("tokenId") or "") == token_id:
            active_entry = entry
            break

    if not active_entry:
        await manager.send_to(session.id, user.id, {
            "type": "summon_action_result",
            "payload": {"ok": False, "error": "active_summon_not_found", "token_id": token_id},
        })
        return

    actor = active_entry.get("actor") if isinstance(active_entry.get("actor"), dict) else {}
    actions = list(actor.get("actions") or [])
    action = next((a for a in actions if str(a.get("id") or "") == action_id), None)
    if not action:
        await manager.send_to(session.id, user.id, {
            "type": "summon_action_result",
            "payload": {"ok": False, "error": "action_not_found", "action_id": action_id},
        })
        return

    # Apply damage to target if applicable
    damage_info = action.get("damage") if isinstance(action.get("damage"), dict) else {}
    damage_applied = 0
    target_token = (session.tokens or {}).get(target_id) if target_id else None
    if target_token and damage_info.get("formula"):
        import random as _random
        import re as _re

        formula = str(damage_info.get("formula") or "").strip()
        pb_value = int(
            actor.get("proficiencyBonus")
            or actor.get("profBonus")
            or ((actor.get("stats") or {}).get("proficiencyBonus") if isinstance(actor.get("stats"), dict) else 0)
            or 0
        )
        normalized_formula = _re.sub(r"\bPB\b", str(pb_value), formula, flags=_re.IGNORECASE).replace(" ", "")
        total = 0
        sign = 1
        for token_part in _re.findall(r"[+-]?[^+-]+", normalized_formula):
            term = token_part.strip()
            if not term:
                continue
            if term[0] == "+":
                sign = 1
                term = term[1:]
            elif term[0] == "-":
                sign = -1
                term = term[1:]
            match = _re.fullmatch(r"(\d*)d(\d+)", term)
            if match:
                qty = int(match.group(1) or 1)
                die = int(match.group(2))
                total += sign * sum(_random.randint(1, die) for _ in range(max(0, qty)))
                continue
            try:
                total += sign * int(term or 0)
            except Exception:
                continue
        total = max(0, total)
        damage_applied = total
        current_hp = int(getattr(target_token, "hp", 0) or 0)
        target_token.hp = max(0, current_hp - damage_applied)
        _sync_combatant_token_state(session, target_token, previous_hp=current_hp)

    result_payload = {
        "ok": True,
        "token_id": token_id,
        "action_id": action_id,
        "target_id": target_id,
        "action_name": str(action.get("displayName") or action.get("name") or action_id),
        "damage_applied": damage_applied,
        "target_hp": int(getattr(target_token, "hp", 0) or 0) if target_token else None,
    }

    result_message = {
        "type": "summon_action_result",
        "payload": result_payload,
    }
    await manager.send_to(session.id, user.id, result_message)
    await manager.broadcast(session.id, result_message)
    await _send_char_profiles(session, user.id)
    await save_campaign_async(session)
