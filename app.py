import os

from dotenv import load_dotenv
from flask import Flask, render_template

from db import init_db

load_dotenv()

app = Flask(__name__)


@app.route("/")
def index():
    return render_template("index.html")


if __name__ == "__main__":
    debug = True
    # In debug mode, Flask's reloader runs this file twice — once in the
    # parent (watcher) and once in the child (actual server). Only init the
    # DB in the child to avoid double backups.
    is_reloader_parent = debug and os.environ.get("WERKZEUG_RUN_MAIN") is None
    if not is_reloader_parent:
        init_db()
    app.run(host="0.0.0.0", port=8080, debug=debug)
