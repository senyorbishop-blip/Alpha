(function initCharacterLibraryPanel(global) {
  function safeInt(value, fallback) {
    const parsed = parseInt(value, 10);
    if (Number.isFinite(parsed)) return parsed;
    return fallback;
  }

  function resolveLevel(entry) {
    if (!entry || typeof entry !== 'object') return null;
    if (entry.level != null) return safeInt(entry.level, 1);
    const charBook = entry.charBook && typeof entry.charBook === 'object' ? entry.charBook : {};
    if (charBook.level != null) return safeInt(charBook.level, 1);
    const charSheet = entry.charSheet && typeof entry.charSheet === 'object' ? entry.charSheet : {};
    if (charSheet.totalLevel != null) return safeInt(charSheet.totalLevel, 1);
    if (charSheet.level != null) return safeInt(charSheet.level, 1);
    const classes = Array.isArray(charSheet.classes) ? charSheet.classes : [];
    if (!classes.length) return null;
    let total = 0;
    let seen = false;
    classes.forEach((cls) => {
      if (!cls || typeof cls !== 'object' || cls.level == null) return;
      seen = true;
      total += safeInt(cls.level, 0);
    });
    return seen ? total : null;
  }

  function resolveClassSummary(entry) {
    if (!entry || typeof entry !== 'object') return '';
    const direct = String(entry.classSummary || '').trim();
    if (direct) return direct;
    const charBook = entry.charBook && typeof entry.charBook === 'object' ? entry.charBook : {};
    const className = String(charBook.className || '').trim();
    const subclass = String(charBook.subclass || '').trim();
    if (className && subclass) return className + ' (' + subclass + ')';
    if (className) return className;
    const charSheet = entry.charSheet && typeof entry.charSheet === 'object' ? entry.charSheet : {};
    const classes = Array.isArray(charSheet.classes) ? charSheet.classes : [];
    return classes.map((cls) => (cls && typeof cls === 'object' ? String(cls.name || '').trim() : '')).filter(Boolean).join(' / ');
  }

  function resolveSourceMode(entry) {
    if (!entry || typeof entry !== 'object') return 'legacy';
    const direct = String(entry.sourceMode || '').trim().toLowerCase();
    if (direct) return direct;
    const native = entry.nativeCharacter && typeof entry.nativeCharacter === 'object' ? entry.nativeCharacter : {};
    const nativeMode = String(native.sourceMode || '').trim().toLowerCase();
    if (nativeMode) return nativeMode;
    const importMeta = entry.importMeta && typeof entry.importMeta === 'object' ? entry.importMeta : {};
    const source = String(importMeta.source || '').trim().toLowerCase();
    const origin = String(importMeta.origin || '').trim().toLowerCase();
    const nativeImportMode = String(importMeta.nativeImportMode || '').trim().toLowerCase();
    if (source.includes('pdf') || origin.includes('pdf') || nativeImportMode.includes('pdf')) return 'ddb_pdf';
    if (source.includes('ddb') || source.includes('d&d beyond') || origin.includes('dndbeyond')) return 'ddb';
    if (source) return source;
    return 'legacy';
  }

  function resolveExternalSource(entry, native) {
    const nativeMeta = native && native.importMeta && typeof native.importMeta === 'object' ? native.importMeta : {};
    const entryMeta = entry && entry.importMeta && typeof entry.importMeta === 'object' ? entry.importMeta : {};
    const external = nativeMeta.externalSource && typeof nativeMeta.externalSource === 'object'
      ? nativeMeta.externalSource
      : (entryMeta.externalSource && typeof entryMeta.externalSource === 'object' ? entryMeta.externalSource : null);
    if (!external) return null;
    const provider = String(external.provider || '').trim().toLowerCase();
    const characterId = String(external.characterId || '').trim();
    if (provider !== 'dndbeyond' || external.linked === false || !characterId) return null;
    return Object.assign({}, external, {
      provider,
      characterId,
      sourceUrl: String(external.sourceUrl || ('https://www.dndbeyond.com/characters/' + characterId)).trim(),
    });
  }

  function formatSourceBadge(mode, externalSource) {
    if (externalSource && externalSource.provider === 'dndbeyond') return 'D&D Beyond • Linked';
    const key = String(mode || 'legacy').toLowerCase();
    if (key === 'native') return 'Casual D&D';
    if (key === 'ddb_pdf' || key === 'dndbeyond_pdf' || key === 'pdf') return 'D&D Beyond PDF';
    if (key === 'ddb' || key === 'dndbeyond' || key === 'dndbeyond_json' || key === 'json') return 'D&D Beyond';
    if (key === 'legacy') return 'Legacy';
    return key;
  }

  function normalizeCharacterEntries(entries) {
    const list = Array.isArray(entries) ? entries : [];
    return list.map((entry, index) => {
      if (!entry || typeof entry !== 'object') return null;
      const id = String(entry.id || 'library-' + (index + 1)).trim();
      if (!id) return null;
      const sourceMode = resolveSourceMode(entry);
      const native = entry.nativeCharacter && typeof entry.nativeCharacter === 'object' ? entry.nativeCharacter : {};
      const externalSource = resolveExternalSource(entry, native);
      const identity = native.identity && typeof native.identity === 'object' ? native.identity : {};
      const species = native.species && typeof native.species === 'object' ? native.species : {};
      const classes = Array.isArray(native.classes) ? native.classes : [];
      const firstClass = classes[0] && typeof classes[0] === 'object' ? classes[0] : {};
      const classData = native.class && typeof native.class === 'object' ? native.class : {};
      const portraitLibrary = global.CasualDnDPortraitLibrary;
      const portraitUrl = String(identity.portraitUrl || entry.portraitUrl || entry.avatarUrl || '').trim();
      const tokenUrl = String(identity.tokenImageUrl || entry.tokenImageUrl || '').trim();
      const resolvedComboPortrait = (!portraitUrl && !tokenUrl && portraitLibrary && typeof portraitLibrary.resolve === 'function')
        ? String(portraitLibrary.resolve({ speciesId: String(species.id || species.name || '').trim(), classId: String(firstClass.classId || classData.id || firstClass.name || '').trim(), gender: String(identity.gender || '').trim() || 'neutral', neutralFallback: '' }) || '').trim()
        : '';
      return {
        id: 'library:' + id,
        libraryId: id,
        kind: 'library-profile',
        selectable: true,
        canUseExisting: true,
        claimTokenId: 'profile:' + id,
        name: String(entry.name || 'Unnamed Character').trim() || 'Unnamed Character',
        classSummary: resolveClassSummary(entry),
        level: resolveLevel(entry),
        sourceMode,
        sourceBadge: formatSourceBadge(sourceMode, externalSource),
        ownerLabel: 'Saved in your profile library',
        nativeCharacter: native || null,
        externalSource,
        isDdbLinked: Boolean(externalSource),
        lastSyncedAt: externalSource ? String(externalSource.lastSuccessfulSyncAt || externalSource.lastSyncedAt || '') : '',
        syncStatus: externalSource ? String(externalSource.syncStatus || '') : '',
        portraitUrl: portraitUrl || resolvedComboPortrait,
        tokenImageUrl: tokenUrl || portraitUrl || resolvedComboPortrait,
        speciesId: String(species.id || '').trim().toLowerCase(),
        speciesLabel: String(species.name || '').trim(),
        gender: String(identity.gender || '').trim().toLowerCase(),
        classId: String(firstClass.classId || classData.id || firstClass.name || '').trim().toLowerCase(),
        color: '#5a4f8f',
        shape: 'rect',
      };
    }).filter(Boolean);
  }

  global.CharacterLibraryPanel = { normalizeCharacterEntries };
})(window);
