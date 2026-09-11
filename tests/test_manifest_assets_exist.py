"""Guardrails for audio manifest asset references and quiet procedural fallback."""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLIENT = ROOT / "client"
MANIFEST_PATH = CLIENT / "static/assets/audio/manifest.json"
SOUND_ENGINE_PATH = CLIENT / "static/js/ui/sound_engine.js"


def _client_path_from_static_url(url: str) -> Path:
    path = url.split("?", 1)[0]
    assert path.startswith("/static/"), f"manifest file URL must be /static/: {url}"
    return CLIENT / path.removeprefix("/")


def test_audio_manifest_files_exist_or_have_quiet_safe_fallback():
    from server.ambient_audio import _TRACK_TO_FAMILY

    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    missing = []
    for track, entry in manifest.get("tracks", {}).items():
        fallback = str(entry.get("fallback") or "")
        asset_probe = entry.get("asset_probe")
        for file_url in entry.get("files") or []:
            path = _client_path_from_static_url(str(file_url))
            if path.exists():
                continue
            # Repo-safe audio binaries are intentionally generated during the
            # FastAPI lifespan. A missing source-tree asset is therefore safe
            # only when the track is covered by that startup generator (or the
            # manifest explicitly marks the probe as startup-generated).
            startup_generated = asset_probe == "startup_generated" or str(track).strip().lower() in _TRACK_TO_FAMILY
            if fallback.startswith("procedural_") and startup_generated:
                continue
            missing.append(f"{track}: {file_url} missing without startup-generated procedural fallback")
    assert not missing, "\n".join(missing)


def test_sound_engine_skips_static_probe_for_startup_generated_fallback_assets():
    src = SOUND_ENGINE_PATH.read_text(encoding="utf-8")
    assert "entry.asset_probe === 'startup_generated'" in src
    assert re.search(r"entry\.asset_probe === 'startup_generated'[\s\S]+?return null;", src)
    assert "asset unavailable" in src
    assert "decode/playback prep failed" in src


def test_startup_generated_manifest_enables_asset_probe_after_generation(tmp_path):
    from server.ambient_audio import ensure_ambient_audio_assets

    ensure_ambient_audio_assets(tmp_path)
    generated = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert generated["tracks"]["forest"]["asset_probe"] == "enabled"
    assert (tmp_path / "forest_loop_20260328.wav").exists()
