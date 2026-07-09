(function (global) {
  'use strict';
  // Live runtime guardrail:
  // This module is intentionally only the first-hop dispatcher.
  // Final gameplay message application remains in play.html via
  // `handleLegacyMessage()` until an explicit wiring migration.
  //
  // Phase 3 ownership reduction:
  // - Keep gameplay source-of-truth in play.html.
  // - Move low-risk websocket/message shell routing here by
  //   delegating to explicit env callbacks.

  const DISPATCH_DEBUG = !!global.__LIVE_DEBUG__ || (global.localStorage && global.localStorage.getItem('dnd_live_debug') === '1');
  function dispatchDebugLog() { if (DISPATCH_DEBUG && global.console && console.debug) console.debug.apply(console, arguments); }

  const recentMessageTypes = [];
  const dispatchChain = [];
  let dispatchDepth = 0;

  const COMBAT_MESSAGE_TYPES = new Set([
    'combat_state',
    'combat_attack_result',
    'combat_move_state',
    'combat_move_preview_result',
    'combat_initiative_rolled',
    'token_move_denied',
  ]);
  let combatModuleLoadRequested = false;

  function rememberMessageType(type) {
    recentMessageTypes.push(String(type || '(missing)'));
    if (recentMessageTypes.length > 30) recentMessageTypes.splice(0, recentMessageTypes.length - 30);
  }

  function logDispatchDiagnostics(reason, err) {
    const payload = {
      reason,
      depth: dispatchDepth,
      chain: dispatchChain.slice(),
      recentMessageTypes: recentMessageTypes.slice(),
    };
    console.error('[AppMessageDispatch] dispatch diagnostics', payload, err || '');
    return payload;
  }

  function isCombatMessageType(type) {
    return COMBAT_MESSAGE_TYPES.has(String(type || ''));
  }

  function ensureCombatMessagesModule() {
    if (global.AppCombatMessages || combatModuleLoadRequested) return;
    if (!global.document || !global.document.createElement) return;
    combatModuleLoadRequested = true;
    try {
      if (global.document.querySelector('script[data-app-module="combat-messages"]')) return;
      const script = global.document.createElement('script');
      script.src = '/static/js/gameplay/combat_messages.js?v=20260624';
      script.async = false;
      script.dataset.appModule = 'combat-messages';
      script.onerror = function () {
        console.warn('[AppMessageDispatch] combat_messages.js failed to load; legacy combat handlers remain active');
      };
      const parent = global.document.head || global.document.documentElement;
      if (parent) parent.appendChild(script);
    } catch (err) {
      console.warn('[AppMessageDispatch] unable to request combat_messages.js; legacy combat handlers remain active', err);
    }
  }

  function tryHandleCombatMessage(msg, env) {
    if (!msg || !isCombatMessageType(msg.type)) return false;
    ensureCombatMessagesModule();
    const combatMessages = global.AppCombatMessages;
    if (!combatMessages || typeof combatMessages.handleIncoming !== 'function') return false;
    return combatMessages.handleIncoming(msg, env || {});
  }

  ensureCombatMessagesModule();

  function handleIncoming(msg, env) {
    if (!msg || typeof msg !== 'object') return false;
    const runtimeEnv = env || {};
    if (typeof runtimeEnv.handleLegacyMessage !== 'function') return false;
    const msgType = String(msg.type || '(missing)');
    rememberMessageType(msgType);
    if (global.liveDebugLog) global.liveDebugLog('message received', { message_type: msgType });
    if (dispatchDepth >= 20) {
      const diagnostics = logDispatchDiagnostics('nested dispatch depth exceeded', null);
      const err = new Error('Nested websocket dispatch depth exceeded 20; refusing recursive message dispatch.');
      err.dispatchDiagnostics = diagnostics;
      if (typeof runtimeEnv.reportClientRuntimeError === 'function') runtimeEnv.reportClientRuntimeError('Message dispatch', err);
      return false;
    }
    dispatchDepth += 1;
    dispatchChain.push(msgType);
    try {
      if (msgType === 'combat_state') {
        const payload = msg.payload || {};
        const order = Array.isArray(payload.combatants) ? payload.combatants.map(c => `${c?.name || c?.id || c?.token_id || '?'}:${c?.initiative ?? '--'}`) : [];
        dispatchDebugLog('[message_dispatch] combat_state', { revision: payload.revision, combatants: order.length, turn: payload.turn, activeIndex: Number(payload.turn || 0) });
      }
      if (tryHandleCombatMessage(msg, runtimeEnv)) return true;
      runtimeEnv.handleLegacyMessage(msg);
      return true;
    } catch (err) {
      const diagnostics = logDispatchDiagnostics(err && /Maximum call stack size exceeded/i.test(String(err.message || err)) ? 'stack overflow' : 'handler exception', err);
      try { err.dispatchDiagnostics = diagnostics; } catch (_err) {}
      if (typeof runtimeEnv.reportClientRuntimeError === 'function') {
        runtimeEnv.reportClientRuntimeError('Message dispatch', err);
      } else {
        console.error('[AppMessageDispatch] Message dispatch failed', err);
      }
      return false;
    } finally {
      dispatchChain.pop();
      dispatchDepth = Math.max(0, dispatchDepth - 1);
    }
  }

  function getDispatchDiagnostics() {
    return {
      depth: dispatchDepth,
      chain: dispatchChain.slice(),
      recentMessageTypes: recentMessageTypes.slice(),
    };
  }

  // Phase 4 message-handler migration: types whose handler in
  // window.AppMessageHandlers is a verified behavior-twin of the former inline
  // case in play.html handleLegacyMessage(). Delegating here makes the module
  // the live path; the inline case is removed once parity is confirmed. See
  // docs/message_handler_migration_map.md. Combat types are intentionally
  // excluded — they are routed to AppCombatMessages before legacy dispatch.
  const MIGRATED_MODULE_HANDLERS = {
    char_profiles_sync: 'handleCharProfilesSync',
    editor_layers_sync: 'handleEditorLayersSync',
    editor_world_sync: 'handleEditorWorldSync',
    viewer_pending_sync: 'handleViewerPendingSync',
    viewer_fx: 'handleViewerFx',
    permission_granted: 'handlePermissionGranted',
  };

  function tryHandleMigratedModuleMessage(msg, runtimeEnv) {
    const handlerName = MIGRATED_MODULE_HANDLERS[msg.type];
    if (!handlerName) return false;
    const handlers = global.AppMessageHandlers;
    if (!handlers || typeof handlers[handlerName] !== 'function') return false;
    return handlers[handlerName](msg.payload || {}, runtimeEnv) === true;
  }

  // Chat-bridge participant events: keep the DM's Chat Party list live and
  // toast on each !join. Overlays handle these separately; play.html has no
  // legacy cases for them, so consuming them here is safe for every role.
  const CHAT_PARTY_EVENT_TYPES = new Set([
    'chat_participant_joined',
    'chat_participant_left',
    'chat_participant_updated',
    'chat_participant_loot_received',
    'chat_participant_item_granted',
    'chat_participants_sync',
  ]);

  function tryHandleChatPartyMessage(msg) {
    if (!CHAT_PARTY_EVENT_TYPES.has(msg.type)) return false;
    const p = msg.payload || {};
    if (global.ROLE === 'dm' && msg.type === 'chat_participant_joined') {
      const name = p.display_name || p.twitch_username || 'A chatter';
      const notify = global.AppUINotifications;
      if (notify && typeof notify.showParchmentNotification === 'function') {
        notify.showParchmentNotification(name + ' joined from Twitch chat — see Stream ▸ Chat Party.', {
          variant: 'info',
          title: 'Chat Party',
          duration: 3500,
        });
      }
    }
    const panel = global.TwitchSettingsPanel;
    if (panel && typeof panel.notifyChatPartyEvent === 'function') {
      panel.notifyChatPartyEvent(msg.type, p);
    }
    return true;
  }

  function handleLegacyDomainMessage(msg, env) {
    if (!msg || typeof msg !== 'object') return false;
    const runtimeEnv = env || {};
    const p = msg.payload || {};
    if (tryHandleMigratedModuleMessage(msg, runtimeEnv)) return true;
    if (tryHandleChatPartyMessage(msg)) return true;
    switch (msg.type) {
      case 'journal_sync':
        if (typeof runtimeEnv.loadJournalEntries === 'function') runtimeEnv.loadJournalEntries(p.entries || []);
        return true;
      case 'party_memory_sync':
        if (typeof runtimeEnv.loadPartyMemory === 'function') runtimeEnv.loadPartyMemory(p.entries || []);
        return true;
      case 'handout_sync':
        if (typeof runtimeEnv.loadHandouts === 'function') runtimeEnv.loadHandouts(p.handouts || []);
        return true;
      case 'private_story_hooks_sync':
        if (typeof runtimeEnv.loadPrivateStoryHooks === 'function') runtimeEnv.loadPrivateStoryHooks(p.hooks || []);
        return true;
      case 'encounter_templates_sync':
        if (typeof runtimeEnv.loadEncounterTemplates === 'function') runtimeEnv.loadEncounterTemplates(p.templates || []);
        return true;
      case 'quest_template_library_sync': {
        if (typeof runtimeEnv.setQuestTemplateLibrary === 'function') {
          runtimeEnv.setQuestTemplateLibrary(Array.isArray(p.templates) ? p.templates.filter(function (entry) {
            return !!entry && typeof entry === 'object';
          }) : []);
        }
        if (typeof runtimeEnv.refreshGuildBoardIfOpen === 'function') runtimeEnv.refreshGuildBoardIfOpen();
        return true;
      }
      case 'prep_pack_library_sync': {
        if (typeof runtimeEnv.setPrepPackLibrary === 'function') {
          runtimeEnv.setPrepPackLibrary(Array.isArray(p.packs) ? p.packs.filter(function (entry) {
            return !!entry && typeof entry === 'object';
          }) : []);
        }
        if (typeof runtimeEnv.refreshGuildBoardIfOpen === 'function') runtimeEnv.refreshGuildBoardIfOpen();
        return true;
      }
      case 'session_quests_sync': {
        if (typeof runtimeEnv.loadSessionQuests === 'function') runtimeEnv.loadSessionQuests(p.session_quests || []);
        if (typeof runtimeEnv.setQuestBoardBindings === 'function' && Array.isArray(p.quest_board_bindings)) {
          runtimeEnv.setQuestBoardBindings(p.quest_board_bindings.slice());
        }
        if (typeof runtimeEnv.setPremiumQuestProgression === 'function' && p.premium_progression && typeof p.premium_progression === 'object') {
          runtimeEnv.setPremiumQuestProgression(p.premium_progression);
        }
        if (typeof runtimeEnv.refreshGuildBoardIfOpen === 'function') runtimeEnv.refreshGuildBoardIfOpen();
        return true;
      }
      default:
        return false;
    }
  }

  global.AppMessageDispatch = {
    handleIncoming,
    handleLegacyDomainMessage,
    getDispatchDiagnostics,
    isCombatMessageType,
  };
})(window);
