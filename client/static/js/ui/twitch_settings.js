/**
 * client/static/js/ui/twitch_settings.js
 *
 * DM-only Twitch Integration panel. Fetches /api/twitch/status to show
 * connection state (including whether the chat bridge service is connected)
 * and exposes connect / disconnect / enable-toggle controls plus OBS overlay
 * URLs.
 *
 * The panel renders into any container element, so the same logic drives
 * both the settings-flyout instance (#twitch-settings-body) and the Stream
 * mode view in the DM Modes rail (see dm_context_render.js). Reuse via:
 *
 *   window.TwitchSettingsPanel.renderInto(containerEl)
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

  // ── Collapsible rows ─────────────────────────────────────────────────────
  // Explicit expand/collapse instead of native <details>: the DM rail's shell
  // interfered with native summary toggling, so rows showed an arrow that did
  // nothing. The whole header row is clickable (and keyboard-operable).

  // The whole panel is re-rendered by the DM rail on context refreshes, so
  // remember which rows are open (keyed by data-collapse-key) and restore
  // their state on rebuild.
  var _openCollapseKeys = {};

  function _setCollapseOpen(header, open) {
    var body = header.nextElementSibling;
    if (!body || !body.classList.contains('tw-collapse-body')) return;
    body.style.display = open ? '' : 'none';
    header.setAttribute('aria-expanded', String(open));
    var arrow = header.querySelector('.tw-collapse-arrow');
    if (arrow) arrow.style.transform = open ? 'rotate(90deg)' : '';
    var key = header.dataset.collapseKey;
    if (key) {
      if (open) _openCollapseKeys[key] = true;
      else delete _openCollapseKeys[key];
    }
  }

  function _toggleCollapse(header) {
    var body = header.nextElementSibling;
    if (!body || !body.classList.contains('tw-collapse-body')) return;
    _setCollapseOpen(header, body.style.display === 'none');
  }

  /**
   * Bind toggle handlers to every .tw-collapse-header under root (idempotent)
   * and restore each keyed row's remembered open state.
   */
  function _wireCollapsibles(root) {
    if (!root || !root.querySelectorAll) return;
    root.querySelectorAll('.tw-collapse-header').forEach(function (header) {
      if (header.dataset.collapseBound !== '1') {
        header.dataset.collapseBound = '1';
        header.addEventListener('click', function (e) {
          e.preventDefault();
          _toggleCollapse(header);
        });
        header.addEventListener('keydown', function (e) {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            _toggleCollapse(header);
          }
        });
      }
      var key = header.dataset.collapseKey;
      if (key && _openCollapseKeys[key]) _setCollapseOpen(header, true);
    });
  }

  var _COLLAPSE_HEADER_CSS =
    'display:flex;align-items:center;gap:0.35rem;cursor:pointer;user-select:none;padding:0.15rem 0;';
  var _COLLAPSE_ARROW_HTML =
    '<span class="tw-collapse-arrow" style="font-size:0.5rem;color:var(--parchment-dim);' +
    'transition:transform 0.12s;flex:none;">&#9654;</span>';

  /**
   * Build a collapsed row: { root, header, content }. labelHtml must already
   * be escaped by the caller; key identifies the row so its open state
   * survives panel re-renders.
   */
  function _makeCollapsible(labelHtml, key) {
    var root = document.createElement('div');
    root.className = 'tw-collapse';
    root.style.cssText = 'margin-bottom:0.35rem;';
    var header = document.createElement('div');
    header.className = 'tw-collapse-header';
    header.setAttribute('role', 'button');
    header.setAttribute('tabindex', '0');
    header.setAttribute('aria-expanded', 'false');
    if (key) header.dataset.collapseKey = key;
    header.style.cssText = _COLLAPSE_HEADER_CSS + 'font-size:0.66rem;color:var(--parchment);';
    header.innerHTML = _COLLAPSE_ARROW_HTML + '<span>' + labelHtml + '</span>';
    var content = document.createElement('div');
    content.className = 'tw-collapse-body';
    content.style.display = 'none';
    root.appendChild(header);
    root.appendChild(content);
    _wireCollapsibles(root);
    return { root: root, header: header, content: content };
  }

  function init() {
    var section = document.getElementById('twitch-settings-section');
    if (!section) return;
    if (window.ROLE !== 'dm') return;
    section.removeAttribute('hidden');
    refresh();
  }

  /** Refresh the settings-flyout instance. */
  function refresh() {
    var body = document.getElementById('twitch-settings-body');
    if (body) renderInto(body);
  }

  /** Fetch status and render the full panel into an arbitrary container. */
  async function renderInto(container) {
    if (!container) return;
    var sessionId = getSessionId();
    if (!sessionId) return;

    container.innerHTML = '<span style="font-size:0.7rem;color:var(--parchment-dim);">Loading…</span>';

    try {
      var res = await fetch('/api/twitch/status/' + encodeURIComponent(sessionId), {
        credentials: 'include',
      });
      if (!res.ok) { renderPanel(null, container); return; }
      var data = await res.json();
      renderPanel(data, container);
    } catch (_) {
      renderPanel(null, container);
    }
  }

  // ── Bridge status row (supervised chat-bridge process ⇄ this session) ──────

  function _bridgeStatusHtml(data) {
    var bridge = (data && data.bridge) || null;
    // Fall back to the legacy connected boolean when the supervisor block is
    // missing (e.g. older server still running behind a cached client).
    var state = bridge ? String(bridge.state || 'stopped')
                       : (data && data.bridge_connected ? 'running' : 'stopped');
    var managed = !!(bridge && bridge.managed);
    var known = !!(bridge || (data && typeof data.bridge_connected === 'boolean'));

    var dotColor, label, labelColor, hint, glow = '';
    if (state === 'running') {
      dotColor = '#3fca6b';
      labelColor = '#3fca6b';
      glow = 'box-shadow:0 0 6px rgba(63,202,107,0.7);';
      label = managed ? 'Bridge running' : 'Bridge running (manual)';
      hint = managed ? 'Chat commands like !join are live.'
                     : 'Started externally (npm start); chat commands are live.';
    } else if (state === 'starting') {
      dotColor = '#e0b040';
      labelColor = '#e0b040';
      glow = 'box-shadow:0 0 6px rgba(224,176,64,0.6);';
      label = 'Bridge starting…';
      hint = (bridge && bridge.retries > 0)
        ? ('Restarting (attempt ' + bridge.retries + '/' + (bridge.max_retries || 5) + ').')
        : 'Launching the chat-bridge process…';
    } else if (state === 'failed') {
      dotColor = '#e05050';
      labelColor = '#e05050';
      label = 'Bridge failed';
      hint = 'Use Restart to try again.';
    } else {
      dotColor = 'rgba(255,255,255,0.28)';
      labelColor = 'var(--parchment-dim)';
      label = 'Bridge stopped';
      hint = (data && data.connected)
        ? 'Enable the chat bridge to start it automatically.'
        : 'Connect Twitch to start the chat bridge automatically.';
    }
    if (!known) { label = 'Bridge status unknown'; hint = ''; }

    var errorHtml = '';
    if (state === 'failed' && bridge && bridge.last_error) {
      errorHtml =
        '<div style="margin:-0.35rem 0 0.55rem;padding:0.3rem 0.45rem;background:rgba(180,30,30,0.12);' +
               'border:1px solid rgba(180,30,30,0.35);border-radius:4px;font-size:0.62rem;color:#e07070;' +
               'font-family:monospace;word-break:break-word;">' +
          escHtml(bridge.last_error) +
        '</div>';
    }

    var logHtml = '';
    if (bridge && bridge.log && bridge.log.length) {
      logHtml =
        '<div class="tw-collapse" style="margin:-0.25rem 0 0.55rem;">' +
          '<div class="tw-collapse-header" role="button" tabindex="0" aria-expanded="false"' +
               ' data-collapse-key="bridge-log"' +
               ' style="' + _COLLAPSE_HEADER_CSS + 'font-size:0.6rem;color:var(--parchment-dim);">' +
            _COLLAPSE_ARROW_HTML +
            '<span>Recent bridge log</span>' +
          '</div>' +
          '<div class="tw-collapse-body" style="display:none;">' +
            '<pre style="margin:0.25rem 0 0;max-height:130px;overflow:auto;padding:0.3rem 0.45rem;' +
                       'background:rgba(0,0,0,0.3);border:1px solid rgba(255,255,255,0.1);border-radius:4px;' +
                       'font-size:0.58rem;line-height:1.4;color:var(--parchment-dim);white-space:pre-wrap;word-break:break-word;">' +
              escHtml(bridge.log.join('\n')) +
            '</pre>' +
          '</div>' +
        '</div>';
    }

    var showRestart = !!(data && data.connected && bridge);
    var restartBtn = showRestart
      ? '<button type="button" class="twitch-bridge-restart"' +
               ' style="padding:0.12rem 0.45rem;background:rgba(201,168,76,0.14);color:#c9a84c;' +
                      'border:1px solid rgba(201,168,76,0.35);border-radius:4px;font-size:0.6rem;cursor:pointer;">Restart bridge</button>'
      : '';

    return (
      '<div class="twitch-bridge-status" style="display:flex;align-items:center;gap:0.4rem;margin-bottom:0.6rem;flex-wrap:wrap;">' +
        '<span style="width:9px;height:9px;border-radius:50%;background:' + dotColor + ';flex-shrink:0;' + glow + '"></span>' +
        '<span style="font-size:0.7rem;font-weight:600;color:' + labelColor + ';">' + label + '</span>' +
        (hint ? '<span style="font-size:0.62rem;color:var(--parchment-dim);">' + hint + '</span>' : '') +
        '<span style="margin-left:auto;display:flex;gap:0.3rem;">' +
          restartBtn +
          '<button type="button" class="twitch-bridge-refresh" title="Re-check bridge status"' +
                  ' style="padding:0.12rem 0.4rem;background:rgba(255,255,255,0.06);color:var(--parchment-dim);' +
                         'border:1px solid rgba(255,255,255,0.15);border-radius:4px;font-size:0.6rem;cursor:pointer;">&#8635;</button>' +
        '</span>' +
      '</div>' +
      errorHtml +
      logHtml
    );
  }

  function _wireBridgeRefresh(container) {
    _wireCollapsibles(container);
    var btn = container.querySelector('.twitch-bridge-refresh');
    if (btn) btn.addEventListener('click', function () { renderInto(container); });
    var restart = container.querySelector('.twitch-bridge-restart');
    if (restart) {
      restart.addEventListener('click', function () {
        restart.disabled = true;
        restart.textContent = 'Restarting…';
        fetch('/api/chat-bridge/restart/' + encodeURIComponent(getSessionId()), {
          method: 'POST',
          credentials: 'include',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({}),
        })
          .then(function () { setTimeout(function () { renderInto(container); }, 1200); })
          .catch(function () { setTimeout(function () { renderInto(container); }, 1200); });
      });
    }
  }

  function renderPanel(data, container) {
    var body = container;
    if (!body) return;

    var sessionId = getSessionId();

    if (!data || !data.connected) {
      body.innerHTML =
        _bridgeStatusHtml(data) +
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
      _wireBridgeRefresh(body);
      return;
    }

    var channel = String(data.channel || '');
    var enabled = !!data.enabled;

    body.innerHTML =
      _bridgeStatusHtml(data) +
      '<div style="display:flex;align-items:center;gap:0.45rem;margin-bottom:0.55rem;">' +
        '<span style="font-size:0.72rem;color:#9146ff;font-weight:700;">&#10003; Connected</span>' +
        '<span style="font-size:0.7rem;color:var(--parchment);">#' + escHtml(channel) + '</span>' +
      '</div>' +
      '<label style="display:flex;align-items:center;gap:0.35rem;font-size:0.7rem;' +
             'color:var(--parchment-dim);cursor:pointer;margin-bottom:0.5rem;">' +
        '<input type="checkbox" class="twitch-enabled-toggle"' + (enabled ? ' checked' : '') +
               ' style="accent-color:#9146ff;" />' +
        ' Chat bridge enabled' +
      '</label>' +
      '<button type="button" class="twitch-disconnect-btn"' +
              ' style="padding:0.28rem 0.65rem;background:rgba(180,30,30,0.18);color:#e07070;' +
                     'border:1px solid rgba(180,30,30,0.4);border-radius:5px;font-size:0.68rem;cursor:pointer;">' +
        'Disconnect' +
      '</button>';

    _wireBridgeRefresh(body);

    body.querySelector('.twitch-enabled-toggle').addEventListener('change', function () {
      var val = this.checked;
      fetch('/api/twitch/toggle/' + encodeURIComponent(sessionId), {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: val }),
      })
        // The toggle now starts/stops the supervised bridge process —
        // re-render shortly so the status row reflects starting/stopped.
        .then(function () { setTimeout(function () { renderInto(body); }, 1200); })
        .catch(function () {});
    });

    body.querySelector('.twitch-disconnect-btn').addEventListener('click', function () {
      if (!confirm('Disconnect Twitch channel #' + channel + '?')) return;
      fetch('/api/twitch/disconnect/' + encodeURIComponent(sessionId), {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
      }).then(function () { renderInto(body); }).catch(function () { renderInto(body); });
    });

    // Overlays section
    var overlaySection = document.createElement('div');
    overlaySection.style.cssText = 'margin-top:0.75rem;border-top:1px solid rgba(255,255,255,0.1);padding-top:0.65rem;';
    overlaySection.innerHTML =
      '<p style="font-size:0.68rem;color:var(--parchment-dim);margin:0 0 0.45rem;font-weight:600;letter-spacing:0.03em;">OBS Overlays</p>' +
      '<div class="overlay-urls-wrap" style="font-size:0.66rem;color:var(--parchment-dim);">Loading…</div>';
    body.appendChild(overlaySection);
    var overlayWrap = overlaySection.querySelector('.overlay-urls-wrap');

    fetch('/api/overlay/urls/' + encodeURIComponent(sessionId), { credentials: 'include' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        var wrap = overlayWrap;
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
        if (overlayWrap) overlayWrap.textContent = 'Could not load overlay URLs.';
      });

    // Rewards: map Twitch events to viewer-power loot tables
    _buildRewardsSection(body);

    // Chat Party: live roster of joined chatters + quick actions
    _buildChatPartySection(body);

    // DM tools: persistence mode + friendly fire + chat character roster
    _buildDmToolsSection(body, data.persistence_mode, data.friendly_fire);
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

  // ── Rewards: Twitch events → viewer-power loot tables ──────────────────────

  var _availablePowers = [];   // [{power_id, name, description}] — also feeds the Chat Party grant dropdown

  function _sectionEl(title) {
    var section = document.createElement('div');
    section.style.cssText = 'margin-top:0.75rem;border-top:1px solid rgba(255,255,255,0.1);padding-top:0.65rem;';
    section.innerHTML =
      '<p style="font-size:0.68rem;color:var(--parchment-dim);margin:0 0 0.45rem;font-weight:600;letter-spacing:0.03em;">' +
        escHtml(title) + '</p>';
    return section;
  }

  function _buildRewardsSection(body) {
    var section = _sectionEl('Rewards — subs, gifts & bits');
    var wrap = document.createElement('div');
    wrap.className = 'twitch-rewards-wrap';
    wrap.style.cssText = 'font-size:0.64rem;color:var(--parchment-dim);';
    wrap.textContent = 'Loading reward tables…';
    section.appendChild(wrap);
    body.appendChild(section);

    var pending = _awaitWS('dm_chat_rewards_result', 4000);
    if (!_sendWS({ type: 'dm_chat_rewards_get', payload: {} })) {
      wrap.textContent = 'Not connected to the game session.';
      return;
    }
    pending.then(function (p) {
      if (!p || !p.config) { wrap.textContent = 'No response — is the session live?'; return; }
      _availablePowers = p.available_powers || [];
      _renderRewardsEditor(wrap, p.config);
    });
  }

  function _renderRewardsEditor(wrap, config) {
    wrap.innerHTML =
      '<p style="margin:0 0 0.4rem;line-height:1.4;">Tick the powers each Twitch event can award. ' +
      'Chatters receive a random power from the table as a usable item (!use / !target).</p>';

    // groups: [{label, key, tierIndex|null, table}]
    var groups = [{ label: 'Single sub / resub', key: 'sub', tierIndex: null, table: config.sub || [] }];
    (config.gift_tiers || []).forEach(function (tier, i) {
      groups.push({ label: 'Gift subs ×' + tier.min_count + '+', key: 'gift_tiers', tierIndex: i, table: tier.table || [] });
    });
    (config.bits_tiers || []).forEach(function (tier, i) {
      groups.push({ label: 'Bits ' + tier.threshold + '+', key: 'bits_tiers', tierIndex: i, table: tier.table || [] });
    });

    groups.forEach(function (group) {
      var inTable = {};
      group.table.forEach(function (e) { inTable[e.power_id] = e.weight || 1; });
      var count = group.table.length;

      var row = _makeCollapsible(
        escHtml(group.label) +
        ' <span class="rw-count" style="color:var(--parchment-dim);">(' + count + ' power' + (count === 1 ? '' : 's') + ')</span>',
        'rewards:' + group.label
      );

      var grid = document.createElement('div');
      grid.style.cssText = 'display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:0.1rem 0.5rem;padding:0.3rem 0 0.2rem 0.95rem;';
      _availablePowers.forEach(function (pw) {
        var label = document.createElement('label');
        label.style.cssText = 'display:flex;align-items:center;gap:0.28rem;font-size:0.62rem;color:var(--parchment-dim);cursor:pointer;';
        label.title = pw.description || '';
        var cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.style.cssText = 'accent-color:#9146ff;';
        cb.checked = !!inTable[pw.power_id];
        cb.setAttribute('data-power-id', pw.power_id);
        cb.setAttribute('data-weight', String(inTable[pw.power_id] || 1));
        cb.setAttribute('data-group-key', group.key);
        cb.setAttribute('data-tier-index', group.tierIndex === null ? '' : String(group.tierIndex));
        cb.addEventListener('change', function () {
          var checked = grid.querySelectorAll('input:checked').length;
          var countEl = row.header.querySelector('.rw-count');
          if (countEl) countEl.textContent = '(' + checked + ' power' + (checked === 1 ? '' : 's') + ')';
        });
        label.appendChild(cb);
        label.appendChild(document.createTextNode(pw.name));
        grid.appendChild(label);
      });
      row.content.appendChild(grid);
      wrap.appendChild(row.root);
    });

    var btnRow = document.createElement('div');
    btnRow.style.cssText = 'display:flex;gap:0.4rem;margin-top:0.45rem;align-items:center;';
    btnRow.innerHTML =
      '<button type="button" class="rw-save" style="padding:0.26rem 0.7rem;background:rgba(145,70,255,0.18);color:#b98aff;' +
              'border:1px solid rgba(145,70,255,0.45);border-radius:5px;font-size:0.66rem;cursor:pointer;">Save rewards</button>' +
      '<button type="button" class="rw-reset" style="padding:0.26rem 0.6rem;background:rgba(255,255,255,0.06);color:var(--parchment-dim);' +
              'border:1px solid rgba(255,255,255,0.15);border-radius:5px;font-size:0.64rem;cursor:pointer;">Reset to defaults</button>' +
      '<span class="rw-status" style="font-size:0.62rem;color:var(--parchment-dim);"></span>';
    wrap.appendChild(btnRow);

    function collectConfig() {
      var out = { version: 1, sub: [], gift_tiers: [], bits_tiers: [] };
      (config.gift_tiers || []).forEach(function (tier) {
        out.gift_tiers.push({ min_count: tier.min_count, table: [] });
      });
      (config.bits_tiers || []).forEach(function (tier) {
        out.bits_tiers.push({ threshold: tier.threshold, table: [] });
      });
      wrap.querySelectorAll('input[data-power-id]').forEach(function (cb) {
        if (!cb.checked) return;
        var entry = { power_id: cb.getAttribute('data-power-id'), weight: parseInt(cb.getAttribute('data-weight'), 10) || 1 };
        var key = cb.getAttribute('data-group-key');
        var tierIdx = cb.getAttribute('data-tier-index');
        if (key === 'sub') out.sub.push(entry);
        else if (out[key] && out[key][parseInt(tierIdx, 10)]) out[key][parseInt(tierIdx, 10)].table.push(entry);
      });
      return out;
    }

    function saveRewards(payload) {
      var status = btnRow.querySelector('.rw-status');
      status.textContent = 'Saving…';
      var pending = _awaitWS('dm_chat_rewards_result', 4000);
      if (!_sendWS({ type: 'dm_chat_rewards_update', payload: payload })) {
        status.textContent = 'Not connected.';
        return;
      }
      pending.then(function (p) {
        if (p && p.config) {
          _availablePowers = p.available_powers || _availablePowers;
          status.textContent = 'Saved ✓ (bridge updated live)';
          setTimeout(function () { status.textContent = ''; }, 3000);
          _renderRewardsEditor(wrap, p.config);
        } else {
          status.textContent = 'Save failed — no response.';
        }
      });
    }

    btnRow.querySelector('.rw-save').addEventListener('click', function () {
      saveRewards({ config: collectConfig() });
    });
    btnRow.querySelector('.rw-reset').addEventListener('click', function () {
      if (!confirm('Reset all reward tables to the shipped defaults?')) return;
      saveRewards({ reset: true });
    });
  }

  // ── Chat Party: live roster of joined chatters ──────────────────────────────

  function _relTime(epochSec) {
    var sec = Math.max(0, Math.floor(Date.now() / 1000 - Number(epochSec || 0)));
    if (!epochSec) return '';
    if (sec < 60) return 'just now';
    if (sec < 3600) return Math.floor(sec / 60) + 'm ago';
    if (sec < 86400) return Math.floor(sec / 3600) + 'h ago';
    return Math.floor(sec / 86400) + 'd ago';
  }

  function _buildChatPartySection(body) {
    var section = _sectionEl('Chat Party — joined chatters');
    var wrap = document.createElement('div');
    wrap.className = 'twitch-chat-party-wrap';
    wrap.style.cssText = 'font-size:0.64rem;color:var(--parchment-dim);';
    wrap.textContent = 'Loading…';
    section.appendChild(wrap);
    body.appendChild(section);
    _refreshChatParty(wrap);
  }

  function _refreshChatParty(wrap) {
    if (!wrap || !wrap.isConnected) return;
    var pending = _awaitWS('dm_chat_participants_result', 4000);
    if (!_sendWS({ type: 'dm_chat_participants_get', payload: {} })) {
      wrap.textContent = 'Not connected to the game session.';
      return;
    }
    pending.then(function (p) {
      if (!wrap.isConnected) return;
      if (!p) { wrap.textContent = 'No response — is the session live?'; return; }
      var list = (p.participants || []).filter(function (cp) { return cp.is_active; });
      if (!list.length) {
        wrap.textContent = 'No one has !joined yet. Viewers type !join in Twitch chat to enter.';
        return;
      }
      wrap.innerHTML = '';
      list.forEach(function (cp) { wrap.appendChild(_chatPartyRow(cp, wrap)); });
    });
  }

  function _chatPartyRow(cp, wrap) {
    var row = document.createElement('div');
    row.style.cssText = 'padding:0.3rem 0;border-bottom:1px solid rgba(255,255,255,0.06);';

    var name = cp.character_name ? (cp.character_name + ' (' + cp.twitch_username + ')') : (cp.display_name || cp.twitch_username);
    var cls = cp.arena_class ? (cp.arena_class + ' L' + (cp.arena_level || 1)) : 'No class yet';
    var items = (cp.inventory || []).map(function (i) {
      var qty = (i.charges_max > 0) ? (i.charges_current + '/' + i.charges_max) : ('x' + (i.qty || 1));
      return (i.name || 'item') + ' ' + qty;
    });
    var itemsLabel = items.length
      ? (items.slice(0, 3).join(', ') + (items.length > 3 ? ' +' + (items.length - 3) + ' more' : ''))
      : 'no usable items';

    var head = document.createElement('div');
    head.style.cssText = 'display:flex;align-items:center;gap:0.4rem;flex-wrap:wrap;';
    head.innerHTML =
      '<span style="font-weight:600;color:var(--parchment);">' + escHtml(name) + '</span>' +
      '<span>' + escHtml(cls) + '</span>' +
      '<span style="margin-left:auto;color:var(--parchment-dim);">joined ' + escHtml(_relTime(cp.joined_at)) + '</span>';
    row.appendChild(head);

    var sub = document.createElement('div');
    sub.style.cssText = 'display:flex;align-items:center;gap:0.35rem;margin-top:0.18rem;flex-wrap:wrap;';
    sub.innerHTML =
      '<span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;" title="' + escHtml(items.join(', ')) + '">🎒 ' +
        escHtml(itemsLabel) + '</span>' +
      '<button class="cp-grant" style="padding:0.12rem 0.42rem;font-size:0.6rem;cursor:pointer;background:rgba(145,70,255,0.14);' +
              'color:#b98aff;border:1px solid rgba(145,70,255,0.38);border-radius:4px;">Grant</button>' +
      '<button class="cp-rename" style="padding:0.12rem 0.42rem;font-size:0.6rem;cursor:pointer;background:rgba(201,168,76,0.12);' +
              'color:#c9a84c;border:1px solid rgba(201,168,76,0.3);border-radius:4px;">Rename</button>' +
      '<button class="cp-kick" style="padding:0.12rem 0.42rem;font-size:0.6rem;cursor:pointer;background:rgba(255,255,255,0.06);' +
              'color:var(--parchment-dim);border:1px solid rgba(255,255,255,0.18);border-radius:4px;">Kick</button>' +
      '<button class="cp-reset" style="padding:0.12rem 0.42rem;font-size:0.6rem;cursor:pointer;background:rgba(180,30,30,0.12);' +
              'color:#e07070;border:1px solid rgba(180,30,30,0.3);border-radius:4px;">Reset</button>';
    row.appendChild(sub);

    // Grant flyout: pick a viewer power to hand out as a usable item
    // (same power list the Rewards tables use).
    sub.querySelector('.cp-grant').addEventListener('click', function () {
      var existing = row.querySelector('.cp-grant-flyout');
      if (existing) { existing.remove(); return; }
      var fly = document.createElement('div');
      fly.className = 'cp-grant-flyout';
      fly.style.cssText = 'display:flex;gap:0.3rem;align-items:center;margin-top:0.25rem;flex-wrap:wrap;';
      var sel = document.createElement('select');
      sel.style.cssText = 'font-size:0.62rem;background:rgba(0,0,0,0.3);color:var(--parchment);' +
                          'border:1px solid rgba(255,255,255,0.15);border-radius:4px;padding:0.12rem 0.25rem;max-width:150px;';
      if (_availablePowers.length) {
        _availablePowers.forEach(function (pw) {
          var opt = document.createElement('option');
          opt.value = pw.power_id;
          opt.textContent = pw.name;
          opt.title = pw.description || '';
          sel.appendChild(opt);
        });
      } else {
        var opt = document.createElement('option');
        opt.value = '';
        opt.textContent = 'Powers unavailable — use custom';
        sel.appendChild(opt);
      }
      var goBtn = document.createElement('button');
      goBtn.textContent = 'Give power';
      goBtn.style.cssText = 'padding:0.12rem 0.45rem;font-size:0.6rem;cursor:pointer;background:rgba(145,70,255,0.18);' +
                            'color:#b98aff;border:1px solid rgba(145,70,255,0.45);border-radius:4px;';
      var customBtn = document.createElement('button');
      customBtn.textContent = 'Custom item…';
      customBtn.style.cssText = 'padding:0.12rem 0.45rem;font-size:0.6rem;cursor:pointer;background:rgba(255,255,255,0.06);' +
                                'color:var(--parchment-dim);border:1px solid rgba(255,255,255,0.15);border-radius:4px;';
      fly.appendChild(sel); fly.appendChild(goBtn); fly.appendChild(customBtn);
      row.appendChild(fly);

      goBtn.addEventListener('click', function () {
        var pid = sel.value;
        if (!pid) return;
        var pw = null;
        _availablePowers.forEach(function (e) { if (e.power_id === pid) pw = e; });
        _sendWS({ type: 'dm_grant_chat_participant_item', payload: {
          twitch_username: cp.twitch_username,
          item_entry: { name: (pw && pw.name) || pid, qty: 1, power_id: pid },
        } });
        fly.remove();
        setTimeout(function () { _refreshChatParty(wrap); }, 400);
      });
      customBtn.addEventListener('click', function () {
        var itemName = prompt('Item name to grant to ' + cp.twitch_username + ':');
        if (!itemName) return;
        _sendWS({ type: 'dm_grant_chat_participant_item', payload: {
          twitch_username: cp.twitch_username,
          item_entry: { name: itemName, qty: 1 },
        } });
        fly.remove();
        setTimeout(function () { _refreshChatParty(wrap); }, 400);
      });
    });

    sub.querySelector('.cp-rename').addEventListener('click', function () {
      var newName = prompt('New character name for ' + cp.twitch_username + ':', cp.character_name || '');
      if (newName === null) return;
      _sendWS({ type: 'dm_chat_participant_update', payload: {
        twitch_username: cp.twitch_username, character_name: newName,
      } });
      setTimeout(function () { _refreshChatParty(wrap); }, 400);
    });

    sub.querySelector('.cp-kick').addEventListener('click', function () {
      if (!confirm('Kick ' + cp.twitch_username + ' from the chat party? They keep their character and can !join again.')) return;
      _sendWS({ type: 'dm_chat_participant_reset', payload: {
        twitch_username: cp.twitch_username, scope: 'kick',
      } });
      setTimeout(function () { _refreshChatParty(wrap); }, 400);
    });

    sub.querySelector('.cp-reset').addEventListener('click', function () {
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
      setTimeout(function () { _refreshChatParty(wrap); }, 400);
    });

    return row;
  }

  // Live refresh: called from the WS dispatcher whenever a chat_participant_*
  // event arrives (join/leave/loot/grant/update). Debounced; refreshes every
  // mounted Chat Party list (settings flyout + Stream panel).
  var _chatPartyRefreshTimer = null;
  function notifyChatPartyEvent(type, payload) {
    if (window.ROLE !== 'dm') return;
    if (_chatPartyRefreshTimer) clearTimeout(_chatPartyRefreshTimer);
    _chatPartyRefreshTimer = setTimeout(function () {
      _chatPartyRefreshTimer = null;
      document.querySelectorAll('.twitch-chat-party-wrap').forEach(function (wrap) {
        _refreshChatParty(wrap);
      });
    }, 350);
  }

  function _buildDmToolsSection(body, persistenceMode, friendlyFire) {
    var section = document.createElement('div');
    section.style.cssText = 'margin-top:0.75rem;border-top:1px solid rgba(255,255,255,0.1);padding-top:0.65rem;';
    section.innerHTML =
      '<p style="font-size:0.68rem;color:var(--parchment-dim);margin:0 0 0.45rem;font-weight:600;letter-spacing:0.03em;">Chat Characters</p>' +
      '<label style="display:flex;align-items:center;gap:0.4rem;font-size:0.66rem;color:var(--parchment-dim);margin-bottom:0.5rem;">' +
        'Persist between sessions:' +
        '<select class="twitch-persist-mode" style="font-size:0.66rem;background:rgba(0,0,0,0.3);color:var(--parchment);' +
                'border:1px solid rgba(255,255,255,0.15);border-radius:4px;padding:0.15rem 0.3rem;">' +
          '<option value="everything">Everything</option>' +
          '<option value="stats">Stats only</option>' +
          '<option value="nothing">Nothing</option>' +
        '</select>' +
      '</label>' +
      '<label style="display:flex;align-items:center;gap:0.4rem;font-size:0.66rem;color:var(--parchment-dim);margin-bottom:0.5rem;cursor:pointer;" ' +
             'title="When off (default), chat items cannot damage party tokens. Heals and blessings always work.">' +
        '<input type="checkbox" class="twitch-friendly-fire" style="accent-color:#c9a84c;" />' +
        'Friendly fire — chat items can damage the party' +
      '</label>' +
      '<button type="button" class="twitch-roster-refresh" style="padding:0.24rem 0.6rem;background:rgba(201,168,76,0.14);color:#c9a84c;' +
              'border:1px solid rgba(201,168,76,0.35);border-radius:5px;font-size:0.66rem;cursor:pointer;">View chat characters</button>' +
      '<div class="twitch-roster-wrap" style="margin-top:0.45rem;font-size:0.64rem;color:var(--parchment-dim);"></div>';
    body.appendChild(section);

    var modeSel = section.querySelector('.twitch-persist-mode');
    modeSel.value = persistenceMode || 'everything';
    modeSel.addEventListener('change', function () {
      _sendWS({ type: 'dm_chat_persistence_mode', payload: { mode: modeSel.value } });
    });

    var ffToggle = section.querySelector('.twitch-friendly-fire');
    ffToggle.checked = !!friendlyFire;
    ffToggle.addEventListener('change', function () {
      _sendWS({ type: 'dm_chat_friendly_fire', payload: { enabled: ffToggle.checked } });
    });

    var rosterWrap = section.querySelector('.twitch-roster-wrap');
    section.querySelector('.twitch-roster-refresh').addEventListener('click', function () {
      _refreshRoster(rosterWrap);
    });
  }

  function _refreshRoster(wrap) {
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
          setTimeout(function () { _refreshRoster(wrap); }, 400);
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
          setTimeout(function () { _refreshRoster(wrap); }, 400);
        });
        wrap.appendChild(row);
      });
    });
  }

  // Public API — reused by the Stream mode panel (dm_context_render.js) and
  // the WS dispatcher (message_dispatch.js live chat-party updates).
  window.TwitchSettingsPanel = Object.freeze({
    renderInto: renderInto,
    refresh: refresh,
    notifyChatPartyEvent: notifyChatPartyEvent,
  });

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
