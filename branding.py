"""Optional appliance branding from private brand packs outside the source checkout.

Packs live in a private directory (runtime/brands by default, never in the public
repository). One of them is active; the choice and the packs can change while the
service runs, from Settings -> Branding. A pack installed the original way (a single
directory from NETEM_BRANDING_DIR or runtime/branding) is offered as the "local" pack.
"""
import json
import base64
import hashlib
import re
import shutil
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlsplit

from flask import Response, abort, jsonify, request, send_from_directory, url_for

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


def image_url(value):
    """Remote assets are browser-loaded HTTPS URLs; never fetched by the server."""
    if not isinstance(value, str) or len(value) > 2048 or not re.fullmatch(r"[A-Za-z0-9:/?&=._~%+#@!$()*;,\[\]-]+", value):
        raise ValueError("Use an HTTPS image or font URL without credentials or whitespace.")
    parsed = urlsplit(value)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Use an HTTPS image or font URL without credentials or whitespace.")
    return value


def asset_url(filename, revision=None):
    if filename.startswith('https://'):
        return image_url(filename)
    return url_for('branding_asset', filename=filename, **({'brand': revision} if revision else {}))


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
            if str(data[key]).startswith('https://'):
                brand[key] = image_url(data[key])
            else:
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
    heading = data.get("heading_font_family", family)
    if not isinstance(heading, str) or not FONT_NAME.fullmatch(heading):
        raise ValueError("Invalid heading font family")
    brand["heading_font_family"] = heading
    if data.get("waiting_video") is not None:
        # An optional animation for the showcase's waiting screen. A brand supplies its own, in its private pack.
        brand["waiting_video"] = _asset(root, data["waiting_video"], {".mp4", ".webm"})
    brand["fonts"] = []
    fonts = data.get("fonts", [])
    if not isinstance(fonts, list) or len(fonts) > 12:
        raise ValueError("Invalid branding fonts")
    for font in fonts:
        if not isinstance(font, dict):
            raise ValueError("Invalid branding font")
        filename = image_url(font['file']) if str(font.get('file', '')).startswith('https://') else _asset(root, font.get("file"), {".woff", ".woff2"})
        weight = font.get("weight", 400)
        if type(weight) is not int or not 100 <= weight <= 900:
            raise ValueError("Invalid branding font weight")
        role = font.get("family", "body")
        if role not in ("body", "heading"):
            raise ValueError("Invalid font role")
        brand["fonts"].append({"file": filename, "weight": weight, "family": role})
        if not filename.startswith('https://'):
            brand["assets"].add(filename)
    return root, brand


def stylesheet(brand, revision=None):
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
        source = asset_url(font["file"], revision)
        family = brand.get("heading_font_family", brand["font_family"]) if font.get("family") == "heading" else brand["font_family"]
        rules.append(
            f'@font-face{{font-family:"{family}";src:url("{source}");'
            f'font-weight:{font["weight"]};font-style:normal;font-display:optional}}'
        )
    rules.append(f'h1,h2,h3{{font-family:"{brand.get("heading_font_family", brand["font_family"])}",Arial,sans-serif}}')
    rules.append(".btn-primary,.btn-primary:hover{color:#fff;background:var(--accent-strong)}")
    rules.append(".nav-link.active,.docs-side-link.active{color:var(--text)}")
    return "\n".join(rules)


PACK_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
LOCAL_PACK = "local"
UPLOAD_LIMIT = 2 * 1024 * 1024
# What each upload slot accepts and the leading bytes each format must start with.
UPLOAD_SLOTS = {
    "logo": {".svg", ".png", ".jpg", ".jpeg", ".webp"},
    "favicon": {".ico", ".png", ".svg"},
    "font_regular": {".woff", ".woff2"},
    "font_bold": {".woff", ".woff2"},
    "font_headline": {".woff", ".woff2"},
}
MAGIC = {".png": (b"\x89PNG\r\n\x1a\n",), ".jpg": (b"\xff\xd8\xff",), ".jpeg": (b"\xff\xd8\xff",),
         ".webp": (b"RIFF",), ".ico": (b"\x00\x00\x01\x00",), ".woff": (b"wOFF",), ".woff2": (b"wOF2",)}
# Uploaded SVGs must be plain pictures: no scripts, event handlers, embedded documents or external references.
UNSAFE_SVG = re.compile(rb"<script|<foreignobject|<iframe|<embed|<object|\son[a-z]+\s*=|javascript:"
                        rb"|(?:xlink:)?href\s*=\s*[\"']\s*(?!#)", re.IGNORECASE)


def slugify(name):
    slug = re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-")[:40].strip("-")
    return slug or "brand"


def check_upload(slot, filename, data):
    """The asset suffix for a valid upload; uploads become files other people's browsers load."""
    suffix = Path(str(filename or "")).suffix.lower()
    if slot not in UPLOAD_SLOTS or suffix not in UPLOAD_SLOTS[slot]:
        raise ValueError(f"Choose a {', '.join(sorted(UPLOAD_SLOTS.get(slot, ())))} file for the {slot.replace('_', ' ')}.")
    if not data or len(data) > UPLOAD_LIMIT:
        raise ValueError(f"The {slot.replace('_', ' ')} must be a non-empty file up to 2 MB.")
    if suffix == ".svg":
        head = data[:4096].lower()
        if b"<svg" not in head or UNSAFE_SVG.search(data):
            raise ValueError("The SVG must be a plain image without scripts, event handlers or external links.")
    elif not any(data.startswith(prefix) for prefix in MAGIC[suffix]) or (suffix == ".webp" and data[8:12] != b"WEBP"):
        raise ValueError(f"The {slot.replace('_', ' ')} is not a valid {suffix[1:].upper()} file.")
    return suffix


class BrandStore:
    """Private brand packs, one of them active. Packs and the choice can change while running."""

    def __init__(self, packs_dir=None, local_dir=None):
        self.packs_dir = Path(packs_dir) if packs_dir else None
        self.local_dir = Path(local_dir) if local_dir else None
        self._lock = threading.Lock()
        self._cache = (None, None, dict(DEFAULT_BRAND))
        self.error = None

    def packs(self):
        """{pack id: directory} for every installed pack."""
        found = {}
        if self.local_dir and (self.local_dir / "branding.json").is_file():
            found[LOCAL_PACK] = self.local_dir
        if self.packs_dir and self.packs_dir.is_dir():
            for path in sorted(self.packs_dir.iterdir()):
                if path.is_dir() and PACK_ID.fullmatch(path.name) and (path / "branding.json").is_file():
                    found.setdefault(path.name, path)
        return found

    def active_id(self):
        if self.packs_dir and (self.packs_dir / "active.json").is_file():
            try:
                data = json.loads((self.packs_dir / "active.json").read_text(encoding="utf-8"))
                if isinstance(data, dict) and (data.get("active") is None or isinstance(data.get("active"), str)):
                    return data.get("active")
            except (OSError, ValueError):
                pass
        # Before anything is chosen, a pack installed the original way stays on, as it always was.
        return LOCAL_PACK if LOCAL_PACK in self.packs() else None

    def set_active(self, pack_id):
        if pack_id is not None and pack_id not in self.packs():
            raise ValueError("That brand is not installed.")
        if not self.packs_dir:
            raise ValueError("No private brand directory is configured.")
        self.packs_dir.mkdir(parents=True, exist_ok=True)
        (self.packs_dir / "active.json").write_text(json.dumps({"active": pack_id}), encoding="utf-8")

    def describe(self):
        """Every installed pack with its name, for the brand menu."""
        rows = []
        for pack_id, directory in self.packs().items():
            try:
                _root, brand = load_pack(directory)
                rows.append({"id": pack_id, "name": brand["name"], "subtitle": brand["subtitle"], "logo": brand.get("logo"),
                             "editable": pack_id != LOCAL_PACK, "valid": True})
            except (OSError, ValueError, TypeError) as exc:
                rows.append({"id": pack_id, "name": pack_id, "subtitle": str(exc)[:120], "logo": None,
                             "editable": pack_id != LOCAL_PACK, "valid": False})
        return rows

    def manifest(self, pack_id):
        directory = self.packs().get(pack_id)
        if not directory:
            return {}
        try:
            data = json.loads((directory / "branding.json").read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def current(self, logger=None):
        """(pack directory or None, brand) for the active pack, reloaded when it changes on disk."""
        pack_id = self.active_id()
        directory = self.packs().get(pack_id) if pack_id else None
        try:
            stamp = (pack_id, (directory / "branding.json").stat().st_mtime_ns,
                     max((item.stat().st_mtime_ns for item in (directory / "assets").glob("*")), default=0)) if directory else (None,)
        except OSError:
            stamp = (pack_id, None)
        with self._lock:
            if self._cache[0] == stamp:
                return self._cache[1], self._cache[2]
            root, brand = None, dict(DEFAULT_BRAND)
            self.error = None
            if directory:
                try:
                    root, brand = load_pack(directory)
                except (OSError, ValueError, TypeError) as exc:
                    self.error = str(exc)[:200]
                    if logger:
                        logger.warning("Branding pack could not be loaded; using default appearance")
            brand["id"] = pack_id if root is not None else None
            self._cache = (stamp, root, brand)
            return root, brand

    def save(self, pack_id, fields, uploads):
        """Create or update a private pack from the brand editor; returns its id.
        The local pack is read-only, so saving it creates an editable copy."""
        if not self.packs_dir:
            raise ValueError("No private brand directory is configured.")
        packs = self.packs()
        source = packs.get(pack_id) if pack_id else None
        manifest = self.manifest(pack_id) if source else {}
        target_id = pack_id if source and pack_id != LOCAL_PACK else None
        name = str(fields.get("name") or manifest.get("name") or "").strip()
        if not name:
            raise ValueError("Give the brand a name.")
        if target_id is None:
            base = slugify(name)
            target_id, index = base, 2
            while target_id in packs or target_id == LOCAL_PACK:
                target_id, index = f"{base[:36]}-{index}", index + 1
        for key in DEFAULT_BRAND:
            value = str(fields.get(key) if fields.get(key) is not None else manifest.get(key, "")).strip()
            if value:
                manifest[key] = value
        tokens = dict(manifest.get("tokens") or {})
        for key in DEFAULT_PALETTE:
            value = fields.get(f"token_{key}")
            if value:
                tokens[key] = value
        manifest["tokens"] = tokens
        if fields.get("font_family"):
            manifest["font_family"] = str(fields["font_family"]).strip()
        if fields.get("heading_font_family"):
            manifest["heading_font_family"] = str(fields["heading_font_family"]).strip()
        # Build the pack beside the others, check it, then swap it in.
        self.packs_dir.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix=f".{target_id}-", dir=self.packs_dir))
        try:
            if source and (source / "assets").is_dir():
                shutil.copytree(source / "assets", work / "assets")
            (work / "assets").mkdir(exist_ok=True)
            fonts = {(font.get("weight"), font.get("family", "body")): font for font in manifest.get("fonts") or [] if isinstance(font, dict)}
            for slot in UPLOAD_SLOTS:
                remote = str(fields.get(slot + '_url') or '').strip()
                if remote and uploads.get(slot):
                    raise ValueError(f"Choose either an upload or a URL for {slot.replace('_', ' ')}.")
                if remote:
                    remote = image_url(remote)
                    if slot in ('logo', 'favicon'):
                        manifest[slot] = remote
                    else:
                        weight = 700 if slot == 'font_bold' else 400
                        role = 'heading' if slot == 'font_headline' else 'body'
                        fonts[(weight, role)] = {'file': remote, 'weight': weight, **({'family': role} if role == 'heading' else {})}
            for slot, upload in uploads.items():
                if not upload:
                    continue
                filename, data = upload
                suffix = check_upload(slot, filename, data)
                stored = f"{slot}{suffix}"
                for old in (work / "assets").glob(f"{slot}.*"):
                    old.unlink()
                (work / "assets" / stored).write_bytes(data)
                if slot in ("logo", "favicon"):
                    manifest[slot] = stored
                else:
                    weight = 700 if slot == "font_bold" else 400
                    role = "heading" if slot == "font_headline" else "body"
                    fonts[(weight, role)] = {"file": stored, "weight": weight, **({"family": role} if role == "heading" else {})}
                    manifest.setdefault("font_family", name if FONT_NAME.fullmatch(name) else "Brand")
            manifest["fonts"] = [fonts[weight] for weight in sorted(fonts)]
            (work / "branding.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            load_pack(work)  # Refuse anything the loader would reject.
            final = self.packs_dir / target_id
            if final.exists():
                shutil.rmtree(final)
            work.rename(final)
        finally:
            if work.exists():
                shutil.rmtree(work, ignore_errors=True)
        return target_id

    def export_config(self, pack_id=None):
        pack_id = pack_id if pack_id is not None else self.active_id()
        directory = self.packs().get(pack_id)
        if not directory:
            return {'schema_version': 1, 'branding': dict(DEFAULT_BRAND), 'assets': {}}
        root, brand = load_pack(directory)
        return {'schema_version': 1, 'branding': self.manifest(pack_id),
                'assets': {filename: base64.b64encode((root / 'assets' / filename).read_bytes()).decode('ascii') for filename in sorted(brand['assets'])}}

    def import_config(self, raw):
        if not isinstance(raw, dict) or set(raw) - {'schema_version', 'branding', 'assets'} or raw.get('schema_version') != 1:
            raise ValueError('Choose a version 1 branding JSON configuration.')
        manifest, assets = raw.get('branding'), raw.get('assets', {})
        if not isinstance(manifest, dict) or not isinstance(assets, dict) or len(assets) > 16:
            raise ValueError('Invalid branding configuration or assets.')
        if set(manifest) - {'name', 'subtitle', 'mark', 'logo', 'favicon', 'font_family', 'heading_font_family', 'fonts', 'tokens'}:
            raise ValueError('Unknown branding configuration fields.')
        if not self.packs_dir:
            raise ValueError('No private brand directory is configured.')
        self.packs_dir.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix='.import-', dir=self.packs_dir))
        try:
            asset_root = work / 'assets'
            asset_root.mkdir()
            total = 0
            for filename, encoded in assets.items():
                if not isinstance(filename, str) or not re.fullmatch(r'[A-Za-z0-9_./-]+', filename):
                    raise ValueError('Invalid branding asset name.')
                target = (asset_root / filename).resolve()
                if not target.is_relative_to(asset_root.resolve()):
                    raise ValueError('Branding asset must remain inside assets/.')
                if not isinstance(encoded, str) or len(encoded) > (UPLOAD_LIMIT * 4 // 3 + 8):
                    raise ValueError('Each embedded asset must be at most 2 MB.')
                try:
                    data = base64.b64decode(encoded, validate=True)
                except ValueError as exc:
                    raise ValueError('Embedded assets must use valid base64.') from exc
                total += len(data)
                if total > 8 * 1024 * 1024:
                    raise ValueError('Embedded assets must total at most 8 MB.')
                slot = next((key for key in ('logo', 'favicon') if manifest.get(key) == filename), 'font_regular')
                check_upload(slot, filename, data)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            (work / 'branding.json').write_text(json.dumps(manifest), encoding='utf-8')
            _, brand = load_pack(work)
            if set(assets) != brand['assets']:
                raise ValueError('Embed exactly the local assets referenced by the configuration.')
            pack_id, index = slugify(brand['name']), 2
            base = pack_id
            while pack_id in self.packs() or pack_id == LOCAL_PACK:
                pack_id, index = f'{base[:36]}-{index}', index + 1
            work.rename(self.packs_dir / pack_id)
            return pack_id
        finally:
            if work.exists():
                shutil.rmtree(work)

    def delete(self, pack_id):
        if pack_id == LOCAL_PACK or not self.packs_dir or not PACK_ID.fullmatch(str(pack_id or "")):
            raise ValueError("Only brands created here can be deleted.")
        directory = self.packs_dir / pack_id
        if not (directory / "branding.json").is_file():
            raise ValueError("That brand is not installed.")
        if self.active_id() == pack_id:
            self.set_active(None)
        shutil.rmtree(directory)


def asset_headers(response):
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    # Opened directly, an uploaded image cannot run anything.
    response.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; sandbox"
    return response


def init_app(app, source=None):
    """Serve the active brand to templates. `source` is a BrandStore or a single pack directory."""
    store = source if isinstance(source, BrandStore) else BrandStore(local_dir=source)
    app.extensions["branding_store"] = store
    app.extensions["branding"] = store.current(app.logger)[1]

    def presentation_revision():
        return hashlib.sha256(repr(store._cache[0]).encode()).hexdigest()[:20]

    def cache_presentation(response):
        if request.args.get('brand') == presentation_revision():
            response.headers['Cache-Control'] = 'private, max-age=31536000, immutable'
        return response

    @app.context_processor
    def branding_context():
        root, brand = store.current(app.logger)
        revision = presentation_revision()
        return {"branding": brand, "branding_enabled": root is not None,
                "branding_asset_url": lambda filename: asset_url(filename, revision),
                "branding_revision": revision}

    @app.get("/branding/theme.css")
    def branding_stylesheet():
        root, brand = store.current(app.logger)
        if root is None:
            abort(404)
        response = Response(stylesheet(brand, presentation_revision()), mimetype="text/css")
        response.headers["Cache-Control"] = "no-cache"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return cache_presentation(response)

    @app.get('/branding/status')
    def branding_status():
        root, brand = store.current(app.logger)
        revision = presentation_revision()
        response = jsonify(enabled=root is not None, revision=revision, name=brand['name'], subtitle=brand['subtitle'],
                           mark=brand['mark'], logo=asset_url(brand['logo'], revision) if brand.get('logo') else None,
                           favicon=asset_url(brand['favicon'], revision) if brand.get('favicon') else None)
        response.headers['Cache-Control'] = 'no-store'
        return response

    @app.get("/branding/assets/<path:filename>")
    def branding_asset(filename):
        root, brand = store.current(app.logger)
        if root is None or filename not in brand["assets"]:
            abort(404)
        try:
            _asset(root, filename, ASSET_SUFFIXES)
        except ValueError:
            abort(404)
        return cache_presentation(asset_headers(send_from_directory(root / "assets", filename)))

    return store
