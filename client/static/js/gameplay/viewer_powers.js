
(function(){
  function viewerProfileEntries(env) { return Object.values(env.viewerProfiles || {}).filter(v => v && typeof v === 'object'); }
  function viewerPowerDefs(env) {
    const base = {
      pebble_toss:     { name:'Pebble Toss',      kind:'single_damage', description:'Deal 1d4 damage to one chosen token.',                                                                    target_mode:'token', cooldown_sec:0 },
      arcane_zap:      { name:'Arcane Zap',        kind:'single_damage', description:'Deal 1d10 damage to one chosen token.',                                                                   target_mode:'token', cooldown_sec:0 },
      healing_spark:   { name:'Healing Spark',     kind:'single_heal',   description:'Restore 1d4 HP to one chosen token.',                                                                    target_mode:'token', cooldown_sec:0 },
      battle_blessing: { name:'Battle Blessing',   kind:'single_heal',   description:'Restore 1d8 HP to one chosen token.',                                                                    target_mode:'token', cooldown_sec:0 },
      fireball:        { name:'Fireball',          kind:'area_damage',   description:'Choose a point on the map. 15 ft burst, Dex save DC 13 for half damage.',                               target_mode:'point', cooldown_sec:90,  area_shape:'burst', radius_ft:15, cone_angle_deg:60, line_width_ft:5 },
      meteor_pop:      { name:'Meteor Pop',        kind:'area_damage',   description:'Choose a point on the map. 10 ft burst, 4d4 damage, Dex save DC 12 for half damage.',                    target_mode:'point', cooldown_sec:45,  area_shape:'burst', radius_ft:10, cone_angle_deg:60, line_width_ft:5 },
      trip_hex:        { name:'Trip Hex',          kind:'single_status', description:'Knock one token prone for 30 seconds. STR save negates. Also deals bonus 1d6 damage on hit.',             target_mode:'token', cooldown_sec:30,  condition:'prone',     duration_sec:30 },
      flash_freeze:    { name:'Flash Freeze',      kind:'single_status', description:'Freeze one token in place for 20 seconds. DEX save negates.',                                           target_mode:'token', cooldown_sec:45,  condition:'restrained', duration_sec:20 },
      goo_burst:       { name:'Goo Burst',         kind:'area_status',   description:'10 ft burst that restrains creatures for 20 seconds unless they pass a Dex save. Great for slowing movement.', target_mode:'point', cooldown_sec:75,  area_shape:'burst', radius_ft:10, cone_angle_deg:60, line_width_ft:5, condition:'restrained', duration_sec:20 },
      smoke_burst:     { name:'Smoke Burst',       kind:'area_status',   description:'10 ft burst that can blind targets briefly. CON save negates. Useful for short vision denial.',          target_mode:'point', cooldown_sec:45,  area_shape:'burst', radius_ft:10, cone_angle_deg:60, line_width_ft:5, condition:'blinded',    duration_sec:10 },
      knockback:       { name:'Knockback',         kind:'knockback',     description:'Choose a point on the map. Blasts the nearest token exactly one 5 ft grid square away from that point.', target_mode:'point', cooldown_sec:30,  area_shape:'burst', radius_ft:5 },
      give_potion:     { name:'Give Potion',       kind:'grant_item',    description:'Give the targeted player token a Potion of Minor Healing (heals 1d4).',                                 target_mode:'token', cooldown_sec:0 },
      chain_lightning: { name:'Chain Lightning',   kind:'chain_damage',  description:'Strike one token, then arc to nearby tokens — bouncing between 4 and 6 in all. Each takes 4d6 damage; Dex save DC 17 for half.', target_mode:'token', cooldown_sec:90 },
      give_random_item:{ name:'Give Random Item',  kind:'grant_random_item', description:'Give the targeted player token a random item drawn from the item library.',                          target_mode:'token', cooldown_sec:30 },
      magic_missile:   { name:'Magic Missile',     kind:'single_damage', description:'Three darts of force strike one target unerringly — 3d4+3 force damage, no attack roll, never misses.',   target_mode:'token', cooldown_sec:30 },
      shield:          { name:'Shield',            kind:'support_status', description:'Ward a party member: an invisible barrier absorbs the next incoming attack (negates its damage) within 60 seconds.', target_mode:'token', cooldown_sec:30, condition:'shielded', duration_sec:60 },
      ray_of_frost:    { name:'Ray of Frost',      kind:'single_damage_status', description:'A beam of frigid cold deals 1d8 cold damage and slows the target (halved movement) for 30 seconds.', target_mode:'token', cooldown_sec:30, condition:'slowed', duration_sec:30 },
      sleep:           { name:'Sleep',             kind:'single_status', description:'Lull one target into a magical slumber for 30 seconds (skips its turn) unless it passes a WIS save DC 12.', target_mode:'token', cooldown_sec:45, condition:'asleep', duration_sec:30 },
      lightning_bolt:  { name:'Lightning Bolt',    kind:'line_damage',   description:'A bolt tears through the target and everything in a line behind it (6 squares, pick the direction) — 2d6+1 lightning damage; Dex save DC 13 for half.', target_mode:'token', cooldown_sec:90 },
    };
    const aliases = { healing_spark:['Healing Word'], battle_blessing:['Bless'], goo_burst:['Grease'], smoke_burst:['Fog Cloud'] };
    Object.entries(aliases).forEach(([pid, names]) => { if (base[pid]) base[pid].aliases = names; });
    return { ...base, ...(env.viewerPowerCatalog || {}) };
  }
  function viewerPowerName(env, powerId) { return String(viewerPowerDefs(env)[powerId]?.name || powerId || 'Viewer Power'); }
  function viewerPowerDescription(env, powerId) {
    const def = viewerPowerDefs(env)[powerId] || {};
    let text = String(def.description || 'Viewer power');
    const aka = (def.aliases || []).filter(Boolean);
    if (aka.length) text += ` (aka ${aka.join(', ')})`;
    if (def.kind === 'single_status' || def.kind === 'area_status') {
      const cond = String(def.condition || '').trim();
      const dur = Math.max(0, Number(def.duration_sec || 0));
      if (cond) text += ` · ${cond}${dur ? ` for ${env.formatShortDurationSeconds(dur)}` : ''}`;
    }
    return text;
  }
  function viewerPowerAreaShape(env, powerId) { return String(viewerPowerDefs(env)[powerId]?.area_shape || 'burst'); }
  function viewerPowerNeedsMapTarget(env, powerId) { return String(viewerPowerDefs(env)[powerId]?.target_mode || '') === 'point'; }
  function viewerPowerNeedsSourceToken(env, powerId) { const shape = viewerPowerAreaShape(env, powerId); return shape === 'cone' || shape === 'line'; }
  function viewerPowerRangePx(env, powerId) { const ft = Number(viewerPowerDefs(env)[powerId]?.radius_ft || 15); return Math.max(25, (ft / 5) * 50); }
  function viewerPowerLineWidthPx(env, powerId) { const ft = Number(viewerPowerDefs(env)[powerId]?.line_width_ft || 5); return Math.max(10, (ft / 5) * 50); }
  function viewerPowerConeAngleDeg(env, powerId) { return Math.max(15, Number(viewerPowerDefs(env)[powerId]?.cone_angle_deg || 60)); }
  function viewerPowerActionLabel(env, powerId) { const shape = viewerPowerAreaShape(env, powerId); if (shape === 'aura') return 'Use Aura'; if (viewerPowerNeedsMapTarget(env, powerId)) return 'Target on Map'; return 'Use on Token'; }
  function viewerPowerCooldownLabel(env, entry) {
    const until = Number(entry?.cooldown_until || 0);
    const now = Date.now() / 1000;
    if (until > now) return `${Math.max(1, Math.ceil(until - now))}s cooldown`;
    const sec = Number(entry?.cooldown_sec || viewerPowerDefs(env)[entry?.power_id]?.cooldown_sec || 0);
    return sec > 0 ? `${sec}s cooldown` : 'No cooldown';
  }
  function revokeViewerPower(env, viewerUserId, powerId) { env.sendWS({ type:'viewer_power_revoke', payload:{ viewer_user_id: viewerUserId, power_id: powerId } }); }

  function viewerFxScreenPoint(env, payload) {
    if (payload && Number.isFinite(Number(payload.x)) && Number.isFinite(Number(payload.y))) {
      return env.worldToScreen(Number(payload.x), Number(payload.y));
    }
    if (payload && Number.isFinite(Number(payload.x2)) && Number.isFinite(Number(payload.y2))) {
      return env.worldToScreen(Number(payload.x2), Number(payload.y2));
    }
    if (payload && String(payload.token_id || '')) {
      const tok = env.tokens[String(payload.token_id)] || null;
      if (tok) {
        const cx = Number(tok.x || 0) + Number(tok.width || 0) / 2;
        const cy = Number(tok.y || 0) + Number(tok.height || 0) / 2;
        return env.worldToScreen(cx, cy);
      }
    }
    return null;
  }


  function appendViewerFxNode(env, wrap, styles) {
    const fx = env.document.createElement('div');
    fx.style.position = 'absolute';
    fx.style.pointerEvents = 'none';
    fx.style.zIndex = '10021';
    Object.entries(styles || {}).forEach(([k, v]) => { fx.style[k] = v; });
    wrap.appendChild(fx);
    return fx;
  }

  function showHealingSparkFx(env, wrap, screenPoint, payload) {
    const point = screenPoint || { x: wrap.clientWidth / 2, y: wrap.clientHeight * 0.3 };
    const halo = appendViewerFxNode(env, wrap, {
      left: `${point.x}px`, top: `${point.y}px`, width: '18px', height: '18px', marginLeft: '-9px', marginTop: '-9px',
      borderRadius: '999px', opacity: '0.98', transform: 'scale(0.4)',
      background: 'radial-gradient(circle, rgba(250,255,240,0.98) 0%, rgba(171,255,214,0.95) 28%, rgba(46,204,113,0.62) 58%, rgba(46,204,113,0) 78%)',
      boxShadow: '0 0 18px rgba(171,255,214,0.95), 0 0 36px rgba(74,222,128,0.65), 0 0 68px rgba(16,185,129,0.28)',
      border: '1px solid rgba(220,252,231,0.95)'
    });
    const ring = appendViewerFxNode(env, wrap, {
      left: `${point.x}px`, top: `${point.y}px`, width: '28px', height: '28px', marginLeft: '-14px', marginTop: '-14px',
      borderRadius: '999px', opacity: '0.9', transform: 'scale(0.6)',
      border: '2px solid rgba(220,252,231,0.9)', boxShadow: '0 0 20px rgba(34,197,94,0.4)'
    });
    const label = appendViewerFxNode(env, wrap, {
      left: `${point.x}px`, top: `${point.y - 28}px`, transform: 'translate(-50%, -50%) scale(0.9)',
      padding: '0.4rem 0.75rem', borderRadius: '999px', fontFamily: "'Cinzel', serif", letterSpacing: '0.08em',
      color: '#f0fdf4', border: '1px solid rgba(220,252,231,0.65)',
      background: 'linear-gradient(180deg, rgba(16,185,129,0.82), rgba(5,150,105,0.66))',
      boxShadow: '0 0 20px rgba(16,185,129,0.32)', whiteSpace: 'nowrap'
    });
    label.textContent = `${payload.label || 'HEAL'}${Number.isFinite(Number(payload.amount)) ? ` +${Number(payload.amount)}` : ''}`;
    const motes = [];
    for (let i = 0; i < 8; i += 1) {
      const angle = (Math.PI * 2 * i) / 8;
      const mote = appendViewerFxNode(env, wrap, {
        left: `${point.x}px`, top: `${point.y}px`, width: '8px', height: '8px', marginLeft: '-4px', marginTop: '-4px',
        borderRadius: '999px', opacity: '0.95', transform: 'translate(0px, 0px) scale(0.6)',
        background: 'radial-gradient(circle, rgba(255,255,255,0.98), rgba(187,247,208,0.95) 52%, rgba(74,222,128,0.1) 75%)',
        boxShadow: '0 0 10px rgba(187,247,208,0.9)'
      });
      motes.push({ mote, dx: Math.cos(angle) * (30 + (i % 2) * 16), dy: Math.sin(angle) * (26 + (i % 3) * 12) });
    }
    requestAnimationFrame(() => {
      halo.style.transition = 'transform 700ms cubic-bezier(.18,.74,.18,1), opacity 750ms ease';
      halo.style.transform = 'scale(6.8)';
      halo.style.opacity = '0';
      ring.style.transition = 'transform 760ms cubic-bezier(.18,.74,.18,1), opacity 760ms ease';
      ring.style.transform = 'scale(4.9)';
      ring.style.opacity = '0';
      label.style.transition = 'transform 760ms ease, opacity 760ms ease';
      label.style.transform = 'translate(-50%, -90%) scale(1.06)';
      label.style.opacity = '0';
      motes.forEach(({ mote, dx, dy }, idx) => {
        mote.style.transition = `transform ${620 + idx * 18}ms cubic-bezier(.18,.74,.18,1), opacity 700ms ease`;
        mote.style.transform = `translate(${dx}px, ${-Math.abs(dy)}px) scale(1.1)`;
        mote.style.opacity = '0';
      });
    });
    setTimeout(() => { [halo, ring, label, ...motes.map(m => m.mote)].forEach(n => n.remove()); }, 900);
  }

  function showLightningStrikeFx(env, wrap, screenPoint, payload) {
    const point = screenPoint || { x: wrap.clientWidth / 2, y: wrap.clientHeight / 2 };
    const radius = Math.max(36, Number(payload.radius || 50));
    const bolt = appendViewerFxNode(env, wrap, {
      left: `${point.x}px`, top: `${point.y - radius * 1.9}px`, width: '10px', height: `${Math.max(130, radius * 3.4)}px`,
      marginLeft: '-5px', transformOrigin: '50% 0%', transform: 'scaleY(0.08)', opacity: '0.02',
      background: 'linear-gradient(180deg, rgba(240,249,255,0.0), rgba(224,242,254,0.96) 16%, rgba(125,211,252,0.96) 40%, rgba(59,130,246,0.92) 68%, rgba(239,246,255,0.98) 100%)',
      clipPath: 'polygon(44% 0%, 62% 14%, 49% 31%, 71% 33%, 41% 58%, 58% 60%, 30% 100%, 46% 68%, 29% 66%, 51% 36%, 34% 34%, 56% 10%)',
      filter: 'drop-shadow(0 0 12px rgba(191,219,254,0.96)) drop-shadow(0 0 28px rgba(96,165,250,0.85)) drop-shadow(0 0 56px rgba(59,130,246,0.55))'
    });
    const flare = appendViewerFxNode(env, wrap, {
      left: `${point.x}px`, top: `${point.y}px`, width: `${radius * 1.35}px`, height: `${radius * 1.35}px`,
      marginLeft: `${-(radius * 1.35) / 2}px`, marginTop: `${-(radius * 1.35) / 2}px`, borderRadius: '999px',
      transform: 'scale(0.22)', opacity: '0.05',
      background: 'radial-gradient(circle, rgba(255,255,255,0.98) 0%, rgba(191,219,254,0.95) 22%, rgba(96,165,250,0.55) 46%, rgba(37,99,235,0.1) 72%, rgba(37,99,235,0) 82%)',
      boxShadow: '0 0 24px rgba(191,219,254,0.95), 0 0 72px rgba(59,130,246,0.45)'
    });
    const scorch = appendViewerFxNode(env, wrap, {
      left: `${point.x}px`, top: `${point.y}px`, width: `${radius * 1.9}px`, height: `${radius * 1.9}px`,
      marginLeft: `${-(radius * 1.9) / 2}px`, marginTop: `${-(radius * 1.9) / 2}px`, borderRadius: '999px',
      border: '2px solid rgba(147,197,253,0.85)', transform: 'scale(0.3)', opacity: '0.75',
      boxShadow: '0 0 26px rgba(96,165,250,0.36), inset 0 0 14px rgba(255,255,255,0.45)'
    });
    const label = appendViewerFxNode(env, wrap, {
      left: `${point.x}px`, top: `${point.y - radius * 0.7}px`, transform: 'translate(-50%, -50%) scale(0.94)',
      padding: '0.42rem 0.78rem', borderRadius: '999px', fontFamily: "'Cinzel', serif", letterSpacing: '0.1em',
      color: '#eff6ff', border: '1px solid rgba(191,219,254,0.72)',
      background: 'linear-gradient(180deg, rgba(37,99,235,0.88), rgba(30,64,175,0.72))',
      boxShadow: '0 0 18px rgba(59,130,246,0.3)', whiteSpace: 'nowrap'
    });
    label.textContent = payload.label || 'LIGHTNING';
    requestAnimationFrame(() => {
      bolt.style.transition = 'transform 150ms ease, opacity 180ms ease';
      bolt.style.transform = 'scaleY(1)';
      bolt.style.opacity = '1';
      flare.style.transition = 'transform 240ms ease, opacity 420ms ease';
      flare.style.transform = 'scale(1.7)';
      flare.style.opacity = '1';
      scorch.style.transition = 'transform 520ms cubic-bezier(.18,.74,.18,1), opacity 620ms ease';
      scorch.style.transform = 'scale(1.25)';
      scorch.style.opacity = '0';
      label.style.transition = 'transform 520ms ease, opacity 560ms ease';
      label.style.transform = 'translate(-50%, -74%) scale(1.03)';
      label.style.opacity = '0';
    });
    setTimeout(() => {
      bolt.style.transition = 'opacity 180ms ease';
      bolt.style.opacity = '0';
      flare.style.transition = 'opacity 220ms ease';
      flare.style.opacity = '0';
    }, 170);
    setTimeout(() => { [bolt, flare, scorch, label].forEach(n => n.remove()); }, 760);
  }

  function showLineEffectFx(env, wrap, payload) {
    // Draw the ACTUAL line of a line power (e.g. Lightning Bolt) between the
    // world endpoints, not just a flash at the target.
    if (!Number.isFinite(Number(payload.x1)) || !Number.isFinite(Number(payload.x2))) return;
    const start = env.worldToScreen(Number(payload.x1), Number(payload.y1));
    const end = env.worldToScreen(Number(payload.x2), Number(payload.y2));
    const dx = end.x - start.x;
    const dy = end.y - start.y;
    const length = Math.max(8, Math.hypot(dx, dy));
    const angle = Math.atan2(dy, dx);
    const beam = appendViewerFxNode(env, wrap, {
      left: `${start.x}px`, top: `${start.y}px`, width: `${length}px`, height: '10px', marginTop: '-5px',
      transformOrigin: '0% 50%', transform: `rotate(${angle}rad) scaleX(0.05)`, opacity: '0.1',
      borderRadius: '6px',
      background: 'linear-gradient(90deg, rgba(224,242,254,0.95), rgba(125,211,252,0.95) 30%, rgba(59,130,246,0.92) 70%, rgba(239,246,255,0.98))',
      boxShadow: '0 0 14px rgba(147,197,253,0.95), 0 0 34px rgba(59,130,246,0.6)'
    });
    const glow = appendViewerFxNode(env, wrap, {
      left: `${start.x}px`, top: `${start.y}px`, width: `${length}px`, height: '26px', marginTop: '-13px',
      transformOrigin: '0% 50%', transform: `rotate(${angle}rad) scaleX(0.05)`, opacity: '0.05',
      borderRadius: '13px',
      background: 'linear-gradient(90deg, rgba(59,130,246,0.0), rgba(96,165,250,0.35), rgba(59,130,246,0.0))',
      filter: 'blur(2px)'
    });
    const label = appendViewerFxNode(env, wrap, {
      left: `${(start.x + end.x) / 2}px`, top: `${(start.y + end.y) / 2 - 24}px`, transform: 'translate(-50%, -50%) scale(0.92)',
      padding: '0.4rem 0.75rem', borderRadius: '999px', fontFamily: "'Cinzel', serif", letterSpacing: '0.1em',
      color: '#eff6ff', border: '1px solid rgba(191,219,254,0.72)',
      background: 'linear-gradient(180deg, rgba(37,99,235,0.88), rgba(30,64,175,0.72))',
      boxShadow: '0 0 18px rgba(59,130,246,0.3)', whiteSpace: 'nowrap'
    });
    label.textContent = payload.label || 'LIGHTNING BOLT';
    requestAnimationFrame(() => {
      beam.style.transition = 'transform 160ms ease-out, opacity 140ms ease';
      beam.style.transform = `rotate(${angle}rad) scaleX(1)`;
      beam.style.opacity = '1';
      glow.style.transition = 'transform 200ms ease-out, opacity 220ms ease';
      glow.style.transform = `rotate(${angle}rad) scaleX(1)`;
      glow.style.opacity = '1';
      label.style.transition = 'transform 620ms ease, opacity 680ms ease';
      label.style.transform = 'translate(-50%, -86%) scale(1.04)';
      label.style.opacity = '0';
    });
    setTimeout(() => {
      beam.style.transition = 'opacity 260ms ease';
      beam.style.opacity = '0';
      glow.style.transition = 'opacity 300ms ease';
      glow.style.opacity = '0';
    }, 420);
    setTimeout(() => { [beam, glow, label].forEach(n => n.remove()); }, 820);
  }

  function showConditionFlashFx(env, wrap, screenPoint, payload) {
    // Brief icon flash on a token that just gained a condition
    // (slowed / asleep / shielded / blessed / …).
    const point = screenPoint || { x: wrap.clientWidth / 2, y: wrap.clientHeight / 2 };
    const icon = appendViewerFxNode(env, wrap, {
      left: `${point.x}px`, top: `${point.y}px`, transform: 'translate(-50%, -50%) scale(0.4)',
      fontSize: '30px', opacity: '0.1', textShadow: '0 0 14px rgba(255,255,255,0.85)'
    });
    icon.textContent = payload.icon || '✳️';
    const ring = appendViewerFxNode(env, wrap, {
      left: `${point.x}px`, top: `${point.y}px`, width: '34px', height: '34px', marginLeft: '-17px', marginTop: '-17px',
      borderRadius: '999px', border: '2px solid rgba(196,225,255,0.85)', transform: 'scale(0.4)', opacity: '0.9',
      boxShadow: '0 0 18px rgba(147,197,253,0.5)'
    });
    requestAnimationFrame(() => {
      icon.style.transition = 'transform 420ms cubic-bezier(.18,.74,.18,1), opacity 300ms ease';
      icon.style.transform = 'translate(-50%, -110%) scale(1.15)';
      icon.style.opacity = '1';
      ring.style.transition = 'transform 520ms cubic-bezier(.18,.74,.18,1), opacity 560ms ease';
      ring.style.transform = 'scale(2.1)';
      ring.style.opacity = '0';
    });
    setTimeout(() => {
      icon.style.transition = 'transform 380ms ease, opacity 420ms ease';
      icon.style.transform = 'translate(-50%, -170%) scale(0.9)';
      icon.style.opacity = '0';
    }, 620);
    setTimeout(() => { [icon, ring].forEach(n => n.remove()); }, 1100);
  }

  function showViewerFx(env, rawPayload) {
    let payload = rawPayload || {};
    const wrap = env.document.getElementById('canvas-wrap') || env.document.body;
    if (!wrap) return;
    if (payload.effect === 'area_shape') payload = { ...payload, effect: (payload.area_shape === 'burst' ? 'fireball' : 'power') };
    const screenPoint = viewerFxScreenPoint(env, payload);
    if (payload.effect === 'line_effect') {
      showLineEffectFx(env, wrap, payload);
      return;
    }
    if (payload.effect === 'condition_flash') {
      showConditionFlashFx(env, wrap, screenPoint, payload);
      return;
    }
    if (payload.effect === 'healing_spark') {
      showHealingSparkFx(env, wrap, screenPoint, payload);
      return;
    }
    if (payload.effect === 'lightning_strike') {
      showLightningStrikeFx(env, wrap, screenPoint, payload);
      return;
    }
    if (payload.effect === 'knockback') {
      const point = screenPoint || { x: wrap.clientWidth / 2, y: wrap.clientHeight / 2 };
      const ring = appendViewerFxNode(env, wrap, {
        left: `${point.x}px`, top: `${point.y}px`, width: '44px', height: '44px', marginLeft: '-22px', marginTop: '-22px',
        borderRadius: '999px', border: '2px solid rgba(255,214,170,0.92)', boxShadow: '0 0 24px rgba(255,190,92,0.34)',
        transform: 'scale(0.25)', opacity: '0.95'
      });
      const burst = appendViewerFxNode(env, wrap, {
        left: `${point.x}px`, top: `${point.y}px`, width: '18px', height: '18px', marginLeft: '-9px', marginTop: '-9px',
        borderRadius: '999px', background: 'radial-gradient(circle, rgba(255,247,237,0.98), rgba(251,191,36,0.95) 45%, rgba(234,88,12,0.1) 78%)',
        boxShadow: '0 0 20px rgba(251,191,36,0.55)', transform: 'scale(0.4)', opacity: '1'
      });
      const arrows = [];
      for (let i = 0; i < 4; i += 1) {
        const angle = (Math.PI * 2 * i) / 4;
        const arrow = appendViewerFxNode(env, wrap, {
          left: `${point.x}px`, top: `${point.y}px`, width: '34px', height: '10px', marginLeft: '-17px', marginTop: '-5px',
          background: 'linear-gradient(90deg, rgba(255,237,213,0.0), rgba(255,237,213,0.95) 38%, rgba(251,146,60,0.95) 100%)',
          clipPath: 'polygon(0% 50%, 54% 50%, 54% 18%, 100% 50%, 54% 82%, 54% 50%, 0% 50%)',
          transformOrigin: '50% 50%', transform: `rotate(${angle}rad) translateX(0px) scale(0.7)`, opacity: '0.92'
        });
        arrows.push({ arrow, angle });
      }
      requestAnimationFrame(() => {
        ring.style.transition = 'transform 420ms cubic-bezier(.18,.74,.18,1), opacity 520ms ease';
        ring.style.transform = 'scale(2.6)';
        ring.style.opacity = '0';
        burst.style.transition = 'transform 280ms ease, opacity 360ms ease';
        burst.style.transform = 'scale(2.2)';
        burst.style.opacity = '0';
        arrows.forEach(({ arrow, angle }, idx) => {
          arrow.style.transition = `transform ${340 + idx * 22}ms cubic-bezier(.18,.74,.18,1), opacity 420ms ease`;
          arrow.style.transform = `rotate(${angle}rad) translateX(52px) scale(1)`;
          arrow.style.opacity = '0';
        });
      });
      setTimeout(() => { [ring, burst, ...arrows.map(a => a.arrow)].forEach(n => n.remove()); }, 640);
      return;
    }
    if (payload.effect === 'projectile') {
      const dest = screenPoint || { x: wrap.clientWidth / 2, y: wrap.clientHeight / 2 };
      const fx = env.document.createElement('div');
      fx.style.position = 'absolute';
      fx.style.left = `${dest.x}px`;
      fx.style.top = `${dest.y}px`;
      fx.style.width = '20px';
      fx.style.height = '20px';
      fx.style.marginLeft = '-10px';
      fx.style.marginTop = '-10px';
      fx.style.borderRadius = '50%';
      fx.style.pointerEvents = 'none';
      fx.style.zIndex = '10021';
      fx.style.background = 'radial-gradient(circle, rgba(255,214,153,0.98), rgba(255,132,61,0.95) 48%, rgba(128,35,10,0.8) 75%)';
      fx.style.boxShadow = '0 0 30px rgba(255,132,61,0.75), 0 0 68px rgba(255,132,61,0.28)';
      let dx = -280;
      let dy = -180;
      if (Number.isFinite(Number(payload.x1)) && Number.isFinite(Number(payload.y1))) {
        const start = env.worldToScreen(Number(payload.x1), Number(payload.y1));
        dx = start.x - dest.x;
        dy = start.y - dest.y;
      }
      fx.style.transform = `translate(${dx}px, ${dy}px) scale(0.7)`;
      wrap.appendChild(fx);
      requestAnimationFrame(() => {
        fx.style.transition = 'transform 420ms cubic-bezier(.18,.74,.18,1), opacity 220ms ease';
        fx.style.transform = 'translate(0px, 0px) scale(1.06)';
      });
      setTimeout(() => { fx.style.opacity = '0'; }, 390);
      setTimeout(() => fx.remove(), 520);
      return;
    }
    if (payload.effect === 'fireball') {
      const pt = screenPoint || { x: wrap.clientWidth / 2, y: wrap.clientHeight / 2 };
      const radius = Math.max(48, Number(payload.radius || 80));
      const flare = appendViewerFxNode(env, wrap, { left:`${pt.x}px`, top:`${pt.y}px`, width:`${radius*1.8}px`, height:`${radius*1.8}px`, marginLeft:`${-(radius*1.8)/2}px`, marginTop:`${-(radius*1.8)/2}px`, borderRadius:'999px', transform:'scale(0.15)', opacity:'0.08', background:`radial-gradient(circle,rgba(255,255,255,0.98) 0%,rgba(255,229,153,0.95) 18%,rgba(255,120,30,0.88) 38%,rgba(200,55,5,0.72) 58%,rgba(30,0,0,0) 90%)`, boxShadow:`0 0 ${radius*0.7}px rgba(255,110,30,0.9),0 0 ${radius*1.4}px rgba(255,80,10,0.45)` });
      const ring1 = appendViewerFxNode(env, wrap, { left:`${pt.x}px`, top:`${pt.y}px`, width:`${radius*2.2}px`, height:`${radius*2.2}px`, marginLeft:`${-(radius*2.2)/2}px`, marginTop:`${-(radius*2.2)/2}px`, borderRadius:'999px', border:'3px solid rgba(255,145,40,0.9)', boxShadow:'0 0 24px rgba(255,100,20,0.5)', transform:'scale(0.18)', opacity:'0.96' });
      const ring2 = appendViewerFxNode(env, wrap, { left:`${pt.x}px`, top:`${pt.y}px`, width:`${radius*3.0}px`, height:`${radius*3.0}px`, marginLeft:`${-(radius*3.0)/2}px`, marginTop:`${-(radius*3.0)/2}px`, borderRadius:'999px', border:'2px solid rgba(255,90,20,0.65)', transform:'scale(0.18)', opacity:'0.88' });
      const lbl = appendViewerFxNode(env, wrap, { left:`${pt.x}px`, top:`${pt.y - radius * 0.55}px`, transform:'translate(-50%,-50%) scale(0.82)', padding:'0.44rem 0.9rem', borderRadius:'999px', fontFamily:"'Cinzel',serif", letterSpacing:'0.1em', color:'#fff8e8', border:'1px solid rgba(255,200,100,0.7)', background:'linear-gradient(180deg,rgba(200,70,10,0.92),rgba(120,30,5,0.78))', boxShadow:'0 0 22px rgba(255,100,20,0.38)', whiteSpace:'nowrap' });
      lbl.textContent = payload.label || 'FIREBALL';
      const embers = [];
      for (let i = 0; i < 10; i++) {
        const angle = (Math.PI * 2 * i) / 10 + (Math.random() - 0.5) * 0.5;
        const dist = radius * (0.55 + Math.random() * 0.7);
        const ember = appendViewerFxNode(env, wrap, { left:`${pt.x}px`, top:`${pt.y}px`, width:'8px', height:'8px', marginLeft:'-4px', marginTop:'-4px', borderRadius:'999px', background:'radial-gradient(circle,rgba(255,255,200,0.98),rgba(255,160,40,0.9) 45%,rgba(200,60,10,0.1) 78%)', boxShadow:'0 0 8px rgba(255,140,30,0.8)', transform:'translate(0,0) scale(0.5)', opacity:'1' });
        embers.push({ ember, dx: Math.cos(angle) * dist, dy: Math.sin(angle) * dist });
      }
      requestAnimationFrame(() => {
        flare.style.transition = 'transform 480ms cubic-bezier(.12,.8,.18,1), opacity 600ms ease';
        flare.style.transform = 'scale(1.7)'; flare.style.opacity = '0';
        ring1.style.transition = 'transform 520ms cubic-bezier(.12,.82,.18,1), opacity 580ms ease';
        ring1.style.transform = 'scale(1.45)'; ring1.style.opacity = '0';
        ring2.style.transition = 'transform 660ms cubic-bezier(.08,.8,.18,1), opacity 720ms ease';
        ring2.style.transform = 'scale(1.22)'; ring2.style.opacity = '0';
        lbl.style.transition = 'transform 580ms ease, opacity 620ms ease';
        lbl.style.transform = 'translate(-50%,-88%) scale(1.04)'; lbl.style.opacity = '0';
        embers.forEach(({ ember, dx, dy }, idx) => {
          ember.style.transition = `transform ${480 + idx * 24}ms cubic-bezier(.1,.82,.18,1), opacity ${520 + idx * 18}ms ease`;
          ember.style.transform = `translate(${dx}px,${dy}px) scale(1.1)`; ember.style.opacity = '0';
        });
      });
      setTimeout(() => { [flare, ring1, ring2, lbl, ...embers.map(e => e.ember)].forEach(n => n.remove()); }, 900);
      return;
    }
    const fx = env.document.createElement('div');
    fx.style.position = 'absolute';
    if (screenPoint) {
      fx.style.left = `${screenPoint.x}px`;
      fx.style.top = `${screenPoint.y}px`;
    } else {
      fx.style.left = '50%';
      fx.style.top = '18%';
    }
    fx.style.transform = 'translate(-50%, -50%) scale(0.85)';
    fx.style.padding = '0.7rem 0.95rem';
    fx.style.borderRadius = '999px';
    fx.style.pointerEvents = 'none';
    fx.style.zIndex = '10020';
    fx.style.fontFamily = "'Cinzel', serif";
    fx.style.letterSpacing = '0.08em';
    fx.style.color = '#fff';
    fx.style.border = '2px solid rgba(110,231,183,0.75)';
    fx.style.background = `radial-gradient(circle, ${payload.color || 'rgba(110,231,183,0.95)'}, rgba(17,24,39,0.72))`;
    fx.style.boxShadow = '0 0 32px rgba(110,231,183,0.35)';
    fx.textContent = payload.label || 'POWER';
    wrap.appendChild(fx);
    requestAnimationFrame(() => {
      fx.style.transition = 'all 700ms ease';
      fx.style.opacity = '0';
      fx.style.transform = 'translate(-50%, -58%) scale(1.12)';
    });
    setTimeout(() => fx.remove(), 760);
  }
  window.AppGameplayViewer = { viewerProfileEntries, viewerPowerDefs, viewerPowerName, viewerPowerDescription, viewerPowerAreaShape, viewerPowerNeedsMapTarget, viewerPowerNeedsSourceToken, viewerPowerRangePx, viewerPowerLineWidthPx, viewerPowerConeAngleDeg, viewerPowerActionLabel, viewerPowerCooldownLabel, revokeViewerPower, viewerFxScreenPoint, showViewerFx, showLineEffectFx, showConditionFlashFx };
})();
