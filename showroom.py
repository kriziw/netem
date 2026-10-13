"""A separate, deliberately small HTTP surface for unattended showroom screens."""
import re
from pathlib import Path

from flask import send_file, Flask, abort, jsonify, render_template, send_from_directory
import branding


def create_showroom_app(snapshot, branding_source=None, video_file=None):
    """`branding_source` is the operator's BrandStore (so brand switches show here too) or a pack directory."""
    root = Path(__file__).resolve().parent
    viewer = Flask("netem_showroom", static_folder=None,
                   template_folder=str(root / "templates"))
    branding.init_app(viewer, branding_source)

    @viewer.before_request
    def read_only():
        from flask import abort, request
        if request.method not in ("GET", "HEAD"):
            abort(405)

    @viewer.after_request
    def response_headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; frame-src https://www.youtube-nocookie.com https://player.vimeo.com; "
            "img-src 'self' https:; font-src 'self' https:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'self'"
        )
        return response

    @viewer.get("/")
    def dashboard():
        return render_template("showroom.html")

    @viewer.get("/api/snapshot")
    def live_snapshot():
        return jsonify(snapshot())

    # Exact routes avoid publishing the operator app's scripts or future assets.
    @viewer.get("/assets/showroom.css")
    def stylesheet():
        return send_from_directory(root / "static", "showroom.css")

    @viewer.get('/assets/branding-live.js')
    def branding_live_script():
        return send_from_directory(root / 'static', 'branding-live.js')

    @viewer.get("/assets/showroom.js")
    def javascript():
        return send_from_directory(root / "static", "showroom.js")

    @viewer.get("/assets/showcase-video")
    def showcase_video():
        # The operator's uploaded video; the waiting screen plays it, with range requests so it can seek.
        path = video_file() if video_file else None
        if path is None:
            abort(404)
        return send_file(path, conditional=True)

    @viewer.get("/assets/time-format.js")
    def time_format_script():
        return send_from_directory(root / "static", "time-format.js")

    @viewer.get("/assets/vendors/<vendor>.png")
    def vendor_mark(vendor):
        if not re.fullmatch(r"[a-z0-9-]{1,40}", vendor) or not (root / "static" / "vendors" / f"{vendor}.png").is_file():
            abort(404)
        return send_from_directory(root / "static" / "vendors", f"{vendor}.png")

    return viewer
