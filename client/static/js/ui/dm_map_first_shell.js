(function () {
  'use strict';

  const MODES = Object.freeze([
    {
      id: 'run',
      label: 'Live Table',
      aliases: ['run-game', 'session', 'table-view'],
      context: ['selected-token', 'party-overview', 'handouts', 'narration', 'save-state'],
    },
    {
      id: 'combat',
      label: 'Combat',
      context: ['current-turn', 'initiative', 'actions-used', 'movement-used', 'hp', 'conditions'],
    },
    {
      id: 'map-build',
      label: 'Map Build',
      context: ['terrain', 'fog', 'walls', 'doors', 'reveal-hide', 'layers', 'lighting-weather'],
    },
    {
      id: 'npc-monster',
      label: 'NPC / Monster',
      context: ['bestiary', 'spawn-token', 'creature-stats', 'notes', 'conditions'],
    },
    {
      id: 'loot-shop',
      label: 'Loot / Shop',
      context: ['items', 'loot-containers', 'shops', 'gold', 'charges', 'attunement'],
    },
    {
      id: 'session-tools',
      label: 'Session Tools',
      context: ['quests', 'handouts', 'journal', 'narration', 'sound', 'polls'],
    },
    {
      id: 'viewer-powers',
      label: 'Viewer Powers',
      context: ['connected-viewers', 'grants', 'approvals', 'cooldowns', 'feedback'],
    },
    {
      id: 'stream',
      label: 'Stream',
      context: ['twitch-connection', 'chat-bridge-status', 'overlay-urls'],
    },
    {
      id: 'debug',
      label: 'Debug',
      context: ['readiness', 'payload', 'reconnect', 'websocket', 'sync-diagnostics'],
      debugOnly: true,
    },
  ]);

  const QUICK_ACTIONS = Object.freeze([
    'select',
    'move',
    'measure',
    'draw',
    'light',
    'notes',
    'more',
  ]);

  function listModes() {
    return MODES.map((mode) => ({ ...mode, context: [...mode.context], aliases: [...(mode.aliases || [])] }));
  }

  function listQuickActions() {
    return [...QUICK_ACTIONS];
  }

  function findMode(modeId) {
    return MODES.find((mode) => {
      if (mode.id === modeId) return true;
      return Array.isArray(mode.aliases) && mode.aliases.includes(modeId);
    }) || MODES[0];
  }

  function setActiveMode(root, modeId) {
    if (!root) return findMode(modeId);
    const mode = findMode(modeId);
    root.dataset.dmMode = mode.id;
    root.querySelectorAll('[data-dm-mode-button]').forEach((button) => {
      const buttonMode = findMode(button.getAttribute('data-dm-mode-button'));
      const active = buttonMode.id === mode.id;
      button.setAttribute('aria-pressed', active ? 'true' : 'false');
      button.toggleAttribute('data-active', active);
    });
    root.querySelectorAll('[data-dm-context-panel]').forEach((panel) => {
      const panelMode = findMode(panel.getAttribute('data-dm-context-panel'));
      const active = panelMode.id === mode.id;
      panel.toggleAttribute('data-active', active);
    });
    return mode;
  }

  function init(root) {
    if (!root) return null;
    root.querySelectorAll('[data-dm-mode-button]').forEach((button) => {
      button.addEventListener('click', () => {
        setActiveMode(root, button.getAttribute('data-dm-mode-button'));
      });
    });
    return setActiveMode(root, root.dataset.dmMode || 'run');
  }

  window.AppUIDMMapFirstShell = Object.freeze({
    modes: MODES,
    quickActions: QUICK_ACTIONS,
    listModes,
    listQuickActions,
    findMode,
    setActiveMode,
    init,
  });
})();
