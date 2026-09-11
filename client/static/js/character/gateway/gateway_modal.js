(function initGatewayModal(global) {
  function escHtml(value) {
    return String(value || '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');
  }

  function getSessionId() {
    const direct = String(global.SESSION_ID || '').trim();
    if (direct) return direct;
    try {
      const params = new URLSearchParams(global.location && global.location.search ? global.location.search : '');
      return String(params.get('session') || params.get('session_id') || '').trim();
    } catch (_) {
      return '';
    }
  }

  function csrfHeaders() {
    const match = document.cookie.match(/(?:^|; )csrf_token=([^;]+)/);
    const token = match ? decodeURIComponent(match[1]) : '';
    return Object.assign({ 'Content-Type': 'application/json' }, token ? { 'X-CSRF-Token': token } : {});
  }

  async function postDdbAction(url) {
    const sessionId = getSessionId();
    if (!sessionId) throw new Error('Missing session ID.');
    const response = await fetch(url, {
      method: 'POST',
      credentials: 'same-origin',
      headers: csrfHeaders(),
      body: JSON.stringify({ session_id: sessionId }),
    });
    let data = {};
    try { data = await response.json(); } catch (_) {}
    if (!response.ok || !data || data.ok !== true) {
      throw new Error(String((data && (data.error || data.detail || data.message)) || 'D&D Beyond request failed.'));
    }
    return data;
  }

  function formatLastSync(item) {
    const raw = String(item && (item.lastSyncedAt || (item.externalSource && (item.externalSource.lastSuccessfulSyncAt || item.externalSource.lastSyncedAt))) || '').trim();
    if (!raw) return '';
    const parsed = new Date(raw);
    if (Number.isNaN(parsed.getTime())) return '';
    const deltaMs = Math.max(0, Date.now() - parsed.getTime());
    const minutes = Math.floor(deltaMs / 60000);
    if (minutes < 1) return 'Last synced just now';
    if (minutes < 60) return 'Last synced ' + minutes + ' min ago';
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return 'Last synced ' + hours + ' hour' + (hours === 1 ? '' : 's') + ' ago';
    const days = Math.floor(hours / 24);
    return 'Last synced ' + days + ' day' + (days === 1 ? '' : 's') + ' ago';
  }

  function plainSourceBadge(item) {
    const key = String(item && item.sourceMode || '').toLowerCase();
    if (key === 'ddb' || key === 'dndbeyond' || key === 'dndbeyond_json' || key === 'json') return 'D&D Beyond';
    if (key === 'ddb_pdf' || key === 'dndbeyond_pdf' || key === 'pdf') return 'D&D Beyond PDF';
    if (key === 'native') return 'Casual D&D';
    return item && item.sourceBadge ? item.sourceBadge : 'Legacy';
  }

  function renderCharacterCards(opts) {
    const gridEl = opts.gridEl;
    const chars = Array.isArray(opts.characters) ? opts.characters : [];
    if (!gridEl) return;
    gridEl.innerHTML = '';

    if (!chars.length) {
      gridEl.innerHTML = '<div class="loading-msg">No existing characters in your profile library yet.</div>';
      return;
    }

    chars.forEach((item) => {
      if (!item || !item.id) return;
      const card = document.createElement('div');
      card.className = 'char-card';
      card.dataset.profileId = item.id;
      card.setAttribute('role', 'button');
      card.setAttribute('aria-pressed', opts.selectedProfileId === item.id ? 'true' : 'false');
      if (opts.selectedProfileId === item.id) card.classList.add('selected');

      const shape = item.shape === 'rect' ? 'rect' : '';
      const initials = String(item.name || '?').slice(0, 2).toUpperCase();
      const ownerLabel = item.ownerLabel || 'Existing profile';
      const ownerClass = item.mine ? 'mine' : '';
      const color = item.color || '#3b5f7a';
      const classSummary = item.classSummary ? String(item.classSummary) : '';
      const levelLabel = (item.level === null || item.level === undefined || item.level === '') ? '' : ('Level ' + item.level);
      const sourceBadge = item.sourceBadge ? String(item.sourceBadge) : '';
      const isDeletable = item.kind === 'library-profile' || (item.kind === 'session-token' && item.mine);
      const external = item.externalSource && typeof item.externalSource === 'object' ? item.externalSource : null;
      const linked = Boolean(item.kind === 'library-profile' && external && item.libraryId);
      const lastSync = linked ? formatLastSync(item) : '';
      const syncTone = linked && String(external.syncStatus || '').toLowerCase() === 'error' ? 'color:#ffb9b9;' : 'opacity:0.75;';
      const sourceUrl = linked ? String(external.sourceUrl || '').trim() : '';

      card.innerHTML = [
        '<div class="char-token ' + shape + '" style="background:' + escHtml(color) + '">',
        escHtml(initials),
        '</div>',
        '<div class="char-info">',
        '<div class="char-name">' + escHtml(item.name || 'Unnamed Character') + '</div>',
        '<div class="char-owner ' + ownerClass + '">' + escHtml(ownerLabel) + '</div>',
        classSummary ? ('<div class="char-owner">' + escHtml(classSummary) + '</div>') : '',
        (levelLabel || sourceBadge)
          ? ('<div class="char-owner">' + (levelLabel ? ('<span>' + escHtml(levelLabel) + '</span>') : '')
              + (sourceBadge ? ('<span style="margin-left:8px;opacity:0.9">' + escHtml(sourceBadge) + '</span>') : '') + '</div>')
          : '',
        lastSync ? ('<div class="char-owner" data-ddb-last-sync="1" style="' + syncTone + '">' + escHtml(lastSync) + '</div>') : '',
        item.ddbMessage ? ('<div class="char-owner" data-ddb-status="1" style="color:#a9ffe7;">' + escHtml(item.ddbMessage) + '</div>') : '',
        '</div>',
        '<span class="check">' + (opts.selectedProfileId === item.id ? '✓ Selected' : '✓') + '</span>',
        (opts.enableLevelupPreview && item.nativeCharacter && item.sourceMode === 'native')
          ? '<button type="button" class="btn btn-ghost" data-levelup-preview="1" style="margin-left:auto; padding:4px 8px; font-size:0.68rem;">Level-Up Preview</button>'
          : '',
        linked
          ? '<div data-ddb-controls="1" style="display:flex; gap:4px; flex-wrap:wrap; margin-left:auto;">'
              + '<button type="button" class="btn btn-ghost" data-ddb-refresh="1" style="padding:4px 8px; font-size:0.68rem;">Refresh</button>'
              + (sourceUrl ? ('<a class="btn btn-ghost" data-ddb-open="1" href="' + escHtml(sourceUrl) + '" target="_blank" rel="noopener noreferrer" style="padding:4px 8px; font-size:0.68rem; text-decoration:none;">Open source</a>') : '')
              + '<button type="button" class="btn btn-ghost" data-ddb-unlink="1" style="padding:4px 8px; font-size:0.68rem;">Unlink</button>'
              + '</div>'
          : '',
        isDeletable
          ? '<button type="button" class="btn btn-ghost" data-delete-profile="1" aria-label="Delete character" title="Delete from library" style="padding:4px 8px; font-size:0.68rem; margin-left:4px; color:#c0392b;">🗑 Delete</button>'
          : '',
      ].join('');

      const levelupBtn = card.querySelector('[data-levelup-preview="1"]');
      if (levelupBtn && typeof opts.onLevelupPreview === 'function') {
        levelupBtn.addEventListener('click', function onLevelupClick(event) {
          event.preventDefault(); event.stopPropagation(); opts.onLevelupPreview(item);
        });
      }

      const refreshBtn = card.querySelector('[data-ddb-refresh="1"]');
      if (refreshBtn) {
        refreshBtn.addEventListener('click', async function onDdbRefresh(event) {
          event.preventDefault(); event.stopPropagation();
          const status = card.querySelector('[data-ddb-status="1"]') || card.querySelector('[data-ddb-last-sync="1"]');
          const originalText = refreshBtn.textContent;
          refreshBtn.disabled = true; refreshBtn.textContent = 'Checking…';
          if (status) { status.textContent = 'Checking D&D Beyond…'; status.style.color = '#f5ddb0'; }
          try {
            const result = await postDdbAction('/api/character/' + encodeURIComponent(item.libraryId) + '/refresh/dndbeyond');
            item.externalSource = result.externalSource || item.externalSource;
            item.lastSyncedAt = String((item.externalSource && (item.externalSource.lastSuccessfulSyncAt || item.externalSource.lastSyncedAt)) || '');
            item.syncStatus = 'ok';
            item.ddbMessage = result.changed ? 'Updated from D&D Beyond.' : 'Character is already up to date.';
            if (result.profile && typeof result.profile === 'object') {
              if (result.profile.name) item.name = result.profile.name;
              if (result.profile.classSummary) item.classSummary = result.profile.classSummary;
              if (result.profile.level != null) item.level = result.profile.level;
            }
            renderCharacterCards(opts);
          } catch (err) {
            if (status) { status.textContent = String((err && err.message) || 'Could not refresh D&D Beyond. Your saved Tavern character is still available.'); status.style.color = '#ffb9b9'; }
            refreshBtn.disabled = false; refreshBtn.textContent = originalText;
          }
        });
      }

      const openBtn = card.querySelector('[data-ddb-open="1"]');
      if (openBtn) openBtn.addEventListener('click', function onOpenSource(event) { event.stopPropagation(); });

      const unlinkBtn = card.querySelector('[data-ddb-unlink="1"]');
      if (unlinkBtn) {
        unlinkBtn.addEventListener('click', async function onDdbUnlink(event) {
          event.preventDefault(); event.stopPropagation();
          const confirmed = global.confirm('Unlink D&D Beyond? Tavern will keep this character, its imported data, token, notes, and campaign state.');
          if (!confirmed) return;
          unlinkBtn.disabled = true; unlinkBtn.textContent = 'Unlinking…';
          try {
            await postDdbAction('/api/character/' + encodeURIComponent(item.libraryId) + '/unlink/dndbeyond');
            item.externalSource = null; item.isDdbLinked = false; item.lastSyncedAt = ''; item.syncStatus = '';
            item.sourceBadge = plainSourceBadge(item);
            item.ddbMessage = 'D&D Beyond unlinked. Tavern character kept.';
            renderCharacterCards(opts);
          } catch (err) {
            const status = card.querySelector('[data-ddb-status="1"]') || card.querySelector('[data-ddb-last-sync="1"]');
            if (status) { status.textContent = String((err && err.message) || 'Could not unlink D&D Beyond.'); status.style.color = '#ffb9b9'; }
            unlinkBtn.disabled = false; unlinkBtn.textContent = 'Unlink';
          }
        });
      }

      const deleteBtn = card.querySelector('[data-delete-profile="1"]');
      if (deleteBtn && typeof opts.onDelete === 'function') {
        deleteBtn.addEventListener('click', function onDeleteClick(event) {
          event.preventDefault(); event.stopPropagation(); opts.onDelete(item);
        });
      }

      card.addEventListener('click', function onCardClick() {
        if (typeof opts.onSelect === 'function') opts.onSelect(item.id);
      });
      gridEl.appendChild(card);
    });
  }

  function setHasExistingState(opts) {
    if (!opts || !opts.emptyHintEl || !opts.existingSectionEl || !opts.actionsSectionEl) return;
    const hasExisting = Array.isArray(opts.characters) && opts.characters.length > 0;
    opts.existingSectionEl.style.display = hasExisting ? '' : 'none';
    opts.emptyHintEl.style.display = hasExisting ? 'none' : '';
    opts.actionsSectionEl.classList.toggle('gateway-actions-prominent', !hasExisting);
  }

  function relabelDdbLinkButton() {
    const button = document.getElementById('gateway-import-ddb-btn');
    if (button) button.textContent = 'Link D&D Beyond';
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', relabelDdbLinkButton, { once: true });
  else relabelDdbLinkButton();

  global.CharacterGatewayModal = { renderCharacterCards, setHasExistingState };
})(window);
