(function (global) {
  'use strict';

  const VALID_SHEET_MODES = new Set(['closed', 'live_play_sheet', 'edit_sheet', 'import_review', 'level_up']);

  function normalizeSheetMode(mode) {
    const value = String(mode || '').trim();
    return VALID_SHEET_MODES.has(value) ? value : 'closed';
  }

  function modeForCharacterBookPage(page) {
    const value = String(page || 'premiumsheet').trim() || 'premiumsheet';
    if (value === 'premiumsheet') return 'live_play_sheet';
    if (value === 'import') return 'import_review';
    if (value === 'levelup') return 'level_up';
    return 'edit_sheet';
  }

  function setRootInactive(root, inactive, activeDisplay = '') {
    if (!root) return;
    const isInactive = !!inactive;
    root.setAttribute('aria-hidden', isInactive ? 'true' : 'false');
    if ('inert' in root) root.inert = isInactive;
    root.hidden = isInactive;
    root.classList.toggle('sheet-root-inactive', isInactive);
    root.classList.remove('active', 'open');
    if (!isInactive) root.classList.add('active');
    root.style.display = isInactive ? 'none' : activeDisplay;
    root.style.visibility = isInactive ? 'hidden' : 'visible';
    root.style.pointerEvents = isInactive ? 'none' : 'auto';
  }

  function closeAllCharacterSheetSurfaces(env, nextMode = 'closed') {
    const mode = normalizeSheetMode(nextMode);
    const doc = env.document;
    const panel = doc.getElementById('char-sheet-panel');
    const premiumRoot = doc.getElementById('cs-premium-mount');
    const oldRoots = [doc.getElementById('sheet-body')].filter(Boolean);
    const sheetRoots = [premiumRoot].concat(oldRoots).filter(Boolean);
    const pages = Array.from(doc.querySelectorAll('#char-book-pages .sheet-page'));
    const activePage = mode === 'live_play_sheet' ? 'premiumsheet'
      : mode === 'import_review' ? 'import'
        : mode === 'level_up' ? 'levelup'
          : mode === 'edit_sheet' ? (env.getActiveCharBookPage && env.getActiveCharBookPage()) || 'identity'
            : '';

    doc.querySelectorAll('.character-sheet-modal.active').forEach(node => {
      if (node !== panel) setRootInactive(node, true);
    });
    doc.querySelectorAll('#char-sheet-panel.open, #char-sheet-panel.active').forEach((node, idx) => {
      if (node !== panel || idx > 0) setRootInactive(node, true);
    });
    doc.querySelectorAll('.character-sheet-backdrop, .char-sheet-backdrop, .sheet-modal-backdrop').forEach(node => node.remove());
    sheetRoots.forEach(root => setRootInactive(root, true));

    if (panel) {
      panel.dataset.sheetMode = mode;
      setRootInactive(panel, mode === 'closed', 'flex');
      panel.classList.toggle('open', mode !== 'closed');
      panel.classList.toggle('active', mode !== 'closed');
      panel.style.zIndex = mode === 'closed' ? '' : '15600';
    }

    pages.forEach(pageEl => {
      const isActive = mode !== 'closed' && pageEl.dataset.page === activePage;
      setRootInactive(pageEl, !isActive, 'flex');
      pageEl.classList.toggle('active', isActive);
    });
    setRootInactive(premiumRoot, mode !== 'live_play_sheet', 'block');
    oldRoots.forEach(root => setRootInactive(root, true));
    if (env.setCharacterSheetMode) env.setCharacterSheetMode(mode);
  }

  function closeCharacterBook(env) {
    closeAllCharacterSheetSurfaces(env, 'closed');
  }

  function updateCharacterBookModeRibbon(env, page) {
    const ribbon = env.document.getElementById('char-book-mode-ribbon');
    if (!ribbon) return;
    const labels = {
      premiumsheet: ['Live Play mode', 'The Sheet ✦ dashboard keeps Actions, Spells, Inventory, Features, and Notes easy to find during the session.'],
      import: ['Import Review mode', 'Review pasted, JSON, or PDF import data here before trusting AC, HP, spells, or equipment.'],
      levelup: ['Level Up mode', 'Use this progression flow after the live sheet and source data look correct.'],
      identity: ['Build/Edit mode', 'Edit notes and identity source data here; the live player dashboard remains on Sheet ✦.'],
      abilities: ['Build/Edit mode', 'Confirm vitals, proficiency, passive perception, and ability scores here.'],
      skills: ['Build/Edit mode', 'Confirm saves and skill math here.'],
      combat: ['Build/Edit mode', 'Review legacy action source data here; use the Sheet ✦ Actions tab during live play.'],
      inventory: ['Build/Edit mode', 'Review imported carried gear here; use the Sheet ✦ Inventory tab during live play.'],
      spelllibrary: ['Build/Edit mode', 'Manage spell library details here; use the Sheet ✦ Spells tab during live play.'],
    };
    const entry = labels[page] || labels.premiumsheet;
    ribbon.innerHTML = '<strong>' + entry[0] + '</strong><span>' + entry[1] + '</span>';
  }

  function updateCharacterBookTabs(env, page) {
    env.document.querySelectorAll('.sheet-page-tab').forEach(btn => btn.classList.toggle('active', btn.dataset.page === page));
    env.document.querySelectorAll('.sheet-workflow-btn').forEach(btn => {
      const target = String(btn.dataset.page || '').trim();
      btn.classList.toggle('active', target === page);
    });
    updateCharacterBookModeRibbon(env, page);
  }

  function goCharacterBookPage(env, page, instant = false) {
    const strip = env.document.getElementById('char-book-pages');
    page = String(page || 'premiumsheet').trim() || 'premiumsheet';
    const pageEl = strip?.querySelector(`.sheet-page[data-page="${page}"]`);
    if (!strip || !pageEl) return;
    env.setActiveCharBookPage(page);
    pageEl.scrollIntoView({ behavior: instant ? 'auto' : 'smooth', block: 'nearest', inline: 'start' });
    updateCharacterBookTabs(env, page);
    closeAllCharacterSheetSurfaces(env, modeForCharacterBookPage(page));
  }

  function handleCharacterBookScroll(env) {
    const strip = env.document.getElementById('char-book-pages');
    if (!strip) return;
    const pageOrder = env.getPageOrder() || [];
    const idx = Math.round(strip.scrollLeft / Math.max(1, strip.clientWidth));
    const page = pageOrder[Math.min(pageOrder.length - 1, Math.max(0, idx))] || 'premiumsheet';
    if (page !== env.getActiveCharBookPage()) {
      env.setActiveCharBookPage(page);
      updateCharacterBookTabs(env, page);
    }
  }

  function openCharacterBook(env, page = 'premiumsheet') {
    closeAllCharacterSheetSurfaces(env, 'closed');
    env.initCharacterBook();
    const charSheet = env.getCharSheet();
    if (charSheet && (Object.keys(charSheet.book || {}).length || (charSheet.name && charSheet.name !== 'Unknown Hero') || (charSheet.classes && charSheet.classes.length))) {
      env.applyCharSheetToBook(charSheet);
    } else {
      env.seedCharacterBookFromCurrentState();
    }
    env.syncCharSheetFromBookData(env.getCharacterBookDataFromUI());
    const panel = env.document.getElementById('char-sheet-panel');
    if (panel && panel.parentElement !== env.document.body) env.document.body.appendChild(panel);
    closeAllCharacterSheetSurfaces(env, modeForCharacterBookPage(page));
    env.setCharacterBookSaveState('saved');
    goCharacterBookPage(env, page, true);
    try {
      env.ensureCharSheetRuntimeDefaults();
      env.requestCharacterBookOverviewRender('openCharacterBook');
      env.renderImportedSpellbookPreview();
      env.renderRulesSpellbook();
      env.updateSpellRulesButtons();
    } catch (err) {
      env.console.error('Character Book open failed', err);
      env.showToast('Character Book opened with a sheet warning.');
    }
  }

  function openCharacterLevelupPlanner(env) {
    if (env.getRole() !== 'player') return;
    closeAllCharacterSheetSurfaces(env, 'closed');
    if (!(global.CharacterLevelupModal && typeof global.CharacterLevelupModal.open === 'function')) {
      env.showToast('Level Up Planner is still loading. Try again in a moment.');
      return;
    }

    const profileId = env.resolveActiveCharProfileId();
    const charProfiles = env.getCharProfiles();
    const selectedProfile = charProfiles.find(p => String(p?.id || '') === String(profileId || '')) || null;
    const activeDoc = env.getActiveNativeCharacterDocument();
    if (!activeDoc && selectedProfile && selectedProfile.stub && typeof global.ensureCharProfileBody === 'function') {
      // Sync only shipped a profile stub; fetch the body, then re-open.
      env.showToast('Loading character…');
      global.ensureCharProfileBody(profileId, function (full) {
        if (full) openCharacterLevelupPlanner(env);
      });
      return;
    }
    const characterDocument = (activeDoc && typeof activeDoc === 'object')
      ? JSON.parse(JSON.stringify(activeDoc))
      : (selectedProfile && !selectedProfile.stub && selectedProfile.nativeCharacter && typeof selectedProfile.nativeCharacter === 'object'
        ? JSON.parse(JSON.stringify(selectedProfile.nativeCharacter))
        : null);

    if (!(characterDocument && typeof characterDocument === 'object' && Object.keys(characterDocument).length)) {
      env.showToast('Level Up Planner requires an imported character. Use the Import tab to import from D&D Beyond (PDF or JSON) first.');
      // Navigate to import tab so the player knows what to do
      goCharacterBookPage(env, 'import');
      return;
    }

    global.CharacterLevelupModal.open({
      sessionId: env.getSessionId(),
      profile: selectedProfile || { id: profileId },
      characterDocument,
      onApplied: (data) => {
        const profilePayload = data?.profile && typeof data.profile === 'object' ? data.profile : null;
        const resolvedProfileId = String((profilePayload && profilePayload.id) || profileId || '').trim();
        const existingProfile = charProfiles.find(p => String(p?.id || '') === resolvedProfileId) || null;

        if (data?.nativeCharacter && typeof data.nativeCharacter === 'object') {
          env.setActiveNativeCharacterDocument(JSON.parse(JSON.stringify(data.nativeCharacter)));
        }
        if (data?.nativeRuntime && typeof data.nativeRuntime === 'object') {
          env.setActiveNativeCharacterRuntime(JSON.parse(JSON.stringify(data.nativeRuntime)));
        }

        let nextProfile = null;
        if (profilePayload && (profilePayload.charBook || profilePayload.charSheet || profilePayload.nativeCharacter)) {
          nextProfile = JSON.parse(JSON.stringify(profilePayload));
        } else if (existingProfile && typeof existingProfile === 'object') {
          nextProfile = JSON.parse(JSON.stringify(existingProfile));
        } else {
          nextProfile = env.collectCurrentCharProfile({ profileId: resolvedProfileId || profileId });
        }

        if (nextProfile && data?.nativeCharacter && typeof data.nativeCharacter === 'object') {
          nextProfile.nativeCharacter = JSON.parse(JSON.stringify(data.nativeCharacter));
        }
        if (nextProfile && data?.nativeRuntime && typeof data.nativeRuntime === 'object') {
          nextProfile.nativeRuntime = JSON.parse(JSON.stringify(data.nativeRuntime));
        }
        if (nextProfile && !nextProfile.id) nextProfile.id = resolvedProfileId || profileId;

        if (nextProfile) {
          nextProfile.sourceMode = String(nextProfile?.nativeCharacter?.sourceMode || nextProfile.sourceMode || 'native').trim() || 'native';
          const mapped = env.mapProfileToPlay(nextProfile);
          const saveId = String(mapped?.id || nextProfile.id || profileId || '').trim();
          env.upsertCharProfile(saveId, mapped);
          env.refreshCharProfileSelect();
          env.applyCharProfileRecord(mapped, { silent: true });
          try { if (typeof global.updateMyChar === 'function') global.updateMyChar(); } catch (_) {}
          try { if (typeof env.renderImportedSpellbookPreview === 'function') env.renderImportedSpellbookPreview(); } catch (_) {}
          try { if (typeof env.renderRulesSpellbook === 'function') env.renderRulesSpellbook(); } catch (_) {}
          try { if (typeof env.updateSpellRulesButtons === 'function') env.updateSpellRulesButtons(); } catch (_) {}
          try { if (typeof env.requestCharacterBookOverviewRender === 'function') env.requestCharacterBookOverviewRender('levelup-applied'); } catch (_) {}
          try { if (typeof global.renderCombat === 'function') global.renderCombat(); } catch (_) {}
          try { if (typeof global.renderPartyStatusPanel === 'function') global.renderPartyStatusPanel(); } catch (_) {}
          try { setTimeout(function () { if (typeof global.updateMyChar === 'function') global.updateMyChar(); }, 0); } catch (_) {}
          try { setTimeout(function () { if (typeof env.requestCharacterBookOverviewRender === 'function') env.requestCharacterBookOverviewRender('levelup-applied'); }, 50); } catch (_) {}
        }

        env.showToast('Level up applied and profile saved.');
      },
    });
  }

  global.AppUICharacterBook = {
    closeAllCharacterSheetSurfaces,
    cleanupCharacterSheetSurfaces: closeAllCharacterSheetSurfaces,
    closeCharacterBook,
    openCharacterBook,
    goCharacterBookPage,
    handleCharacterBookScroll,
    updateCharacterBookTabs,
    openCharacterLevelupPlanner,
  };
})(window);
