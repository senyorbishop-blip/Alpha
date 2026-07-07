'use strict';

/**
 * Per-user per-command cooldown + global rate limiter.
 *
 * Cooldowns are loaded from config/cooldowns.json:
 *   { "default_seconds": 5, "target_seconds": 30 }
 */
class RateLimiter {
  constructor(config = {}) {
    this._defaultCooldown = (config.default_seconds ?? 5) * 1000;
    this._commandCooldowns = {};
    for (const [cmd, secs] of Object.entries(config.commands ?? {})) {
      this._commandCooldowns[cmd] = secs * 1000;
    }

    // Global bucket: max N messages per window
    this._globalMax = config.global_max ?? 30;
    this._globalWindowMs = (config.global_window_seconds ?? 10) * 1000;
    this._globalCount = 0;
    this._globalWindowStart = Date.now();

    // Per-user last-used map: username → { command → timestamp }
    this._userMap = new Map();
  }

  /**
   * Returns true if the command is allowed (not rate-limited).
   * Automatically records the usage if allowed.
   */
  check(username, command) {
    const now = Date.now();

    // Global bucket
    if (now - this._globalWindowStart > this._globalWindowMs) {
      this._globalCount = 0;
      this._globalWindowStart = now;
    }
    if (this._globalCount >= this._globalMax) return false;

    // Per-user cooldown
    if (!this._userMap.has(username)) this._userMap.set(username, {});
    const userCmds = this._userMap.get(username);
    const cooldownMs = this._commandCooldowns[command] ?? this._defaultCooldown;
    const last = userCmds[command] ?? 0;
    if (now - last < cooldownMs) return false;

    userCmds[command] = now;
    this._globalCount++;
    return true;
  }

  /** Remaining cooldown in seconds (0 if not limited). */
  remaining(username, command) {
    const now = Date.now();
    const userCmds = this._userMap.get(username) ?? {};
    const cooldownMs = this._commandCooldowns[command] ?? this._defaultCooldown;
    const last = userCmds[command] ?? 0;
    const rem = cooldownMs - (now - last);
    return rem > 0 ? Math.ceil(rem / 1000) : 0;
  }
}

module.exports = { RateLimiter };
