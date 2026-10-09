"""Optional appliance branding from a directory outside the source checkout."""
import json
import re
from pathlib import Path

from flask import Response, abort, send_from_directory, url_for

DEFAULT_PALETTE = {
    "bg": "#08101c",
    "bg-deep": "#050b13",
    "surface": "#0d1726",
    "surface-2": "#111e30",
    "surface-3": "#172438",
    "surface-hover": "#1a2a41",
    "border": "#213149",
    "border-strong": "#30445f",
    "text": "#edf4fb",
    "text-soft": "#c1cedd",
    "muted": "#7f91a8",
    "muted-2": "#5f7188",
    "accent": "#36d6b0",
    "accent-strong": "#16b991",
    "blue": "#5da8ff",
    "warning": "#f3b84b",
    "danger": "#ff6b7a",
    "success": "#54d889"
}
COLOR_ALIASES = {
    "showroom-483521": "surface-3",
    "showroom-09111e": "bg",
    "showroom-eef5ff": "text",
    "showroom-142d45": "surface-3",
    "showroom-8baecf": "muted",
    "showroom-b0c5db": "text-soft",
    "showroom-31475e": "border-strong",
    "showroom-6aebbd": "success",
    "showroom-2f7c66": "success",
    "showroom-ffc58a": "warning",
    "showroom-b0753e": "warning",
    "showroom-101e30": "surface",
    "showroom-263c54": "border",
    "showroom-a5b9cf": "muted",
    "showroom-72b8ff": "blue",
    "showroom-183e35": "surface-3",
    "showroom-7cebc3": "success",
    "showroom-ffd28f": "warning",
    "showroom-4a2530": "surface-3",
    "showroom-ff9dac": "danger",
    "showroom-75baff": "blue",
    "showroom-72e7be": "accent",
    "showroom-8ba4bf": "muted",
    "color-36d6b0": "accent",
    "color-5da8ff": "blue",
    "color-050b13": "bg-deep",
    "color-9fb0c4": "text-soft",
    "color-eafff9": "text",
    "color-30445f": "border-strong",
    "color-d7a946": "warning",
    "color-f3b84b": "warning",
    "color-8295ac": "muted",
    "color-60738b": "muted-2",
    "color-213149": "border",
    "color-08101c": "bg",
    "color-0d1726": "surface",
    "color-54d889": "success",
    "color-ff6b7a": "danger",
    "color-ffd1d6": "text",
    "color-c9fff1": "text",
    "color-ffdce0": "text",
    "color-45617f": "muted-2",
    "color-042018": "bg",
    "color-4ce2bd": "accent",
    "color-041812": "bg-deep",
    "color-ffc4cb": "text-soft",
    "color-ffe1a5": "text-soft",
    "color-c8f8d9": "text-soft",
    "color-ffd0d5": "text",
    "color-cfe5ff": "text",
    "color-111e30": "surface-2",
    "color-e7eef7": "text",
    "color-baf4cf": "text-soft",
    "color-ffe0a0": "text-soft",
    "color-ffc3ca": "text-soft",
    "color-7890aa": "muted",
    "color-bff5d1": "text-soft",
    "color-eaf2fb": "text",
    "color-091321": "bg",
    "color-17263a": "surface-3",
    "color-7f91a8": "muted",
    "color-59708c": "muted-2",
    "color-0a1422": "surface",
    "color-07111d": "bg",
    "color-a9b8ca": "text-soft",
    "color-45607e": "muted-2",
    "color-38516f": "border-strong",
    "color-edf4fb": "text",
    "color-dce7f3": "text",
    "color-a9bbcf": "text-soft",
    "color-3b526f": "border-strong",
    "color-08121f": "bg",
    "color-3d5674": "border-strong",
    "color-39516e": "border-strong",
    "color-081c1e": "surface",
    "color-173034": "surface-3",
    "color-06121f": "bg",
    "color-ff9f6b": "warning"
}
DEFAULT_BRAND = {
    "name": "NetEm WAN Lab",
    "subtitle": "Resilience test platform",
    "mark": "N",
}
ASSET_SUFFIXES = {".svg", ".png", ".jpg", ".jpeg", ".webp", ".ico", ".woff", ".woff2"}
HEX_COLOR = re.compile(r"#[0-9a-fA-F]{6}")
FONT_NAME = re.compile(r"[A-Za-z][A-Za-z0-9 -]{0,63}")


def _asset(root, filename, suffixes):
    if not isinstance(filename, str) or not filename or "\\" in filename:
        raise ValueError("Invalid branding asset name")
    path = (root / "assets" / filename).resolve()
    if not path.is_relative_to((root / "assets").resolve()) or not path.is_file():
        raise ValueError("Branding asset must exist inside assets/")
    if path.suffix.lower() not in suffixes:
        raise ValueError("Unsupported branding asset type")
    if not re.fullmatch(r"[A-Za-z0-9_./-]+", filename):
        raise ValueError("Invalid branding asset name")
    return filename


def load_pack(directory):
    root = Path(directory).resolve()
    data = json.loads((root / "branding.json").read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Branding manifest must be an object")
    brand = dict(DEFAULT_BRAND)
    for key in DEFAULT_BRAND:
        value = data.get(key, brand[key])
        if not isinstance(value, str) or not value.strip() or len(value) > 120:
            raise ValueError("Invalid branding text")
        brand[key] = value
    brand["assets"] = set()
    for key in ("logo", "favicon"):
        if data.get(key):
            brand[key] = _asset(root, data[key], ASSET_SUFFIXES - {".woff", ".woff2"})
            brand["assets"].add(brand[key])
    tokens = data.get("tokens", {})
    if not isinstance(tokens, dict):
        raise ValueError("Branding tokens must be an object")
    for key, value in tokens.items():
        if key not in DEFAULT_PALETTE or not isinstance(value, str) or not HEX_COLOR.fullmatch(value):
            raise ValueError("Invalid branding colour token")
    brand["tokens"] = {**DEFAULT_PALETTE, **tokens}
    family = data.get("font_family", "Arial")
    if not isinstance(family, str) or not FONT_NAME.fullmatch(family):
        raise ValueError("Invalid branding font family")
    brand["font_family"] = family
    brand["fonts"] = []
    fonts = data.get("fonts", [])
    if not isinstance(fonts, list) or len(fonts) > 12:
        raise ValueError("Invalid branding fonts")
    for font in fonts:
        if not isinstance(font, dict):
            raise ValueError("Invalid branding font")
        filename = _asset(root, font.get("file"), {".woff", ".woff2"})
        weight = font.get("weight", 400)
        if type(weight) is not int or not 100 <= weight <= 900:
            raise ValueError("Invalid branding font weight")
        brand["fonts"].append({"file": filename, "weight": weight})
        brand["assets"].add(filename)
    return root, brand


def stylesheet(brand):
    declarations = []
    for key, value in brand["tokens"].items():
        declarations.append(f"--{key}:{value}")
    for key, target in COLOR_ALIASES.items():
        value = brand["tokens"][target]
        channels = ", ".join(str(int(value[index:index + 2], 16)) for index in (1, 3, 5))
        declarations.extend((f"--{key}:var(--{target})", f"--{key}-rgb:{channels}"))
    for key in ("accent", "blue", "warning", "danger", "success"):
        value = brand["tokens"][key]
        channels = ", ".join(str(int(value[index:index + 2], 16)) for index in (1, 3, 5))
        declarations.append(f"--{key}-soft:rgba({channels},.12)")
    declarations.append(f'--font-family:"{brand["font_family"]}",Arial,sans-serif')
    rules = [":root{" + ";".join(declarations) + "}"]
    for font in brand["fonts"]:
        source = url_for("branding_asset", filename=font["file"])
        rules.append(
            f'@font-face{{font-family:"{brand["font_family"]}";src:url("{source}");'
            f'font-weight:{font["weight"]};font-style:normal;font-display:swap}}'
        )
    rules.append(".btn-primary,.btn-primary:hover{color:#fff;background:var(--accent-strong)}")
    rules.append(".nav-link.active,.docs-side-link.active{color:var(--text)}")
    return "\n".join(rules)


def init_app(app, directory=None):
    root = None
    brand = dict(DEFAULT_BRAND)
    if directory:
        try:
            root, brand = load_pack(directory)
        except (OSError, ValueError, TypeError):
            app.logger.warning("Branding pack could not be loaded; using default appearance")
    app.extensions["branding"] = brand

    @app.context_processor
    def branding_context():
        return {"branding": brand, "branding_enabled": root is not None}

    @app.get("/branding/theme.css")
    def branding_stylesheet():
        if root is None:
            abort(404)
        response = Response(stylesheet(brand), mimetype="text/css")
        response.headers["Cache-Control"] = "no-cache"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/branding/assets/<path:filename>")
    def branding_asset(filename):
        if root is None or filename not in brand["assets"]:
            abort(404)
        try:
            _asset(root, filename, ASSET_SUFFIXES)
        except ValueError:
            abort(404)
        response = send_from_directory(root / "assets", filename)
        response.headers["Cache-Control"] = "no-cache"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response
