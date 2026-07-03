// Synthesized sound effects for the race page (wheel ticks + coin flip).
// Everything is generated with the Web Audio API — no audio assets, no CDN.
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
    // material for both the tick click and the whoosh sweep.
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

    // Short bright click, like a prize-wheel pawl snapping past a peg:
    // a few-ms noise burst through a highpass filter with a fast decay.
    function tick() {
        if (muted) return;
        var ac = getContext();
        if (!ac) return;
        try {
            var t0 = ac.currentTime;
            var src = ac.createBufferSource();
            src.buffer = noiseBuffer(ac);
            var hp = ac.createBiquadFilter();
            hp.type = "highpass";
            hp.frequency.value = 2500;
            var g = ac.createGain();
            g.gain.setValueAtTime(0.4, t0);
            g.gain.exponentialRampToValueAtTime(0.001, t0 + 0.03);
            src.connect(hp);
            hp.connect(g);
            g.connect(ac.destination);
            src.start(t0);
            src.stop(t0 + 0.03);
        } catch (e) {}
    }

    // ~1.1s filtered-noise sweep for the coin flip: bandpass center rises
    // then falls, matching the coin's 1.2s CSS flip animation.
    function whoosh() {
        if (muted) return;
        var ac = getContext();
        if (!ac) return;
        try {
            var t0 = ac.currentTime;
            var dur = 1.1;
            var src = ac.createBufferSource();
            src.buffer = noiseBuffer(ac);
            src.loop = true;
            var bp = ac.createBiquadFilter();
            bp.type = "bandpass";
            bp.Q.value = 1.4;
            bp.frequency.setValueAtTime(300, t0);
            bp.frequency.linearRampToValueAtTime(1800, t0 + dur * 0.55);
            bp.frequency.linearRampToValueAtTime(250, t0 + dur);
            var g = ac.createGain();
            g.gain.setValueAtTime(0.0001, t0);
            g.gain.linearRampToValueAtTime(0.3, t0 + 0.12);
            g.gain.setValueAtTime(0.3, t0 + dur * 0.7);
            g.gain.linearRampToValueAtTime(0.0001, t0 + dur);
            src.connect(bp);
            bp.connect(g);
            g.connect(ac.destination);
            src.start(t0);
            src.stop(t0 + dur);
        } catch (e) {}
    }

    // Arcade-style rising chime for a successful coin flip: three square
    // notes (C5 → E5 → G5), the last held a little longer.
    function success() {
        if (muted) return;
        var ac = getContext();
        if (!ac) return;
        try {
            var t0 = ac.currentTime;
            var notes = [523.25, 659.25, 783.99];
            for (var i = 0; i < notes.length; i++) {
                var start = t0 + i * 0.12;
                var hold = i === notes.length - 1 ? 0.4 : 0.15;
                var osc = ac.createOscillator();
                osc.type = "square";
                osc.frequency.value = notes[i];
                var g = ac.createGain();
                g.gain.setValueAtTime(0.0001, start);
                g.gain.linearRampToValueAtTime(0.18, start + 0.02);
                g.gain.exponentialRampToValueAtTime(0.001, start + hold);
                osc.connect(g);
                g.connect(ac.destination);
                osc.start(start);
                osc.stop(start + hold + 0.05);
            }
        } catch (e) {}
    }

    // Comedic descending "womp" for a failed coin flip: two slightly
    // detuned saws sliding down in pitch together.
    function fail() {
        if (muted) return;
        var ac = getContext();
        if (!ac) return;
        try {
            var t0 = ac.currentTime;
            var dur = 0.7;
            var freqs = [220, 224];
            for (var i = 0; i < freqs.length; i++) {
                var osc = ac.createOscillator();
                osc.type = "sawtooth";
                osc.frequency.setValueAtTime(freqs[i], t0);
                osc.frequency.exponentialRampToValueAtTime(freqs[i] * 0.32, t0 + dur);
                var g = ac.createGain();
                g.gain.setValueAtTime(0.0001, t0);
                g.gain.linearRampToValueAtTime(0.14, t0 + 0.03);
                g.gain.exponentialRampToValueAtTime(0.001, t0 + dur);
                osc.connect(g);
                g.connect(ac.destination);
                osc.start(t0);
                osc.stop(t0 + dur + 0.05);
            }
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
