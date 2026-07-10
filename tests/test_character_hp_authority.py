import asyncio

from server.character.export_mapper import map_character_to_charsheet
from server.character.schema import default_character_document
from server.character.service import build_profile_upsert_payload, resolve_runtime
from server.handlers import tokens as token_handlers
from server.session import Session, User, create_token, normalize_profile_owner_key
from server.sessions.service import _sync_player_state_from_profile


def _build_document(*, class_id: str, level: int, con: int = 14) -> dict:
    doc = default_character_document()
    doc["classes"] = [{"classId": class_id, "name": class_id.title(), "level": level}]
    abilities = doc.get("abilities") if isinstance(doc.get("abilities"), dict) else {}
    scores = abilities.get("scores") if isinstance(abilities.get("scores"), dict) else {}
    scores["con"] = con
    abilities["scores"] = scores
    doc["abilities"] = abilities
    return doc


def test_class_aware_max_hp_derivation_fighter_vs_wizard():
    fighter_doc = _build_document(class_id="fighter", level=5, con=14)
    wizard_doc = _build_document(class_id="wizard", level=5, con=14)

    fighter_runtime = resolve_runtime(fighter_doc)["runtime"]
    wizard_runtime = resolve_runtime(wizard_doc)["runtime"]

    assert fighter_runtime["hp"]["max"] > wizard_runtime["hp"]["max"]


def test_persisted_current_hp_survives_profile_resolve_and_save():
    doc = _build_document(class_id="fighter", level=5, con=14)
    payload = build_profile_upsert_payload(
        doc,
        profile_id="fighter-1",
        persisted_runtime={"hp": {"max": 44, "current": 17, "temp": 0}},
    )

    hp = payload["nativeRuntime"]["hp"]
    assert hp["current"] == 17
    assert hp["max"] >= hp["current"]


def test_persisted_temp_hp_survives_profile_resolve_and_save():
    doc = _build_document(class_id="fighter", level=5, con=14)
    payload = build_profile_upsert_payload(
        doc,
        profile_id="fighter-1",
        persisted_runtime={"hp": {"max": 44, "current": 22, "temp": 9}},
    )

    hp = payload["nativeRuntime"]["hp"]
    assert hp["temp"] == 9


def test_full_hp_levelup_stays_full_at_new_max():
    level5_doc = _build_document(class_id="fighter", level=5, con=14)
    old_runtime = resolve_runtime(level5_doc)["runtime"]
    old_max = int(old_runtime["hp"]["max"])

    level6_doc = _build_document(class_id="fighter", level=6, con=14)
    payload = build_profile_upsert_payload(
        level6_doc,
        profile_id="fighter-1",
        persisted_runtime={"hp": {"max": old_max, "current": old_max, "temp": 0}},
    )

    hp = payload["nativeRuntime"]["hp"]
    assert hp["max"] > old_max
    assert hp["current"] == hp["max"]


def test_token_hp_update_persists_to_profile_and_rejoin_sync(monkeypatch):
    async def _noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(token_handlers, "save_campaign_async", _noop)
    monkeypatch.setattr(token_handlers, "_broadcast_token_event", _noop)
    monkeypatch.setattr(token_handlers, "_broadcast_combat", _noop)
    monkeypatch.setattr(token_handlers, "_sync_combatant_token_state", lambda *_args, **_kwargs: False)

    session = Session(id="hp-authority")
    user = User(id="player-1", name="Aster", role="player")
    session.users[user.id] = user

    owner_key = normalize_profile_owner_key(user.name)
    session.char_profiles = {
        owner_key: [
            {
                "id": "profile-1",
                "name": "Aster",
                "nativeRuntime": {"hp": {"max": 30, "current": 18, "temp": 4}},
                "charBook": {"maxHp": 30, "currentHp": 18, "tempHp": 4},
                "charSheet": {"hp": {"max": 30, "current": 18, "temp": 4}},
            }
        ]
    }
    session.active_char_profiles = {user.id: "profile-1"}

    token = create_token(session, "dm", "Aster", 0, 0, owner_id=user.id, hp=18, max_hp=30, temp_hp=4)
    asyncio.run(token_handlers.handle_token_hp_update({"token_id": token.id, "hp": 11}, session, user))

    profile = session.char_profiles[owner_key][0]
    assert profile["nativeRuntime"]["hp"]["current"] == 11

    # Rejoin/claim must never overwrite live token HP: the token is the
    # runtime authority for current HP; the profile only supplies max HP.
    _sync_player_state_from_profile(session, user, profile)
    assert token.hp == 11

    # Even a stale profile copy (sheet still at full HP) must not undo damage.
    stale = dict(profile)
    stale["nativeRuntime"] = {"hp": {"max": 30, "current": 30, "temp": 0}}
    _sync_player_state_from_profile(session, user, stale)
    assert token.hp == 11
    assert token.max_hp == 30

    # Rejoin sync now ships stubs — the updated vitals ride the stub scalars,
    # and the full body (with nativeRuntime) is served by char_profile_fetch.
    state = session.to_state_dict_for_role("player", user.id)
    stub = state["char_profiles"][0]
    assert stub["stub"] is True
    assert stub["curhp"] == 11
    assert stub["hp"] == 30
    assert "nativeRuntime" not in stub


def test_legacy_export_charsheet_hp_uses_runtime_hp_without_regression():
    doc = _build_document(class_id="wizard", level=4, con=12)
    runtime = resolve_runtime(doc)["runtime"]
    runtime["hp"] = {"max": 21, "current": 13, "temp": 2, "hitDice": []}

    sheet = map_character_to_charsheet(doc, runtime)
    assert sheet["hp"]["max"] == 21
    assert sheet["hp"]["current"] == 13
    assert sheet["hp"]["temp"] == 2


def test_level4_sorcerer_con12_resolves_to_22_hp():
    doc = _build_document(class_id="sorcerer", level=4, con=12)
    runtime = resolve_runtime(doc)["runtime"]

    assert runtime["hp"]["max"] == 22
    assert runtime["hp"]["current"] == 22


def test_damaged_hp_stays_damaged_across_levelup_and_clamps_to_new_max():
    level4_doc = _build_document(class_id="sorcerer", level=4, con=12)
    level5_doc = _build_document(class_id="sorcerer", level=5, con=12)

    old_max = resolve_runtime(level4_doc)["runtime"]["hp"]["max"]
    payload = build_profile_upsert_payload(
        level5_doc,
        profile_id="sorc-1",
        persisted_runtime={"hp": {"max": old_max, "current": 15, "temp": 3}},
    )

    hp = payload["nativeRuntime"]["hp"]
    assert hp["max"] > old_max
    assert hp["current"] == 15
    assert hp["current"] <= hp["max"]
    assert hp["temp"] == 3
