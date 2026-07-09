"""
main.py — FastAPI entry point for D&D Tabletop (Phase 1)
"""
import asyncio
import json
import os
import time
import subprocess
import threading
import logging
from contextlib import asynccontextmanager
from pathlib import Path

# Load .env FIRST — must happen before any server modules are imported so that
# module-level os.environ.get() calls in handlers (e.g. narration.py) pick up
# the keys from the file.
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv(override=False)  # don't overwrite already-set env vars
except ImportError:
    pass

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.responses import FileResponse

from server.session import get_session
from server.db import init_db, migrate_char_profiles_dedup, save_campaign_async, load_campaign, create_creature
from server.paths import DATA_DIR, DB_PATH, MAPS_DIR, BACKUPS_DIR, ensure_data_dirs, migrate_legacy_data, create_startup_backup
from server.connections import manager
from server.handlers import handle_message
from server.handlers.common import build_live_state_debug_summary
from server.handlers.move_coalescer import cancel_session_flushes
from server.handlers.durability import mark_session_dirty, flush_session_durability
from server.auth.models import init_auth_db, merge_legacy_users_from_db
from server.auth.dependencies import install_auth_runtime
from server.auth.routes import router as auth_router
from server.auth.jwt_utils import verify_token
from server.http.auth import get_request_user
from server.http.session_access import get_or_restore_session, request_has_dm_access, restore_session
from server.campaigns.claim import router as claim_router
from server.users.assets import router as assets_router, USER_ASSETS_DIR
from server.users.stats import router as stats_router
from server.sessions.routes import router as sessions_router
from server.campaigns.routes import router as campaigns_router
from server.assets.routes import router as asset_library_router
from server.ambient_audio import ensure_ambient_audio_assets
from server.map_library import init_map_library_db, save_map as save_library_map
from server.pages.routes import build_router as build_pages_router
from server.rules.routes import router as rules_router
from server.integrations.routes import router as integrations_router
from server.assistant.routes import router as assistant_router
from server.narration.routes import router as narration_router
from server.creatures.routes import router as creatures_router
from server.maps.routes import router as maps_router
from server.commercial.routes import router as commercial_router
from server.character.routes import router as character_router
from server.twitch_ext.routes import router as twitch_ext_router
from server.http.health import router as health_router
from server.chat_bridge_routes import router as chat_bridge_router
from server.twitch_oauth_routes import router as twitch_oauth_router
from server.overlay_routes import router as overlay_router
from server.config import load_config
from server.static_compat import resolve_legacy_class_portrait
from server.item_library_srd import get_srd_items_version

# ── GPU TTS system (Chatterbox + Dia + Kokoro) ────────────────────────────────
try:
    from tts_server import tts_router as _tts_router, startup_tts as _startup_tts
    _TTS_AVAILABLE = True
except ImportError:
    _TTS_AVAILABLE = False
    _tts_router = None
    _startup_tts = None

_tts_startup_error: str | None = None  # set if startup_tts() raises

# ── Startup config ──
APP_CONFIG = load_config(Path(__file__).parent / "config.txt")
PUBLIC_DOMAIN = APP_CONFIG.public_domain
PORT = APP_CONFIG.port
NGROK_DOMAIN = APP_CONFIG.ngrok_domain  # kept for backward compat

logging.basicConfig(
    level=getattr(logging, APP_CONFIG.log_level, logging.INFO),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)


def _auth_is_enforced() -> bool:
    """Return True when WebSocket JWT auth should be enforced.

    ``verify_token`` signs and verifies with ``DND_JWT_SECRET``.  Legacy/local
    invite-code sessions remain unauthenticated when that secret is unset.
    """
    return bool(os.environ.get("DND_JWT_SECRET", "").strip())


from server.session import display_user_handle as _display_user_handle  # shared handle algorithm


def install_asyncio_exception_filters():
    """Silence known noisy disconnect traces on Windows/Proactor loops without hiding real app errors."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    prior_handler = loop.get_exception_handler()

    def _handler(loop, context):
        exc = context.get("exception")
        message = str(context.get("message") or "")
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            winerror = getattr(exc, "winerror", None)
            if winerror == 10054 or "forcibly closed by the remote host" in str(exc).lower():
                return
        if "_ProactorBasePipeTransport._call_connection_lost" in message:
            return
        if prior_handler:
            prior_handler(loop, context)
        else:
            loop.default_exception_handler(context)

    loop.set_exception_handler(_handler)

def print_urls():
    """Print the URLs players can use to connect."""
    import time
    time.sleep(1)
    print("\n  ============================================")
    if PUBLIC_DOMAIN:
        url = f"http://{PUBLIC_DOMAIN}" if PORT == 80 else f"http://{PUBLIC_DOMAIN}:{PORT}"
        print(f"  Player link:  {url}")
        print(f"  Share this link with your players!")
    else:
        # Try ngrok if configured
        if NGROK_DOMAIN:
            print(f"  Player link:  https://{NGROK_DOMAIN}")
            print(f"  Share this link with your players!")
        else:
            try:
                import urllib.request, json as _json
                with urllib.request.urlopen("http://127.0.0.1:4040/api/tunnels", timeout=3) as r:
                    data = _json.loads(r.read())
                    for t in data.get("tunnels", []):
                        if t.get("public_url", "").startswith("https"):
                            print(f"  Player link:  {t['public_url']}")
                            print(f"  Share this link with your players!")
                            break
            except Exception:
                import socket
                hostname = socket.gethostname()
                local_ip = socket.gethostbyname(hostname)
                print(f"  Local link:   http://{local_ip}:{PORT}")
                print(f"  (Local network only — set PUBLIC_DOMAIN in config.txt for internet access)")
    print(f"  DM link:      http://localhost:{PORT}")
    print("  ============================================\n")

def maybe_start_ngrok():
    if PUBLIC_DOMAIN:
        return  # Using own domain — no ngrok needed
    if not NGROK_DOMAIN:
        return
    try:
        cmd = ["ngrok", "http", f"--domain={NGROK_DOMAIN}", str(PORT)]
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except FileNotFoundError:
        pass

@asynccontextmanager
async def lifespan(app):
    install_asyncio_exception_filters()
    try:
        install_auth_runtime(app)
    except RuntimeError as exc:
        logger.critical(
            "[BOOT] Fatal auth dependency check failed. Refusing to start with a broken auth runtime: %s",
            exc,
        )
        raise RuntimeError(
            "Startup aborted: required auth dependency check failed. "
            "Install dependencies with 'pip install -r requirements.txt' and restart."
        ) from exc
    ensure_data_dirs()
    migration = migrate_legacy_data()
    backup_path = create_startup_backup()
    init_db()
    # One-time storage-layout dedup of persisted char_profiles (idempotent;
    # runs only after create_startup_backup() above so it is always revertible).
    migrate_char_profiles_dedup()
    init_map_library_db()
    init_auth_db()
    legacy_user_migration = merge_legacy_users_from_db()
    ensure_ambient_audio_assets(Path(__file__).parent / "client" / "static" / "assets" / "audio")
    logger.info("[DATA] Using data folder: %s", DATA_DIR)
    logger.info("[DATA] Database: %s", DB_PATH)
    logger.info("[DATA] Maps: %s", MAPS_DIR)
    logger.info("[DATA] Backups: %s", BACKUPS_DIR)
    if backup_path:
        logger.info("[BACKUP] Startup backup created: %s", backup_path.name)
    if migration.get("db_copied") or migration.get("maps_copied"):
        logger.info("[DATA] Imported legacy local data: db_copied=%s, maps_copied=%s", migration.get('db_copied'), migration.get('maps_copied'))
    if legacy_user_migration.get('copied'):
        logger.info("[AUTH] Imported %s legacy user account(s) from %s", legacy_user_migration.get('copied'), legacy_user_migration.get('legacy_db'))
    logger.info(
        "[BOOT] app_env=%s public_base_url=%s trust_proxy_headers=%s auth_cookie_secure=%s",
        APP_CONFIG.app_env,
        APP_CONFIG.public_base_url or "(unset)",
        APP_CONFIG.trust_proxy_headers,
        APP_CONFIG.auth_cookie_secure,
    )
    if APP_CONFIG.app_env == "production" and not APP_CONFIG.trust_proxy_headers:
        logger.warning(
            "[BOOT] TRUST_PROXY_HEADERS=false in production. "
            "If this server runs behind a reverse proxy (nginx, Caddy, Apache, etc.) "
            "auth rate-limiting and IP logging will see the proxy's IP instead of the "
            "real client IP. Set TRUST_PROXY_HEADERS=true when a trusted proxy forwards "
            "X-Forwarded-For. See docs/hosting-access-guide.md for setup details."
        )
    maybe_start_ngrok()
    threading.Thread(target=print_urls, daemon=True).start()
    # ── GPU TTS startup (non-blocking: failure logged but not fatal) ──────────
    global _tts_startup_error
    if _TTS_AVAILABLE and _startup_tts is not None:
        try:
            await _startup_tts()
        except Exception as _tts_err:
            _tts_startup_error = str(_tts_err)
            logger.warning("[TTS] startup failed (non-fatal): %s", _tts_err)
    yield
    # Clean shutdown: terminate any supervised chat-bridge child processes.
    try:
        from server.chat_bridge_supervisor import bridge_supervisor
        await bridge_supervisor.shutdown_all()
    except Exception:
        logger.exception("[chat-bridge] supervisor shutdown failed")

app = FastAPI(title="D&D Tabletop Phase 1", lifespan=lifespan)

if APP_CONFIG.trust_proxy_headers:
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")

# Register auth, campaign claim, and user asset/stat routers
app.include_router(health_router)
app.include_router(auth_router)
app.include_router(claim_router)
app.include_router(assets_router)
app.include_router(stats_router)
app.include_router(sessions_router)
app.include_router(campaigns_router)
app.include_router(asset_library_router)
app.include_router(rules_router)
app.include_router(integrations_router)
app.include_router(assistant_router)
app.include_router(narration_router)
app.include_router(creatures_router)
app.include_router(maps_router)
app.include_router(commercial_router)
app.include_router(character_router)
app.include_router(twitch_ext_router)
app.include_router(chat_bridge_router)
app.include_router(twitch_oauth_router)
app.include_router(overlay_router)

# Creature-library compatibility contract (intentionally declarative).
# Real route implementation lives in server/creatures/routes.py via include_router above.
# Legacy/refactor contract surface expected from main.py:
# - "/api/library/creatures"
# - "/api/library/creatures/{creature_id}"
# - "/api/library/creatures/{creature_id}/variant"
# - "/api/library/creatures/{creature_id}/spawn"
# Spawn behavior contract remains:
# - WebSocket event type: "token_created"
# - Payload flag: "from_bestiary"

# Register GPU TTS router at /api/tts/*
if _TTS_AVAILABLE and _tts_router is not None:
    app.include_router(_tts_router)
else:
    # Fallback status endpoint so the UI can always query TTS engine state even
    # when the optional tts_server dependencies are not installed.
    from fastapi import APIRouter as _APIRouter
    _tts_fallback_router = _APIRouter(prefix="/api/tts", tags=["tts"])

    @_tts_fallback_router.get("/status")
    async def _tts_status_unavailable():
        return JSONResponse({
            "startup_ok": False,
            "tts_available": False,
            "startup_error": _tts_startup_error,
            "stack": {
                "primary_path": "unavailable",
                "fallback_path": "browser_fallback",
                "notes": ["TTS server dependencies not installed — browser speech synthesis only."],
            },
            "chatterbox": {"ready": False},
            "dia":        {"ready": False},
            "kokoro":     {"ready": False},
        })

    app.include_router(_tts_fallback_router)

# Increase max upload size to 100MB
from starlette.middleware.base import BaseHTTPMiddleware
class LargeUploadMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        request._body_size_limit = 100 * 1024 * 1024  # 100MB
        return await call_next(request)
app.add_middleware(LargeUploadMiddleware)

# CSRF protection – double-submit cookie pattern.
# Added last so it becomes the outermost middleware layer (Starlette applies
# middleware in LIFO order), ensuring CSRF validation runs before any route
# or inner middleware processes the request body.
from server.csrf import CSRFMiddleware
app.add_middleware(
    CSRFMiddleware,
    cookie_secure=APP_CONFIG.auth_cookie_secure,
    cookie_samesite=APP_CONFIG.auth_cookie_samesite,
    # The Twitch Extension EBS is called cross-origin from Twitch's hosted
    # iframe and is authenticated by Twitch-signed Extension JWTs, so it cannot
    # participate in the double-submit cookie scheme.
    # The chat-bridge auth endpoint is a service-to-service call from the Node
    # bridge process (no browser, no cookies) authenticated by the shared
    # CHAT_BRIDGE_TOKEN secret, so it cannot carry the CSRF cookie/header pair.
    exempt_prefixes=("/api/twitch/ext/", "/api/chat-bridge/auth"),
)


# Static files + templates
BASE = Path(__file__).parent
static_dir = BASE / "client" / "static"
single_prop_dir = BASE / "vtt_single_props"
static_dir.mkdir(parents=True, exist_ok=True)
# Serve map files with long cache so players don't re-download on refresh

@app.get("/static/maps/{filename}")
async def serve_map(filename: str):
    path = MAPS_DIR / filename
    if not path.exists():
        from fastapi import HTTPException
        raise HTTPException(404)
    return FileResponse(str(path), headers={"Cache-Control": "public, max-age=604800"})


@app.get("/static/importer/portraits/class/{filename}")
async def serve_legacy_class_portrait(filename: str):
    path = resolve_legacy_class_portrait(static_dir, filename)
    if path is None:
        from fastapi import HTTPException
        raise HTTPException(404)
    return FileResponse(str(path), headers={"Cache-Control": "public, max-age=86400"})


@app.get("/static/importer/portraits/classes/{filename}")
async def serve_legacy_classes_portrait(filename: str):
    path = resolve_legacy_class_portrait(static_dir, filename)
    if path is None:
        from fastapi import HTTPException
        raise HTTPException(404)
    return FileResponse(str(path), headers={"Cache-Control": "public, max-age=86400"})

# Serve user-uploaded assets (tokens, power icons) before the generic /static mount
# so uploaded map URLs like /static/user_uploads/maps/... are not swallowed by the
# main static mount and turned into 404s.
USER_ASSETS_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static/user_uploads", StaticFiles(directory=str(USER_ASSETS_DIR)), name="user_uploads")
app.mount("/static", StaticFiles(directory=static_dir), name="static")
if single_prop_dir.exists():
    app.mount("/vtt_single_props", StaticFiles(directory=str(single_prop_dir)), name="vtt_single_props")

templates = Jinja2Templates(directory=str(BASE / "client" / "templates"))
app.include_router(build_pages_router(templates, PUBLIC_DOMAIN, PORT))



def _map_api_response(*, ok: bool, map_entry: dict | None = None, error: str | None = None, status_code: int = 200, details: dict | None = None, **extra):
    payload = {"ok": ok, "map": map_entry, "error": error, "details": details or None}
    if extra:
        payload.update(extra)
    return JSONResponse(payload, status_code=status_code)


async def _parse_json_request(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _map_request_user_id(request: Request) -> str | None:
    user = get_request_user(request)
    if user and user.get('id'):
        return str(user['id'])
    return None


def _map_is_editable(entry: dict | None, request: Request | None = None) -> bool:
    if not entry:
        return False
    source_type = str(entry.get('source_type') or '').lower()
    if source_type in {'builtin', 'built_in'}:
        return False
    owner_user_id = str(entry.get('owner_user_id') or '').strip()
    requester_user_id = _map_request_user_id(request) if request is not None else None
    if owner_user_id and requester_user_id and owner_user_id != requester_user_id:
        return False
    return True


def _sanitize_map_update_payload(payload: dict) -> dict:
    allowed = {'title', 'description', 'tags', 'map_scope', 'grid_type', 'width_cells', 'height_cells', 'scale', 'scale_label', 'image_style', 'archived', 'thumbnail_url', 'thumbnail_override_url', 'transform'}
    clean = {key: payload.get(key) for key in allowed if key in payload}
    if 'tags' in clean and isinstance(clean['tags'], str):
        clean['tags'] = [tag.strip() for tag in clean['tags'].split(',') if tag.strip()]
    transform = payload.get('transform') or {}
    if any(key in payload for key in ('offset_x', 'offset_y', 'origin_x', 'origin_y', 'rotation', 'snap_to_grid')):
        transform = dict(transform)
        for key in ('offset_x', 'offset_y', 'origin_x', 'origin_y', 'rotation', 'snap_to_grid'):
            if key in payload:
                transform[key] = payload.get(key)
    if transform:
        clean['transform'] = transform
    return clean

# ---------------------------------------------------------------------------
# HTTP Routes
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------

# ── Existing Cartographer API ────────────────────────────────────────────────
# Token image upload is handled by server/sessions/routes.py via sessions_router.

@app.get("/api/cartographer/presets")
async def api_cartographer_presets(request: Request):
    """Return all built-in presets for the AI Cartographer UI."""
    auth_user = get_request_user(request)
    if not auth_user:
        return JSONResponse({"error": "Authentication required"}, status_code=401)
    from server.handlers.cartographer import get_presets_manifest
    return JSONResponse(get_presets_manifest())


@app.post("/api/cartographer/generate")
async def api_cartographer_generate(request: Request):
    """
    AI Cartographer map generation pipeline.

    Body (all fields optional):
      description       – free-text DM description
      map_scope         – world | region | local_area | settlement | interior
      output_mode       – illustrated_overview | tactical_grid | hybrid
      terrain_preset    – key from TERRAIN_PRESETS
      build_preset      – key from BUILD_PRESETS
      interior_preset   – key from INTERIOR_PRESETS
      image_style       – atlas | painterly | inkwash | tactical | realistic | dark | ancient | vibrant
      grid_type         – none | square | hex
      grid_scale        – 5ft | 10ft | 25ft | 50ft | custom
      dimensions_preset – tiny | small | medium | large | huge | custom
      grid_width        – explicit cell width (overrides preset)
      grid_height       – explicit cell height (overrides preset)
      pixel_export_size – target image pixel size (1024..4096)
      detail_density    – low | medium | high
      poi_density       – low | medium | high

    Returns editor-importable result with plan + image.
    DM-only: requires authenticated DM session.
    """
    auth_user = get_request_user(request)
    if not auth_user:
        return JSONResponse({"error": "Authentication required"}, status_code=401)
    if str(auth_user.get("role") or "").strip().lower() not in ("dm", "assistant_dm"):
        return JSONResponse({"error": "DM access required"}, status_code=403)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    # Basic sanitization
    allowed_modes = {"illustrated_overview", "tactical_grid", "hybrid"}
    allowed_scopes = {"world", "region", "local_area", "settlement", "interior"}
    allowed_grid_types = {"none", "square", "hex"}

    request_data = {
        "description": str(body.get("description") or "")[:800],
        "map_scope": body.get("map_scope", "interior") if body.get("map_scope") in allowed_scopes else "interior",
        "output_mode": body.get("output_mode", "illustrated_overview") if body.get("output_mode") in allowed_modes else "illustrated_overview",
        "terrain_preset": str(body.get("terrain_preset") or "")[:64],
        "build_preset": str(body.get("build_preset") or "")[:64],
        "interior_preset": str(body.get("interior_preset") or "")[:64],
        "image_style": str(body.get("image_style") or "atlas")[:32],
        "grid_type": body.get("grid_type", "square") if body.get("grid_type") in allowed_grid_types else "square",
        "grid_scale": str(body.get("grid_scale") or "5ft")[:16],
        "dimensions_preset": str(body.get("dimensions_preset") or "medium")[:16],
        "grid_width": int(body["grid_width"]) if body.get("grid_width") else None,
        "grid_height": int(body["grid_height"]) if body.get("grid_height") else None,
        "pixel_export_size": min(4096, max(512, int(body.get("pixel_export_size") or 2048))),
        "detail_density": str(body.get("detail_density") or "medium")[:16],
        "poi_density": str(body.get("poi_density") or "medium")[:16],
    }

    try:
        from server.handlers.cartographer import generate_map
        result = await generate_map(request_data)
        if body.get("save_to_library", True):
            editor_import = result.get("editor_import") or {}
            plan = result.get("plan") or {}
            image = result.get("image") or {}
            saved = save_library_map({
                "title": str(body.get("title") or editor_import.get("title") or plan.get("title") or "Generated Map"),
                "description": str(body.get("description") or plan.get("summary") or "")[:4000],
                "source_type": "generated",
                "asset_source_type": "generated",
                "map_scope": request_data.get("map_scope", "interior").replace("local_area", "location").replace("settlement", "location").replace("world", "region"),
                "terrain": request_data.get("terrain_preset") or "",
                "build_type": request_data.get("build_preset") or "",
                "interior_type": request_data.get("interior_preset") or "",
                "output_mode": request_data.get("output_mode") or "",
                "image_style": request_data.get("image_style") or "",
                "grid_type": editor_import.get("grid_type") or request_data.get("grid_type") or "square",
                "scale_label": str(editor_import.get("grid_scale") or request_data.get("grid_scale") or "5 ft").replace("ft", " ft"),
                "width_cells": editor_import.get("grid_width") or request_data.get("grid_width"),
                "height_cells": editor_import.get("grid_height") or request_data.get("grid_height"),
                "width_px": image.get("width"),
                "height_px": image.get("height"),
                "thumbnail_url": image.get("url"),
                "full_map_url": image.get("url"),
                "preview_url": image.get("url"),
                "generation_prompt_text": plan.get("image_prompt") or request_data.get("description") or "",
                "generation_payload_json": request_data,
                "normalized_spec_json": plan,
                "map_data_json": editor_import,
                "tags": list(dict.fromkeys([*(plan.get("terrain_tags") or []), *(plan.get("build_tags") or []), *(body.get("theme_tags") or []), *(body.get("atmosphere_tags") or []), *(request_data.get("interior_preset") and [request_data.get("interior_preset")] or [])]))[:16],
                "theme_tags": body.get("theme_tags") or [],
                "atmosphere_tags": body.get("atmosphere_tags") or [],
                "metadata_json": {
                    "result_id": result.get("result_id"),
                    "provider": image.get("provider"),
                    "stub": image.get("stub", False),
                    "asset_source_type": "generated",
                    "availability": "ready",
                },
            })
            result["library_entry"] = saved
        return JSONResponse(result)
    except Exception as exc:
        import logging
        logging.getLogger(__name__).error("[Cartographer] generate error: %s", exc)
        return JSONResponse({"error": "Map generation failed", "detail": str(exc)}, status_code=500)


@app.post("/api/ai/generate-map")
async def api_ai_generate_map(request: Request):
    return await api_cartographer_generate(request)


@app.post("/api/cartographer/generate-interior")
async def api_cartographer_generate_interior(request: Request):
    """
    Generate a linked interior map from a POI.

    Body:
      poi_id            – ID of the parent POI
      poi_name          – name of the POI
      poi_type          – type: cave | tavern | dungeon | castle | etc.
      parent_context    – { title, grid_scale, image_style, terrain_preset }
      output_mode       – tactical_grid | hybrid (default: tactical_grid)
      interior_preset   – override auto-detected preset
      description       – additional DM description
      grid_type         – square | hex (default: square)
      grid_scale        – 5ft | 10ft | etc.
      dimensions_preset – tiny | small | medium | large | huge | custom
      grid_width        – explicit cell width
      grid_height       – explicit cell height
      pixel_export_size – target image pixel size
      image_style       – art style override

    Returns editor-importable result with interior_link metadata.
    DM-only: requires authenticated DM session.
    """
    auth_user = get_request_user(request)
    if not auth_user:
        return JSONResponse({"error": "Authentication required"}, status_code=401)
    if str(auth_user.get("role") or "").strip().lower() not in ("dm", "assistant_dm"):
        return JSONResponse({"error": "DM access required"}, status_code=403)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    allowed_modes = {"illustrated_overview", "tactical_grid", "hybrid"}
    allowed_grid_types = {"none", "square", "hex"}

    parent_ctx = body.get("parent_context") or {}

    request_data = {
        "poi_id": str(body.get("poi_id") or "")[:64],
        "poi_name": str(body.get("poi_name") or "")[:120],
        "poi_type": str(body.get("poi_type") or "")[:64].lower(),
        "parent_context": {
            "title": str(parent_ctx.get("title") or "")[:120],
            "grid_scale": str(parent_ctx.get("grid_scale") or "5ft")[:16],
            "image_style": str(parent_ctx.get("image_style") or "atlas")[:32],
            "terrain_preset": str(parent_ctx.get("terrain_preset") or "")[:64],
        },
        "output_mode": body.get("output_mode", "tactical_grid") if body.get("output_mode") in allowed_modes else "tactical_grid",
        "interior_preset": str(body.get("interior_preset") or "")[:64],
        "description": str(body.get("description") or "")[:800],
        "grid_type": body.get("grid_type", "square") if body.get("grid_type") in allowed_grid_types else "square",
        "grid_scale": str(body.get("grid_scale") or parent_ctx.get("grid_scale") or "5ft")[:16],
        "dimensions_preset": str(body.get("dimensions_preset") or "medium")[:16],
        "grid_width": int(body["grid_width"]) if body.get("grid_width") else None,
        "grid_height": int(body["grid_height"]) if body.get("grid_height") else None,
        "pixel_export_size": min(4096, max(512, int(body.get("pixel_export_size") or 2048))),
        "image_style": str(body.get("image_style") or parent_ctx.get("image_style") or "atlas")[:32],
        "detail_density": str(body.get("detail_density") or "high")[:16],
        "poi_density": str(body.get("poi_density") or "medium")[:16],
    }

    try:
        from server.handlers.cartographer import generate_interior
        result = await generate_interior(request_data)
        return JSONResponse(result)
    except Exception as exc:
        import logging
        logging.getLogger(__name__).error("[Cartographer] interior error: %s", exc)
        return JSONResponse({"error": "Interior generation failed", "detail": str(exc)}, status_code=500)


async def _websocket_heartbeat_loop(
    *,
    websocket: WebSocket,
    session_id: str,
    user_id: str,
    connection_id: str,
    last_pong: dict,
    ping_interval: float = 30,
    pong_timeout: float = 60,
    connection_manager=manager,
):
    """Send heartbeat pings only while this task owns the current socket."""
    logger.debug("[WS] heartbeat start user_id=%s connection_id=%s", user_id, connection_id)
    try:
        while True:
            await asyncio.sleep(ping_interval)
            if not connection_manager.is_current_connection(session_id, user_id, connection_id):
                logger.debug("[WS] heartbeat stale exit user_id=%s connection_id=%s", user_id, connection_id)
                return
            if asyncio.get_running_loop().time() - last_pong["t"] > pong_timeout:
                if connection_manager.is_current_connection(session_id, user_id, connection_id):
                    logger.warning(
                        "[WS] timeout closing current socket user_id=%s connection_id=%s",
                        user_id,
                        connection_id,
                    )
                    try:
                        await websocket.close(code=1001, reason="Heartbeat timeout")
                    except Exception:
                        pass
                else:
                    logger.debug("[WS] heartbeat stale exit user_id=%s connection_id=%s", user_id, connection_id)
                return
            logger.debug("[WS] sending ping user_id=%s session_id=%s connection_id=%s", user_id, session_id, connection_id)
            await connection_manager.send_to(session_id, user_id, {"type": "ping"})
    except asyncio.CancelledError:
        raise
    except Exception as _hb_err:
        logger.error("[HEARTBEAT] Task crashed for session %s user %s: %s", session_id, user_id, _hb_err)


@app.websocket("/ws/{session_id}/{user_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str, user_id: str, token: str = None, client_socket_id: str = None, reason: str = None):
    """WebSocket endpoint.

    ``token`` is a JWT passed as a query parameter (e.g. ``?token=<jwt>``).
    When ``DND_JWT_SECRET`` is set, DM and player sockets must provide a valid
    token; viewers and legacy no-secret sessions can still connect without one.
    Provided tokens are always validated and closed with code 4001 on failure.
    """
    logger.info("[live_state] websocket_connect session_id=%s user_id=%s client_socket_id=%s reason=%s", session_id, user_id, client_socket_id, reason)
    session = get_session(session_id)
    # Auto-restore from DB if not in memory
    if not session:
        loop = asyncio.get_running_loop()
        db_data = await loop.run_in_executor(None, load_campaign, session_id)
        if db_data:
            session, _ = restore_session(db_data)
            logger.info("[live_state] session_restore session_id=%s user_id=%s restored=%s", session_id, user_id, bool(session))
            if session:
                restored_active_id = str((getattr(session, "active_char_profiles", {}) or {}).get(user_id) or "")
                logger.info(
                    "[live_state] active_profile_restored session_id=%s user_id=%s active_profile_id=%s",
                    session_id, user_id, restored_active_id or "(none)",
                )
    if not session:
        await websocket.close(code=4004, reason="Session not found")
        return

    user = session.users.get(user_id)

    # Allow the chat bridge to auto-create its service-account user on first connect.
    # The JWT must carry role="chat_bridge" and sub="chat_bridge" for this path.
    if not user and user_id == "chat_bridge" and token:
        _pre_jwt = verify_token(token)
        if _pre_jwt and str(_pre_jwt.get("role") or "") == "chat_bridge":
            from server.session import User as _User
            user = _User(id="chat_bridge", name="Chat Bridge", role="chat_bridge")
            session.users["chat_bridge"] = user

    # Allow overlay clients to auto-create their ephemeral read-only user on connect.
    # user_id must be "overlay_game" or "overlay_arena"; JWT must carry role="overlay".
    if not user and user_id in ("overlay_game", "overlay_arena") and token:
        _pre_jwt = verify_token(token)
        if _pre_jwt and str(_pre_jwt.get("role") or "") == "overlay":
            from server.session import User as _User
            user = _User(id=user_id, name="Overlay", role="overlay")
            session.users[user_id] = user

    if not user:
        await websocket.close(code=4003, reason="User not found in session")
        return

    jwt_payload = None
    if token:
        jwt_payload = verify_token(token)
        if not jwt_payload:
            await websocket.close(code=4001, reason="Invalid or expired token")
            return
        # Verify the JWT subject matches the user_id path parameter to prevent
        # identity spoofing (connecting with a valid JWT but another user's id).
        token_sub = str(jwt_payload.get("sub") or "").strip()
        if token_sub and token_sub != str(user_id or "").strip():
            await websocket.close(code=4001, reason="Token identity does not match user_id")
            return
    _role_lower = str(getattr(user, "role", "") or "").strip().lower()
    if _auth_is_enforced() and _role_lower in {"dm", "player", "chat_bridge"} and not jwt_payload:
        await websocket.close(code=4001, reason="Missing or invalid token")
        return

    _user_agent = None
    try:
        _user_agent = websocket.headers.get("user-agent")
    except Exception:
        _user_agent = None
    connection_id = await manager.connect(
        session_id, user_id, websocket, role=user.role,
        client_socket_id=client_socket_id, reason=reason, user_agent=_user_agent,
    )
    user.connected = True

    # The session is live: auto-start the supervised chat bridge if this
    # campaign has a connected Twitch channel with the chat bridge enabled.
    # (No-op otherwise; bridge failures never propagate to the game server.)
    if user.role == 'dm':
        try:
            from server.chat_bridge_supervisor import bridge_supervisor
            asyncio.create_task(bridge_supervisor.ensure_started(session_id))
        except Exception:
            logger.exception("[chat-bridge] auto-start scheduling failed")

    # Send full state on connect (DM gets POI dm_notes, others don't).
    # Reconnects (client passes ?reason=reconnect) skip this unconditional
    # send: every current client immediately issues request_state with its
    # known per-domain revisions, and handle_request_state answers with a
    # delta that omits unchanged heavy domains (server/state_delta.py).
    defer_state_to_request = str(reason or "").strip().lower() == "reconnect"
    if defer_state_to_request:
        logger.info(
            "[live_state] initial_state_sync deferred to request_state (reconnect delta) session_id=%s user_id=%s",
            session_id, user_id,
        )
    else:
        from server.state_delta import apply_reconnect_delta
        state = session.to_state_dict_for_role(user.role, user_id)
        # Fresh connect: nothing known client-side, so no domain is omitted —
        # this only attaches domain_revisions for the client to echo later.
        apply_reconnect_delta(state, None)
        logger.info("[live_state] initial_state_sync %s", build_live_state_debug_summary(session, user_id, user.role, state))
        await manager.send_to(session_id, user_id, {
            "type": "state_sync",
            "payload": state
        })
        try:
            setattr(user, "_last_initial_state_sync_at", time.monotonic())
        except Exception:
            pass
        snapshot_v2 = session.to_authoritative_snapshot_for_role(user.role, user_id, source="ws_connect")
        snapshot_payload = snapshot_v2.get("payload") if isinstance(snapshot_v2.get("payload"), dict) else {}
        character_block = snapshot_payload.get("character") if isinstance(snapshot_payload.get("character"), dict) else {}
        inventory_block = snapshot_payload.get("inventory") if isinstance(snapshot_payload.get("inventory"), dict) else {}
        spells_block = snapshot_payload.get("spells") if isinstance(snapshot_payload.get("spells"), dict) else {}
        logger.info(
            "[live_state] snapshot_character_block session_id=%s user_id=%s active_profile_id=%s character_hydration=%s inventory_hydration=%s spells_hydration=%s",
            session_id, user_id,
            character_block.get("active_profile_id") or "",
            character_block.get("hydration_status") or "unknown",
            inventory_block.get("hydration_status") or "unknown",
            spells_block.get("hydration_status") or "unknown",
        )
        await manager.send_to(session_id, user_id, snapshot_v2)

    # Send item library sync with only the SRD version.  The SRD list is large,
    # so clients request it separately only when their local versioned cache is
    # missing or stale.
    await manager.send_to(session_id, user_id, {
        "type": "item_library_sync",
        "payload": {
            "entries": list(getattr(session, "item_library_entries", []) or []),
            "srd_items_version": get_srd_items_version(),
        },
    })

    # Notify DM and others of join/reconnect
    await manager.broadcast(session_id, {
        "type": "user_joined",
        "payload": {
            "user": {"handle": _display_user_handle(session_id, user.id), "name": user.name, "role": user.role, "subgroup_id": session.get_user_subgroup_id(user.id)},
        }
    }, exclude_user=user_id)

    # Send init_audio so reconnecting clients restore ambient state (Task 5)
    sound_state = getattr(session, "sound_state", None) or {}
    ambient_track = sound_state.get("track", "silence")
    ambient_volume = sound_state.get("volume", 0.7)
    if ambient_track and ambient_track != "silence":
        await manager.send_to(session_id, user_id, {
            "type": "init_audio",
            "payload": {
                "ambient": ambient_track,
                "ambient_volume": ambient_volume,
            }
        })

    # Auto-save every 60 seconds for DM
    async def autosave_task():
        try:
            while manager.is_connected(session_id, user_id):
                await asyncio.sleep(60)
                if user.role == 'dm' and manager.is_connected(session_id, user_id):
                    try:
                        await save_campaign_async(session)
                    except Exception as _save_err:
                        logger.error("[AUTOSAVE] Failed for session %s: %s", session_id, _save_err)
        except Exception as _task_err:
            logger.error("[AUTOSAVE] Task crashed for session %s user %s: %s", session_id, user_id, _task_err)

    autosave_handle = None
    if user.role == 'dm':
        autosave_handle = asyncio.create_task(autosave_task())

    # Heartbeat: detect silent disconnections (AFK / network drop)
    _last_pong = {"t": asyncio.get_running_loop().time()}

    heartbeat_handle = asyncio.create_task(_websocket_heartbeat_loop(
        websocket=websocket,
        session_id=session_id,
        user_id=user_id,
        connection_id=connection_id,
        last_pong=_last_pong,
    ))

    try:
        while True:
            raw_text = await websocket.receive_text()
            try:
                raw = json.loads(raw_text)
            except json.JSONDecodeError:
                continue

            # Decode the message type for heartbeat handling and downstream dispatch.
            msg_type = str(raw.get("type") or "")

            # Heartbeat liveness: any valid frame the client sends proves the
            # socket is alive, so refresh the last-seen timestamp here rather than
            # only on pong. This stops active play (chat, combat, movement) from
            # tripping a false heartbeat timeout when a pong happens to be delayed.
            _last_pong["t"] = asyncio.get_running_loop().time()
            logger.debug("[WS] received frame type=%s user_id=%s connection_id=%s last_seen updated", msg_type or "(missing)", user_id, connection_id)

            # Handle heartbeat pong — already counted above; skip gameplay dispatch.
            if msg_type == "pong":
                logger.debug("[WS] pong received user_id=%s connection_id=%s", user_id, connection_id)
                continue

            # Role permission policy is centralized in server.handlers.ws_permissions
            # and enforced by handle_message immediately before dispatch. Keep this
            # endpoint focused on transport/auth/heartbeat concerns so role allow-lists
            # cannot drift from the canonical handler policy.

            try:
                await handle_message(raw, session, user)
            except Exception as e:
                logger.error("[WS] handler error for %s: %s", raw.get('type'), e, exc_info=True)
                try:
                    await manager.send_to(session_id, user_id, {
                        "type": "error",
                        "payload": {"message": "Something went wrong. Please try again."}
                    })
                except Exception:
                    pass

    except WebSocketDisconnect:
        removed_current = manager.disconnect(session_id, user_id, websocket)
        if removed_current:
            user.connected = False
            # Auto-save when DM disconnects
            if user.role == 'dm':
                await save_campaign_async(session)
            leave_log = session.add_log(f"{user.name} ({user.role}) disconnected.", "system")
            await manager.broadcast(session_id, {
                "type": "user_left",
                "payload": {
                    "user_id": user_id,
                    "user_name": user.name,
                    "role": user.role,
                    "log": leave_log,
                }
            })
        # When the last connection for this session is gone, tear down its
        # deferred timers: cancel pending move flushes (so no late send fires
        # against a dead session), then flush the debounced durability save once
        # synchronously so the final ~2s window isn't lost on clean shutdown.
        if not manager.get_session_connections(session_id):
            cancel_session_flushes(session_id)
            await flush_session_durability(session)
    finally:
        heartbeat_handle.cancel()
        if autosave_handle is not None:
            autosave_handle.cancel()
        await asyncio.gather(heartbeat_handle, *( [autosave_handle] if autosave_handle is not None else [] ), return_exceptions=True)

@app.post("/api/generate_loot")
async def api_generate_loot(request: Request):
    """Level-aware loot generation endpoint (DM only).

    Request body:
        dungeon_level  int   1-20
        chest_id       str   prop ID of the target chest (required when confirmed=true)
        session_id     str   active session ID
        confirmed      bool  if false: return preview; if true: apply to chest

    Returns a preview payload or a success confirmation.
    """
    from server.session import get_session
    from server.handlers.inventory import generate_loot_preview, apply_loot_to_chest
    from server.connections import manager as _mgr

    body = {}
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body."}, status_code=400)

    session_id = str(body.get("session_id") or "").strip()
    dungeon_level = max(1, min(20, int(body.get("dungeon_level") or 1)))
    chest_id = str(body.get("chest_id") or "").strip()[:64]
    confirmed = bool(body.get("confirmed", False))

    if not session_id:
        return JSONResponse({"error": "session_id is required."}, status_code=400)

    session = get_or_restore_session(session_id)
    if session is None:
        return JSONResponse({"error": "Session not found."}, status_code=404)

    if not request_has_dm_access(request, session, fallback_user_id=str(body.get("user_id") or "")):
        return JSONResponse({"error": "DM role required."}, status_code=403)

    preview = generate_loot_preview(dungeon_level)

    if not confirmed:
        return JSONResponse({"preview": True, **preview})

    # confirmed=True: apply to chest and broadcast
    if not chest_id:
        return JSONResponse({"error": "chest_id is required when confirmed=true."}, status_code=400)

    applied = apply_loot_to_chest(session, chest_id, preview)
    if not applied:
        return JSONResponse({"error": f"Prop '{chest_id}' not found in session."}, status_code=404)

    mark_session_dirty(session)
    await save_campaign_async(session)

    # Broadcast updated props so every connected client sees refreshed chest contents.
    import asyncio
    props_snapshot = dict(getattr(session, "editor_props", {}) or {})
    asyncio.ensure_future(_mgr.broadcast(session_id, {
        "type": "editor_props_sync",
        "payload": {"props": props_snapshot},
    }))

    return JSONResponse({
        "confirmed": True,
        "dungeon_level": dungeon_level,
        "gold": preview["gold"],
        "items": preview["items"],
        "message": f"Added {len(preview['items'])} items and {preview['gold']} gp to chest.",
    })


@app.post("/api/chest/{chest_id}/add_loot")
async def api_chest_add_loot(chest_id: str, request: Request):
    """Save pre-generated loot to a chest prop (DM only).

    This is the second step of the two-step loot flow:
      1. POST /api/generate_loot (preview only, never writes to DB)
      2. POST /api/chest/{chest_id}/add_loot (called when DM clicks "Add to Chest")

    Request body:
        session_id   str   active session ID
        user_id      str   DM user ID (fallback when JWT cookie absent)
        gold         int   gold pieces to add
        items        list  list of {name, qty, rarity} dicts

    Returns 200 with {"confirmed": true, "gold": N, "items": [...], "message": "..."}
    or an error response with a descriptive message.
    """
    from server.session import get_session
    from server.handlers.inventory import apply_loot_to_chest
    from server.connections import manager as _mgr

    chest_id = str(chest_id or "").strip()[:64]
    if not chest_id:
        return JSONResponse({"error": "chest_id is required."}, status_code=400)

    body = {}
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body."}, status_code=400)

    session_id = str(body.get("session_id") or "").strip()
    if not session_id:
        return JSONResponse({"error": "session_id is required."}, status_code=400)

    session = get_or_restore_session(session_id)
    if session is None:
        return JSONResponse({"error": "Session not found."}, status_code=404)

    if not request_has_dm_access(request, session, fallback_user_id=str(body.get("user_id") or "")):
        return JSONResponse({"error": "DM role required."}, status_code=403)

    # Validate and sanitise loot payload
    try:
        gold = max(0, int(body.get("gold") or 0))
    except (TypeError, ValueError):
        gold = 0

    raw_items = body.get("items")
    items: list = []
    if isinstance(raw_items, list):
        for it in raw_items[:200]:  # cap at 200 items
            if isinstance(it, dict):
                items.append({
                    "name": str(it.get("name") or "Item")[:80],
                    "qty": max(1, int(it.get("qty") or 1)),
                    "rarity": str(it.get("rarity") or "")[:40],
                })

    loot_data = {"gold": gold, "items": items}

    applied = apply_loot_to_chest(session, chest_id, loot_data)
    if not applied:
        return JSONResponse(
            {"error": "Chest not found", "chest_id": chest_id},
            status_code=404,
        )

    mark_session_dirty(session)
    await save_campaign_async(session)

    # Broadcast updated props so every connected client sees the refreshed chest.
    import asyncio
    props_snapshot = dict(getattr(session, "editor_props", {}) or {})
    asyncio.ensure_future(_mgr.broadcast(session_id, {
        "type": "editor_props_sync",
        "payload": {"props": props_snapshot},
    }))

    return JSONResponse({
        "confirmed": True,
        "chest_id": chest_id,
        "gold": gold,
        "items": items,
        "message": f"Added {len(items)} item{'s' if len(items) != 1 else ''} and {gold} gp to chest.",
    })


@app.get("/api/srd_items/count")
async def api_srd_item_count():
    """Return the count of SRD items in the library database."""
    from server.rules_db import get_srd_item_count
    count = get_srd_item_count()
    return JSONResponse({"count": count})


if __name__ == "__main__":
    import uvicorn
    # ws_per_message_deflate: explicit so WebSocket compression can never be
    # lost to a changed uvicorn default. The repetitive JSON in state/token
    # sync compresses ~5-10x on the wire (see [ws] outbound_send
    # deflate_estimate_bytes in the payload logs).
    uvicorn.run(app, host="0.0.0.0", port=PORT, reload=False, access_log=False, ws_per_message_deflate=True)
