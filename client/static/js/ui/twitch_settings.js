/**
 * client/static/js/ui/twitch_settings.js
 *
 * DM-only Twitch Integration panel inside the flyout-theme settings flyout.
 * Fetches /api/twitch/status to show connection state and exposes connect /
 * disconnect / enable-toggle controls.
 */
(function () {
  'use strict';

  function getSessionId() {
    return new URLSearchParams(location.search).get('session_id') || '';
  }

  function escHtml(s) {
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function init() {
    var section = document.getElementById('twitch-settings-section');
    if (!section) return;
    if (window.ROLE !== 'dm') return;
    section.removeAttribute('hidden');
    refresh();
  }

  async function refresh() {
    var sessionId = getSessionId();
    if (!sessionId) return;

    var body = document.getElementById('twitch-settings-body');
    if (body) body.innerHTML = '<span style="font-size:0.7rem;color:var(--parchment-dim);">Loading…</span>';

    try {
      var res = await fetch('/api/twitch/status/' + encodeURIComponent(sessionId), {
        credentials: 'include',
      });
      if (!res.ok) { renderPanel(null); return; }
      var data = await res.json();
      renderPanel(data);
    } catch (_) {
      renderPanel(null);
    }
  }

  function renderPanel(data) {
    var body = document.getElementById('twitch-settings-body');
    if (!body) return;

    var sessionId = getSessionId();

    if (!data || !data.connected) {
      body.innerHTML =
        '<p style="margin:0 0 0.65rem;font-size:0.7rem;color:var(--parchment-dim);line-height:1.45;">' +
          'Connect your Twitch channel so viewers can use chat commands, earn loot, and trigger live events.' +
        '</p>' +
        '<a class="twitch-connect-btn" href="/api/twitch/connect/' + encodeURIComponent(sessionId) + '"' +
           ' style="display:inline-block;padding:0.4rem 0.9rem;background:#9146ff;color:#fff;' +
                  'border-radius:6px;font-size:0.72rem;font-weight:600;text-decoration:none;' +
                  'transition:opacity 0.15s;" ' +
           ' onmouseover="this.style.opacity=\'0.85\'" onmouseout="this.style.opacity=\'1\'">' +
          'Connect Twitch' +
        '</a>';
      return;
    }

    var channel = String(data.channel || '');
    var enabled = !!data.enabled;

    body.innerHTML =
      '<div style="display:flex;align-items:center;gap:0.45rem;margin-bottom:0.55rem;">' +
        '<span style="font-size:0.72rem;color:#9146ff;font-weight:700;">&#10003; Connected</span>' +
        '<span style="font-size:0.7rem;color:var(--parchment);">#' + escHtml(channel) + '</span>' +
      '</div>' +
      '<label style="display:flex;align-items:center;gap:0.35rem;font-size:0.7rem;' +
             'color:var(--parchment-dim);cursor:pointer;margin-bottom:0.5rem;">' +
        '<input type="checkbox" id="twitch-enabled-toggle"' + (enabled ? ' checked' : '') +
               ' style="accent-color:#9146ff;" />' +
        ' Chat bridge enabled' +
      '</label>' +
      '<button id="twitch-disconnect-btn"' +
              ' style="padding:0.28rem 0.65rem;background:rgba(180,30,30,0.18);color:#e07070;' +
                     'border:1px solid rgba(180,30,30,0.4);border-radius:5px;font-size:0.68rem;cursor:pointer;">' +
        'Disconnect' +
      '</button>';

    document.getElementById('twitch-enabled-toggle').addEventListener('change', function () {
      var val = this.checked;
      fetch('/api/twitch/toggle/' + encodeURIComponent(sessionId), {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: val }),
      }).catch(function () {});
    });

    document.getElementById('twitch-disconnect-btn').addEventListener('click', function () {
      if (!confirm('Disconnect Twitch channel #' + channel + '?')) return;
      fetch('/api/twitch/disconnect/' + encodeURIComponent(sessionId), {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
      }).then(function () { refresh(); }).catch(function () { refresh(); });
    });

    // Overlays section
    var overlaySection = document.createElement('div');
    overlaySection.style.cssText = 'margin-top:0.75rem;border-top:1px solid rgba(255,255,255,0.1);padding-top:0.65rem;';
    overlaySection.innerHTML =
      '<p style="font-size:0.68rem;color:var(--parchment-dim);margin:0 0 0.45rem;font-weight:600;letter-spacing:0.03em;">OBS Overlays</p>' +
      '<div id="overlay-urls-wrap" style="font-size:0.66rem;color:var(--parchment-dim);">Loading…</div>';
    body.appendChild(overlaySection);

    fetch('/api/overlay/urls/' + encodeURIComponent(sessionId), { credentials: 'include' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        var wrap = document.getElementById('overlay-urls-wrap');
        if (!wrap || !d) return;
        wrap.innerHTML = _buildOverlayRow('Game Overlay', d.game_url, sessionId, 'game', '1920 × 1080 (full canvas, transparent)') +
                         _buildOverlayRow('Arena Panel',  d.arena_url, sessionId, 'arena', '520 × 300 (compact corner panel)');

        wrap.querySelectorAll('.ov-copy-btn').forEach(function (btn) {
          btn.addEventListener('click', function () {
            var url = btn.getAttribute('data-url');
            if (navigator.clipboard) {
              navigator.clipboard.writeText(url).then(function () {
                btn.textContent = 'Copied!';
                setTimeout(function () { btn.textContent = 'Copy'; }, 1500);
              });
            } else {
              var ta = document.createElement('textarea');
              ta.value = url; document.body.appendChild(ta); ta.select(); document.execCommand('copy');
              document.body.removeChild(ta);
              btn.textContent = 'Copied!';
              setTimeout(function () { btn.textContent = 'Copy'; }, 1500);
            }
          });
        });

        wrap.querySelectorAll('.ov-regen-btn').forEach(function (btn) {
          btn.addEventListener('click', function () {
            var type = btn.getAttribute('data-type');
            if (!confirm('Regenerate the ' + type + ' overlay URL? The old URL will stop working.')) return;
            btn.disabled = true;
            fetch('/api/overlay/regenerate/' + encodeURIComponent(sessionId) + '?type=' + type, {
              method: 'POST',
              credentials: 'include',
            })
              .then(function (r) { return r.ok ? r.json() : null; })
              .then(function (d) {
                if (!d || !d.url) { btn.disabled = false; return; }
                var row = btn.closest('.ov-row');
                if (row) {
                  row.setAttribute('data-url', d.url);
                  var copyBtn = row.querySelector('.ov-copy-btn');
                  if (copyBtn) copyBtn.setAttribute('data-url', d.url);
                  var input = row.querySelector('.ov-url-input');
                  if (input) input.value = d.url;
                }
                btn.disabled = false;
              })
              .catch(function () { btn.disabled = false; });
          });
        });
      })
      .catch(function () {
        var wrap = document.getElementById('overlay-urls-wrap');
        if (wrap) wrap.textContent = 'Could not load overlay URLs.';
      });

    // DM tools: persistence mode + chat character roster
    _buildDmToolsSection(body, data.persistence_mode);
  }

  function _buildOverlayRow(label, url, sessionId, type, dims) {
    var safeUrl  = escHtml(url  || '');
    var safeLabel = escHtml(label || '');
    var dimsHtml = dims
      ? '<div style="font-size:0.6rem;color:var(--parchment-dim);margin-top:0.15rem;">Recommended size: ' + escHtml(dims) + '</div>'
      : '';
    return (
      '<div class="ov-row" data-url="' + safeUrl + '" style="margin-bottom:0.5rem;">' +
        '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:0.2rem;">' +
          '<span style="color:var(--parchment);font-weight:600;">' + safeLabel + '</span>' +
          '<div style="display:flex;gap:0.3rem;">' +
            '<button class="ov-copy-btn" data-url="' + safeUrl + '"' +
                    ' style="padding:0.2rem 0.45rem;background:rgba(201,168,76,0.18);color:#c9a84c;' +
                           'border:1px solid rgba(201,168,76,0.4);border-radius:4px;font-size:0.64rem;cursor:pointer;">Copy</button>' +
            '<button class="ov-regen-btn" data-type="' + escHtml(type) + '"' +
                    ' style="padding:0.2rem 0.45rem;background:rgba(120,80,30,0.18);color:#c9a07c;' +
                           'border:1px solid rgba(120,80,30,0.4);border-radius:4px;font-size:0.64rem;cursor:pointer;">Regen</button>' +
          '</div>' +
        '</div>' +
        '<input class="ov-url-input" type="text" value="' + safeUrl + '" readonly' +
               ' style="width:100%;font-size:0.62rem;padding:0.2rem 0.4rem;background:rgba(0,0,0,0.25);' +
                      'color:var(--parchment-dim);border:1px solid rgba(255,255,255,0.12);' +
                      'border-radius:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;" />' +
        dimsHtml +
      '</div>'
    );
  }

  // ── DM tools: persistence mode + chat character roster ─────────────────────

  function _sendWS(msg) {
    if (typeof window.sendWS === 'function') { window.sendWS(msg); return true; }
    if (window.ws && window.ws.readyState === 1) {
      try { window.ws.send(JSON.stringify(msg)); return true; } catch (_) {}
    }
    return false;
  }

  // One-shot wait for a WS message type (settings panel only; the main game
  // dispatcher ignores these dm_* result types).
  function _awaitWS(type, timeoutMs) {
    return new Promise(function (resolve) {
      var sock = window.ws;
      if (!sock) { resolve(null); return; }
      var timer = setTimeout(function () {
        sock.removeEventListener('message', onMsg);
        resolve(null);
      }, timeoutMs || 4000);
      function onMsg(ev) {
        var msg;
        try { msg = JSON.parse(ev.data); } catch (_) { return; }
        if (msg && msg.type === type) {
          clearTimeout(timer);
          sock.removeEventListener('message', onMsg);
          resolve(msg.payload || {});
        }
      }
      sock.addEventListener('message', onMsg);
    });
  }

  function _buildDmToolsSection(body, persistenceMode) {
    var section = document.createElement('div');
    section.style.cssText = 'margin-top:0.75rem;border-top:1px solid rgba(255,255,255,0.1);padding-top:0.65rem;';
    section.innerHTML =
      '<p style="font-size:0.68rem;color:var(--parchment-dim);margin:0 0 0.45rem;font-weight:600;letter-spacing:0.03em;">Chat Characters</p>' +
      '<label style="display:flex;align-items:center;gap:0.4rem;font-size:0.66rem;color:var(--parchment-dim);margin-bottom:0.5rem;">' +
        'Persist between sessions:' +
        '<select id="twitch-persist-mode" style="font-size:0.66rem;background:rgba(0,0,0,0.3);color:var(--parchment);' +
                'border:1px solid rgba(255,255,255,0.15);border-radius:4px;padding:0.15rem 0.3rem;">' +
          '<option value="everything">Everything</option>' +
          '<option value="stats">Stats only</option>' +
          '<option value="nothing">Nothing</option>' +
        '</select>' +
      '</label>' +
      '<button id="twitch-roster-refresh" style="padding:0.24rem 0.6rem;background:rgba(201,168,76,0.14);color:#c9a84c;' +
              'border:1px solid rgba(201,168,76,0.35);border-radius:5px;font-size:0.66rem;cursor:pointer;">View chat characters</button>' +
      '<div id="twitch-roster-wrap" style="margin-top:0.45rem;font-size:0.64rem;color:var(--parchment-dim);"></div>';
    body.appendChild(section);

    var modeSel = section.querySelector('#twitch-persist-mode');
    modeSel.value = persistenceMode || 'everything';
    modeSel.addEventListener('change', function () {
      _sendWS({ type: 'dm_chat_persistence_mode', payload: { mode: modeSel.value } });
    });

    section.querySelector('#twitch-roster-refresh').addEventListener('click', _refreshRoster);
  }

  function _refreshRoster() {
    var wrap = document.getElementById('twitch-roster-wrap');
    if (!wrap) return;
    wrap.textContent = 'Loading…';
    var pending = _awaitWS('dm_chat_participants_result', 4000);
    if (!_sendWS({ type: 'dm_chat_participants_get', payload: {} })) {
      wrap.textContent = 'Not connected to the game session.';
      return;
    }
    pending.then(function (p) {
      if (!p) { wrap.textContent = 'No response — is the session live?'; return; }
      var list = p.participants || [];
      if (!list.length) { wrap.textContent = 'No chat participants yet.'; return; }
      wrap.innerHTML = '';
      list.forEach(function (cp) {
        var row = document.createElement('div');
        row.style.cssText = 'display:flex;align-items:center;gap:0.4rem;padding:0.22rem 0;' +
                            'border-bottom:1px solid rgba(255,255,255,0.06);';
        var stats = cp.lifetime_stats || {};
        var arena = cp.arena_stats || {};
        var name = cp.character_name ? (cp.character_name + ' (' + cp.twitch_username + ')') : cp.twitch_username;
        var meta = (cp.arena_class ? cp.arena_class + ' L' + cp.arena_level + ' · ' : '') +
                   (cp.inventory || []).length + ' items · ' +
                   (arena.wins || 0) + 'W/' + (arena.losses || 0) + 'L · ' +
                   (stats.damage_dealt || 0) + ' dmg';
        row.innerHTML =
          '<span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;color:' +
            (cp.is_active ? 'var(--parchment)' : 'var(--parchment-dim)') + ';">' +
            escHtml(name) + ' <span style="color:var(--parchment-dim);">' + escHtml(meta) + '</span></span>' +
          '<button class="cp-rename" style="padding:0.12rem 0.4rem;font-size:0.6rem;cursor:pointer;' +
                  'background:rgba(201,168,76,0.12);color:#c9a84c;border:1px solid rgba(201,168,76,0.3);border-radius:4px;">Rename</button>' +
          '<button class="cp-reset" style="padding:0.12rem 0.4rem;font-size:0.6rem;cursor:pointer;' +
                  'background:rgba(180,30,30,0.12);color:#e07070;border:1px solid rgba(180,30,30,0.3);border-radius:4px;">Reset</button>';
        row.querySelector('.cp-rename').addEventListener('click', function () {
          var newName = prompt('New character name for ' + cp.twitch_username + ':', cp.character_name || '');
          if (newName === null) return;
          _sendWS({ type: 'dm_chat_participant_update', payload: {
            twitch_username: cp.twitch_username, character_name: newName,
          } });
          setTimeout(_refreshRoster, 400);
        });
        row.querySelector('.cp-reset').addEventListener('click', function () {
          var scope = prompt(
            'Reset scope for ' + cp.twitch_username + ':\n' +
            'all = stats + inventory + arena + name\n' +
            'stats | inventory | arena | remove', 'all');
          if (!scope) return;
          scope = scope.trim().toLowerCase();
          if (['all', 'stats', 'inventory', 'arena', 'remove'].indexOf(scope) === -1) return;
          if (!confirm('Reset "' + scope + '" for ' + cp.twitch_username + '?')) return;
          _sendWS({ type: 'dm_chat_participant_reset', payload: {
            twitch_username: cp.twitch_username, scope: scope,
          } });
          setTimeout(_refreshRoster, 400);
        });
        wrap.appendChild(row);
      });
    });
  }

  document.addEventListener('DOMContentLoaded', function () {
    // Re-check each time the settings flyout is opened.
    document.addEventListener('flyout-opened', function (e) {
      if (e && e.detail && e.detail.id === 'flyout-theme') init();
    });
    init();

    // Show a brief toast if the DM just completed OAuth.
    if (new URLSearchParams(location.search).get('twitch_connected') === '1') {
      history.replaceState(
        null, '',
        (location.pathname + location.search)
          .replace(/([?&])twitch_connected=1(&|$)/, function (_, p, s) { return s === '&' ? p : ''; })
          .replace(/[?&]$/, '')
      );
      // Open the settings flyout so the DM sees the connected state.
      if (typeof toggleFlyout === 'function') toggleFlyout('flyout-theme');
    }
  });
})();
