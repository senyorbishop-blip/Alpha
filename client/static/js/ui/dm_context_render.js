/*
 * dm_context_render.js
 * Casual D&D / Alpha — rich per-mode content for the DM map-first context panel.
 *
 * Replaces the old "navigation marker" lists with real, live content
 * (current turn, initiative order, party HP, quick actions, grouped tools).
 *
 * Design rules:
 *   - Reads LIVE state (_combat, _normalizeCombatRoster, charProfiles) defensively.
 *     Any missing global falls back to a friendly placeholder; never throws, never blanks.
 *   - Every control calls AppUIDMActions, a small adapter that reuses existing
 *     play.html globals/buttons and warns clearly when a target is unavailable.
 *
 * Public API (used by dm_panel_mode_bridge.js):
 *   AppUIDMContextRender.meta(mode)  -> { gly, title, note }
 *   AppUIDMContextRender.tabs(mode)  -> [[key,label,badge?], ...]
 *   AppUIDMContextRender.render(mode)-> html string
 *   AppUIDMContextRender.afterRender(container) -> wire dynamic bits (search input)
 */
(function () {
  'use strict';

  /* ---------- safe helpers ---------- */
  function g(name) { try { return window[name]; } catch (_e) { return undefined; } }
  function num(v, d) { v = Number(v); return Number.isFinite(v) ? v : d; }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }
  function pct(hp, max) {
    max = num(max, 0); hp = num(hp, 0);
    if (max <= 0) return 0;
    return Math.max(0, Math.min(100, Math.round((hp / max) * 100)));
  }
  function hpBar(hp, max, full) {
    var w = pct(hp, max);
    var fill = full ? ' data-full="true"' : '';
    return '<div class="dcx-hpbar"><i style="width:' + w + '%"' + fill + '></i></div>';
  }

  /* ---------- live state readers (all guarded) ---------- */
  function combat() { var c = g('_combat'); return (c && typeof c === 'object') ? c : null; }

  function roster() {
    // Preferred: the page's own normalizer (handles visibility, current turn, etc.)
    var fn = g('_normalizeCombatRoster');
    var c = combat();
    if (typeof fn === 'function') {
      try {
        var out = fn(c || undefined);
        if (Array.isArray(out)) return out;
        if (out && Array.isArray(out.rows)) return out.rows;       // tolerate {rows:[…]}
        if (out && Array.isArray(out.normalized)) return out.normalized;
      } catch (_e) { /* fall through */ }
    }
    // Fallback: raw combatants
    if (c && Array.isArray(c.combatants)) {
      var turn = num(c.turn, 0);
      return c.combatants.map(function (com, i) {
        return {
          name: com && com.name, color: (com && com.color) || '#888',
          initiative: com && com.initiative, isCurrent: c.active && i === turn,
          isPlayerOwned: !!(com && (com.is_player || com.owner_id)),
          combatant: com, token: null, defeated: num(com && com.hp, 1) <= 0,
        };
      });
    }
    return [];
  }

  function rowHp(r) {
    var com = r.combatant || {}; var tok = r.token || {};
    var hp = com.hp != null ? com.hp : (tok.hp != null ? tok.hp : (tok.currentHP != null ? tok.currentHP : null));
    var max = com.max_hp != null ? com.max_hp : (tok.max_hp != null ? tok.max_hp : (tok.maxHP != null ? tok.maxHP : null));
    return { hp: hp, max: max };
  }

  function modifier(v) {
    if (v == null || v === '') return '';
    v = Number(v); if (!Number.isFinite(v)) return '';
    return (v >= 0 ? '+' : '') + v;
  }

  function isLive() { var c = combat(); return !!(c && c.active && roster().length); }
  function selectedTokenId() {
    var id = g('_teTokenId');
    if (id) return String(id);
    var tok = g('ctxToken');
    if (tok && tok.id) return String(tok.id);
    if (typeof g('getSelectedTokenId') === 'function') {
      try { return String(g('getSelectedTokenId')() || ''); } catch (_e) {}
    }
    return '';
  }
  function selectedCombatant() {
    var tid = selectedTokenId();
    var rows = roster();
    if (tid) {
      for (var i = 0; i < rows.length; i++) {
        var com = rows[i] && rows[i].combatant;
        if (com && String(com.token_id || '') === tid) return com;
      }
    }
    return (rows.filter(function (r) { return r.isCurrent; })[0] || rows[0] || {}).combatant || null;
  }


  /* ---------- DM action bridge: clean panel -> live runtime controls ---------- */
  function warnAction(name, detail) {
    if (window.console) console.warn('[dm-actions] ' + name + ' unavailable' + (detail ? ': ' + detail : ''));
    try { if (typeof window.showToast === 'function') window.showToast(name + ' is not available in this view.'); } catch (_e) {}
  }
  function callGlobal(name, args) {
    var fn = g(name);
    if (typeof fn === 'function') {
      try { return fn.apply(window, args || []); } catch (err) { if (window.console) console.warn('[dm-actions] ' + name + ' failed', err); return undefined; }
    }
    warnAction(name, 'missing global function');
    return undefined;
  }
  function clickId(id, label, options) {
    options = options || {};
    var el = document.getElementById(id);
    if (el && typeof el.click === 'function') { el.click(); return true; }
    if (!options.quiet) warnAction(label || id, 'missing #' + id);
    return false;
  }
  function openLegacyDrawer(tab) {
    if (typeof g('switchRTab') === 'function') callGlobal('switchRTab', [tab]);
    else warnAction('Open ' + tab, 'switchRTab missing');
    try {
      document.body.classList.add('dm-legacy-drawer-open');
      document.body.dataset.dmLegacyDrawerTab = String(tab || '');
      if (window.AppUIDMPanelModeBridge && typeof window.AppUIDMPanelModeBridge.forceLegacyRightPanelsClosed === 'function') {
        window.AppUIDMPanelModeBridge.forceLegacyRightPanelsClosed(document, { allowDrawer: true });
      }
    } catch (_e) {}
  }
  function refreshContext() {
    try {
      if (window.AppUIDMPanelModeBridge && typeof window.AppUIDMPanelModeBridge.refresh === 'function') {
        window.AppUIDMPanelModeBridge.refresh(document);
        return true;
      }
    } catch (err) { if (window.console) console.warn('[dm-actions] refreshContext failed', err); }
    warnAction('Refresh context', 'AppUIDMPanelModeBridge.refresh missing');
    return false;
  }
  function sendCombatRollInitiative(combatantId) {
    if (typeof g('sendWS') !== 'function') return warnAction('Roll initiative', 'sendWS missing');
    var payload = combatantId ? { combatant_id: combatantId } : {};
    return callGlobal('sendWS', [{ type: 'combat_roll_initiative', payload: payload }]);
  }
  function closeLegacyDrawer() {
    try {
      document.body.classList.remove('dm-legacy-drawer-open');
      delete document.body.dataset.dmLegacyDrawerTab;
    } catch (_e) {}
  }
  function openFlyout(id) {
    if (typeof g('toggleFlyout') === 'function') return callGlobal('toggleFlyout', [id]);
    return clickId(id, 'Open ' + id);
  }
  function firstPendingId() {
    var fn = g('currentViewerPendingEntries');
    if (typeof fn === 'function') {
      try { var rows = fn(); return rows && rows[0] && rows[0].id; } catch (_e) {}
    }
    return '';
  }
  var Actions = window.AppUIDMActions || {};
  Object.assign(Actions, {
    refreshContext: refreshContext,
    selectToken: function (tokenOrId) {
      var id = tokenOrId && tokenOrId.id ? tokenOrId.id : tokenOrId;
      if (id) { try { window._teTokenId = String(id); } catch (_e) {} }
      refreshContext();
      return !!id;
    },
    startCombat: function () { return (typeof g('combatStart') === 'function') ? callGlobal('combatStart') : clickId('combat-start-btn', 'Start combat'); },
    previousTurn: function () { return (typeof g('combatPrev') === 'function') ? callGlobal('combatPrev') : clickId('combat-prev-btn', 'Previous turn'); },
    nextTurn: function () { return (typeof g('combatNext') === 'function') ? callGlobal('combatNext') : clickId('combat-next-btn', 'Next turn'); },
    endTurn: function () { return (typeof g('combatEndTurn') === 'function') ? callGlobal('combatEndTurn') : Actions.nextTurn(); },
    endCombat: function () { return (typeof g('combatClear') === 'function') ? callGlobal('combatClear') : clickId('combat-end-btn', 'End combat'); },
    clearCombat: function () { return Actions.endCombat(); },
    rollInitiativeSelected: function () { var target = selectedCombatant(); if (target && target.id) return (typeof g('combatRollInitiative') === 'function') ? callGlobal('combatRollInitiative', [target.id]) : sendCombatRollInitiative(target.id); return clickId('combat-roll-selected-btn', 'Roll selected initiative', { quiet: true }) || warnAction('Roll selected initiative', 'no selected/current combatant available'); },
    rollInitiativeAll: function () { var rows = roster(); var any = false; rows.forEach(function (r) { var com = r && r.combatant; if (com && com.id) { any = true; (typeof g('combatRollInitiative') === 'function') ? callGlobal('combatRollInitiative', [com.id]) : sendCombatRollInitiative(com.id); } }); if (!any) return clickId('combat-roll-all-btn', 'Roll all initiative', { quiet: true }) || sendCombatRollInitiative(); return true; },
    rollInitiative: function () { return Actions.rollInitiativeSelected(); },
    addSelectedToCombat: function () { var id = selectedTokenId(); if (id && typeof g('combatAddTokenToInitiative') === 'function') return callGlobal('combatAddTokenToInitiative', [id]); if (id && typeof g('combatAddSelectedTokenToInitiative') === 'function') return callGlobal('combatAddSelectedTokenToInitiative', [id]); return clickId('combat-add-selected-btn', 'Add selected token to combat', { quiet: true }) || warnAction('Add selected token to combat', id ? 'no add-token function available' : 'no selected token'); },
    addCombatant: function () { return callGlobal('combatAddManual'); },
    addSelectedTokenToCombat: function () { return Actions.addSelectedToCombat(); },
    openCombatTracker: function () { return openLegacyDrawer('combat'); },
    openCompactCombatDrawer: function () { return openLegacyDrawer('combat'); },
    openViewerPowers: function () { openFlyout('flyout-perm'); return openLegacyDrawer('party'); },
    grantViewerPower: function () { return callGlobal('grantViewerPower') || openLegacyDrawer('party'); },
    grantViewerPowerPreset: function () { return callGlobal('grantViewerPowerPreset'); },
    approveViewerPower: function (id) { id = id || firstPendingId(); return id ? callGlobal('decideViewerPending', [id, true]) : warnAction('Approve viewer power', 'no pending approval selected'); },
    rejectViewerPower: function (id) { id = id || firstPendingId(); return id ? callGlobal('decideViewerPending', [id, false]) : warnAction('Reject viewer power', 'no pending approval selected'); },
    openViewerPowerSettings: function () { return openFlyout('flyout-perm'); },
    openBestiary: function () { return openLegacyDrawer('bestiary'); },
    spawnSelectedCreature: function () { return callGlobal('beginBestiarySpawn') || openLegacyDrawer('bestiary'); },
    editSelectedToken: function () { return openFlyout('flyout-token'); },
    toggleSelectedHidden: function () { return clickId('te-hidden', 'Toggle selected hidden') || openFlyout('flyout-token'); },
    openParty: function () { return openLegacyDrawer('party'); },
    openMemory: function () { return openLegacyDrawer('memory'); },
    openHandouts: function () { return openLegacyDrawer('handouts'); },
    openInventory: function () { return openLegacyDrawer('inventory'); },
    openShop: function () { return openLegacyDrawer('shop'); },
    openChat: function () { return openLegacyDrawer('log'); },
    openEditor: function () { return openFlyout('flyout-editor'); },
    openFog: function () { return openFlyout('flyout-fog'); },
    openMap: function () { return openFlyout('flyout-map'); },
    openSound: function () { return openFlyout('flyout-sound'); },
    openJournal: function () { return openFlyout('flyout-journal'); },
    closeLegacyDrawer: closeLegacyDrawer,

    openItemSearch: function () {
      // Open the item library manager (search + manage SRD & custom items)
      if (typeof g('openItemLibraryManager') === 'function') return callGlobal('openItemLibraryManager');
      if (typeof g('openItemLibraryModal') === 'function') return callGlobal('openItemLibraryModal', ['manage']);
      return warnAction('Item Search', 'openItemLibraryManager not available');
    },

    openLootContainers: function () {
      // Open inventory tab and scroll to the party stash / loot section
      openLegacyDrawer('inventory');
      try {
        setTimeout(function () {
          var stash = document.getElementById('party-stash-widget');
          if (stash) {
            stash.style.display = stash.style.display === 'none' ? 'flex' : stash.style.display;
            stash.scrollIntoView({ behavior: 'smooth', block: 'start' });
          }
        }, 120);
      } catch (_e) {}
    },

    openShopSetup: function () {
      // Try to open ShopPanel for a selected shop prop; fall back to opening a blank ShopPanel
      var ShopPanel = g('ShopPanel');
      if (ShopPanel && typeof ShopPanel.open === 'function') {
        // Try to find a currently selected shop prop
        var prop = null;
        try {
          var ctxProp = g('ctxProp') || g('_ctxShopProp') || g('_selectedProp');
          if (ctxProp && ctxProp.kind && String(ctxProp.kind).toLowerCase().indexOf('shop') !== -1) prop = ctxProp;
        } catch (_e) {}
        if (prop) {
          try { if (typeof g('configureShopFromProp') === 'function') return callGlobal('configureShopFromProp', [prop]); } catch (_e) {}
        }
        // Open with generic defaults — DM can still configure all fields
        try {
          ShopPanel.open({ name: 'New Shop', shopkeeper: '', type: 'general', personality: 'friendly', inventory: [] }, function () {});
          return;
        } catch (_e) {}
      }
      // Last resort: open the shop ledger tab
      openLegacyDrawer('shop');
      try {
        if (typeof g('showToast') === 'function') g('showToast')('Right-click a shop prop on the map to configure it.');
      } catch (_e) {}
    },

    openGrantItem: function () {
      openLegacyDrawer('inventory');
      try {
        setTimeout(function () {
          if (typeof g('openInventoryManualAddModal') === 'function') callGlobal('openInventoryManualAddModal');
        }, 150);
      } catch (_e) {}
    },

    openGrantGold: function () {
      openLegacyDrawer('inventory');
      try {
        setTimeout(function () {
          if (typeof g('openInventoryGoldModal') === 'function') callGlobal('openInventoryGoldModal', ['add']);
        }, 150);
      } catch (_e) {}
    },

    openAttunement: function () {
      // Attunement is managed per-item in the inventory panel
      openLegacyDrawer('inventory');
      try {
        if (typeof g('showToast') === 'function') g('showToast')('Select an item in inventory to attune/unattune it.');
      } catch (_e) {}
    }
  });
  window.AppUIDMActions = Actions;

  /* ---------- party rows ---------- */
  function partyRows() {
    var rows = roster().filter(function (r) { return r.isPlayerOwned; });
    if (rows.length) {
      return rows.map(function (r) {
        var h = rowHp(r);
        return { name: r.name || 'Adventurer', hp: h.hp, max: h.max, full: num(h.hp, 0) >= num(h.max, 0) && num(h.max, 0) > 0 };
      });
    }
    // Out of combat: try character profiles
    var cp = g('charProfiles');
    if (Array.isArray(cp) && cp.length) {
      return cp.map(function (p) {
        var n = p && (p.name || p.character_name || p.charName);
        var hp = p && (p.hp != null ? p.hp : (p.current_hp != null ? p.current_hp : null));
        var max = p && (p.max_hp != null ? p.max_hp : (p.maxHp != null ? p.maxHp : null));
        return { name: n || 'Adventurer', hp: hp, max: max, full: num(hp, 0) >= num(max, 0) && num(max, 0) > 0 };
      });
    }
    return [];
  }

  /* ---------- small templates ---------- */
  function block(title, inner, opts) {
    opts = opts || {};
    var act = opts.action
      ? '<button class="dcx-eyebrow-act" type="button" onclick="' + esc(opts.action) + '">' + esc(opts.actionLabel || 'Open') + '</button>'
      : '';
    var eb = title ? '<div class="dcx-eyebrow">' + esc(title) + act + '</div>' : '';
    return '<section class="dcx-section">' + eb + '<div class="' + (opts.cls || 'dcx-card') + '">' + inner + '</div></section>';
  }
  function action(ic, t, s, onclick) {
    return '<button class="dcx-action" type="button" onclick="' + esc(onclick) + '">' +
      '<span class="dcx-ai">' + ic + '</span><span class="dcx-al"><span class="t">' + esc(t) + '</span>' +
      '<span class="s">' + esc(s) + '</span></span></button>';
  }
  function tool(ic, t, s, onclick) {
    return '<button class="dcx-tl" type="button" onclick="' + esc(onclick) + '"><span class="dcx-ti">' + ic + '</span>' +
      '<span class="tt">' + esc(t) + '</span>' + (s ? '<span class="ts">' + esc(s) + '</span>' : '') + '</button>';
  }
  function stool(ic, l, onclick) {
    return '<button class="dcx-stool" type="button" onclick="' + esc(onclick) + '"><span class="g">' + ic + '</span>' + esc(l) + '</button>';
  }
  function empty(msg) { return '<div class="dcx-empty">' + msg + '</div>'; }

  /* ---------- shared blocks ---------- */
  function partyBlock() {
    var rows = partyRows();
    var inner;
    if (!rows.length) {
      inner = empty('No party loaded yet.');
    } else {
      inner = '<div class="dcx-party">' + rows.map(function (p) {
        var label = (p.hp != null && p.max != null) ? esc(p.hp) + '/' + esc(p.max) : '—';
        return '<div class="dcx-party-row' + (p.full ? ' full' : '') + '">' +
          '<span class="dcx-av"></span><span class="dcx-pn">' + esc(p.name) + '</span>' +
          '<span class="dcx-pcol">' + hpBar(p.hp, p.max, p.full) + '<span class="dcx-pnum">' + label + '</span></span></div>';
      }).join('') + '</div>';
    }
    return block('Party overview', inner, { action: "AppUIDMActions.openParty()", actionLabel: 'Full party' });
  }

  function initiativeBlock() {
    var rows = roster();
    if (!isLive()) {
      return block('Initiative order',
        empty('Combat isn\u2019t running.<br><button class="dcx-start" type="button" onclick="AppUIDMActions.startCombat()">\u2694 Start combat</button>'));
    }
    var inner = '<div class="dcx-init">' + rows.map(function (r, i) {
      var h = rowHp(r);
      var hpTxt = (h.hp != null && h.max != null) ? esc(h.hp) + '/' + esc(h.max) : '';
      var kind = r.isPlayerOwned ? 'Player' : (r.combatant && r.combatant.kind) || 'NPC';
      return '<div class="dcx-init-row" data-active="' + (!!r.isCurrent) + '" ' +
        'onclick="AppUIDMActions.openCombatTracker()">' +
        '<span class="dcx-ord">' + (i + 1) + '</span>' +
        '<span class="dcx-who"><span class="n">' + esc(r.name || 'Combatant') + '</span><span class="k">' + esc(kind) + '</span></span>' +
        '<span class="dcx-mod">' + esc(modifier(r.initiative)) + '</span>' +
        '<span class="dcx-ihp">' + hpTxt + '</span></div>';
    }).join('') + '</div>';
    return block('Initiative order', inner, { action: "AppUIDMActions.openCombatTracker()", actionLabel: 'Open tracker' });
  }

  function currentTurnBlock() {
    if (!isLive()) {
      return block('Current turn', empty('Start combat to track turns.'), { cls: 'dcx-card glow' });
    }
    var rows = roster();
    var cur = rows.filter(function (r) { return r.isCurrent; })[0] || rows[0];
    var h = rowHp(cur);
    var inner =
      '<div class="dcx-turn">' +
        '<div class="dcx-portrait" style="background:radial-gradient(circle at 35% 30%,' + esc(cur.color || '#6fe0d2') + ',rgba(0,0,0,.35))"></div>' +
        '<div class="dcx-tinfo"><div class="r1"><span class="nm">' + esc(cur.name || 'Combatant') + '</span></div>' +
          '<div class="cls">' + (cur.isPlayerOwned ? 'Player turn' : 'NPC turn') + '</div>' +
          '<div class="dcx-chips"><span class="dcx-chip">Init <b>' + esc(modifier(cur.initiative) || '0') + '</b></span></div></div>' +
        '<div class="dcx-hpwrap"><span class="dcx-hpnum">' + (h.hp != null ? esc(h.hp) : '—') +
          '<span class="sm">' + (h.max != null ? '/' + esc(h.max) : '') + '</span></span>' + hpBar(h.hp, h.max) + '</div>' +
      '</div>' +
      '<div class="dcx-turn-cta"><button class="dcx-primary" type="button" onclick="AppUIDMActions.nextTurn()">End turn \u25B8</button></div>';
    return block('Current turn', inner, { cls: 'dcx-card glow' });
  }

  function sessionToolsBlock() {
    return block('Session tools',
      '<div class="dcx-stools">' +
        stool('\uD83D\uDCD3', 'Notes', "AppUIDMActions.openMemory()") +
        stool('\uD83D\uDCDC', 'Handouts', "AppUIDMActions.openHandouts()") +
        stool('\uD83C\uDFB5', 'Sound', "AppUIDMActions.openSound()") +
        stool('\uD83D\uDCD3', 'Journal', "AppUIDMActions.openJournal()") +
      '</div>');
  }

  /* ---------- per-mode definitions ---------- */
  var META = {
    'run':           { gly: '\u25B6',     title: 'Live Table',    note: 'Map remains primary' },
    'combat':        { gly: '\u2694',     title: 'Combat',        note: 'Turn-based control' },
    'map-build':     { gly: '\uD83E\uDDF1', title: 'Map Build',   note: 'Editing layer \u00B7 DM only' },
    'npc-monster':   { gly: '\uD83D\uDC09', title: 'NPC / Monster', note: 'Bestiary \u00B7 spawn' },
    'loot-shop':     { gly: '\uD83C\uDFEA', title: 'Loot / Shop', note: 'Economy \u00B7 rewards' },
    'session-tools': { gly: '\uD83D\uDCDC', title: 'Session Tools', note: 'Story \u00B7 journal \u00B7 audio' },
    'viewer-powers': { gly: '\uD83D\uDC41', title: 'Viewer Powers', note: 'DM only \u00B7 chaos control' },
    'stream':        { gly: '\uD83D\uDCFA', title: 'Stream',      note: 'Twitch \u00B7 overlays \u00B7 bridge' },
    'debug':         { gly: '\uD83D\uDC1E', title: 'Debug',       note: 'Troubleshooting' },
  };
  var TABS = {
    'run':           [['party', 'Party'], ['inventory', 'Inventory'], ['memory', 'Moments'], ['combat', 'Combat']],
    'combat':        [['combat', 'Turns'], ['party', 'Party'], ['memory', 'Log']],
    'map-build':     [],
    'npc-monster':   [['bestiary', 'Bestiary'], ['combat', 'Encounter']],
    'loot-shop':     [['shop', 'Items'], ['inventory', 'Party']],
    'session-tools': [['handouts', 'Handouts'], ['memory', 'Journal']],
    'viewer-powers': [['party', 'Viewers']],
    'stream':        [],
    'debug':         [],
  };

  var RENDER = {
    'run': function () {
      return currentTurnBlock().replace('Current turn', 'Selected / current') + partyBlock() + sessionToolsBlock();
    },
    'combat': function () {
      return currentTurnBlock() + initiativeBlock() + partyBlock() +
        block('Encounter controls',
          '<div class="dcx-actions dcx-combat-controls">' +
            action('\u2694', 'Start Combat', 'pull current map tokens', "AppUIDMActions.startCombat()") +
            action('\u25C0', 'Previous Turn', 'step initiative back', "AppUIDMActions.previousTurn()") +
            action('\u25B6', 'Next / End Turn', 'advance initiative', "AppUIDMActions.nextTurn()") +
            action('\u2715', 'End Combat / Clear Combat', 'clear encounter', "AppUIDMActions.endCombat()") +
            action('\uD83C\uDFB2', 'Roll Initiative', 'selected or current combatant', "AppUIDMActions.rollInitiativeSelected()") +
            action('\uD83C\uDFB2', 'Roll Selected Initiative', 'selected token in encounter', "AppUIDMActions.rollInitiativeSelected()") +
            action('\uD83C\uDFB2', 'Roll All Initiative', 'all combatants', "AppUIDMActions.rollInitiativeAll()") +
            action('\u2795', 'Add Combatant', 'manual entry', "AppUIDMActions.addCombatant()") +
            action('\uD83D\uDCCC', 'Add Selected Token to Combat', 'current map token', "AppUIDMActions.addSelectedToCombat()") +
            action('\uD83D\uDCCB', 'Open Tracker', 'compact details drawer', "AppUIDMActions.openCombatTracker()") +
          '</div>') +
        sessionToolsBlock();
    },
    'map-build': function () {
      return block('Build tools',
        '<div class="dcx-toollist">' +
          tool('\u26F0', 'Map Editor', 'terrain, walls, doors, props', "AppUIDMActions.openEditor()") +
          tool('\uD83D\uDDFA', 'Place Token', 'create / place on map', "AppUIDMActions.editSelectedToken()") +
          tool('\u263C', 'Lighting / Weather', '', "AppUIDMActions.openMap()") +
          tool('\uD83C\uDF2B', 'Fog of War', 'reveal/hide', "AppUIDMActions.openFog()") +
        '</div>') +
        block('Apply', '<button class="dcx-primary wide" type="button" onclick="AppUIDMActions.openEditor()">Open map editor \u25B8</button>');
    },
    'npc-monster': function () {
      return block('Find a creature',
        '<div class="dcx-searchrow"><input id="dcx-bestiary-search" type="search" placeholder="Search bestiary\u2026" autocomplete="off">' +
          '<button class="dcx-filter" type="button" onclick="AppUIDMActions.openBestiary()">Filters</button></div>' +
          '<div class="dcx-hint">Type to search, then pick a creature in the Bestiary tab.</div>') +
        block('Place on map',
          '<div class="dcx-primary-actions">' +
            '<button class="dcx-pa go" type="button" onclick="AppUIDMActions.spawnSelectedCreature()">\u2295 Spawn</button>' +
            '<button class="dcx-pa" type="button" onclick="AppUIDMActions.openCombatTracker()">+ Encounter</button>' +
            '<button class="dcx-pa" type="button" onclick="AppUIDMActions.editSelectedToken()">\u270E Edit stats</button>' +
            '<button class="dcx-pa" type="button" onclick="AppUIDMActions.toggleSelectedHidden()">\u25D0 Hide / reveal</button>' +
          '</div>');
    },
    'loot-shop': function () {
      return block('Economy tools',
        '<div class="dcx-toollist">' +
          tool('\uD83D\uDD0E', 'Item search', 'SRD + custom library', "AppUIDMActions.openItemSearch()") +
          tool('\uD83D\uDCE6', 'Loot containers', 'party stash &amp; chests', "AppUIDMActions.openLootContainers()") +
          tool('\uD83C\uDFEA', 'Shop setup', 'configure NPC shop', "AppUIDMActions.openShopSetup()") +
          tool('\uD83C\uDF81', 'Grant item', 'add to player inventory', "AppUIDMActions.openGrantItem()") +
          tool('\uD83E\uDE99', 'Grant gold', 'award currency', "AppUIDMActions.openGrantGold()") +
          tool('\u26A1', 'Charges / attunement', 'manage in inventory', "AppUIDMActions.openAttunement()") +
        '</div>');
    },
    'session-tools': function () {
      return block('Story &amp; table',
        '<div class="dcx-toollist">' +
          tool('\uD83C\uDFAF', 'Quests', '', "AppUIDMActions.openJournal()") +
          tool('\uD83D\uDCD3', 'Journal', '', "AppUIDMActions.openJournal()") +
          tool('\uD83D\uDCDC', 'Handouts', 'share', "AppUIDMActions.openHandouts()") +
          tool('\uD83C\uDF99', 'Narration', '', "AppUIDMActions.openSound()") +
          tool('\uD83C\uDFB5', 'Sound / ambience', '', "AppUIDMActions.openSound()") +
          tool('\uD83D\uDCAC', 'Party message', '', "AppUIDMActions.openChat()") +
        '</div>');
    },
    'viewer-powers': function () {
      return block('Connected viewers', '<div id="dcx-viewer-summary">' + empty('No viewers connected.<br>Share the viewer link to let spectators join.') + '</div>') +
        block('Power controls',
          '<div class="dcx-toollist">' +
            tool('\uD83C\uDF81', 'Grant Power / Presets', 'Fireball, Knockback\u2026', "AppUIDMActions.openViewerPowers()") +
            tool('\uD83D\uDCE6', 'Grant Preset Pack', 'Support / Chaos / Boss', "AppUIDMActions.grantViewerPowerPreset()") +
            tool('\u23F3', 'Pending Approvals', 'review queue', "AppUIDMActions.openViewerPowers()") +
            tool('\u2705', 'Approve Pending', 'first queued power', "AppUIDMActions.approveViewerPower()") +
            tool('\u274C', 'Reject Pending', 'first queued power', "AppUIDMActions.rejectViewerPower()") +
            tool('\u2744', 'Cooldowns / Settings', 'FX + limits', "AppUIDMActions.openViewerPowerSettings()") +
            tool('\uD83C\uDFAF', 'Target Selection', 'viewer target/source', "AppUIDMActions.openViewerPowers()") +
          '</div>') +
        block('Management', '<button class="dcx-primary wide" type="button" onclick="AppUIDMActions.openViewerPowers()">Open compact viewer powers drawer \u25B8</button>');
    },
    'stream': function () {
      // Content is the shared Twitch Integration panel (twitch_settings.js) —
      // mounted by afterRender so connect/disconnect/overlay logic stays in
      // one place instead of being duplicated here.
      return block('Twitch Integration',
        '<div id="dcx-stream-panel" style="font-size:0.7rem;">' +
          empty('Loading Twitch settings…') +
        '</div>');
    },
    'debug': function () {
      return block('Diagnostics',
        '<div class="dcx-toollist">' +
          tool('\uD83D\uDCE1', 'Stream readiness', 'WS + sync status', "if(window.renderStreamReadinessPanel)renderStreamReadinessPanel()") +
        '</div>') +
        '<div id="stream-readiness-panel" aria-live="polite"></div>';
    },
  };

  /* ---------- public API ---------- */
  function meta(mode) { return META[mode] || META.run; }
  function tabs(mode) { return TABS[mode] || []; }
  function render(mode) {
    var fn = RENDER[mode] || RENDER.run;
    try { return fn(); } catch (e) {
      if (window.console) console.warn('[dm-context-render] render failed', mode, e);
      return empty('This panel hit a snag. Tools are still available from the rail.');
    }
  }
  function afterRender(container) {
    if (!container || !container.querySelector) return;
    var search = container.querySelector('#dcx-bestiary-search');
    if (search) {
      search.addEventListener('input', function () {
        var real = document.getElementById('bestiary-search');
        if (real) { real.value = search.value; real.dispatchEvent(new Event('input', { bubbles: true })); }
      });
    }
    var streamPanel = container.querySelector('#dcx-stream-panel');
    if (streamPanel) {
      if (window.TwitchSettingsPanel && typeof window.TwitchSettingsPanel.renderInto === 'function') {
        window.TwitchSettingsPanel.renderInto(streamPanel);
      } else {
        streamPanel.innerHTML = empty('Twitch settings module not loaded.');
      }
    }
  }

  window.AppUIDMContextRender = Object.freeze({ meta: meta, tabs: tabs, render: render, afterRender: afterRender });
})();
