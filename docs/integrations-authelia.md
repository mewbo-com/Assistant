# Authelia

## Sign in behind your proxy

[Authelia](https://www.authelia.com) is most often deployed as a forward-auth companion to a reverse proxy. On allow, Authelia returns the signed-in user's identity in response headers, and Mewbo consumes them through its `trusted_header` authenticator. A deployment already protecting other services with Authelia gets Mewbo behind the same policy with no new login flow.

Authelia can also act as a full OpenID Connect provider. Take forward auth if Authelia already fronts your internal services. Take OIDC if you want Mewbo to own its login button and issue a session independent of the proxy.

---

## What this unlocks

- Users sign in to the Mewbo console and REST API on the Authelia session they already hold, and their Authelia groups become Mewbo roles through the ordinary mapping rules.
- Authelia's own access control rules, two factor policy, network policy and per-domain rules apply to Mewbo without Mewbo implementing any of them.

---

## Path A: forward auth (recommended when Authelia already fronts your services)

### How the trust boundary sits

In forward auth, Mewbo never speaks to Authelia.

```mermaid
flowchart LR
    U([Browser]) -->|"1 request"| P["Reverse proxy<br/>nginx / Traefik / Caddy"]
    P -->|"2 verify session"| A["Authelia"]
    A -->|"3 allow + Remote-* headers"| P
    P -->|"4 request + Remote-* headers"| M["Mewbo API"]
    M -.->|"peer address must be<br/>inside trusted_proxies"| M

    subgraph trusted ["Trust boundary: only this hop may assert identity"]
        P
        M
    end
```

**Any host that can open a TCP connection to Mewbo from an allowlisted address can assert any identity it likes.** Read the [security requirements](#security-requirements-read-before-enabling) section before you enable this.

### Configure Mewbo

Authelia emits its identity headers with the `Remote-` prefix and separates multiple groups with commas.

```json title="configs/app.json" linenums="1"
{
  "api": {
    "auth": {
      "enabled": true,
      "authenticators": [
        {
          "name": "authelia",
          "kind": "trusted_header",
          "issuer": "https://auth.example.com",
          "user_header": "Remote-User",
          "groups_header": "Remote-Groups",
          "email_header": "Remote-Email",
          "name_header": "Remote-Name",
          "group_delimiter": ",",
          "trusted_proxies": ["10.42.0.7/32"]
        }
      ],
      "session": { "secret": "generate-a-long-random-string" },
      "role_mappings": {
        "default_role": "viewer",
        "rules": [
          { "match": "mewbo-admins", "target": "admin" },
          { "match": "engineering", "target": "operator" }
        ]
      },
      "bootstrap": { "admin_group": "mewbo-admins" }
    }
  }
}
```

Field notes, verified against [`authenticators.py`](repo:packages/mewbo_iam/src/mewbo_iam/authenticators.py).

| Field | Default | Notes |
|---|---|---|
| `user_header` | `X-Forwarded-User` | Set it to `Remote-User` for Authelia. This header is the join key; a request without it is simply anonymous. |
| `groups_header` | `X-Forwarded-Groups` | Authelia sends `Remote-Groups`. |
| `email_header` | `X-Forwarded-Email` | Authelia sends `Remote-Email`. |
| `name_header` | `X-Forwarded-Preferred-Username` | Authelia sends `Remote-Name`, which carries the display name. |
| `group_delimiter` | `,` | Only `,` and `&#124;` are accepted. Authelia uses a comma, which is already the default. |
| `trusted_proxies` | none, **required** | A list of CIDRs. The config will not validate without at least one, and every entry must parse as an IP network. |
| `issuer` | `trusted-header` | A label recorded on the identity and in the audit trail. Set it to something you will recognise. |

`session.secret` is required as soon as any non-API-key authenticator is configured, and Mewbo refuses to start without it.

### Configure the proxy to forward the headers

This is the step teams miss most often. Authelia returns the identity headers to the **proxy**, and the proxy drops them unless you tell it to forward them. Skip this and every request reaches Mewbo with no identity.

=== "nginx"

    ```nginx
    location /api/authelia {
        internal;
        proxy_pass http://authelia:9091/api/verify;
        proxy_pass_request_body off;
        proxy_set_header Content-Length "";
        proxy_set_header X-Original-URL $scheme://$http_host$request_uri;
    }

    location / {
        auth_request /api/authelia;

        # Lift the identity out of the auth_request response ...
        auth_request_set $user  $upstream_http_remote_user;
        auth_request_set $groups $upstream_http_remote_groups;
        auth_request_set $email $upstream_http_remote_email;
        auth_request_set $name  $upstream_http_remote_name;

        # ... and forward it to Mewbo.
        proxy_set_header Remote-User   $user;
        proxy_set_header Remote-Groups $groups;
        proxy_set_header Remote-Email  $email;
        proxy_set_header Remote-Name   $name;

        proxy_pass http://mewbo-api:5000;
    }
    ```

    The `auth_request_set` lines are load-bearing. Without them the `proxy_set_header` values are empty strings.

=== "Traefik"

    ```yaml
    http:
      middlewares:
        authelia:
          forwardAuth:
            address: "http://authelia:9091/api/verify?rd=https://auth.example.com"
            trustForwardHeader: true
            authResponseHeaders:
              - "Remote-User"
              - "Remote-Groups"
              - "Remote-Email"
              - "Remote-Name"
    ```

    `authResponseHeaders` does the job of nginx's `auth_request_set` pair. A middleware without it authenticates correctly and forwards nothing.

=== "Caddy"

    ```caddyfile
    console.example.com {
        forward_auth authelia:9091 {
            uri /api/verify?rd=https://auth.example.com
            copy_headers Remote-User Remote-Groups Remote-Email Remote-Name
        }
        reverse_proxy mewbo-api:5000
    }
    ```

    `copy_headers` is the directive that forwards the identity upstream.

### Strip the headers from client input

The proxy must also **overwrite** these headers on every inbound request, so a client cannot send `Remote-User: admin` itself. The directives above do this implicitly by assigning the value from the auth response. In a proxy configuration you build yourself, make the overwrite explicit and unconditional.

---

## Security requirements (read before enabling)

Trusted-header authentication is an authentication-bypass class of feature when it is misconfigured. The full model, the allowlist requirement, the audit behaviour for untrusted peers and the credential ordering are covered once in [Security preconditions](authentication.md#trusted-header-mode). Mewbo enforces what it can. The rest is yours to get right.

Two things are specific to Authelia. `trusted_proxies` must name the proxy that opens the TCP connection, never Authelia itself, which in forward auth never talks to Mewbo directly. And Authelia's own network policy and two factor rules do not substitute for that allowlist, because they protect the login rather than the header handoff.

**Cookie mode requires a pinned CORS origin.** This applies to the OIDC path below rather than to forward auth. With a browser-login authenticator enabled, Mewbo refuses to boot on a wildcard `*`. Set it to the console's exact origin.

---

## Path B: Authelia as an OpenID Connect provider

To give Mewbo its own login button, register it as an OIDC client in Authelia's `identity_providers.oidc.clients` list and point Mewbo at the discovery document.

```json title="configs/app.json"
{
  "name": "authelia",
  "kind": "oidc",
  "issuer": "https://auth.example.com",
  "discovery_url": "https://auth.example.com/.well-known/openid-configuration",
  "client_id": "mewbo",
  "client_secret": "the-secret-you-configured-in-authelia",
  "scopes": ["openid", "email", "profile", "groups"],
  "groups_claim": "groups"
}
```

Authelia exposes group membership on a dedicated `groups` scope, which the default scope list omits. Request it explicitly.

> [!IMPORTANT] `group_delimiter` does not apply on this path
> It exists only on the `trusted_header` authenticator above, so the groups claim here must be a JSON **array**. See [why claim paths are the recurring theme](authentication-providers.md#why-claim-paths-are-the-recurring-theme). Authelia emits a proper list, so this bites only if you have customised the claim.

Register this redirect URI on the Authelia side.

```
https://console.example.com/api/auth/callback
```

Mewbo derives that URI from the request origin, so a proxied deployment must forward `X-Forwarded-Proto` and `X-Forwarded-Host` or it will not match what you registered.

---

## Verify it works

1. **Check the resolved identity.** Sign in through the proxy, then call `GET /api/auth/me`. Expect `authenticated: true`, the subject taken from `Remote-User`, the resolved `roles`, the full `permissions` set, and an `auth_method` reporting `kind: "trusted_header"` with your `issuer`.
2. **Confirm the groups arrived.** If `roles` shows only your `default_role`, the groups header is missing or delimited differently than configured. Compare `teams` and `roles` against the Authelia group names.
3. **Prove the boundary holds.** From a host *outside* your `trusted_proxies` range, send a forged `Remote-User` header directly to Mewbo. It must resolve as anonymous, and a `login_failure` event with reason `untrusted_proxy_source` must appear in the audit trail. If that request succeeds, your allowlist is wrong and the deployment is open.

---

## Failure modes

| Symptom | Cause | Fix |
|---|---|---|
| Every user is anonymous; `/api/auth/me` returns 401 | The proxy authenticates but does not forward the headers | Add `auth_request_set` + `proxy_set_header` (nginx), `authResponseHeaders` (Traefik), or `copy_headers` (Caddy) |
| Identity resolves, but everyone lands on the default role | `groups_header` name is wrong, or the delimiter does not match | Confirm Authelia sends `Remote-Groups` and that `group_delimiter` is `,` |
| Server refuses to boot: `trusted_proxies must be non-empty` | The allowlist is missing | Add the proxy's CIDR |
| Server refuses to boot: `invalid trusted-proxy CIDR` | An entry is not a parseable IP network | Use CIDR notation, for example `10.42.0.7/32` rather than a hostname |
| Audit shows `untrusted_proxy_source` for legitimate traffic | The connecting peer is not the address you allowlisted, usually because another proxy sits in between | Allowlist the real last hop, or apply `ProxyFix` with the correct hop count |
| Users bounce back to the login page in a loop | Cookie is not being stored because the deployment is served over http while marked `Secure`, or `X-Forwarded-Proto` is not set | Serve over https and forward `X-Forwarded-Proto` |

The boot failures shared by every provider, including an unset `session.secret`, are in [Troubleshooting](authentication-providers.md#troubleshooting).

---

## What is not covered yet

- Mewbo has no login rate limiting or account lockout. On the forward-auth path that does not matter, because Authelia performs the credential check and applies its own regulation policy.
- Group membership is re-read from the headers on each request, so an Authelia group change takes effect as soon as the proxy issues headers reflecting it.

For the concepts behind this page, see [Authentication and Access](authentication.md). This guide connects one provider and deliberately does not restate the model.
