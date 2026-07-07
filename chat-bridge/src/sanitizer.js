'use strict';

const HTML_ESCAPE = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
const DEFAULT_MAX = 64;

/**
 * Escape HTML entities and strip angle brackets, then truncate.
 * Used for any user-supplied string that will be rendered in an overlay.
 */
function sanitize(value, maxLen = DEFAULT_MAX) {
  const str = String(value ?? '').trim();
  const escaped = str.replace(/[&<>"']/g, ch => HTML_ESCAPE[ch]);
  return escaped.replace(/[<>]/g, '').slice(0, maxLen);
}

/**
 * Validate a Twitch username (alphanumeric + underscore, 1-25 chars).
 * Returns the lowercased username or null if invalid.
 */
function validateUsername(username) {
  const raw = String(username ?? '').trim().toLowerCase();
  if (/^[a-z0-9_]{1,25}$/.test(raw)) return raw;
  return null;
}

/**
 * Sanitize a command argument (target name, etc.).
 * Allow letters, numbers, spaces, hyphens, apostrophes, underscores.
 */
function sanitizeArg(value, maxLen = DEFAULT_MAX) {
  const str = String(value ?? '').trim();
  const cleaned = str.replace(/[^a-zA-Z0-9 '\-_]/g, '');
  return cleaned.slice(0, maxLen);
}

module.exports = { sanitize, validateUsername, sanitizeArg };
