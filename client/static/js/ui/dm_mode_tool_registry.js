(function () {
  'use strict';

  const MODE_TOOL_REGISTRY = Object.freeze({
    run: Object.freeze({
      label: 'Live Table',
      purpose: 'Live session control while the table is actively playing.',
      primaryTools: Object.freeze([
        'selected-token-summary',
        'party-overview',
        'current-scene-notes',
        'handout-shortcuts',
        'journal-shortcuts',
        'narration-shortcuts',
        'viewer-power-shortcuts',
        'compact-save-state',
      ]),
      keepOut: Object.freeze([
        'wall-editor',
        'fog-brush',
        'shop-editor',
        'full-item-library',
        'full-bestiary-editor',
        'debug-diagnostics',
      ]),
    }),
    combat: Object.freeze({
      label: 'Combat',
      purpose: 'Turn-based encounter control.',
      primaryTools: Object.freeze([
        'initiative-order',
        'current-turn',
        'action-usage',
        'movement-usage',
        'hp-summary',
        'conditions',
        'attack-roll-helpers',
        'damage-roll-helpers',
        'save-dc-helpers',
        'end-turn-controls',
      ]),
      keepOut: Object.freeze(['map-asset-library', 'shop-editor', 'debug-diagnostics']),
    }),
    'map-build': Object.freeze({
      label: 'Map Build',
      purpose: 'Map preparation, editing, and environment control.',
      primaryTools: Object.freeze([
        'terrain-tools',
        'fog-tools',
        'wall-tools',
        'door-tools',
        'reveal-hide-tools',
        'token-layer',
        'prop-layer',
        'lighting-weather-tools',
        'asset-library',
        'map-save-apply',
      ]),
      keepOut: Object.freeze(['initiative-order', 'shop-editor', 'viewer-approval-queue']),
    }),
    'npc-monster': Object.freeze({
      label: 'NPC / Monster',
      purpose: 'Creature search, spawning, notes, visibility, and quick actions.',
      primaryTools: Object.freeze([
        'bestiary-search',
        'spawn-token',
        'creature-hp-ac-speed',
        'visibility-state',
        'initiative-modifier',
        'conditions',
        'creature-notes',
        'creature-quick-actions',
      ]),
      keepOut: Object.freeze(['shop-editor', 'full-map-terrain-tools']),
    }),
    'loot-shop': Object.freeze({
      label: 'Loot / Shop',
      purpose: 'Economy, items, shops, rewards, charges, and party inventory changes.',
      primaryTools: Object.freeze([
        'item-search',
        'loot-containers',
        'corpse-loot',
        'shop-setup',
        'grant-item',
        'grant-gold',
        'charges',
        'attunement',
        'party-inventory-adjustments',
      ]),
      keepOut: Object.freeze(['wall-editor', 'initiative-order', 'debug-diagnostics']),
    }),
    'session-tools': Object.freeze({
      label: 'Session Tools',
      purpose: 'Story, journal, handouts, narration, audio, polls, and save tools.',
      primaryTools: Object.freeze([
        'quests',
        'handouts',
        'journal',
        'discoveries',
        'narration',
        'sound',
        'polls',
        'party-messages',
        'autosave-save-tools',
      ]),
      keepOut: Object.freeze(['wall-editor', 'combat-turn-controls']),
    }),
    'viewer-powers': Object.freeze({
      label: 'Viewer Powers',
      purpose: 'Viewer chaos management without crowding the live table.',
      primaryTools: Object.freeze([
        'connected-viewers',
        'viewer-power-grants',
        'pending-approvals',
        'cooldowns',
        'target-selection',
        'approved-rejected-feedback',
      ]),
      keepOut: Object.freeze(['map-editor-tools', 'player-character-sheet', 'debug-diagnostics']),
    }),
    stream: Object.freeze({
      label: 'Stream',
      purpose: 'Twitch integration: channel connection, chat bridge status, and OBS overlay URLs.',
      primaryTools: Object.freeze([
        'twitch-connection',
        'chat-bridge-status',
        'chat-bridge-toggle',
        'overlay-urls',
        'chat-character-roster',
      ]),
      keepOut: Object.freeze(['map-editor-tools', 'initiative-order', 'debug-diagnostics']),
    }),
    debug: Object.freeze({
      label: 'Debug',
      purpose: 'Troubleshooting only. Closed by default.',
      primaryTools: Object.freeze([
        'stream-readiness',
        'payload-warnings',
        'reconnect-warnings',
        'websocket-diagnostics',
        'sync-diagnostics',
        'visibility-checks',
        'dm-focus-testing-guidance',
        'development-only-hints',
      ]),
      keepOut: Object.freeze([]),
      closedByDefault: true,
    }),
  });

  function getModeTools(modeId) {
    return MODE_TOOL_REGISTRY[modeId] || MODE_TOOL_REGISTRY.run;
  }

  function listModeToolRegistry() {
    return Object.entries(MODE_TOOL_REGISTRY).map(([id, config]) => ({
      id,
      label: config.label,
      purpose: config.purpose,
      primaryTools: [...config.primaryTools],
      keepOut: [...config.keepOut],
      closedByDefault: Boolean(config.closedByDefault),
    }));
  }

  window.AppUIDMModeToolRegistry = Object.freeze({
    modes: MODE_TOOL_REGISTRY,
    getModeTools,
    listModeToolRegistry,
  });
})();
