from server.session import (
    Session,
    Token,
    User,
    build_token_runtime_payload,
    normalize_profile_owner_key,
    resolve_token_character_profile,
)


def _profile(profile_id="prof-1", *, name="Aria", hp_max=24, hp_current=24, ac=16, speed=35):
    return {
        "id": profile_id,
        "name": name,
        "classSummary": "Ranger (Hunter)",
        "nativeCharacter": {
            "identity": {
                "name": name,
                "portraitUrl": "/portraits/aria.png",
                "tokenImageUrl": "/tokens/aria.webp",
                "characterId": "ddb-123",
            },
            "species": {"id": "elf", "name": "Elf"},
            "classes": [{"classId": "ranger", "name": "Ranger", "subclassName": "Hunter"}],
            "actions": [{"id": "longbow", "name": "Longbow"}],
            "spells": [{"id": "hunters-mark", "name": "Hunter's Mark"}],
        },
        "nativeRuntime": {
            "hp": {"max": hp_max, "current": hp_current, "temp": 2},
            "combat": {"ac": ac, "speed": speed, "initiative": 3},
            "speed": {"walk": speed},
        },
        "charSheet": {"level": 5},
    }


def test_resolve_token_character_profile_by_profile_id_and_runtime_merge_preserves_token_state():
    session = Session(id="sess")
    user = User(id="u1", name="Aria Player", role="player")
    session.users[user.id] = user
    owner_key = normalize_profile_owner_key(user.name)
    session.char_profiles = {owner_key: [_profile()]}
    token = Token(
        id="tok1",
        name="Old Token Name",
        x=10,
        y=20,
        width=50,
        height=50,
        color="#abc",
        shape="circle",
        owner_id=user.id,
        profile_id="prof-1",
        hp=7,
        max_hp=12,
        temp_hp=1,
        ac=11,
        speed=20,
        conditions=["poisoned"],
        map_context="dungeon-1",
    )
    session.tokens[token.id] = token

    resolved = resolve_token_character_profile(session, token)
    assert resolved["id"] == "prof-1"
    payload = build_token_runtime_payload(session, token)

    assert payload["profile_id"] == "prof-1"
    assert payload["characterId"] == "ddb-123"
    assert payload["name"] == "Aria"
    assert payload["image_url"] == "/tokens/aria.webp"
    assert payload["classSummary"] == "Ranger (Hunter)"
    assert payload["class_id"] == "ranger"
    assert payload["species_name"] == "Elf"
    assert payload["level"] == 5
    assert payload["maxHp"] == 24
    assert payload["hp"] == 7
    assert payload["tempHp"] == 1
    assert payload["ac"] == 16
    assert payload["speed"] == 35
    assert payload["conditions"] == ["poisoned"]
    assert payload["x"] == 10
    assert payload["y"] == 20
    assert payload["map_context"] == "dungeon-1"

    # Full action/spell collections stay on the linked profile and are fetched
    # through the character/profile path; token broadcasts carry only the slim
    # identity/combat summary above.
    assert resolved["nativeCharacter"]["actions"][0]["id"] == "longbow"
    assert resolved["nativeCharacter"]["spells"][0]["id"] == "hunters-mark"
    assert "actions" not in payload
    assert "spells" not in payload


def test_active_profile_update_changes_token_display_without_moving_or_resetting_hp():
    session = Session(id="sess")
    user = User(id="u1", name="Aria Player", role="player")
    session.users[user.id] = user
    session.active_char_profiles = {user.id: "prof-1"}
    owner_key = normalize_profile_owner_key(user.name)
    session.char_profiles = {owner_key: [_profile(name="Aria", hp_max=24, hp_current=24)]}
    token = Token(
        id="tok1",
        name="Aria",
        x=100,
        y=125,
        width=50,
        height=50,
        color="#abc",
        shape="circle",
        owner_id=user.id,
        hp=5,
        max_hp=24,
        map_context="world",
    )

    first = build_token_runtime_payload(session, token)
    session.char_profiles[owner_key][0] = _profile(name="Aria the Swift", hp_max=31, hp_current=31, ac=18, speed=40)
    second = build_token_runtime_payload(session, token)

    assert first["name"] == "Aria"
    assert second["name"] == "Aria the Swift"
    assert second["maxHp"] == 31
    assert second["hp"] == 5
    assert second["ac"] == 18
    assert second["speed"] == 40
    assert second["x"] == 100
    assert second["y"] == 125
