/*
 * Photo score entry.
 *
 * Wires up the "Take photo" / "Upload photo" controls on a score form:
 * downscales the chosen image on a canvas (max 1200px long edge, JPEG ~0.8),
 * stores the base64 in the form's hidden photo_data field so the photo is
 * saved with the cup on submit, and — when extraction is enabled — POSTs it
 * to /extract-scores and pre-fills matching score inputs. It NEVER submits
 * the form on its own; the human always reviews first. Manual entry works
 * regardless of what happens here.
 *
 * Silent-drop guards (the photo attach is async, so a submit could otherwise
 * race it or follow a failed decode without anyone noticing):
 *   - The photo buttons ship disabled and are enabled + wired here — if this
 *     script never loads, the picker simply can't open, so a pick can never
 *     happen unguarded.
 *   - A prominent attach indicator (.photo-attach-status) shows success
 *     ("Photo attached ✓") or a tinted error on decode failure.
 *   - A submit guard on the surrounding form: submit while a downscale is
 *     pending is blocked and auto-resumed when it settles; submit while the
 *     extract fetch is in flight is blocked WITHOUT auto-resume (the user
 *     must see and review the model-filled scores — never auto-submit them);
 *     submit with every score empty is blocked client-side (the server would
 *     reject it anyway, and the redirect would wipe the attached photo);
 *     submit after a failed attach requires an explicit confirm() to proceed
 *     photoless.
 *   - Busy UX while the extract fetch is in flight: a spinner shows beside
 *     the status line and the form's submit button is visually disabled
 *     (re-enabled on completion, error, or a superseding new pick). That's
 *     UX only — the submit guard above remains the backstop.
 *
 * Usage (per page):
 *   initPhotoScore({
 *     extractUrl: "/extract-scores" or null,   // null = attach-only mode
 *     getPayload: function () { return {cup_id: 7}; }  // merged into the POST
 *   });
 */
window.initPhotoScore = function (opts) {
    var block = document.getElementById("photo-score");
    if (!block) return;
    var attachEl = block.querySelector(".photo-attach-status");
    var statusEl = block.querySelector(".photo-status");
    var spinnerEl = block.querySelector(".photo-extract-spinner");
    var notesEl = block.querySelector(".photo-notes");
    var preview = block.querySelector(".photo-preview");
    var dataField = document.getElementById("photo-data");
    var mimeField = document.getElementById("photo-mime");
    var form = block.closest("form");
    var submitBtn = form ? form.querySelector('button[type="submit"]') : null;

    var MAX_EDGE = 1200;
    var JPEG_QUALITY = 0.8;
    var DECODE_ERROR_MSG =
        "Couldn't read that image — try taking the photo with the camera, " +
        "or use a JPEG/PNG.";

    // Bumped every time a new photo is picked; async work carries the value it
    // started with and bails if a newer pick has superseded it, so a stale
    // extraction response can never overwrite the newer photo's results.
    var requestSeq = 0;

    // Submit-guard state.
    var pending = false;         // a downscale is in flight
    var extracting = false;      // an /extract-scores fetch is in flight
    var lastPickFailed = false;  // the most recent pick failed to attach
    var waitingToSubmit = false; // a submit was blocked while pending

    function setStatus(msg) {
        statusEl.textContent = msg;
    }

    // Extraction-in-flight UX: spinner beside the status line + visually
    // disabled submit button so users wait for the fill instead of typing
    // scores in parallel. Pure UX — the submit guard below stays the actual
    // safety mechanism (backstop if this ever misses a path). Only extract()
    // and its settle paths call this, so attach-only mode (extractUrl null)
    // and extraction-disabled pages can never disable anything.
    function setExtractBusy(busy) {
        if (spinnerEl) spinnerEl.hidden = !busy;
        if (submitBtn) submitBtn.disabled = busy;
    }

    // kind: "success" | "error" | "pending" | null (null hides the indicator)
    function setAttachState(kind, msg) {
        attachEl.classList.remove("is-success", "is-error");
        if (!kind) {
            attachEl.hidden = true;
            attachEl.textContent = "";
            return;
        }
        if (kind === "success") attachEl.classList.add("is-success");
        if (kind === "error") attachEl.classList.add("is-error");
        attachEl.textContent = msg;
        attachEl.hidden = false;
    }

    function downscale(file, cb) {
        var img = new Image();
        var url = URL.createObjectURL(file);
        img.onload = function () {
            var w = img.naturalWidth, h = img.naturalHeight;
            var scale = Math.min(1, MAX_EDGE / Math.max(w, h));
            var canvas = document.createElement("canvas");
            canvas.width = Math.max(1, Math.round(w * scale));
            canvas.height = Math.max(1, Math.round(h * scale));
            canvas.getContext("2d").drawImage(img, 0, 0, canvas.width, canvas.height);
            URL.revokeObjectURL(url);
            cb(canvas.toDataURL("image/jpeg", JPEG_QUALITY));
        };
        img.onerror = function () {
            URL.revokeObjectURL(url);
            cb(null);
        };
        img.src = url;
    }

    function fillScores(scores) {
        var filled = 0;
        Object.keys(scores).forEach(function (pid) {
            var row = document.querySelector('.score-row[data-player-id="' + pid + '"]');
            if (!row || row.classList.contains("removed")) return;
            var input = row.querySelector(".score-input");
            if (!input) return;
            input.value = scores[pid];
            // Trigger the existing listeners so line-score sync + placement
            // recalculation run exactly as if the value was typed.
            input.dispatchEvent(new Event("input", { bubbles: true }));
            filled++;
        });
        return filled;
    }

    function extract(base64, seq) {
        extracting = true;
        setExtractBusy(true);
        setStatus("Reading scores from the photo…");
        var payload = opts.getPayload();
        payload.image = base64;
        payload.mime_type = "image/jpeg";
        fetch(opts.extractUrl, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        }).then(function (res) {
            return res.json().catch(function () { return {}; }).then(function (body) {
                return { ok: res.ok, status: res.status, body: body };
            });
        }).then(function (res) {
            if (seq !== requestSeq) return; // superseded by a newer photo
            extracting = false;
            setExtractBusy(false);
            if (!res.ok) {
                var msg = (res.body && res.body.error) ||
                    "Extraction failed (" + res.status + ")";
                setStatus(msg + " — photo is still attached; enter scores manually.");
                return;
            }
            var body = res.body || {};
            var filled = fillScores(body.scores || {});
            setStatus(
                "Filled " + filled + " score" + (filled === 1 ? "" : "s") +
                " from the photo — review before submitting."
            );
            var notes = [];
            if (body.ambiguous && body.ambiguous.length) {
                notes.push("Couldn't decide (shared character): " + body.ambiguous.join(", "));
            }
            if (body.unmatched_players && body.unmatched_players.length) {
                notes.push("No match — fill in manually: " + body.unmatched_players.join(", "));
            }
            notesEl.textContent = notes.join(" · ");
        }).catch(function () {
            if (seq !== requestSeq) return; // superseded by a newer photo
            extracting = false;
            setExtractBusy(false);
            setStatus("Network error during extraction — photo is still attached; enter scores manually.");
        });
    }

    // Called when the latest pick's downscale settles (success or failure).
    // If a submit was blocked while pending, resume it — requestSubmit re-runs
    // every submit handler (including the guard below, which now sees the
    // settled state, and any tie-check confirms on the page).
    function settled() {
        pending = false;
        if (!waitingToSubmit || !form) return;
        waitingToSubmit = false;
        if (form.requestSubmit) form.requestSubmit();
        else form.submit();
    }

    function handleFile(file) {
        if (!file) return;
        var seq = ++requestSeq;
        pending = true;
        // A new pick supersedes any in-flight extraction: its response will
        // bail on the seq check (and so never clears this flag itself) — so
        // reset the busy UX here; extract() re-engages it if this pick runs.
        extracting = false;
        setExtractBusy(false);
        lastPickFailed = false;
        waitingToSubmit = false; // a new pick supersedes a blocked submit
        setAttachState("pending", "Processing photo…");
        setStatus("");
        notesEl.textContent = "";
        downscale(file, function (dataUrl) {
            if (seq !== requestSeq) return; // superseded by a newer photo
            if (!dataUrl) {
                // Failed decode: make sure no stale photo from an earlier
                // pick rides along — the indicator says "no photo", so the
                // form state must match.
                dataField.value = "";
                mimeField.value = "";
                preview.removeAttribute("src");
                preview.style.display = "none";
                lastPickFailed = true;
                setAttachState("error", DECODE_ERROR_MSG);
                settled();
                return;
            }
            var base64 = dataUrl.split(",")[1];
            dataField.value = base64;
            mimeField.value = "image/jpeg";
            preview.src = dataUrl;
            preview.style.display = "";
            setAttachState("success", "Photo attached ✓ — it will be saved with the cup.");
            if (opts.extractUrl) {
                extract(base64, seq);
            }
            settled();
        });
    }

    // Wire the visible buttons to the hidden file inputs. The buttons ship
    // disabled in the markup, so if this script fails to load the picker
    // never opens — no pick can happen without the guards below in place.
    block.querySelectorAll("button[data-photo-input]").forEach(function (btn) {
        var input = document.getElementById(btn.getAttribute("data-photo-input"));
        if (!input) return;
        btn.disabled = false;
        btn.addEventListener("click", function () {
            input.click();
        });
    });

    ["photo-take", "photo-pick"].forEach(function (id) {
        var input = document.getElementById(id);
        if (!input) return;
        input.addEventListener("change", function () {
            handleFile(input.files && input.files[0]);
            input.value = ""; // allow re-picking the same file
        });
    });

    // True iff at least one submittable score field has a value — the same
    // minimum the server enforces (disabled inputs aren't submitted, matching
    // the removed-row handling on the manual form). Nothing more: all other
    // validation stays server-side.
    function hasAnyScore() {
        var inputs = form.querySelectorAll(".score-input");
        for (var i = 0; i < inputs.length; i++) {
            if (!inputs[i].disabled && inputs[i].value.trim() !== "") return true;
        }
        return false;
    }

    // Submit guard: never let the form race or silently drop the photo.
    if (form) {
        form.addEventListener("submit", function (e) {
            if (pending) {
                // Downscale still in flight — hold the submit and resume it
                // automatically when the pick settles.
                e.preventDefault();
                waitingToSubmit = true;
                setAttachState("pending", "Finishing photo — submitting in a moment…");
                return;
            }
            if (extracting) {
                // Extraction still in flight — block, but do NOT auto-resume:
                // resuming would submit model-filled scores the user never
                // saw, and this feature never auto-submits. The user reviews
                // the filled scores and submits themselves. (A downscale
                // auto-resume can land here — it just shows this message and
                // waits for the user; no loop, since waitingToSubmit stays
                // false.)
                e.preventDefault();
                setStatus(
                    "Still reading scores from the photo — they'll fill in " +
                    "shortly; review them, then submit."
                );
                return;
            }
            if (!hasAnyScore()) {
                // The server rejects an all-empty submit with a flash +
                // redirect that would wipe the attached photo and all form
                // state — fail fast client-side instead.
                e.preventDefault();
                setStatus(
                    "Enter scores first — or wait for the photo scores to fill in."
                );
                return;
            }
            if (lastPickFailed && !dataField.value) {
                if (!confirm("Your photo didn't attach — submit without it?")) {
                    e.preventDefault();
                }
            }
        });
    }
};
