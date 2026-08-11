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
 * This is the PURE-cup entry point. A mixed (Wii + Switch) cup has two results
 * screens, one per console, and uses `initBlockPhoto` at the bottom of this
 * file instead — one panel per console block, each filling only its own half.
 *
 * Auto-fill is still suppressed entirely when a response carries
 * `partial_half: true` — a BLOCKLESS mixed-cup request, where the photographed
 * screen holds only one console's half of the scoring but the field it would
 * fill is the cup TOTAL. Filling half totals there would record silently-wrong
 * numbers that look completely plausible, so those responses render the mapping
 * panel as a READ-ONLY reference list (no dropdowns, no write path to the score
 * inputs) plus an explanatory status line. No UI reaches that path today (the
 * mixed page is per-block), but the server still answers it and the guard is
 * kept as the second lock.
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
// Shared canvas downscale (max 1200px long edge, JPEG ~0.8). Hoisted to module
// scope so the block-photo entry point below reuses the exact same encoder —
// the size budget it produces is what keeps a photo POST under the server's
// 1 MB MAX_CONTENT_LENGTH.
var PHOTO_MAX_EDGE = 1200;
var PHOTO_JPEG_QUALITY = 0.8;

function downscalePhoto(file, cb) {
    var img = new Image();
    var url = URL.createObjectURL(file);
    img.onload = function () {
        var w = img.naturalWidth, h = img.naturalHeight;
        var scale = Math.min(1, PHOTO_MAX_EDGE / Math.max(w, h));
        var canvas = document.createElement("canvas");
        canvas.width = Math.max(1, Math.round(w * scale));
        canvas.height = Math.max(1, Math.round(h * scale));
        canvas.getContext("2d").drawImage(img, 0, 0, canvas.width, canvas.height);
        URL.revokeObjectURL(url);
        cb(canvas.toDataURL("image/jpeg", PHOTO_JPEG_QUALITY));
    };
    img.onerror = function () {
        URL.revokeObjectURL(url);
        cb(null);
    };
    img.src = url;
}

window.initPhotoScore = function (opts) {
    var block = document.getElementById("photo-score");
    if (!block) return;
    var attachEl = block.querySelector(".photo-attach-status");
    var statusEl = block.querySelector(".photo-status");
    var spinnerEl = block.querySelector(".photo-extract-spinner");
    var notesEl = block.querySelector(".photo-notes");
    var preview = block.querySelector(".photo-preview");
    var mappingEl = block.querySelector(".photo-mapping");
    var mappingTitleEl = block.querySelector(".photo-mapping-title");
    var mappingRowsEl = block.querySelector(".photo-mapping-rows");
    // The markup's own title, restored whenever the interactive panel renders.
    var MAPPING_TITLE = mappingTitleEl ? mappingTitleEl.textContent : "";
    var REFERENCE_TITLE = "Rows read from the photo — this console only:";
    var mapWarnEl = block.querySelector(".photo-map-warning");
    var mapUnassignedEl = block.querySelector(".photo-map-unassigned");
    var dataField = document.getElementById("photo-data");
    var mimeField = document.getElementById("photo-mime");
    var form = block.closest("form");
    var submitBtn = form ? form.querySelector('button[type="submit"]') : null;

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

    var downscale = downscalePhoto;

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

    // ---- Mix-and-match mapping panel ------------------------------------
    // After extraction, render one <select> per roster player over ALL rows the
    // model read off the photo — HIGHLIGHTED (human) rows first, each marked ★,
    // then the remaining rows (CPUs) under a divider but still SELECTABLE, so
    // the user can always hand-pick the right row even when highlight detection
    // missed it. Pre-selected to the server's auto-fill; picking a row fills
    // that player's .score-input (via the same input event as typing); a row
    // chosen by one player is disabled in every other dropdown. The count and
    // unassigned banners still key off the HIGHLIGHTED rows as the expected
    // human set. Purely an editing aid — it never submits the form.

    // The roster in DOM order: non-removed score rows with their player id/name.
    function getRoster() {
        var out = [];
        document.querySelectorAll(".score-row").forEach(function (row) {
            if (row.classList.contains("removed")) return;
            var input = row.querySelector(".score-input");
            if (input && input.disabled) return; // removed/inactive row
            var nameEl = row.querySelector(".score-name");
            var pid = row.getAttribute("data-player-id");
            out.push({
                pid: pid,
                name: nameEl ? nameEl.textContent.trim() : "Player " + pid,
                row: row
            });
        });
        return out;
    }

    function clearMapping() {
        if (!mappingEl) return;
        mappingRowsEl.innerHTML = "";
        if (mappingTitleEl) mappingTitleEl.textContent = MAPPING_TITLE;
        if (mapWarnEl) { mapWarnEl.hidden = true; mapWarnEl.textContent = ""; }
        if (mapUnassignedEl) { mapUnassignedEl.hidden = true; mapUnassignedEl.textContent = ""; }
        mappingEl.hidden = true;
    }

    // Read-only variant of the panel, used when the photo covers only PART of
    // the cup's scoring (partial_half — a mixed cup). It lists what the model
    // read off the screen and NOTHING else: no <select>, no writes to any score
    // input. The interactive panel is titled "Map each player to a highlighted
    // row from the photo", which would be instructing the user down a
    // three-tap path to persisting half totals as the cup's scores — directly
    // contradicting the warning above it. Reading the numbers off the photo is
    // the only part of the panel that's safe here, so that's all it does.
    //
    // Deliberately NOT additive (no "add this to the existing value"): silent
    // arithmetic on top of a typed number is a worse footgun than either a
    // plain overwrite or this.
    function renderReferenceRows(rawRows) {
        if (!mappingEl) return;
        mappingRowsEl.innerHTML = "";
        if (mappingTitleEl) mappingTitleEl.textContent = REFERENCE_TITLE;
        rawRows.forEach(function (r) {
            var row = document.createElement("div");
            row.className = "photo-map-row photo-map-readonly";
            var star = r.is_highlighted ? "★ " : "";
            row.textContent =
                star + "P" + r.position + " · " + r.character + " — " + r.points + " pts";
            mappingRowsEl.appendChild(row);
        });
        // The count/unassigned banners describe an assignment that no longer
        // exists here.
        if (mapWarnEl) { mapWarnEl.hidden = true; mapWarnEl.textContent = ""; }
        if (mapUnassignedEl) { mapUnassignedEl.hidden = true; mapUnassignedEl.textContent = ""; }
        mappingEl.hidden = false;
    }

    // Fill (or clear) a player's score input from a chosen row, firing the
    // existing input event so line-sync + placement recalc run. `orows` is the
    // ordered row list (highlighted first) the option value indexes into. The
    // score input is NEVER disabled — this only writes a value, and the user
    // can always overtype it by hand afterward.
    function applySelection(row, value, orows) {
        var input = row.querySelector(".score-input");
        if (!input) return;
        if (value === "") {
            input.value = "";
        } else {
            var idx = parseInt(value, 10);
            if (orows[idx]) input.value = orows[idx].points;
        }
        input.dispatchEvent(new Event("input", { bubbles: true }));
    }

    // Live-update the disabled options (a row assigned to one player is greyed
    // out everywhere else — across ALL rows now, highlighted or not) and the
    // count-mismatch / unassigned banners. The banners still key off the
    // HIGHLIGHTED rows (indices 0..hlCount-1 in the ordered list) as the
    // expected human set; when nothing is highlighted (Switch / detection
    // miss) they're suppressed since there's no reliable human count.
    function refreshMapping(orows, hlCount, roster) {
        var selects = mappingRowsEl.querySelectorAll(".photo-map-select");
        var chosen = {}; // option value -> count
        selects.forEach(function (s) {
            if (s.value !== "") chosen[s.value] = (chosen[s.value] || 0) + 1;
        });
        selects.forEach(function (s) {
            var cur = s.value;
            s.querySelectorAll("option").forEach(function (opt) {
                if (opt.value === "") { opt.disabled = false; return; }
                opt.disabled = !!(chosen[opt.value] && opt.value !== cur);
            });
        });
        if (mapWarnEl) {
            if (hlCount > 0 && hlCount !== roster.length) {
                mapWarnEl.textContent =
                    "Photo shows " + hlCount + " highlighted player" +
                    (hlCount === 1 ? "" : "s") + ", but this cup has " +
                    roster.length + " — check the mapping.";
                mapWarnEl.hidden = false;
            } else {
                mapWarnEl.hidden = true;
                mapWarnEl.textContent = "";
            }
        }
        if (mapUnassignedEl) {
            // Highlighted rows (the first hlCount entries) claimed by nobody.
            var unassigned = 0;
            for (var i = 0; i < hlCount; i++) {
                if (!chosen[String(i)]) unassigned++;
            }
            if (hlCount > 0 && unassigned > 0) {
                mapUnassignedEl.textContent =
                    unassigned + " highlighted player" +
                    (unassigned === 1 ? "" : "s") + " unassigned";
                mapUnassignedEl.hidden = false;
            } else {
                mapUnassignedEl.hidden = true;
                mapUnassignedEl.textContent = "";
            }
        }
    }

    function renderMapping(rawRows, partialHalf) {
        if (!mappingEl) return;
        rawRows = rawRows || [];
        if (!rawRows.length) { clearMapping(); return; }
        // Partial-half photo: reference list only, no write path at all.
        if (partialHalf) { renderReferenceRows(rawRows); return; }
        if (mappingTitleEl) mappingTitleEl.textContent = MAPPING_TITLE;

        // Order: highlighted (human) rows first, then the rest. Option values
        // index into this ordered list. Highlighted rows get a ★ marker; all
        // rows — highlighted or not — remain SELECTABLE so the user can always
        // hand-pick the correct row even when highlight detection missed it.
        var hrows = rawRows.filter(function (r) { return r && r.is_highlighted; });
        var nrows = rawRows.filter(function (r) { return !(r && r.is_highlighted); });
        var orows = hrows.concat(nrows);
        var hlCount = hrows.length;

        var roster = getRoster();
        mappingRowsEl.innerHTML = "";

        // Pre-select by reconstructing the auto-match from the already-filled
        // score inputs: each player with a value claims the first unclaimed
        // row whose points equal it. Highlighted rows come first in `orows`, so
        // a highlighted match is preferred (mirroring the server auto-fill).
        // Blank players stay blank. This reads inputs but never writes them, so
        // a value the user typed by hand is never clobbered by the render.
        var claimed = {};
        var preselect = {};
        roster.forEach(function (p) {
            var input = p.row.querySelector(".score-input");
            var val = input && input.value.trim() !== "" ? parseInt(input.value, 10) : null;
            var chosen = -1;
            if (val !== null && !isNaN(val)) {
                for (var i = 0; i < orows.length; i++) {
                    if (!claimed[i] && orows[i].points === val) { chosen = i; break; }
                }
            }
            if (chosen !== -1) claimed[chosen] = true;
            preselect[p.pid] = chosen;
        });

        function makeOption(r, i) {
            var opt = document.createElement("option");
            opt.value = String(i);
            var star = r.is_highlighted ? "★ " : "";
            opt.textContent =
                star + "P" + r.position + " · " + r.character + " — " + r.points + " pts";
            return opt;
        }

        roster.forEach(function (p) {
            var wrap = document.createElement("div");
            wrap.className = "photo-map-row";
            var name = document.createElement("span");
            name.className = "photo-map-name";
            name.textContent = p.name;
            var sel = document.createElement("select");
            sel.className = "photo-map-select";
            sel.setAttribute("data-player-id", p.pid);
            var blank = document.createElement("option");
            blank.value = "";
            blank.textContent = "— leave blank —";
            sel.appendChild(blank);
            // Highlighted (human) rows first, under a labeled group when there
            // are also non-highlighted rows to separate them from.
            if (hlCount && nrows.length) {
                var gHuman = document.createElement("optgroup");
                gHuman.label = "Human players ★";
                hrows.forEach(function (r, i) { gHuman.appendChild(makeOption(r, i)); });
                sel.appendChild(gHuman);
                var gOther = document.createElement("optgroup");
                gOther.label = "Other rows";
                nrows.forEach(function (r, i) { gOther.appendChild(makeOption(r, hlCount + i)); });
                sel.appendChild(gOther);
            } else {
                // All-highlighted or all-plain: no divider needed.
                orows.forEach(function (r, i) { sel.appendChild(makeOption(r, i)); });
            }
            sel.value = preselect[p.pid] >= 0 ? String(preselect[p.pid]) : "";
            sel.addEventListener("change", function () {
                applySelection(p.row, sel.value, orows);
                refreshMapping(orows, hlCount, roster);
            });
            wrap.appendChild(name);
            wrap.appendChild(sel);
            mappingRowsEl.appendChild(wrap);
        });

        mappingEl.hidden = false;
        refreshMapping(orows, hlCount, roster);
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
                clearMapping();
                return;
            }
            var body = res.body || {};
            // partial_half: the photo covers only PART of the cup's scoring
            // (a mixed cup — the second console restarted at zero), so its
            // numbers are half totals. NEVER auto-fill those: they look
            // plausible next to a cup total and would be recorded silently
            // wrong. The server also sends scores: {} for this case; the guard
            // here is the second lock, not the only one.
            var partialHalf = !!body.partial_half;
            var filled = partialHalf ? 0 : fillScores(body.scores || {});
            // Build the mix-and-match panel AFTER fillScores so pre-selection
            // can read the auto-filled score inputs. On a partial-half photo
            // this renders the READ-ONLY reference list instead.
            renderMapping(body.raw_rows || [], partialHalf);
            if (partialHalf) {
                setStatus(
                    "Read " + (body.raw_rows || []).length + " row" +
                    ((body.raw_rows || []).length === 1 ? "" : "s") +
                    " from the photo — nothing was filled in. This screen shows " +
                    "one console's half only; add both halves together and enter " +
                    "each player's combined total."
                );
                notesEl.textContent = "";
                return;
            }
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
            clearMapping();
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
        clearMapping();
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

/*
 * Per-console block photos (MIXED cups only).
 *
 * A mixed cup is two consoles' results screens, each starting from zero, so it
 * gets one independent panel per BLOCK. This is a SECOND entry point rather
 * than a mode of initPhotoScore on purpose: that function owns the shipped
 * pure-cup flow and its documented silent-drop guards, and none of them should
 * shift under a feature that doesn't touch pure cups.
 *
 * What is different here, and why:
 *   - The photo POSTs on its OWN request to /cups/<id>/photo/<block> instead of
 *     riding the form in a hidden field. Two base64 photos would flirt with the
 *     server's 1 MB MAX_CONTENT_LENGTH (413 -> a flash-less error page), and
 *     the swap photo is taken during race 2, long before the completion form
 *     exists. A side effect worth naming: the mixed form carries no photo
 *     payload at all, so there is no attach/submit race to guard against — the
 *     server confirms each photo as it lands.
 *   - Auto-fill writes into that block's OWN input, never the total. The total
 *     is computed from both halves by the page, so a filled block can never
 *     masquerade as a cup total (the failure this whole feature prevents).
 *   - Extraction is per photo, with the server resolving THAT block's console.
 *     Never both editions against one screen: a player's other-console main can
 *     be a CPU here, and Switch rows carry no highlight to veto the match.
 *   - Nothing is ever read-only: every block input stays hand-editable and the
 *     form is never auto-submitted. Extraction reads digits reliably but cannot
 *     tell a stale photo (one taken a race early reads perfectly while being 10
 *     points short), so the visible per-block breakdown is the real guard.
 *
 * Usage (once per block):
 *   initBlockPhoto({
 *     block: 1, cupId: 7, label: "Wii",
 *     uploadUrl: "/cups/7/photo/1",
 *     extractUrl: "/extract-scores" or null   // null = attach-only mode
 *   });
 */

// Shared across the panels on a page: how many block photos are mid-flight.
// Both panels drive the same submit button, so a per-panel flag would let the
// first one to finish re-enable it while the other is still working.
var blockPhotoBusy = 0;

window.initBlockPhoto = function (opts) {
    var block = opts.block;
    var root = document.getElementById("photo-block-" + block);
    if (!root) return;

    var attachEl = root.querySelector(".photo-attach-status");
    var statusEl = root.querySelector(".photo-status");
    var spinnerEl = root.querySelector(".photo-extract-spinner");
    var notesEl = root.querySelector(".photo-notes");
    var preview = root.querySelector(".photo-preview");
    var mappingEl = root.querySelector(".photo-mapping");
    var mappingRowsEl = root.querySelector(".photo-mapping-rows");
    var mapWarnEl = root.querySelector(".photo-map-warning");
    var mapUnassignedEl = root.querySelector(".photo-map-unassigned");
    var readBtn = root.querySelector(".photo-read-btn");
    // Every "Upload photo" control for this block, wherever it lives (the race
    // page repeats it inside the swap-reminder modal) — they all relabel to
    // "Replace photo" once a photo is saved.
    var pickBtns = document.querySelectorAll(
        '[data-photo-input="photo-pick-' + block + '"]'
    );
    var form = root.closest("form");
    var submitBtn = form ? form.querySelector('button[type="submit"]') : null;

    var DECODE_ERROR_MSG =
        "Couldn't read that image — try taking the photo with the camera, " +
        "or use a JPEG/PNG.";

    // Bumped per pick; async work bails when a newer pick has superseded it.
    var requestSeq = 0;
    var busy = false;

    function setStatus(msg) {
        statusEl.textContent = msg;
    }

    function setBusy(isBusy) {
        if (isBusy === busy) return;
        busy = isBusy;
        blockPhotoBusy += isBusy ? 1 : -1;
        if (blockPhotoBusy < 0) blockPhotoBusy = 0;
        if (spinnerEl) spinnerEl.hidden = !isBusy;
        if (submitBtn) submitBtn.disabled = blockPhotoBusy > 0;
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

    // --- Writing into THIS block's inputs (never the total) ----------------

    function blockInput(pid) {
        var row = document.querySelector('.score-row[data-player-id="' + pid + '"]');
        if (!row || row.classList.contains("removed")) return null;
        return row.querySelector('.block-score-input[data-block="' + block + '"]');
    }

    function fillBlockScores(scores) {
        var filled = 0;
        Object.keys(scores).forEach(function (pid) {
            var input = blockInput(pid);
            if (!input) return;
            input.value = scores[pid];
            // Same event a human typing would fire: the page recomputes the
            // total from both halves and re-runs placements/tiebreakers.
            input.dispatchEvent(new Event("input", { bubbles: true }));
            filled++;
        });
        return filled;
    }

    // --- Mix-and-match mapping panel (this block only) ---------------------

    function getRoster() {
        var out = [];
        document.querySelectorAll(".score-row").forEach(function (row) {
            if (row.classList.contains("removed")) return;
            var input = row.querySelector('.block-score-input[data-block="' + block + '"]');
            if (!input || input.disabled) return;
            var nameEl = row.querySelector(".score-name");
            var pid = row.getAttribute("data-player-id");
            out.push({
                pid: pid,
                name: nameEl ? nameEl.textContent.trim() : "Player " + pid,
                input: input
            });
        });
        return out;
    }

    function clearMapping() {
        if (!mappingEl) return;
        mappingRowsEl.innerHTML = "";
        if (mapWarnEl) { mapWarnEl.hidden = true; mapWarnEl.textContent = ""; }
        if (mapUnassignedEl) { mapUnassignedEl.hidden = true; mapUnassignedEl.textContent = ""; }
        mappingEl.hidden = true;
    }

    function applySelection(input, value, orows) {
        if (value === "") {
            input.value = "";
        } else {
            var idx = parseInt(value, 10);
            if (orows[idx]) input.value = orows[idx].points;
        }
        input.dispatchEvent(new Event("input", { bubbles: true }));
    }

    // A row assigned to one player is greyed out in every other dropdown; the
    // banners key off the HIGHLIGHTED rows as the expected human set (Switch
    // photos have none, so they're simply suppressed there).
    function refreshMapping(orows, hlCount, roster) {
        var selects = mappingRowsEl.querySelectorAll(".photo-map-select");
        var chosen = {};
        selects.forEach(function (s) {
            if (s.value !== "") chosen[s.value] = (chosen[s.value] || 0) + 1;
        });
        selects.forEach(function (s) {
            var cur = s.value;
            s.querySelectorAll("option").forEach(function (opt) {
                if (opt.value === "") { opt.disabled = false; return; }
                opt.disabled = !!(chosen[opt.value] && opt.value !== cur);
            });
        });
        if (mapWarnEl) {
            if (hlCount > 0 && hlCount !== roster.length) {
                mapWarnEl.textContent =
                    "Photo shows " + hlCount + " highlighted player" +
                    (hlCount === 1 ? "" : "s") + ", but this cup has " +
                    roster.length + " — check the mapping.";
                mapWarnEl.hidden = false;
            } else {
                mapWarnEl.hidden = true;
                mapWarnEl.textContent = "";
            }
        }
        if (mapUnassignedEl) {
            var unassigned = 0;
            for (var i = 0; i < hlCount; i++) {
                if (!chosen[String(i)]) unassigned++;
            }
            if (hlCount > 0 && unassigned > 0) {
                mapUnassignedEl.textContent =
                    unassigned + " highlighted player" +
                    (unassigned === 1 ? "" : "s") + " unassigned";
                mapUnassignedEl.hidden = false;
            } else {
                mapUnassignedEl.hidden = true;
                mapUnassignedEl.textContent = "";
            }
        }
    }

    function renderMapping(rawRows) {
        if (!mappingEl) return;
        rawRows = rawRows || [];
        if (!rawRows.length) { clearMapping(); return; }

        var hrows = rawRows.filter(function (r) { return r && r.is_highlighted; });
        var nrows = rawRows.filter(function (r) { return !(r && r.is_highlighted); });
        var orows = hrows.concat(nrows);
        var hlCount = hrows.length;

        var roster = getRoster();
        mappingRowsEl.innerHTML = "";

        // Pre-select by reconstructing the auto-match from the already-filled
        // block inputs: each player with a value claims the first unclaimed row
        // whose points equal it. Reads inputs, never writes them, so a value
        // typed by hand survives the render.
        var claimed = {};
        var preselect = {};
        roster.forEach(function (p) {
            var val = p.input.value.trim() !== "" ? parseInt(p.input.value, 10) : null;
            var chosen = -1;
            if (val !== null && !isNaN(val)) {
                for (var i = 0; i < orows.length; i++) {
                    if (!claimed[i] && orows[i].points === val) { chosen = i; break; }
                }
            }
            if (chosen !== -1) claimed[chosen] = true;
            preselect[p.pid] = chosen;
        });

        function makeOption(r, i) {
            var opt = document.createElement("option");
            opt.value = String(i);
            var star = r.is_highlighted ? "★ " : "";
            opt.textContent =
                star + "P" + r.position + " · " + r.character + " — " + r.points + " pts";
            return opt;
        }

        roster.forEach(function (p) {
            var wrap = document.createElement("div");
            wrap.className = "photo-map-row";
            var name = document.createElement("span");
            name.className = "photo-map-name";
            name.textContent = p.name;
            var sel = document.createElement("select");
            sel.className = "photo-map-select";
            sel.setAttribute("data-player-id", p.pid);
            sel.setAttribute("data-block", String(block));
            var blank = document.createElement("option");
            blank.value = "";
            blank.textContent = "— leave blank —";
            sel.appendChild(blank);
            if (hlCount && nrows.length) {
                var gHuman = document.createElement("optgroup");
                gHuman.label = "Human players ★";
                hrows.forEach(function (r, i) { gHuman.appendChild(makeOption(r, i)); });
                sel.appendChild(gHuman);
                var gOther = document.createElement("optgroup");
                gOther.label = "Other rows";
                nrows.forEach(function (r, i) { gOther.appendChild(makeOption(r, hlCount + i)); });
                sel.appendChild(gOther);
            } else {
                orows.forEach(function (r, i) { sel.appendChild(makeOption(r, i)); });
            }
            sel.value = preselect[p.pid] >= 0 ? String(preselect[p.pid]) : "";
            sel.addEventListener("change", function () {
                applySelection(p.input, sel.value, orows);
                refreshMapping(orows, hlCount, roster);
            });
            wrap.appendChild(name);
            wrap.appendChild(sel);
            mappingRowsEl.appendChild(wrap);
        });

        mappingEl.hidden = false;
        refreshMapping(orows, hlCount, roster);
    }

    // --- Extraction --------------------------------------------------------

    // payload carries either a fresh image or nothing (server re-reads the
    // photo it already stored for this block — the swap photo).
    function extract(payload, seq) {
        setBusy(true);
        setStatus("Reading " + opts.label + " scores from the photo…");
        payload.cup_id = opts.cupId;
        payload.block = block;
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
            setBusy(false);
            if (!res.ok) {
                var msg = (res.body && res.body.error) ||
                    "Extraction failed (" + res.status + ")";
                setStatus(msg + " — the photo is saved; enter this half by hand.");
                clearMapping();
                return;
            }
            var body = res.body || {};
            var filled = fillBlockScores(body.scores || {});
            renderMapping(body.raw_rows || []);
            setStatus(
                "Filled " + filled + " " + opts.label + " score" +
                (filled === 1 ? "" : "s") + " — check them against the photo " +
                "before submitting."
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
            if (seq !== requestSeq) return;
            setBusy(false);
            setStatus("Network error during extraction — the photo is saved; enter this half by hand.");
            clearMapping();
        });
    }

    // --- Upload ------------------------------------------------------------

    function upload(base64, seq) {
        setBusy(true);
        setAttachState("pending", "Saving " + opts.label + " photo…");
        fetch(opts.uploadUrl, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ image: base64, mime_type: "image/jpeg" })
        }).then(function (res) {
            return res.json().catch(function () { return {}; }).then(function (body) {
                return { ok: res.ok, status: res.status, body: body };
            });
        }).then(function (res) {
            if (seq !== requestSeq) return;
            setBusy(false);
            if (!res.ok || !res.body || !res.body.ok) {
                var msg = (res.body && res.body.error) ||
                    "Couldn't save the photo (" + res.status + ")";
                setAttachState("error", msg + " — try again, or enter this half by hand.");
                return;
            }
            // Server-confirmed, so the label can say so honestly.
            setAttachState("success", (res.body.label || opts.label) + " photo saved ✓");
            pickBtns.forEach(function (btn) { btn.textContent = "Replace photo"; });
            if (readBtn) { readBtn.hidden = false; readBtn.disabled = false; }
            if (opts.extractUrl) extract({ image: base64, mime_type: "image/jpeg" }, seq);
        }).catch(function () {
            if (seq !== requestSeq) return;
            setBusy(false);
            setAttachState("error", "Network error saving the photo — try again, or enter this half by hand.");
        });
    }

    function handleFile(file) {
        if (!file) return;
        var seq = ++requestSeq;
        setBusy(true);
        setAttachState("pending", "Processing photo…");
        setStatus("");
        notesEl.textContent = "";
        clearMapping();
        downscalePhoto(file, function (dataUrl) {
            if (seq !== requestSeq) return;
            setBusy(false);
            if (!dataUrl) {
                setAttachState("error", DECODE_ERROR_MSG);
                return;
            }
            preview.src = dataUrl;
            preview.style.display = "";
            upload(dataUrl.split(",")[1], seq);
        });
    }

    // Wire the visible buttons to the hidden file inputs. They ship disabled,
    // so a dead script means the picker never opens. Queried document-wide (by
    // THIS block's input ids, so panels can't cross-wire) because the race page
    // repeats the same control inside the swap-reminder modal, outside the
    // panel — both must drive the same pick.
    document.querySelectorAll(
        '[data-photo-input="photo-take-' + block + '"],' +
        '[data-photo-input="photo-pick-' + block + '"]'
    ).forEach(function (btn) {
        var input = document.getElementById(btn.getAttribute("data-photo-input"));
        if (!input) return;
        btn.disabled = false;
        btn.addEventListener("click", function () { input.click(); });
    });

    ["photo-take-" + block, "photo-pick-" + block].forEach(function (id) {
        var input = document.getElementById(id);
        if (!input) return;
        input.addEventListener("change", function () {
            handleFile(input.files && input.files[0]);
            input.value = ""; // allow re-picking the same file
        });
    });

    // "Read scores" (this block's stored photo): no image in the body — the server reads
    // the photo already stored for this block (typically taken at the swap).
    if (readBtn && opts.extractUrl) {
        readBtn.disabled = false;
        readBtn.addEventListener("click", function () {
            var seq = ++requestSeq;
            notesEl.textContent = "";
            clearMapping();
            extract({}, seq);
        });
    }

    // Block a submit while this panel is still saving/reading a photo — the
    // scores would not be in the form yet. Never auto-resumes: the human
    // reviews what got filled and submits themselves.
    if (form) {
        form.addEventListener("submit", function (e) {
            if (!busy) return;
            e.preventDefault();
            setStatus(
                "Still working on the " + opts.label + " photo — it'll finish " +
                "shortly; review the scores, then submit."
            );
        });
    }
};
