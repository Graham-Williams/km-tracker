/*
 * Photo score entry.
 *
 * Wires up the "Take photo" / "Upload photo" controls on a score form:
 * downscales the chosen image on a canvas (max 1200px long edge, JPEG ~0.8),
 * stores the base64 in the form's hidden photo_data field so the photo is
 * saved with the cup on submit, and — when extraction is enabled — POSTs it
 * to /extract-scores and pre-fills matching score inputs. It NEVER submits
 * the form; the human always reviews first. Manual entry works regardless of
 * what happens here.
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
    var statusEl = block.querySelector(".photo-status");
    var notesEl = block.querySelector(".photo-notes");
    var preview = block.querySelector(".photo-preview");
    var dataField = document.getElementById("photo-data");
    var mimeField = document.getElementById("photo-mime");

    var MAX_EDGE = 1200;
    var JPEG_QUALITY = 0.8;

    function setStatus(msg) {
        statusEl.textContent = msg;
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

    function extract(base64) {
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
            setStatus("Network error during extraction — photo is still attached; enter scores manually.");
        });
    }

    function handleFile(file) {
        if (!file) return;
        setStatus("Processing photo…");
        notesEl.textContent = "";
        downscale(file, function (dataUrl) {
            if (!dataUrl) {
                setStatus("Couldn't read that image — try another file.");
                return;
            }
            var base64 = dataUrl.split(",")[1];
            dataField.value = base64;
            mimeField.value = "image/jpeg";
            preview.src = dataUrl;
            preview.style.display = "";
            if (opts.extractUrl) {
                extract(base64);
            } else {
                setStatus("Photo attached — it will be saved with the cup.");
            }
        });
    }

    ["photo-take", "photo-pick"].forEach(function (id) {
        var input = document.getElementById(id);
        if (!input) return;
        input.addEventListener("change", function () {
            handleFile(input.files && input.files[0]);
            input.value = ""; // allow re-picking the same file
        });
    });
};
