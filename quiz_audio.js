/* QuantInsti Quiz — sound and music.
 *
 * Everything here is synthesised live with the Web Audio API. That is deliberate:
 * no audio files to host, nothing to download, no CDN, and it works offline in a
 * meeting room where the wifi is already carrying twenty phones.
 *
 * Two roles, because they need very different amounts of sound:
 *   role 'host'   — the screen projected in the room. Gets the music bed: a calm
 *                   lobby loop, a pulse under each question that tightens in the
 *                   last few seconds, and the podium fanfare.
 *   role 'player' — a phone in someone's pocket. Short cues only, never a loop,
 *                   at low volume, because twenty phones playing a bed at once is
 *                   just noise.
 *
 * Browsers refuse to start audio before a user gesture, so an AudioContext is
 * created suspended and resumed on the first click/tap. Anything asked for before
 * that simply does nothing rather than throwing.
 */
(function () {
  'use strict';

  var ctx = null, master = null, musicBus = null, sfxBus = null;
  var role = 'host';
  var enabled = true;
  var started = false;          // has a gesture unlocked the context
  var scheduler = null;         // setInterval id for the music sequencer
  var pattern = null;           // the loop currently being sequenced
  var nextNoteTime = 0, step = 0;
  var STORE_KEY = 'qi_quiz_sound';

  // Equal-tempered note helper: name like 'A3', 'C#5', 'Eb4'.
  var SEMI = { C: 0, D: 2, E: 4, F: 5, G: 7, A: 9, B: 11 };
  function hz(note) {
    var m = /^([A-G])([#b]?)(-?\d)$/.exec(note);
    if (!m) return 440;
    var n = SEMI[m[1]] + (m[2] === '#' ? 1 : m[2] === 'b' ? -1 : 0);
    return 440 * Math.pow(2, (n - 9) / 12 + (parseInt(m[3], 10) - 4));
  }

  function store(v) { try { localStorage.setItem(STORE_KEY, v ? '1' : '0'); } catch (e) {} }
  function restore() {
    try {
      var v = localStorage.getItem(STORE_KEY);
      if (v === '0') return false;
      if (v === '1') return true;
    } catch (e) {}
    return null;   // nothing saved yet — fall back to the role default
  }

  function build() {
    if (ctx) return ctx;
    var AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return null;
    try { ctx = new AC(); } catch (e) { return null; }

    master = ctx.createGain();
    // Deliberately conservative: this plays out of a laptop into a room, and
    // being asked to turn it down is worse than being asked to turn it up.
    master.gain.value = role === 'host' ? 0.34 : 0.22;
    master.connect(ctx.destination);

    musicBus = ctx.createGain();
    musicBus.gain.value = role === 'host' ? 0.5 : 0;   // phones never get the bed
    musicBus.connect(master);

    sfxBus = ctx.createGain();
    sfxBus.gain.value = 1;
    sfxBus.connect(master);
    return ctx;
  }

  function ready() {
    if (!enabled) return false;
    if (!build()) return false;
    return started && ctx.state === 'running';
  }

  // ── primitives ─────────────────────────────────────────────────────────────

  /** One shaped note. Everything audible is built from this. */
  function note(o) {
    if (!ready()) return;
    var t0   = o.at != null ? o.at : ctx.currentTime;
    var dur  = o.dur != null ? o.dur : 0.25;
    var peak = o.gain != null ? o.gain : 0.3;
    var osc  = ctx.createOscillator();
    osc.type = o.type || 'triangle';
    osc.frequency.setValueAtTime(o.freq, t0);
    if (o.glideTo) osc.frequency.exponentialRampToValueAtTime(o.glideTo, t0 + dur);

    var g = ctx.createGain();
    var atk = o.attack != null ? o.attack : 0.012;
    g.gain.setValueAtTime(0.0001, t0);
    g.gain.exponentialRampToValueAtTime(peak, t0 + atk);
    // Exponential decay to near-silence reads as a natural instrument tail; a
    // linear ramp to 0 clicks on short notes.
    g.gain.exponentialRampToValueAtTime(0.0001, t0 + dur);

    var out = g;
    if (o.filter) {
      var f = ctx.createBiquadFilter();
      f.type = 'lowpass';
      f.frequency.value = o.filter;
      g.connect(f);
      out = f;
    }
    osc.connect(g);
    out.connect(o.bus === 'music' ? musicBus : sfxBus);
    osc.start(t0);
    osc.stop(t0 + dur + 0.05);
  }

  /** Filtered noise burst: hats, ticks, the confetti "whoosh". */
  function noise(o) {
    if (!ready()) return;
    var t0  = o.at != null ? o.at : ctx.currentTime;
    var dur = o.dur != null ? o.dur : 0.06;
    var frames = Math.max(1, Math.floor(ctx.sampleRate * dur));
    var buf = ctx.createBuffer(1, frames, ctx.sampleRate);
    var d = buf.getChannelData(0);
    for (var i = 0; i < frames; i++) d[i] = Math.random() * 2 - 1;

    var src = ctx.createBufferSource();
    src.buffer = buf;
    var f = ctx.createBiquadFilter();
    f.type = o.type || 'highpass';
    f.frequency.value = o.freq != null ? o.freq : 6000;
    var g = ctx.createGain();
    g.gain.setValueAtTime(o.gain != null ? o.gain : 0.12, t0);
    g.gain.exponentialRampToValueAtTime(0.0001, t0 + dur);
    src.connect(f); f.connect(g);
    g.connect(o.bus === 'music' ? musicBus : sfxBus);
    src.start(t0);
    src.stop(t0 + dur + 0.02);
  }

  /** Low sine thump with a fast pitch drop — the pulse under a live question. */
  function thump(at, gain) {
    if (!ready()) return;
    var t0 = at != null ? at : ctx.currentTime;
    var osc = ctx.createOscillator();
    osc.type = 'sine';
    osc.frequency.setValueAtTime(120, t0);
    osc.frequency.exponentialRampToValueAtTime(48, t0 + 0.14);
    var g = ctx.createGain();
    g.gain.setValueAtTime(0.0001, t0);
    g.gain.exponentialRampToValueAtTime(gain != null ? gain : 0.5, t0 + 0.008);
    g.gain.exponentialRampToValueAtTime(0.0001, t0 + 0.18);
    osc.connect(g); g.connect(musicBus);
    osc.start(t0); osc.stop(t0 + 0.22);
  }

  function chord(freqs, o) {
    o = o || {};
    freqs.forEach(function (f, i) {
      note({
        freq: f, dur: o.dur || 0.9, type: o.type || 'triangle',
        gain: (o.gain || 0.22) / Math.max(1, freqs.length * 0.55),
        at: (o.at != null ? o.at : (ctx ? ctx.currentTime : 0)) + i * (o.spread || 0),
        attack: o.attack || 0.02, bus: o.bus,
      });
    });
  }

  // ── music sequencer ────────────────────────────────────────────────────────
  // A lookahead scheduler: a timer wakes often and queues notes a little way into
  // the future on the audio clock. setTimeout alone is far too jittery to keep a
  // pulse steady, and mobile throttles it hard in the background.

  var LOOKAHEAD = 0.12;   // seconds of audio scheduled ahead of the clock
  var TICK_MS   = 25;

  var MUSIC_FADE = 0.2;   // seconds; stopMusic() fades out over this

  function musicLevel() { return role === 'host' ? 0.5 : 0; }

  function stopMusic() {
    if (scheduler) { clearInterval(scheduler); scheduler = null; }
    pattern = null;
    if (musicBus && ctx) {
      // Short fade rather than a hard cut, so stopping never pops.
      var now = ctx.currentTime;
      musicBus.gain.cancelScheduledValues(now);
      musicBus.gain.setValueAtTime(musicBus.gain.value, now);
      musicBus.gain.linearRampToValueAtTime(0.0001, now + MUSIC_FADE * 0.9);
      musicBus.gain.setValueAtTime(musicLevel(), now + MUSIC_FADE);
    }
  }

  function runPattern(p) {
    stopMusic();
    if (!ready()) return;
    pattern = p;
    step = 0;
    // Start *after* stopMusic()'s fade-out has finished. Scheduling into the fade
    // would silence the first beat or two of every new pattern — the opening of
    // the question bed is exactly where the room needs to hear it.
    nextNoteTime = ctx.currentTime + MUSIC_FADE + 0.05;
    scheduler = setInterval(function () {
      if (!pattern || !ctx) return;
      while (nextNoteTime < ctx.currentTime + LOOKAHEAD) {
        pattern.play(step, nextNoteTime);
        nextNoteTime += pattern.interval(step);
        step++;
      }
    }, TICK_MS);
  }

  // Lobby: an unhurried arpeggio over Cmaj9 / Am9, soft and low in the mix. It is
  // there to stop the room feeling silent while people join, nothing more.
  var LOBBY_NOTES = ['C4','G4','B4','D5','A4','E4','G4','D5',
                     'A3','E4','G4','B4','G4','D4','E4','G4'];
  function lobbyPattern() {
    return {
      interval: function () { return 0.42; },
      play: function (s, at) {
        var n = LOBBY_NOTES[s % LOBBY_NOTES.length];
        note({ freq: hz(n), dur: 1.5, type: 'triangle', gain: 0.14,
               attack: 0.06, at: at, filter: 2600, bus: 'music' });
        if (s % 8 === 0) {
          note({ freq: hz('C3'), dur: 3.2, type: 'sine', gain: 0.12,
                 attack: 0.35, at: at, bus: 'music' });
        }
      },
    };
  }

  // Question bed: a steady pulse, no melody — a melody under a question people
  // are trying to read is a distraction. Urgency comes from the pulse doubling
  // and the bass rising once the clock is nearly out.
  function questionPattern(getRemainingFraction) {
    var BASS = ['A2','A2','G2','G2'];
    return {
      interval: function () {
        return getRemainingFraction() <= 0.22 ? 0.24 : 0.48;   // double time at the end
      },
      play: function (s, at) {
        var urgent = getRemainingFraction() <= 0.22;
        thump(at, urgent ? 0.42 : 0.3);
        if (s % 2 === 1 || urgent) {
          noise({ at: at, dur: 0.045, freq: urgent ? 8200 : 7000,
                  gain: urgent ? 0.09 : 0.055, bus: 'music' });
        }
        if (s % 4 === 0) {
          var b = BASS[(s / 4) % BASS.length];
          note({ freq: hz(b) * (urgent ? 1.5 : 1), dur: urgent ? 0.5 : 0.9,
                 type: 'sine', gain: 0.2, at: at, bus: 'music' });
        }
      },
    };
  }

  // ── the public cues ────────────────────────────────────────────────────────

  var api = {
    /** Call once with 'host' or 'player' before anything else. */
    setup: function (r) {
      role = r === 'player' ? 'player' : 'host';
      var saved = restore();
      enabled = saved === null ? true : saved;
      build();
      // Any gesture unlocks audio. Passive listeners so this never delays a tap.
      var unlock = function () {
        started = true;
        if (ctx && ctx.state === 'suspended') ctx.resume();
      };
      ['pointerdown', 'keydown', 'touchend'].forEach(function (ev) {
        document.addEventListener(ev, unlock, { passive: true });
      });
      // A tab coming back from the background can leave the context suspended.
      document.addEventListener('visibilitychange', function () {
        if (!document.hidden && started && ctx && ctx.state === 'suspended') ctx.resume();
      });
      return api;
    },

    isEnabled: function () { return enabled; },

    setEnabled: function (on) {
      enabled = !!on;
      store(enabled);
      if (!enabled) {
        stopMusic();
        if (master && ctx) master.gain.setTargetAtTime(0.0001, ctx.currentTime, 0.02);
      } else {
        build();
        if (ctx && ctx.state === 'suspended') ctx.resume();
        if (master && ctx) {
          master.gain.setTargetAtTime(role === 'host' ? 0.34 : 0.22, ctx.currentTime, 0.05);
        }
      }
      return enabled;
    },

    toggle: function () { return api.setEnabled(!enabled); },

    lobby:        function () { if (role === 'host') runPattern(lobbyPattern()); },
    stopMusic:    stopMusic,

    /** getRemainingFraction: () => seconds left / total, used to tighten the bed. */
    question: function (getRemainingFraction) {
      if (role === 'host') runPattern(questionPattern(getRemainingFraction));
    },

    /** 3 - 2 - 1, each a step higher, then a brighter note on "go". */
    countdown: function (n) {
      if (!ready()) return;
      var map = { 3: 'C5', 2: 'E5', 1: 'G5' };
      note({ freq: hz(map[n] || 'C5'), dur: 0.3, type: 'triangle', gain: 0.34 });
      noise({ dur: 0.05, freq: 5000, gain: 0.07 });
    },

    go: function () {
      if (!ready()) return;
      chord([hz('C5'), hz('E5'), hz('G5'), hz('C6')],
            { dur: 0.7, gain: 0.34, spread: 0.03, attack: 0.01 });
    },

    /** The moment the four options appear and answering opens. */
    optionsReveal: function () {
      if (!ready()) return;
      var t = ctx.currentTime;
      ['G4','B4','D5'].forEach(function (n, i) {
        note({ freq: hz(n), dur: 0.22, type: 'sine', gain: 0.2, at: t + i * 0.055 });
      });
    },

    /** Short tick as someone locks an answer in. */
    answerTap: function () {
      if (!ready()) return;
      note({ freq: hz('D5'), dur: 0.1, type: 'sine', gain: 0.26, glideTo: hz('A5') });
      noise({ dur: 0.035, freq: 7000, gain: 0.06 });
    },

    /** Host: the answer is revealed. A settled major cadence, not a verdict. */
    revealAnswer: function () {
      if (!ready()) return;
      var t = ctx.currentTime;
      chord([hz('G3'), hz('D4'), hz('G4')], { at: t, dur: 0.5, gain: 0.24 });
      chord([hz('C4'), hz('E4'), hz('G4'), hz('C5')],
            { at: t + 0.22, dur: 1.1, gain: 0.28, spread: 0.02 });
    },

    /** Participant: your answer was right. Rising major third to a fifth. */
    correct: function () {
      if (!ready()) return;
      var t = ctx.currentTime;
      ['E5','G5','C6'].forEach(function (n, i) {
        note({ freq: hz(n), dur: 0.34, type: 'triangle', gain: 0.3, at: t + i * 0.09 });
      });
    },

    /** Participant: wrong. Two soft descending notes — clear, not punishing. */
    wrong: function () {
      if (!ready()) return;
      var t = ctx.currentTime;
      note({ freq: hz('E4'), dur: 0.24, type: 'triangle', gain: 0.26, at: t });
      note({ freq: hz('B3'), dur: 0.42, type: 'triangle', gain: 0.24, at: t + 0.14 });
    },

    /** Nobody answered in time. */
    timeUp: function () {
      if (!ready()) return;
      var t = ctx.currentTime;
      note({ freq: hz('A4'), dur: 0.2, type: 'square', gain: 0.14, at: t, filter: 1400 });
      note({ freq: hz('F4'), dur: 0.5, type: 'triangle', gain: 0.22, at: t + 0.16 });
    },

    /** Leaderboard sting: a quick upward run, like scores settling. */
    leaderboard: function () {
      if (!ready()) return;
      var t = ctx.currentTime;
      ['C5','D5','E5','G5'].forEach(function (n, i) {
        note({ freq: hz(n), dur: 0.22, type: 'triangle', gain: 0.24, at: t + i * 0.075 });
      });
      noise({ dur: 0.18, freq: 3000, gain: 0.05, at: t });
    },

    /** One podium place landing. Lower rank = lower pitch, so 3rd → 2nd → 1st rises. */
    podiumStep: function (rank) {
      if (!ready()) return;
      var root = { 3: 'C4', 2: 'E4', 1: 'G4' }[rank] || 'C4';
      var t = ctx.currentTime;
      thump(t, 0.34);
      note({ freq: hz(root), dur: 0.5, type: 'triangle', gain: 0.3, at: t });
      note({ freq: hz(root) * 2, dur: 0.42, type: 'sine', gain: 0.18, at: t + 0.04 });
      noise({ dur: 0.22, freq: 2600, gain: 0.07, at: t });
    },

    /** Winner fanfare: a short rising figure into a held major chord. */
    fanfare: function () {
      if (!ready()) return;
      var t = ctx.currentTime;
      var run = ['C5','E5','G5','C6'];
      run.forEach(function (n, i) {
        note({ freq: hz(n), dur: 0.2, type: 'triangle', gain: 0.3, at: t + i * 0.11 });
      });
      var hold = t + run.length * 0.11;
      chord([hz('C4'), hz('G4'), hz('C5'), hz('E5'), hz('G5')],
            { at: hold, dur: 2.2, gain: 0.34, spread: 0.025, attack: 0.02 });
      // A little shimmer on top so the hold does not sit completely still.
      [0, 0.3, 0.6].forEach(function (d, i) {
        note({ freq: hz(['E6','G6','C7'][i]), dur: 0.5, type: 'sine',
               gain: 0.09, at: hold + 0.15 + d });
      });
      noise({ dur: 0.5, freq: 2200, gain: 0.06, at: hold });
    },

    /** Everything off: force stop, reset, logout. */
    allOff: function () { stopMusic(); },
  };

  window.QuizAudio = api;
})();
