/**
 * message_handlers.js — env-injected message router (partially live).
 *
 * Phase 4 migration note:
 * - This file IS loaded by `client/templates/play.html`.
 * - It is the live runtime path for the verified behavior-twin handlers that
 *   `AppMessageDispatch.handleLegacyDomainMessage` delegates to (see
 *   docs/message_handler_migration_map.md for the migrated set).
 * - The remaining handlers in this file are NOT yet routed: their inline twins
 *   in play.html `handleLegacyMessage()` have drifted from these copies, so for
 *   those types the live path remains:
 *   AppWS -> AppMessageDispatch -> play.html handleLegacyMessage().
 * - Behavior-preserving only: do not "improve" or rewire a handler here without
 *   first verifying parity against its inline twin.
 */
(function(){

  function applyInventoryPayload(payload, env) {
    const p = payload || {};
    if (Object.prototype.hasOwnProperty.call(p, 'player_inventories')) env.applyPlayerInventoryState(p.player_inventories);
    if (Object.prototype.hasOwnProperty.call(p, 'player_inventory')) env.applySelfInventoryState(p.player_inventory);
    if (Object.prototype.hasOwnProperty.call(p, 'player_gold')) env.applySelfGoldState(p.player_gold);
    if (Object.prototype.hasOwnProperty.call(p, 'party_loot_log')) env.applyPartyLootLogState(p.party_loot_log);
    env.renderInventoryPanel();
    return true;
  }

  function handlePlayerInventorySync(payload, env) {
    return applyInventoryPayload(payload, env);
  }
  function handleEditorLayersSync(payload, env) {
    env.setEditorLayersAll(env.preserveDirtyEditorContextInSync('terrain', (payload && payload.layers) || {}));
    if (env.shouldReloadEditorLayerFromSync('terrain')) env.ensureEditorLayerLoaded(true);
    return true;
  }
  function handleEditorWallsSync(payload, env) {
    env.setEditorWallsAll(env.preserveDirtyEditorContextInSync('walls', (payload && payload.walls) || {}));
    if (env.shouldReloadEditorLayerFromSync('walls')) env.ensureEditorWallsLoaded(true);
    return true;
  }
  function handleEditorPropsSync(payload, env) {
    env.setEditorPropsAll(env.preserveDirtyEditorContextInSync('props', (payload && payload.props) || {}));
    if (env.shouldReloadEditorLayerFromSync('props')) env.ensureEditorPropsLoaded(true);
    if (env.getOpenPropPopupId()) {
      const idx = env.findEditorPropIndexById(env.getOpenPropPopupId());
      if (idx >= 0) env.refreshEditorPropPopup(env.getEditorPropItemAt(idx));
      else env.propPopupClose();
    }
    if (env.getOpenPropInventoryId()) env.refreshPropInventoryModal();
    return true;
  }
  function handleMapSettingsSync(payload, env) {
    env.setMapSettingsAll({ ...(((payload && payload.map_settings) || {})) });
    env.invalidateFogCache();
    env.refreshEditorWeatherControls();
    env.refreshEditorMapStyleControls();
    env.drawFrame();
    return true;
  }
  function handleEditorWorldSync(payload, env) {
    env.setEditorPathsAll({ ...(((payload && payload.paths) || {})) });
    env.setEditorLabelsAll({ ...(((payload && payload.labels) || {})) });
    env.setEditorMarkersAll({ ...(((payload && payload.markers) || {})) });
    env.setEditorLightsAll({ ...(((payload && payload.lights) || {})) });
    env.ensureEditorPathsLoaded(true);
    env.ensureEditorLabelsLoaded(true);
    env.ensureEditorMarkersLoaded(true);
    env.refreshEditorWeatherControls();
    env.refreshEditorMapStyleControls();
    env.drawFrame();
    return true;
  }
  function handleFogState(payload, env) {
    env.fogApplyState(payload || {});
    return true;
  }
  function handleFogUpdate(payload, env) {
    env.applyFogUpdate(payload || {});
    return true;
  }
  function handlePoiCreated(payload, env) {
    const poi = env.resolvePoiPayload(payload || {});
    if (!poi || !poi.id) return false;
    if (env.getRole() !== 'dm' && poi.revealed_to_players === false) {
      env.deletePoi(poi.id);
      return true;
    }
    env.setPoi(poi.id, poi);
    env.handlePendingPoiCreateArtifacts(poi.id);
    return true;
  }
  function handlePoiUpdated(payload, env) {
    const poi = env.resolvePoiPayload(payload || {});
    if (!poi || !poi.id) return false;
    if (env.getRole() !== 'dm' && poi.revealed_to_players === false) {
      env.deletePoi(poi.id);
      if (env.isPoiPopupOpenFor(poi.id)) env.poiPopupClose();
      return true;
    }
    env.setPoi(poi.id, poi);
    if (env.isPoiPopupOpenFor(poi.id)) env.openPoiPopup(poi, null, null);
    return true;
  }
  function handlePoiDeleted(payload, env) {
    const poiId = payload && payload.poi_id;
    if (!poiId) return false;
    env.deletePoi(poiId);
    if (env.isPoiPopupOpenFor(poiId)) env.poiPopupClose();
    return true;
  }
  function handleLocalMapEnter(payload, env) {
    if (env.shouldIgnoreStaleNav(payload || {})) return true;
    env.acceptNavVersion(payload || {});
    if (payload && payload.dm_map_context) env.setDmMapContext(payload.dm_map_context);
    env.enterLocalMap(payload || {});
    if (env.getRole() !== 'dm') env.showCenterNotice(`Journey: ${payload.poi_name || 'New location'}`, 'warn', 2800);
    return true;
  }
  function handleBringAllToMap(payload, env) {
    env.enterLocalMap(payload || {});
    env.showToast('The DM has brought you to ' + ((payload && payload.poi_name) || 'this location'));
    if (env.getRole() !== 'dm') env.showCenterNotice(`Journey: ${payload.poi_name || 'New location'}`, 'warn', 2800);
    return true;
  }
  function handleLocalMapExit(payload, env) {
    if (env.shouldIgnoreStaleNav(payload || {})) return true;
    env.acceptNavVersion(payload || {});
    if (payload && payload.dm_map_context !== undefined) env.setDmMapContext(payload.dm_map_context);
    env.exitLocalMap(payload || {});
    if (env.getRole() !== 'dm') {
      const ctx = env.getDmMapContext() || 'world';
      env.setFogMapContext(ctx);
      env.fogLoadMap(ctx);
      env.showCenterNotice('Journey: returned to the world map', 'warn', 2600);
    }
    return true;
  }

  function handleJournalSync(payload, env) {
    env.loadJournalEntries(payload.entries || []);
    return true;
  }
  function handleLibrarySync(payload, env) {
    env.loadLibraryEntries(payload.entries || []);
    return true;
  }
  function handleCharProfilesSync(payload, env) {
    env.loadCharProfiles(payload.char_profiles || payload.profiles || []);
    return true;
  }
  function handlePermissionGranted(payload, env) {
    const tok = env.getToken(payload.token_id);
    if (tok) {
      if (!tok.temp_permissions) tok.temp_permissions = {};
      tok.temp_permissions[payload.user_id] = Date.now() / 1000 + payload.duration;
    }
    if (payload.log) env.addLogEntry(payload.log);
    if (payload.user_id === env.getUserId()) env.showToast(`You have control of token for ${payload.duration}s!`);
    return true;
  }
  function handleViewerProfilesSync(payload, env) {
    env.setViewerProfiles({ ...(payload.viewer_profiles || {}) });
    env.renderViewerPanel();
    return true;
  }
  function handleViewerPowerCatalogSync(payload, env) {
    env.setViewerPowerCatalog({ ...(payload.viewer_power_catalog || {}) });
    env.renderViewerPanel();
    return true;
  }
  function handleHazardZonesSync(payload, env) {
    env.setHazardZones({ ...(payload.hazard_zones || {}) });
    env.renderHazardZones();
    env.drawFrame();
    return true;
  }
  function handleViewerPendingSync(payload, env) {
    env.setViewerPendingActions({ ...(payload.viewer_pending_actions || {}) });
    env.renderViewerPanel();
    return true;
  }
  function handleViewerPowerStatus(payload, env) {
    if (payload.message) env.showToast(payload.message);
    return true;
  }
  function handleErrorMessage(payload, env) {
    env.showToast('⚠ ' + (payload.message || 'Unknown error'));
    return true;
  }
  function handleItemLibrarySync(payload, env) {
    env.loadItemLibraryEntries(payload.entries || []);
    return true;
  }
  function handleChatMessage(payload, env) {
    if (payload.log) {
      env.addLogEntry({ ...payload.log, type: 'chat', role: payload.role, private: !!payload.private, target_user_name: payload.target_user_name });
    }
    if (payload.private && payload.target_user_id === env.getUserId() && payload.user_name && payload.user_name !== env.getUserName()) {
      env.showToast(`Private message from ${payload.user_name}`);
      env.showCenterNotice(`Private message from ${payload.user_name}`, 'info', 3000);
    }
    return true;
  }
  function handleUserJoined(payload, env) {
    const user = payload && payload.user;
    if (!user || !user.id) return false;
    const users = env.getUsers();
    users[user.id] = { ...user, connected: true };
    env.renderPlayerList();
    env.renderViewerPanel();
    env.renderChatTargets();
    env.updatePermSelects();
    env.renderInventoryPanel();
    return true;
  }
  function handleUserLeft(payload, env) {
    const users = env.getUsers();
    if (payload && payload.user_id && users[payload.user_id]) users[payload.user_id].connected = false;
    env.renderPlayerList();
    env.renderViewerPanel();
    env.renderChatTargets();
    env.updatePermSelects();
    env.renderInventoryPanel();
    return true;
  }
  function handleMapChanged(payload, env) {
    if (Array.isArray(payload.world_map_layers)) env.setWorldMapLayers(payload.world_map_layers);
    if (payload.map_image_url && (!Array.isArray(payload.world_map_layers) || !payload.world_map_layers.length)) {
      env.loadMapImage(payload.map_image_url, true);
    } else if (!payload.map_image_url && (!Array.isArray(payload.world_map_layers) || !payload.world_map_layers.length)) {
      env.clearMapImage();
      env.updateMapPreviewUI(null);
    }
    return true;
  }


  function handleTokensSync(payload, env) {
    env.applyAuthoritativeTokenSync(payload || {});
    return true;
  }
  function handleTokenHiddenChanged(payload, env) {
    const tok = (payload && (payload.token || payload)) || null;
    if (!tok || !tok.id) return false;
    env.removeTokenFromAllMapCaches(tok.id);
    if (tok.hidden && env.getRole() !== 'dm') {
      env.deleteLiveToken(tok.id);
      env.deleteStagingToken(tok.id);
      return true;
    }
    const myCtx = env.getCurrentMapContext();
    const tokCtx = tok.map_context || 'world';
    if (tok.staged) {
      env.upsertStagingToken(tok);
      env.deleteLiveToken(tok.id);
    } else if (tokCtx === myCtx) {
      env.upsertLiveToken(tok);
      env.deleteStagingToken(tok.id);
    } else {
      env.upsertStagingToken(tok);
      env.deleteLiveToken(tok.id);
    }
    if (!(tok.hidden && env.getRole() !== 'dm')) env.cacheTokenByContext(tok);
    env.buildStagingArea();
    if (env.getRole() === 'player' && tok.owner_id === env.getUserId()) env.syncMyCharacterFormFromToken();
    return true;
  }
  function handleTokenSentToStaging(payload, env) {
    const tok = (payload && (payload.token || payload)) || null;
    if (!tok || !tok.id) return false;
    env.removeTokenFromAllMapCaches(tok.id);
    env.deleteLiveToken(tok.id);
    env.upsertStagingToken(tok);
    env.cacheTokenByContext(tok);
    env.buildStagingArea();
    if (env.getRole() === 'player' && tok.owner_id === env.getUserId()) env.syncMyCharacterFormFromToken();
    return true;
  }
  function handleTokenRemovedHidden(payload, env) {
    const id = payload && payload.id;
    if (!id) return false;
    env.removeTokenFromAllMapCaches(id);
    env.deleteLiveToken(id);
    env.deleteStagingToken(id);
    return true;
  }
  function handleTokenDeleted(payload, env) {
    const tokenId = payload && payload.token_id;
    if (!tokenId) return false;
    env.removeTokenFromAllMapCaches(tokenId);
    env.deleteLiveToken(tokenId);
    env.deleteStagingToken(tokenId);
    if (payload.log) env.addLogEntry(payload.log);
    env.updatePermSelects();
    return true;
  }


  function handleTokenCreated(payload, env) {
    const newTok = payload && payload.token;
    if (!newTok || !newTok.id) return false;
    env.cacheTokenByContext(newTok);
    const myCtx = env.getCurrentMapContext();
    const tokCtx = newTok.map_context || 'world';

    env.removeStagingTokensForOwner(newTok.owner_id);

    if (newTok.staged) {
      env.upsertStagingToken(newTok);
    } else if (tokCtx === myCtx) {
      if (newTok.maxHp && newTok.hp === undefined) newTok.hp = newTok.maxHp;
      env.upsertLiveToken(newTok);
    } else if (newTok.owner_id) {
      env.upsertStagingToken(newTok);
    }

    env.buildStagingArea();
    if (env.hasPendingTokenImageCreate()) {
      if (payload.client_nonce && payload.client_nonce === env.getPendingTokenImageCreateNonce()) {
        const pending = env.takePendingTokenImageCreate();
        env.uploadTokenImageFile(newTok.id, pending.file)
          .then(() => env.showToast('Token image uploaded.'))
          .catch(err => env.showToast((err && err.message) || 'Token image upload failed.'));
      } else {
        env.deferPendingTokenImageCreateFallback('token_created');
      }
    }
    if (env.getRole() === 'player' && newTok.owner_id === env.getUserId()) env.syncMyCharacterFormFromToken(false);
    if (payload.log) env.addLogEntry(payload.log);
    env.updatePermSelects();
    return true;
  }
  function handleTokenPlaced(payload, env) {
    const tokenId = payload && payload.token_id;
    if (!tokenId) return false;
    env.removeTokenFromAllMapCaches(tokenId);
    const myCtx = env.getCurrentMapContext();
    const placedTok = (payload && payload.token) || env.getStagingToken(tokenId) || env.getLiveToken(tokenId);
    if (!placedTok) return true;
    placedTok.x = payload.x;
    placedTok.y = payload.y;
    placedTok.map_context = payload.map_context || placedTok.map_context || 'world';
    placedTok.staged = false;
    env.deleteStagingToken(tokenId);
    if ((placedTok.map_context || 'world') === myCtx) {
      env.upsertLiveToken(placedTok);
    } else if (placedTok.owner_id) {
      env.upsertStagingToken(placedTok);
      env.deleteLiveToken(tokenId);
    }
    env.cacheTokenByContext(placedTok);
    env.buildStagingArea();
    if (env.getRole() === 'player' && placedTok.owner_id === env.getUserId()) env.syncMyCharacterFormFromToken(true);
    return true;
  }
  function handleTokenMoved(payload, env) {
    const tokenId = payload && payload.token_id;
    if (!tokenId) return false;
    env.clearPendingMoveConfirmForToken(tokenId);
    const liveTok = env.getLiveToken(tokenId);
    if (liveTok) {
      liveTok.x = payload.x;
      liveTok.y = payload.y;
      env.cacheTokenByContext(liveTok);
      return true;
    }
    const stagingTok = env.getStagingToken(tokenId);
    if (stagingTok) {
      stagingTok.x = payload.x;
      stagingTok.y = payload.y;
      env.cacheTokenByContext(stagingTok);
    }
    return true;
  }
  function handleTokenMoveDenied(payload, env) {
    const tokenId = payload && payload.token_id;
    if (!tokenId) return false;
    env.clearPendingMoveConfirmForToken(tokenId);
    const liveTok = env.getLiveToken(tokenId);
    if (liveTok) {
      if (Number.isFinite(Number(payload.x))) liveTok.x = Number(payload.x);
      if (Number.isFinite(Number(payload.y))) liveTok.y = Number(payload.y);
      env.cacheTokenByContext(liveTok);
    }
    if (payload.movement) {
      env.setCombatMovement(payload.movement);
      env.renderCombat();
    }
    if (payload.message) env.showToast(payload.message);
    return true;
  }
  function handleTokenHpUpdated(payload, env) {
    const tokenId = payload && payload.token_id;
    if (!tokenId) return false;
    const updTok = env.getLiveToken(tokenId) || env.getStagingToken(tokenId);
    if (updTok) {
      updTok.hp = payload.hp;
      updTok.maxHp = payload.maxHp;
      if (payload.tempHp !== undefined) updTok.tempHp = payload.tempHp;
      env.cacheTokenByContext(updTok);
      if (env.getHpPopupTokenId() === tokenId) env.updateHpPopupDisplay(updTok);
      if (env.getRole() === 'player' && updTok.owner_id === env.getUserId()) env.syncMyCharacterFormFromToken(false);
    }
    if (payload.log) env.addLogEntry({ ...payload.log, type: 'system' });
    return true;
  }
  function handleTokenEdited(payload, env) {
    const tokenId = payload && payload.id;
    if (!tokenId) return false;
    const ed = env.getLiveToken(tokenId) || env.getStagingToken(tokenId);
    if (!ed) return true;
    Object.assign(ed, payload);
    env.cacheTokenByContext(ed);
    if (env.getRole() === 'player' && ed.owner_id === env.getUserId()) env.syncMyCharacterFormFromToken(false);
    return true;
  }

  function handleCombatMoveState(payload, env) {
    env.setCombatMovement(payload && typeof payload === 'object' ? payload : null);
    env.renderCombat();
    return true;
  }
  function handleTokenConditionChanged(payload, env) {
    const tc = payload || {};
    const tok = env.getToken(tc.token_id);
    if (tok) {
      tok.conditions = tc.conditions || [];
      tok.condition_timers = tc.condition_timers || {};
      env.cacheTokenByContext(tok);
    }
    env.refreshConditionPopupIfOpen(tc.token_id, tc.conditions || [], tc.condition_timers || {});
    env.refreshConditionSummaries();
    return true;
  }

  function handleCombatAttackResult(payload, env) {
    if (payload && payload.log) env.addLogEntry(payload.log);
    env.showCombatAttackResult(payload || {});
    return true;
  }
  function handleCombatState(payload, env) {
    env.combatApplyState(payload);
    return true;
  }

  function handleMapPing(payload, env) {
    env.addPing(payload.x, payload.y, payload.color || '#f1c40f');
    return true;
  }
  function handleRulerShown(payload, env) {
    env.showRemoteRuler(payload);
    return true;
  }
  function handleTokenImageUpdated(payload, env) {
    const tok = env.getToken(payload.token_id);
    if (!tok) return true;
    tok.image_url = payload.image_url || null;
    if ('imageFit' in payload || 'image_fit' in payload) tok.imageFit = payload.imageFit || payload.image_fit || 'cover';
    if ('imageZoom' in payload || 'image_zoom' in payload) tok.imageZoom = Number(payload.imageZoom ?? payload.image_zoom ?? 1) || 1;
    if ('imageOffsetX' in payload || 'image_offset_x' in payload) tok.imageOffsetX = Number(payload.imageOffsetX ?? payload.image_offset_x ?? 0) || 0;
    if ('imageOffsetY' in payload || 'image_offset_y' in payload) tok.imageOffsetY = Number(payload.imageOffsetY ?? payload.image_offset_y ?? 0) || 0;
    if (payload.image_url) env.preloadTokenImage(payload.image_url);
    env.drawFrame();
    return true;
  }
  function handleSpellMarkerSaved(payload, env) {
    env.setCtxSpellMarker({ ...payload });
    env.drawFrame();
    return true;
  }
  function handleSpellMarkerCleared(_payload, env) {
    env.setCtxSpellMarker(null);
    env.drawFrame();
    return true;
  }

  function handlePropActionResult(payload, env) {
    if (payload && payload.message) env.showToast(payload.message);
    if (env.getOpenPropInventoryId()) env.refreshPropInventoryModal();
    return true;
  }
  function handleInventoryActionResult(payload, env) {
    if (payload && payload.message) env.showToast(payload.message);
    env.renderInventoryPanel();
    return true;
  }
  function handleSpellMarkerAdd(payload, env) {
    const markers = env.getSpellMarkers();
    if (!markers.find(m => m && m.id === payload.id)) markers.push({ ...payload });
    env.setSpellMarkers(markers);
    return true;
  }
  function handleSpellMarkerRemove(payload, env) {
    env.setSpellMarkers(env.getSpellMarkers().filter(m => !m || m.id !== payload.id));
    return true;
  }
  function handleSpellMarkerClear(_payload, env) {
    env.setSpellMarkers([]);
    return true;
  }
  function handleCharHpUpdate(payload, env) {
    const targetToken = env.findTokenByOwnerId(payload && payload.user_id);
    if (targetToken) {
      targetToken.hp = payload.hp;
      targetToken.maxHp = payload.maxHp;
      env.cacheTokenByContext(targetToken);
    }
    env.addLogEntry({
      id: 'hp_' + Date.now(),
      type: 'system',
      user: payload.name,
      message: `HP: ${payload.hp} / ${payload.maxHp}`,
      timestamp: Date.now() / 1000,
    });
    if (env.getRole() === 'dm') env.addDMNotif(`${payload.name}: ${payload.hp}/${payload.maxHp} HP`);
    return true;
  }
  function handleDiceResult(payload, env) {
    if (payload.log) env.addLogEntry({ ...payload.log, type: 'dice' });
    if (payload.user_id === env.getUserId()) {
      const meta = {
        rollId: payload.roll_id || '',
        seed: payload.seed,
        modifierMeta: payload.modifier_meta || null,
        mode: payload.mode || null,
      };
      if (typeof env.appDiceSyncAuthoritativePayload === 'function') {
        env.appDiceSyncAuthoritativePayload({ ...payload, ...meta });
      } else if (typeof globalThis.appDiceSyncAuthoritativePayload === 'function') {
        globalThis.appDiceSyncAuthoritativePayload({ ...payload, ...meta });
      } else if (typeof env.appDiceSyncResult === 'function') {
        env.appDiceSyncResult(
          payload.rolls,
          payload.total,
          payload.dice_type,
          payload.quantity,
          (payload.modifier ?? payload.init_bonus ?? 0),
          (payload.roll_label || ''),
          meta,
        );
      }
    }
    return true;
  }
  function handleViewerFx(payload, env) {
    env.showViewerFx(payload);
    return true;
  }
  function handleStateSync(payload, env) {
    const p = payload || {};
    // Delta reconnect syncs omit unchanged heavy domains (their payload keys
    // are absent) — mirror play.html's inline handler and only clear/apply a
    // domain when its keys are present. Full syncs always carry every key.
    const syncHasTokens = ('tokens' in p);
    const syncHasCharProfiles = ('char_profiles' in p);
    const syncHasEditor = ('editor_layers' in p) || ('editor_walls' in p) || ('editor_props' in p);
    if (p.domain_revisions && typeof p.domain_revisions === 'object') {
      window.__stateDomainRevisions = { ...p.domain_revisions };
    }
    env.clearStateSyncCollections();
    env.setWorldMapImageUrl(p.map_image_url || null);
    env.setWorldMapLayers(p.world_map_layers || []);

    const syncCtx = (p.dm_map_context && p.dm_map_context !== 'world') ? p.dm_map_context : 'world';
    let stagingCount = 0;
    Object.values((syncHasTokens && p.tokens) || {}).forEach(t => {
      if (t.maxHp && t.hp === undefined) t.hp = t.maxHp;
      const tCtx = t.map_context || 'world';
      env.cacheTokenByContext(t);
      const shouldStage = !!t.staged || (!!t.owner_id && tCtx !== syncCtx);
      if (shouldStage) {
        env.upsertStagingToken(t);
        stagingCount += 1;
      } else if (tCtx === syncCtx) {
        env.upsertLiveToken(t);
      }
    });
    if (env.getRole() === 'dm' && stagingCount > 0) {
      setTimeout(() => env.showToast(`${stagingCount} token${stagingCount > 1 ? 's' : ''} in staging from other maps`), 800);
    }
    const users = env.getUsers();
    Object.values(p.users || {}).forEach(u => { users[u.id] = u; });

    env.renderLogFeed(p.log || [], users);
    env.renderPlayerList();
    env.renderViewerPanel();
    env.renderChatTargets();
    env.updatePermSelects();

    env.loadJournalEntries(p.journal_entries || []);
    env.loadLibraryEntries(p.library_entries || []);
    env.loadItemLibraryEntries(p.item_library_entries || []);
    if (syncHasCharProfiles) env.loadCharProfiles(p.char_profiles || []);

    if (syncHasEditor) {
      env.setEditorLayersAll(env.preserveDirtyEditorContextInSync('terrain', p.editor_layers || {}));
      if (env.shouldReloadEditorLayerFromSync('terrain')) env.ensureEditorLayerLoaded(true);
      env.setEditorWallsAll(env.preserveDirtyEditorContextInSync('walls', p.editor_walls || {}));
      if (env.shouldReloadEditorLayerFromSync('walls')) env.ensureEditorWallsLoaded(true);
      env.setEditorPropsAll(env.preserveDirtyEditorContextInSync('props', p.editor_props || {}));
      if (env.shouldReloadEditorLayerFromSync('props')) env.ensureEditorPropsLoaded(true);
      env.setMapSettingsAll({ ...(p.map_settings || {}) });
      env.setEditorPathsAll({ ...(p.editor_paths || {}) });
      env.setEditorLabelsAll({ ...(p.editor_labels || {}) });
      env.setEditorMarkersAll({ ...(p.editor_markers || {}) });
      env.setEditorLightsAll({ ...(p.editor_lights || {}) });
    }
    env.setViewerProfiles({ ...(p.viewer_profiles || {}) });
    env.setViewerPendingActions({ ...(p.viewer_pending_actions || {}) });
    env.setViewerPowerCatalog({ ...(p.viewer_power_catalog || {}) });
    env.setHazardZones({ ...(p.hazard_zones || {}) });
    if (syncHasEditor) {
      env.ensureEditorPathsLoaded(true);
      env.ensureEditorLabelsLoaded(true);
      env.ensureEditorMarkersLoaded(true);
    }
    env.renderHazardZones();

    env.setPoisAll(p.pois || {});

    const dmCtx = p.dm_map_context;
    const localUrl = p.dm_current_map_url;
    const onLocal = dmCtx && dmCtx !== 'world';
    const syncNavVersion = Number(p.map_nav_version || 0);
    const serverNavIntent = Number(p.dm_nav_intent || 0);
    if (syncNavVersion > env.getMapNavVersion()) env.setMapNavVersion(syncNavVersion);

    if (!env.shouldTrustServerNav(serverNavIntent)) {
      setTimeout(env.resyncDmMapNav, 0);
    } else {
      if (dmCtx !== undefined) env.setDmMapContext(dmCtx);
      if (onLocal) {
        const dmPoi = env.findPoiById(dmCtx);
        const poiName = dmPoi ? dmPoi.name : 'Local Map';
        const mapUrl = localUrl || (dmPoi && dmPoi.local_map_url);
        if (mapUrl) {
          env.handleLocalMapEnter({ poi_id: dmCtx, poi_name: poiName, map_url: mapUrl });
        } else if (env.getWorldMapImageUrl()) {
          env.loadMapImage(env.getWorldMapImageUrl(), true);
        }
      } else {
        env.handleLocalMapExit();
        if (env.getWorldMapImageUrl()) env.loadMapImage(env.getWorldMapImageUrl(), true);
      }
    }

    if (syncHasTokens) env.applyAuthoritativeTokenSync({ tokens: p.tokens || {} });
    if (p.fog_maps !== undefined) env.fogApplyState(p);
    if (p.combat !== undefined) env.combatApplyState(p.combat);
    env.drawFrame();
    applyInventoryPayload(p, env);

    if (env.getRole() === 'player') env.syncMyCharacterFormFromToken(true);
    if (env.isReturningPlayer()) {
      const myTokens = env.findTokensByOwnerId(env.getUserId());
      if (myTokens.length > 0) {
        env.showToast('Welcome back, ' + env.getDisplayName() + '! Your token is ready.');
        const t = myTokens[0];
        env.centerCameraOnToken(t);
      } else {
        env.showToast('Welcome back, ' + env.getDisplayName() + '!');
      }
    }
    return true;
  }
  window.AppMessageHandlers = { handleStateSync, handlePlayerInventorySync, handleEditorLayersSync, handleEditorWallsSync, handleEditorPropsSync, handleMapSettingsSync, handleEditorWorldSync, handleFogState, handleFogUpdate, handlePoiCreated, handlePoiUpdated, handlePoiDeleted, handleLocalMapEnter, handleBringAllToMap, handleLocalMapExit, handleJournalSync, handleLibrarySync, handleCharProfilesSync, handlePermissionGranted, handleViewerProfilesSync, handleViewerPowerCatalogSync, handleHazardZonesSync, handleViewerPendingSync, handleViewerPowerStatus, handleErrorMessage, handleItemLibrarySync, handleChatMessage, handleUserJoined, handleUserLeft, handleMapChanged, handleTokensSync, handleTokenHiddenChanged, handleTokenSentToStaging, handleTokenRemovedHidden, handleTokenDeleted, handleTokenCreated, handleTokenPlaced, handleTokenMoved, handleTokenMoveDenied, handleCombatMoveState, handleTokenConditionChanged, handleCombatAttackResult, handleCombatState, handleTokenHpUpdated, handleTokenEdited, handleMapPing, handleRulerShown, handleTokenImageUpdated, handleSpellMarkerSaved, handleSpellMarkerCleared, handlePropActionResult, handleInventoryActionResult, handleSpellMarkerAdd, handleSpellMarkerRemove, handleSpellMarkerClear, handleCharHpUpdate, handleDiceResult, handleViewerFx };
})();
