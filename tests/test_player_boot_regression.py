import re
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLAY = ROOT / "client/templates/play.html"


def _play_src() -> str:
    return PLAY.read_text(encoding="utf-8")


def test_player_page_loaded_scripts_have_no_top_level_syntax_errors():
    """Boot smoke: every script loaded by play.html must parse cleanly."""
    src = _play_src()
    script_tags = re.findall(r'<script\b([^>]*)>', src, re.I)
    checked = []
    module_checked = []
    for attrs in script_tags:
        src_match = re.search(r'\bsrc=["\']([^"\']+)["\']', attrs, re.I)
        if not src_match:
            continue
        url = src_match.group(1)
        static_url = url.split("?", 1)[0]
        if not static_url.startswith("/static/") or not static_url.endswith(".js"):
            continue
        path = ROOT / "client/static" / static_url[len("/static/"):]
        assert path.exists(), f"play.html references missing script {static_url}"

        type_match = re.search(r'\btype=["\']([^"\']+)["\']', attrs, re.I)
        script_type = (type_match.group(1).strip().lower() if type_match else "")
        if script_type == "module":
            # package.json declares "type": "commonjs", so `node --check file.js`
            # parses .js files as CommonJS. Browser module scripts need an .mjs
            # temporary copy so top-level `import`/`export` syntax is checked in
            # module mode without changing the runtime file or package type.
            with tempfile.NamedTemporaryFile("w", suffix=".mjs", encoding="utf-8", delete=False) as handle:
                handle.write(path.read_text(encoding="utf-8"))
                temp_module_path = handle.name
            subprocess.run(["node", "--check", temp_module_path], cwd=ROOT, check=True, text=True, capture_output=True, timeout=30)
            module_checked.append(static_url)
        else:
            subprocess.run(["node", "--check", str(path)], cwd=ROOT, check=True, text=True, capture_output=True, timeout=30)
        checked.append(static_url)
    assert "/static/js/render/fog.js" in checked
    assert "/static/js/ui/dm_assistant.js" in checked
    assert "/static/js/dice/dice3d.js" in module_checked

    inline_scripts = re.findall(r'<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>', src, re.S)
    main_inline = max(inline_scripts, key=len)
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as handle:
        handle.write(main_inline)
        temp_path = handle.name
    subprocess.run(["node", "--check", temp_path], cwd=ROOT, check=True, text=True, capture_output=True, timeout=30)


def test_player_boot_does_not_eagerly_call_dm_only_or_heavy_endpoints():
    play = _play_src()
    assistant_init = play[play.index("(function _integrationStatusInit()"):play.index("// ═══════════════════════════════════════════════════════════════════", play.index("(function _integrationStatusInit()"))]
    assert "String(ROLE || '').toLowerCase() === 'dm'" in assistant_init
    assert "if (!_isDmBoot || _safeOff) return;" in assistant_init

    preload = play[play.index("(function _schedulePreloadSounds()"):play.index("/* ── UI click sounds", play.index("(function _schedulePreloadSounds()"))]
    assert "if (String(ROLE || '').toLowerCase() !== 'dm') return;" in preload
    assert "SFX_KEYS.forEach" in preload
    assert "AMBIENT_KEYS.forEach" in preload

    assistant = (ROOT / "client/static/js/ui/dm_assistant.js").read_text(encoding="utf-8")
    mount = assistant[assistant.index("mount() {"):assistant.index("setRole(role)")]
    assert "role !== 'dm'" in mount
    assert "this.refreshStatus();" in mount


def test_tts_client_metadata_fetches_are_dm_role_gated():
    tts = (ROOT / "client/static/tts_client.js").read_text(encoding="utf-8")
    init = tts[tts.index("async function _init()"):tts.index("if (document.readyState === 'loading')", tts.index("async function _init()"))]
    assert "document.getElementById('narration-voice-preset') !== null" not in init
    assert "new URLSearchParams(window.location.search || '').get('role')" in init
    assert "const isDM = role === 'dm';" in init
    assert "await _populateVoiceDropdown();" in init
    assert "await _renderWarmupPhrases();" in init


def test_player_boot_no_tts_metadata_fetches_under_player_role():
    script = r"""
global.window = global;
window.location = { search: '?role=player' };
window.ROLE = 'player';
const calls = [];
global.fetch = (url) => { calls.push(String(url)); return Promise.resolve({ ok: true, json: async () => ({ grouped: {}, phrases: [] }) }); };
global.console = { warn: () => {}, error: () => {}, info: () => {}, debug: () => {}, log: console.log };
global.document = {
  readyState: 'complete',
  head: { appendChild: () => {} },
  body: { appendChild: () => {} },
  createElement: (tag) => ({
    tagName: tag, style: {}, className: '', textContent: '', innerHTML: '',
    appendChild: () => {}, addEventListener: () => {}, remove: () => {},
    setAttribute: () => {}, querySelector: () => null, querySelectorAll: () => [],
  }),
  getElementById: (id) => ({
    id, style: {}, value: '', textContent: '', innerHTML: '',
    appendChild: () => {}, addEventListener: () => {}, remove: () => {},
    querySelector: () => null, querySelectorAll: () => [],
  }),
  querySelector: () => null,
  querySelectorAll: () => [],
  addEventListener: () => {},
};
global.Audio = function () { return { addEventListener: () => {}, play: () => Promise.resolve(), pause: () => {} }; };
global.CustomEvent = function CustomEvent(type, init) { return { type, detail: init && init.detail }; };
require('./client/static/tts_client.js');
Promise.resolve().then(() => Promise.resolve()).then(() => {
  console.log(JSON.stringify({ calls }));
});
"""
    result = subprocess.check_output(["node", "-e", script], cwd=ROOT, text=True, timeout=30)
    data = __import__('json').loads(result.strip().splitlines()[-1])
    assert not any('/api/tts/voices' in call for call in data['calls'])
    assert not any('/api/tts/warmup-phrases' in call for call in data['calls'])


def test_autosave_render_stack_is_hard_blocked_not_deferred():
    play = _play_src()
    guard = play[play.index("function scheduleCharProfileAutosave"):play.index("function scheduleCharBookQuickPanelSync")]
    assert "__warnBlockedCharAutosave('scheduleCharProfileAutosave'" in guard
    assert "__deferCharProfileAutosaveUntilRenderUnwinds(reason)" not in guard
    assert "autosave deferred during render/select stack" not in guard
    assert "[char profile] blocked autosave during render" in play


def test_player_state_sync_boot_autosave_deferral_does_not_loop():
    snippet_src = _play_src()
    start = snippet_src.index("function __isCharProfileRenderStackActive")
    end = snippet_src.index("function scheduleCharBookQuickPanelSync")
    autosave_guard = snippet_src[start:end]
    depth_start = snippet_src.index("function __enterDepth(name")
    depth_end = snippet_src.index("function __enablePlayerUiSafeMode")
    depth_guard = snippet_src[depth_start:depth_end]
    script = r'''
global.window = global;
window.__depths = { renderPlayerActionsHub: 1, selectQuickActions: 1, scheduleCharProfileSave: 0, markCharProfileDirty: 0 };
window.__debugTrace = [];
global.traceEnter = () => {};
global.__debugSnapshot = (e) => e || {};
global.console = { error: () => {}, warn: () => {}, info: () => {}, debug: () => {}, log: console.log };
''' + depth_guard + r'''
let ROLE = 'player';
let isApplyingRemoteState = false;
let isHydrating = false;
let isSaving = false;
let _charSheet = { name: 'Player' };
let charProfiles = [];
let _charProfileAutosaveTimer = null;
let marked = 0;
let saved = 0;
let timers = 0;
window.__safeMode = {};
window.__combatApplyStateActive = false;
window.__stateSyncApplying = true;
global.document = { getElementById: () => ({ value: 'Player' }) };
const queuedTimers = [];
global.setTimeout = (fn, ms) => { timers += 1; queuedTimers.push({ fn, ms }); return timers; };
global.clearTimeout = () => {};
function __isRemoteStateOrCombatApplying() { return !!window.__stateSyncApplying; }
function markCharProfileDirty() { marked += 1; return true; }
function saveCurrentCharProfile() { saved += 1; }
''' + autosave_guard + r'''
scheduleCharProfileAutosave('state_sync_boot');
Promise.resolve().then(() => {
  window.__depths.renderPlayerActionsHub = 0;
  window.__depths.selectQuickActions = 0;
  window.__stateSyncApplying = false;
  while (queuedTimers.length) queuedTimers.shift().fn();
  return Promise.resolve();
}).then(() => {
  console.log(JSON.stringify({ marked, saved, timers, depth: window.__depths.scheduleCharProfileSave }));
});
'''
    result = subprocess.check_output(["node", "-e", script], cwd=ROOT, text=True, timeout=30)
    assert result.strip().endswith('{"marked":0,"saved":0,"timers":0,"depth":0}')


from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient
from server.pages.routes import build_router


def _rendered_play(role="player", extra=""):
    app = FastAPI()
    templates = Jinja2Templates(directory=str(ROOT / "client/templates"))
    app.include_router(build_router(templates, "", 8000))
    with TestClient(app) as client:
        resp = client.get(f"/play?session_id=s1&user_id=u1&role={role}{extra}")
    assert resp.status_code == 200
    return resp.text


def _script_srcs(html):
    return re.findall(r'<script[^>]+src="([^"]+)"', html)


def test_rendered_player_loads_full_live_runtime():
    """All interactive roles share the single live play.html runtime.

    Regression guard for the player live-sync outage: a previous build gated the
    whole runtime to DM/assistant_dm and gave players a stubbed boot shell
    (player_boot_stub.js) with no-op initUI/initCanvas/_setWsStatus, leaving them
    with no map, tabs, tokens, character sheet, or quick actions.
    """
    html = _rendered_play("player")
    player_scripts = "\n".join(_script_srcs(html))
    required = [
        "/static/js/editor/serialization.js",
        "/static/js/character/combat_quick_actions.js",
        "/static/js/character/character_sheet_container.js",
        "/static/js/render/boot.js",
        "/static/js/ui/tabs.js",
        "/static/js/ui/player_shell.js",
    ]
    for needle in required:
        assert needle in player_scripts, needle
    # The no-op player/viewer boot stub must no longer be loaded.
    assert "/static/js/core/player_boot_stub.js" not in player_scripts
    assert "PLAYER_BOOT_HTML_LOADED" in html
    assert "Player boot failed before scripts loaded." in html


def test_rendered_dm_manifest_keeps_dm_editor_tools():
    html = _rendered_play("dm")
    for needle in ["/static/js/editor/serialization.js", "/static/js/ui/editor_panel.js", "/static/js/ui/asset_library.js", "/static/js/ui/dm_assistant.js", "/static/js/cartographer.js"]:
        assert needle in html


def test_rendered_player_response_size_sane_and_no_full_char_profiles_embed():
    html = _rendered_play("player")
    # Players now load the full live runtime (same as the DM), so the page is the
    # large single-runtime document rather than a stripped shell.
    assert len(html.encode("utf-8")) < 3_000_000
    assert "field=char_profiles" not in html
    assert "4555336" not in html


def test_returning_player_uses_full_live_runtime():
    html = _rendered_play("player", "&returning=1")
    # Returning players use the same single live runtime (not a stubbed shell).
    assert "/static/js/ui/player_shell.js" in html
    assert "/static/js/editor/serialization.js" in html
    assert "/static/js/character/combat_quick_actions.js" in html
    assert "/static/js/core/player_boot_stub.js" not in html


def test_player_runtime_first_external_js_is_core_boot():
    html = _rendered_play("player")
    scripts = _script_srcs(html)
    # auth.js patches window.fetch to attach the bearer token to same-origin
    # /api/ calls; it must load before every other runtime script (spells_tab.js,
    # character routes, etc.) or those calls fall back to cookie-only auth and
    # 401 after reconnect. The core boot modules load right after it, before
    # the rest of the live runtime.
    assert scripts[0] == "/static/js/auth.js"
    assert scripts[1] == "/static/js/core/diagnostics.js"
    assert scripts[2] == "/static/js/core/csrf.js"
    for core in [
        "/static/js/state/store.js",
        "/static/js/core/runtime_bridge.js",
        "/static/js/core/boot_shell.js",
        "/static/js/core/ws.js?v=heartbeat-pong-v6-seq-gap-watchdog",
        "/static/js/core/message_dispatch.js",
    ]:
        assert core in scripts, core
    # These heavy metadata/audio assets must never be eagerly fetched at boot.
    # (DM-only API calls such as /api/assistant/status are role-gated at runtime;
    # see test_player_boot_does_not_eagerly_call_dm_only_or_heavy_endpoints.)
    forbidden = ["/api/tts/voices", "/api/tts/warmup-phrases", "/static/assets/audio/manifest.json", "battle_loop_20260328.wav"]
    for needle in forbidden:
        assert needle not in html


def test_player_boot_role_is_defined_before_diagnostics_marks_scripts_started():
    """Regression: __PLAY_BOOT_ROLE must be set before diagnostics.js runs so it can
    mark __playerBootState.scriptsStarted; otherwise the player sees a false
    'Player boot failed before scripts loaded.' overlay after a successful boot."""
    html = _rendered_play("player")
    role_assign = 'window.__PLAY_BOOT_ROLE = "player";'
    assert role_assign in html
    diagnostics_tag = '<script src="/static/js/core/diagnostics.js">'
    assert diagnostics_tag in html
    assert html.index(role_assign) < html.index(diagnostics_tag)


def test_diagnostics_first_iife_marks_scripts_started_when_role_preset():
    """diagnostics.js's first IIFE must set scriptsStarted=true when the player role
    is already on window before it runs (the live boot ordering)."""
    diagnostics = (ROOT / "client/static/js/core/diagnostics.js").read_text(encoding="utf-8")
    first_iife = diagnostics[: diagnostics.index("(function (global)")]
    script = r"""
global.window = global;
window.__PLAY_BOOT_ROLE = 'player';
""" + first_iife + r"""
console.log(JSON.stringify({ started: !!(window.__playerBootState && window.__playerBootState.scriptsStarted) }));
"""
    result = subprocess.check_output(["node", "-e", script], cwd=ROOT, text=True, timeout=30)
    data = __import__('json').loads(result.strip().splitlines()[-1])
    assert data["started"] is True
