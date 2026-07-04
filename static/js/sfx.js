// Synthesized sound effects for the race page (wheel ticks + coin flip).
// Everything is generated with the Web Audio API — no audio assets, no CDN.
// The four sounds are Graham's picks from a three-round audition:
//   tick    — "Soft ratchet": muffled mechanical tick, noise through a
//             900 Hz bandpass with a rounded 25 ms envelope.
//   whoosh  — "Whip flips": four short "fwip" whooshes with fast bandpass
//             sweeps, decelerating like a coin turning end over end
//             (~0.6 s total, leaving air before the ~1.1 s reveal).
//   success — "Cadence ta-DAA": a short G-major hit into a big sustained
//             C-major, both detuned-saw chords, tremolo on the resolution.
//   fail    — "Deep power-down": four square-wave steps down in fifths
//             into a low sustained buzz that fades out.
// The AudioContext is created lazily on the first play call, which is always
// downstream of a user click (Spin, manual override, half-veto pick), so
// browser autoplay policies are satisfied. When muted (persisted in
// localStorage under "km-muted", same pattern as the theme picker's
// "km-theme") every play call is a no-op, so call sites stay unconditional.
// A missing/broken Web Audio API degrades to silent no-ops, never a JS error.
var SFX = (function () {
    var MUTE_KEY = "km-muted";
    var ctx = null;
    var unavailable = false;
    var noise = null;

    var muted = (function () {
        try { return localStorage.getItem(MUTE_KEY) === "1"; }
        catch (e) { return false; }
    })();

    function getContext() {
        if (unavailable) return null;
        if (!ctx) {
            var Ctor = window.AudioContext || window.webkitAudioContext;
            if (!Ctor) { unavailable = true; return null; }
            try { ctx = new Ctor(); }
            catch (e) { unavailable = true; return null; }
        }
        if (ctx.state === "suspended" && ctx.resume) {
            try {
                var p = ctx.resume();
                if (p && p.catch) p.catch(function () {});
            } catch (e) {}
        }
        return ctx;
    }

    // Shared 1.2s white-noise buffer (created once per context) — source
    // material for both the ratchet tick and the coin-flip fwips.
    function noiseBuffer(ac) {
        if (!noise) {
            var frames = Math.round(ac.sampleRate * 1.2);
            noise = ac.createBuffer(1, frames, ac.sampleRate);
            var data = noise.getChannelData(0);
            for (var i = 0; i < frames; i++) {
                data[i] = Math.random() * 2 - 1;
            }
        }
        return noise;
    }

    // Tear down a finished sound's node chain once its source ends — the
    // tick fires ~30/s during a spin, so nothing may linger connected.
    function cleanupOnEnded(src, nodes) {
        src.onended = function () {
            for (var i = 0; i < nodes.length; i++) {
                try { nodes[i].disconnect(); } catch (e) {}
            }
        };
    }

    // Small helper: oscillator -> gain -> destination, with a linear
    // attack and exponential release gated to zero.
    function tone(ac, opts) {
        var osc = ac.createOscillator();
        osc.type = opts.type || "sine";
        osc.frequency.value = opts.freq;
        var g = ac.createGain();
        var atk = opts.attack != null ? opts.attack : 0.012;
        g.gain.setValueAtTime(0.0001, opts.start);
        g.gain.linearRampToValueAtTime(opts.gain, opts.start + atk);
        g.gain.exponentialRampToValueAtTime(0.001, opts.start + opts.dur);
        osc.connect(g);
        g.connect(ac.destination);
        osc.start(opts.start);
        osc.stop(opts.start + opts.dur + 0.05);
        cleanupOnEnded(osc, [osc, g]);
    }

    // Soft ratchet — a muffled mechanical tick, like a felt-lined pawl:
    // a noise burst through a 900 Hz bandpass with a rounded 25 ms
    // envelope (6 ms rise, linear fall to silence).
    function tick() {
        if (muted) return;
        var ac = getContext();
        if (!ac) return;
        try {
            var t0 = ac.currentTime;
            var src = ac.createBufferSource();
            src.buffer = noiseBuffer(ac);
            var bp = ac.createBiquadFilter();
            bp.type = "bandpass";
            bp.frequency.value = 900;
            bp.Q.value = 2;
            var g = ac.createGain();
            g.gain.setValueAtTime(0.0001, t0);
            g.gain.linearRampToValueAtTime(0.5, t0 + 0.006);
            g.gain.linearRampToValueAtTime(0.0001, t0 + 0.025);
            src.connect(bp);
            bp.connect(g);
            g.connect(ac.destination);
            src.start(t0);
            src.stop(t0 + 0.03);
            cleanupOnEnded(src, [src, bp, g]);
        } catch (e) {}
    }

    // One "fwip": a short noise burst whose bandpass whips down then back
    // up — one coin edge slicing past.
    function fwip(ac, start, dur, gain) {
        var src = ac.createBufferSource();
        src.buffer = noiseBuffer(ac);
        var bp = ac.createBiquadFilter();
        bp.type = "bandpass";
        bp.Q.value = 2.2;
        bp.frequency.setValueAtTime(2200, start);
        bp.frequency.exponentialRampToValueAtTime(550, start + dur * 0.45); // down...
        bp.frequency.exponentialRampToValueAtTime(2400, start + dur);       // ...and up
        var g = ac.createGain();
        g.gain.setValueAtTime(0.0001, start);
        g.gain.linearRampToValueAtTime(gain, start + 0.012);
        g.gain.setValueAtTime(gain, start + dur * 0.55);
        g.gain.linearRampToValueAtTime(0.0001, start + dur);
        src.connect(bp);
        bp.connect(g);
        g.connect(ac.destination);
        src.start(start);
        src.stop(start + dur + 0.01);
        cleanupOnEnded(src, [src, bp, g]);
    }

    // Whip flips — four discrete fwip whooshes with slightly decelerating
    // intervals (a coin turning end over end), done by ~0.6 s so there's a
    // beat of air before the result sound lands at ~1.1 s.
    function whoosh() {
        if (muted) return;
        var ac = getContext();
        if (!ac) return;
        try {
            var t0 = ac.currentTime;
            var starts = [0, 0.12, 0.27, 0.46];  // ~120 ms apart, widening
            var durs = [0.1, 0.11, 0.12, 0.14];  // each turn a touch longer
            for (var i = 0; i < starts.length; i++) {
                fwip(ac, t0 + starts[i], durs[i], 0.28);
            }
        } catch (e) {}
    }

    // Chord voicings — each voice becomes a ±6-cent detuned saw pair.
    var G_MAJ = [392.0, 493.88, 587.33];   // G4 B4 D5
    var C_MAJ = [523.25, 659.25, 783.99];  // C5 E5 G5

    // The tada-chord synth, parameterized by voicing and duration.
    // big=true adds the 5.5 Hz tremolo + long sustain/release (the
    // "DAA!"); big=false is a tight fully-gated hit (the "ta").
    // Envelope always gates to zero; node chains disconnect on end.
    function sawChord(ac, freqs, start, end, level, big) {
        var dur = end - start;
        var envG = ac.createGain();
        envG.gain.setValueAtTime(0.0001, start);
        envG.gain.linearRampToValueAtTime(level, start + (big ? 0.03 : 0.02));
        if (big) {
            envG.gain.setValueAtTime(level, end - 0.35);
        } else {
            envG.gain.setValueAtTime(level, start + dur * 0.55);
        }
        envG.gain.exponentialRampToValueAtTime(0.001, end);

        if (big) {
            // Tremolo is multiplicative so it can't hold the tail open.
            var tremStage = ac.createGain();
            tremStage.gain.value = 1;
            var trem = ac.createOscillator();
            trem.frequency.value = 5.5;
            var tremG = ac.createGain();
            tremG.gain.value = 0.15;
            trem.connect(tremG);
            tremG.connect(tremStage.gain);
            envG.connect(tremStage);
            tremStage.connect(ac.destination);
            trem.start(start);
            trem.stop(end + 0.05);
            cleanupOnEnded(trem, [trem, tremG, tremStage]);
        } else {
            envG.connect(ac.destination);
        }

        var chain = [envG];
        var lastOsc = null;
        for (var v = 0; v < freqs.length; v++) {
            var vgNode = ac.createGain();
            vgNode.gain.value = 1;
            vgNode.connect(envG);
            chain.push(vgNode);
            var cents = [-6, 6];
            for (var c = 0; c < cents.length; c++) {
                var osc = ac.createOscillator();
                osc.type = "sawtooth";
                osc.frequency.value = freqs[v];
                osc.detune.value = cents[c];
                osc.connect(vgNode);
                osc.start(start);
                osc.stop(end + 0.05);
                lastOsc = osc;
            }
        }
        // The last oscillator tears down the shared chain (envG + the
        // per-voice gains) once the chord ends.
        cleanupOnEnded(lastOsc, [lastOsc].concat(chain));
    }

    // Cadence ta-DAA — short G-major hit into the big sustained C-major.
    // Classic V -> I resolution: the most "you won" musically.
    function success() {
        if (muted) return;
        var ac = getContext();
        if (!ac) return;
        try {
            var t0 = ac.currentTime;
            sawChord(ac, G_MAJ, t0, t0 + 0.18, 0.1, false);
            sawChord(ac, C_MAJ, t0 + 0.22, t0 + 1.22, 0.11, true);
        } catch (e) {}
    }

    // Deep power-down — four big square steps, each a whole fifth down,
    // ending on a low sustained buzz that fades. Weightier "shutting down".
    function fail() {
        if (muted) return;
        var ac = getContext();
        if (!ac) return;
        try {
            var t0 = ac.currentTime;
            var steps = [660.0, 440.0, 293.66, 196.0]; // E5 A4 D4 G3 — down in fifths
            for (var i = 0; i < steps.length; i++) {
                tone(ac, { type: "square", freq: steps[i], start: t0 + i * 0.14, dur: 0.13, gain: 0.09, attack: 0.006 });
            }
            // The dying buzz.
            tone(ac, { type: "square", freq: 150, start: t0 + 0.56, dur: 0.55, gain: 0.1, attack: 0.02 });
        } catch (e) {}
    }

    function toggleMute() {
        muted = !muted;
        try { localStorage.setItem(MUTE_KEY, muted ? "1" : "0"); }
        catch (e) {}
        return muted;
    }

    var api = {
        tick: tick,
        whoosh: whoosh,
        success: success,
        fail: fail,
        toggleMute: toggleMute,
    };
    try {
        Object.defineProperty(api, "muted", {
            get: function () { return muted; },
        });
    } catch (e) {
        api.muted = muted;
    }
    return api;
})();
