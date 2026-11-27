(() => {
  "use strict";

  const W = 390;
  const H = 844;
  const HUD_H = 74;
  const DANGER_Y = 696;
  const LAUNCH_X = 195;
  const LAUNCH_Y = 758;
  const COLS = 6;
  const PAD_X = 18;
  const CELL_GAP = 6;
  const CELL_W = (W - PAD_X * 2 - CELL_GAP * (COLS - 1)) / COLS;
  const CELL_H = 44;
  const ORB_R = 6.5;
  const ORB_SPEED = 740;

  const canvas = document.getElementById("game");
  const ctx = canvas.getContext("2d");

  const UPGRADES = [
    { id: "split", title: "Split-Shot +2", desc: "+2 Orbs per salvo", color: "#38bdf8" },
    { id: "arc", title: "Chain Arc", desc: "Lightning arcs to 2 nearby hazards", color: "#a855f7" },
    { id: "pierce", title: "Pierce Core", desc: "+1 Bonus damage & core punch", color: "#f59e0b" },
    { id: "merge", title: "Merge Surge x2", desc: "Double nova damage & score", color: "#22c55e" },
    { id: "gravity", title: "Gravity Pull", desc: "Orbs curve gently toward Merge Cores", color: "#ec4899" },
    { id: "shield", title: "Aegis Pulse", desc: "+1 Shield & wider blast radius", color: "#06b6d4" },
  ];

  // Deterministic Mulberry32 PRNG (strictly zero Math.random usage)
  let rngState = 42 >>> 0;
  function rand() {
    rngState = (rngState + 0x6d2b79f5) >>> 0;
    let t = rngState;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  }
  function randInt(min, max) {
    return min + Math.floor(rand() * (max - min + 1));
  }

  // Lazy WebAudio procedural synth (no external files)
  let audioCtx = null;
  function playTone(freq, dur, type, vol) {
    if (manualMode) return;
    try {
      if (!audioCtx) {
        const AC = window.AudioContext || window.webkitAudioContext;
        if (!AC) return;
        audioCtx = new AC();
      }
      if (audioCtx.state === "suspended") audioCtx.resume().catch(() => {});
      const osc = audioCtx.createOscillator();
      const gain = audioCtx.createGain();
      osc.type = type || "triangle";
      osc.frequency.setValueAtTime(freq, audioCtx.currentTime);
      gain.gain.setValueAtTime(vol || 0.04, audioCtx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.0001, audioCtx.currentTime + (dur || 0.08));
      osc.connect(gain);
      gain.connect(audioCtx.destination);
      osc.start();
      osc.stop(audioCtx.currentTime + (dur || 0.08));
    } catch (_) {}
  }

  // Simulation state
  let manualMode = false;
  let rafId = 0;
  let state = null;

  function makeInitialState(seed) {
    rngState = (Number(seed) >>> 0) || 42;
    const s = {
      seed: rngState,
      tick: 0,
      phase: "PLAYING", // PLAYING | DRAFT | REVIVE | GAMEOVER
      wave: 1,
      score: 0,
      combo: 0,
      bestCombo: 0,
      game_over: false,
      awaiting_revive: false,
      reviveUsed: false,
      shields: 0,
      orbCount: 4,
      bonusDmg: 0,
      chainArc: 0,
      mergeMult: 1,
      gravityPull: 0,
      blastScale: 1,
      launcherX: LAUNCH_X,
      aimAngle: -Math.PI / 2,
      aimActive: false,
      prevDown: false,
      firingQueue: 0,
      fireCooldown: 0,
      fireDirX: 0,
      fireDirY: -1,
      hitStop: 0,
      shake: 0,
      flashAlpha: 0,
      blocks: [],
      orbs: [],
      particles: [],
      arcs: [],
      floats: [],
      draftChoices: [],
      nextBlockId: 1,
    };
    spawnWaveRow(s, 3);
    return s;
  }

  function tierColor(tier) {
    const palette = {
      2: "#06b6d4",
      4: "#22c55e",
      8: "#f59e0b",
      16: "#ec4899",
      32: "#a855f7",
      64: "#ef4444",
    };
    return palette[tier] || "#38bdf8";
  }

  function spawnWaveRow(s, rows) {
    const numRows = rows || 1;
    // Shift existing blocks down first when advancing wave
    if (rows === undefined) {
      for (const b of s.blocks) {
        b.row += 1;
        b.targetY = HUD_H + 16 + b.row * (CELL_H + CELL_GAP);
      }
    }
    for (let r = 0; r < numRows; r++) {
      const targetRow = rows ? r : 0;
      let placed = 0;
      for (let c = 0; c < COLS; c++) {
        if (rand() < 0.72 || (c === COLS - 1 && placed === 0)) {
          placed++;
          const isMerge = rand() < 0.34;
          const tier = isMerge ? (rand() < 0.7 ? 2 : 4) : 0;
          const baseHp = isMerge ? tier + Math.floor(s.wave * 0.6) : Math.max(2, s.wave * 2 + randInt(0, s.wave + 1));
          const x = PAD_X + c * (CELL_W + CELL_GAP);
          const y = HUD_H + 16 + targetRow * (CELL_H + CELL_GAP);
          s.blocks.push({
            id: s.nextBlockId++,
            col: c,
            row: targetRow,
            x,
            y,
            targetY: y,
            w: CELL_W,
            h: CELL_H,
            hp: baseHp,
            maxHp: baseHp,
            isMerge,
            tier,
            hitFlash: 0,
          });
        }
      }
    }
    tryAutoMergeRows(s);
    checkDangerLine(s);
  }

  function tryAutoMergeRows(s) {
    // Drop-merge adjacent identical Merge Cores vertically or horizontally into higher tiers
    const byPos = new Map();
    for (const b of s.blocks) {
      if (b.isMerge) byPos.set(`${b.col},${b.row}`, b);
    }
    const mergedIds = new Set();
    for (const b of s.blocks) {
      if (!b.isMerge || mergedIds.has(b.id)) continue;
      const right = byPos.get(`${b.col + 1},${b.row}`);
      const below = byPos.get(`${b.col},${b.row + 1}`);
      const mate = (right && !mergedIds.has(right.id) && right.tier === b.tier)
        ? right
        : (below && !mergedIds.has(below.id) && below.tier === b.tier ? below : null);
      if (mate) {
        mergedIds.add(mate.id);
        b.tier = Math.min(64, b.tier * 2);
        b.hp = Math.max(b.hp, b.tier + s.wave);
        b.maxHp = b.hp;
        b.hitFlash = 1;
        s.score += b.tier * 5 * s.mergeMult;
        addBurst(s, b.x + b.w / 2, b.y + b.h / 2, tierColor(b.tier), 8);
        s.floats.push({ x: b.x + b.w / 2, y: b.y + 8, text: `MERGE ${b.tier}!`, color: tierColor(b.tier), life: 0.9 });
      }
    }
    if (mergedIds.size > 0) {
      s.blocks = s.blocks.filter((b) => !mergedIds.has(b.id));
    }
  }

  function checkDangerLine(s) {
    let breached = false;
    for (const b of s.blocks) {
      if (b.targetY + b.h >= DANGER_Y) {
        breached = true;
        break;
      }
    }
    if (!breached) return;
    if (s.shields > 0) {
      s.shields -= 1;
      clearBottomRows(s, 2);
      s.flashAlpha = 0.45;
      s.floats.push({ x: W / 2, y: DANGER_Y - 30, text: "AEGIS SHIELD CLEAR!", color: "#38bdf8", life: 1.0 });
      playTone(520, 0.18, "sine", 0.06);
      return;
    }
    if (!s.reviveUsed) {
      s.phase = "REVIVE";
      s.awaiting_revive = true;
      s.game_over = false;
      s.orbs = [];
      s.firingQueue = 0;
    } else {
      s.phase = "GAMEOVER";
      s.awaiting_revive = false;
      s.game_over = true;
      s.orbs = [];
      s.firingQueue = 0;
    }
  }

  function clearBottomRows(s, count) {
    if (s.blocks.length === 0) return;
    const maxRow = Math.max(...s.blocks.map((b) => b.row));
    const cutoff = maxRow - (count - 1);
    const survivors = [];
    for (const b of s.blocks) {
      if (b.row >= cutoff) {
        s.score += 15;
        addBurst(s, b.x + b.w / 2, b.y + b.h / 2, "#38bdf8", 7);
      } else {
        survivors.push(b);
      }
    }
    s.blocks = survivors;
  }

  function addBurst(s, x, y, color, count) {
    for (let i = 0; i < count; i++) {
      const a = rand() * Math.PI * 2;
      const sp = 50 + rand() * 190;
      s.particles.push({
        x,
        y,
        vx: Math.cos(a) * sp,
        vy: Math.sin(a) * sp,
        r: 2 + rand() * 2.5,
        color,
        life: 0.42 + rand() * 0.25,
        maxLife: 0.65,
      });
    }
  }

  function resolveCoord(rawX, rawY) {
    const nx = typeof rawX === "number" && Number.isFinite(rawX) ? rawX : 0.5;
    const ny = typeof rawY === "number" && Number.isFinite(rawY) ? rawY : 0.4;
    const px = nx >= 0 && nx <= 1.05 ? nx * W : nx;
    const py = ny >= 0 && ny <= 1.05 ? ny * H : ny;
    return { x: Math.max(8, Math.min(W - 8, px)), y: Math.max(HUD_H + 10, Math.min(H - 10, py)) };
  }

  function triggerDraft(s) {
    s.phase = "DRAFT";
    s.orbs = [];
    s.firingQueue = 0;
    const pool = [...UPGRADES];
    const picked = [];
    for (let i = 0; i < 3 && pool.length > 0; i++) {
      const idx = Math.floor(rand() * pool.length);
      picked.push(pool.splice(idx, 1)[0]);
    }
    s.draftChoices = picked;
  }

  function applyDraftChoice(s, choiceIdx) {
    const idx = Math.max(0, Math.min(s.draftChoices.length - 1, choiceIdx | 0));
    const card = s.draftChoices[idx];
    if (!card) return;
    if (card.id === "split") s.orbCount = Math.min(16, s.orbCount + 2);
    else if (card.id === "arc") s.chainArc = Math.min(4, s.chainArc + 1);
    else if (card.id === "pierce") s.bonusDmg += 1;
    else if (card.id === "merge") s.mergeMult = Math.min(8, s.mergeMult * 2);
    else if (card.id === "gravity") s.gravityPull = Math.min(260, s.gravityPull + 95);
    else if (card.id === "shield") {
      s.shields += 1;
      s.blastScale = Math.min(2.0, s.blastScale + 0.25);
    }
    s.wave += 1;
    s.phase = "PLAYING";
    s.draftChoices = [];
    spawnWaveRow(s);
    s.floats.push({ x: W / 2, y: LAUNCH_Y - 70, text: `${card.title.toUpperCase()}!`, color: card.color, life: 1.0 });
    playTone(660, 0.14, "sine", 0.05);
  }

  function fireSalvo(s, angle) {
    if (s.orbs.length > 0 || s.firingQueue > 0) return;
    const clamped = Math.max(-Math.PI + 0.22, Math.min(-0.22, angle));
    s.aimAngle = clamped;
    s.fireDirX = Math.cos(clamped);
    s.fireDirY = Math.sin(clamped);
    s.firingQueue = s.orbCount;
    s.fireCooldown = 0;
    s.combo = 0;
    playTone(340, 0.06, "triangle", 0.04);
  }

  function damageBlock(s, block, dmg, hitX, hitY) {
    block.hp -= dmg;
    block.hitFlash = 1.0;
    s.combo += 1;
    if (s.combo > s.bestCombo) s.bestCombo = s.combo;
    const mult = 1 + Math.floor(s.combo / 4);
    const pts = dmg * 10 * mult * (block.isMerge ? s.mergeMult : 1);
    s.score += pts;
    s.shake = Math.min(8, s.shake + 1.4);
    addBurst(s, hitX, hitY, block.isMerge ? tierColor(block.tier) : "#38bdf8", 4);
    if (s.combo % 3 === 1 || block.hp <= 0) {
      s.floats.push({
        x: hitX,
        y: hitY - 6,
        text: block.hp <= 0 ? `+${pts}` : `${dmg}x${mult}`,
        color: block.isMerge ? tierColor(block.tier) : "#fde047",
        life: 0.55,
      });
    }
    playTone(260 + Math.min(480, s.combo * 24), 0.05, "triangle", 0.03);

    if (block.hp <= 0) {
      if (block.isMerge) {
        triggerMergeNova(s, block);
      } else {
        addBurst(s, block.x + block.w / 2, block.y + block.h / 2, "#fb7185", 10);
      }
      if (s.chainArc > 0) {
        triggerChainArc(s, block);
      }
    }
  }

  function triggerMergeNova(s, centerBlock) {
    const cx = centerBlock.x + centerBlock.w / 2;
    const cy = centerBlock.y + centerBlock.h / 2;
    const radius = 92 * s.blastScale;
    const splashDmg = Math.max(2, centerBlock.tier) * s.mergeMult;
    s.hitStop = 0.035;
    s.shake = 7.5;
    s.flashAlpha = 0.22;
    addBurst(s, cx, cy, tierColor(centerBlock.tier), 16);
    s.floats.push({ x: cx, y: cy - 14, text: `NOVA x${s.mergeMult}!`, color: tierColor(centerBlock.tier), life: 0.85 });
    playTone(180, 0.16, "sawtooth", 0.05);

    for (const other of s.blocks) {
      if (other.id === centerBlock.id || other.hp <= 0) continue;
      const ox = other.x + other.w / 2;
      const oy = other.y + other.h / 2;
      const d = Math.hypot(ox - cx, oy - cy);
      if (d <= radius) {
        other.hp -= splashDmg;
        other.hitFlash = 1.0;
        s.score += splashDmg * 12;
        s.arcs.push({ x1: cx, y1: cy, x2: ox, y2: oy, color: tierColor(centerBlock.tier), life: 0.22 });
      }
    }
  }

  function triggerChainArc(s, origin) {
    const cx = origin.x + origin.w / 2;
    const cy = origin.y + origin.h / 2;
    const candidates = s.blocks
      .filter((b) => b.id !== origin.id && b.hp > 0)
      .map((b) => ({ b, d: Math.hypot(b.x + b.w / 2 - cx, b.y + b.h / 2 - cy) }))
      .filter((x) => x.d <= 150)
      .sort((a, b) => a.d - b.d)
      .slice(0, s.chainArc + 1);

    for (const { b } of candidates) {
      const dmg = 2 + s.bonusDmg;
      b.hp -= dmg;
      b.hitFlash = 1.0;
      s.score += dmg * 8;
      s.arcs.push({ x1: cx, y1: cy, x2: b.x + b.w / 2, y2: b.y + b.h / 2, color: "#a855f7", life: 0.18 });
    }
  }

  function stepSim(dtMs, input) {
    if (!state) state = makeInitialState(42);
    const s = state;
    const dt = Math.min(0.05, Math.max(0.001, (Number(dtMs) || 16.6667) / 1000));
    s.tick += 1;

    const inp = input || {};
    const down = Boolean(inp.down);
    const tap = Boolean(inp.tap);
    const pt = resolveCoord(inp.x, inp.y);

    // Phase-specific input handling
    if (s.phase === "DRAFT") {
      if (inp.choice !== undefined && inp.choice !== null) {
        applyDraftChoice(s, Number(inp.choice));
      } else if (tap || (!down && s.prevDown)) {
        // Map tap X/Y into one of 3 draft cards
        const cardIdx = pt.y < 380 ? 0 : pt.y < 510 ? 1 : 2;
        applyDraftChoice(s, cardIdx);
      }
      s.prevDown = down;
      render(s);
      return;
    }

    if (s.phase === "REVIVE") {
      if (tap || (!down && s.prevDown) || (inp.choice !== undefined && inp.choice !== null)) {
        s.reviveUsed = true;
        s.awaiting_revive = false;
        s.game_over = false;
        s.shields += 1;
        s.phase = "PLAYING";
        clearBottomRows(s, 2);
        s.floats.push({ x: W / 2, y: DANGER_Y - 40, text: "REVIVED + SHIELD!", color: "#22c55e", life: 1.1 });
        playTone(720, 0.2, "sine", 0.06);
      }
      s.prevDown = down;
      render(s);
      return;
    }

    if (s.phase === "GAMEOVER") {
      if (tap) {
        state = makeInitialState(s.seed + 1);
      }
      s.prevDown = down;
      render(state);
      return;
    }

    // PLAYING input: single-thumb drag-aim & release or tap
    const idleSalvo = s.orbs.length === 0 && s.firingQueue === 0;
    if (down && idleSalvo) {
      s.aimActive = true;
      const dx = pt.x - s.launcherX;
      const dy = Math.min(-24, pt.y - LAUNCH_Y);
      s.aimAngle = Math.max(-Math.PI + 0.22, Math.min(-0.22, Math.atan2(dy, dx)));
    }
    if ((tap || (!down && s.prevDown)) && idleSalvo) {
      const dx = pt.x - s.launcherX;
      const dy = Math.min(-24, pt.y - LAUNCH_Y);
      const ang = Math.atan2(dy, dx);
      s.aimActive = false;
      fireSalvo(s, ang);
    }
    if (!down) s.aimActive = false;
    s.prevDown = down;

    // Hit-stop juice timer
    if (s.hitStop > 0) {
      s.hitStop = Math.max(0, s.hitStop - dt);
      s.shake = Math.max(0, s.shake - dt * 18);
      s.flashAlpha = Math.max(0, s.flashAlpha - dt * 1.5);
      render(s);
      return;
    }

    // Smoothly settle blocks
    for (const b of s.blocks) {
      b.y += (b.targetY - b.y) * Math.min(1, dt * 14);
      b.hitFlash = Math.max(0, b.hitFlash - dt * 6);
    }

    // Spawn salvo orbs sequentially
    if (s.firingQueue > 0) {
      s.fireCooldown -= dt;
      if (s.fireCooldown <= 0) {
        s.firingQueue -= 1;
        s.fireCooldown = 0.055;
        s.orbs.push({
          x: s.launcherX,
          y: LAUNCH_Y,
          vx: s.fireDirX * ORB_SPEED,
          vy: s.fireDirY * ORB_SPEED,
          returned: false,
        });
      }
    }

    // Sub-stepped orb physics
    const subSteps = 2;
    const subDt = dt / subSteps;
    let firstReturnedX = null;

    for (let step = 0; step < subSteps; step++) {
      for (const orb of s.orbs) {
        if (orb.returned) continue;

        // Optional gravity well pull toward nearest Merge Core
        if (s.gravityPull > 0) {
          let bestCore = null;
          let bestDist = 180;
          for (const b of s.blocks) {
            if (!b.isMerge || b.hp <= 0) continue;
            const d = Math.hypot(b.x + b.w / 2 - orb.x, b.y + b.h / 2 - orb.y);
            if (d < bestDist && d > 10) {
              bestDist = d;
              bestCore = b;
            }
          }
          if (bestCore) {
            const tx = bestCore.x + bestCore.w / 2 - orb.x;
            const ty = bestCore.y + bestCore.h / 2 - orb.y;
            const len = Math.hypot(tx, ty) || 1;
            orb.vx += (tx / len) * s.gravityPull * subDt;
            orb.vy += (ty / len) * s.gravityPull * subDt;
            const spd = Math.hypot(orb.vx, orb.vy) || 1;
            orb.vx = (orb.vx / spd) * ORB_SPEED;
            orb.vy = (orb.vy / spd) * ORB_SPEED;
          }
        }

        orb.x += orb.vx * subDt;
        orb.y += orb.vy * subDt;

        // Wall reflections
        if (orb.x - ORB_R < 4) {
          orb.x = 4 + ORB_R;
          orb.vx = Math.abs(orb.vx);
          playTone(210, 0.03, "sine", 0.02);
        } else if (orb.x + ORB_R > W - 4) {
          orb.x = W - 4 - ORB_R;
          orb.vx = -Math.abs(orb.vx);
          playTone(210, 0.03, "sine", 0.02);
        }
        if (orb.y - ORB_R < HUD_H + 4) {
          orb.y = HUD_H + 4 + ORB_R;
          orb.vy = Math.abs(orb.vy);
          playTone(240, 0.03, "sine", 0.02);
        }

        // Bottom return
        if (orb.y >= LAUNCH_Y + 8) {
          orb.returned = true;
          if (firstReturnedX === null) firstReturnedX = Math.max(36, Math.min(W - 36, orb.x));
          continue;
        }

        // Prevent flat infinite horizontal loops
        if (Math.abs(orb.vy) < 42) {
          orb.vy = (orb.vy >= 0 ? 1 : -1) * 42;
          const spd = Math.hypot(orb.vx, orb.vy) || 1;
          orb.vx = (orb.vx / spd) * ORB_SPEED;
          orb.vy = (orb.vy / spd) * ORB_SPEED;
        }

        // Block AABB collision
        for (const b of s.blocks) {
          if (b.hp <= 0) continue;
          const cx = Math.max(b.x, Math.min(orb.x, b.x + b.w));
          const cy = Math.max(b.y, Math.min(orb.y, b.y + b.h));
          const dx = orb.x - cx;
          const dy = orb.y - cy;
          if (dx * dx + dy * dy <= ORB_R * ORB_R) {
            const overlapLeft = Math.abs(orb.x - b.x);
            const overlapRight = Math.abs(b.x + b.w - orb.x);
            const overlapTop = Math.abs(orb.y - b.y);
            const overlapBottom = Math.abs(b.y + b.h - orb.y);
            const minOverlap = Math.min(overlapLeft, overlapRight, overlapTop, overlapBottom);
            if (minOverlap === overlapLeft) {
              orb.vx = -Math.abs(orb.vx);
              orb.x = b.x - ORB_R - 0.5;
            } else if (minOverlap === overlapRight) {
              orb.vx = Math.abs(orb.vx);
              orb.x = b.x + b.w + ORB_R + 0.5;
            } else if (minOverlap === overlapTop) {
              orb.vy = -Math.abs(orb.vy);
              orb.y = b.y - ORB_R - 0.5;
            } else {
              orb.vy = Math.abs(orb.vy);
              orb.y = b.y + b.h + ORB_R + 0.5;
            }
            damageBlock(s, b, 1 + s.bonusDmg, cx, cy);
            break;
          }
        }
      }
    }

    // Remove destroyed blocks & returned orbs
    s.blocks = s.blocks.filter((b) => b.hp > 0);
    const hadOrbs = s.orbs.length > 0;
    s.orbs = s.orbs.filter((o) => !o.returned);
    if (firstReturnedX !== null) s.launcherX = firstReturnedX;

    // Salvo finished -> advance wave or open 3-card draft
    if (hadOrbs && s.orbs.length === 0 && s.firingQueue === 0) {
      if (s.blocks.length === 0) {
        s.score += 100 * s.wave;
        triggerDraft(s);
      } else {
        s.wave += 1;
        spawnWaveRow(s);
        if (s.wave % 3 === 0 && s.phase === "PLAYING") {
          triggerDraft(s);
        }
      }
    }

    // Update juice FX
    s.shake = Math.max(0, s.shake - dt * 18);
    s.flashAlpha = Math.max(0, s.flashAlpha - dt * 1.4);
    for (const p of s.particles) {
      p.x += p.vx * dt;
      p.y += p.vy * dt;
      p.vx *= 0.94;
      p.vy *= 0.94;
      p.life -= dt;
    }
    s.particles = s.particles.filter((p) => p.life > 0);
    for (const a of s.arcs) a.life -= dt;
    s.arcs = s.arcs.filter((a) => a.life > 0);
    for (const f of s.floats) {
      f.y -= 28 * dt;
      f.life -= dt;
    }
    s.floats = s.floats.filter((f) => f.life > 0);

    render(s);
  }

  function drawRoundedRect(x, y, w, h, r) {
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.lineTo(x + w - r, y);
    ctx.quadraticCurveTo(x + w, y, x + w, y + r);
    ctx.lineTo(x + w, y + h - r);
    ctx.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
    ctx.lineTo(x + r, y + h);
    ctx.quadraticCurveTo(x, y + h, x, y + h - r);
    ctx.lineTo(x, y + r);
    ctx.quadraticCurveTo(x, y, x + r, y);
    ctx.closePath();
  }

  function render(s) {
    ctx.save();
    if (s.shake > 0.2) {
      const sx = (Math.sin(s.tick * 1.9) * s.shake);
      const sy = (Math.cos(s.tick * 2.3) * s.shake);
      ctx.translate(sx, sy);
    }

    // Arena backdrop
    const bg = ctx.createLinearGradient(0, 0, 0, H);
    bg.addColorStop(0, "#0b1224");
    bg.addColorStop(1, "#060913");
    ctx.fillStyle = bg;
    ctx.fillRect(0, 0, W, H);

    // Subtle cyber grid
    ctx.strokeStyle = "rgba(56, 189, 248, 0.05)";
    ctx.lineWidth = 1;
    for (let gx = 0; gx <= W; gx += 39) {
      ctx.beginPath();
      ctx.moveTo(gx, HUD_H);
      ctx.lineTo(gx, H);
      ctx.stroke();
    }

    // HUD top bar
    ctx.fillStyle = "rgba(12, 20, 40, 0.94)";
    ctx.fillRect(0, 0, W, HUD_H);
    ctx.strokeStyle = "#1e293b";
    ctx.beginPath();
    ctx.moveTo(0, HUD_H);
    ctx.lineTo(W, HUD_H);
    ctx.stroke();

    ctx.fillStyle = "#94a3b8";
    ctx.font = "600 11px system-ui, sans-serif";
    ctx.fillText("WAVE", 16, 24);
    ctx.fillText("SCORE", 110, 24);
    ctx.fillText("COMBO", 232, 24);
    ctx.fillText("ORBS", 322, 24);

    ctx.fillStyle = "#f8fafc";
    ctx.font = "800 20px system-ui, sans-serif";
    ctx.fillText(String(s.wave), 16, 52);
    ctx.fillStyle = "#38bdf8";
    ctx.fillText(String(s.score), 110, 52);
    ctx.fillStyle = s.combo > 2 ? "#f59e0b" : "#e2e8f0";
    ctx.fillText(`x${Math.max(1, s.combo)}`, 232, 52);
    ctx.fillStyle = "#22c55e";
    ctx.fillText(`${s.orbCount}${s.shields > 0 ? "🛡" : ""}`, 322, 52);

    // Danger line
    ctx.strokeStyle = "rgba(239, 68, 68, 0.45)";
    ctx.setLineDash([6, 6]);
    ctx.beginPath();
    ctx.moveTo(12, DANGER_Y);
    ctx.lineTo(W - 12, DANGER_Y);
    ctx.stroke();
    ctx.setLineDash([]);

    // Blocks & Merge Cores
    for (const b of s.blocks) {
      const col = b.isMerge
        ? tierColor(b.tier)
        : (b.hp > 10 ? "#ef4444" : b.hp > 5 ? "#f59e0b" : "#3b82f6");
      ctx.fillStyle = b.hitFlash > 0.2 ? "#ffffff" : col;
      ctx.globalAlpha = b.isMerge ? 0.92 : 0.85;
      drawRoundedRect(b.x, b.y, b.w, b.h, 8);
      ctx.fill();
      ctx.globalAlpha = 1;

      ctx.strokeStyle = b.isMerge ? "#f8fafc" : "rgba(255,255,255,0.22)";
      ctx.lineWidth = b.isMerge ? 2 : 1;
      ctx.stroke();

      ctx.fillStyle = "#070b18";
      ctx.font = "800 14px system-ui, sans-serif";
      ctx.textAlign = "center";
      ctx.fillText(b.isMerge ? `◆${b.tier}` : String(b.hp), b.x + b.w / 2, b.y + 22);
      if (b.isMerge) {
        ctx.font = "700 9px system-ui, sans-serif";
        ctx.fillText(`HP ${b.hp}`, b.x + b.w / 2, b.y + 36);
      }
      ctx.textAlign = "left";
    }

    // Chain lightning / Nova arcs
    for (const a of s.arcs) {
      ctx.strokeStyle = a.color;
      ctx.globalAlpha = Math.min(1, a.life * 4.5);
      ctx.lineWidth = 2.5;
      ctx.beginPath();
      ctx.moveTo(a.x1, a.y1);
      ctx.lineTo((a.x1 + a.x2) / 2 + ((s.tick % 2) ? 6 : -6), (a.y1 + a.y2) / 2);
      ctx.lineTo(a.x2, a.y2);
      ctx.stroke();
      ctx.globalAlpha = 1;
    }

    // Aim trajectory guide (idle or dragging)
    if (s.phase === "PLAYING" && s.orbs.length === 0 && s.firingQueue === 0) {
      const dx = Math.cos(s.aimAngle);
      const dy = Math.sin(s.aimAngle);
      ctx.strokeStyle = s.aimActive ? "rgba(56, 189, 248, 0.85)" : "rgba(148, 163, 184, 0.35)";
      ctx.lineWidth = s.aimActive ? 2.5 : 1.5;
      ctx.setLineDash([5, 7]);
      ctx.beginPath();
      ctx.moveTo(s.launcherX, LAUNCH_Y);
      ctx.lineTo(s.launcherX + dx * 280, LAUNCH_Y + dy * 280);
      ctx.stroke();
      ctx.setLineDash([]);

      if (s.wave === 1 && s.tick < 120 && !s.aimActive) {
        ctx.fillStyle = "rgba(226, 232, 240, 0.72)";
        ctx.font = "600 12px system-ui, sans-serif";
        ctx.textAlign = "center";
        ctx.fillText("DRAG & RELEASE TO RICOCHET + MERGE", W / 2, LAUNCH_Y + 46);
        ctx.textAlign = "left";
      }
    }

    // Orbs
    for (const orb of s.orbs) {
      ctx.fillStyle = "#38bdf8";
      ctx.beginPath();
      ctx.arc(orb.x, orb.y, ORB_R + 2, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = "#f8fafc";
      ctx.beginPath();
      ctx.arc(orb.x, orb.y, ORB_R - 1.5, 0, Math.PI * 2);
      ctx.fill();
    }

    // Launcher turret
    ctx.fillStyle = "#22c55e";
    ctx.beginPath();
    ctx.arc(s.launcherX, LAUNCH_Y, 12, 0, Math.PI * 2);
    ctx.fill();
    ctx.strokeStyle = "#f8fafc";
    ctx.lineWidth = 2;
    ctx.stroke();

    // Particles & floating texts
    for (const p of s.particles) {
      ctx.fillStyle = p.color;
      ctx.globalAlpha = Math.max(0, p.life / p.maxLife);
      ctx.beginPath();
      ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
      ctx.fill();
    }
    ctx.globalAlpha = 1;

    ctx.textAlign = "center";
    for (const f of s.floats) {
      ctx.fillStyle = f.color;
      ctx.globalAlpha = Math.min(1, f.life * 1.4);
      ctx.font = "800 13px system-ui, sans-serif";
      ctx.fillText(f.text, f.x, f.y);
    }
    ctx.globalAlpha = 1;
    ctx.textAlign = "left";

    if (s.flashAlpha > 0.01) {
      ctx.fillStyle = `rgba(56, 189, 248, ${s.flashAlpha})`;
      ctx.fillRect(0, 0, W, H);
    }

    // Roguelite 3-card Draft overlay
    if (s.phase === "DRAFT") {
      ctx.fillStyle = "rgba(5, 8, 20, 0.84)";
      ctx.fillRect(0, 0, W, H);
      ctx.fillStyle = "#f8fafc";
      ctx.font = "800 20px system-ui, sans-serif";
      ctx.textAlign = "center";
      ctx.fillText(`WAVE ${s.wave} CLEARED!`, W / 2, 180);
      ctx.fillStyle = "#94a3b8";
      ctx.font = "600 13px system-ui, sans-serif";
      ctx.fillText("CHOOSE 1 SYNERGY UPGRADE", W / 2, 208);

      s.draftChoices.forEach((card, idx) => {
        const cy = 248 + idx * 126;
        ctx.fillStyle = "#0f172a";
        drawRoundedRect(34, cy, W - 68, 106, 12);
        ctx.fill();
        ctx.strokeStyle = card.color;
        ctx.lineWidth = 2;
        ctx.stroke();

        ctx.fillStyle = card.color;
        ctx.font = "800 18px system-ui, sans-serif";
        ctx.fillText(card.title, W / 2, cy + 44);
        ctx.fillStyle = "#cbd5e1";
        ctx.font = "500 13px system-ui, sans-serif";
        ctx.fillText(card.desc, W / 2, cy + 72);
      });
      ctx.textAlign = "left";
    }

    // Near-miss Revive / Game Over overlay
    if (s.phase === "REVIVE" || s.phase === "GAMEOVER") {
      ctx.fillStyle = "rgba(5, 8, 20, 0.86)";
      ctx.fillRect(0, 0, W, H);
      ctx.textAlign = "center";
      ctx.fillStyle = s.phase === "REVIVE" ? "#f59e0b" : "#ef4444";
      ctx.font = "800 26px system-ui, sans-serif";
      ctx.fillText(s.phase === "REVIVE" ? "NEAR MISS!" : "RUN COMPLETE", W / 2, 320);

      ctx.fillStyle = "#f8fafc";
      ctx.font = "700 18px system-ui, sans-serif";
      ctx.fillText(`Wave ${s.wave} · Score ${s.score}`, W / 2, 366);

      ctx.fillStyle = s.phase === "REVIVE" ? "#22c55e" : "#3b82f6";
      drawRoundedRect(54, 420, W - 108, 64, 12);
      ctx.fill();
      ctx.fillStyle = "#040812";
      ctx.font = "800 16px system-ui, sans-serif";
      ctx.fillText(
        s.phase === "REVIVE" ? "CONTINUE (+SHIELD & CLEAR)" : "PLAY AGAIN",
        W / 2,
        458
      );
      ctx.textAlign = "left";
    }

    ctx.restore();
  }

  // Unified human pointer/touch events (disabled when headless __GAME.reset takes over)
  const humanInput = { x: 0.5, y: 0.4, down: false, tap: false };
  function updatePointer(ev) {
    const rect = canvas.getBoundingClientRect();
    humanInput.x = ((ev.clientX - rect.left) / (rect.width || 1));
    humanInput.y = ((ev.clientY - rect.top) / (rect.height || 1));
  }
  canvas.addEventListener("pointerdown", (ev) => {
    if (manualMode) return;
    updatePointer(ev);
    humanInput.down = true;
  });
  canvas.addEventListener("pointermove", (ev) => {
    if (manualMode || !humanInput.down) return;
    updatePointer(ev);
  });
  canvas.addEventListener("pointerup", (ev) => {
    if (manualMode) return;
    updatePointer(ev);
    humanInput.down = false;
    humanInput.tap = true;
  });

  function browserLoop() {
    if (manualMode) return;
    stepSim(16.6667, humanInput);
    humanInput.tap = false;
    rafId = window.requestAnimationFrame(browserLoop);
  }

  // Deterministic Benchmark Bridge for ARENA-TMA-01 verifier
  window.__GAME = {
    reset(seed_u32) {
      manualMode = true;
      if (rafId) {
        window.cancelAnimationFrame(rafId);
        rafId = 0;
      }
      state = makeInitialState(seed_u32);
      render(state);
    },
    step(dt_ms, input) {
      manualMode = true;
      stepSim(dt_ms, input || {});
    },
    state() {
      const s = state || makeInitialState(42);
      return {
        phase: s.phase,
        wave: s.wave,
        score: s.score,
        entities: s.blocks.length + s.orbs.length,
        combo: s.combo,
        game_over: s.game_over,
        awaiting_revive: s.awaiting_revive,
      };
    },
  };

  // Boot live human playable session
  state = makeInitialState(2026);
  render(state);
  rafId = window.requestAnimationFrame(browserLoop);
})();
