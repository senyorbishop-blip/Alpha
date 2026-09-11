"""HTTP routes for persistent D&D Beyond character links.

These routes intentionally layer on top of the existing DDB preview/normalizer
flow. The external service is optional: saved Tavern character data remains
authoritative and playable when D&D Beyond cannot be reached.
"""
from __future__ import annotations

import copy
import json
import os
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from server.character.ddb_linking import (
    DDB_PROVIDER,
    attach_external_source_metadata,
    build_change_summary,
    build_external_source_metadata,
    get_external_source,
    mark_external_source_failure,
    merge_profile_state_for_upsert,
    merge_refresh_document,
    remove_external_source_metadata,
    source_fingerprint,
)
from server.character.import_normalizer import normalize_ddb_json_payload
from server.character.library_matcher import attach_library_gap_report
from server.character.routes import (
    _attach_import_review,
    _document_import_required_choices,
    _normalize_profile_entry,
    _resolve_owner_key,
    resolve_owned_profile_or_403,
)
from server.character.service import build_profile_upsert_payload, normalize_incoming_document
from server.character.validation import CharacterValidationError
from server.handlers.common import save_campaign_async
from server.handlers.content import _apply_profile_max_hp_to_linked_tokens, upsert_char_profile_for_owner
from server.http.auth import get_request_user
from server.http.session_access import get_or_restore_session
from server.integrations.service import extract_ddb_character_id, fetch_ddb_character_response

router = APIRouter()
_DEFAULT_MAX_REMOTE_BYTES = 2 * 1024 * 1024


def _max_remote_bytes() -> int:
    try:
        return max(64 * 1024, int(str(os.environ.get("DDB_CHARACTER_MAX_BYTES", _DEFAULT_MAX_REMOTE_BYTES)).strip()))
    except Exception:
        return _DEFAULT_MAX_REMOTE_BYTES


def _friendly_ddb_failure(message: Any = "") -> str:
    raw = str(message or "").strip()
    lower = raw.lower()
    if any(token in lower for token in ("401", "403", "404", "409", "private", "could not return")):
        return "D&D Beyond could not return this character. Make the character public and try again."
    if "timeout" in lower or "timed out" in lower:
        return "D&D Beyond did not respond in time. Your saved Tavern character is still available."
    if "malformed" in lower or "schema" in lower or "payload" in lower:
        return "D&D Beyond returned character data Tavern could not safely read. Your saved Tavern character is still available."
    return "Could not refresh D&D Beyond. Your saved Tavern character is still available."


def _character_payload(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    return data if isinstance(data, dict) else payload


def validate_remote_ddb_payload(payload: Any, *, expected_id: str = "") -> dict[str, Any]:
    """Validate enough remote identity/shape to fail closed on API schema drift."""
    if not isinstance(payload, dict) or not payload:
        raise ValueError("Malformed D&D Beyond response payload.")
    character = _character_payload(payload)
    if not isinstance(character, dict) or not character:
        raise ValueError("Malformed D&D Beyond character payload.")
    remote_id = str(character.get("id") or "").strip()
    if expected_id and remote_id and remote_id != str(expected_id):
        raise ValueError("D&D Beyond returned a mismatched character id.")
    name = str(character.get("name") or "").strip()
    classes = character.get("classes")
    if not name or not isinstance(classes, list) or not classes:
        raise ValueError("D&D Beyond response schema is missing required character identity or class fields.")
    has_class = False
    for row in classes[:30]:
        if not isinstance(row, dict):
            continue
        definition = row.get("definition") if isinstance(row.get("definition"), dict) else {}
        if str(definition.get("name") or row.get("name") or "").strip():
            has_class = True
            break
    if not has_class:
        raise ValueError("D&D Beyond response schema is missing class definitions.")
    return character


def _decode_response_body(response) -> dict[str, Any]:
    body = bytes(getattr(response, "body", b"") or b"")
    if len(body) > _max_remote_bytes():
        raise ValueError("D&D Beyond payload exceeded the safe size limit.")
    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception as exc:
        raise ValueError("Malformed D&D Beyond response payload.") from exc
    if not isinstance(payload, dict):
        raise ValueError("Malformed D&D Beyond response payload.")
    return payload


async def _fetch_normalized_source(character_input: str, *, import_resolution: Any = None) -> tuple[str, dict[str, Any], dict[str, Any]]:
    character_id = extract_ddb_character_id(character_input)
    if not character_id or not character_id.isdigit():
        raise HTTPException(status_code=400, detail="Invalid D&D Beyond character ID or URL.")
    response = await fetch_ddb_character_response(character_id)
    try:
        response_payload = _decode_response_body(response)
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=_friendly_ddb_failure(str(exc))) from exc
    if int(getattr(response, "status_code", 500) or 500) != 200:
        raise HTTPException(status_code=502, detail=_friendly_ddb_failure(response_payload.get("error")))
    try:
        validate_remote_ddb_payload(response_payload, expected_id=character_id)
        if isinstance(import_resolution, dict) and import_resolution:
            merged_payload = dict(response_payload)
            existing = merged_payload.get("import_resolution") if isinstance(merged_payload.get("import_resolution"), dict) else {}
            merged_payload["import_resolution"] = {**existing, **import_resolution}
        else:
            merged_payload = response_payload
        normalized = normalize_ddb_json_payload(merged_payload, external_id=character_id)
    except (CharacterValidationError, ValueError, TypeError, KeyError) as exc:
        raise HTTPException(status_code=502, detail=_friendly_ddb_failure(str(exc))) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail="D&D Beyond data could not be normalised safely. Your saved Tavern character is still available.",
        ) from exc
    document = normalized.get("document")
    if not isinstance(document, dict) or not document:
        raise HTTPException(status_code=502, detail="D&D Beyond returned no usable character data.")
    return character_id, document, normalized


def _extract_preview_character_id(document: dict[str, Any], explicit: str = "") -> str:
    if explicit:
        parsed = extract_ddb_character_id(explicit)
        if parsed and parsed.isdigit():
            return parsed
    import_meta = document.get("importMeta") if isinstance(document.get("importMeta"), dict) else {}
    candidate = str(import_meta.get("externalId") or "").strip()
    parsed = extract_ddb_character_id(candidate)
    return parsed if parsed and parsed.isdigit() else ""


def _validate_preview_document(document: dict[str, Any], character_id: str) -> None:
    source_mode = str(document.get("sourceMode") or "").strip().lower()
    import_meta = document.get("importMeta") if isinstance(document.get("importMeta"), dict) else {}
    origin = str(import_meta.get("origin") or import_meta.get("source") or "").strip().lower()
    if source_mode != DDB_PROVIDER and DDB_PROVIDER not in origin:
        raise HTTPException(status_code=400, detail="Preview document is not a D&D Beyond import.")
    identity = document.get("identity") if isinstance(document.get("identity"), dict) else {}
    classes = document.get("classes")
    if not str(identity.get("name") or identity.get("displayName") or "").strip():
        raise HTTPException(status_code=400, detail="D&D Beyond preview is missing the character name.")
    if not isinstance(classes, list) or not classes:
        raise HTTPException(status_code=400, detail="D&D Beyond preview is missing class data.")
    embedded = str(import_meta.get("externalId") or identity.get("characterId") or "").strip()
    embedded_id = extract_ddb_character_id(embedded)
    if character_id and embedded_id and embedded_id.isdigit() and embedded_id != character_id:
        raise HTTPException(status_code=400, detail="D&D Beyond preview character ID does not match the requested link.")


def _find_profile_owner_key(session, profile: dict[str, Any], profile_id: str) -> str:
    all_profiles = dict(getattr(session, "char_profiles", {}) or {})
    for owner_key, rows in all_profiles.items():
        if isinstance(rows, list) and any(row is profile for row in rows):
            return str(owner_key)
    target = str(profile_id or "").strip()
    for owner_key, rows in all_profiles.items():
        if isinstance(rows, list) and any(isinstance(row, dict) and str(row.get("id") or "").strip() == target for row in rows):
            return str(owner_key)
    return ""


def _summary(saved_profile: dict[str, Any]) -> dict[str, Any]:
    summary = _normalize_profile_entry(saved_profile, fallback_id="linked")
    external = get_external_source(saved_profile)
    if external:
        summary["externalSource"] = external
    return summary


def _profile_runtime_hp(profile: dict[str, Any]) -> tuple[Any, Any, Any]:
    runtime = profile.get("nativeRuntime") if isinstance(profile.get("nativeRuntime"), dict) else {}
    hp = runtime.get("hp") if isinstance(runtime.get("hp"), dict) else {}
    return hp.get("max"), hp.get("current"), hp.get("temp")


def _append_runtime_change_summary(changes: list[dict[str, Any]], before_profile: dict[str, Any], after_profile: dict[str, Any]) -> None:
    before_max, _, _ = _profile_runtime_hp(before_profile)
    after_max, _, _ = _profile_runtime_hp(after_profile)
    if before_max != after_max and not any(row.get("field") == "maxHp" for row in changes):
        changes.append({"field": "maxHp", "before": before_max, "after": after_max})
    before_runtime = before_profile.get("nativeRuntime") if isinstance(before_profile.get("nativeRuntime"), dict) else {}
    after_runtime = after_profile.get("nativeRuntime") if isinstance(after_profile.get("nativeRuntime"), dict) else {}
    before_ac = before_runtime.get("ac")
    after_ac = after_runtime.get("ac")
    if before_ac != after_ac and not any(row.get("field") == "ac" for row in changes):
        changes.append({"field": "ac", "before": before_ac, "after": after_ac})


async def _save_document(*, session, owner_key: str, document: dict[str, Any], profile_id: str = "",
                         existing_profile: dict[str, Any] | None = None, source: str = "dndbeyond") -> dict[str, Any]:
    attach_library_gap_report(document)
    try:
        document = normalize_incoming_document(document)
    except CharacterValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _attach_import_review(document, source=source)
    persisted_runtime = existing_profile.get("nativeRuntime") if isinstance(existing_profile, dict) else None
    upsert = build_profile_upsert_payload(document, profile_id=profile_id, persisted_runtime=persisted_runtime)
    upsert = merge_profile_state_for_upsert(existing_profile, upsert)
    saved = upsert_char_profile_for_owner(session, owner_key, upsert)
    if not isinstance(saved, dict):
        raise HTTPException(status_code=500, detail="Character profile could not be saved.")
    _apply_profile_max_hp_to_linked_tokens(session, saved)
    await save_campaign_async(session)
    return saved


def _parse_link_payload(payload: dict[str, Any]) -> tuple[str, str, str, dict[str, Any] | None]:
    session_id = str(payload.get("session_id") or payload.get("sessionId") or "").strip().upper()
    profile_id = str(payload.get("profile_id") or payload.get("profileId") or "").strip()
    character_input = str(payload.get("character") or payload.get("character_id") or payload.get("characterId") or "").strip()
    preview = None
    for key in ("preview_document", "canonical_document", "character_document", "document"):
        candidate = payload.get(key)
        if isinstance(candidate, dict):
            preview = candidate
            break
    return session_id, profile_id, character_input, preview


async def _link_from_payload(request: Request, payload: dict[str, Any]) -> JSONResponse:
    auth_user = get_request_user(request)
    if not auth_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    session_id, profile_id, character_input, preview = _parse_link_payload(payload)
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required")
    session = get_or_restore_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    existing_profile = None
    owner_key = _resolve_owner_key(auth_user)
    if profile_id:
        existing_profile = resolve_owned_profile_or_403(session, auth_user, profile_id, allow_dm=True)
        owner_key = _find_profile_owner_key(session, existing_profile, profile_id)
    if not owner_key:
        raise HTTPException(status_code=400, detail="Unable to resolve profile owner")

    normalized: dict[str, Any] = {"warnings": [], "requires_resolution": False, "required_choices": []}
    if preview is not None:
        try:
            source_document = normalize_incoming_document(preview)
        except CharacterValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        character_id = _extract_preview_character_id(source_document, character_input)
        if not character_id:
            raise HTTPException(status_code=400, detail="D&D Beyond character ID is missing from the preview.")
        _validate_preview_document(source_document, character_id)
    else:
        if not character_input:
            raise HTTPException(status_code=400, detail="character or preview_document is required")
        character_id, source_document, normalized = await _fetch_normalized_source(
            character_input, import_resolution=payload.get("import_resolution")
        )
        if normalized.get("requires_resolution"):
            return JSONResponse({
                "ok": False, "source": DDB_PROVIDER, "warnings": normalized.get("warnings") or [],
                "requires_resolution": True, "required_choices": normalized.get("required_choices") or [],
            }, status_code=400)

    required_choices = _document_import_required_choices(source_document)
    if required_choices:
        return JSONResponse({
            "ok": False, "source": DDB_PROVIDER,
            "warnings": ((source_document.get("importMeta") or {}).get("warnings") or []),
            "requires_resolution": True, "required_choices": required_choices,
        }, status_code=400)

    old_external = get_external_source(existing_profile or {})
    previous_for_same_character = old_external if str(old_external.get("characterId") or "") == character_id else {}
    if existing_profile:
        existing_doc = existing_profile.get("nativeCharacter")
        if not isinstance(existing_doc, dict):
            raise HTTPException(status_code=400, detail="Existing Tavern profile has no canonical character document.")
        merged_document = merge_refresh_document(
            existing_doc, source_document,
            previous_for_same_character.get("sourceSnapshot") if previous_for_same_character else None,
        )
    else:
        merged_document = copy.deepcopy(source_document)

    metadata = build_external_source_metadata(
        character_id=character_id, source_document=source_document, previous=previous_for_same_character,
        auto_refresh=payload.get("auto_refresh") if isinstance(payload.get("auto_refresh"), bool) else None,
    )
    linked_document = attach_external_source_metadata(merged_document, metadata)
    saved = await _save_document(
        session=session, owner_key=owner_key, document=linked_document,
        profile_id=profile_id, existing_profile=existing_profile,
    )
    return JSONResponse({
        "ok": True, "session_id": session_id, "profile_id": str(saved.get("id") or ""),
        "profile": _summary(saved), "externalSource": get_external_source(saved),
        "warnings": normalized.get("warnings") or [], "source": DDB_PROVIDER, "source_type": DDB_PROVIDER,
        "import_review": ((saved.get("importMeta") or {}).get("importReview") or {}),
    })


@router.post("/api/character/link/dndbeyond")
async def api_character_link_dndbeyond(request: Request):
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return await _link_from_payload(request, payload)


@router.post("/api/character/import/ddb-id/commit")
async def api_character_import_ddb_id_commit_linked(request: Request):
    """Compatibility path used by the existing CharacterImportModal."""
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return await _link_from_payload(request, payload)


@router.post("/api/character/{profile_id}/refresh/dndbeyond")
async def api_character_refresh_dndbeyond(profile_id: str, request: Request):
    auth_user = get_request_user(request)
    if not auth_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    session_id = str(payload.get("session_id") or payload.get("sessionId") or "").strip().upper()
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required")
    session = get_or_restore_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    existing = resolve_owned_profile_or_403(session, auth_user, profile_id, allow_dm=True)
    external = get_external_source(existing)
    if (str(external.get("provider") or "").lower() != DDB_PROVIDER
            or external.get("linked") is False or not str(external.get("characterId") or "").strip()):
        raise HTTPException(status_code=400, detail="Character is not linked to D&D Beyond.")
    character_id = str(external.get("characterId") or "").strip()
    old_profile_snapshot = copy.deepcopy(existing)
    old_document = existing.get("nativeCharacter") if isinstance(existing.get("nativeCharacter"), dict) else {}
    if not old_document:
        raise HTTPException(status_code=400, detail="Linked Tavern character has no canonical character document.")

    try:
        _, fresh_document, normalized = await _fetch_normalized_source(character_id)
        if normalized.get("requires_resolution"):
            raise HTTPException(
                status_code=502,
                detail="D&D Beyond changed data that now requires import review. Your saved Tavern character was not changed.",
            )
    except HTTPException as exc:
        mark_external_source_failure(existing, str(exc.detail))
        try:
            await save_campaign_async(session)
        except Exception:
            pass
        return JSONResponse({
            "ok": False, "changed": False, "profile_id": profile_id,
            "error": _friendly_ddb_failure(exc.detail), "externalSource": get_external_source(existing),
        }, status_code=exc.status_code if exc.status_code >= 500 else 502)
    except Exception:
        mark_external_source_failure(existing, "D&D Beyond refresh failed.")
        try:
            await save_campaign_async(session)
        except Exception:
            pass
        return JSONResponse({
            "ok": False, "changed": False, "profile_id": profile_id,
            "error": "Could not refresh D&D Beyond. Your saved Tavern character is still available.",
            "externalSource": get_external_source(existing),
        }, status_code=502)

    fresh_hash = source_fingerprint(fresh_document)
    if str(external.get("sourceRevisionHash") or "") == fresh_hash:
        metadata = build_external_source_metadata(
            character_id=character_id, source_document=fresh_document, previous=external
        )
        native_meta = existing.get("nativeCharacter", {}).get("importMeta")
        if not isinstance(native_meta, dict):
            native_meta = {}
            existing["nativeCharacter"]["importMeta"] = native_meta
        native_meta["externalSource"] = metadata
        existing["importMeta"] = native_meta
        await save_campaign_async(session)
        return JSONResponse({
            "ok": True, "changed": False, "changes": [], "profile_id": profile_id,
            "message": "Character is already up to date.", "externalSource": metadata,
        })

    previous_snapshot = external.get("sourceSnapshot") if isinstance(external.get("sourceSnapshot"), dict) else {}
    merged = merge_refresh_document(old_document, fresh_document, previous_snapshot)
    metadata = build_external_source_metadata(
        character_id=character_id, source_document=fresh_document, previous=external
    )
    merged = attach_external_source_metadata(merged, metadata)
    owner_key = _find_profile_owner_key(session, existing, profile_id)
    if not owner_key:
        raise HTTPException(status_code=500, detail="Unable to resolve profile owner.")
    changes = build_change_summary(old_document, merged)

    try:
        saved = await _save_document(
            session=session, owner_key=owner_key, document=merged,
            profile_id=profile_id, existing_profile=existing,
        )
    except Exception:
        existing.clear()
        existing.update(old_profile_snapshot)
        try:
            await save_campaign_async(session)
        except Exception:
            pass
        return JSONResponse({
            "ok": False, "changed": False, "profile_id": profile_id,
            "error": "Could not refresh D&D Beyond. Your saved Tavern character is still available.",
            "externalSource": get_external_source(existing),
        }, status_code=500)

    _append_runtime_change_summary(changes, old_profile_snapshot, saved)
    return JSONResponse({
        "ok": True, "changed": True, "changes": changes, "profile_id": profile_id,
        "profile": _summary(saved), "message": "Updated from D&D Beyond.",
        "externalSource": get_external_source(saved),
    })


@router.post("/api/character/{profile_id}/unlink/dndbeyond")
async def api_character_unlink_dndbeyond(profile_id: str, request: Request):
    auth_user = get_request_user(request)
    if not auth_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    session_id = str(payload.get("session_id") or payload.get("sessionId") or "").strip().upper()
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required")
    session = get_or_restore_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    profile = resolve_owned_profile_or_403(session, auth_user, profile_id, allow_dm=True)
    external = get_external_source(profile)
    if not external:
        return JSONResponse({"ok": True, "profile_id": profile_id, "linked": False})
    native = profile.get("nativeCharacter")
    if not isinstance(native, dict):
        raise HTTPException(status_code=400, detail="Character has no canonical Tavern document.")
    unlinked = remove_external_source_metadata(native)
    profile["nativeCharacter"] = unlinked
    profile["importMeta"] = unlinked.get("importMeta") if isinstance(unlinked.get("importMeta"), dict) else {}
    await save_campaign_async(session)
    return JSONResponse({
        "ok": True, "profile_id": profile_id, "linked": False,
        "message": "D&D Beyond unlinked. Your Tavern character and campaign state were kept.",
    })
