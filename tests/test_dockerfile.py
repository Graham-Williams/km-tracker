"""The Dockerfile copies an explicit allowlist of top-level files (deliberately
not `*.py`). A local module that app.py imports but the allowlist omits only
fails at container start (`ModuleNotFoundError` -> crash-loop on the box), long
after the fast unit job went green. This test closes that gap: every local
top-level module app.py imports must appear on a `COPY ... ./` line.
"""

import ast
import os
import re

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _dockerfile_root_copies():
    """Files copied into the image root by `COPY <files...> ./` (or `.`)."""
    copied = set()
    with open(os.path.join(REPO_ROOT, "Dockerfile")) as f:
        for line in f:
            line = line.strip()
            if not line.startswith("COPY "):
                continue
            parts = line.split()[1:]
            if len(parts) < 2 or parts[-1] not in ("./", "."):
                continue
            copied.update(parts[:-1])
    return copied


def _local_modules_imported_by(filename):
    """Top-level `import x` / `from x import ...` names in `filename` that
    resolve to a `<name>.py` in the repo root (i.e. our own modules)."""
    with open(os.path.join(REPO_ROOT, filename)) as f:
        tree = ast.parse(f.read(), filename)
    names = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return {n for n in names if os.path.isfile(os.path.join(REPO_ROOT, n + ".py"))}


def test_every_local_module_app_imports_is_copied_into_the_image():
    copied = _dockerfile_root_copies()
    assert "app.py" in copied
    local = _local_modules_imported_by("app.py")
    # Sanity: the known set, so an empty parse can't pass vacuously.
    assert {"db", "maps", "extraction", "line_finder"} <= local
    missing = sorted(f"{m}.py" for m in local if f"{m}.py" not in copied)
    assert not missing, (
        f"Dockerfile COPY allowlist is missing {missing} — app.py imports them, "
        f"so the image would crash at start with ModuleNotFoundError."
    )


def test_local_modules_imported_transitively_are_copied_too():
    """Modules our modules import (e.g. line_finder -> maps) must ship too."""
    copied = _dockerfile_root_copies()
    seen, todo = set(), ["app.py"]
    while todo:
        fname = todo.pop()
        if fname in seen:
            continue
        seen.add(fname)
        for mod in _local_modules_imported_by(fname):
            todo.append(f"{mod}.py")
    missing = sorted(f for f in seen if f not in copied)
    assert not missing, f"Dockerfile COPY allowlist is missing {missing}"


def test_schema_ships_in_the_image():
    # db.init_db() reads schema.sql next to db.py at container start.
    assert "schema.sql" in _dockerfile_root_copies()


def test_allowlist_stays_explicit():
    with open(os.path.join(REPO_ROOT, "Dockerfile")) as f:
        text = f.read()
    assert not re.search(r"^COPY\s+.*\*\.py", text, re.M), "keep the COPY allowlist explicit"
