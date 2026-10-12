# Branding configuration and automatic propagation

Settings > Branding accepts version 1 JSON files with `schema_version`, `branding` and `assets`. The branding object contains name/subtitle/mark, logo/favicon, font_family/heading_font_family, fonts and palette tokens. Assets map referenced local filenames to base64. Config files are limited to 12 MB, each embedded asset to 2 MB, and embedded content totals to 8 MB. Imports create a new private pack; activation is optional. Export produces a complete round-trip configuration without unreferenced private files.

The editor accepts an upload or HTTPS URL for logo, favicon, regular/bold/headline fonts. Existing references persist when fields are left empty. Remote URLs are browser-loaded and are not fetched by the server. Use a font host that permits browser cross-origin access, or upload/embed the font instead. Fonts declare weights and optional `family: heading` for headline typography. SVG uploads must be plain pictures.

Activating a pack applies it to NetEm and its separate read-only Showroom using their shared store. NetEm sends the active bundle to the simulator via its authenticated, certificate-pinned management API. The agent validates it, stores it atomically in private runtime storage and serves local copies of uploaded images/fonts. It does not depend on cross-origin access to NetEm. NetEm checks the agent revision and retries every 30 seconds, including after reconnection/reinstallation. Browser presentation refreshes every ten seconds without resetting test state, charts or unsaved edits. An offline agent retains its last presentation; Branding settings reports pending synchronization.

Both components must include the branding API for simulator synchronization. Neither import nor activation changes the active workload or test. Resetting NetEm to the default look also updates Showroom and agent presentation.

The requested BT International configuration and research notes are delivered in private runtime/exports. Fonts and future operator-provided logos are not included in the public Git repository.
