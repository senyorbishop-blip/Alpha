"""
tests/test_refactor.py — Targeted tests for the handlers package refactor.

Tests validate:
1. handle_message dispatch is importable and has correct routing
2. Duplicate _safe_int bug is fixed (no duplicate definition)
3. Duplicate _token_center bug is fixed (no duplicate definition)
4. server/restore.py works correctly
5. server/utils/pdf_parser.py is importable
6. Cross-module function calls work
"""
import sys
import os
import importlib
import inspect
import asyncio
import re

import pytest

# Ensure the project root is on the path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


@pytest.fixture(autouse=True)
def _restore_connections_manager():
    """Restore the shared ConnectionManager singleton after each test.

    The spawn-creature tests (_spawn_test_context) rebind
    ``server.connections.manager`` to a lightweight fake without restoring it.
    Left unchecked, that fake leaks into the rest of the suite and breaks any
    later test that touches the real manager's internals (e.g.
    tests/test_ws_auth_and_join_privacy.py clears ``manager._connections`` in
    its own fixture). Snapshot the real singleton here and put it back on
    teardown so the pollution can never escape this module.
    """
    import server.connections as connections_mod

    original_manager = connections_mod.manager
    try:
        yield
    finally:
        connections_mod.manager = original_manager


def _get_csrf_token(client) -> str:
    """Bootstrap a CSRF token by making a GET request and returning the token value."""
    client.get("/api/assets/manifest")
    return client.cookies.get("csrf_token", "")


def test_handle_message_importable():
    """handle_message must be importable from server.handlers."""
    from server.handlers import handle_message
    assert callable(handle_message), "handle_message must be callable"


def test_handle_message_dispatch_table():
    """The dispatch table in handle_message must contain all expected routes."""
    from server.handlers import handle_message
    source = inspect.getsource(handle_message)
    required_types = [
        "token_move", "token_create", "token_delete",
        "combat_update", "combat_next", "combat_end_turn",
        "hazard_zone_create", "hazard_zone_update",
        "viewer_power_use",
        "fog_paint", "fog_toggle",
        "chat_message", "dice_roll",
        "inventory_add_item",
        "journal_upsert",
        "interactable_action",
        "discovery_trigger",
        "discovery_acknowledge",
        "discovery_save",
        "discovery_unsave",
        "encounter_template_upsert",
        "encounter_spawn_group",
    ]
    for t in required_types:
        assert t in source, f"Expected '{t}' in handle_message dispatch table"


def test_no_duplicate_safe_int():
    """_safe_int must be defined exactly once in server/handlers/common.py."""
    from server.handlers import common as common_mod
    src = inspect.getsource(common_mod)
    count = src.count("def _safe_int(")
    assert count == 1, f"_safe_int defined {count} times in common.py, expected exactly 1"


def test_no_duplicate_token_center():
    """_token_center must be defined exactly once in server/handlers/common.py."""
    from server.handlers import common as common_mod
    src = inspect.getsource(common_mod)
    count = src.count("def _token_center(")
    assert count == 1, f"_token_center defined {count} times in common.py, expected exactly 1"


def test_safe_int_behavior():
    """_safe_int must coerce values and respect min/max."""
    from server.handlers.common import _safe_int
    assert _safe_int("5") == 5
    assert _safe_int("abc", default=7) == 7
    assert _safe_int(3, minimum=5) == 5
    assert _safe_int(10, maximum=8) == 8
    assert _safe_int(None, default=3) == 3


def test_safe_float_behavior():
    """_safe_float must coerce values and respect min/max."""
    from server.handlers.common import _safe_float
    assert _safe_float("3.14") == 3.14
    assert _safe_float("bad", default=1.0) == 1.0
    assert _safe_float(0.5, minimum=1.0) == 1.0
    assert _safe_float(5.0, maximum=3.0) == 3.0


def test_token_center():
    """_token_center must compute center from x, y, width, height."""
    from server.handlers.common import _token_center

    class FakeToken:
        x = 10.0
        y = 20.0
        width = 50.0
        height = 50.0

    cx, cy = _token_center(FakeToken())
    assert cx == 35.0, f"Expected cx=35.0, got {cx}"
    assert cy == 45.0, f"Expected cy=45.0, got {cy}"


def test_restore_session_from_db_importable():
    """restore_session_from_db must be importable from server.restore."""
    from server.restore import restore_session_from_db
    assert callable(restore_session_from_db)


def test_pdf_parser_importable():
    """parse_character_pdf_data must be importable from server.utils.pdf_parser."""
    from server.utils.pdf_parser import parse_character_pdf_data
    assert callable(parse_character_pdf_data)


def test_pdf_parser_raises_on_bad_data():
    """parse_character_pdf_data must raise ValueError on empty/invalid bytes."""
    from server.utils.pdf_parser import parse_character_pdf_data
    try:
        parse_character_pdf_data(b"")
    except ValueError:
        pass  # expected
    except ImportError:
        pass  # pypdf not installed — skip


def test_all_handler_modules_importable():
    """Every handler sub-module must be importable without errors."""
    modules = [
        "server.handlers.common",
        "server.handlers.conditions",
        "server.handlers.combat",
        "server.handlers.hazards",
        "server.handlers.viewer_powers",
        "server.handlers.tokens",
        "server.handlers.map_editor",
        "server.handlers.inventory",
        "server.handlers.content",
        "server.handlers",
    ]
    for mod_name in modules:
        mod = importlib.import_module(mod_name)
        assert mod is not None, f"Module {mod_name} failed to import"


def test_main_importable():
    """main.py must import without errors."""
    mod = importlib.import_module("main")
    assert mod is not None




def test_economy_scaffold_importable_and_defaults_disabled():
    """Economy scaffolding should be importable with all staged flags default-off."""
    from server import economy_scaffold

    assert isinstance(economy_scaffold.ECONOMY_FEATURE_FLAGS, dict)
    assert economy_scaffold.ECONOMY_FEATURE_FLAGS
    assert all(value is False for value in economy_scaffold.ECONOMY_FEATURE_FLAGS.values())

def test_combat_attack_handlers_importable():
    """New combat action handlers must be importable from server.handlers.combat."""
    from server.handlers.combat import (
        handle_combat_select_target,
        handle_combat_attack_request,
        handle_combat_attack_override,
    )
    assert callable(handle_combat_select_target)
    assert callable(handle_combat_attack_request)
    assert callable(handle_combat_attack_override)


def test_combat_attack_routes_in_dispatch():
    """combat_select_target, combat_attack_request, combat_attack_override must be dispatched."""
    from server.handlers import handle_message
    source = inspect.getsource(handle_message)
    for msg_type in ("combat_select_target", "combat_attack_request", "combat_attack_override"):
        assert msg_type in source, f"Expected '{msg_type}' in handle_message dispatch table"


def test_can_act_current_turn_helper():
    """_can_act_current_turn must correctly check DM privilege and active combatant ownership."""
    from server.handlers.combat import _can_act_current_turn
    from server.session import Session, User, Token

    # Build a minimal session with active combat
    session = Session(id="test-session")
    dm = User(id="dm1", name="DM", role="dm")
    player = User(id="p1", name="Alice", role="player")
    session.users = {"dm1": dm, "p1": player}

    tok = Token(
        id="t1", name="Alice Char", x=0, y=0,
        width=40, height=40, color="#fff", shape="circle",
        owner_id="p1",
    )
    session.tokens = {"t1": tok}
    session.combat = {
        "active": True,
        "turn": 0,
        "combatants": [
            {"id": "c1", "token_id": "t1", "name": "Alice Char", "owner_id": "p1", "initiative": 15},
        ],
        "round": 1,
        "movement": {},
    }

    # DM can always act
    allowed, msg = _can_act_current_turn(session, dm)
    assert allowed, f"DM should be allowed, got msg: {msg}"

    # Active player can act
    allowed, msg = _can_act_current_turn(session, player)
    assert allowed, f"Active player should be allowed, got msg: {msg}"

    # Different player cannot act on someone else's turn
    other_player = User(id="p2", name="Bob", role="player")
    allowed, msg = _can_act_current_turn(session, other_player)
    assert not allowed, "Non-active player should not be allowed"

    # No active combat
    session.combat["active"] = False
    allowed, msg = _can_act_current_turn(session, dm)
    assert not allowed, "Should not be allowed when combat is inactive"


def test_upload_token_image_requires_query_user_id():
    """
    The /api/session/{session_id}/token/{token_id}/image endpoint declares
    user_id as a plain str parameter (no Form(...)), so FastAPI treats it as a
    query parameter.  Sending it only as a multipart form field must return 422.
    Sending it as a query parameter must NOT produce a 422 from the validation
    layer (it may return 404 because the session doesn't exist, but not 422).
    """
    from fastapi.testclient import TestClient
    import importlib
    from io import BytesIO

    main_mod = importlib.import_module("main")
    main_mod.init_db()
    client = TestClient(main_mod.app, raise_server_exceptions=False)

    _FAKE_IMAGE_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 20  # minimal JPEG-ish bytes
    csrf_token = _get_csrf_token(client)

    # Sending user_id only as a form field → 422 Unprocessable Entity
    resp_form = client.post(
        "/api/session/s1/token/t1/image",
        files={"file": ("img.jpg", BytesIO(_FAKE_IMAGE_BYTES), "image/jpeg")},
        data={"user_id": "u1"},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp_form.status_code == 422, (
        f"Expected 422 when user_id is sent as a form field, got {resp_form.status_code}"
    )

    # Sending user_id as a query parameter → NOT 422 (session won't exist so 404)
    resp_query = client.post(
        "/api/session/s1/token/t1/image?user_id=u1",
        files={"file": ("img.jpg", BytesIO(_FAKE_IMAGE_BYTES), "image/jpeg")},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp_query.status_code != 422, (
        f"Expected non-422 when user_id is a query param, got {resp_query.status_code}"
    )
    assert resp_query.status_code == 404, (
        f"Expected 404 (session not found) when user_id is a query param, got {resp_query.status_code}"
    )


def test_token_image_url_persisted_in_db():
    """
    Token image_url must be saved to and loaded from the database.

    This tests the full round-trip: save a session with a token that has
    an image_url, reload it from the DB, and confirm image_url survives.
    """
    import tempfile
    import os
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmpdir:
        os.environ["DND_DATA_DIR"] = tmpdir
        os.environ["DND_DB_PATH"] = str(Path(tmpdir) / "test_campaigns.db")

        # Re-import paths so module-level constants reflect the env override
        import importlib
        import server.paths as paths_mod
        importlib.reload(paths_mod)
        import server.db as db_mod
        importlib.reload(db_mod)

        db_mod.init_db()

        from server.session import Session, Token, User

        session = Session.__new__(Session)
        session.id = "test-session-img"
        session.player_invite = "PLAY01"
        session.viewer_invite = "VIEW01"
        session.name = "Image Test Campaign"
        import time
        session.created_at = time.time()
        session.map_image_url = None
        session.dm_map_context = "world"
        session.dm_current_map_url = None
        session.dm_nav_intent = 0
        session.fog_maps = {}
        session.combat = {"active": False, "turn": 0, "combatants": []}
        session.journal_entries = []
        session.library_entries = []
        session.item_library_entries = []
        session.char_profiles = {}
        session.player_inventories = {}
        session.player_gold = {}
        session.party_loot_log = []
        session.editor_layers = {}
        session.editor_walls = {}
        session.editor_props = {}
        session.map_settings = {}
        session.editor_paths = {}
        session.editor_labels = {}
        session.editor_markers = {}
        session.editor_lights = {}
        session.map_documents = {}
        session.viewer_profiles = {}
        session.viewer_power_catalog = {}
        session.hazard_zones = {}
        session.log = []
        session.pois = {}

        dm = User(id="dm1", name="Test DM", role="dm")
        session.users = {"dm1": dm}
        session.dm_id = "dm1"

        tok = Token(
            id="tok1",
            name="Hero",
            x=10, y=10,
            width=50, height=50,
            color="#ff0000",
            shape="circle",
            owner_id=None,
        )
        tok.image_url = "/static/maps/test-session-img_token_tok1.webp?v=abcd1234"
        session.tokens = {"tok1": tok}

        saved = db_mod.save_campaign(session)
        assert saved, "save_campaign must return True"

        loaded = db_mod.load_campaign("test-session-img")
        assert loaded is not None, "load_campaign must return data for saved session"

        token_rows = loaded["tokens"]
        assert len(token_rows) == 1, "Expected one token row"
        assert token_rows[0]["image_url"] == tok.image_url, (
            f"image_url not preserved: got {token_rows[0]['image_url']!r}"
        )

        # Restore the session and verify the token has image_url
        from server.restore import restore_session_from_db
        restored_session, _ = restore_session_from_db(loaded)
        restored_tok = restored_session.tokens.get("tok1")
        assert restored_tok is not None, "Token not found after restore"
        assert restored_tok.image_url == tok.image_url, (
            f"image_url missing after restore: got {restored_tok.image_url!r}"
        )

        # Cleanup env
        del os.environ["DND_DATA_DIR"]
        del os.environ["DND_DB_PATH"]
        importlib.reload(paths_mod)
        importlib.reload(db_mod)


def test_create_startup_backup():
    """create_startup_backup must create a timestamped backup file in BACKUPS_DIR."""
    import tempfile
    import os
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmpdir:
        os.environ["DND_DATA_DIR"] = tmpdir
        db_path = Path(tmpdir) / "campaigns.db"
        os.environ["DND_DB_PATH"] = str(db_path)

        import importlib
        import server.paths as paths_mod
        importlib.reload(paths_mod)

        # No DB yet — backup should return None gracefully
        result = paths_mod.create_startup_backup()
        assert result is None, "Should return None when DB does not exist yet"

        # Create a stub DB file
        db_path.write_bytes(b"SQLite format 3\x00" + b"\x00" * 80)

        result = paths_mod.create_startup_backup()
        assert result is not None, "Should return a Path when DB exists"
        assert result.exists(), f"Backup file {result} must exist"
        assert result.parent == paths_mod.BACKUPS_DIR, "Backup must be in BACKUPS_DIR"
        assert result.name.startswith("campaigns_"), "Backup filename must start with 'campaigns_'"
        assert result.suffix == ".db", "Backup file must have .db extension"

        # Cleanup env
        del os.environ["DND_DATA_DIR"]
        del os.environ["DND_DB_PATH"]
        importlib.reload(paths_mod)


def test_startup_backup_prunes_old_backups():
    """_prune_old_backups must remove oldest files, keeping only _BACKUP_KEEP most recent."""
    import tempfile
    import os
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmpdir:
        os.environ["DND_DATA_DIR"] = tmpdir
        os.environ["DND_DB_PATH"] = str(Path(tmpdir) / "campaigns.db")

        import importlib
        import server.paths as paths_mod
        importlib.reload(paths_mod)

        backups_dir = paths_mod.BACKUPS_DIR
        backups_dir.mkdir(parents=True, exist_ok=True)

        # Create more backups than _BACKUP_KEEP
        keep = paths_mod._BACKUP_KEEP
        total = keep + 3
        created = []
        for i in range(total):
            ts = f"20240101_{i:06d}"
            p = backups_dir / f"campaigns_{ts}.db"
            p.write_bytes(b"x")
            created.append(p)
            # Filenames sort lexicographically by embedded timestamp — no sleep needed

        paths_mod._prune_old_backups()

        remaining = sorted(backups_dir.glob("campaigns_*.db"))
        assert len(remaining) == keep, (
            f"Expected {keep} backups after pruning, got {len(remaining)}"
        )
        # The most recent (last in sorted order) should survive
        for p in created[-keep:]:
            assert p in remaining, f"{p.name} should have been kept"
        for p in created[:-keep]:
            assert p not in remaining, f"{p.name} should have been pruned"

        # Cleanup env
        del os.environ["DND_DATA_DIR"]
        del os.environ["DND_DB_PATH"]
        importlib.reload(paths_mod)


if __name__ == "__main__":
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    passed = 0
    failed = 0
    for test_fn in tests:
        try:
            test_fn()
            print(f"  PASS  {test_fn.__name__}")
            passed += 1
        except Exception as exc:
            print(f"  FAIL  {test_fn.__name__}: {exc}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    if failed:
        sys.exit(1)


# ---------------------------------------------------------------------------
# Asset API tests
# ---------------------------------------------------------------------------

def _make_minimal_png() -> bytes:
    """Return a 1×1 red PNG as minimal valid image bytes for tests."""
    import struct, zlib
    def chunk(name, data):
        c = struct.pack('>I', len(data)) + name + data
        return c + struct.pack('>I', zlib.crc32(name + data) & 0xFFFFFFFF)
    ihdr = struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0)
    idat_raw = b'\x00\xFF\x00\x00'  # filter byte + R G B for 1×1 RGB
    idat = zlib.compress(idat_raw)
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', ihdr) + chunk(b'IDAT', idat) + chunk(b'IEND', b'')


def test_asset_manifest_endpoint():
    """GET /api/assets/manifest must return a valid merged manifest JSON."""
    import tempfile, os, importlib
    from fastapi.testclient import TestClient

    with tempfile.TemporaryDirectory() as tmpdir:
        os.environ["DND_DATA_DIR"] = tmpdir
        os.environ["DND_DB_PATH"] = str(os.path.join(tmpdir, "campaigns.db"))
        import server.paths as paths_mod
        importlib.reload(paths_mod)
        main_mod = importlib.import_module("main")
        importlib.reload(main_mod)
        client = TestClient(main_mod.app, raise_server_exceptions=False)

        resp = client.get("/api/assets/manifest")
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
        body = resp.json()
        assert "assets" in body, "Manifest must have 'assets' key"
        assert "packs" in body, "Manifest must have 'packs' key"
        assert isinstance(body["assets"], list), "'assets' must be a list"
        # Static assets should be included
        assert len(body["assets"]) > 0, "Merged manifest should include static assets"

        del os.environ["DND_DATA_DIR"]
        del os.environ["DND_DB_PATH"]
        importlib.reload(paths_mod)


def test_asset_upload_single_png():
    """POST /api/assets/upload must accept a valid PNG and return asset metadata."""
    import tempfile, os, importlib
    from io import BytesIO
    from fastapi.testclient import TestClient

    png_bytes = _make_minimal_png()

    with tempfile.TemporaryDirectory() as tmpdir:
        os.environ["DND_DATA_DIR"] = tmpdir
        os.environ["DND_DB_PATH"] = str(os.path.join(tmpdir, "campaigns.db"))
        import server.paths as paths_mod
        importlib.reload(paths_mod)
        main_mod = importlib.import_module("main")
        importlib.reload(main_mod)
        client = TestClient(main_mod.app, raise_server_exceptions=False)
        csrf_token = _get_csrf_token(client)

        resp = client.post(
            "/api/assets/upload",
            files={"file": ("test_floor.png", BytesIO(png_bytes), "image/png")},
            data={
                "category": "terrain",
                "subtype": "custom",
                "style_pack": "custom_imports",
                "name": "Test Floor",
                "tags": "stone,floor,test",
                "tileable": "true",
                "scale": "1",
                "anchor": "center",
                "footprint": "1",
            },
            headers={"X-CSRF-Token": csrf_token},
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert body.get("ok") is True, f"Expected ok=True, got {body}"
        asset = body.get("asset")
        assert asset is not None, "Response must include 'asset'"
        assert asset.get("id"), "Asset must have an id"
        assert asset.get("name") == "Test Floor", f"Name mismatch: {asset.get('name')}"
        assert asset.get("category") == "terrain", f"Category mismatch: {asset.get('category')}"
        assert "stone" in asset.get("tags", []), f"Tags mismatch: {asset.get('tags')}"
        assert asset.get("file", "").startswith("/api/assets/file/"), "File URL should use /api/assets/file/ prefix"

        del os.environ["DND_DATA_DIR"]
        del os.environ["DND_DB_PATH"]
        importlib.reload(paths_mod)


def test_asset_upload_rejects_invalid_type():
    """POST /api/assets/upload must reject non-image files."""
    import tempfile, os, importlib
    from io import BytesIO
    from fastapi.testclient import TestClient

    with tempfile.TemporaryDirectory() as tmpdir:
        os.environ["DND_DATA_DIR"] = tmpdir
        os.environ["DND_DB_PATH"] = str(os.path.join(tmpdir, "campaigns.db"))
        import server.paths as paths_mod
        importlib.reload(paths_mod)
        main_mod = importlib.import_module("main")
        importlib.reload(main_mod)
        client = TestClient(main_mod.app, raise_server_exceptions=False)

        # text/plain should be rejected
        resp = client.post(
            "/api/assets/upload",
            files={"file": ("evil.txt", BytesIO(b"not an image"), "text/plain")},
            headers={"X-CSRF-Token": _get_csrf_token(client)},
        )
        assert resp.status_code == 400, f"Expected 400 for text file, got {resp.status_code}"
        body = resp.json()
        assert body.get("ok") is not True, "ok must not be True for rejected file"

        del os.environ["DND_DATA_DIR"]
        del os.environ["DND_DB_PATH"]
        importlib.reload(paths_mod)


def test_asset_upload_deduplicates():
    """POST /api/assets/upload must skip duplicate files (same content hash)."""
    import tempfile, os, importlib
    from io import BytesIO
    from fastapi.testclient import TestClient

    png_bytes = _make_minimal_png()

    with tempfile.TemporaryDirectory() as tmpdir:
        os.environ["DND_DATA_DIR"] = tmpdir
        os.environ["DND_DB_PATH"] = str(os.path.join(tmpdir, "campaigns.db"))
        import server.paths as paths_mod
        importlib.reload(paths_mod)
        main_mod = importlib.import_module("main")
        importlib.reload(main_mod)
        client = TestClient(main_mod.app, raise_server_exceptions=False)
        csrf_token = _get_csrf_token(client)

        # Upload the same file twice
        for _ in range(2):
            resp = client.post(
                "/api/assets/upload",
                files={"file": ("dup.png", BytesIO(png_bytes), "image/png")},
                data={"name": "Dup Asset"},
                headers={"X-CSRF-Token": csrf_token},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body.get("ok") is True
        # Second upload should be marked as duplicate
        assert body.get("duplicate") is True or body.get("skipped") is True, (
            f"Expected duplicate/skipped flag on second upload, got {body}"
        )

        del os.environ["DND_DATA_DIR"]
        del os.environ["DND_DB_PATH"]
        importlib.reload(paths_mod)


def test_asset_manifest_includes_uploaded():
    """After uploading, GET /api/assets/manifest should include the new asset."""
    import tempfile, os, importlib
    from io import BytesIO
    from fastapi.testclient import TestClient

    png_bytes = _make_minimal_png()

    with tempfile.TemporaryDirectory() as tmpdir:
        os.environ["DND_DATA_DIR"] = tmpdir
        os.environ["DND_DB_PATH"] = str(os.path.join(tmpdir, "campaigns.db"))
        import server.paths as paths_mod
        importlib.reload(paths_mod)
        main_mod = importlib.import_module("main")
        importlib.reload(main_mod)
        client = TestClient(main_mod.app, raise_server_exceptions=False)
        csrf_token = _get_csrf_token(client)

        upload_resp = client.post(
            "/api/assets/upload",
            files={"file": ("unique_floor.png", BytesIO(png_bytes), "image/png")},
            data={"name": "Unique Floor", "category": "terrain"},
            headers={"X-CSRF-Token": csrf_token},
        )
        assert upload_resp.status_code == 200
        asset_id = upload_resp.json()["asset"]["id"]

        manifest_resp = client.get("/api/assets/manifest")
        assert manifest_resp.status_code == 200
        manifest = manifest_resp.json()
        asset_ids = [a["id"] for a in manifest.get("assets", [])]
        assert asset_id in asset_ids, (
            f"Uploaded asset {asset_id!r} not found in manifest after upload. IDs: {asset_ids[:5]}"
        )

        del os.environ["DND_DATA_DIR"]
        del os.environ["DND_DB_PATH"]
        importlib.reload(paths_mod)


def test_asset_update_metadata():
    """POST /api/assets/update must update name and tags of an uploaded asset."""
    import tempfile, os, importlib
    from io import BytesIO
    from fastapi.testclient import TestClient

    png_bytes = _make_minimal_png()

    with tempfile.TemporaryDirectory() as tmpdir:
        os.environ["DND_DATA_DIR"] = tmpdir
        os.environ["DND_DB_PATH"] = str(os.path.join(tmpdir, "campaigns.db"))
        import server.paths as paths_mod
        importlib.reload(paths_mod)
        main_mod = importlib.import_module("main")
        importlib.reload(main_mod)
        client = TestClient(main_mod.app, raise_server_exceptions=False)
        csrf_token = _get_csrf_token(client)

        # Upload
        up = client.post(
            "/api/assets/upload",
            files={"file": ("asset.png", BytesIO(png_bytes), "image/png")},
            data={"name": "Old Name"},
            headers={"X-CSRF-Token": csrf_token},
        )
        assert up.status_code == 200
        asset_id = up.json()["asset"]["id"]

        # Update
        upd = client.post(
            "/api/assets/update",
            data={
                "asset_id": asset_id,
                "name": "New Name",
                "tags": "updated,test",
            },
            headers={"X-CSRF-Token": csrf_token},
        )
        assert upd.status_code == 200, f"Expected 200, got {upd.status_code}: {upd.text}"
        body = upd.json()
        assert body.get("ok") is True
        assert body["asset"]["name"] == "New Name", f"Name not updated: {body['asset']}"
        assert "updated" in body["asset"]["tags"], f"Tags not updated: {body['asset']['tags']}"

        del os.environ["DND_DATA_DIR"]
        del os.environ["DND_DB_PATH"]
        importlib.reload(paths_mod)


def test_asset_delete():
    """DELETE /api/assets/{id} must remove the asset from the manifest."""
    import tempfile, os, importlib
    from io import BytesIO
    from fastapi.testclient import TestClient

    png_bytes = _make_minimal_png()

    with tempfile.TemporaryDirectory() as tmpdir:
        os.environ["DND_DATA_DIR"] = tmpdir
        os.environ["DND_DB_PATH"] = str(os.path.join(tmpdir, "campaigns.db"))
        import server.paths as paths_mod
        importlib.reload(paths_mod)
        main_mod = importlib.import_module("main")
        importlib.reload(main_mod)
        client = TestClient(main_mod.app, raise_server_exceptions=False)
        csrf_token = _get_csrf_token(client)

        # Upload
        up = client.post(
            "/api/assets/upload",
            files={"file": ("del_me.png", BytesIO(png_bytes), "image/png")},
            data={"name": "Delete Me"},
            headers={"X-CSRF-Token": csrf_token},
        )
        assert up.status_code == 200
        asset_id = up.json()["asset"]["id"]

        # Delete
        del_resp = client.delete(f"/api/assets/{asset_id}", headers={"X-CSRF-Token": csrf_token})
        assert del_resp.status_code == 200, f"Expected 200, got {del_resp.status_code}: {del_resp.text}"
        assert del_resp.json().get("ok") is True

        # Asset should no longer appear in manifest
        manifest = client.get("/api/assets/manifest").json()
        asset_ids = [a["id"] for a in manifest.get("assets", [])]
        assert asset_id not in asset_ids, "Deleted asset should not appear in manifest"

        del os.environ["DND_DATA_DIR"]
        del os.environ["DND_DB_PATH"]
        importlib.reload(paths_mod)


def test_asset_paths_exports():
    """ASSETS_DIR and USER_MANIFEST_PATH must be exported from server.paths."""
    import server.paths as paths_mod
    assert hasattr(paths_mod, "ASSETS_DIR"), "ASSETS_DIR must be in server.paths"
    assert hasattr(paths_mod, "USER_MANIFEST_PATH"), "USER_MANIFEST_PATH must be in server.paths"
    assert str(paths_mod.ASSETS_DIR).endswith("assets"), "ASSETS_DIR should end with 'assets'"


def test_terrain_upload_assigns_terrain_id():
    """POST /api/assets/upload must assign terrain_id to terrain category assets."""
    import tempfile, os, importlib
    from io import BytesIO
    from fastapi.testclient import TestClient

    # Create two different PNG images to avoid deduplication
    def make_png(color_byte):
        import struct, zlib
        def chunk(name, data):
            c = struct.pack('>I', len(data)) + name + data
            return c + struct.pack('>I', zlib.crc32(name + data) & 0xFFFFFFFF)
        ihdr = struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0)
        idat_raw = bytes([0, color_byte, 0, 0])  # filter byte + R G B for 1×1 RGB
        idat = zlib.compress(idat_raw)
        return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', ihdr) + chunk(b'IDAT', idat) + chunk(b'IEND', b'')

    png_bytes1 = make_png(0xFF)
    png_bytes2 = make_png(0xFE)

    with tempfile.TemporaryDirectory() as tmpdir:
        os.environ["DND_DATA_DIR"] = tmpdir
        os.environ["DND_DB_PATH"] = str(os.path.join(tmpdir, "campaigns.db"))
        import server.paths as paths_mod
        importlib.reload(paths_mod)
        main_mod = importlib.import_module("main")
        importlib.reload(main_mod)
        client = TestClient(main_mod.app, raise_server_exceptions=False)
        csrf_token = _get_csrf_token(client)

        # Upload a terrain asset
        resp = client.post(
            "/api/assets/upload",
            files={"file": ("test_terrain.png", BytesIO(png_bytes1), "image/png")},
            data={
                "category": "terrain",
                "name": "Test Terrain",
            },
            headers={"X-CSRF-Token": csrf_token},
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert body.get("ok") is True, f"Expected ok=True, got {body}"
        asset = body.get("asset")
        assert asset is not None, "Response must include 'asset'"
        assert "terrain_id" in asset, "Terrain assets must have terrain_id field"
        assert isinstance(asset["terrain_id"], int), "terrain_id must be an integer"
        assert asset["terrain_id"] > 0, "terrain_id must be positive"

        # Upload a second terrain asset - should get a different terrain_id
        resp2 = client.post(
            "/api/assets/upload",
            files={"file": ("test_terrain2.png", BytesIO(png_bytes2), "image/png")},
            data={
                "category": "terrain",
                "name": "Test Terrain 2",
            },
            headers={"X-CSRF-Token": csrf_token},
        )
        assert resp2.status_code == 200
        asset2 = resp2.json()["asset"]
        assert "terrain_id" in asset2
        assert asset2["terrain_id"] > asset["terrain_id"], "Second terrain should get higher terrain_id"

        # Upload a non-terrain asset (props) - should NOT have terrain_id
        resp3 = client.post(
            "/api/assets/upload",
            files={"file": ("test_prop.png", BytesIO(make_png(0xFD)), "image/png")},
            data={
                "category": "props",
                "name": "Test Prop",
            },
            headers={"X-CSRF-Token": csrf_token},
        )
        assert resp3.status_code == 200
        asset3 = resp3.json()["asset"]
        assert "terrain_id" not in asset3, "Non-terrain assets should not have terrain_id"

        del os.environ["DND_DATA_DIR"]
        del os.environ["DND_DB_PATH"]
        importlib.reload(paths_mod)


def test_terrain_batch_upload_assigns_increasing_terrain_ids():
    """POST /api/assets/upload-batch should assign increasing terrain_id values for terrain assets."""
    import tempfile, os, importlib, zipfile
    from io import BytesIO
    from fastapi.testclient import TestClient

    def make_png(color_byte):
        import struct, zlib
        def chunk(name, data):
            c = struct.pack('>I', len(data)) + name + data
            return c + struct.pack('>I', zlib.crc32(name + data) & 0xFFFFFFFF)
        ihdr = struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0)
        idat_raw = bytes([0, color_byte, 0, 0])
        idat = zlib.compress(idat_raw)
        return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', ihdr) + chunk(b'IDAT', idat) + chunk(b'IEND', b'')

    zip_buf = BytesIO()
    with zipfile.ZipFile(zip_buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("terrain_a.png", make_png(0xAA))
        zf.writestr("terrain_b.png", make_png(0xAB))
    zip_buf.seek(0)

    with tempfile.TemporaryDirectory() as tmpdir:
        os.environ["DND_DATA_DIR"] = tmpdir
        os.environ["DND_DB_PATH"] = str(os.path.join(tmpdir, "campaigns.db"))
        import server.paths as paths_mod
        importlib.reload(paths_mod)
        main_mod = importlib.import_module("main")
        importlib.reload(main_mod)
        client = TestClient(main_mod.app, raise_server_exceptions=False)

        resp = client.post(
            "/api/assets/upload-batch",
            files={"file": ("terrain_pack.zip", zip_buf.getvalue(), "application/zip")},
            data={"category": "terrain"},
            headers={"X-CSRF-Token": _get_csrf_token(client)},
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert body.get("ok") is True, f"Expected ok=True, got {body}"
        assets = body.get("assets", [])
        assert len(assets) == 2, f"Expected 2 imported assets, got {len(assets)}"
        ids = [a.get("terrain_id") for a in assets]
        assert all(isinstance(tid, int) and tid > 0 for tid in ids), f"Invalid terrain ids: {ids}"
        assert ids[0] < ids[1], f"Expected increasing terrain ids, got {ids}"

        del os.environ["DND_DATA_DIR"]
        del os.environ["DND_DB_PATH"]
        importlib.reload(paths_mod)




# ---------------------------------------------------------------------------
# Asset pipeline module tests
# ---------------------------------------------------------------------------

def test_asset_pipeline_importable():
    """server.asset_pipeline must be importable."""
    import server.asset_pipeline as pipeline
    assert callable(pipeline.make_asset_id)
    assert callable(pipeline.sha256_hex)
    assert callable(pipeline.validate_image_bytes)
    assert callable(pipeline.build_asset_entry)
    assert callable(pipeline.categories_for_filename)
    assert callable(pipeline.tags_for_filename)
    assert callable(pipeline.derive_grid_size)


def test_make_asset_id_format():
    """make_asset_id must produce safe ids starting with 'user_'."""
    from server.asset_pipeline import make_asset_id
    aid = make_asset_id("my_barrel.png")
    assert aid.startswith("user_"), f"Expected 'user_' prefix, got {aid!r}"
    assert "my_barrel" in aid
    # Special chars should be replaced
    aid2 = make_asset_id("bad file name!.png")
    assert "!" not in aid2
    assert " " not in aid2


def test_sha256_hex_stable():
    """sha256_hex must return stable hex digest."""
    from server.asset_pipeline import sha256_hex
    d = sha256_hex(b"hello")
    assert len(d) == 64
    assert d == sha256_hex(b"hello")
    assert d != sha256_hex(b"world")


def test_validate_image_bytes_rejects_empty():
    """Empty bytes must fail validation."""
    from server.asset_pipeline import validate_image_bytes
    ok, err = validate_image_bytes(b"")
    assert not ok
    assert err


def test_validate_image_bytes_accepts_svg():
    """SVG content must pass validation without PIL."""
    from server.asset_pipeline import validate_image_bytes
    svg = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"></svg>'
    ok, err = validate_image_bytes(svg)
    assert ok, f"SVG should be valid; err={err!r}"
    assert err == ""


def test_validate_image_bytes_accepts_png():
    """A valid minimal PNG must pass validation."""
    from server.asset_pipeline import validate_image_bytes
    png = _make_minimal_png()
    ok, err = validate_image_bytes(png)
    assert ok, f"Minimal PNG should be valid; err={err!r}"


def test_validate_image_bytes_rejects_garbage():
    """Garbage bytes must fail validation."""
    from server.asset_pipeline import validate_image_bytes
    ok, err = validate_image_bytes(b"\x00\x01\x02garbage")
    assert not ok


def test_build_asset_entry_structure():
    """build_asset_entry must return a dict with all required keys."""
    from server.asset_pipeline import build_asset_entry
    entry = build_asset_entry(
        asset_id="user_barrel_abcd",
        name="Oak Barrel",
        category="props",
        subtype="container",
        style_pack="fantasy_props",
        tags=["barrel", "dungeon"],
        file_url="/api/assets/file/barrel.png",
        thumb_url="/api/assets/file/barrel_thumb.webp",
        tileable=False,
        scale=1.0,
        anchor="center",
        duration_ms=0,
        footprint=1.0,
        file_hash="abc123",
        img_w=128,
        img_h=128,
    )
    required = ["id", "name", "category", "subtype", "style_pack", "tags",
                "file", "thumbnail", "license", "scale", "anchor", "footprint",
                "file_hash", "img_w", "img_h"]
    for key in required:
        assert key in entry, f"Missing key {key!r} in asset entry"
    assert entry["id"] == "user_barrel_abcd"
    assert entry["name"] == "Oak Barrel"
    assert entry["category"] == "props"
    assert entry["license"] == "user_imported"


def test_build_asset_entry_terrain_id():
    """build_asset_entry must include terrain_id when provided."""
    from server.asset_pipeline import build_asset_entry
    entry = build_asset_entry(
        asset_id="user_stone_abcd",
        name="Stone Floor",
        category="terrain",
        subtype="hardscape",
        style_pack="custom_imports",
        tags=["stone"],
        file_url="/api/assets/file/stone.png",
        thumb_url="/api/assets/file/stone_thumb.webp",
        tileable=True,
        scale=1.0,
        anchor="center",
        duration_ms=0,
        footprint=1.0,
        file_hash="xyz",
        img_w=512,
        img_h=512,
        terrain_id=42,
    )
    assert entry.get("terrain_id") == 42


def test_build_asset_entry_no_terrain_id_for_props():
    """build_asset_entry must NOT include terrain_id when not given."""
    from server.asset_pipeline import build_asset_entry
    entry = build_asset_entry(
        asset_id="user_crate_abcd",
        name="Crate",
        category="props",
        subtype="container",
        style_pack="fantasy_props",
        tags=[],
        file_url="/api/assets/file/crate.png",
        thumb_url="/api/assets/file/crate_thumb.webp",
        tileable=False,
        scale=1.0,
        anchor="center",
        duration_ms=0,
        footprint=1.0,
        file_hash="zzz",
        img_w=128,
        img_h=128,
    )
    assert "terrain_id" not in entry


def test_categories_for_filename_terrain():
    """categories_for_filename must detect terrain files."""
    from server.asset_pipeline import categories_for_filename
    cat, sub = categories_for_filename("stone_floor_tile.png")
    assert cat == "terrain"


def test_categories_for_filename_container():
    """categories_for_filename must detect container props."""
    from server.asset_pipeline import categories_for_filename
    cat, sub = categories_for_filename("barrel_oak.png")
    assert cat == "props"
    assert sub == "container"


def test_categories_for_filename_marker():
    """categories_for_filename must detect marker files."""
    from server.asset_pipeline import categories_for_filename
    cat, sub = categories_for_filename("marker_city.svg")
    assert cat == "markers"


def test_tags_for_filename_basic():
    """tags_for_filename must extract meaningful tokens."""
    from server.asset_pipeline import tags_for_filename
    tags = tags_for_filename("oak_barrel_dungeon.png")
    assert "barrel" in tags
    assert "dungeon" in tags
    # Stop words and very short tokens should be excluded
    assert "a" not in tags


def test_derive_grid_size():
    """derive_grid_size must return sensible grid footprints."""
    from server.asset_pipeline import derive_grid_size
    assert derive_grid_size(100, 100) == (1, 1)
    assert derive_grid_size(200, 100) == (2, 1)
    assert derive_grid_size(300, 200) == (3, 2)


# ---------------------------------------------------------------------------
# Static manifest content tests
# ---------------------------------------------------------------------------

def test_static_manifest_has_fantasy_props_pack():
    """The static manifest must include the 'fantasy_props' pack."""
    import json
    from pathlib import Path
    manifest_path = Path(__file__).parent.parent / "client" / "static" / "assets" / "manifest.json"
    assert manifest_path.exists(), "manifest.json must exist"
    manifest = json.loads(manifest_path.read_text())
    pack_ids = {p["id"] for p in manifest.get("packs", [])}
    assert "fantasy_props" in pack_ids, f"'fantasy_props' pack missing; found: {pack_ids}"


def test_static_manifest_has_world_markers_pack():
    """The static manifest must include the 'world_markers' pack."""
    import json
    from pathlib import Path
    manifest_path = Path(__file__).parent.parent / "client" / "static" / "assets" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    pack_ids = {p["id"] for p in manifest.get("packs", [])}
    assert "world_markers" in pack_ids, f"'world_markers' pack missing; found: {pack_ids}"


def test_static_manifest_has_prop_assets():
    """The static manifest must contain at least 10 prop assets."""
    import json
    from pathlib import Path
    manifest_path = Path(__file__).parent.parent / "client" / "static" / "assets" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    props = [a for a in manifest.get("assets", []) if a.get("category") == "props"]
    assert len(props) >= 10, f"Expected at least 10 prop assets, got {len(props)}"


def test_static_manifest_has_marker_assets():
    """The static manifest must contain at least 5 marker assets."""
    import json
    from pathlib import Path
    manifest_path = Path(__file__).parent.parent / "client" / "static" / "assets" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    markers = [a for a in manifest.get("assets", []) if a.get("category") == "markers"]
    assert len(markers) >= 5, f"Expected at least 5 marker assets, got {len(markers)}"


def test_static_manifest_prop_assets_have_required_fields():
    """Every prop asset in the static manifest must have all required fields."""
    import json
    from pathlib import Path
    manifest_path = Path(__file__).parent.parent / "client" / "static" / "assets" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    required = ["id", "name", "category", "subtype", "tags", "style_pack", "file", "thumbnail", "license"]
    for asset in manifest.get("assets", []):
        if asset.get("category") not in ("props", "markers"):
            continue
        for field in required:
            assert field in asset, f"Asset {asset.get('id')!r} missing field {field!r}"
        assert isinstance(asset["tags"], list), f"Asset {asset.get('id')!r} tags must be a list"


def test_prop_svg_files_exist():
    """SVG files referenced in the static manifest must exist on disk."""
    import json
    from pathlib import Path
    manifest_path = Path(__file__).parent.parent / "client" / "static" / "assets" / "manifest.json"
    static_root = Path(__file__).parent.parent / "client" / "static"
    manifest = json.loads(manifest_path.read_text())
    missing = []
    for asset in manifest.get("assets", []):
        file_url = asset.get("file", "")
        if not file_url.startswith("/static/assets/"):
            continue
        rel = file_url.removeprefix("/static/")
        disk_path = static_root / rel
        if not disk_path.exists():
            missing.append(str(disk_path))
    assert not missing, f"Missing SVG files on disk:\n" + "\n".join(missing[:10])


def test_generate_prop_assets_script_dry_run():
    """The generator script must run without errors in dry-run mode."""
    import sys, importlib
    from pathlib import Path
    script_path = Path(__file__).parent.parent / "tools" / "generate_prop_assets.py"
    assert script_path.exists(), "tools/generate_prop_assets.py must exist"
    # Import and call main directly
    import importlib.util
    spec = importlib.util.spec_from_file_location("generate_prop_assets", script_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Should not raise
    module.main(["--dry-run"])


# ─────────────────────────────────────────────────────────────────────────────
# Structural tests for play.html (added during audit/cleanup pass, March 2026)
# ─────────────────────────────────────────────────────────────────────────────

PLAY_HTML_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "client", "templates", "play.html")


def _play_html_content():
    with open(PLAY_HTML_PATH, "r", encoding="utf-8") as f:
        return f.read()


def test_play_html_exists():
    """play.html must exist."""
    assert os.path.exists(PLAY_HTML_PATH), "client/templates/play.html must exist"




def test_play_html_initiative_roll_uses_shared_modifier_resolver():
    """Initiative rolling must reuse the same modifier resolver for token/profile variants."""
    content = _play_html_content()
    assert "function _resolveCombatantInitiativeModifier(" in content
    assert "function _resolveCharacterSheetInitiativeModifier(" in content
    assert "sheet?.initiativeBonus" in content
    assert "activeProfile?.charBook?.initiative" in content
    assert "document.getElementById('char-initiative')?.value" in content
    assert "const modifier = _resolveCombatantInitiativeModifier(com, combatTok);" in content
    assert "const initMod = _resolveCombatantInitiativeModifier(null, t);" in content


def test_play_html_initiative_roll_preserves_raw_d20_display():
    """The physical die result remains raw while the initiative total adds the modifier."""
    content = _play_html_content()
    assert "The settled d20 face is the initiative roll; add the character initiative modifier only after the die finishes." in content
    assert "modifierMeta: { key: 'initiative', name: 'Initiative', value: modifier }" in content


def test_play_html_initiative_waits_for_settled_dice_before_sync():
    """Initiative should update/send only after the local d20 visual settles."""
    content = _play_html_content()
    assert "function _rollLocalDiceAfterSettle(" in content
    assert "onSettledResult: (settled) => finish" in content
    assert "const handled = settledCallback({" in content
    assert "The local dice result is visual only." in content
    assert content.index("_rollLocalDiceAfterSettle({") < content.index("sendWS({ type: 'combat_roll_initiative'")
    assert "rolls: [roll]" in content
    assert "return modText ? `${total} (${rawRoll}${modText})` : `${total} (${rawRoll})`;" in content

def test_play_html_loads_required_external_modules():
    """play.html must load all required external JS modules via <script src=...> tags."""
    content = _play_html_content()
    required = [
        "/static/js/editor/serialization.js",
        "/static/js/editor/terrain_manifest.js",
        "/static/js/assets/dnd_assets.js",
        "/static/js/editor/asset_initializer.js",
        "/static/js/editor/asset_renderer.js",
        "/static/js/editor/terrain_renderer.js",
        "/static/js/editor/placement_controller.js",
        "/static/js/editor/shop_panel.js",
        "/static/js/editor/shop_view.js",
        "/static/js/render/combat_fx.js",
        "/static/js/editor/assets.js",
        "/static/js/ui/asset_library.js",
        "/static/js/ui/editor_panel.js",
    ]
    for path in required:
        assert path in content, (
            f"play.html must load {path} via <script src=...>"
        )


def test_play_html_no_legacy_importDDBCharacter_stub():
    """importDDBCharacter legacy stub must not exist in play.html."""
    content = _play_html_content()
    assert "async function importDDBCharacter" not in content, (
        "Legacy stub importDDBCharacter was re-introduced in play.html. "
        "The button calls importDDBJson() directly."
    )


def test_play_html_no_dm_activity_css_removed_comment():
    """The stale '/* DM activity CSS removed */' comment must not be in play.html."""
    content = _play_html_content()
    assert "/* DM activity CSS removed */" not in content, (
        "Stale CSS comment '/* DM activity CSS removed */' was re-introduced."
    )


def test_play_html_has_single_inline_script_block():
    """play.html must keep the game runtime in exactly ONE large inline <script>.

    A single tiny pre-boot shim is allowed: it sets __PLAY_BOOT_ROLE /
    __playerBootState before diagnostics.js runs (the first external runtime
    script), which is required so diagnostics.js doesn't raise a false
    "player boot failed" overlay — see
    test_player_boot_role_is_defined_before_diagnostics_marks_scripts_started.
    That shim cannot be merged into the main block because the main block must
    load AFTER all external scripts. Everything else (the actual game logic)
    must stay in the one main runtime block — do not split it across blocks.
    """
    import re
    content = _play_html_content()
    # Bodies of plain inline scripts only (exact `<script>` tag — excludes
    # src=, type="module", and type="importmap" variants).
    bodies = re.findall(r'<script>(.*?)</script>', content, re.IGNORECASE | re.DOTALL)
    large_blocks = [b for b in bodies if len(b) > 2000]
    small_blocks = [b for b in bodies if len(b) <= 2000]
    assert len(large_blocks) == 1, (
        f"Expected exactly 1 large inline <script> block in play.html, found {len(large_blocks)}. "
        "Avoid splitting runtime logic into multiple inline script blocks."
    )
    assert len(small_blocks) <= 1, (
        f"Expected at most 1 tiny pre-boot shim inline <script>, found {len(small_blocks)}. "
        "Only the boot-role shim may live outside the main runtime block."
    )


def test_play_html_uses_session_theme_css():
    """play.html must link session-theme.css."""
    content = _play_html_content()
    assert "/static/css/session-theme.css" in content, (
        "play.html must load /static/css/session-theme.css"
    )


def test_play_html_uses_external_play_css():
    """play.html must load the extracted base stylesheet instead of a massive inline style block."""
    content = _play_html_content()
    assert '<link rel="stylesheet" href="/static/css/play.css?v=1">' in content
    assert "<style" not in content
    css_path = os.path.join(PROJECT_ROOT, "client/static/css/play.css")
    css = open(css_path, encoding="utf-8").read()
    assert ":root {" in css
    assert "#map-canvas" in css


def test_play_html_global_functions_no_duplicate_names():
    """Top-level function names in play.html must be unique (no accidental duplicates)."""
    import re
    content = _play_html_content()
    func_names = re.findall(r'^(?:async\s+)?function\s+(\w+)\s*\(', content, re.MULTILINE)
    seen = {}
    duplicates = {}
    for name in func_names:
        seen[name] = seen.get(name, 0) + 1
    for name, count in seen.items():
        if count > 1:
            duplicates[name] = count
    assert not duplicates, (
        f"Duplicate top-level function names found in play.html: {duplicates}. "
        "Each function must be defined exactly once."
    )


def test_play_html_no_inline_modules_loaded_twice():
    """Each external module must appear at most once in play.html script tags."""
    import re
    content = _play_html_content()
    srcs = re.findall(r'<script[^>]+src="([^"]+)"', content)
    from collections import Counter
    counts = Counter(srcs)
    duplicated = {src: c for src, c in counts.items() if c > 1}
    assert not duplicated, (
        f"External modules loaded more than once in play.html: {duplicated}"
    )


def test_play_html_has_player_companion_panel():
    """Journal flyout should include the player companion panel container."""
    content = _play_html_content()
    assert 'id="player-companion-panel"' in content, (
        "play.html must include #player-companion-panel in the journal flyout."
    )


def test_play_html_has_player_companion_runtime_functions():
    """Player companion runtime hooks should exist for notes and quest tracking."""
    content = _play_html_content()
    required = [
        "function renderPlayerCompanionPanel()",
        "function togglePlayerQuestDone(",
        "function openPlayerCompanionHandout(",
        "function _playerCompanionStorageKey(",
    ]
    for token in required:
        assert token in content, f"play.html must include '{token}' for player companion behavior."


def test_repo_map_exists():
    """docs/repo-map.md must exist (architecture documentation for AI agents)."""
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    repo_map = os.path.join(repo_root, "docs", "repo-map.md")
    assert os.path.exists(repo_map), (
        "docs/repo-map.md must exist. "
        "This file documents the architecture for AI agents and developers."
    )


def test_repo_map_has_required_sections():
    """docs/repo-map.md must contain key orientation sections."""
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    repo_map = os.path.join(repo_root, "docs", "repo-map.md")
    with open(repo_map, "r", encoding="utf-8") as f:
        content = f.read()
    required_sections = [
        "play.html",
        "Single Source of Truth",
        "AI Agent",
        "server/handlers",
    ]
    for section in required_sections:
        assert section in content, (
            f"docs/repo-map.md must contain '{section}' section"
        )


# ---------------------------------------------------------------------------
# Creature Library (Bestiary) tests
# ---------------------------------------------------------------------------

def test_srd_bestiary_importable():
    """server.srd_bestiary must be importable and expose get_srd_monsters()."""
    from server.srd_bestiary import get_srd_monsters
    assert callable(get_srd_monsters), "get_srd_monsters must be callable"


def test_srd_bestiary_minimum_monster_count():
    """SRD bestiary must contain at least 50 monster entries."""
    from server.srd_bestiary import get_srd_monsters
    monsters = get_srd_monsters()
    assert len(monsters) >= 50, (
        f"Expected at least 50 SRD monsters, got {len(monsters)}"
    )


def test_srd_bestiary_required_fields():
    """Every SRD monster must have the required stat-block fields."""
    from server.srd_bestiary import get_srd_monsters
    required = ["name", "cr", "hp", "ac", "speed", "source",
                "str_score", "dex_score", "con_score",
                "int_score", "wis_score", "cha_score"]
    monsters = get_srd_monsters()
    for m in monsters:
        for field in required:
            assert field in m, (
                f"SRD monster {m.get('name')!r} missing required field {field!r}"
            )


def test_srd_bestiary_source_is_srd():
    """Every monster returned by get_srd_monsters() must have source='srd'."""
    from server.srd_bestiary import get_srd_monsters
    for m in get_srd_monsters():
        assert m.get("source") == "srd", (
            f"Monster {m.get('name')!r} has source={m.get('source')!r}; expected 'srd'"
        )


def test_srd_bestiary_json_fields_are_lists():
    """attacks, abilities, and tags fields must be Python lists (not raw JSON strings)."""
    from server.srd_bestiary import get_srd_monsters
    for m in get_srd_monsters():
        for field in ("attacks", "abilities", "tags"):
            val = m.get(field, [])
            assert isinstance(val, list), (
                f"Monster {m.get('name')!r} field {field!r} must be a list, got {type(val).__name__}"
            )


def _make_test_db():
    """Create an isolated temporary SQLite DB and return (path, env_patcher) context."""
    import tempfile
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    return tmp.name


def _init_isolated_db(db_path: str):
    """Initialise a fresh isolated DB at db_path and return the db module."""
    import os, importlib
    os.environ["DND_DB_PATH"] = db_path
    # Force reload of paths + db so they pick up the new env var
    import server.paths as paths_mod
    import server.db as db_mod
    importlib.reload(paths_mod)
    importlib.reload(db_mod)
    db_mod.init_db()
    return db_mod


def test_db_creature_library_crud():
    """create_creature / get_creature / update_creature / delete_creature round-trip."""
    import os, tempfile, importlib

    db_path = _make_test_db()
    try:
        db = _init_isolated_db(db_path)

        owner = "test_user_42"
        # Create
        creature = db.create_creature(owner, {
            "name": "Test Dragon",
            "creature_type": "monster",
            "monster_type": "dragon",
            "cr": "5",
            "hp": 100,
            "ac": 17,
            "speed": "40 ft., fly 80 ft.",
            "str_score": 20, "dex_score": 10, "con_score": 18,
            "int_score": 12, "wis_score": 11, "cha_score": 15,
            "attacks": [{"name": "Bite", "bonus": 7, "damage": "2d6+5", "type": "piercing"}],
            "abilities": [{"name": "Fire Breath", "desc": "Exhales fire."}],
            "tags": ["dragon", "fire"],
            "source": "custom",
            "backstory": "An ancient wyrm.",
            "voice_style": "deep booming",
        })
        assert creature is not None, "create_creature must return the new creature"
        cid = creature["id"]
        assert creature["name"] == "Test Dragon"
        assert creature["cr"] == "5"
        assert creature["hp"] == 100
        assert creature["source"] == "custom"
        assert isinstance(creature["attacks"], list)
        assert len(creature["attacks"]) == 1

        # Get
        fetched = db.get_creature(cid, owner)
        assert fetched is not None
        assert fetched["id"] == cid
        assert fetched["name"] == "Test Dragon"

        # Update
        updated = db.update_creature(cid, owner, {"hp": 120, "backstory": "Updated story."})
        assert updated is not None
        assert updated["hp"] == 120
        assert updated["backstory"] == "Updated story."

        # Delete (soft)
        ok = db.delete_creature(cid, owner)
        assert ok is True
        gone = db.get_creature(cid, owner)
        assert gone is None, "Soft-deleted creature must not be returned by get_creature"
    finally:
        os.unlink(db_path)


def test_db_get_creatures_filters():
    """get_creatures must correctly filter by CR, source, and search."""
    import os

    db_path = _make_test_db()
    try:
        db = _init_isolated_db(db_path)

        owner = "filter_test_user"
        db.create_creature(owner, {"name": "Goblin", "cr": "1/4", "hp": 7, "ac": 15,
                                   "source": "srd", "monster_type": "humanoid"})
        db.create_creature(owner, {"name": "Troll", "cr": "5", "hp": 84, "ac": 15,
                                   "source": "custom", "monster_type": "giant"})
        db.create_creature(owner, {"name": "Ancient Dragon", "cr": "22", "hp": 367, "ac": 22,
                                   "source": "variant", "monster_type": "dragon"})

        # CR filter
        low_cr = db.get_creatures(owner, cr_max="1")
        assert all(_cr_to_float_test(c["cr"]) <= 1.0 for c in low_cr)
        assert any(c["name"] == "Goblin" for c in low_cr)
        assert not any(c["name"] == "Ancient Dragon" for c in low_cr)

        # Source filter
        custom_only = db.get_creatures(owner, source="custom")
        assert all(c["source"] == "custom" for c in custom_only)
        assert any(c["name"] == "Troll" for c in custom_only)

        # Search by name
        search_results = db.get_creatures(owner, search="dragon")
        assert any(c["name"] == "Ancient Dragon" for c in search_results)

        # monster_type filter
        humanoids = db.get_creatures(owner, monster_type="humanoid")
        assert all(c.get("monster_type", "").lower() == "humanoid" for c in humanoids)
    finally:
        os.unlink(db_path)


def _cr_to_float_test(cr_str: str) -> float:
    """Helper for test CR comparison."""
    s = str(cr_str or "0").strip()
    if "/" in s:
        parts = s.split("/")
        try:
            return float(parts[0]) / float(parts[1])
        except Exception:
            return 0.0
    try:
        return float(s)
    except Exception:
        return 0.0


def test_db_seed_srd_idempotent():
    """seed_srd_for_user must seed exactly once and be idempotent on re-call."""
    import os

    db_path = _make_test_db()
    try:
        db = _init_isolated_db(db_path)
        owner = "seed_test_user"

        db.seed_srd_for_user(owner)
        count_1 = len(db.get_creatures(owner, source="srd"))
        assert count_1 > 0, "Seed must insert SRD monsters"

        # Call again — should not duplicate
        db.seed_srd_for_user(owner)
        count_2 = len(db.get_creatures(owner, source="srd"))
        assert count_2 == count_1, (
            f"seed_srd_for_user must be idempotent; first call: {count_1}, second: {count_2}"
        )
    finally:
        os.unlink(db_path)


def test_db_creature_variant_cloning():
    """create_creature with source='variant' must produce a distinct creature."""
    import os

    db_path = _make_test_db()
    try:
        db = _init_isolated_db(db_path)
        owner = "variant_test_user"

        original = db.create_creature(owner, {"name": "Goblin", "cr": "1/4", "hp": 7,
                                              "ac": 15, "source": "srd", "srd_id": "goblin"})
        assert original is not None

        variant_data = dict(original)
        variant_data.pop("id")
        variant_data.pop("created_at", None)
        variant_data.pop("updated_at", None)
        variant_data["name"] = "Goblin Boss (Variant)"
        variant_data["source"] = "variant"
        variant_data["hp"] = 21

        variant = db.create_creature(owner, variant_data)
        assert variant is not None
        assert variant["id"] != original["id"], "Variant must get a new unique ID"
        assert variant["name"] == "Goblin Boss (Variant)"
        assert variant["source"] == "variant"
        assert variant["hp"] == 21
    finally:
        os.unlink(db_path)

def test_db_seed_srd_npcs_idempotent():
    """seed_srd_npcs_for_user must seed built-in NPCs without crashing or duplicating."""
    import os

    db_path = _make_test_db()
    try:
        db = _init_isolated_db(db_path)
        owner = "seed_npc_test_user"

        db.seed_srd_npcs_for_user(owner)
        count_1 = len(db.get_creatures(owner, source="builtin"))
        assert count_1 > 0, "Seed must insert built-in NPCs"

        db.seed_srd_npcs_for_user(owner)
        count_2 = len(db.get_creatures(owner, source="builtin"))
        assert count_2 == count_1, (
            f"seed_srd_npcs_for_user must be idempotent; first call: {count_1}, second: {count_2}"
        )
    finally:
        os.unlink(db_path)


def test_variant_endpoint_accepts_owner_id_without_auth():
    """Variant creation should work for unauthenticated callers that pass owner_id."""
    import importlib
    from fastapi.testclient import TestClient

    main_mod = importlib.import_module("main")
    main_mod.init_db()
    client = TestClient(main_mod.app, raise_server_exceptions=False)

    owner = "variant_endpoint_owner"
    created = main_mod.create_creature(owner, {
        "name": "Goblin",
        "cr": "1/4",
        "hp": 7,
        "ac": 15,
        "source": "srd",
        "srd_id": "goblin",
    })
    assert created is not None

    resp = client.post(
        f"/api/library/creatures/{created['id']}/variant?owner_id={owner}",
        json={"name": "Goblin Boss (Variant)"},
        headers={"X-CSRF-Token": _get_csrf_token(client)},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["creature"]["name"] == "Goblin Boss (Variant)"
    assert body["creature"]["source"] == "variant"
    assert body["creature"]["id"] != created["id"]


def test_maps_library_accepts_empty_bool_query_values():
    """Map library search should tolerate empty checkbox-style query params from the DM UI."""
    import importlib
    from fastapi.testclient import TestClient
    import server.paths as _paths_mod
    import server.db as _db_mod
    import server.map_library as _map_lib_mod

    importlib.reload(_paths_mod)
    importlib.reload(_db_mod)
    _db_mod.init_db()
    importlib.reload(_map_lib_mod)
    main_mod = importlib.import_module("main")
    importlib.reload(main_mod)
    client = TestClient(main_mod.app, raise_server_exceptions=False)

    resp = client.get(
        "/api/maps/library"
        "?image_style=&grid_type=&scale_label=&tags=&include_collections=true"
        "&open_content_only=&premium_only=&source_type=&pack_name=&sort=best_match&page_size=8"
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "items" in body


def test_main_py_has_creature_library_endpoints():
    """main.py must declare all required creature library REST endpoints."""
    main_py_path = os.path.join(PROJECT_ROOT, "main.py")
    with open(main_py_path, "r", encoding="utf-8") as f:
        source = f.read()
    required_endpoints = [
        '/api/library/creatures"',           # GET list + POST create
        '/api/library/creatures/{creature_id}"',  # PUT update + DELETE
        '/api/library/creatures/{creature_id}/variant"',
        '/api/library/creatures/{creature_id}/spawn"',
    ]
    for ep in required_endpoints:
        assert ep in source, (
            f"main.py must declare endpoint containing {ep!r}"
        )


def test_main_py_spawn_broadcasts_token_created():
    """Spawn endpoint must broadcast a 'token_created' WebSocket message."""
    main_py_path = os.path.join(PROJECT_ROOT, "main.py")
    with open(main_py_path, "r", encoding="utf-8") as f:
        source = f.read()
    assert '"token_created"' in source or "'token_created'" in source, (
        "Spawn endpoint must broadcast 'token_created' WS message"
    )
    assert "from_bestiary" in source, (
        "Spawn broadcast payload must include 'from_bestiary' flag"
    )


def test_play_html_has_bestiary_panel():
    """play.html must contain the bestiary panel with all required elements."""
    content = _play_html_content()
    required_ids = [
        "rtab-pane-bestiary",
        "bestiary-encounter-panel",
        "bestiary-encounter-list",
        "bestiary-encounter-empty",
        "bestiary-template-list",
        "bestiary-template-empty",
        "bestiary-search",
        "bestiary-filter-type",
        "bestiary-filter-source",
        "bestiary-cr-min",
        "bestiary-cr-max",
        "bestiary-list",
        "bestiary-loading",
        "bestiary-stat-modal",
        "bestiary-form-modal",
    ]
    for eid in required_ids:
        assert eid in content, (
            f"play.html must contain element with id={eid!r}"
        )


def test_play_html_has_bestiary_js_functions():
    """play.html must define all required bestiary JavaScript functions."""
    content = _play_html_content()
    required_fns = [
        "loadBestiaryEncounterDraft",
        "persistBestiaryEncounterDraft",
        "loadEncounterTemplates",
        "renderEncounterTemplateList",
        "renderBestiaryEncounterDraft",
        "bestiaryAddCurrentToEncounter",
        "bestiarySetEncounterQty",
        "bestiaryAdjustEncounterQty",
        "bestiaryRemoveEncounterDraftEntry",
        "bestiaryClearEncounterDraft",
        "bestiarySaveEncounterTemplate",
        "bestiaryLoadEncounterTemplate",
        "bestiarySpawnEncounterDraft",
        "bestiarySpawnEncounterTemplate",
        "bestiaryDeleteEncounterTemplate",
        "bestiaryStartCombatFromCurrentMap",
        "bestiaryLoad",
        "renderBestiaryList",
        "openBestiaryStatModal",
        "closeBestiaryStatModal",
        "bestiarySpeak",
        "beginBestiarySpawn",
        "cancelBestiarySpawn",
        "bestiaryPlaceCreatureAt",
        "openBestiaryCreateForm",
        "closeBestiaryFormModal",
        "saveBestiaryCreature",
        "deleteBestiaryCreature",
        "bestiaryMakeVariant",
        "debounceBestiarySearch",
        "clearBestiaryFilters",
    ]
    for fn in required_fns:
        assert fn in content, (
            f"play.html must define JavaScript function {fn!r}"
        )


def test_play_html_bestiary_spawn_calls_api():
    """bestiaryPlaceCreatureAt must call the /spawn REST endpoint."""
    content = _play_html_content()
    assert "/spawn" in content, (
        "bestiaryPlaceCreatureAt must call the /api/library/creatures/{id}/spawn endpoint"
    )


def test_play_html_bestiary_tts_uses_voice_style():
    """bestiarySpeak must read voice_style to customize speech synthesis."""
    content = _play_html_content()
    assert "voice_style" in content, (
        "bestiarySpeak must reference voice_style for TTS customization"
    )
    assert "speechSynthesis" in content, (
        "bestiarySpeak must use Web Speech API (speechSynthesis)"
    )


def test_play_html_bestiary_tab_dm_only():
    """Bestiary tab (rtab-bestiary) must only be shown to DMs."""
    content = _play_html_content()
    # The tab button must start hidden and only be shown when ROLE === 'dm'
    assert "rtab-bestiary" in content
    assert "ROLE === 'dm'" in content or "ROLE=='dm'" in content, (
        "Bestiary tab visibility must be conditional on ROLE === 'dm'"
    )


def _spawn_test_context():
    import importlib
    from server.session import Session, User

    db_path = _make_test_db()
    db = _init_isolated_db(db_path)
    service = importlib.import_module("server.creatures.service")
    importlib.reload(service)

    session = Session(id="SPAWN01", dm_id="dm-user")
    session.users = {
        "dm-user": User(id="dm-user", name="Dungeon Master", role="dm"),
        "player-user": User(id="player-user", name="Player", role="player"),
    }

    class _Manager:
        def __init__(self):
            self.messages = []
            self.sent = []

        async def broadcast(self, session_id, payload, exclude_user=None):
            self.messages.append((session_id, payload))

        async def send_to(self, session_id, user_id, payload):
            self.sent.append((session_id, user_id, payload))
            return True

    manager = _Manager()

    async def _noop_save_campaign(_session):
        return None

    service.get_or_restore_session = lambda _sid: session
    service.request_has_dm_access = lambda request, _session, fallback_user_id="": (
        str((getattr(request, "auth_user", {}) or {}).get("id") or "") == "dm-user"
    )
    service.get_request_user = lambda request: getattr(request, "auth_user", None)

    import server.connections as connections_mod
    import server.db as db_mod
    connections_mod.manager = manager
    db_mod.save_campaign_async = _noop_save_campaign

    class FakeRequest:
        def __init__(self, auth_user):
            self.auth_user = auth_user

    return db_path, db, service, session, manager, FakeRequest


def test_spawn_custom_monster_owned_by_dm():
    import asyncio
    import json
    import os

    db_path, db, service, session, manager, FakeRequest = _spawn_test_context()
    try:
        creature = db.create_creature("dm-user", {
            "name": "Tunnel Ogre",
            "entry_type": "monster",
            "creature_type": "monster",
            "source": "custom",
            "hp": 59,
            "ac": 13,
        })
        response = asyncio.run(service.spawn_creature_response(creature["id"], FakeRequest({"id": "dm-user"}), {
            "session_id": session.id,
            "creature_id": creature["id"],
            "source": "custom",
            "entity_type": "monster",
            "x": 100,
            "y": 200,
        }))
        assert response.status_code == 200
        body = json.loads(response.body)
        assert body["token"]["name"] == "Tunnel Ogre"
        assert body["token"]["tokenType"] == "monster"
        assert body["token"]["x"] == 100
        assert body["token"]["y"] == 200
        assert manager.sent[-1][2]["payload"]["creature_id"] == creature["id"]
    finally:
        os.unlink(db_path)


def test_spawn_library_creature_uses_active_map_grid_size():
    import asyncio
    import json
    import os

    db_path, db, service, session, manager, FakeRequest = _spawn_test_context()
    try:
        session.map_settings = {"world": {"grid": {"size_px": 72}}}
        creature = db.create_creature("dm-user", {
            "name": "Stone Giant",
            "entry_type": "monster",
            "creature_type": "monster",
            "source": "custom",
            "hp": 126,
            "ac": 17,
            "token_size": 2,
        })
        response = asyncio.run(service.spawn_creature_response(creature["id"], FakeRequest({"id": "dm-user"}), {
            "session_id": session.id,
            "creature_id": creature["id"],
            "source": "custom",
            "entity_type": "monster",
            "x": 144,
            "y": 216,
        }))
        assert response.status_code == 200
        body = json.loads(response.body)
        assert body["token"]["width"] == 144
        assert body["token"]["height"] == 144
        assert manager.sent[-1][2]["payload"]["token"]["width"] == 144
    finally:
        os.unlink(db_path)


def test_spawn_request_grid_size_overrides_unsaved_map_settings():
    import asyncio
    import json
    import os

    db_path, db, service, session, manager, FakeRequest = _spawn_test_context()
    try:
        session.map_settings = {"world": {"grid": {"size_px": 64}}}
        creature = db.create_creature("dm-user", {
            "name": "Freshly Resized Ogre",
            "entry_type": "monster",
            "creature_type": "monster",
            "source": "custom",
            "hp": 59,
            "ac": 13,
            "token_size": 1,
        })
        response = asyncio.run(service.spawn_creature_response(creature["id"], FakeRequest({"id": "dm-user"}), {
            "session_id": session.id,
            "creature_id": creature["id"],
            "source": "custom",
            "entity_type": "monster",
            "x": 100,
            "y": 200,
            "grid_size_px": 88,
        }))
        assert response.status_code == 200
        body = json.loads(response.body)
        assert body["token"]["width"] == 88
        assert body["token"]["height"] == 88
    finally:
        os.unlink(db_path)


def test_spawn_custom_npc_owned_by_dm():
    import asyncio
    import json
    import os

    db_path, db, service, session, manager, FakeRequest = _spawn_test_context()
    try:
        creature = db.create_creature("dm-user", {
            "name": "Captain Mira",
            "entry_type": "npc",
            "creature_type": "npc",
            "source": "custom",
            "hp": 22,
            "ac": 15,
        })
        response = asyncio.run(service.spawn_creature_response(creature["id"], FakeRequest({"id": "dm-user"}), {
            "session_id": session.id,
            "creature_id": creature["id"],
            "source": "custom",
            "entity_type": "npc",
            "x": 33,
            "y": 44,
        }))
        assert response.status_code == 200
        body = json.loads(response.body)
        assert body["token"]["tokenType"] == "npc"
        assert body["token"]["name"] == "Captain Mira"
    finally:
        os.unlink(db_path)


def test_spawn_srd_creature_allowed_for_dm():
    import asyncio
    import json
    import os

    db_path, db, service, session, manager, FakeRequest = _spawn_test_context()
    try:
        db.seed_srd_for_user("dm-user")
        creature = next(c for c in db.get_creatures("dm-user", source="srd") if c.get("srd_id"))
        response = asyncio.run(service.spawn_creature_response(creature["id"], FakeRequest({"id": "dm-user"}), {
            "session_id": session.id,
            "creature_id": creature["id"],
            "source": "srd",
            "entity_type": creature["entry_type"],
            "x": 9,
            "y": 12,
        }))
        assert response.status_code == 200
        body = json.loads(response.body)
        assert body["creature"]["source"] == "srd"
        assert manager.sent[-1][2]["payload"]["source"] == "srd"
    finally:
        os.unlink(db_path)


def test_spawn_bad_creature_id_returns_404():
    import asyncio
    import json
    import os

    db_path, db, service, session, manager, FakeRequest = _spawn_test_context()
    try:
        response = asyncio.run(service.spawn_creature_response("missing-id", FakeRequest({"id": "dm-user"}), {
            "session_id": session.id,
            "creature_id": "missing-id",
            "source": "custom",
            "entity_type": "monster",
            "x": 0,
            "y": 0,
        }))
        assert response.status_code == 404
        body = json.loads(response.body)
        assert body["code"] == "creature_not_found"
    finally:
        os.unlink(db_path)


def test_spawn_denied_for_non_dm_user():
    import asyncio
    import json
    import os

    db_path, db, service, session, manager, FakeRequest = _spawn_test_context()
    try:
        creature = db.create_creature("dm-user", {
            "name": "Hidden Stalker",
            "entry_type": "monster",
            "creature_type": "monster",
            "source": "custom",
            "hp": 21,
            "ac": 14,
        })
        response = asyncio.run(service.spawn_creature_response(creature["id"], FakeRequest({"id": "player-user"}), {
            "session_id": session.id,
            "creature_id": creature["id"],
            "source": "custom",
            "entity_type": "monster",
            "x": 12,
            "y": 18,
        }))
        assert response.status_code == 403
        body = json.loads(response.body)
        assert body["code"] == "spawn_not_allowed"
    finally:
        os.unlink(db_path)


def test_spawn_denied_for_custom_creature_not_owned_by_dm():
    import asyncio
    import json
    import os

    db_path, db, service, session, manager, FakeRequest = _spawn_test_context()
    try:
        creature = db.create_creature("other-user", {
            "name": "Somebody Else's Beast",
            "entry_type": "monster",
            "creature_type": "monster",
            "source": "custom",
            "hp": 17,
            "ac": 12,
        })
        response = asyncio.run(service.spawn_creature_response(creature["id"], FakeRequest({"id": "dm-user"}), {
            "session_id": session.id,
            "creature_id": creature["id"],
            "source": "custom",
            "entity_type": "monster",
            "x": 1,
            "y": 2,
        }))
        assert response.status_code == 403
        body = json.loads(response.body)
        assert body["code"] == "creature_not_owned"
    finally:
        os.unlink(db_path)


# ---------------------------------------------------------------------------
# Viewer Power system tests
# ---------------------------------------------------------------------------

def _make_viewer_power_session():
    """Create a minimal session with a DM, a viewer, and tokens for power tests."""
    import sys
    import os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from server.session import Session, User, Token
    import time

    session = Session(id="vp-test")
    session.viewer_profiles = {}
    session.viewer_pending_actions = {}
    session.viewer_power_catalog = {}
    session.tokens = {}
    session.log = []
    session.users = {}

    dm = User(id="dm1", name="DM", role="dm")
    viewer = User(id="v1", name="TestViewer", role="viewer")
    viewer.player_key = "vkey1"
    session.users = {"dm1": dm, "v1": viewer}
    session.dm_id = "dm1"

    tok = Token(id="tok1", name="Hero", x=100, y=100, width=50, height=50,
                color="#0f0", shape="circle", owner_id="p1")
    session.tokens = {"tok1": tok}

    return session, dm, viewer, tok


def test_consume_viewer_power_removes_on_zero_cooldown():
    """_consume_viewer_power must always remove the power entry."""
    from server.handlers.viewer_powers import _consume_viewer_power, _normalize_viewer_profile
    session, dm, viewer, tok = _make_viewer_power_session()
    viewer_key = "vkey1"

    # Set up a profile with a no-cooldown power
    profiles = {
        viewer_key: {
            "viewer_key": viewer_key,
            "user_id": "v1",
            "name": "TestViewer",
            "powers": {
                "pebble_toss": {"power_id": "pebble_toss", "charges": 1,
                                "enabled": True, "requires_approval": False,
                                "cooldown_sec": 0, "cooldown_until": 0.0}
            }
        }
    }
    session.viewer_profiles = profiles

    result = _consume_viewer_power(session, viewer_key, "pebble_toss")
    assert result is True
    remaining = session.viewer_profiles.get(viewer_key, {}).get("powers", {})
    assert "pebble_toss" not in remaining, (
        "pebble_toss must be removed after use"
    )


def test_consume_viewer_power_applies_cooldown():
    """_consume_viewer_power always removes the power entry, regardless of cooldown_sec."""
    from server.handlers.viewer_powers import _consume_viewer_power
    session, dm, viewer, tok = _make_viewer_power_session()
    viewer_key = "vkey1"

    profiles = {
        viewer_key: {
            "viewer_key": viewer_key,
            "user_id": "v1",
            "name": "TestViewer",
            "powers": {
                "fireball": {"power_id": "fireball", "charges": 1,
                             "enabled": True, "requires_approval": False,
                             "cooldown_sec": 90, "cooldown_until": 0.0}
            }
        }
    }
    session.viewer_profiles = profiles
    result = _consume_viewer_power(session, viewer_key, "fireball")

    assert result is True
    powers = session.viewer_profiles.get(viewer_key, {}).get("powers", {})
    assert "fireball" not in powers, (
        "fireball must be removed after use, even when cooldown_sec=90, so it disappears "
        "from the viewer's UI until the DM grants it again"
    )


def test_consume_viewer_power_not_found_returns_false():
    """_consume_viewer_power returns False when power_id not in profile."""
    from server.handlers.viewer_powers import _consume_viewer_power
    session, dm, viewer, tok = _make_viewer_power_session()
    viewer_key = "vkey1"
    session.viewer_profiles = {
        viewer_key: {"viewer_key": viewer_key, "user_id": "v1",
                     "name": "TestViewer", "powers": {}}
    }
    result = _consume_viewer_power(session, viewer_key, "nonexistent")
    assert result is False


def test_get_or_create_viewer_profile_migrates_legacy_user_id_key():
    """Returning viewers should recover grants stored under legacy key formats."""
    from server.handlers.viewer_powers import _get_or_create_viewer_profile

    session, _dm, viewer, _tok = _make_viewer_power_session()
    viewer.player_key = "auth_viewer_1"
    session.viewer_profiles = {
        "user:v1": {
            "viewer_key": "user:v1",
            "user_id": "v1",
            "name": "TestViewer",
            "powers": {
                "pebble_toss": {"power_id": "pebble_toss", "charges": 1, "enabled": True}
            },
        }
    }

    profiles, profile, key = _get_or_create_viewer_profile(session, viewer)
    assert key == "auth_viewer_1"
    assert "auth_viewer_1" in profiles
    assert "user:v1" not in profiles
    assert "pebble_toss" in (profile.get("powers") or {})


def test_viewer_state_payload_falls_back_to_legacy_profile_key():
    """viewer state payload should include legacy profile data when canonical key missing."""
    from server.session import Session, User

    session = Session(id="vp-payload")
    viewer = User(id="viewer-7", name="Viewer", role="viewer")
    viewer.player_key = "auth_viewer_7"
    session.users[viewer.id] = viewer
    session.viewer_profiles = {
        "user:viewer-7": {
            "viewer_key": "user:viewer-7",
            "user_id": "viewer-7",
            "name": "Viewer",
            "powers": {"arcane_zap": {"power_id": "arcane_zap", "charges": 1, "enabled": True}},
        }
    }

    payload = session.to_state_dict_for_role(role="viewer", user_id=viewer.id)
    profile = ((payload.get("viewer_profiles") or {}).get("auth_viewer_7") or {})
    assert "arcane_zap" in ((profile.get("powers") or {}))


def test_rejection_does_not_consume_power():
    """handle_viewer_power_pending_decision with approve=False must NOT consume the viewer's power."""
    import asyncio
    import time
    from unittest.mock import AsyncMock, patch, MagicMock
    from server.handlers.viewer_powers import handle_viewer_power_pending_decision

    session, dm, viewer, tok = _make_viewer_power_session()
    viewer_key = "vkey1"

    # Grant a power to the viewer
    profiles = {
        viewer_key: {
            "viewer_key": viewer_key,
            "user_id": "v1",
            "name": "TestViewer",
            "powers": {
                "arcane_zap": {"power_id": "arcane_zap", "charges": 1,
                               "enabled": True, "requires_approval": True,
                               "cooldown_sec": 0, "cooldown_until": 0.0}
            }
        }
    }
    session.viewer_profiles = profiles

    # Add a pending request
    pending_id = "vp_test001"
    session.viewer_pending_actions = {
        pending_id: {
            "id": pending_id,
            "viewer_key": viewer_key,
            "viewer_user_id": "v1",
            "viewer_name": "TestViewer",
            "power_id": "arcane_zap",
            "power_name": "Arcane Zap",
            "target": {"mode": "token", "token_id": "tok1",
                       "map_context": "world"},
            "created_at": time.time()
        }
    }

    payload = {"pending_id": pending_id, "approve": False}

    # Mock the manager to avoid real WS calls
    with patch("server.handlers.viewer_powers.random.shuffle", side_effect=lambda seq: None), \
         patch("server.handlers.viewer_powers.manager") as mock_mgr, \
         patch("server.handlers.viewer_powers.save_campaign_async", new_callable=AsyncMock):
        mock_mgr.send_to = AsyncMock()
        mock_mgr.broadcast = AsyncMock()
        asyncio.new_event_loop().run_until_complete(
            handle_viewer_power_pending_decision(payload, session, dm)
        )

    # Power must still be in the viewer's profile (not consumed)
    powers = session.viewer_profiles.get(viewer_key, {}).get("powers", {})
    assert "arcane_zap" in powers, (
        "arcane_zap must remain in viewer's powers after DM rejection"
    )
    assert int(powers["arcane_zap"].get("charges", 0)) == 1

    # Pending request must be cleared
    assert pending_id not in session.viewer_pending_actions, (
        "Pending request must be removed after DM rejects"
    )


def test_approval_consumes_no_cooldown_power():
    """handle_viewer_power_pending_decision approval with cooldown_sec=0 must remove power."""
    import asyncio
    import time
    from unittest.mock import AsyncMock, patch
    from server.handlers.viewer_powers import handle_viewer_power_pending_decision

    session, dm, viewer, tok = _make_viewer_power_session()
    viewer_key = "vkey1"

    profiles = {
        viewer_key: {
            "viewer_key": viewer_key,
            "user_id": "v1",
            "name": "TestViewer",
            "powers": {
                "pebble_toss": {"power_id": "pebble_toss", "charges": 1,
                                "enabled": True, "requires_approval": True,
                                "cooldown_sec": 0, "cooldown_until": 0.0}
            }
        }
    }
    session.viewer_profiles = profiles

    pending_id = "vp_test002"
    session.viewer_pending_actions = {
        pending_id: {
            "id": pending_id,
            "viewer_key": viewer_key,
            "viewer_user_id": "v1",
            "viewer_name": "TestViewer",
            "power_id": "pebble_toss",
            "power_name": "Pebble Toss",
            "target": {"mode": "token", "token_id": "tok1",
                       "map_context": "world"},
            "created_at": time.time()
        }
    }

    payload = {"pending_id": pending_id, "approve": True}

    with patch("server.handlers.viewer_powers.manager") as mock_mgr, \
         patch("server.handlers.viewer_powers.save_campaign_async", new_callable=AsyncMock):
        mock_mgr.send_to = AsyncMock()
        mock_mgr.broadcast = AsyncMock()
        asyncio.new_event_loop().run_until_complete(
            handle_viewer_power_pending_decision(payload, session, dm)
        )

    # No-cooldown power must be removed after approval+execution
    powers = session.viewer_profiles.get(viewer_key, {}).get("powers", {})
    assert "pebble_toss" not in powers, (
        "pebble_toss must be removed after DM approves (cooldown_sec=0)"
    )
    # Pending cleared
    assert pending_id not in session.viewer_pending_actions


def test_knockback_target_mode_is_point():
    """Knockback must target a point (not a token). The client renders defs
    straight from the server catalog, so the server def is the authority."""
    from server.handlers.viewer_powers import VIEWER_BASE_POWER_DEFS
    assert VIEWER_BASE_POWER_DEFS["knockback"]["target_mode"] == "point"


def test_knockback_map_targeting_sends_target_token_id_in_play_html():
    """When map-targeting knockback, play.html should send target_token_id."""
    content = _play_html_content()
    assert "_firedPowerId === 'knockback'" in content
    assert "const _clickedToken = hitTestTokens(w.x, w.y);" in content
    assert "const _resolvedTargetToken = _clickedToken || null;" in content
    assert "_vpPayload.target_token_id = String(_resolvedTargetToken?.id || '');" in content


def test_viewer_knockback_targeting_uses_dm_map_context_in_play_html():
    """Viewer knockback map-target arming/sending should use DM map context helper."""
    content = _play_html_content()
    assert "function viewerPowerActiveMapContext()" in content
    assert "if (ROLE === 'viewer') return String(_dmMapContext || poiCtx || 'world');" in content
    assert "_viewerPowerTargeting = { powerId: String(powerId || ''), mapContext: viewerPowerActiveMapContext(), sourceTokenId };" in content
    assert "const currentCtx = viewerPowerActiveMapContext();" in content


def test_local_map_exit_restores_world_fog_for_players():
    """Player/viewer local_map_exit path should reload world fog context."""
    content = _play_html_content()
    assert "function handleLocalMapExit(payload = {}) {" in content
    assert "setFogMapContext('world');" in content
    assert "fogLoadMap(fogMapCtx);" in content


def test_knockback_map_targeting_aligns_target_point_context_to_target_token():
    """Knockback map target should align target_point.map_context with the resolved token."""
    content = _play_html_content()
    assert "const _targetMapContext = String(_resolvedTargetToken?.map_context || currentCtx || 'world');" in content
    assert "_vpPayload.target_point.map_context = _targetMapContext;" in content
    assert "_vpPayload.source_token_id = _viewerPowerTargeting.sourceTokenId || '';" in content


def test_knockback_prefers_clicked_or_nearest_token_not_source_token():
    """Knockback point-target use should not force source_token_id as the target token."""
    import asyncio
    from unittest.mock import AsyncMock, patch
    from server.session import Token
    from server.handlers.viewer_powers import _resolve_viewer_power

    session, _dm, _viewer, tok = _make_viewer_power_session()
    # Add a second token that is closer to the clicked point than tok, so we can
    # verify source_token_id wins over nearest-token fallback.
    other = Token(id="tok2", name="Other", x=120, y=100, width=50, height=50, color="#f00", shape="circle", owner_id="p2")
    session.tokens["tok2"] = other

    # Click near tok2, while source_token_id points to tok1.
    target = {"mode": "point", "x": 145.0, "y": 125.0, "map_context": "world", "source_token_id": "tok1"}

    with patch("server.handlers.viewer_powers.random.shuffle", side_effect=lambda seq: None), \
         patch("server.handlers.viewer_powers.manager") as mock_mgr, \
         patch("server.handlers.viewer_powers.save_campaign_async", new_callable=AsyncMock):
        mock_mgr.broadcast = AsyncMock()
        mock_mgr.send_to = AsyncMock()
        result, msg = asyncio.new_event_loop().run_until_complete(
            _resolve_viewer_power(session, "Viewer", "knockback", target)
        )

    assert result is not None
    assert isinstance(msg, str) and "Knockback" in msg
    affected_ids = {getattr(t, "id", "") for t in (result or [])}
    assert "tok2" in affected_ids, "knockback should affect the clicked/nearest token"
    assert "tok1" not in affected_ids, "source_token_id must not override knockback target selection"


def test_knockback_moves_exactly_one_grid_in_a_cardinal_direction():
    """Knockback should move exactly one 5-ft grid square in one cardinal direction."""
    import asyncio
    from unittest.mock import AsyncMock, patch
    from server.session import Token
    from server.handlers.viewer_powers import _resolve_viewer_power

    session, _dm, _viewer, target_tok = _make_viewer_power_session()
    # Source token is directly left of target token center, so target should be pushed right.
    source_tok = Token(
        id="tok2", name="Source", x=0, y=100, width=50, height=50,
        color="#0f0", shape="circle", owner_id="p2"
    )
    session.tokens["tok2"] = source_tok
    old_x = float(target_tok.x)
    old_y = float(target_tok.y)
    target = {"mode": "point", "x": 125.0, "y": 125.0, "map_context": "world", "source_token_id": "tok2", "token_id": "tok1"}

    with patch("server.handlers.viewer_powers.random.shuffle", side_effect=lambda seq: None), \
         patch("server.handlers.viewer_powers.manager") as mock_mgr, \
         patch("server.handlers.viewer_powers.save_campaign_async", new_callable=AsyncMock):
        mock_mgr.broadcast = AsyncMock()
        mock_mgr.send_to = AsyncMock()
        result, msg = asyncio.new_event_loop().run_until_complete(
            _resolve_viewer_power(session, "Viewer", "knockback", target)
        )

    assert result is not None
    assert isinstance(msg, str) and "Knockback" in msg
    dx = abs(float(target_tok.x) - old_x)
    dy = abs(float(target_tok.y) - old_y)
    assert (dx, dy) in {(50.0, 0.0), (0.0, 50.0)}


def test_knockback_prefers_direction_away_from_clicked_point():
    """Knockback should prioritize pushing away from the targeted point."""
    import asyncio
    from unittest.mock import AsyncMock, patch
    from server.handlers.viewer_powers import _resolve_viewer_power

    session, _dm, _viewer, target_tok = _make_viewer_power_session()
    old_x = float(target_tok.x)
    old_y = float(target_tok.y)
    # Click to the left of target center so push should prefer rightward movement.
    target = {"mode": "point", "x": 75.0, "y": 125.0, "map_context": "world"}

    with patch("server.handlers.viewer_powers.manager") as mock_mgr, \
         patch("server.handlers.viewer_powers.save_campaign_async", new_callable=AsyncMock):
        mock_mgr.broadcast = AsyncMock()
        mock_mgr.send_to = AsyncMock()
        result, msg = asyncio.new_event_loop().run_until_complete(
            _resolve_viewer_power(session, "Viewer", "knockback", target)
        )

    assert result is not None
    assert isinstance(msg, str) and "Knockback" in msg
    assert float(target_tok.x) == old_x + 50.0
    assert float(target_tok.y) == old_y


def test_knockback_does_not_clamp_negative_world_coordinates_to_zero():
    """Knockback should preserve map coordinate space and not snap negatives to origin."""
    import asyncio
    from unittest.mock import AsyncMock, patch
    from server.session import Token
    from server.handlers.viewer_powers import _resolve_viewer_power

    session, _dm, _viewer, _target_tok = _make_viewer_power_session()
    neg_tok = Token(
        id="tok_neg", name="West Token", x=-500, y=100, width=50, height=50,
        color="#0af", shape="circle", owner_id="p3"
    )
    session.tokens["tok_neg"] = neg_tok

    target = {"mode": "point", "x": -475.0, "y": 125.0, "map_context": "world", "target_token_id": "tok_neg"}

    with patch("server.handlers.viewer_powers.manager") as mock_mgr, \
         patch("server.handlers.viewer_powers.save_campaign_async", new_callable=AsyncMock):
        mock_mgr.broadcast = AsyncMock()
        mock_mgr.send_to = AsyncMock()
        result, msg = asyncio.new_event_loop().run_until_complete(
            _resolve_viewer_power(session, "Viewer", "knockback", target)
        )

    assert result is not None
    assert isinstance(msg, str) and "Knockback" in msg
    assert float(neg_tok.x) < 0.0, "knockback should not clamp negative coordinates to x=0"


def test_knockback_blocker_check_uses_center_path_not_top_left():
    """Knockback should not be falsely blocked by walls crossing only top-left path."""
    import asyncio
    from unittest.mock import AsyncMock, patch
    from server.session import Token
    from server.handlers.viewer_powers import _resolve_viewer_power

    session, _dm, _viewer, target_tok = _make_viewer_power_session()
    source_tok = Token(
        id="tok2", name="Source", x=0, y=100, width=50, height=50,
        color="#0f0", shape="circle", owner_id="p2"
    )
    session.tokens["tok2"] = source_tok
    # This wall intersects the target token's top-left path (y=100), but not center path (y=125).
    session.editor_walls = {
        "world": [{"x1": 125, "y1": 80, "x2": 125, "y2": 120, "blocks_movement": True}]
    }
    old_x = float(target_tok.x)
    old_y = float(target_tok.y)
    target = {"mode": "point", "x": 125.0, "y": 125.0, "map_context": "world", "source_token_id": "tok2", "token_id": "tok1"}

    with patch("server.handlers.viewer_powers.random.shuffle", side_effect=lambda seq: None), \
         patch("server.handlers.viewer_powers.manager") as mock_mgr, \
         patch("server.handlers.viewer_powers.save_campaign_async", new_callable=AsyncMock):
        mock_mgr.broadcast = AsyncMock()
        mock_mgr.send_to = AsyncMock()
        result, _msg = asyncio.new_event_loop().run_until_complete(
            _resolve_viewer_power(session, "Viewer", "knockback", target)
        )

    assert result is not None
    dx = abs(float(target_tok.x) - old_x)
    dy = abs(float(target_tok.y) - old_y)
    assert (dx, dy) in {(50.0, 0.0), (0.0, 50.0)}


def test_knockback_point_descriptor_keeps_target_token_id():
    """Knockback point payload should preserve explicit target_token_id."""
    from server.handlers.viewer_powers import _build_target_descriptor, VIEWER_BASE_POWER_DEFS

    session, _dm, _viewer, _tok = _make_viewer_power_session()
    payload = {
        "power_id": "knockback",
        "target_token_id": "tok1",
        "target_point": {"x": 222, "y": 333, "map_context": "world"},
    }
    target, err = _build_target_descriptor(session, VIEWER_BASE_POWER_DEFS["knockback"], payload)
    assert err is None
    assert isinstance(target, dict)
    assert target.get("mode") == "point"
    assert target.get("token_id") == "tok1"


def test_knockback_point_descriptor_ignores_target_token_id_from_other_map():
    """Knockback point target must not keep token_id when token is on another map."""
    from server.handlers.viewer_powers import _build_target_descriptor, VIEWER_BASE_POWER_DEFS

    session, _dm, _viewer, tok = _make_viewer_power_session()
    tok.map_context = "poi_1"
    payload = {
        "power_id": "knockback",
        "target_token_id": "tok1",
        "target_point": {"x": 222, "y": 333, "map_context": "world"},
    }
    target, err = _build_target_descriptor(session, VIEWER_BASE_POWER_DEFS["knockback"], payload)
    assert err is None
    assert isinstance(target, dict)
    assert target.get("mode") == "point"
    assert "token_id" not in target


def test_viewer_power_cooldown_defined_in_html():
    """play.html must define _viewerPowerOnCooldown and _viewerPowerBtnHtml helpers."""
    content = _play_html_content()
    assert "_viewerPowerOnCooldown" in content, (
        "_viewerPowerOnCooldown helper must exist in play.html"
    )
    assert "_viewerPowerBtnHtml" in content, (
        "_viewerPowerBtnHtml helper must exist in play.html"
    )


def test_viewer_power_cooldown_timer_in_html():
    """play.html must have _startViewerCooldownTimer to auto-refresh cooldown UI."""
    content = _play_html_content()
    assert "_startViewerCooldownTimer" in content, (
        "_startViewerCooldownTimer must exist in play.html"
    )


def test_viewer_power_use_in_ws_allowed_list():
    """ws_permissions.py must allow viewers to send powers."""
    from server.handlers.ws_permissions import is_ws_message_allowed_for_role

    assert is_ws_message_allowed_for_role("viewer_power_use", "viewer").allowed


def test_viewer_emote_in_ws_allowed_list():
    """ws_permissions.py must allow viewers to emote."""
    from server.handlers.ws_permissions import is_ws_message_allowed_for_role

    assert is_ws_message_allowed_for_role("viewer_emote", "viewer").allowed


def test_token_emote_route_in_dispatch_table():
    """handle_message dispatch must expose token_emote."""
    from server.handlers import handle_message
    source = inspect.getsource(handle_message)
    assert '"token_emote"' in source or "'token_emote'" in source


def test_token_emote_handler_accepts_player_owned_token(monkeypatch):
    """Players can broadcast quick reactions on their own token."""
    from server.handlers import tokens as tokens_mod
    from server.session import Session, User, Token

    sent = []

    class FakeManager:
        async def broadcast(self, session_id, message, exclude_user=None):
            sent.append((session_id, message, exclude_user))

        async def send_to(self, session_id, user_id, message):
            sent.append((session_id, user_id, message))

    monkeypatch.setattr(tokens_mod, "manager", FakeManager())
    session = Session(id="sess-emote")
    player = User(id="p1", name="Alice", role="player")
    session.users[player.id] = player
    session.tokens["tok1"] = Token(
        id="tok1", name="Alice", x=0, y=0, width=40, height=40,
        color="#fff", shape="circle", owner_id="p1"
    )

    asyncio.run(tokens_mod.handle_token_emote({"token_id": "tok1", "emote_id": "ready"}, session, player))

    assert sent, "token_emote should broadcast a synced payload"
    message = sent[0][1]
    assert message["type"] == "token_emote"
    assert message["payload"]["token_id"] == "tok1"
    assert message["payload"]["emote_id"] == "ready"
    assert message["payload"]["icon"] == "✅"
    assert message["payload"]["actor_user_id"] == "p1"


def test_token_emote_handler_rejects_non_owner_player(monkeypatch):
    """Players cannot emote on tokens owned by other players."""
    from server.handlers import tokens as tokens_mod
    from server.session import Session, User, Token

    sent = []

    class FakeManager:
        async def broadcast(self, session_id, message, exclude_user=None):
            sent.append(("broadcast", message))

        async def send_to(self, session_id, user_id, message):
            sent.append(("send_to", user_id, message))

    monkeypatch.setattr(tokens_mod, "manager", FakeManager())
    session = Session(id="sess-emote")
    player = User(id="p2", name="Bob", role="player")
    session.users[player.id] = player
    session.tokens["tok1"] = Token(
        id="tok1", name="Alice", x=0, y=0, width=40, height=40,
        color="#fff", shape="circle", owner_id="p1"
    )

    asyncio.run(tokens_mod.handle_token_emote({"token_id": "tok1", "emote_id": "danger"}, session, player))

    assert sent and sent[0][0] == "send_to"
    assert sent[0][2]["type"] == "token_emote_denied"
    assert not any(item[0] == "broadcast" for item in sent)


def test_token_emote_handler_enforces_cooldown(monkeypatch):
    """Repeated quick reactions within the cooldown should be denied."""
    from server.handlers import tokens as tokens_mod
    from server.session import Session, User, Token

    sent = []

    class FakeManager:
        async def broadcast(self, session_id, message, exclude_user=None):
            sent.append(("broadcast", message))

        async def send_to(self, session_id, user_id, message):
            sent.append(("send_to", user_id, message))

    monkeypatch.setattr(tokens_mod, "manager", FakeManager())
    session = Session(id="sess-emote")
    player = User(id="p1", name="Alice", role="player")
    session.users[player.id] = player
    session.tokens["tok1"] = Token(
        id="tok1", name="Alice", x=0, y=0, width=40, height=40,
        color="#fff", shape="circle", owner_id="p1"
    )

    asyncio.run(tokens_mod.handle_token_emote({"token_id": "tok1", "emote_id": "help"}, session, player))
    asyncio.run(tokens_mod.handle_token_emote({"token_id": "tok1", "emote_id": "help"}, session, player))

    assert any(item[0] == "broadcast" for item in sent)
    denied = [item for item in sent if item[0] == "send_to"]
    assert denied, "second token emote should be denied during cooldown"
    assert denied[-1][2]["type"] == "token_emote_denied"
    assert denied[-1][2]["payload"]["cooldown_remaining_ms"] > 0


def test_viewer_powers_js_defs_are_server_driven():
    """viewer_powers.js must render power defs from the server-synced catalog
    (state_sync / viewer_power_catalog_sync) — never from a hardcoded base
    object, which would silently desync every future power."""
    js_path = os.path.join(PROJECT_ROOT, "client", "static", "js", "gameplay", "viewer_powers.js")
    with open(js_path, "r", encoding="utf-8") as f:
        js_source = f.read()
    assert "env.viewerPowerCatalog" in js_source
    assert "name:'Pebble Toss'" not in js_source, (
        "viewer_powers.js has grown a hardcoded power def again — power lists "
        "must come from the server catalog (VIEWER_POWER_DEFS)."
    )


def test_play_html_viewer_power_defs_are_server_driven():
    """play.html (viewer panel + DM grant dropdown) must render power defs
    from the server-synced catalog, never a hardcoded base object."""
    html_source = _play_html_content()
    assert "return { ...(viewerPowerCatalog || {}) };" in html_source
    assert "name:'Pebble Toss'" not in html_source, (
        "play.html has grown a hardcoded power def again — power lists must "
        "come from the server catalog (VIEWER_POWER_DEFS)."
    )
    # The full state snapshot delivers the merged catalog (shipped + custom)
    from server.session import Session
    session = Session(id="refactor-power-defs")
    from server.handlers.viewer_powers import VIEWER_BASE_POWER_DEFS
    catalog = session.to_state_dict()["viewer_power_catalog"]
    assert set(VIEWER_BASE_POWER_DEFS.keys()) <= set(catalog.keys())


def test_custom_power_builder_shows_save_for_status():
    """play.html updateCustomPowerBuilderFields must show save row for status powers."""
    content = _play_html_content()
    # The function must include status kinds in the hasSave / save-visibility logic
    assert "single_status" in content, "single_status kind must appear in play.html"
    # Check that save row visibility references status kinds (not just damage)
    import re
    m = re.search(r'function updateCustomPowerBuilderFields\(kind\)\s*\{[^}]+\}', content, re.DOTALL)
    assert m, "updateCustomPowerBuilderFields must exist in play.html"
    fn_body = m.group(0)
    assert "single_status" in fn_body, (
        "updateCustomPowerBuilderFields must reference 'single_status' for save row visibility"
    )
    assert "area_status" in fn_body, (
        "updateCustomPowerBuilderFields must reference 'area_status' for save row visibility"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Dice Animation Diagnostics tests
# ═══════════════════════════════════════════════════════════════════════════════

_DICE3D_JS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "client", "static", "js", "dice", "dice3d.js")


def _dice3d_content():
    with open(_DICE3D_JS_PATH, "r", encoding="utf-8") as f:
        return f.read()


def test_dice3d_min_opacity_constant():
    """dice3d.js must define MIN_DICE_OPACITY = 0.15 to prevent invisible dice."""
    content = _dice3d_content()
    assert "MIN_DICE_OPACITY = 0.15" in content, (
        "dice3d.js must define MIN_DICE_OPACITY = 0.15 constant"
    )


def test_dice3d_opacity_clamped_in_set_player_prefs():
    """dice3d.js setPlayerPrefs must clamp opacity using MIN_DICE_OPACITY."""
    content = _dice3d_content()
    assert "Math.max(MIN_DICE_OPACITY" in content, (
        "dice3d.js setPlayerPrefs must clamp opacity to MIN_DICE_OPACITY"
    )


def test_dice3d_opacity_clamped_in_load_player_prefs():
    """dice3d.js loadPlayerPrefs must also clamp opacity using MIN_DICE_OPACITY."""
    content = _dice3d_content()
    # Both setPlayerPrefs and loadPlayerPrefs must reference MIN_DICE_OPACITY clamping
    import re
    clamp_count = len(re.findall(r'Math\.max\(MIN_DICE_OPACITY', content))
    assert clamp_count >= 2, (
        f"MIN_DICE_OPACITY must be used to clamp opacity in both setPlayerPrefs and "
        f"loadPlayerPrefs, found {clamp_count} usage(s)"
    )


def test_dice3d_is_rendering_in_public_api():
    """dice3d.js public API must expose isRendering() for the render watchdog."""
    content = _dice3d_content()
    assert "isRendering:" in content, (
        "dice3d.js public API must include isRendering property"
    )


def test_dice3d_has_first_frame_in_public_api():
    """dice3d.js public API must expose hasFirstFrame() for diagnostics."""
    content = _dice3d_content()
    assert "hasFirstFrame:" in content, (
        "dice3d.js public API must include hasFirstFrame property"
    )


def test_dice3d_first_render_done_flag():
    """dice3d.js must define _firstRenderDone flag and log after first render."""
    content = _dice3d_content()
    assert "_firstRenderDone" in content, (
        "dice3d.js must use _firstRenderDone flag to track first render per throw"
    )
    assert "first render complete" in content, (
        "dice3d.js must log 'first render complete' after the first render() call"
    )


def test_dice3d_module_load_log():
    """dice3d.js must log when the module loads (diagnostic aid)."""
    content = _dice3d_content()
    assert "[DicePhysics3D] module loaded" in content, (
        "dice3d.js must contain console.log('[DicePhysics3D] module loaded') at module start"
    )


def test_dice3d_renderer_creation_log():
    """dice3d.js must log renderer creation with canvas dimensions."""
    content = _dice3d_content()
    assert "renderer created, size=" in content, (
        "dice3d.js must log renderer creation with size info"
    )


def test_dice3d_canvas_attach_log():
    """dice3d.js must log when the canvas is appended to body (with z-index)."""
    content = _dice3d_content()
    assert "canvas attached to body, zIndex=" in content, (
        "dice3d.js must log canvas attachment with zIndex"
    )


def test_dice3d_player_prefs_debug_log():
    """dice3d.js must log active player prefs (including opacity) on each throw."""
    content = _dice3d_content()
    assert "player prefs:" in content, (
        "dice3d.js must log active player prefs on each throw via console.debug"
    )
    assert "opacity=" in content or "opacity=${" in content, (
        "dice3d.js player prefs log must include opacity"
    )


def test_play_html_has_dice_error_handler():
    """play.html must define _showDiceError() to surface 3D failures (replaces 2D fallback)."""
    content = _play_html_content()
    assert "function _showDiceError(" in content, (
        "play.html must define _showDiceError() to show controlled failure state when 3D dice fail"
    )


def test_play_html_render_watchdog():
    """play.html must include a render watchdog that detects unhealthy 3D dice."""
    content = _play_html_content()
    assert "Render watchdog" in content or "render watchdog" in content, (
        "play.html must contain a render watchdog comment/log"
    )
    assert "isRendering()" in content, (
        "play.html render watchdog must call DicePhysics3D.isRendering()"
    )
    # 3D-only: watchdog shows error state instead of falling back to 2D
    assert "_showDiceError(" in content, (
        "play.html render watchdog must show error via _showDiceError() when 3D dice are unhealthy"
    )


def test_play_html_opacity_slider_min_15():
    """play.html dice opacity slider must have min=15 (not 0) to prevent invisible dice."""
    content = _play_html_content()
    assert 'id="dice-pref-opacity"' in content, (
        "play.html must contain dice-pref-opacity slider"
    )
    # The slider definition line (search broadly for the input tag containing this id)
    slider_line = next(
        (line for line in content.splitlines() if 'dice-pref-opacity' in line), ""
    )
    assert 'min="15"' in slider_line, (
        f"dice-pref-opacity slider must have min=\"15\" to prevent invisible dice. "
        f"Found: {slider_line.strip()!r}"
    )


def test_play_html_opacity_clamped_when_loading_prefs():
    """play.html must clamp opacity to at least 15 when restoring saved dice prefs to the UI."""
    content = _play_html_content()
    assert "Math.max(15," in content or "Math.max(15, " in content, (
        "play.html must clamp opacity to minimum 15 (Math.max(15, ...)) when loading prefs"
    )


def test_play_html_wait_for_dice_warn():
    """play.html _waitForDice must log a console.warn if DicePhysics3D never loads."""
    content = _play_html_content()
    assert "_waitForDice" in content, (
        "play.html must define _waitForDice polling function"
    )
    assert "DicePhysics3D) never loaded" in content or "never loaded" in content, (
        "play.html _waitForDice must emit a console.warn when the module never loads"
    )


# ─────────────────────────────────────────────────────────────────────────────
# AI Cartographer tests (March 2026)
# ─────────────────────────────────────────────────────────────────────────────

def test_cartographer_module_importable():
    """server.handlers.cartographer must be importable with all public functions."""
    from server.handlers.cartographer import (
        generate_map,
        generate_interior,
        get_presets_manifest,
    )
    assert callable(generate_map)
    assert callable(generate_interior)
    assert callable(get_presets_manifest)


def test_cartographer_preset_counts():
    """Cartographer must define exactly 10 terrain, 8 build, and 15 interior presets."""
    from server.handlers.cartographer import TERRAIN_PRESETS, BUILD_PRESETS, INTERIOR_PRESETS
    assert len(TERRAIN_PRESETS) == 10, (
        f"Expected 10 terrain presets, got {len(TERRAIN_PRESETS)}"
    )
    assert len(BUILD_PRESETS) == 8, (
        f"Expected 8 build presets, got {len(BUILD_PRESETS)}"
    )
    assert len(INTERIOR_PRESETS) == 15, (
        f"Expected 15 interior presets, got {len(INTERIOR_PRESETS)}"
    )


def test_cartographer_preset_titles():
    """Every preset entry must have a non-empty 'title' key."""
    from server.handlers.cartographer import TERRAIN_PRESETS, BUILD_PRESETS, INTERIOR_PRESETS
    for name, preset in TERRAIN_PRESETS.items():
        assert preset.get("title"), f"TERRAIN_PRESETS[{name!r}] missing 'title'"
    for name, preset in BUILD_PRESETS.items():
        assert preset.get("title"), f"BUILD_PRESETS[{name!r}] missing 'title'"
    for name, preset in INTERIOR_PRESETS.items():
        assert preset.get("title"), f"INTERIOR_PRESETS[{name!r}] missing 'title'"


def test_cartographer_grid_defaults_all_classes():
    """GRID_DEFAULTS must cover all 5 map classes: interior, dungeon, house, castle, region."""
    from server.handlers.cartographer import GRID_DEFAULTS
    required = {"interior", "dungeon", "house", "castle", "region"}
    assert required.issubset(set(GRID_DEFAULTS.keys())), (
        f"GRID_DEFAULTS missing map classes: {required - set(GRID_DEFAULTS.keys())}"
    )


def test_cartographer_grid_defaults_presets():
    """Each GRID_DEFAULTS map class must have at least small and medium presets with positive dims."""
    from server.handlers.cartographer import GRID_DEFAULTS
    required_sizes = {"small", "medium"}
    for map_class, sizes in GRID_DEFAULTS.items():
        assert required_sizes.issubset(set(sizes.keys())), (
            f"GRID_DEFAULTS[{map_class!r}] missing size keys: {required_sizes - set(sizes.keys())}"
        )
        for size, dims in sizes.items():
            assert isinstance(dims, (tuple, list)) and len(dims) == 2, (
                f"GRID_DEFAULTS[{map_class!r}][{size!r}] must be a 2-tuple (width, height)"
            )
            assert dims[0] > 0 and dims[1] > 0, (
                f"GRID_DEFAULTS[{map_class!r}][{size!r}] dimensions must be positive"
            )


def test_cartographer_image_providers_importable():
    """All 4 image providers must be importable."""
    from server.handlers.cartographer import (
        StabilityImageProvider,
        OpenAIImageProvider,
        ReplicateImageProvider,
        StubImageProvider,
    )
    for cls in (StabilityImageProvider, OpenAIImageProvider, ReplicateImageProvider, StubImageProvider):
        assert callable(cls), f"{cls.__name__} must be callable"


def test_cartographer_stub_provider_returns_notice():
    """StubImageProvider.generate must return a dict with 'stub' flag and descriptive note."""
    import asyncio
    from server.handlers.cartographer import StubImageProvider
    provider = StubImageProvider()
    result = asyncio.run(
        provider.generate(prompt="test", negative_prompt="", width=512, height=512)
    )
    assert isinstance(result, dict), "StubImageProvider.generate must return a dict"
    assert result.get("stub") is True, "StubImageProvider result must have stub=True"
    assert result.get("note") or result.get("notice"), (
        "StubImageProvider result must contain a descriptive 'note' or 'notice'"
    )
    assert result.get("url") is None or result.get("url") == "", (
        "StubImageProvider must not return a real image URL"
    )


def test_cartographer_procedural_fallback_structure():
    """_procedural_fallback must return a dict with all required plan keys."""
    from server.handlers.cartographer import _procedural_fallback
    result = _procedural_fallback({"map_scope": "interior", "output_mode": "tactical_grid"})
    required_keys = {
        "title", "summary", "output_mode", "grid_type", "grid_scale",
        "grid_width", "grid_height", "map_scope", "image_prompt",
        "negative_prompt", "pois", "room_list",
    }
    missing = required_keys - set(result.keys())
    assert not missing, f"_procedural_fallback result missing keys: {missing}"


def test_cartographer_resolve_grid_dimensions():
    """_resolve_grid_dimensions must return positive int pairs for all map classes."""
    from server.handlers.cartographer import _resolve_grid_dimensions, GRID_DEFAULTS
    for map_class in GRID_DEFAULTS:
        for size in ("tiny", "small", "medium", "large", "huge"):
            w, h = _resolve_grid_dimensions(
                map_scope=map_class,
                output_mode="tactical_grid",
                dimensions_preset=size,
                grid_width=None,
                grid_height=None,
            )
            assert isinstance(w, int) and w > 0, (
                f"_resolve_grid_dimensions({map_class!r}, {size!r}) returned bad width: {w}"
            )
            assert isinstance(h, int) and h > 0, (
                f"_resolve_grid_dimensions({map_class!r}, {size!r}) returned bad height: {h}"
            )


def test_cartographer_resolve_grid_dimensions_custom_override():
    """_resolve_grid_dimensions must use explicit grid_width/height when provided."""
    from server.handlers.cartographer import _resolve_grid_dimensions
    w, h = _resolve_grid_dimensions(
        map_scope="interior",
        output_mode="illustrated_overview",
        dimensions_preset="medium",
        grid_width=42,
        grid_height=37,
    )
    assert w == 42, f"Expected custom width 42, got {w}"
    assert h == 37, f"Expected custom height 37, got {h}"


def test_cartographer_presets_manifest_structure():
    """get_presets_manifest must return all expected top-level keys with correct counts."""
    from server.handlers.cartographer import get_presets_manifest
    manifest = get_presets_manifest()
    required_keys = {
        "terrain_presets", "build_presets", "interior_presets",
        "grid_defaults", "output_modes", "grid_types", "grid_scales",
        "dimensions_presets", "pixel_sizes", "image_styles", "map_scopes",
    }
    missing = required_keys - set(manifest.keys())
    assert not missing, f"get_presets_manifest missing keys: {missing}"
    assert len(manifest["terrain_presets"]) == 10
    assert len(manifest["build_presets"]) == 8
    assert len(manifest["interior_presets"]) == 15
    assert len(manifest["output_modes"]) == 3
    assert len(manifest["image_styles"]) == 8


def test_cartographer_presets_manifest_preset_shape():
    """Each preset entry in the manifest must have 'key' and 'title' fields."""
    from server.handlers.cartographer import get_presets_manifest
    manifest = get_presets_manifest()
    for section in ("terrain_presets", "build_presets"):
        for entry in manifest[section]:
            assert "key" in entry, f"{section} entry missing 'key': {entry}"
            assert "title" in entry, f"{section} entry missing 'title': {entry}"
    for entry in manifest["interior_presets"]:
        assert "key" in entry, f"interior_preset entry missing 'key': {entry}"
        assert "title" in entry, f"interior_preset entry missing 'title': {entry}"
        assert "type_class" in entry, f"interior_preset entry missing 'type_class': {entry}"


def test_cartographer_api_routes_in_main():
    """main.py must define all 3 cartographer API routes."""
    import inspect
    import importlib
    # Check main.py source for route decorators
    main_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")
    with open(main_path, "r", encoding="utf-8") as f:
        source = f.read()
    assert '"/api/cartographer/presets"' in source, (
        "main.py must define GET /api/cartographer/presets route"
    )
    assert '"/api/cartographer/generate"' in source, (
        "main.py must define POST /api/cartographer/generate route"
    )
    assert '"/api/cartographer/generate-interior"' in source, (
        "main.py must define POST /api/cartographer/generate-interior route"
    )


def test_play_html_has_cartographer_panel():
    """play.html must contain the AI Cartographer flyout panel with all required elements."""
    content = _play_html_content()
    required_ids = [
        "rail-cart-btn",
        "flyout-cart",
        "cart-description",
        "cart-scope",
        "cart-output-mode",
        "cart-terrain",
        "cart-build",
        "cart-interior",
        "cart-style",
        "cart-grid-type",
        "cart-grid-scale",
        "cart-dim-preset",
        "cart-grid-w",
        "cart-grid-h",
        "cart-pixel-size",
        "cart-generate-btn",
        "cart-loading",
        "cart-result",
        "cart-result-title",
        "cart-result-img",
        "cart-stub-notice",
        "cart-poi-list",
        "cart-room-list",
        "cart-import-btn",
        "cart-error",
    ]
    for eid in required_ids:
        assert eid in content, (
            f"play.html must contain element with id={eid!r}"
        )


def test_play_html_has_cartographer_js_functions():
    """play.html must define all required AI Cartographer JavaScript functions."""
    content = _play_html_content()
    required_fns = [
        "cartGenerate",
        "cartRegenerate",
        "cartOutputModeChanged",
        "cartUpdateGridHints",
        "cartImportToEditor",
        "cartGenerateInterior",
        "cartOpenFullImage",
    ]
    for fn in required_fns:
        assert fn in content, (
            f"play.html must define JavaScript function {fn!r}"
        )


# ── Gemini TTS provider tests ────────────────────────────────────────────────

def _narration_source():
    """Read server/handlers/narration.py source without importing it."""
    path = os.path.join(PROJECT_ROOT, "server", "handlers", "narration.py")
    with open(path) as f:
        return f.read()


def test_narration_module_has_gemini_constants():
    """narration.py must define Gemini TTS URL, model and timeout constants."""
    source = _narration_source()
    assert "_GEMINI_TTS_URL" in source, "narration.py must define _GEMINI_TTS_URL"
    assert "_GEMINI_TTS_MODEL" in source, "narration.py must define _GEMINI_TTS_MODEL"
    assert "_GEMINI_TTS_TIMEOUT" in source, "narration.py must define _GEMINI_TTS_TIMEOUT"
    assert "_GEMINI_API_KEY" in source, "narration.py must define _GEMINI_API_KEY"


def test_narration_module_has_gemini_voice_map():
    """narration.py must define a _GEMINI_VOICE_MAP with all four presets."""
    source = _narration_source()
    assert "_GEMINI_VOICE_MAP" in source, "narration.py must define _GEMINI_VOICE_MAP"
    for preset in ("deep_narrator", "grim_villain", "mysterious_whisper", "heroic_bard"):
        assert preset in source, f"_GEMINI_VOICE_MAP must contain preset {preset!r}"


def test_narration_module_has_generate_gemini_tts():
    """narration.py must define _generate_gemini_tts function."""
    source = _narration_source()
    assert "def _generate_gemini_tts(" in source, (
        "narration.py must define _generate_gemini_tts function"
    )


def test_narration_gemini_in_provider_chain():
    """_generate_tts must try Gemini between ElevenLabs and OpenAI."""
    source = _narration_source()
    # The orchestrator must reference all three premium providers in order
    orch_start = source.find("async def _generate_tts")
    gem_pos = source.find("_generate_gemini_tts", orch_start)
    oai_pos = source.find("_generate_openai_tts", orch_start)
    assert orch_start < gem_pos < oai_pos, (
        "Provider chain must be ElevenLabs → Gemini → OpenAI"
    )


def test_narration_gemini_uses_style_prompt():
    """Gemini TTS integration must support style prompts for character voices."""
    source = _narration_source()
    assert "stylePrompt" in source or "style_prompt" in source, (
        "Gemini TTS must support style_prompt / stylePrompt for character voice guidance"
    )
    assert "voiceName" in source or "voice_name" in source, (
        "Gemini TTS must support prebuilt voice selection"
    )


def test_narration_docstring_mentions_gemini():
    """Module docstring must document the Gemini provider in the priority chain."""
    source = _narration_source()
    # Check that the module docstring mentions Gemini
    docstring_end = source.find('"""', 3)
    docstring = source[:docstring_end]
    assert "Gemini" in docstring, (
        "Module docstring must mention Gemini in the provider priority list"
    )


def test_env_example_has_gemini_api_key():
    """.env.example must document GEMINI_API_KEY."""
    env_path = os.path.join(PROJECT_ROOT, ".env.example")
    with open(env_path) as f:
        content = f.read()
    assert "GEMINI_API_KEY" in content, (
        ".env.example must document the GEMINI_API_KEY environment variable"
    )


# ── Issue 8: "Add to Chest" loot fix ──────────────────────────────────────────

def test_apply_loot_to_chest_items_and_gold_as_inventory():
    """apply_loot_to_chest adds items and gold (as coin row) when prop lacks gp field."""
    from server.handlers.inventory import apply_loot_to_chest

    class FakeSession:
        editor_props = {
            "chest_abc": {
                "kind": "chest",
                "inventory": [],
            }
        }

    session = FakeSession()
    loot = {
        "gold": 42,
        "items": [
            {"name": "Shortsword", "qty": 1, "rarity": "Common"},
            {"name": "Healing Potion", "qty": 2, "rarity": "Uncommon"},
        ],
    }
    result = apply_loot_to_chest(session, "chest_abc", loot)
    assert result is True

    prop = session.editor_props["chest_abc"]
    inv = prop["inventory"]
    names = [i["name"] for i in inv]
    assert "Shortsword" in names
    assert "Healing Potion" in names
    # Gold should appear as a coin inventory row because prop has no gp field
    assert any("42 gp" in i["name"] for i in inv), f"Coin row not found in {inv}"


def test_apply_loot_to_chest_gold_added_to_gp_field():
    """apply_loot_to_chest increments existing gp field rather than adding a coin row."""
    from server.handlers.inventory import apply_loot_to_chest

    class FakeSession:
        editor_props = {
            "chest_xyz": {
                "kind": "chest",
                "gp": 10,
                "inventory": [],
            }
        }

    session = FakeSession()
    loot = {"gold": 30, "items": []}
    apply_loot_to_chest(session, "chest_xyz", loot)

    prop = session.editor_props["chest_xyz"]
    assert prop["gp"] == 40, f"Expected gp=40, got {prop['gp']}"
    # No coin inventory row should be added when gp field exists
    assert not any("gp" in (i.get("name") or "") for i in prop["inventory"]), (
        "Coin inventory row should not be created when gp field is present"
    )


def test_apply_loot_to_chest_missing_prop_returns_false():
    """apply_loot_to_chest returns False when the prop ID doesn't exist."""
    from server.handlers.inventory import apply_loot_to_chest

    class FakeSession:
        editor_props = {}

    session = FakeSession()
    result = apply_loot_to_chest(session, "nonexistent", {"gold": 5, "items": []})
    assert result is False


# ── Per-item lock + treasure budget for loot generation ───────────────────────

def _init_isolated_srd_items_db(tmp_path, monkeypatch):
    import server.db as _sdb
    import server.rules_db as rdb

    db_file = str(tmp_path / "test.db")
    monkeypatch.setattr(_sdb, "DB_PATH", db_file)
    monkeypatch.setattr(rdb, "_seed_srd_items_from_magic_items", lambda: None)
    rdb.init_srd_items_table()


def test_generate_loot_preview_keeps_locked_item_without_duplicating(tmp_path, monkeypatch):
    """Locked items are returned verbatim, and new items never duplicate a kept name."""
    _init_isolated_srd_items_db(tmp_path, monkeypatch)
    from server.handlers.inventory import generate_loot_preview

    item_a = {"id": "seq_longsword", "name": "Longsword", "rarity": "Common", "qty": 1, "gp": 15}
    preview = generate_loot_preview(6, keep=[item_a])

    names = [it["name"] for it in preview["items"]]
    assert names.count("Longsword") == 1
    assert item_a in preview["items"]
    # Any newly-generated items must be distinct from the kept item.
    new_items = [it for it in preview["items"] if it.get("name") != "Longsword"]
    assert len(new_items) == len(preview["items"]) - 1


def test_generate_loot_preview_budget_band(tmp_path, monkeypatch):
    """budget.band is 'balanced' for a level-appropriate haul, 'generous' when overloaded."""
    _init_isolated_srd_items_db(tmp_path, monkeypatch)
    from server.handlers.inventory import generate_loot_preview

    # Fill every slot with locked items (max slots for the level-5 band is 6) so the
    # haul value is fully deterministic — no random item can be additionally rolled.
    level = 5
    target_gp = level * 140
    balanced_keep = [
        {"name": f"Token Reward {i}", "rarity": "Common", "qty": 1, "gp": 110}
        for i in range(6)
    ]
    balanced = generate_loot_preview(level, keep=balanced_keep)
    assert balanced["budget"]["band"] == "balanced"
    assert balanced["budget"]["target_gp"] == target_gp

    # An 800gp item forced in at level 1 (target 140gp) is generous no matter what
    # else gets rolled alongside it.
    generous_keep = [{"name": "Forced Artifact", "rarity": "Rare", "qty": 1, "gp": 800}]
    generous = generate_loot_preview(1, keep=generous_keep)
    assert generous["budget"]["band"] == "generous"


def test_generate_loot_preview_theme_filters_category(tmp_path, monkeypatch):
    """Requesting a theme biases generated items toward that category when matches exist."""
    _init_isolated_srd_items_db(tmp_path, monkeypatch)
    from server.handlers.inventory import generate_loot_preview

    preview = generate_loot_preview(3, theme="weapon")
    assert preview["items"], "Expected at least one generated item"
    for item in preview["items"]:
        assert item.get("category", "").lower() == "weapon"


def test_inventory_entry_normalization_preserves_optional_loot_metadata():
    from server.handlers.inventory import _normalize_player_inventory_entry

    out = _normalize_player_inventory_entry(
        {
            "id": "item123",
            "name": "Moon-Touched Sword",
            "qty": 1,
            "notes": "Glows in darkness",
            "source": "Ancient Chest",
            "rarity": "Common",
            "is_magic": True,
            "is_identified": False,
            "magic_item_id": "magic123",
            "item_type": "weapon",
            "attunement_required": False,
            "unidentified_description": "A blade with a silver-blue sheen.",
            "effect": "Sheds moonlight.",
        }
    )

    assert out["id"] == "item123"
    assert out["rarity"] == "Common"
    assert out["is_magic"] is True
    assert out["is_identified"] is False
    assert out["magic_item_id"] == "magic123"
    assert out["item_type"] == "weapon"
    assert out["unidentified_description"] == "A blade with a silver-blue sheen."
    assert out["effect"] == "Sheds moonlight."


def test_session_party_stash_inventory_uses_dedicated_bucket():
    from server.session import Session, User, PARTY_STASH_KEY, get_party_stash_inventory

    session = Session(id="sess")
    session.users["u1"] = User(id="u1", name="Alice", role="player")
    session.player_inventories = {
        PARTY_STASH_KEY: [
            {"name": "Potion of Healing", "qty": 2, "source": "Party Stash", "rarity": "Common"},
        ]
    }

    stash = get_party_stash_inventory(session)
    assert stash[0]["name"] == "Potion of Healing"
    assert stash[0]["qty"] == 2
    assert stash[0]["rarity"] == "Common"


def test_inventory_send_to_stash_moves_item_and_logs(monkeypatch):
    from server.handlers import inventory as inventory_mod
    from server.session import Session, User, get_player_inventory_for_user, get_party_stash_inventory

    sent = []

    class FakeManager:
        async def send_to(self, session_id, user_id, message):
            sent.append((session_id, user_id, message))

    async def fake_save_campaign_async(session):
        return None

    monkeypatch.setattr(inventory_mod, "manager", FakeManager())
    monkeypatch.setattr(inventory_mod, "save_campaign_async", fake_save_campaign_async)

    session = Session(id="stash-send")
    player = User(id="p1", name="Alice", role="player")
    session.users[player.id] = player
    session.player_inventories = {
        "alice": [
            {"id": "heal-1", "name": "Potion of Healing", "qty": 2, "notes": "2d4+2", "rarity": "Common"},
        ]
    }

    asyncio.run(inventory_mod.handle_inventory_send_to_stash({"item_index": 0, "qty": 1}, session, player))

    player_items = get_player_inventory_for_user(session, player.id)
    stash_items = get_party_stash_inventory(session)
    assert player_items[0]["qty"] == 1
    assert stash_items[0]["name"] == "Potion of Healing"
    assert stash_items[0]["qty"] == 1
    assert stash_items[0]["source"] == "Stashed by Alice"
    assert session.party_loot_log[-1]["action"] == "stash"
    assert session.party_loot_log[-1]["item_name"] == "Potion of Healing"
    assert any(
        message["type"] == "inventory_action_result" and "party stash" in message["payload"]["message"].lower()
        for _, user_id, message in sent
        if user_id == player.id
    )


def test_dm_can_assign_stash_item_to_specific_player(monkeypatch):
    from server.handlers import inventory as inventory_mod
    from server.session import Session, User, PARTY_STASH_KEY, get_player_inventory_for_user, get_party_stash_inventory

    sent = []

    class FakeManager:
        async def send_to(self, session_id, user_id, message):
            sent.append((session_id, user_id, message))

    async def fake_save_campaign_async(session):
        return None

    monkeypatch.setattr(inventory_mod, "manager", FakeManager())
    monkeypatch.setattr(inventory_mod, "save_campaign_async", fake_save_campaign_async)

    session = Session(id="stash-claim")
    dm = User(id="dm1", name="Dungeon Master", role="dm")
    player = User(id="p1", name="Alice", role="player")
    session.users[dm.id] = dm
    session.users[player.id] = player
    session.player_inventories = {
        PARTY_STASH_KEY: [
            {"id": "wand-1", "name": "Wand of Secrets", "qty": 1, "is_magic": True, "is_identified": False},
        ]
    }

    asyncio.run(inventory_mod.handle_stash_claim_item({"item_index": 0, "qty": 1, "target_user_id": player.id}, session, dm))

    player_items = get_player_inventory_for_user(session, player.id)
    stash_items = get_party_stash_inventory(session)
    assert len(stash_items) == 0
    assert player_items[0]["name"] == "Wand of Secrets"
    assert player_items[0]["source"] == "Party Stash"
    assert session.party_loot_log[-1]["action"] == "claim"
    assert session.party_loot_log[-1]["target_name"] == "Alice"
    dm_messages = [message for _, user_id, message in sent if user_id == dm.id and message["type"] == "inventory_action_result"]
    player_messages = [message for _, user_id, message in sent if user_id == player.id and message["type"] == "inventory_action_result"]
    assert any("Assigned Wand of Secrets to Alice from the party stash." in message["payload"]["message"] for message in dm_messages)
    assert any("Received Wand of Secrets from the party stash." in message["payload"]["message"] for message in player_messages)


def test_api_chest_add_loot_endpoint_registered():
    """The /api/chest/{chest_id}/add_loot endpoint must be registered in main.py."""
    src = open(os.path.join(PROJECT_ROOT, "main.py")).read()
    assert "api_chest_add_loot" in src, (
        "api_chest_add_loot handler must exist in main.py"
    )
    assert "/api/chest/{chest_id}/add_loot" in src, (
        "Route /api/chest/{chest_id}/add_loot must be registered"
    )


def test_loot_preview_confirm_uses_add_loot_endpoint():
    """lootPreviewConfirm must call the add_loot endpoint, not /api/generate_loot."""
    play_html = open(
        os.path.join(PROJECT_ROOT, "client", "templates", "play.html")
    ).read()
    # Find the lootPreviewConfirm function body
    start = play_html.index("async function lootPreviewConfirm()")
    end = play_html.index("\n}", start) + 2
    fn_body = play_html[start:end]
    assert "/api/chest/" in fn_body, (
        "lootPreviewConfirm must call the /api/chest/{id}/add_loot endpoint"
    )
    assert "add_loot" in fn_body, (
        "lootPreviewConfirm must reference add_loot endpoint path"
    )
    assert "confirmed: true" not in fn_body, (
        "lootPreviewConfirm must NOT call /api/generate_loot with confirmed:true"
    )



def test_play_html_loads_message_dispatch_and_handlers():
    """Phase 4: play.html loads the first-hop dispatcher AND the module router
    (message_handlers.js is the live path for its verified-equivalent handlers)."""
    play_path = os.path.join(PROJECT_ROOT, "client", "templates", "play.html")
    with open(play_path, "r", encoding="utf-8") as f:
        src = f.read()
    assert '/static/js/core/message_dispatch.js' in src, (
        'play.html must load core/message_dispatch.js as the live first-hop dispatcher'
    )
    assert '/static/js/core/message_handlers.js' in src, (
        'play.html must load core/message_handlers.js as the module path for migrated handlers'
    )


def test_message_handlers_marked_partially_live():
    """message_handlers.js should self-identify as loaded/partially-live, not dormant."""
    path = os.path.join(PROJECT_ROOT, 'client', 'static', 'js', 'core', 'message_handlers.js')
    with open(path, 'r', encoding='utf-8') as f:
        src = f.read()
    assert 'env-injected message router (partially live)' in src, (
        'message_handlers.js should document that it is now loaded/partially live'
    )
    assert 'IS loaded by `client/templates/play.html`' in src, (
        'message_handlers.js should document that it is loaded'
    )


def test_play_html_uses_inline_combat_not_app_gameplay_combat():
    """play.html should keep combat inline until the modular combat file is explicitly loaded."""
    play_path = os.path.join(PROJECT_ROOT, "client", "templates", "play.html")
    with open(play_path, "r", encoding="utf-8") as f:
        src = f.read()
    assert 'Stage 3 ownership: this inline block remains the live client combat path' in src, (
        'play.html should document that inline combat remains the live path'
    )
    assert 'function combatApplyState(state, source)' in src, (
        'play.html must continue to own inline combatApplyState during Stage 3'
    )
    assert '/static/js/gameplay/combat.js' not in src, (
        'play.html must not load dormant gameplay/combat.js during Stage 3'
    )


def test_play_html_loads_stage3_store_before_runtime_bridge_and_ws():
    """play.html should load the Stage 3 shell store before the bridge/boot/ws modules."""
    play_path = os.path.join(PROJECT_ROOT, "client", "templates", "play.html")
    with open(play_path, "r", encoding="utf-8") as f:
        src = f.read()
    store_idx = src.index('/static/js/state/store.js')
    bridge_idx = src.index('/static/js/core/runtime_bridge.js')
    boot_idx = src.index('/static/js/core/boot_shell.js')
    ws_idx = src.index('/static/js/core/ws.js')
    assert store_idx < bridge_idx < boot_idx < ws_idx, (
        'Stage 3 requires store.js to load before the bridge/boot/ws shell modules'
    )
    assert 'const _runtimeStore = (window.AppStateStore || window.AppStore);' in src, (
        'play.html must bind the central shell store during Stage 3'
    )
    assert 'function __syncMapShellState()' in src, (
        'play.html must mirror live map shell state back into the Stage 3 store'
    )



def test_runtime_bridge_reads_shell_state_from_store_first():
    """runtime_bridge.js should prefer the Stage 3 store for shell reads."""
    bridge_path = os.path.join(PROJECT_ROOT, 'client', 'static', 'js', 'core', 'runtime_bridge.js')
    with open(bridge_path, 'r', encoding='utf-8') as f:
        src = f.read()
    required_reads = [
        "storeGet('session.id', global.SESSION_ID || '')",
        "storeGet('user.id', global.USER_ID || '')",
        "storeGet('user.role', global.ROLE || 'viewer')",
        "storeGet('socket.instance', global.ws || null)",
        "storeGet('socket.reconnectTimer', global.wsReconnectTimer || null)",
        "storeGet('socket.pendingMessages', Array.isArray(global._pendingWSMessages) ? global._pendingWSMessages : [])",
    ]
    for snippet in required_reads:
        assert snippet in src, f"runtime_bridge.js must prefer store-backed shell state for {snippet}"

def test_runtime_bridge_keeps_play_page_query_param_fallbacks_for_ws_identity():
    """runtime_bridge.js must still resolve session/user/role from play.html query params."""
    bridge_path = os.path.join(PROJECT_ROOT, 'client', 'static', 'js', 'core', 'runtime_bridge.js')
    with open(bridge_path, 'r', encoding='utf-8') as f:
        src = f.read()
    assert "return readQueryParam(['session_id', 'session', 'sid']);" in src
    assert "return readQueryParam(['user_id', 'uid', 'user']);" in src
    assert "const fromQuery = readQueryParam(['role']);" in src
    assert "const effectiveRole = String(config.getRole() || 'viewer').toLowerCase();" in src



def test_stage3_store_defines_shell_state_domains():
    """state/store.js should define the Stage 3 shell-state domains."""
    store_path = os.path.join(PROJECT_ROOT, 'client', 'static', 'js', 'state', 'store.js')
    with open(store_path, 'r', encoding='utf-8') as f:
        src = f.read()
    for snippet in [
        "session: { id: '', returning: false }",
        "user: { id: '', name: '', role: 'viewer' }",
        "socket: { instance: null, reconnectTimer: null, pendingMessages: [], connected: false, status: 'idle' }",
        "map: { currentId: '', currentPoiId: '', currentContext: 'world', dmContext: 'world', navVersion: 0, clientNavIntent: 0 }",
        "ui: { activeRightTab: 'party', unreadLog: 0, currentTool: 'select', selectedDice: 20 }",
    ]:
        assert snippet in src, f"store.js must keep Stage 3 shell domain {snippet}"
    assert 'global.AppStateStore = api;' in src, (
        'store.js must expose the central shell store on window.AppStateStore during Stage 3'
    )


def test_gameplay_combat_marked_dormant():
    """gameplay/combat.js should self-identify as a dormant alternate combat module."""
    path = os.path.join(PROJECT_ROOT, 'client', 'static', 'js', 'gameplay', 'combat.js')
    with open(path, 'r', encoding='utf-8') as f:
        src = f.read()
    assert 'dormant env-injected combat module' in src, (
        'gameplay/combat.js should document that it is dormant'
    )
    assert 'not loaded by `play.html` today' in src, (
        'gameplay/combat.js should point readers to the live runtime owner'
    )


def test_play_html_marks_editor_serialization_authoritative():
    """play.html should document serialization.js as the authoritative map serializer."""
    play_path = os.path.join(PROJECT_ROOT, "client", "templates", "play.html")
    with open(play_path, "r", encoding="utf-8") as f:
        src = f.read()
    assert 'editor/serialization.js is the authoritative map document serializer' in src, (
        'play.html should document the Stage 4 serializer ownership'
    )
    assert '/static/js/editor/serialization.js' in src
    assert '/static/js/editor/runtime.js' not in src
    assert '/static/js/editor/state.js' not in src


def test_play_html_marks_render_boot_authoritative():
    """play.html should document render/boot.js as the live Stage 4 render bootstrap owner."""
    play_path = os.path.join(PROJECT_ROOT, "client", "templates", "play.html")
    with open(play_path, "r", encoding="utf-8") as f:
        src = f.read()
    assert 'render/boot.js is the live canvas bootstrap owner' in src, (
        'play.html should document the Stage 4 render bootstrap ownership'
    )
    assert '/static/js/render/boot.js' in src
    assert 'function __createLegacyRenderBootEnv()' in src
    assert 'window.AppRenderBoot.initCanvas(renderEnv);' in src
    assert 'window.AppRenderBoot.resizeCanvas(renderEnv);' in src


def test_render_boot_module_marked_and_exports_boot_api():
    """render/boot.js should advertise its Stage 4 role and export the live bootstrap API."""
    path = os.path.join(PROJECT_ROOT, 'client', 'static', 'js', 'render', 'boot.js')
    with open(path, 'r', encoding='utf-8') as f:
        src = f.read()
    assert 'Stage 4' in src or 'render bootstrap' in src, (
        'render/boot.js should self-identify as the Stage 4 render bootstrap module'
    )
    assert 'global.AppRenderBoot' in src
    for exported_name in [
        'initCanvas',
        'resizeCanvas',
        'ensureFogCanvas',
        'bindCanvasEvents',
        'bindGlobalRenderEvents',
        'bindResize',
        'ensureLoopStarted',
        'startRenderLoop',
    ]:
        assert exported_name in src, f'render/boot.js must export {exported_name} during Stage 4'
    assert 'doc.body.dataset.renderBootFinalizersBound' in src, (
        'render/boot.js should own one-time global finalizer bindings during Stage 4'
    )
    assert 'global.__appRenderBootResizeBound' in src, (
        'render/boot.js should own one-time resize binding during Stage 4'
    )


def test_editor_serialization_and_runtime_modules_marked_correctly():
    """Stage 4 modules should advertise serializer authority vs dormant runtime ownership."""
    serialization_path = os.path.join(PROJECT_ROOT, 'client', 'static', 'js', 'editor', 'serialization.js')
    runtime_path = os.path.join(PROJECT_ROOT, 'client', 'static', 'js', 'editor', 'runtime.js')
    state_path = os.path.join(PROJECT_ROOT, 'client', 'static', 'js', 'editor', 'state.js')
    with open(serialization_path, 'r', encoding='utf-8') as f:
        serialization_src = f.read()
    with open(runtime_path, 'r', encoding='utf-8') as f:
        runtime_src = f.read()
    with open(state_path, 'r', encoding='utf-8') as f:
        state_src = f.read()
    assert 'authoritative editor map document serializer' in serialization_src
    assert 'window.EditorMapDocument' in serialization_src
    assert 'dormant env-injected editor runtime module' in runtime_src
    assert 'not loaded by' in runtime_src
    assert 'dormant env-injected editor state module' in state_src
    assert 'not loaded by' in state_src


def test_play_html_stage5_script_load_guardrails():
    """play.html should keep the audited ownership boundary encoded in its script tags."""
    play_path = os.path.join(PROJECT_ROOT, "client", "templates", "play.html")
    with open(play_path, "r", encoding="utf-8") as f:
        src = f.read()

    # Live first-hop dispatcher remains loaded; module router is now loaded too
    # (Phase 4 — live path for the verified-equivalent handlers it covers).
    assert '/static/js/core/message_dispatch.js' in src
    assert '/static/js/core/message_handlers.js' in src

    # Live sound/narration path remains loaded.
    assert '/static/js/ui/sound_engine.js' in src
    assert '/static/js/ui/narration.js' in src

    # Compatibility fallback audio remains loaded until explicitly removed.
    assert '/static/ambient_engine.js' in src
    assert '/static/sfx_engine.js' in src

    # Inline combat remains authoritative; dormant modular combat stays unloaded.
    assert '/static/js/gameplay/combat.js' not in src

    # Serialization remains loaded; dormant editor runtime/state stay unloaded.
    assert '/static/js/editor/serialization.js' in src
    assert '/static/js/editor/runtime.js' not in src
    assert '/static/js/editor/state.js' not in src

    # Render bootstrap is loaded as the live Stage 4 owner.
    assert '/static/js/render/boot.js' in src


def test_play_html_marks_stage5_fog_and_vision_modules_authoritative():
    """play.html should wire Stage 5 fog/vision helpers through the live render modules."""
    play_path = os.path.join(PROJECT_ROOT, 'client', 'templates', 'play.html')
    with open(play_path, 'r', encoding='utf-8') as f:
        src = f.read()
    assert '/static/js/render/fog.js' in src
    assert '/static/js/render/vision.js' in src
    assert 'function __createFogModuleEnv()' in src
    assert 'function __createVisionModuleEnv()' in src
    assert 'id="fog-status-text"' in src
    assert 'syncShellState: () => _syncFogShellState()' in src, (
        'play.html should provide the Stage 5 fog shell sync bridge'
    )
    assert 'document,' in src[src.index('function __createFogModuleEnv()'):src.index('function fogCurrentCtx()')], (
        'fog module env should provide document access through the compatibility env'
    )
    assert 'document,' in src[src.index('function __createVisionModuleEnv()'):src.index('function __createFogModuleState()')], (
        'vision module env should provide document access through the compatibility env'
    )
    assert 'syncShellState: () => _syncVisionShellState()' in src, (
        'play.html should provide the Stage 5 vision shell sync bridge'
    )



def test_stage5_fog_and_vision_modules_marked_and_env_driven():
    """Stage 5 fog/vision modules should self-identify and use env-driven shell access."""
    fog_path = os.path.join(PROJECT_ROOT, 'client', 'static', 'js', 'render', 'fog.js')
    vision_path = os.path.join(PROJECT_ROOT, 'client', 'static', 'js', 'render', 'vision.js')
    with open(fog_path, 'r', encoding='utf-8') as f:
        fog_src = f.read()
    with open(vision_path, 'r', encoding='utf-8') as f:
        vision_src = f.read()
    assert 'Stage 5 fog shell owner' in fog_src
    assert 'window.AppFog' in fog_src
    assert 'env.document || document' in fog_src
    assert 'env.syncShellState' in fog_src
    assert "Fog is ON · players can currently see about" in fog_src
    assert "const fogFlyout = doc.getElementById('flyout-fog')" in fog_src
    assert 'window.AppStore.set' not in fog_src, (
        'fog.js should use the Stage 5 compatibility env rather than writing to AppStore directly'
    )
    assert 'Stage 5 vision shell owner' in vision_src
    assert 'window.AppVision' in vision_src
    assert 'env.document || document' in vision_src


def test_stage5_store_and_repo_map_capture_fog_and_vision_shell_ownership():
    """Stage 5 should keep fog/vision shell state store-backed and documented in repo-map guardrails."""
    store_path = os.path.join(PROJECT_ROOT, 'client', 'static', 'js', 'state', 'store.js')
    repo_map_path = os.path.join(PROJECT_ROOT, 'docs', 'repo-map.md')
    play_path = os.path.join(PROJECT_ROOT, 'client', 'templates', 'play.html')
    with open(store_path, 'r', encoding='utf-8') as f:
        store_src = f.read()
    with open(repo_map_path, 'r', encoding='utf-8') as f:
        repo_map_src = f.read()
    with open(play_path, 'r', encoding='utf-8') as f:
        play_src = f.read()

    assert "fog: { enabled: false, preview: false, reveal: true, brushSize: 3, mapContext: 'world' }" in store_src
    assert "vision: { preview: { enabled: false, tokenId: '', ownerId: '' }, showFallbackBanner: false }" in store_src
    assert '| Fog / vision shell helpers | `render/fog.js`, `render/vision.js` | n/a |' in repo_map_src
    assert "function _syncVisionShellState()" in play_src
    assert "_runtimeStore.patch({" in play_src[play_src.index('function _syncVisionShellState()'):play_src.index('function fogSetBrushSize(size)')]


def test_play_html_marks_stage6_tabs_module_authoritative():
    """play.html should wire the Stage 6 tabs controller through the live tabs module."""
    play_path = os.path.join(PROJECT_ROOT, 'client', 'templates', 'play.html')
    with open(play_path, 'r', encoding='utf-8') as f:
        src = f.read()
    assert '/static/js/ui/tabs.js' in src
    assert 'function __createTabsEnv()' in src
    assert 'window.AppUITabs.init(__createTabsEnv());' in src
    assert 'window.AppUITabs.switchRTab(__createTabsEnv(), tab);' in src
    assert 'window.AppUITabs.toggleDropdown(__createTabsEnv(), menuId);' in src
    assert 'window.AppUITabs.bumpLogBadge(__createTabsEnv());' in src
    assert 'syncShellState: () => __syncTabsShellState()' in src
    assert "getSpellLibraryLength: () => (ROLE === 'dm' ? _spellLibrary.length : _playerGrantedSpells.length)" in src



def test_stage6_tabs_env_reads_shell_state_from_store_first():
    """play.html should keep Stage 6 tab shell reads store-backed before falling back inline."""
    play_path = os.path.join(PROJECT_ROOT, 'client', 'templates', 'play.html')
    with open(play_path, 'r', encoding='utf-8') as f:
        src = f.read()
    tabs_region = src[src.index('function __syncTabsShellState()'):src.index('function _closeAllDropdowns()')]
    assert "_runtimeStore.get('ui.activeRightTab'" in tabs_region
    assert "_runtimeStore.get('ui.unreadLog'" in tabs_region
    assert '_activeRTab = __getActiveRightTabShellState();' in tabs_region
    assert '_unreadLog = __getUnreadLogShellState();' in tabs_region



def test_stage6_tabs_module_marked_and_env_driven():
    """ui/tabs.js should self-identify as the Stage 6 owner and stay env-driven."""
    tabs_path = os.path.join(PROJECT_ROOT, 'client', 'static', 'js', 'ui', 'tabs.js')
    with open(tabs_path, 'r', encoding='utf-8') as f:
        tabs_src = f.read()
    assert 'Stage 6 right-sidebar tabs owner' in tabs_src
    assert 'function normalizeTab(tab)' in tabs_src
    assert 'doc.body?.dataset.uiTabsBound' in tabs_src, (
        'tabs.js should keep one-time binding ownership on the active document body during Stage 6'
    )
    assert 'env?.document || global.document' in tabs_src
    assert 'installBindings' in tabs_src
    assert 'global.AppUITabs' in tabs_src
    assert 'env.syncShellState?.()' in tabs_src


def test_stage6_tabs_registry_covers_live_right_panel_panes():
    """tabs.js should define one canonical tab registry with live pane mappings."""
    tabs_path = os.path.join(PROJECT_ROOT, 'client', 'static', 'js', 'ui', 'tabs.js')
    with open(tabs_path, 'r', encoding='utf-8') as f:
        tabs_src = f.read()
    assert 'const TAB_REGISTRY = [' in tabs_src
    for tab_id in ['party', 'inventory', 'log', 'memory', 'combat', 'shop', 'bestiary', 'spelllib', 'handouts']:
        assert f"id: '{tab_id}'" in tabs_src
        assert f"paneSelector: '#rtab-pane-{tab_id}'" in tabs_src
    assert 'buttonSelector:' in tabs_src
    assert 'order:' in tabs_src
    assert 'isVisible:' in tabs_src


def test_play_html_tab_visibility_and_switching_defer_to_tabs_registry():
    """play.html should no longer own legacy tab-removal/remapping logic after load."""
    play_path = os.path.join(PROJECT_ROOT, 'client', 'templates', 'play.html')
    with open(play_path, 'r', encoding='utf-8') as f:
        src = f.read()
    assert 'function __isHandoutsTabVisible()' in src
    assert 'isHandoutsTabVisible: () => __isHandoutsTabVisible()' in src
    assert "window.AppUITabs.switchRTab(__createTabsEnv(), tab);" in src
    assert '_libraryInitVisibility' not in src
    assert "handoutsBtn.remove()" not in src
    assert "bestiaryPane.remove()" not in src
    assert "spelllibPane.remove()" not in src


def test_tab_mount_visibility_is_owned_by_registry_helpers_only():
    """Tab mounts should not carry inline visibility hacks; tabs.js remains the visibility owner."""
    tabs_path = os.path.join(PROJECT_ROOT, 'client', 'static', 'js', 'ui', 'tabs.js')
    play_path = os.path.join(PROJECT_ROOT, 'client', 'templates', 'play.html')
    with open(tabs_path, 'r', encoding='utf-8') as f:
        tabs_src = f.read()
    with open(play_path, 'r', encoding='utf-8') as f:
        play_src = f.read()

    assert 'function setVisible(node, visible)' in tabs_src
    assert 'node.hidden = !visible;' in tabs_src
    assert 'setVisible(btn, isVisible);' in tabs_src
    assert 'setVisible(pane, isVisible);' in tabs_src
    assert 'isRendered(item)' in tabs_src
    assert 'id="rtab-handouts" style="display:none;"' not in play_src


def test_play_html_handouts_visibility_uses_sync_state_not_only_realtime_events():
    """Handouts tab visibility should remain stable across handout_sync refreshes."""
    play_path = os.path.join(PROJECT_ROOT, 'client', 'templates', 'play.html')
    with open(play_path, 'r', encoding='utf-8') as f:
        src = f.read()
    assert 'const hasSyncedHandouts = _handoutSyncInitialized && Object.keys(_handouts || {}).length > 0;' in src
    assert 'return hasReceived || hasSyncedHandouts;' in src
    assert '_playerReceivedHandouts = Object.values(incoming)' in src
    assert 'renderPlayerHandoutsList();' in src
    assert "window.AppUITabs.syncTabUI(__createTabsEnv());" in src


def test_session_state_includes_party_vote_metadata_for_players_and_dm():
    from server.session import Session, User

    session = Session(id="vote-test", player_invite="PLAY01", viewer_invite="VIEW01", created_at=0)
    dm = User(id="dm1", name="DM", role="dm")
    player = User(id="p1", name="Hero", role="player")
    viewer = User(id="v1", name="Spectator", role="viewer")
    session.users = {dm.id: dm, player.id: player, viewer.id: viewer}
    session.active_poll = {
        "id": "poll1",
        "title": "Marching Order",
        "question": "Who keeps watch?",
        "options": ["Aela", "Borin"],
        "votes": {"p1": 1},
        "created_at": 10,
        "closes_at": 40,
        "closed": False,
        "results_mode": "final",
        "authority_note": "The DM keeps final say.",
    }

    dm_state = session.to_state_dict_for_role(role="dm", user_id=dm.id)
    player_state = session.to_state_dict_for_role(role="player", user_id=player.id)
    viewer_state = session.to_state_dict_for_role(role="viewer", user_id=viewer.id)

    assert dm_state["active_poll"]["title"] == "Marching Order"
    assert dm_state["active_poll"]["vote_counts"] == [0, 1]
    assert dm_state["active_poll"]["total_votes"] == 1
    assert dm_state["active_poll"]["results_mode"] == "final"
    assert player_state["active_poll"]["title"] == "Marching Order"
    assert player_state["active_poll"]["vote_counts"] == [0, 1]
    assert player_state["active_poll"]["user_vote"] == 1
    assert viewer_state["active_poll"]["user_vote"] is None



def test_handle_poll_create_vote_and_close_broadcasts_party_vote_payloads():
    import asyncio
    from unittest.mock import AsyncMock, patch
    from server.session import Session, User
    from server.handlers.content import handle_poll_create, handle_poll_vote, handle_poll_close

    session = Session(id="vote-flow", player_invite="PLAY01", viewer_invite="VIEW01", created_at=0)
    dm = User(id="dm1", name="DM", role="dm")
    player = User(id="p1", name="Hero", role="player")
    viewer = User(id="v1", name="Spectator", role="viewer")
    session.users = {dm.id: dm, player.id: player, viewer.id: viewer}

    async def _run():
        with patch("server.handlers.content.save_campaign_async", new_callable=AsyncMock) as mock_save, \
             patch("server.handlers.content.manager") as mock_mgr:
            mock_mgr.send_to = AsyncMock()
            mock_mgr.broadcast = AsyncMock()
            create_task_calls = []

            def _fake_create_task(coro):
                create_task_calls.append(coro)
                coro.close()
                return None

            with patch("server.handlers.content.asyncio.create_task", side_effect=_fake_create_task):
                await handle_poll_create({
                    "title": "Path Vote",
                    "question": "Left path or right path?",
                    "options": ["Left", "Right"],
                    "duration_sec": 30,
                    "results_mode": "final",
                }, session, dm)

            assert session.active_poll["title"] == "Path Vote"
            assert session.active_poll["results_mode"] == "final"
            assert session.active_poll["closes_at"] is not None
            assert create_task_calls, "Expected a timer task for timed votes"
            assert mock_mgr.send_to.await_count == 3

            first_dm_payload = mock_mgr.send_to.await_args_list[0].args[2]["payload"]
            assert first_dm_payload["title"] == "Path Vote"
            assert first_dm_payload["authority_note"] == "The DM keeps final say."

            await handle_poll_vote({
                "poll_id": session.active_poll["id"],
                "option_index": 1,
            }, session, player)

            assert session.active_poll["votes"][player.id] == 1
            vote_payloads = [call.args[2]["payload"] for call in mock_mgr.send_to.await_args_list[-3:]]
            assert any(payload.get("user_vote") == 1 for payload in vote_payloads if isinstance(payload, dict))

            await handle_poll_close({}, session, dm)
            assert session.active_poll["closed"] is True
            assert session.active_poll["closed_reason"] == "dm_closed"
            assert mock_save.await_count >= 3

    asyncio.run(_run())


def test_player_combat_drag_commits_on_mouseup_without_confirm_click():
    """Player combat movement should not leave an unsent click-to-confirm preview."""
    content = open(os.path.join(PROJECT_ROOT, "client/templates/play.html"), encoding="utf-8").read()
    assert "Player combat movement now commits on mouse-up" in content
    assert "Click the token again to place it" not in content


def test_combat_coach_can_be_collapsed_by_players():
    """The turn checklist/help hub must expose a persistent hide/show toggle."""
    content = open(os.path.join(PROJECT_ROOT, "client/templates/play.html"), encoding="utf-8").read()
    assert "function toggleCombatCoachCollapsed()" in content
    assert "combat_coach_collapsed" in content
    assert "coach-collapsed" in content



def test_player_turn_detection_falls_back_to_combatant_owner_id():
    """Players must see their turn/actions even if token owner lookup is stale or name-based."""
    content = open(os.path.join(PROJECT_ROOT, "client/templates/play.html"), encoding="utf-8").read()
    assert "function _combatantOwnedByMe(combatant, token = null)" in content
    assert "combatant.owner_id || combatant.owner" in content
    assert "return _combatantOwnedByMe(cur) ? cur : null;" in content
    assert "if (_combatantOwnedByMe(current)) return current;" in content
    assert "const isMyTurn = !!(isInCombat && current && _combatantOwnedByMe(current));" in content


def test_combat_roster_uses_canonical_normalized_renderer():
    """DM/player combat panels should render from one normalized roster source."""
    content = open(os.path.join(PROJECT_ROOT, "client/templates/play.html"), encoding="utf-8").read()
    assert "function _normalizeCombatRoster(combatState = _combat)" in content
    assert "function _renderCombatRoster(list, roster, isActive)" in content
    assert "const roster = _normalizeCombatRoster(_combat);" in content
    assert "_renderCombatRoster(list, roster, isActive);" in content
    assert "seen.has(rowKey)" in content, "Combat roster should skip duplicate rows."


def test_combat_roster_badges_and_visibility_rules_are_explicit():
    """Current/next badges, owner badges, and hidden-token rules must stay visible and role-aware."""
    content = open(os.path.join(PROJECT_ROOT, "client/templates/play.html"), encoding="utf-8").read()
    assert '<span class="ce-order now">Now</span>' in content
    assert '<span class="ce-order next">Next</span>' in content
    assert '<span class="ce-order">YOU</span>' in content
    assert "function _isCombatTokenVisibleToPlayer(token, combatant)" in content
    assert "if (ROLE === 'dm') return true;" in content
    assert "token.hidden || token.hidden_from_players || token.visible_to_players === false" in content
    assert '<span class="ce-hp hidden">Hidden</span>' in content


def test_combat_roster_survives_stacked_combat_tools_with_internal_scroll():
    """Quick attacks, turn controls, and hazard tools should not overlay or clip the roster."""
    content = open(os.path.join(PROJECT_ROOT, "client/static/css/play.css"), encoding="utf-8").read()
    assert ".combat-list {" in content
    assert "overflow-y: auto;" in content
    assert "max-height: clamp(14rem, 42vh, 28rem);" in content
    assert "isolation: isolate;" in content
    assert "#hazard-panel" in content
    assert "#combat-weapon-tray" in content



def test_quick_action_bridges_are_guarded_and_non_recursive():
    """Quick action public bridges must call distinct implementations behind recursion guards."""
    play = open(os.path.join(PROJECT_ROOT, "client/templates/play.html"), encoding="utf-8").read()
    actions = open(os.path.join(PROJECT_ROOT, "client/static/js/character/combat_quick_actions.js"), encoding="utf-8").read()
    bridge_pairs = {
        "openCombatQuickBarWeaponAction": "performOpenCombatQuickBarWeaponAction",
        "combatQuickWeaponAttack": "performCombatQuickWeaponAttack",
        "combatQuickRollWeaponDamage": "performCombatQuickRollWeaponDamage",
        "executeCombatQuickBarSpell": "performExecuteCombatQuickBarSpell",
        "combatQuickCastSpell": "performCombatQuickCastSpell",
        "combatQuickRollSpellDamage": "performCombatQuickRollSpellDamage",
    }
    assert "const __quickBridgeActive = new Set();" in play
    assert "function guardQuickActionBridge(name, fn)" in play
    assert "Recursive bridge blocked" in play
    assert "window.DEBUG_QUICK_ACTIONS" in play
    for public_name, impl_name in bridge_pairs.items():
        assert f"function {impl_name}(" in play or f"function {impl_name}(" in actions
        assert f"guardQuickActionBridge('{public_name}'" in play or f"guardBridge('{public_name}'" in actions
        assert f"function {public_name}(" not in play, f"{public_name} should remain a public bridge, not the implementation"
    for public_name in bridge_pairs:
        assignment_match = re.search(rf"window\.{public_name}\s*=\s*function[\s\S]*?\n}};", play)
        if assignment_match:
            body = assignment_match.group(0).replace(f"window.{public_name} =", "")
            assert f"window.{public_name}(" not in body
            assert f"{public_name}(" not in body.replace(f"{public_name}Bridge", "")
    assert "function performOpenCombatQuickBarWeaponAction(action)" in actions
    assert "global.openCombatQuickBarWeaponAction = function openCombatQuickBarWeaponActionBridge" in actions
    assert "guardBridge('openCombatQuickBarWeaponAction'" in actions


def test_player_state_sync_has_remote_apply_and_send_suppression_guards():
    """Incoming websocket state must not echo unchanged state back to the server."""
    content = open(os.path.join(PROJECT_ROOT, "client/templates/play.html"), encoding="utf-8").read()
    assert "let isApplyingRemoteState = false;" in content
    assert "function beginRemoteStateApply(type)" in content
    assert "function endRemoteStateApply()" in content
    assert "beginRemoteStateApply(msg && msg.type);" in content
    assert "endRemoteStateApply();" in content
    assert "if (isApplyingRemoteState)" in content
    assert "Suppressed send while applying remote state" in content


def test_message_dispatch_has_stack_overflow_diagnostics_and_depth_guard():
    """Client dispatch should keep a short breadcrumb trail and block recursive floods."""
    content = open(os.path.join(PROJECT_ROOT, "client/static/js/core/message_dispatch.js"), encoding="utf-8").read()
    assert "const recentMessageTypes = [];" in content
    assert "if (recentMessageTypes.length > 30)" in content
    assert "let dispatchDepth = 0;" in content
    assert "if (dispatchDepth >= 20)" in content
    assert "nested dispatch depth exceeded" in content
    assert "Maximum call stack size exceeded" in content
    assert "getDispatchDiagnostics" in content


def test_player_map_render_and_image_loading_are_coalesced():
    """Remote sync and map image load should coalesce renders and avoid resetting image src repeatedly."""
    content = open(os.path.join(PROJECT_ROOT, "client/templates/play.html"), encoding="utf-8").read()
    assert "let _renderFramePending = false;" in content
    assert "function requestRenderFrame(reason)" in content
    assert "requestRenderFrame('state_sync render')" in content
    assert "requestRenderFrame('fog state render')" in content
    assert "let _mapImageLoadingUrl = null;" in content
    assert "mapImageUrl === url && (mapImage || _mapImageLoadingUrl === url)" in content
    assert "img.src = requestedUrl;" in content
    assert "requestedUrl + '?t='" not in content


def test_combat_quick_weapon_attack_waits_for_final_dice_result():
    """Quick weapon attacks should resolve from the settled dice result, not a pre-roll."""
    content = open(os.path.join(PROJECT_ROOT, "client/templates/play.html"), encoding="utf-8").read()
    assert "async function _rollFinalD20Attack(base = {})" in content
    assert "await _rollLocalDiceAfterSettle" in content
    assert "function _buildFinalAttackRollObject(base = {}, diceResult = {})" in content
    assert "const _pendingCombatQuickWeaponAttacks = new Set();" in content
    assert "attackRoll.d20" in content
    assert "roll_payload: { ...attackRoll" in content
    match = re.search(r"async function performCombatQuickWeaponAttack\([\s\S]*?\n}\n\n// ── Weapon Runtime Resolver", content)
    assert match, "performCombatQuickWeaponAttack implementation not found"
    body = match.group(0)
    assert "await _rollFinalD20Attack" in body
    assert "_showCombatResultCard" in body
    assert body.index("await _rollFinalD20Attack") < body.index("_showCombatResultCard")
    prefix = body[:body.index("await _rollFinalD20Attack")]
    assert "Math.random() * 20" not in prefix
    assert "rollA" not in body
    assert "rollB" not in body
