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
        wrap.innerHTML = _buildOverlayRow('Game Overlay', d.game_url, sessionId, 'game') +
                         _buildOverlayRow('Arena Panel',  d.arena_url, sessionId, 'arena');

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
  }

  function _buildOverlayRow(label, url, sessionId, type) {
    var safeUrl  = escHtml(url  || '');
    var safeLabel = escHtml(label || '');
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
      '</div>'
    );
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
