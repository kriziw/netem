# Users and Authentik

## Enable local accounts

Run as the same service account that runs NetEm, from its installation directory:

```bash
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python access_control.py create-admin admin
```

The command prompts for a password of at least 12 characters without putting it in command-line arguments or shell history. The first local administrator enables authentication on the operator listener. Open NetEm and sign in, then use **Settings → System & integrations → Users & access** to add accounts, change roles, reset local passwords or disable users. Keep at least one enabled local administrator; NetEm prevents disabling or demoting the last one. Password, role and enable/disable changes revoke the affected user's existing logins immediately.

Existing installations retain their trusted-lab access until a local administrator is provisioned. To prevent access before provisioning, set `NETEM_AUTH_REQUIRED=1` in the systemd service environment and restart. There is no unauthenticated web registration or first-user takeover endpoint. Bootstrap and password recovery require server access.

For an HTTPS deployment, terminate TLS at your trusted reverse proxy and set `NETEM_SECURE_COOKIES=1` in the NetEm service environment. This makes the login cookie HTTPS-only. Sessions are HttpOnly, SameSite=Lax and expire after eight hours; successful sign-in rotates session state. Browser mutations require a CSRF token. APIs return HTTP 401 when authentication is required and HTTP 403 when the role is insufficient. Existing unauthenticated operator API or metrics clients must be updated to authenticate using a session; this change does not add long-lived API tokens.

## Roles

| Role | Permissions |
| --- | --- |
| Viewer | Read dashboards, telemetry, test results and reports |
| Presenter | Viewer access plus start/stop lab sessions and prepared tests; pause/resume and previous/next phase |
| Operator | Presenter access plus runtime WAN changes, profiles, probes, captures, workload controls and test definitions |
| Admin | All controls, including users, OIDC, topology, appliances, traffic-path setup, integrations, branding and system updates |

Permissions apply on the server to HTML and API routes. Unavailable navigation and action shortcuts are removed. Unauthorized forms and alternate submit actions are locked read-only while keeping live preview and measurement widgets working. These interface restrictions are usability aids; direct requests are still checked on the server. New mutation endpoints default to Admin until explicitly assigned a lower role. Presenter test execution can apply the WAN impairments defined by a prepared test, but does not grant arbitrary WAN configuration access. Users currently control one shared lab; RBAC does not provide independent concurrent sessions. Parallel-session feasibility is tracked in [issue #148](https://github.com/kriziw/netem/issues/148).

The separate showcase listener remains an intentionally public, read-only presentation surface with its existing restricted routes. Protect it at the network/reverse-proxy boundary if the audience must be restricted. It does not expose operator control or user-management routes.

## Configure Authentik

1. In Authentik, create an application and an OAuth2/OpenID provider with a **confidential** client. Select a signing key. Use strict redirect URI matching and register the exact NetEm callback, e.g. `https://netem.example.com/auth/oidc/callback`.
2. Assign `openid`, `profile` and `email` scope mappings. Include a `groups` claim containing an array of group names in the signed ID token. For example, a custom OAuth2 scope mapping assigned to the provider can return:

   ```python
   return {"groups": [group.name for group in request.user.ak_groups.all()]}
   ```

   Include that mapping in an allowed/requested scope (for example `profile`), and enable **Include claims in id_token**. Match your Authentik version's scope-mapping UI. NetEm requests `openid profile email` and reads groups from the validated ID token, not from an unverified browser payload or email address.
3. In NetEm **Users & access**, enter the issuer URL exactly as published by Authentik's discovery document, typically `https://auth.example.com/application/o/netem/`. Discovery is fetched at `ISSUER/.well-known/openid-configuration`; the configured issuer must exactly match discovery and token `iss`, including its trailing slash. Configure Authentik's per-provider issuer mode for this layout.
4. Enter the client ID and client secret, the exact external callback URL, and group-to-role mappings:

   ```json
   {
     "netem-admins": "admin",
     "netem-operators": "operator",
     "netem-presenters": "presenter",
     "netem-viewers": "viewer"
   }
   ```

5. Enable OIDC, save, and test **Sign in with Authentik** in a separate browser session. Local login remains available. An unmapped user is denied; membership in multiple mapped groups grants the highest mapped role. New external accounts are provisioned on successful mapped login and are keyed by issuer plus subject. An email or username match never merges an external identity with a local administrator.

NetEm uses Authlib's authorization-code flow with S256 PKCE, state and nonce. Authlib validates the signed ID token using discovery/JWKS, issuer, audience and expiration. TLS verification stays enabled; issuer, callback and discovery endpoints must use HTTPS. The callback URL is explicitly configured, so login does not depend on trusting arbitrary forwarded-host headers.

OIDC roles are synchronized at each login; an existing login expires after eight hours. Provider-side membership changes therefore take effect at the next login or expiry. Disabling a user in NetEm revokes local access immediately, including future OIDC login until re-enabled. Changing OIDC configuration revokes all existing NetEm OIDC sessions. **Sign out** ends the NetEm session only; it does not sign the user out of all Authentik applications.

User password hashes, identity mappings and the OIDC secret live in the private `runtime/access.sqlite3` database, separate from configuration exports/test archives. The secret is never rendered back into the settings page; leaving the secret field blank preserves it. Back up this database together with `runtime/session.secret` using private file permissions. Do not commit either file to Git.

## Recovery

Use the enabled local administrator when Authentik is unavailable. To reset a local password from the server:

```bash
.venv/bin/python access_control.py reset-password admin
```

This revokes existing sessions for the account. If necessary, provision another local administrator with `create-admin` from the server. Login failures are rate limited per client address and username. Behind a proxy, NetEm uses the directly connected address unless the deployment supplies a trusted proxy arrangement; it does not blindly trust forwarded client-IP headers.

Official references:

- [Authentik OAuth2/OpenID provider](https://docs.goauthentik.io/add-secure-apps/providers/oauth2/)
- [Authentik provider mappings](https://docs.goauthentik.io/add-secure-apps/providers/property-mappings/)
- [Authlib Flask OIDC integration](https://docs.authlib.org/en/latest/oauth2/client/web/flask.html)
