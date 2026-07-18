/* extension/config.js — broadcaster (DM) configuration page.
 *
 * Binds the Extension to a live game session: the DM enters the current game
 * session_id and (optionally) which powers are purchasable. We persist to the
 * Twitch broadcaster configuration AND POST to the EBS so it knows
 * channelId -> session_id for routing transactions.
 */

// Set this to your EBS (this app's) HTTPS domain — same value as viewer.js.
const EBS_BASE = 'https://YOUR-EBS-DOMAIN';

const twitch = window.Twitch ? window.Twitch.ext : null;

// The toggle list is fetched from the EBS (GET /api/twitch/ext/powers), which
// derives it from the server's power defs + SKU map — the single source of
// truth. FALLBACK_POWERS only renders when the EBS is unreachable, so the
// page stays usable offline; it does not need to be exhaustive.
const FALLBACK_POWERS = [
  { power_id: 'pebble_toss', name: 'Pebble Toss' },
  { power_id: 'arcane_zap', name: 'Arcane Zap' },
  { power_id: 'healing_spark', name: 'Healing Spark' },
  { power_id: 'battle_blessing', name: 'Battle Blessing' },
  { power_id: 'fireball', name: 'Fireball' },
  { power_id: 'meteor_pop', name: 'Meteor Pop' },
  { power_id: 'trip_hex', name: 'Trip Hex' },
  { power_id: 'flash_freeze', name: 'Flash Freeze' },
  { power_id: 'goo_burst', name: 'Goo Burst' },
  { power_id: 'smoke_burst', name: 'Smoke Burst' },
  { power_id: 'knockback', name: 'Knockback' },
  { power_id: 'give_potion', name: 'Give Potion' },
  { power_id: 'chain_lightning', name: 'Chain Lightning' },
  { power_id: 'give_random_item', name: 'Give Random Item' },
  { power_id: 'magic_missile', name: 'Magic Missile' },
  { power_id: 'shield', name: 'Shield' },
  { power_id: 'ray_of_frost', name: 'Ray of Frost' },
  { power_id: 'sleep', name: 'Sleep' },
  { power_id: 'lightning_bolt', name: 'Lightning Bolt' },
];

// Populated from the EBS on load; starts as the fallback so the page renders
// something immediately.
let powerCatalog = FALLBACK_POWERS.slice();

const state = { token: '', channelId: '' };
const $ = (id) => document.getElementById(id);

function setStatus(msg) { const el = $('status'); if (el) el.textContent = msg; }
function setSaveStatus(msg) { const el = $('save-status'); if (el) el.textContent = msg; }

function renderPowerToggles(selected) {
  const wrap = $('power-config');
  wrap.innerHTML = '';
  const chosen = new Set(selected || []);
  powerCatalog.forEach((p) => {
    const id = `pc_${p.power_id}`;
    const row = document.createElement('label');
    row.className = 'config__power';
    if (p.description) row.title = p.description;
    row.innerHTML = `<input type="checkbox" id="${id}" value="${p.power_id}" ${chosen.has(p.power_id) ? 'checked' : ''}/> <span></span>`;
    row.querySelector('span').textContent = p.name || p.power_id;
    wrap.appendChild(row);
  });
}

function selectedPowers() {
  return powerCatalog
    .map((p) => p.power_id)
    .filter((pid) => { const el = document.getElementById(`pc_${pid}`); return el && el.checked; });
}

/** Fetch the authoritative power list from the EBS (server power defs). */
async function refreshPowerCatalog() {
  if (!state.token) return;
  try {
    const resp = await fetch(`${EBS_BASE}/api/twitch/ext/powers`, {
      headers: { 'Authorization': `Bearer ${state.token}` },
    });
    const data = await resp.json().catch(() => ({}));
    if (resp.ok && data.ok && Array.isArray(data.powers) && data.powers.length) {
      powerCatalog = data.powers;
      loadExisting(); // re-render toggles with the fresh list, keeping saved ticks
    }
  } catch (_) { /* keep the fallback list */ }
}

if (twitch) {
  twitch.onAuthorized((auth) => {
    state.token = auth.token;
    state.channelId = auth.channelId;
    setStatus('Ready.');
    loadExisting();
    refreshPowerCatalog();
  });

  twitch.configuration.onChanged(() => {
    loadExisting();
  });
} else {
  setStatus('Twitch Extension Helper not loaded.');
}

function loadExisting() {
  let cfg = {};
  try {
    const raw = twitch && twitch.configuration && twitch.configuration.broadcaster;
    if (raw && raw.content) cfg = JSON.parse(raw.content);
  } catch (_) { cfg = {}; }
  if (cfg.session_id) $('session-id').value = cfg.session_id;
  renderPowerToggles(cfg.purchasable_powers || []);
}

$('save').addEventListener('click', async () => {
  const sessionId = $('session-id').value.trim();
  if (!sessionId) { setSaveStatus('Enter a session ID first.'); return; }
  const purchasable = selectedPowers();
  const payload = { session_id: sessionId, purchasable_powers: purchasable };

  // 1) Persist to Twitch broadcaster configuration (survives reloads).
  if (twitch && twitch.configuration) {
    twitch.configuration.set('broadcaster', '1', JSON.stringify(payload));
  }

  // 2) Tell the EBS so it can route channelId -> session_id.
  setSaveStatus('Binding…');
  try {
    const resp = await fetch(`${EBS_BASE}/api/twitch/ext/bind-session`, {
      method: 'POST',
      headers: { 'Authorization': `Bearer ${state.token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await resp.json().catch(() => ({}));
    if (resp.ok && data.ok) {
      setSaveStatus('Saved. This channel is now bound to your game.');
    } else {
      setSaveStatus((data && data.message) || 'Could not bind on the backend.');
    }
  } catch (e) {
    setSaveStatus('Network error reaching the backend.');
  }
});

// Render once immediately so the page is usable before onAuthorized fires.
renderPowerToggles([]);
