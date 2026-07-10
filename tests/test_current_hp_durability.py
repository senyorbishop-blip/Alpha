"""Current HP durability contract.

One rule everywhere a token is rehydrated from a character profile (login,
claim, profile edit/save, reconnect, restart): profile edits may change max
HP, but current HP/damage only changes through game actions. The live session
token is authoritative for current/temp HP; the sheet supplies max HP and
derived stats. If max HP drops below current, current clamps to the new max.
"""
import tempfile
import time

from server.handlers.common import _apply_damage, _persist_token_hp_to_owned_profiles, _sync_combatant_token_state
from server.handlers.content import _apply_profile_max_hp_to_linked_tokens
from server.session import Session, User, create_token, normalize_profile_owner_key
from server.sessions.service import _sync_player_state_from_profile

from tests.test_persistence_schema import _build_session, _cleanup_modules, _reload_db_modules


def _profile_row(profile_id: str, *, max_hp: int, cur_hp: int, temp_hp: int = 0, name: str = "Bishop") -> dict:
    return {
        "id": profile_id,
        "name": name,
        "nativeRuntime": {"hp": {"max": max_hp, "current": cur_hp, "temp": temp_hp}},
        "charBook": {"maxHp": max_hp, "currentHp": cur_hp, "tempHp": temp_hp},
        "charSheet": {"hp": {"max": max_hp, "current": cur_hp, "temp": temp_hp}},
    }


def _session_with_damaged_token(*, hp: int = 63, max_hp: int = 103, profile_cur: int | None = None):
    """A live session where the token took damage but the sheet still shows full HP."""
    session = Session(id="hp-durability")
    user = User(id="player-1", name="Bishop", role="player")
    session.users[user.id] = user
    owner_key = normalize_profile_owner_key(user.name)
    profile = _profile_row("profile-1", max_hp=max_hp, cur_hp=profile_cur if profile_cur is not None else max_hp)
    session.char_profiles = {owner_key: [profile]}
    session.active_char_profiles = {user.id: "profile-1"}
    token = create_token(
        session, "dm", "Bishop", 0, 0,
        owner_id=user.id, hp=hp, max_hp=max_hp, profile_id="profile-1",
    )
    return session, user, token, profile


def test_claim_after_damage_keeps_live_token_hp():
    # Chat fireball: 103 -> 63 via a path that never touched the profile.
    session, user, token, profile = _session_with_damaged_token(hp=103, max_hp=103)
    _apply_damage(token, 40)
    assert token.hp == 63

    # Player logs in / claims the character while the sheet still says 103.
    _sync_player_state_from_profile(session, user, profile)
    assert token.hp == 63
    assert token.max_hp == 103


def test_claim_seeds_hp_from_profile_only_when_token_has_none():
    session, user, token, profile = _session_with_damaged_token(hp=103, max_hp=103, profile_cur=48)
    token.hp = None
    _sync_player_state_from_profile(session, user, profile)
    assert token.hp == 48
    assert token.max_hp == 103


def test_damage_paths_refresh_profile_hp_mirror():
    # Every HP mutation site calls _sync_combatant_token_state; it must keep
    # the owner's sheet mirror current so nothing stale can ever be re-applied.
    session, user, token, profile = _session_with_damaged_token(hp=103, max_hp=103)
    _apply_damage(token, 40)
    _sync_combatant_token_state(session, token)

    owner_key = normalize_profile_owner_key(user.name)
    row = session.char_profiles[owner_key][0]
    assert row["nativeRuntime"]["hp"]["current"] == 63
    assert row["charBook"]["currentHp"] == 63
    assert row["charSheet"]["hp"]["current"] == 63
    assert row["curhp"] == 63


def test_profile_mirror_never_writes_hp_for_hpless_tokens():
    session, user, token, profile = _session_with_damaged_token(hp=103, max_hp=103, profile_cur=77)
    token.hp = None
    assert _persist_token_hp_to_owned_profiles(session, token) is False
    owner_key = normalize_profile_owner_key(user.name)
    assert session.char_profiles[owner_key][0]["nativeRuntime"]["hp"]["current"] == 77


def test_sheet_edit_raising_max_hp_keeps_current_hp():
    session, user, token, _ = _session_with_damaged_token(hp=63, max_hp=103)
    edited = _profile_row("profile-1", max_hp=110, cur_hp=110)

    _apply_profile_max_hp_to_linked_tokens(session, edited, owner_id=user.id)
    assert token.max_hp == 110
    assert token.hp == 63

    # The rejoin path must agree with the edit path.
    _sync_player_state_from_profile(session, user, edited)
    assert token.max_hp == 110
    assert token.hp == 63


def test_sheet_edit_lowering_max_hp_clamps_current_hp():
    session, user, token, _ = _session_with_damaged_token(hp=63, max_hp=103)
    edited = _profile_row("profile-1", max_hp=40, cur_hp=40)

    _apply_profile_max_hp_to_linked_tokens(session, edited, owner_id=user.id)
    assert token.max_hp == 40
    assert token.hp == 40

    session2, user2, token2, _ = _session_with_damaged_token(hp=63, max_hp=103)
    _sync_player_state_from_profile(session2, user2, edited)
    assert token2.max_hp == 40
    assert token2.hp == 40


def test_server_restart_preserves_current_hp_and_survives_first_rejoin():
    with tempfile.TemporaryDirectory() as tmpdir:
        paths_mod, db_mod = _reload_db_modules(tmpdir)
        try:
            from server.restore import restore_session_from_db

            session = _build_session()
            player = User(id="player-1", name="Bishop", role="player")
            session.users[player.id] = player
            owner_key = normalize_profile_owner_key(player.name)
            # Sheet is stale at full HP: the fireball path missed the mirror.
            session.char_profiles = {owner_key: [_profile_row("profile-1", max_hp=103, cur_hp=103)]}
            session.active_char_profiles = {player.id: "profile-1"}
            token = create_token(
                session, "dm1", "Bishop", 5, 5,
                owner_id=player.id, hp=103, max_hp=103, profile_id="profile-1",
            )
            _apply_damage(token, 40)
            assert token.hp == 63

            assert db_mod.save_campaign(session) is True
            loaded = db_mod.load_campaign(session.id)
            restored, _ = restore_session_from_db(loaded)

            restored_token = restored.tokens[token.id]
            assert restored_token.hp == 63
            assert restored_token.max_hp == 103

            # First login after the restart re-syncs from the profile —
            # it must not resurrect the token from the stale sheet.
            restored_player = restored.users[player.id]
            profile = restored.char_profiles[owner_key][0]
            _sync_player_state_from_profile(restored, restored_player, profile)
            assert restored.tokens[token.id].hp == 63
        finally:
            _cleanup_modules(paths_mod, db_mod)


def test_restart_mid_combat_preserves_initiative_conditions_and_death_saves():
    with tempfile.TemporaryDirectory() as tmpdir:
        paths_mod, db_mod = _reload_db_modules(tmpdir)
        try:
            from server.restore import restore_session_from_db

            session = _build_session()
            player = User(id="player-1", name="Bishop", role="player")
            session.users[player.id] = player
            # Condition timers persist as {condition: expiry_epoch}; expired
            # entries are pruned on load, so pick a timestamp in the future.
            poison_expiry = time.time() + 3600.0
            token = create_token(
                session, "dm1", "Bishop", 5, 5,
                owner_id=player.id, hp=0, max_hp=103,
                conditions=["poisoned", "prone"],
                condition_timers={"poisoned": poison_expiry},
                initiative_mod=2,
            )
            session.combat = {
                "active": True,
                "turn": 1,
                "round": 4,
                "combatants": [
                    {"id": "c1", "token_id": token.id, "name": "Bishop", "initiative": 17,
                     "hp": 0, "max_hp": 103,
                     "death_saves": {"successes": 1, "fails": 2, "stable": False, "dead": False}},
                    {"id": "c2", "token_id": "goblin-1", "name": "Goblin", "initiative": 9},
                ],
            }

            assert db_mod.save_campaign(session) is True
            loaded = db_mod.load_campaign(session.id)
            restored, _ = restore_session_from_db(loaded)

            restored_token = restored.tokens[token.id]
            assert restored_token.hp == 0
            assert restored_token.conditions == ["poisoned", "prone"]
            assert restored_token.condition_timers == {"poisoned": poison_expiry}
            assert restored_token.initiative_mod == 2

            combat = restored.combat
            assert combat["active"] is True
            assert combat["round"] == 4
            hero = next(c for c in combat["combatants"] if c.get("token_id") == token.id)
            assert hero["initiative"] == 17
            assert hero["death_saves"] == {"successes": 1, "fails": 2, "stable": False, "dead": False}
        finally:
            _cleanup_modules(paths_mod, db_mod)
