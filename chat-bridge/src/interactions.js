'use strict';

/**
 * PendingInteractions — per-user multi-step chat interaction state.
 *
 * Some powers need a follow-up choice after !use (e.g. Lightning Bolt's
 * direction). The power's server-side def declares what it needs — a spec of
 * shape {id, prompt, options: [{value, aliases}]} — and the server returns it
 * via error_code=needs_follow_up. The bridge holds one pending interaction
 * per user; while it is pending, the user's next bare chat message (no !
 * prefix) that matches a valid answer resolves it.
 *
 * Rules:
 *  - one pending interaction per user; a newer one replaces the older one
 *  - timeout (default 30s) → onTimeout fires and the state is dropped
 *  - "cancel" / "nevermind" / "stop" cancels explicitly
 *  - non-matching messages are ordinary chat and are left alone
 *  - state is in-memory only: it does not survive a bridge restart (an
 *    orphaned reply is a plain chat message and is simply ignored)
 *
 * The machine is generic: nothing here knows about directions or Lightning
 * Bolt — future powers (e.g. AoE placement "which square?") reuse it by
 * declaring their own spec in their power def.
 */

const CANCEL_WORDS = new Set(['cancel', 'nevermind', 'never mind', 'stop', 'abort']);

const DEFAULT_TIMEOUT_MS = 30000;

class PendingInteractions {
  constructor({ timeoutMs, logger } = {}) {
    this._timeoutMs = timeoutMs ?? DEFAULT_TIMEOUT_MS;
    this._logger = logger ?? console;
    this._pending = new Map();  // username → {spec, answers, timer, onAnswer, onCancel, onTimeout}
  }

  has(username) { return this._pending.has(username); }
  get(username) { return this._pending.get(username) ?? null; }
  size() { return this._pending.size; }

  /**
   * Register (or replace) the pending interaction for a user.
   * entry: {spec, answers?, onAnswer(value, entry), onCancel(entry), onTimeout(entry)}
   */
  begin(username, entry) {
    this.clear(username);
    const record = { ...entry, answers: entry.answers ?? {} };
    record.timer = setTimeout(() => {
      this._pending.delete(username);
      Promise.resolve()
        .then(() => record.onTimeout?.(record))
        .catch(err => this._logger.error('[interactions] onTimeout error:', err));
    }, this._timeoutMs);
    if (typeof record.timer.unref === 'function') record.timer.unref();
    this._pending.set(username, record);
    return record;
  }

  /** Drop a user's pending interaction silently (no callbacks). */
  clear(username) {
    const prev = this._pending.get(username);
    if (prev) {
      clearTimeout(prev.timer);
      this._pending.delete(username);
    }
    return prev ?? null;
  }

  clearAll() {
    for (const username of [...this._pending.keys()]) this.clear(username);
  }

  /**
   * Normalize a raw reply against a follow-up spec. Returns the canonical
   * option value, the string 'cancel', or null when the text matches nothing.
   */
  static matchAnswer(spec, raw) {
    const text = String(raw ?? '').trim().toLowerCase();
    if (!text) return null;
    if (CANCEL_WORDS.has(text)) return 'cancel';
    for (const opt of spec?.options ?? []) {
      const value = String(opt?.value ?? '').trim().toLowerCase();
      if (!value) continue;
      if (text === value) return value;
      for (const alias of opt?.aliases ?? []) {
        if (text === String(alias ?? '').trim().toLowerCase()) return value;
      }
    }
    return null;
  }

  /**
   * Try to resolve a user's pending interaction with a bare chat message.
   * Returns true when the message was consumed (valid answer or cancel);
   * false when nothing is pending or the text matches no valid answer.
   */
  async handleMessage(username, raw) {
    const entry = this._pending.get(username);
    if (!entry) return false;
    const value = PendingInteractions.matchAnswer(entry.spec, raw);
    if (value === null) return false;
    this.clear(username);
    try {
      if (value === 'cancel') await entry.onCancel?.(entry);
      else await entry.onAnswer?.(value, entry);
    } catch (err) {
      this._logger.error('[interactions] handler error:', err);
    }
    return true;
  }
}

module.exports = { PendingInteractions, CANCEL_WORDS, DEFAULT_TIMEOUT_MS };
