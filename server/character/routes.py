from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

from server.character.import_normalizer import normalize_ddb_json_payload, normalize_pdf_payload
from server.character.import_review import build_import_review, normalize_import_source
from server.character.library_matcher import attach_library_gap_report, summarize_library_gaps_from_profiles
from server.character.service import (
    apply_character_levelup,
    build_profile_upsert_payload,
    get_builder_rules_catalog,
    normalize_incoming_document,
    preview_levelup,
)
from server.character.validation import CharacterValidationError
from server.character.spell_compendium import (
    build_character_spell_manifest,
    build_multiclass_spell_context,
    build_spell_card,
    build_spell_limits_for_class,
    get_effective_document_spell_state,
    get_spell_by_id as get_compendium_spell_by_id,
    list_spells as list_compendium_spells,
    repair_spell_state_for_document,
    validate_spell_selection,
)
from server.character.progression import LevelupApplyError
from server.character.resolver import resolve_character_runtime
from server.character.rules_catalog import get_class_catalog_row, get_species_catalog_row, get_subclass_catalog_row
from server.character.feature_catalog import build_runtime_feature_payload
from server.handlers.common import save_campaign_async
from server.handlers.content import upsert_char_profile_for_owner, _apply_profile_max_hp_to_linked_tokens
from server.http.auth import auth_display_name, auth_player_key, get_request_user
from server.http.session_access import get_or_restore_session, resolve_session_authority
from server.integrations.service import fetch_ddb_character_response, parse_character_pdf_response
from server.session import normalize_profile_owner_key, resolve_owner_identity

router = APIRouter()
logger = logging.getLogger(__name__)

_KNOWN_RULES_MODES = {"casual", "classic", "custom"}


def _safe_int(value, default: int = 0, minimum: int = 0, maximum: int = 30) -> int:
    try:
        parsed = int(value)
    except Exception:
        return default
    return max(minimum, min(maximum, parsed))


def _resolve_profile_level(payload: dict) -> int | None:
    if not isinstance(payload, dict):
        return None

    direct = payload.get("level")
    if direct is not None:
        return _safe_int(direct, default=1)

    char_book = payload.get("charBook") if isinstance(payload.get("charBook"), dict) else {}
    if char_book.get("level") is not None:
        return _safe_int(char_book.get("level"), default=1)

    char_sheet = payload.get("charSheet") if isinstance(payload.get("charSheet"), dict) else {}
    for key in ("totalLevel", "level"):
        if char_sheet.get(key) is not None:
            return _safe_int(char_sheet.get(key), default=1)

    classes = char_sheet.get("classes") if isinstance(char_sheet.get("classes"), list) else []
    if classes:
        total = 0
        seen = False
        for cls in classes:
            if not isinstance(cls, dict):
                continue
            lvl = cls.get("level")
            if lvl is None:
                continue
            seen = True
            total += _safe_int(lvl, default=0)
        if seen:
            return _safe_int(total, default=1)

    return None


def _resolve_profile_class_summary(payload: dict) -> str:
    if not isinstance(payload, dict):
        return ""

    class_summary = str(payload.get("classSummary") or "").strip()
    if class_summary:
        return class_summary[:120]

    char_book = payload.get("charBook") if isinstance(payload.get("charBook"), dict) else {}
    class_name = str(char_book.get("className") or "").strip()
    subclass = str(char_book.get("subclass") or "").strip()
    if class_name and subclass:
        return f"{class_name} ({subclass})"[:120]
    if class_name:
        return class_name[:120]

    char_sheet = payload.get("charSheet") if isinstance(payload.get("charSheet"), dict) else {}
    classes = char_sheet.get("classes") if isinstance(char_sheet.get("classes"), list) else []
    labels = []
    for cls in classes:
        if not isinstance(cls, dict):
            continue
        cls_name = str(cls.get("name") or "").strip()
        if cls_name:
            labels.append(cls_name)
    if labels:
        return " / ".join(labels)[:120]

    return ""


def _resolve_source_mode(payload: dict) -> str:
    if not isinstance(payload, dict):
        return "legacy"

    direct = str(payload.get("sourceMode") or "").strip().lower()
    if direct:
        return direct[:40]

    native = payload.get("nativeCharacter") if isinstance(payload.get("nativeCharacter"), dict) else {}
    native_mode = str(native.get("sourceMode") or "").strip().lower()
    if native_mode:
        return native_mode[:40]

    import_meta = payload.get("importMeta") if isinstance(payload.get("importMeta"), dict) else {}
    import_source = str(import_meta.get("source") or "").strip().lower()
    if import_source:
        return normalize_import_source(import_source)

    source_type = str(import_meta.get("sourceType") or "").strip().lower()
    if source_type:
        return normalize_import_source(source_type)

    return "legacy"


def _sync_native_spellbook_entries(native: dict, *, known_ids: list[str], prepared_ids: list[str]) -> None:
    """Keep imported/native spellbook entries aligned with live spell picks.

    The app still falls back to spellbookEntries when explicit known/prepared lists are
    empty, so removing or replacing spells must update that source of truth too.
    """
    if not isinstance(native, dict):
        return
    spell_state = native.get("spellState") if isinstance(native.get("spellState"), dict) else {}
    if not spell_state:
        native["spellState"] = spell_state = {}
    existing_entries = spell_state.get("spellbookEntries") if isinstance(spell_state.get("spellbookEntries"), list) else []

    desired_ids: list[str] = []
    seen_ids: set[str] = set()
    for raw in list(known_ids or []) + list(prepared_ids or []):
        spell_id = str(raw or "").strip()
        if not spell_id or spell_id in seen_ids:
            continue
        seen_ids.add(spell_id)
        desired_ids.append(spell_id)

    existing_by_id: dict[str, dict] = {}
    for entry in existing_entries:
        if isinstance(entry, dict):
            raw_id = str(entry.get("id") or entry.get("name") or entry.get("displayName") or "").strip()
        else:
            raw_id = str(entry or "").strip()
        if not raw_id:
            continue
        spell = get_compendium_spell_by_id(raw_id) or get_compendium_spell_by_id(raw_id.lower().replace(" ", "-"))
        spell_id = str((spell or {}).get("id") or raw_id).strip()
        if not spell_id:
            continue
        if isinstance(entry, dict):
            existing_by_id[spell_id] = dict(entry)
        else:
            existing_by_id[spell_id] = {"name": raw_id}

    synced_entries: list[dict] = []
    for spell_id in desired_ids:
        existing = existing_by_id.get(spell_id)
        if existing:
            row = dict(existing)
            if not row.get("id"):
                row["id"] = spell_id
            if not row.get("name"):
                comp = get_compendium_spell_by_id(spell_id) or {}
                row["name"] = str(comp.get("name") or spell_id).strip()
            synced_entries.append(row)
            continue
        comp = get_compendium_spell_by_id(spell_id) or {}
        synced_entries.append({
            "id": spell_id,
            "name": str(comp.get("name") or spell_id).strip(),
        })

    spell_state["spellbookEntries"] = synced_entries
    native["spellbookEntries"] = list(synced_entries)


def _normalize_profile_entry(payload: dict, *, fallback_id: str) -> dict:
    char_book = payload.get("charBook") if isinstance(payload.get("charBook"), dict) else {}
    identity = payload.get("nativeCharacter") if isinstance(payload.get("nativeCharacter"), dict) else {}
    identity = identity.get("identity") if isinstance(identity.get("identity"), dict) else {}

    profile_id = str(
        payload.get("id")
        or identity.get("characterId")
        or char_book.get("id")
        or fallback_id
    ).strip()
    if not profile_id:
        profile_id = fallback_id

    name = str(payload.get("name") or identity.get("displayName") or identity.get("name") or char_book.get("name") or "").strip()
    class_summary = _resolve_profile_class_summary(payload)
    level = _resolve_profile_level(payload)
    source_mode = _resolve_source_mode(payload)

    return {
        "id": profile_id[:128],
        "name": (name or "Unnamed Character")[:80],
        "classSummary": class_summary,
        "level": level,
        "sourceMode": source_mode,
    }


def _resolve_owner_key(auth_user: dict) -> str:
    owner_key = normalize_profile_owner_key(auth_display_name(auth_user, fallback=""))
    if not owner_key:
        owner_key = normalize_profile_owner_key(auth_user.get("username") or "")
    if not owner_key:
        owner_key = str(auth_user.get("id") or "").strip()
    return owner_key


def _profile_owner_candidates(auth_user: dict) -> list[str]:
    """Return profile owner bucket keys that can belong to an auth principal."""
    candidates: list[str] = []

    def add(value: str) -> None:
        value = str(value or "").strip()
        if value and value not in candidates:
            candidates.append(value)

    add(_resolve_owner_key(auth_user))
    add(normalize_profile_owner_key(auth_display_name(auth_user, fallback="")))
    add(normalize_profile_owner_key(auth_user.get("username") or ""))
    add(str(auth_user.get("id") or "").strip())
    return candidates


def _auth_user_is_session_dm_or_admin(session, auth_user: dict) -> bool:
    """Return whether the authenticated user has session-wide character access."""
    if not isinstance(auth_user, dict) or not auth_user:
        return False
    auth_role = str(auth_user.get("role") or auth_user.get("account_role") or "").strip().lower()
    if auth_role in {"admin", "superadmin"}:
        return True

    auth_user_id = str(auth_user.get("id") or "").strip()
    auth_key = auth_player_key(auth_user_id) if auth_user_id else ""
    session_users = dict(getattr(session, "users", {}) or {})
    candidate_ids = [value for value in (auth_user_id, auth_key) if value]

    for candidate in candidate_ids:
        session_user = session_users.get(candidate)
        role = str(getattr(session_user, "role", "") or "").strip().lower() if session_user else ""
        if role == "dm":
            return True
        if candidate and str(getattr(session, "dm_id", "") or "").strip() == candidate:
            return True

    if auth_key:
        for session_user in session_users.values():
            if str(getattr(session_user, "player_key", "") or "").strip() == auth_key:
                role = str(getattr(session_user, "role", "") or "").strip().lower()
                if role == "dm":
                    return True

    return auth_role == "dm"


def _find_profile_in_owner_buckets(session, owner_keys: list[str], profile_id: str) -> dict | None:
    all_profiles = dict(getattr(session, "char_profiles", {}) or {})
    target_profile_id = str(profile_id or "").strip()
    for owner_key in owner_keys:
        owner_profiles = all_profiles.get(owner_key)
        if not isinstance(owner_profiles, list):
            continue
        for item in owner_profiles:
            if not isinstance(item, dict):
                continue
            if str(item.get("id") or "").strip() == target_profile_id:
                return item
    return None


def _session_member_owner_candidates(session_user, session_user_id: str) -> list[str]:
    """Return profile owner bucket keys for a session-membership principal (no JWT account).

    Built from the canonical :func:`resolve_owner_identity` so the keys match
    exactly what combat ownership checks and the authority layer use — the
    normalized display-name bucket, the session-user id, AND the resolved
    player_key (``auth_<id>``). The player_key was previously omitted here,
    so a session-link player whose profile bucket is keyed by player_key could
    be wrongly rejected for their own profile.
    """
    candidates: list[str] = []

    def add(value: str) -> None:
        value = str(value or "").strip()
        if value and value not in candidates:
            candidates.append(value)

    # Display-name bucket first (the common profile-bucket key), then the
    # remaining canonical identity keys (id + player_key).
    add(normalize_profile_owner_key(getattr(session_user, "name", "")))
    for key in resolve_owner_identity(None, session_user):
        add(key)
    add(session_user_id)
    return candidates


def _auth_user_session_ids(auth_user: dict) -> list[str]:
    """Return the session-user id(s) a JWT-authenticated account maps to.

    A logged-in account is keyed in ``session.users``/``active_char_profiles``
    by ``auth_<id>`` (see ``auth_player_key``). Some legacy sessions may also
    key directly by the raw account id, so both are checked.
    """
    auth_user_id = str((auth_user or {}).get("id") or "").strip()
    if not auth_user_id:
        return []
    candidates = [auth_user_id]
    auth_key = auth_player_key(auth_user_id)
    if auth_key not in candidates:
        candidates.append(auth_key)
    return candidates


def _session_active_profile_matches(session, auth_user: dict, target_profile_id: str) -> bool:
    """Safe fallback #1: this session restored an active-profile selection for this user.

    After a reconnect/server restart, ``session.active_char_profiles`` is restored
    from persisted state and may legitimately point this authenticated user's
    session-user id at ``target_profile_id`` even though the JWT-derived owner-key
    bucket lookup misses (e.g. display name normalization changed). This does not
    let an arbitrary user claim a profile_id by guessing it — it only matches when
    the SERVER already recorded that this specific session user has that profile
    active.
    """
    active_map = dict(getattr(session, "active_char_profiles", {}) or {})
    if not active_map:
        return False
    target = str(target_profile_id or "").strip()
    for session_user_id in _auth_user_session_ids(auth_user):
        if str(active_map.get(session_user_id) or "").strip() == target:
            return True
    return False


def _token_linked_profile_matches(session, auth_user: dict, target_profile_id: str) -> bool:
    """Safe fallback #2: a token owned by this session user is linked to the profile.

    Mirrors ``resolve_token_character_profile``'s ownership notion: a token's
    ``owner_id`` must be this user's session-user id, and the token's
    profile/library/character ref must match ``target_profile_id``.
    """
    target = str(target_profile_id or "").strip()
    if not target:
        return False
    session_ids = set(_auth_user_session_ids(auth_user))
    if not session_ids:
        return False
    for token in (getattr(session, "tokens", {}) or {}).values():
        owner_id = str(getattr(token, "owner_id", "") or "").strip()
        if owner_id not in session_ids:
            continue
        refs = {
            str(getattr(token, "profile_id", "") or "").strip(),
            str(getattr(token, "library_id", "") or "").strip(),
            str(getattr(token, "character_id", "") or "").strip(),
        }
        refs.discard("")
        if target in refs:
            return True
    return False


def resolve_owned_profile_or_403(
    session,
    auth_user: dict,
    profile_id: str,
    allow_dm: bool = True,
    *,
    session_user=None,
    session_user_id: str = "",
    is_session_dm: bool = False,
) -> dict:
    """Resolve a character profile without letting players enumerate others' profiles.

    Normal players may only resolve profiles in their own owner-key bucket(s).
    DM/admin users may resolve any profile in the session when allow_dm is true.

    When there is no JWT-authenticated `auth_user` (e.g. an invite-link guest
    with a session-membership identity only), `session_user`/`session_user_id`/
    `is_session_dm` describe that session-membership principal instead, resolved
    via resolve_session_authority so HTTP read access matches what the socket
    and session-authority checks already grant that guest.
    """
    target_profile_id = str(profile_id or "").strip()
    if not target_profile_id:
        raise HTTPException(status_code=404, detail="Character not found")

    if isinstance(auth_user, dict) and auth_user:
        owner_candidates = _profile_owner_candidates(auth_user)
        owned_profile = _find_profile_in_owner_buckets(session, owner_candidates, target_profile_id)
        if isinstance(owned_profile, dict):
            return owned_profile

        session_wide_profile = _find_profile_by_id(session, target_profile_id)
        if not isinstance(session_wide_profile, dict):
            raise HTTPException(status_code=404, detail="Character not found")

        if allow_dm and _auth_user_is_session_dm_or_admin(session, auth_user):
            return session_wide_profile

        # Reconnect-safe fallbacks: the owner-key bucket lookup above is the normal
        # path, but a restored session can legitimately know this user's active
        # profile or token linkage even when the bucket lookup misses. Neither
        # fallback lets a user reach a profile_id they merely guessed — both
        # require the SERVER's own session state to already associate this
        # specific authenticated user with target_profile_id.
        if _session_active_profile_matches(session, auth_user, target_profile_id):
            logger.info(
                "[live_state] spell_profile_access_fallback session_id=%s profile_id=%s via=active_session_profile",
                str(getattr(session, "id", "") or ""), target_profile_id,
            )
            return session_wide_profile
        if _token_linked_profile_matches(session, auth_user, target_profile_id):
            logger.info(
                "[live_state] spell_profile_access_fallback session_id=%s profile_id=%s via=token_linked_profile",
                str(getattr(session, "id", "") or ""), target_profile_id,
            )
            return session_wide_profile

        raise HTTPException(status_code=403, detail="Not authorized for this character")

    if session_user is not None or session_user_id:
        owner_candidates = _session_member_owner_candidates(session_user, session_user_id)
        owned_profile = _find_profile_in_owner_buckets(session, owner_candidates, target_profile_id)
        if isinstance(owned_profile, dict):
            return owned_profile

        session_wide_profile = _find_profile_by_id(session, target_profile_id)
        if not isinstance(session_wide_profile, dict):
            raise HTTPException(status_code=404, detail="Character not found")

        if allow_dm and is_session_dm:
            return session_wide_profile

        active_map = dict(getattr(session, "active_char_profiles", {}) or {})
        if session_user_id and str(active_map.get(session_user_id) or "").strip() == target_profile_id:
            logger.info(
                "[live_state] spell_profile_access_fallback session_id=%s profile_id=%s via=active_session_profile",
                str(getattr(session, "id", "") or ""), target_profile_id,
            )
            return session_wide_profile
        if session_user_id:
            for token in (getattr(session, "tokens", {}) or {}).values():
                if str(getattr(token, "owner_id", "") or "").strip() != session_user_id:
                    continue
                refs = {
                    str(getattr(token, "profile_id", "") or "").strip(),
                    str(getattr(token, "library_id", "") or "").strip(),
                    str(getattr(token, "character_id", "") or "").strip(),
                }
                refs.discard("")
                if target_profile_id in refs:
                    logger.info(
                        "[live_state] spell_profile_access_fallback session_id=%s profile_id=%s via=token_linked_profile",
                        str(getattr(session, "id", "") or ""), target_profile_id,
                    )
                    return session_wide_profile

        raise HTTPException(status_code=403, detail="Not authorized for this character")

    raise HTTPException(status_code=401, detail="Not authenticated")


def _resolve_request_principal(request: Request, session, user_id: str = ""):
    """Resolve the requesting principal for character/spell read endpoints.

    Prefers a JWT-authenticated account (unchanged behavior). When absent,
    falls back to the session-membership identity used by the websocket and
    `resolve_session_authority` (e.g. an invite-link guest with no account),
    so a session member who passes the socket/authority checks is not
    rejected by HTTP reads with a 401. Returns
    (auth_user, session_user, session_user_id, is_session_dm).
    """
    auth_user = get_request_user(request)
    if auth_user:
        return auth_user, None, "", False

    fallback_user_id = str(user_id or request.query_params.get("user_id") or "").strip()
    if not fallback_user_id:
        return None, None, "", False

    authority = resolve_session_authority(request, session, fallback_user_id=fallback_user_id)
    session_user_id = str(authority.get("resolved_session_user_id") or "").strip()
    session_user = session.users.get(session_user_id) if session_user_id else None
    is_session_dm = bool(authority.get("is_session_dm"))
    return None, session_user, session_user_id, is_session_dm


async def _persist_imported_document(*, session_id: str, auth_user: dict, document: dict, profile_id: str = "", source: str = "unknown") -> dict:
    session = get_or_restore_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    owner_key = _resolve_owner_key(auth_user)
    if not owner_key:
        raise HTTPException(status_code=400, detail="Unable to resolve profile owner")

    attach_library_gap_report(document)
    upsert_payload = build_profile_upsert_payload(document, profile_id=profile_id)
    review = _attach_import_review(
        upsert_payload.get("nativeCharacter") if isinstance(upsert_payload.get("nativeCharacter"), dict) else document,
        source=source,
        runtime=upsert_payload.get("nativeRuntime") if isinstance(upsert_payload.get("nativeRuntime"), dict) else None,
    )
    if review:
        native = upsert_payload.get("nativeCharacter") if isinstance(upsert_payload.get("nativeCharacter"), dict) else {}
        upsert_payload["nativeCharacter"] = native
        upsert_payload["importMeta"] = native.get("importMeta") if isinstance(native.get("importMeta"), dict) else upsert_payload.get("importMeta", {})
    saved_profile = upsert_char_profile_for_owner(session, owner_key, upsert_payload)
    _apply_profile_max_hp_to_linked_tokens(session, saved_profile)
    await save_campaign_async(session)

    return _normalize_profile_entry(saved_profile if isinstance(saved_profile, dict) else {}, fallback_id="imported")


def _source_type_for_api(source: str) -> str:
    return normalize_import_source(source)


def _attach_import_review(document: dict, *, source: str, runtime: dict | None = None) -> dict:
    if not isinstance(document, dict):
        return {}
    review = build_import_review(document, source_type=_source_type_for_api(source), runtime=runtime)
    import_meta = document.get("importMeta") if isinstance(document.get("importMeta"), dict) else {}
    import_meta = dict(import_meta)
    import_meta["sourceType"] = review.get("sourceType")
    import_meta["importReview"] = review
    document["importMeta"] = import_meta
    return review


def _character_import_preview_payload(*, source: str, normalized: dict) -> dict:
    document = normalized.get("document") if isinstance(normalized.get("document"), dict) else {}
    gap_report = attach_library_gap_report(document) if document else {
        "items": {"exact": [], "alias": [], "partial": [], "missing": []},
        "spells": {"exact": [], "alias": [], "partial": [], "missing": []},
        "features": {"exact": [], "alias": [], "partial": [], "missing": []},
    }
    preview_runtime = None
    if document:
        try:
            preview_runtime = (resolve_character_runtime(document).get("runtime") or {})
        except Exception:
            preview_runtime = None
    review = _attach_import_review(document, source=source, runtime=preview_runtime) if document else build_import_review({}, source_type=_source_type_for_api(source))
    return {
        "ok": True,
        "source": source,
        "source_type": _source_type_for_api(source),
        "preview_document": document,
        "library_gap_report": gap_report,
        "import_review": review,
        "warnings": normalized.get("warnings") or [],
        "requires_resolution": bool(normalized.get("requires_resolution")) or bool(review.get("blockingIssues")),
        "required_choices": normalized.get("required_choices") or [],
    }


def _blocking_import_choices(normalized: dict) -> list:
    required = normalized.get("required_choices")
    if isinstance(required, list):
        return required
    warnings = normalized.get("warnings") if isinstance(normalized.get("warnings"), list) else []
    return [item for item in warnings if isinstance(item, dict) and item.get("blocking")]


def _merge_import_resolution(raw_payload, resolution: dict | None):
    if not isinstance(raw_payload, dict):
        return raw_payload
    if not isinstance(resolution, dict) or not resolution:
        return raw_payload
    merged = dict(raw_payload)
    existing = merged.get("import_resolution") if isinstance(merged.get("import_resolution"), dict) else {}
    merged["import_resolution"] = {**existing, **resolution}
    return merged


def _document_import_required_choices(document: dict) -> list:
    if not isinstance(document, dict):
        return []
    import_meta = document.get("importMeta") if isinstance(document.get("importMeta"), dict) else {}
    warnings = import_meta.get("warnings") if isinstance(import_meta.get("warnings"), list) else []
    return [item for item in warnings if isinstance(item, dict) and item.get("blocking")]


def _normalize_commit_document(payload: dict) -> dict | None:
    for key in ("preview_document", "canonical_document", "character_document", "document"):
        candidate = payload.get(key) if isinstance(payload, dict) else None
        if isinstance(candidate, dict):
            return normalize_incoming_document(candidate)
    return None


def _unresolved_import_response(*, source: str, normalized: dict) -> JSONResponse:
    return JSONResponse(
        {
            "ok": False,
            "source": source,
            "warnings": normalized.get("warnings") or [],
            "requires_resolution": True,
            "required_choices": _blocking_import_choices(normalized),
        },
        status_code=400,
    )


def _normalize_rules_mode(value: str) -> str:
    mode = str(value or "").strip().lower()
    if mode in _KNOWN_RULES_MODES:
        return mode
    return "casual"


def _rules_mode_allows_row(row: dict, rules_mode: str) -> bool:
    if not isinstance(row, dict):
        return False
    allowed = row.get("allowedRulesModes")
    if isinstance(allowed, (list, tuple, set)):
        normalized = {
            str(item or "").strip().lower()
            for item in allowed
            if str(item or "").strip()
        }
        if normalized:
            return rules_mode in normalized
    return True


def _parse_spell_ids(raw_value: str | None) -> list[str]:
    if not raw_value:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for part in str(raw_value).split(","):
        spell_id = str(part or "").strip()
        if not spell_id or spell_id in seen:
            continue
        seen.add(spell_id)
        out.append(spell_id)
    return out


def _parse_abilities_payload(raw_value: str | None) -> dict:
    if not raw_value:
        return {}
    try:
        parsed = json.loads(raw_value)
    except Exception:
        return {}
    if isinstance(parsed, dict):
        return parsed
    return {}


def _highest_unlocked_spell_level(spell_slots: dict) -> int:
    highest = 0
    if not isinstance(spell_slots, dict):
        return highest
    for key, raw_count in spell_slots.items():
        try:
            count = int(raw_count)
        except Exception:
            count = 0
        if count <= 0:
            continue
        token = str(key or "").strip().lower()
        digits = "".join(ch for ch in token if ch.isdigit())
        level = _safe_int(digits, default=0) if digits else 0
        if level > highest:
            highest = level
    return highest


def _fallback_highest_unlocked_spell_level(class_id: str, class_level: int, class_spell_pool: set[str]) -> int:
    """Infer highest unlocked spell level from class spell unlock metadata.

    Some classes (notably pact casters) do not expose tiered entries in
    spellSlots for all levels, so relying on slot maps alone can report
    "cantrips only" even when leveled spells are legal at this class level.
    """
    normalized_class_id = str(class_id or "").strip().lower()
    if not normalized_class_id or class_level <= 0:
        return 0

    class_row = get_class_catalog_row(normalized_class_id) if normalized_class_id else {}
    progression = class_row.get("progressionTable") if isinstance(class_row.get("progressionTable"), list) else []
    level_row = next(
        (
            row for row in progression
            if isinstance(row, dict) and _safe_int(row.get("level"), default=0, minimum=0, maximum=20) == class_level
        ),
        None,
    )
    mechanics = level_row.get("classMechanics") if isinstance(level_row, dict) and isinstance(level_row.get("classMechanics"), dict) else {}

    pact_slot_level = _safe_int(mechanics.get("pactSlotLevel"), default=0, minimum=0, maximum=9)
    if pact_slot_level > 0:
        return pact_slot_level

    # Fallback floor for known/prepared casters with legal leveled spells but no
    # explicit slot map exposed on this endpoint payload.
    if _safe_int(mechanics.get("spellsKnown"), default=0, minimum=0, maximum=99) > 0:
        return 1
    if str(mechanics.get("spellsPreparedFormula") or "").strip():
        return 1
    return 0


@lru_cache(maxsize=1)
def _builder_class_spell_pool() -> dict[str, set[str]]:
    root = Path(__file__).resolve().parents[1] / "data" / "rules" / "5e2024" / "class_spell_lists.json"
    try:
        payload = json.loads(root.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out: dict[str, set[str]] = {}
    if not isinstance(payload, dict):
        return out
    for raw_class_id, buckets in payload.items():
        class_id = str(raw_class_id or "").strip().lower()
        if not class_id:
            continue
        spell_ids: set[str] = set()
        if isinstance(buckets, dict):
            for values in buckets.values():
                if not isinstance(values, list):
                    continue
                for raw_spell_id in values:
                    spell_id = str(raw_spell_id or "").strip().lower()
                    if spell_id:
                        spell_ids.add(spell_id)
        if spell_ids:
            out[class_id] = spell_ids
    return out


@router.get("/api/character/content/catalog")
async def api_character_content_catalog(request: Request, rules_mode: str = "casual"):
    auth_user = get_request_user(request)
    if not auth_user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    normalized_rules_mode = _normalize_rules_mode(rules_mode)
    catalog = get_builder_rules_catalog()

    species_rows = [
        row for row in (catalog.get("species") or [])
        if isinstance(row, dict) and _rules_mode_allows_row(row, normalized_rules_mode)
    ]
    class_rows = [
        row for row in (catalog.get("classes") or [])
        if isinstance(row, dict) and _rules_mode_allows_row(row, normalized_rules_mode)
    ]
    class_ids = {str(row.get("id") or "").strip().lower() for row in class_rows}
    subclass_rows = [
        row for row in (catalog.get("subclasses") or [])
        if (
            isinstance(row, dict)
            and _rules_mode_allows_row(row, normalized_rules_mode)
            and str(row.get("classId") or "").strip().lower() in class_ids
        )
    ]
    talent_rows = [
        row for row in (catalog.get("talents") or [])
        if (
            isinstance(row, dict)
            and _rules_mode_allows_row(row, normalized_rules_mode)
            and any(str(class_id or "").strip().lower() in class_ids for class_id in (row.get("classRestrictions") or []))
        )
    ]
    awakening_rows = [
        row for row in (catalog.get("awakenings") or [])
        if (
            isinstance(row, dict)
            and _rules_mode_allows_row(row, normalized_rules_mode)
            and any(str(class_id or "").strip().lower() in class_ids for class_id in (row.get("classRestrictions") or []))
        )
    ]
    feat_rows = [
        row for row in (catalog.get("featsGeneral") or []) + (catalog.get("featsOrigin") or [])
        if isinstance(row, dict) and _rules_mode_allows_row(row, normalized_rules_mode)
    ]

    subclasses_by_class: dict[str, list[dict]] = {}
    for row in subclass_rows:
        class_id = str(row.get("classId") or "").strip().lower()
        if not class_id:
            continue
        subclasses_by_class.setdefault(class_id, []).append(
            {
                "id": str(row.get("id") or "").strip(),
                "classId": class_id,
                "parentClassId": str(row.get("parentClassId") or "").strip(),
                "displayName": str(row.get("displayName") or "").strip(),
                "flavorText": str(row.get("flavorText") or "").strip(),
                "featureUnlocksByLevel": row.get("featureUnlocksByLevel")
                if isinstance(row.get("featureUnlocksByLevel"), dict)
                else {},
                "features": row.get("features") if isinstance(row.get("features"), list) else [],
                "featureDefinitions": row.get("featureDefinitions")
                if isinstance(row.get("featureDefinitions"), dict)
                else {},
            }
        )

    talents_by_class: dict[str, list[dict]] = {}
    for row in talent_rows:
        class_restrictions = row.get("classRestrictions") if isinstance(row.get("classRestrictions"), list) else []
        for class_id_raw in class_restrictions:
            class_id = str(class_id_raw or "").strip().lower()
            if not class_id or class_id not in class_ids:
                continue
            talents_by_class.setdefault(class_id, []).append(
                {
                    "id": str(row.get("id") or "").strip(),
                    "displayName": str(row.get("displayName") or "").strip(),
                    "classRestrictions": class_restrictions,
                    "minimumLevel": _safe_int(row.get("minimumLevel"), default=1, minimum=1, maximum=20),
                    "grants": row.get("grants") if isinstance(row.get("grants"), list) else [],
                    "tags": row.get("tags") if isinstance(row.get("tags"), list) else [],
                    "source": str(row.get("source") or "casualdnd_talent").strip() or "casualdnd_talent",
                }
            )

    awakenings_by_class: dict[str, list[dict]] = {}
    for row in awakening_rows:
        class_restrictions = row.get("classRestrictions") if isinstance(row.get("classRestrictions"), list) else []
        for class_id_raw in class_restrictions:
            class_id = str(class_id_raw or "").strip().lower()
            if not class_id or class_id not in class_ids:
                continue
            awakenings_by_class.setdefault(class_id, []).append(
                {
                    "id": str(row.get("id") or "").strip(),
                    "displayName": str(row.get("displayName") or "").strip(),
                    "classRestrictions": class_restrictions,
                    "minimumLevel": _safe_int(row.get("minimumLevel"), default=15, minimum=1, maximum=20),
                    "grants": row.get("grants") if isinstance(row.get("grants"), list) else [],
                    "tiers": row.get("tiers") if isinstance(row.get("tiers"), list) else [],
                    "source": str(row.get("source") or "casualdnd_awakening").strip() or "casualdnd_awakening",
                }
            )

    return JSONResponse(
        {
            "ok": True,
            "rulesMode": normalized_rules_mode,
            "rulesetId": str(catalog.get("rulesetId") or ""),
            "catalogVersion": 1,
            "species": [
                {
                    "id": str(row.get("id") or "").strip(),
                    "displayName": str(row.get("displayName") or "").strip(),
                    "size": str(row.get("size") or "").strip(),
                    "movement": row.get("movement") if isinstance(row.get("movement"), dict) else {},
                    "senses": row.get("senses") if isinstance(row.get("senses"), dict) else {},
                    "tags": row.get("tags") if isinstance(row.get("tags"), list) else [],
                    "languages": row.get("languages") if isinstance(row.get("languages"), list) else [],
                    "abilityBonuses": row.get("abilityBonuses"),
                    "proficiencies": row.get("proficiencies") if isinstance(row.get("proficiencies"), dict) else {},
                    "traits": row.get("traits") if isinstance(row.get("traits"), list) else [],
                    "flavorText": str(row.get("flavorText") or "").strip(),
                    "roleplayNotes": str(row.get("roleplayNotes") or "").strip(),
                    "recommendedClasses": row.get("recommendedClasses")
                    if isinstance(row.get("recommendedClasses"), list)
                    else [],
                }
                for row in species_rows
            ],
            "classes": [
                {
                    "id": str(row.get("id") or "").strip(),
                    "displayName": str(row.get("displayName") or "").strip(),
                    "hitDie": _safe_int(row.get("hitDie"), default=0, minimum=0, maximum=20),
                    "primaryAbilities": row.get("primaryAbilities")
                    if isinstance(row.get("primaryAbilities"), list)
                    else [],
                    "savingThrows": row.get("savingThrows")
                    if isinstance(row.get("savingThrows"), list)
                    else [],
                    "armorProficiencies": row.get("armorProficiencies")
                    if isinstance(row.get("armorProficiencies"), list)
                    else [],
                    "weaponProficiencies": row.get("weaponProficiencies")
                    if isinstance(row.get("weaponProficiencies"), list)
                    else [],
                    "toolProficiencies": row.get("toolProficiencies")
                    if isinstance(row.get("toolProficiencies"), list)
                    else [],
                    "skillChoices": row.get("skillChoices")
                    if isinstance(row.get("skillChoices"), dict)
                    else {},
                    "spellcastingType": str(row.get("spellcastingType") or "none").strip() or "none",
                    "subclassLevel": _safe_int(row.get("subclassLevel"), default=0, minimum=0, maximum=20),
                    "buildTips": row.get("buildTips") if isinstance(row.get("buildTips"), dict) else {},
                    "subclassDisplayName": str(row.get("subclassDisplayName") or "Subclass").strip() or "Subclass",
                    "classDescription": str(row.get("classDescription") or "").strip(),
                    "roleIdentity": str(row.get("roleIdentity") or "").strip(),
                    "progressionTable": row.get("progressionTable")
                    if isinstance(row.get("progressionTable"), list)
                    else [],
                    "progressionSummary": row.get("progressionSummary")
                    if isinstance(row.get("progressionSummary"), list)
                    else [],
                    "featureDefinitions": row.get("featureDefinitions")
                    if isinstance(row.get("featureDefinitions"), dict)
                    else {},
                    "featuresByLevel": row.get("featuresByLevel")
                    if isinstance(row.get("featuresByLevel"), list)
                    else [],
                    "spellSlots": row.get("spellSlots")
                    if isinstance(row.get("spellSlots"), dict)
                    else {},
                    "cantripsKnownByLevel": row.get("cantripsKnownByLevel")
                    if isinstance(row.get("cantripsKnownByLevel"), dict)
                    else {},
                    "spellsKnownByLevel": row.get("spellsKnownByLevel")
                    if isinstance(row.get("spellsKnownByLevel"), dict)
                    else {},
                }
                for row in class_rows
            ],
            "subclasses": subclass_rows,
            "subclassesByClass": subclasses_by_class,
            "feats": [
                {
                    "id": str(row.get("id") or "").strip(),
                    "displayName": str(row.get("displayName") or row.get("name") or "").strip(),
                    "prerequisite": row.get("prerequisite"),
                    "description": str(row.get("description") or "").strip(),
                    "abilityBonus": row.get("abilityBonus") if isinstance(row.get("abilityBonus"), dict) else {},
                    "source": str(row.get("source") or "feat").strip() or "feat",
                }
                for row in feat_rows
                if str(row.get("id") or "").strip()
            ],
            "talents": talent_rows,
            "talentsByClass": talents_by_class,
            "awakenings": awakening_rows,
            "awakeningsByClass": awakenings_by_class,
            "futureContent": {
                "feats": [],
                "spells": [],
                "awakening": awakening_rows,
            },
        }
    )


@router.get("/api/character/builder/spells/options")
async def api_character_builder_spell_options(
    request: Request,
    class_id: str,
    level: int = 1,
    subclass_id: str = "",
    known: str = "",
    prepared: str = "",
    abilities: str = "",
):
    auth_user = get_request_user(request)
    if not auth_user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    normalized_class_id = str(class_id or "").strip().lower()
    class_level = _safe_int(level, default=1, minimum=1, maximum=20)
    known_ids = _parse_spell_ids(known)
    prepared_ids = _parse_spell_ids(prepared)
    abilities_payload = _parse_abilities_payload(abilities)
    pseudo_document = {
        "classes": [{
            "classId": normalized_class_id,
            "level": class_level,
            "subclassId": str(subclass_id or "").strip().lower(),
        }],
        "spellState": {"known": known_ids, "prepared": prepared_ids},
    }
    limits = build_spell_limits_for_class(
        normalized_class_id,
        class_level,
        abilities_payload,
        document=pseudo_document,
        subclass_id=str(subclass_id or "").strip().lower(),
    )
    validation = validate_spell_selection(
        class_id=normalized_class_id,
        class_level=class_level,
        abilities=abilities_payload,
        known=known_ids,
        prepared=prepared_ids,
        document=pseudo_document,
        subclass_id=str(subclass_id or "").strip().lower(),
    )

    highest_unlocked = _highest_unlocked_spell_level(limits.get("spellSlots") if isinstance(limits, dict) else {})
    known_set = {str(v or "").strip().lower() for v in (validation.get("known") or []) if str(v or "").strip()}
    prepared_set = {str(v or "").strip().lower() for v in (validation.get("prepared") or []) if str(v or "").strip()}
    subclass_grants = validation.get("subclassGrants") if isinstance(validation.get("subclassGrants"), dict) else {}
    class_bonus = validation.get("classBonusGrants") if isinstance(validation.get("classBonusGrants"), dict) else {}
    bonus_access = set(subclass_grants.get("alwaysPrepared") or []) | set(subclass_grants.get("alwaysKnown") or []) | set(class_bonus.get("alwaysKnown") or [])
    class_spell_pool = _builder_class_spell_pool().get(normalized_class_id, set())
    if not class_spell_pool:
        # Fallback for environments missing class_spell_lists metadata.
        class_spell_pool = {
            str(row.get("id") or "").strip().lower()
            for row in list_compendium_spells(cls=normalized_class_id)
            if isinstance(row, dict)
        }
    if highest_unlocked <= 0:
        highest_unlocked = _fallback_highest_unlocked_spell_level(normalized_class_id, class_level, class_spell_pool)

    cards: list[dict] = []
    for spell in list_compendium_spells():
        if not isinstance(spell, dict):
            continue
        spell_id = str(spell.get("id") or "").strip().lower()
        if not spell_id:
            continue
        in_class_pool = spell_id in class_spell_pool
        unlock = (spell.get("classUnlockLevels") or {}).get(normalized_class_id)
        has_bonus_access = spell_id in bonus_access
        if not in_class_pool and not has_bonus_access and spell_id not in known_set and spell_id not in prepared_set:
            continue

        is_accessible = False
        if has_bonus_access:
            is_accessible = True
        elif in_class_pool and unlock is not None and class_level >= _safe_int(unlock, 99):
            is_accessible = True

        spell_level = _safe_int(spell.get("level"), 0)
        if spell_level > 0 and highest_unlocked >= 0 and spell_level > highest_unlocked and not has_bonus_access:
            is_accessible = False
        if not is_accessible and spell_id not in known_set and spell_id not in prepared_set:
            continue
        cards.append(
            build_spell_card(
                spell,
                character_context={
                    "unlockLevel": unlock,
                    "isKnown": spell_id in known_set,
                    "isPrepared": spell_id in prepared_set,
                    "isAccessible": is_accessible,
                    "blockedReason": "" if is_accessible else "Not unlocked at current class level.",
                    "highestAvailableSlot": highest_unlocked,
                    "selectionMode": "prepared" if limits.get("preparedLimit") is not None else ("known" if limits.get("spellsKnown") is not None else "library"),
                },
            )
        )

    return JSONResponse({
        "ok": True,
        "classId": normalized_class_id,
        "level": class_level,
        "limits": limits,
        "validation": validation,
        "cards": cards,
        "known": validation.get("known") or [],
        "prepared": validation.get("prepared") or [],
        "highestUnlockedSpellLevel": highest_unlocked,
    })


@router.get("/api/character/library")
async def api_character_library(request: Request, session_id: str = "", include_native: bool = False):
    auth_user = get_request_user(request)
    if not auth_user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    profiles: list[dict] = []
    normalized_session_id = str(session_id or "").strip().upper()
    if normalized_session_id:
        session = get_or_restore_session(normalized_session_id)
        if session:
            all_profiles = dict(getattr(session, "char_profiles", {}) or {})
            owner_candidates = []

            display_name_key = normalize_profile_owner_key(auth_display_name(auth_user, fallback=""))
            if display_name_key:
                owner_candidates.append(display_name_key)

            username_key = normalize_profile_owner_key(auth_user.get("username") or "")
            if username_key and username_key not in owner_candidates:
                owner_candidates.append(username_key)

            user_id = str(auth_user.get("id") or "").strip()
            if user_id:
                owner_candidates.append(user_id)

            mine: list[dict] = []
            for owner_key in owner_candidates:
                candidate = all_profiles.get(owner_key)
                if isinstance(candidate, list) and candidate:
                    mine = list(candidate)
                    break

            profiles = []
            for idx, item in enumerate(mine):
                row = item if isinstance(item, dict) else {}
                summary = _normalize_profile_entry(row, fallback_id=f"legacy-{idx + 1}")
                if include_native:
                    native_doc = row.get("nativeCharacter") if isinstance(row.get("nativeCharacter"), dict) else None
                    if native_doc:
                        summary["nativeCharacter"] = native_doc
                    native_runtime = row.get("nativeRuntime") if isinstance(row.get("nativeRuntime"), dict) else None
                    if native_runtime:
                        summary["nativeRuntime"] = native_runtime
                    char_book = row.get("charBook") if isinstance(row.get("charBook"), dict) else None
                    if char_book:
                        summary["charBook"] = char_book
                    char_sheet = row.get("charSheet") if isinstance(row.get("charSheet"), dict) else None
                    if char_sheet:
                        summary["charSheet"] = char_sheet
                profiles.append(summary)

    return JSONResponse(
        {
            "ok": True,
            "session_id": normalized_session_id,
            "profiles": profiles,
        }
    )

@router.get("/api/character/library-gaps")
async def api_character_library_gaps(request: Request, session_id: str = "", limit: int = 20):
    """Return an aggregate dev/admin view of missing imported character content."""
    auth_user = get_request_user(request)
    if not auth_user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    normalized_session_id = str(session_id or "").strip().upper()
    if not normalized_session_id:
        raise HTTPException(status_code=400, detail="session_id is required")

    session = get_or_restore_session(normalized_session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    all_profiles = dict(getattr(session, "char_profiles", {}) or {})

    # DMs/admin-dev users can see campaign-wide gaps; players only see their own
    # imported gaps so the endpoint does not leak unrelated character details.
    requester_id = str(auth_user.get("id") or "").strip()
    requester = session.users.get(requester_id) if requester_id else None
    if requester is not None and requester.role != "dm":
        owner_key = _resolve_owner_key(auth_user)
        all_profiles = {owner_key: all_profiles.get(owner_key) or []}

    try:
        normalized_limit = int(limit or 20)
    except Exception:
        normalized_limit = 20
    summary = summarize_library_gaps_from_profiles(all_profiles, limit=max(1, min(normalized_limit, 100)))
    return JSONResponse({"session_id": normalized_session_id, **summary})


@router.post("/api/character/levelup/preview")
async def api_character_levelup_preview(request: Request):
    auth_user = get_request_user(request)
    if not auth_user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    incoming_document = payload.get("character_document")
    if incoming_document is None:
        incoming_document = payload.get("document")
    if incoming_document is None:
        incoming_document = payload.get("nativeCharacter")
    if incoming_document is None:
        incoming_document = payload

    try:
        canonical_document = normalize_incoming_document(incoming_document)
        classes = canonical_document.get("classes") if isinstance(canonical_document.get("classes"), list) else []
        primary_class = classes[0] if classes and isinstance(classes[0], dict) else {}
        class_id = str(primary_class.get("classId") or primary_class.get("id") or primary_class.get("name") or "").strip().lower()
        class_level = _safe_int(primary_class.get("level"), default=1, minimum=1, maximum=20)
        if class_id:
            repair_spell_state_for_document(
                canonical_document,
                class_id=class_id,
                class_level=class_level,
                abilities=canonical_document.get("abilities") if isinstance(canonical_document.get("abilities"), dict) else {},
                subclass_id=str(primary_class.get("subclassId") or "").strip().lower(),
            )
        preview = preview_levelup(canonical_document)
    except CharacterValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return JSONResponse({"ok": True, **preview})


@router.post("/api/character/levelup/apply")
async def api_character_levelup_apply(request: Request):
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

    incoming_document = payload.get("character_document")
    if incoming_document is None:
        incoming_document = payload.get("document")
    if incoming_document is None:
        incoming_document = payload.get("nativeCharacter")
    if incoming_document is None:
        incoming_document = payload

    try:
        canonical_document = normalize_incoming_document(incoming_document)
        classes = canonical_document.get("classes") if isinstance(canonical_document.get("classes"), list) else []
        primary_class = classes[0] if classes and isinstance(classes[0], dict) else {}
        class_id = str(primary_class.get("classId") or primary_class.get("id") or primary_class.get("name") or "").strip().lower()
        class_level = _safe_int(primary_class.get("level"), default=1, minimum=1, maximum=20)
        if class_id:
            repair_spell_state_for_document(
                canonical_document,
                class_id=class_id,
                class_level=class_level,
                abilities=canonical_document.get("abilities") if isinstance(canonical_document.get("abilities"), dict) else {},
                subclass_id=str(primary_class.get("subclassId") or "").strip().lower(),
            )
        applied = apply_character_levelup(
            canonical_document,
            choices=payload.get("choices") if isinstance(payload.get("choices"), dict) else {},
        )
    except CharacterValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LevelupApplyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    profile_id = str(payload.get("profile_id") or payload.get("profileId") or "").strip()
    upsert_payload = build_profile_upsert_payload(
        applied.get("document") or {},
        profile_id=profile_id,
        persisted_runtime=payload.get("nativeRuntime"),
    )

    owner_key = _resolve_owner_key(auth_user)
    if not owner_key:
        raise HTTPException(status_code=400, detail="Unable to resolve profile owner")

    saved_profile = upsert_char_profile_for_owner(session, owner_key, upsert_payload)
    _apply_profile_max_hp_to_linked_tokens(session, saved_profile)
    await save_campaign_async(session)

    summary = _normalize_profile_entry(saved_profile if isinstance(saved_profile, dict) else {}, fallback_id="native")
    return JSONResponse(
        {
            "ok": True,
            "session_id": session_id,
            "profile_id": str(saved_profile.get("id") or ""),
            "profile": summary,
            "nativeCharacter": upsert_payload.get("nativeCharacter") or {},
            "nativeRuntime": upsert_payload.get("nativeRuntime") or {},
            "applied": applied.get("applied") or {},
            "meta": applied.get("meta") or {},
        }
    )


@router.post("/api/characters/{character_id}/levelup")
async def apply_levelup_endpoint(character_id: str, request: Request):
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

    owner_key = _resolve_owner_key(auth_user)
    if not owner_key:
        raise HTTPException(status_code=400, detail="Unable to resolve profile owner")

    owner_profiles = session.char_profiles.get(owner_key) if isinstance(session.char_profiles, dict) else None
    profile_rows = owner_profiles if isinstance(owner_profiles, list) else []
    profile_index = -1
    existing_profile: dict | None = None
    for idx, row in enumerate(profile_rows):
        if not isinstance(row, dict):
            continue
        if str(row.get("id") or "").strip() == character_id:
            profile_index = idx
            existing_profile = row
            break
    if existing_profile is None:
        raise HTTPException(status_code=404, detail="Character not found")

    native_document = existing_profile.get("nativeCharacter")
    if not isinstance(native_document, dict):
        raise HTTPException(status_code=400, detail="Character does not have a native document")

    try:
        canonical_document = normalize_incoming_document(native_document)
        applied = apply_character_levelup(
            canonical_document,
            choices=payload.get("choices") if isinstance(payload.get("choices"), dict) else {},
        )
    except CharacterValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LevelupApplyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    upsert_payload = build_profile_upsert_payload(
        applied.get("document") or {},
        profile_id=character_id,
        persisted_runtime=existing_profile.get("nativeRuntime") if isinstance(existing_profile, dict) else None,
    )
    saved_profile = upsert_char_profile_for_owner(session, owner_key, upsert_payload)
    if profile_index >= 0 and isinstance(saved_profile, dict):
        profile_rows[profile_index] = saved_profile
    _apply_profile_max_hp_to_linked_tokens(session, saved_profile)
    await save_campaign_async(session)

    return JSONResponse(
        {
            "ok": True,
            "session_id": session_id,
            "profile_id": character_id,
            "profile": _normalize_profile_entry(saved_profile if isinstance(saved_profile, dict) else {}, fallback_id=character_id),
            "nativeCharacter": upsert_payload.get("nativeCharacter") or {},
            "nativeRuntime": upsert_payload.get("nativeRuntime") or {},
            "applied": applied.get("applied") or {},
            "meta": applied.get("meta") or {},
        }
    )


@router.post("/api/character/save")
async def api_character_save(request: Request):
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

    incoming_document = payload.get("character_document")
    if incoming_document is None:
        incoming_document = payload.get("document")
    if incoming_document is None:
        incoming_document = payload

    try:
        canonical_document = normalize_incoming_document(incoming_document)
    except CharacterValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    profile_id = str(payload.get("profile_id") or payload.get("profileId") or "").strip()
    upsert_payload = build_profile_upsert_payload(
        canonical_document,
        profile_id=profile_id,
        persisted_runtime=payload.get("nativeRuntime"),
    )

    owner_key = _resolve_owner_key(auth_user)
    if not owner_key:
        raise HTTPException(status_code=400, detail="Unable to resolve profile owner")

    saved_profile = upsert_char_profile_for_owner(session, owner_key, upsert_payload)
    _apply_profile_max_hp_to_linked_tokens(session, saved_profile)
    await save_campaign_async(session)

    summary = _normalize_profile_entry(saved_profile if isinstance(saved_profile, dict) else {}, fallback_id="native")
    return JSONResponse(
        {
            "ok": True,
            "session_id": session_id,
            "profile_id": str(saved_profile.get("id") or ""),
            "profile": summary,
        }
    )


@router.delete("/api/character/profile/{profile_id}")
async def api_character_profile_delete(request: Request, profile_id: str):
    auth_user = get_request_user(request)
    if not auth_user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    session_id = str(request.query_params.get("session_id") or "").strip().upper()
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required")

    session = get_or_restore_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    normalized_profile_id = str(profile_id).strip()
    profiles = dict(getattr(session, "char_profiles", {}) or {})
    owner_candidates: list[str] = []
    display_name_key = normalize_profile_owner_key(auth_display_name(auth_user, fallback=""))
    if display_name_key:
        owner_candidates.append(display_name_key)

    username_key = normalize_profile_owner_key(auth_user.get("username") or "")
    if username_key and username_key not in owner_candidates:
        owner_candidates.append(username_key)

    user_id = str(auth_user.get("id") or "").strip()
    if user_id and user_id not in owner_candidates:
        owner_candidates.append(user_id)

    removed_any = False
    for owner_key in owner_candidates:
        mine = list(profiles.get(owner_key, []) or [])
        cleaned = [p for p in mine if str(p.get("id") or "") != normalized_profile_id]
        if len(cleaned) != len(mine):
            profiles[owner_key] = cleaned
            removed_any = True

    if not removed_any:
        raise HTTPException(status_code=404, detail="Profile not found")

    session.char_profiles = profiles
    await save_campaign_async(session)

    return JSONResponse({"ok": True, "profile_id": normalized_profile_id})


@router.post("/api/character/import/ddb-id/preview")
async def api_character_import_ddb_id_preview(request: Request):
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
    character_id = str(payload.get("character_id") or payload.get("characterId") or "").strip()
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required")
    if not character_id:
        raise HTTPException(status_code=400, detail="character_id is required")

    ddb_response = await fetch_ddb_character_response(character_id)
    try:
        response_payload = json.loads((ddb_response.body or b"{}").decode("utf-8"))
    except Exception:
        response_payload = {}

    if ddb_response.status_code != 200:
        raise HTTPException(status_code=400, detail=str(response_payload.get("error") or "D&D Beyond import failed"))

    normalized = normalize_ddb_json_payload(
        _merge_import_resolution(response_payload, payload.get("import_resolution")),
        external_id=character_id,
    )
    return JSONResponse(_character_import_preview_payload(source="dndbeyond", normalized=normalized))


@router.post("/api/character/import/ddb-id/commit")
async def api_character_import_ddb_id_commit(request: Request):
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

    document = _normalize_commit_document(payload)
    normalized: dict | None = None
    if document is None:
        character_id = str(payload.get("character_id") or payload.get("characterId") or "").strip()
        if not character_id:
            raise HTTPException(status_code=400, detail="character_id or preview_document is required")
        ddb_response = await fetch_ddb_character_response(character_id)
        try:
            response_payload = json.loads((ddb_response.body or b"{}").decode("utf-8"))
        except Exception:
            response_payload = {}
        if ddb_response.status_code != 200:
            raise HTTPException(status_code=400, detail=str(response_payload.get("error") or "D&D Beyond import failed"))
        normalized = normalize_ddb_json_payload(
            _merge_import_resolution(response_payload, payload.get("import_resolution")),
            external_id=character_id,
        )
        if normalized.get("requires_resolution"):
            return _unresolved_import_response(source="dndbeyond", normalized=normalized)
        document = normalized["document"]

    required_choices = _document_import_required_choices(document)
    if required_choices:
        return _unresolved_import_response(
            source="dndbeyond",
            normalized={
                "warnings": (document.get("importMeta") or {}).get("warnings") or [],
                "required_choices": required_choices,
            },
        )

    profile = await _persist_imported_document(
        session_id=session_id, auth_user=auth_user, document=document, source="dndbeyond"
    )
    return JSONResponse(
        {
            "ok": True,
            "session_id": session_id,
            "profile": profile,
            "warnings": (normalized or {}).get("warnings") or [],
            "source": "dndbeyond",
            "source_type": "dndbeyond",
            "import_review": ((profile.get("importMeta") or {}).get("importReview") if isinstance(profile, dict) else {}) or {},
        }
    )


@router.post("/api/character/import/ddb-id")
async def api_character_import_ddb_id(request: Request):
    return await api_character_import_ddb_id_commit(request)


@router.post("/api/character/import/json/preview")
async def api_character_import_json_preview(request: Request):
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
    ddb_payload = payload.get("ddb_json")
    if ddb_payload is None:
        ddb_payload = payload.get("character")
    if ddb_payload is None:
        ddb_payload = payload.get("payload")

    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required")
    if not isinstance(ddb_payload, dict):
        raise HTTPException(status_code=400, detail="ddb_json object is required")

    normalized = normalize_ddb_json_payload(
        _merge_import_resolution(ddb_payload, payload.get("import_resolution"))
    )
    return JSONResponse(_character_import_preview_payload(source="dndbeyond_json", normalized=normalized))


@router.post("/api/character/import/json/commit")
async def api_character_import_json_commit(request: Request):
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

    document = _normalize_commit_document(payload)
    normalized: dict | None = None
    if document is None:
        ddb_payload = payload.get("ddb_json")
        if ddb_payload is None:
            ddb_payload = payload.get("character")
        if ddb_payload is None:
            ddb_payload = payload.get("payload")
        if not isinstance(ddb_payload, dict):
            raise HTTPException(status_code=400, detail="ddb_json object or preview_document is required")
        normalized = normalize_ddb_json_payload(
            _merge_import_resolution(ddb_payload, payload.get("import_resolution"))
        )
        if normalized.get("requires_resolution"):
            return _unresolved_import_response(source="dndbeyond_json", normalized=normalized)
        document = normalized["document"]

    required_choices = _document_import_required_choices(document)
    if required_choices:
        return _unresolved_import_response(
            source="dndbeyond_json",
            normalized={
                "warnings": (document.get("importMeta") or {}).get("warnings") or [],
                "required_choices": required_choices,
            },
        )

    profile = await _persist_imported_document(
        session_id=session_id, auth_user=auth_user, document=document, source="dndbeyond"
    )
    return JSONResponse(
        {
            "ok": True,
            "session_id": session_id,
            "profile": profile,
            "warnings": (normalized or {}).get("warnings") or [],
            "source": "dndbeyond_json",
            "source_type": "dndbeyond",
            "import_review": ((profile.get("importMeta") or {}).get("importReview") if isinstance(profile, dict) else {}) or {},
        }
    )


@router.post("/api/character/import/json")
async def api_character_import_json(request: Request):
    return await api_character_import_json_commit(request)


@router.post("/api/character/import/pdf/preview")
async def api_character_import_pdf_preview(
    request: Request,
    session_id: str = Form(""),
    file: UploadFile = File(...),
):
    auth_user = get_request_user(request)
    if not auth_user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    normalized_session_id = str(session_id or "").strip().upper()
    if not normalized_session_id:
        raise HTTPException(status_code=400, detail="session_id is required")

    content = await file.read()
    parsed_response = parse_character_pdf_response(content)
    try:
        parsed_payload = json.loads((parsed_response.body or b"{}").decode("utf-8"))
    except Exception:
        parsed_payload = {}

    if parsed_response.status_code != 200 or not parsed_payload.get("ok"):
        raise HTTPException(status_code=400, detail=str(parsed_payload.get("error") or "PDF import failed"))

    normalized = normalize_pdf_payload(parsed_payload.get("character"), filename=file.filename or "")
    return JSONResponse(_character_import_preview_payload(source="pdf", normalized=normalized))


@router.post("/api/character/import/pdf/commit")
async def api_character_import_pdf_commit(
    request: Request,
    session_id: str = Form(""),
    preview_document: str = Form(""),
    character_document: str = Form(""),
    file: UploadFile | None = File(None),
):
    auth_user = get_request_user(request)
    if not auth_user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    normalized_session_id = str(session_id or "").strip().upper()
    if not normalized_session_id:
        raise HTTPException(status_code=400, detail="session_id is required")

    document = None
    normalized: dict | None = None
    raw_document = preview_document or character_document
    if raw_document:
        try:
            parsed_document = json.loads(raw_document)
        except Exception:
            raise HTTPException(status_code=400, detail="preview_document must be valid JSON")
        if not isinstance(parsed_document, dict):
            raise HTTPException(status_code=400, detail="preview_document object is required")
        document = normalize_incoming_document(parsed_document)
    elif file is not None:
        content = await file.read()
        parsed_response = parse_character_pdf_response(content)
        try:
            parsed_payload = json.loads((parsed_response.body or b"{}").decode("utf-8"))
        except Exception:
            parsed_payload = {}
        if parsed_response.status_code != 200 or not parsed_payload.get("ok"):
            raise HTTPException(status_code=400, detail=str(parsed_payload.get("error") or "PDF import failed"))
        normalized = normalize_pdf_payload(parsed_payload.get("character"), filename=file.filename or "")
        if normalized.get("requires_resolution"):
            return _unresolved_import_response(source="pdf", normalized=normalized)
        document = normalized["document"]
    else:
        raise HTTPException(status_code=400, detail="file or preview_document is required")

    required_choices = _document_import_required_choices(document)
    if required_choices:
        return _unresolved_import_response(
            source="pdf",
            normalized={
                "warnings": (document.get("importMeta") or {}).get("warnings") or [],
                "required_choices": required_choices,
            },
        )

    profile = await _persist_imported_document(
        session_id=normalized_session_id, auth_user=auth_user, document=document, source="pdf"
    )
    return JSONResponse(
        {
            "ok": True,
            "session_id": normalized_session_id,
            "profile": profile,
            "warnings": (normalized or {}).get("warnings") or [],
            "source": "pdf",
            "source_type": "pdf",
            "import_review": ((profile.get("importMeta") or {}).get("importReview") if isinstance(profile, dict) else {}) or {},
        }
    )


@router.post("/api/character/import/pdf")
async def api_character_import_pdf(
    request: Request,
    session_id: str = Form(""),
    file: UploadFile = File(...),
):
    return await api_character_import_pdf_commit(
        request,
        session_id=session_id,
        preview_document="",
        character_document="",
        file=file,
    )


# ---------------------------------------------------------------------------
# Spell library cache and helpers
# ---------------------------------------------------------------------------

_SPELL_DIR = Path(__file__).resolve().parent.parent / "data" / "rules" / "5e2024" / "spells"

_LEVEL_TO_FILENAME: dict[str, str] = {
    "cantrip": "spells-cantrip.json",
    "0": "spells-cantrip.json",
    "1": "spells-1st.json",
    "2": "spells-2nd.json",
    "3": "spells-3rd.json",
    "4": "spells-4th.json",
    "5": "spells-5th.json",
    "6": "spells-6th.json",
    "7": "spells-7th.json",
    "8": "spells-8th.json",
    "9": "spells-9th.json",
}

# Canonical iteration order for loading all spells; excludes the alias "0"
# so cantrips are only loaded once (via "cantrip").
_ALL_SPELL_LEVEL_KEYS = ["cantrip", "1", "2", "3", "4", "5", "6", "7", "8", "9"]
_spell_cache: dict[str, list[dict]] = {}
_spell_log = logging.getLogger(__name__)


def _load_spells_for_level(level_key: str) -> list[dict]:
    if level_key in _spell_cache:
        return _spell_cache[level_key]
    filename = _LEVEL_TO_FILENAME.get(level_key)
    if not filename:
        return []
    path = _SPELL_DIR / filename
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        spells = data.get("spells") if isinstance(data, dict) else []
        result = spells if isinstance(spells, list) else []
    except FileNotFoundError:
        _spell_log.warning("Spell file not found: %s", path)
        result = []
    except Exception:
        _spell_log.exception("Failed to parse spell file: %s", path)
        result = []
    _spell_cache[level_key] = result
    return result


def _load_all_spells() -> list[dict]:
    all_spells: list[dict] = []
    for key in _ALL_SPELL_LEVEL_KEYS:
        all_spells.extend(_load_spells_for_level(key))
    return all_spells


def _find_profile_by_id(session, profile_id: str) -> dict | None:
    all_profiles = dict(getattr(session, "char_profiles", {}) or {})
    for owner_profiles in all_profiles.values():
        if not isinstance(owner_profiles, list):
            continue
        for item in owner_profiles:
            if not isinstance(item, dict):
                continue
            pid = str(item.get("id") or "").strip()
            if pid == profile_id:
                return item
    return None


def _load_class_data(class_id: str) -> dict:
    if not class_id:
        return {}
    safe_id = class_id.strip().lower().replace(" ", "-")
    path = Path(__file__).resolve().parent.parent / "data" / "rules" / "5e2024" / "classes" / f"{safe_id}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _load_species_data(species_id: str) -> dict:
    if not species_id:
        return {}
    safe_id = species_id.strip().lower().replace(" ", "-")
    path = Path(__file__).resolve().parent.parent / "data" / "rules" / "5e2024" / "species" / f"{safe_id}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# New spell library endpoint
# ---------------------------------------------------------------------------


@router.get("/api/spells")
async def get_spell_library(
    request: Request,
    level: str | None = None,
    school: str | None = None,
    cls: str | None = None,
    search: str | None = None,
    limit: int = 200,
    profile_id: str | None = None,
    session_id: str | None = None,
) -> JSONResponse:
    """Return normalized spell library data, optionally hydrated for a character."""
    rows: list[dict] = []
    if level is not None:
        requested_levels = [l.strip() for l in level.split(",") if l.strip()]
        seen_ids: set[str] = set()
        for lv in requested_levels:
            try:
                lv_num = int(lv)
            except Exception:
                continue
            for spell in list_compendium_spells(level=lv_num, school=school, cls=cls, search=search):
                sid = str(spell.get("id") or "")
                if sid and sid not in seen_ids:
                    seen_ids.add(sid)
                    rows.append(spell)
    else:
        rows = list_compendium_spells(school=school, cls=cls, search=search)

    manifest = None
    if profile_id and session_id:
        session = get_or_restore_session(str(session_id).strip().upper())
        if not session:
            logger.info("[live_state] spell_manifest_request session_id=%s profile_id=%s result=404_session", session_id, profile_id)
            raise HTTPException(status_code=404, detail="Session not found")
        auth_user, session_user, session_user_id, is_session_dm = _resolve_request_principal(request, session)
        try:
            profile = resolve_owned_profile_or_403(
                session, auth_user, profile_id,
                session_user=session_user, session_user_id=session_user_id, is_session_dm=is_session_dm,
            )
        except HTTPException as exc:
            logger.info(
                "[live_state] spell_manifest_request session_id=%s profile_id=%s result=%s authenticated=%s",
                session_id, profile_id, exc.status_code, bool(auth_user) or bool(session_user),
            )
            raise
        logger.info("[live_state] spell_manifest_request session_id=%s profile_id=%s result=200", session_id, profile_id)
        native = profile.get("nativeCharacter") if isinstance(profile.get("nativeCharacter"), dict) else {}
        if native:
            manifest = build_character_spell_manifest(native)
            card_map = {str(card.get("id") or ""): card for card in (manifest.get("cards") or []) if isinstance(card, dict)}
            spell_context = build_multiclass_spell_context(native)
            source_map = spell_context.get("classSourcesBySpell") if isinstance(spell_context.get("classSourcesBySpell"), dict) else {}
            known_set = set((manifest or {}).get("known") or [])
            prepared_set = set((manifest or {}).get("prepared") or [])
            hydrated = []
            highest_available_slot = _safe_int(spell_context.get("highestAvailableSlot"), 0)
            highest_available_slot = max(highest_available_slot, _safe_int((spell_context.get("pactMagic") or {}).get("highestSlotLevel"), 0))
            selection_mode = 'library'
            for row in rows:
                row_id = str(row.get("id") or "")
                if row_id in card_map:
                    hydrated.append({**row, **card_map.get(row_id, {})})
                    continue
                sources = list(source_map.get(row_id) or [])
                primary_source = sources[0] if sources else {}
                accessible = bool(sources)
                hydrated.append({**row, **build_spell_card(row, character_context={
                    "unlockLevel": primary_source.get("unlockLevel") if isinstance(primary_source, dict) else None,
                    "isKnown": row_id in known_set,
                    "isPrepared": row_id in prepared_set,
                    "isAccessible": accessible,
                    "blockedReason": '' if accessible else 'Not unlocked for any current class/level.',
                    "highestAvailableSlot": highest_available_slot,
                    "selectionMode": selection_mode,
                    "sourceClass": str(primary_source.get("classId") or "") if isinstance(primary_source, dict) else "",
                    "sourceSubclass": str(primary_source.get("subclassId") or "") if isinstance(primary_source, dict) else "",
                    "sourceType": str(primary_source.get("sourceType") or "") if isinstance(primary_source, dict) else "",
                    "sourceClasses": sources,
                })})
            rows = hydrated

    capped = min(max(1, limit) if isinstance(limit, int) else 200, 1000)
    total = len(rows)
    return JSONResponse({"spells": rows[:capped], "total": total, "page": 1, "manifest": manifest or {}})


@router.get("/api/spells/{spell_id}")
async def get_spell_detail(spell_id: str, request: Request, profile_id: str | None = None, session_id: str | None = None) -> JSONResponse:
    spell = get_compendium_spell_by_id(spell_id)
    if not spell:
        raise HTTPException(status_code=404, detail="Spell not found")
    card = {}
    manifest = None
    if profile_id and session_id:
        session = get_or_restore_session(str(session_id).strip().upper())
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        auth_user, session_user, session_user_id, is_session_dm = _resolve_request_principal(request, session)
        profile = resolve_owned_profile_or_403(
            session, auth_user, profile_id,
            session_user=session_user, session_user_id=session_user_id, is_session_dm=is_session_dm,
        )
        native = profile.get("nativeCharacter") if isinstance(profile.get("nativeCharacter"), dict) else {}
        if native:
            manifest = build_character_spell_manifest(native)
            card = next((entry for entry in (manifest.get("cards") or []) if str(entry.get("id") or "") == spell_id), {}) or {}
            if not card:
                spell_context = build_multiclass_spell_context(native)
                source_map = spell_context.get("classSourcesBySpell") if isinstance(spell_context.get("classSourcesBySpell"), dict) else {}
                spell_key = str(spell.get("id") or "")
                sources = list(source_map.get(spell_key) or [])
                primary_source = sources[0] if sources else {}
                accessible = bool(sources)
                highest_available_slot = max(_safe_int(spell_context.get("highestAvailableSlot"), 0), _safe_int((spell_context.get("pactMagic") or {}).get("highestSlotLevel"), 0))
                card = build_spell_card(spell, character_context={
                    "unlockLevel": primary_source.get("unlockLevel") if isinstance(primary_source, dict) else None,
                    "isKnown": spell_key in set((manifest or {}).get("known") or []),
                    "isPrepared": spell_key in set((manifest or {}).get("prepared") or []),
                    "isAccessible": accessible,
                    "blockedReason": '' if accessible else 'Not unlocked for any current class/level.',
                    "highestAvailableSlot": highest_available_slot,
                    "selectionMode": 'library',
                    "sourceClass": str(primary_source.get("classId") or "") if isinstance(primary_source, dict) else "",
                    "sourceSubclass": str(primary_source.get("subclassId") or "") if isinstance(primary_source, dict) else "",
                    "sourceType": str(primary_source.get("sourceType") or "") if isinstance(primary_source, dict) else "",
                    "sourceClasses": sources,
                })
    return JSONResponse({"ok": True, "spell": {**spell, **card}, "manifest": manifest or {}})


# ---------------------------------------------------------------------------
# Character sheet endpoint
# ---------------------------------------------------------------------------


@router.get("/api/character/{profile_id}/sheet")
async def get_character_sheet(
    profile_id: str,
    request: Request,
) -> JSONResponse:
    """Return the full character sheet data for a character profile."""
    session_id = str(request.query_params.get("session_id") or "").strip().upper()
    session = get_or_restore_session(session_id) if session_id else None

    profile: dict = {}
    if session:
        auth_user, session_user, session_user_id, is_session_dm = _resolve_request_principal(request, session)
        profile = resolve_owned_profile_or_403(
            session, auth_user, profile_id,
            session_user=session_user, session_user_id=session_user_id, is_session_dm=is_session_dm,
        )

    native = profile.get("nativeCharacter") if isinstance(profile.get("nativeCharacter"), dict) else {}
    runtime = profile.get("nativeRuntime") if isinstance(profile.get("nativeRuntime"), dict) else {}
    classes = native.get("classes") if isinstance(native.get("classes"), list) else []
    primary_class = classes[0] if classes and isinstance(classes[0], dict) else {}
    species_block = native.get("species") if isinstance(native.get("species"), dict) else {}

    class_id = str(primary_class.get("classId") or primary_class.get("id") or primary_class.get("name") or "").strip().lower().replace(" ", "-")
    subclass_id = str(primary_class.get("subclassId") or primary_class.get("subclass") or "").strip().lower().replace(" ", "-")
    species_id = str(species_block.get("id") or species_block.get("name") or "").strip().lower().replace(" ", "-")
    char_level = sum(_safe_int((row or {}).get("level"), default=0, minimum=0, maximum=20) for row in classes if isinstance(row, dict)) or _safe_int(runtime.get("levelTotal"), default=1, minimum=1, maximum=20)

    class_data = get_class_catalog_row(class_id) or _load_class_data(class_id)
    species_data = get_species_catalog_row(species_id) or _load_species_data(species_id)
    subclass_data = get_subclass_catalog_row(subclass_id) if subclass_id else None

    class_name = str((class_data or {}).get("displayName") or primary_class.get("name") or "").strip()
    spellcasting_type = str((class_data or {}).get("spellcastingType") or runtime.get("spellcastingType") or "none").strip() or "none"
    abilities = native.get("abilities") if isinstance(native.get("abilities"), dict) else {}
    ability_scores = abilities.get("scores") if isinstance(abilities.get("scores"), dict) else {}
    feature_payload = build_runtime_feature_payload(
        class_data if isinstance(class_data, dict) else None,
        class_name=class_name or class_id.title(),
        level=char_level,
        subclass_row=subclass_data if isinstance(subclass_data, dict) else None,
        ability_scores=ability_scores if isinstance(ability_scores, dict) else None,
    )

    available_spells: list[dict] = []
    if spellcasting_type != "none" and class_name:
        available_spells = [
            s for s in _load_all_spells()
            if any(str(c).lower() == class_name.lower() for c in (s.get("classes") or []))
        ]

    spell_state = native.get("spellState") if isinstance(native.get("spellState"), dict) else {}

    return JSONResponse(
        {
            "ok": True,
            "profile_id": profile_id,
            "character": {
                "identity": native.get("identity") if isinstance(native.get("identity"), dict) else {},
                "progression": {"level": char_level},
                "classFeatures": feature_payload.get("classFeatures") or [],
                "speciesTraits": (species_data or {}).get("traits") or [],
                "spellcastingType": spellcasting_type,
                "spellState": spell_state,
                "resources": feature_payload.get("resources") or [],
                "summonActions": runtime.get("summonActions") if isinstance(runtime.get("summonActions"), list) else [],
            },
            "classData": class_data or {},
            "speciesData": species_data or {},
            "availableSpells": available_spells[:200],
            "level": char_level,
        }
    )


# ---------------------------------------------------------------------------
# Known/prepared spells endpoints
# ---------------------------------------------------------------------------


@router.get("/api/character/{profile_id}/spells")
async def get_character_spells_manifest(
    profile_id: str,
    request: Request,
) -> JSONResponse:
    """Get the character spell manifest used by the sheet spell manager."""
    session_id = str(request.query_params.get("session_id") or "").strip().upper()
    session = get_or_restore_session(session_id) if session_id else None

    profile: dict = {}
    if session:
        auth_user, session_user, session_user_id, is_session_dm = _resolve_request_principal(request, session)
        profile = resolve_owned_profile_or_403(
            session, auth_user, profile_id,
            session_user=session_user, session_user_id=session_user_id, is_session_dm=is_session_dm,
        )

    native = profile.get("nativeCharacter") if isinstance(profile.get("nativeCharacter"), dict) else {}
    if native:
        classes = native.get("classes") if isinstance(native.get("classes"), list) else []
        primary_class = classes[0] if classes and isinstance(classes[0], dict) else {}
        class_id = str(primary_class.get("classId") or primary_class.get("id") or primary_class.get("name") or "").strip().lower()
        class_level = _safe_int(primary_class.get("level"), default=1, minimum=1, maximum=20)
        if class_id:
            repair_spell_state_for_document(
                native,
                class_id=class_id,
                class_level=class_level,
                abilities=native.get("abilities") if isinstance(native.get("abilities"), dict) else {},
                subclass_id=str(primary_class.get("subclassId") or "").strip().lower(),
            )
    spell_state = native.get("spellState") if isinstance(native.get("spellState"), dict) else {}
    manifest = build_character_spell_manifest(native) if native else {"known": spell_state.get("known") or [], "prepared": spell_state.get("prepared") or [], "cards": []}

    return JSONResponse(
        {
            "ok": True,
            "profile_id": profile_id,
            "known": manifest.get("known") or spell_state.get("known") or [],
            "prepared": manifest.get("prepared") or spell_state.get("prepared") or [],
            "slots": spell_state.get("slots") or {},
            "rituals": spell_state.get("rituals") or [],
            "limits": manifest.get("limits") or {},
            "validation": manifest.get("validation") or {},
            "cards": manifest.get("cards") or [],
        }
    )


@router.get("/api/character/{profile_id}/spells/known")
async def get_character_spells_known(
    profile_id: str,
    request: Request,
) -> JSONResponse:
    """Get the character's known/prepared spells."""
    session_id = str(request.query_params.get("session_id") or "").strip().upper()
    session = get_or_restore_session(session_id) if session_id else None

    profile: dict = {}
    if session:
        auth_user, session_user, session_user_id, is_session_dm = _resolve_request_principal(request, session)
        profile = resolve_owned_profile_or_403(
            session, auth_user, profile_id,
            session_user=session_user, session_user_id=session_user_id, is_session_dm=is_session_dm,
        )

    native = profile.get("nativeCharacter") if isinstance(profile.get("nativeCharacter"), dict) else {}
    if native:
        classes = native.get("classes") if isinstance(native.get("classes"), list) else []
        primary_class = classes[0] if classes and isinstance(classes[0], dict) else {}
        class_id = str(primary_class.get("classId") or primary_class.get("id") or primary_class.get("name") or "").strip().lower()
        class_level = _safe_int(primary_class.get("level"), default=1, minimum=1, maximum=20)
        if class_id:
            repair_spell_state_for_document(
                native,
                class_id=class_id,
                class_level=class_level,
                abilities=native.get("abilities") if isinstance(native.get("abilities"), dict) else {},
                subclass_id=str(primary_class.get("subclassId") or "").strip().lower(),
            )
    spell_state = native.get("spellState") if isinstance(native.get("spellState"), dict) else {}
    manifest = build_character_spell_manifest(native) if native else {"known": spell_state.get("known") or [], "prepared": spell_state.get("prepared") or [], "cards": []}

    return JSONResponse(
        {
            "ok": True,
            "profile_id": profile_id,
            "known": manifest.get("known") or spell_state.get("known") or [],
            "prepared": manifest.get("prepared") or spell_state.get("prepared") or [],
            "slots": spell_state.get("slots") or {},
            "rituals": spell_state.get("rituals") or [],
            "limits": manifest.get("limits") or {},
            "validation": manifest.get("validation") or {},
            "cards": manifest.get("cards") or [],
        }
    )


@router.post("/api/character/{profile_id}/spells/known")
async def update_character_spells_known(
    profile_id: str,
    request: Request,
) -> JSONResponse:
    auth_user = get_request_user(request)
    if not auth_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    known = payload.get("known")
    if not isinstance(known, list):
        raise HTTPException(status_code=400, detail="'known' list is required")
    session_id = str(payload.get("session_id") or payload.get("sessionId") or "").strip().upper()
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required")
    session = get_or_restore_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    profile = resolve_owned_profile_or_403(session, auth_user, profile_id)
    native = profile.get("nativeCharacter") if isinstance(profile.get("nativeCharacter"), dict) else {}
    if not native:
        raise HTTPException(status_code=400, detail="Character does not have a native document")
    spell_state = native.get("spellState") if isinstance(native.get("spellState"), dict) else {}
    if not spell_state:
        from server.character.schema import default_character_document
        default_spell_state = dict(default_character_document().get("spellState") or {})
        native["spellState"] = default_spell_state
        spell_state = native["spellState"]
    primary_class = ((native.get("classes") if isinstance(native.get("classes"), list) else []) or [{}])[0]
    class_id = str(primary_class.get("classId") or primary_class.get("id") or primary_class.get("name") or "").strip().lower()
    class_level = _safe_int(primary_class.get("level"), 1)
    validation = validate_spell_selection(
        class_id=class_id,
        class_level=class_level,
        abilities=native.get("abilities") if isinstance(native.get("abilities"), dict) else {},
        known=known,
        prepared=list(spell_state.get("prepared") or []),
        document=native,
        subclass_id=str(primary_class.get("subclassId") or "").strip().lower(),
    )
    if not validation.get("ok"):
        raise HTTPException(status_code=400, detail={"errors": validation.get("errors") or [], "limits": validation.get("limits") or {}})
    spell_state["known"] = validation.get("known") or []
    spell_state["prepared"] = [spell_id for spell_id in (spell_state.get("prepared") or []) if spell_id in set(validation.get("known") or [])] if validation.get("limits", {}).get("preparedLimit") is None else list(spell_state.get("prepared") or [])
    _sync_native_spellbook_entries(native, known_ids=list(spell_state.get("known") or []), prepared_ids=list(spell_state.get("prepared") or []))
    await save_campaign_async(session)
    return JSONResponse({"ok": True, "success": True, "profile_id": profile_id, "known": spell_state.get("known") or [], "validation": validation, "limits": validation.get("limits") or {}})


@router.post("/api/character/{profile_id}/spells/prepare")
async def update_character_spells_prepared(
    profile_id: str,
    request: Request,
) -> JSONResponse:
    """Update the character's prepared spells list."""
    auth_user = get_request_user(request)
    if not auth_user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    prepared = payload.get("prepared")
    if not isinstance(prepared, list):
        raise HTTPException(status_code=400, detail="'prepared' list is required")

    session_id = str(payload.get("session_id") or payload.get("sessionId") or "").strip().upper()
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required")

    session = get_or_restore_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    profile = resolve_owned_profile_or_403(session, auth_user, profile_id)
    native = profile.get("nativeCharacter") if isinstance(profile.get("nativeCharacter"), dict) else {}
    if not native:
        raise HTTPException(status_code=400, detail="Character does not have a native document")

    existing_spell_state = native.get("spellState") if isinstance(native.get("spellState"), dict) else None
    spell_state = existing_spell_state if existing_spell_state is not None else {}
    if not spell_state:
        from server.character.schema import default_character_document
        spell_state = dict(default_character_document().get("spellState") or {})
    current_known = list(spell_state.get("known") or [])
    primary_class = ((native.get("classes") if isinstance(native.get("classes"), list) else []) or [{}])[0]
    class_id = str(primary_class.get("classId") or primary_class.get("id") or primary_class.get("name") or "").strip().lower()
    class_level = _safe_int(primary_class.get("level"), 1)
    validation = validate_spell_selection(
        class_id=class_id,
        class_level=class_level,
        abilities=native.get("abilities") if isinstance(native.get("abilities"), dict) else {},
        known=current_known,
        prepared=prepared,
        document=native,
        subclass_id=str(primary_class.get("subclassId") or "").strip().lower(),
    )
    if not validation.get("ok"):
        raise HTTPException(status_code=400, detail={"errors": validation.get("errors") or [], "limits": validation.get("limits") or {}})
    if existing_spell_state is None or native.get("spellState") is not spell_state:
        native["spellState"] = spell_state
    spell_state["prepared"] = validation.get("prepared") or []
    _sync_native_spellbook_entries(native, known_ids=list(spell_state.get("known") or []), prepared_ids=list(spell_state.get("prepared") or []))

    await save_campaign_async(session)

    return JSONResponse(
        {
            "ok": True,
            "success": True,
            "profile_id": profile_id,
            "prepared": spell_state.get("prepared") or [],
            "limits": validation.get("limits") or {},
            "validation": validation,
        }
    )
