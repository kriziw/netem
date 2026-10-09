# Appliance branding

The application supports one brand per appliance. Brand data is deployment
configuration: keep it outside the Git checkout in production. The repository
contains only the generic loader, default appearance and this schema.

## Install

Place a pack in a private directory such as `/etc/netem/branding/`:

```text
branding/
  branding.json
  assets/
    logo.svg
    favicon.png
    regular.woff2
    bold.woff2
```

Add a systemd override with `sudo systemctl edit netem`:

```ini
[Service]
Environment=NETEM_BRANDING_DIR=/etc/netem/branding
```

Then run `sudo systemctl daemon-reload` and `sudo systemctl restart netem`.
The service user needs read access to the pack. Branding is read at startup;
restart after changes. Remove the environment setting to restore the default.
The pack survives Git updates because it lives outside the checkout.

For local development only, `runtime/branding/branding.json` is detected
automatically when no environment override is present. `runtime/` is ignored
by Git. An explicit environment path takes precedence, including when invalid.
A missing or invalid pack logs a generic warning and uses the default appearance.

## Manifest

```json
{
  "name": "Example Resilience Lab",
  "subtitle": "Network validation platform",
  "mark": "E",
  "logo": "logo.svg",
  "favicon": "favicon.png",
  "font_family": "Example Sans",
  "fonts": [
    {"file": "regular.woff2", "weight": 400},
    {"file": "bold.woff2", "weight": 700}
  ],
  "tokens": {
    "accent": "#4050D0",
    "accent-strong": "#3040B0",
    "bg": "#08101C",
    "surface": "#0D1726",
    "text": "#EDF4FB"
  }
}
```

Every field is optional. Omit logo/favicon/fonts until those files are available;
the text mark and system font provide fallbacks. Asset filenames are relative
to `assets/`. Supported images are SVG, PNG, JPEG, WebP and ICO; fonts are
WOFF/WOFF2. Use trusted, locally supplied assets. No external font or CDN requests
are needed. Font-family names accept letters, digits, spaces and hyphens.
Text is escaped in templates.

Colour values must be six-digit hex strings. Supported tokens:

- Backgrounds: `bg`, `bg-deep`, `surface`, `surface-2`, `surface-3`,
  `surface-hover`.
- Borders: `border`, `border-strong`.
- Text: `text`, `text-soft`, `muted`, `muted-2`.
- Brand: `accent`, `accent-strong`, `blue`.
- Status: `warning`, `danger`, `success`.

The loader derives translucent colours and component colour aliases from the
palette. Override the full palette for a consistent appearance. Keep status
colours distinguishable and check contrast when creating a theme, including white
text on primary buttons. Navigation, charts, controls, help pages, dialogs and
page titles and the separate read-only showroom share the theme. Printable session reports include the brand name
and optional logo while retaining readable white print backgrounds. Project
credits and technical NetEm references remain intact.

## Privacy

Only the generated theme stylesheet and manifest-listed assets are served.
The manifest and other private files have no download route. Branding assets
are visible to users of the appliance; this feature does not make them secret
from browser users or add authentication.

Distribute packs directly or through private internal storage. Do not include
them in public release bundles, CI artifacts, screenshots or PR attachments.
Keep production packs outside the repository and review staged files before
publishing changes. A local ignore rule is an additional safeguard, not a way
to remove already committed content.
