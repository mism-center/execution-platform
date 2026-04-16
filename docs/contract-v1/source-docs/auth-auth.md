## Authentication and Authorization

### Overview

All launch endpoints MUST be protected using OAuth 2.0 bearer access
tokens issued by the platform's OpenID Connect (OIDC) identity provider.

Clients MUST include an access token in the HTTP `Authorization` header
using the Bearer scheme:

```http
Authorization: Bearer <access_token>
```

The API MUST reject requests that do not include a valid bearer access
token.

> Note: OIDC provides the identity layer, while API protection is
> typically enforced using OAuth 2.0 access tokens. In practice, this
> means the API accepts and validates bearer access tokens issued by the
> OIDC/OAuth provider.

### Token Requirements

The launch API MUST validate the presented access token before
authorizing execution. Validation MUST include, at minimum:

- signature validation
- issuer validation (`iss`)
- audience validation (`aud`)
- expiration validation (`exp`)
- not-before validation (`nbf`), if present
- scope and/or role validation, if authorization is scope- or role-based

If token validation fails, the API MUST return `401 Unauthorized`.

If the token is valid but does not grant permission to invoke the
requested launch endpoint, the API MUST return `403 Forbidden`.

### Recommended HTTP Behavior

#### Authorized request

```http
POST /launch
Authorization: Bearer <access_token>
Content-Type: application/json
```

#### Missing or invalid token

```http
HTTP/1.1 401 Unauthorized
WWW-Authenticate: Bearer
Content-Type: application/json

{
  "error": "unauthorized",
  "message": "Missing, invalid, or expired bearer token."
}
```

#### Insufficient permissions

```http
HTTP/1.1 403 Forbidden
Content-Type: application/json

{
  "error": "forbidden",
  "message": "Token does not grant permission to invoke this endpoint."
}
```

### What an OIDC Bearer Token Provides

A bearer token provides two things relevant to this API:

1. **Proof of authorization**: possession of a valid access token
   authorizes the caller to access the protected API, subject to
   validation and permission checks. Bearer tokens are bearer-style
   credentials, meaning any party in possession of the token can use it
   unless additional controls are applied, so tokens MUST be protected
   in transit and at rest.

2. **Caller identity claims**: when the token is JWT-formatted, it may
   carry claims that identify the caller and the issuer. In particular:
   - `sub` identifies the subject
   - `iss` identifies the issuer
   - the pair (`iss`, `sub`) forms the stable unique identity key

The OpenID Connect specification defines `sub` as unique only within the
scope of a given issuer. Accordingly, the API MUST treat the combination
of `iss` and `sub` as the canonical caller identity.

### Canonical Caller Identity

The API MUST treat the combination of:

- `sub`
- `iss`

as the canonical caller identity.

The API MUST NOT assume that `sub` alone is globally unique.

### Derived Internal Username

To support internal systems that require a deterministic caller
identifier, the platform SHOULD derive usernames from the token's `sub`
and `iss` claims.

Because OIDC claims may contain uppercase letters and other characters
that are not valid in Kubernetes object names, the platform MUST define
two distinct derived forms:

- an **internal username** for general identity handling
- a **Kubernetes-safe username** for naming Kubernetes resources

#### Internal username

The internal username is intended for application-level identity usage
where only alphanumeric characters are required.

Let:

- `sub` = subject claim from the validated token
- `iss` = issuer claim from the validated token
- `issuer_hash` = a deterministic hash of `iss`, encoded as lowercase
  hexadecimal

Recommended internal username:

```text
<normalized-sub><issuer-hash>
```

Where:

1. `normalized-sub` is produced by removing all non-alphanumeric
   characters from `sub`
2. alphabetic characters in `normalized-sub` MAY retain original case
3. `issuer_hash` is computed from `iss` using a stable one-way hash
   function
4. the final username contains only ASCII letters and digits

Example conceptual construction:

```text
internal_username = alnum(sub) + hex(sha256(iss))
```

#### Kubernetes-safe username

The Kubernetes-safe username is intended for use in Kubernetes object
names and MUST comply with Kubernetes naming constraints for the target
resource type. At minimum, it MUST avoid uppercase letters.

Recommended Kubernetes-safe username:

```text
<dns-label-safe-sub>-<issuer-hash-prefix>
```

Where:

1. start with `sub`
2. convert all alphabetic characters to lowercase
3. replace each run of non-alphanumeric characters with a single `-`
4. trim leading and trailing `-`
5. append a separator `-` and a deterministic lowercase hexadecimal hash
   prefix derived from `iss`
6. if required by the Kubernetes resource type, truncate the result to
   the permitted maximum length while preserving uniqueness
7. if the normalized prefix becomes empty, use a fixed fallback such as
   `user`

Example conceptual construction:

```text
k8s_username = dns_label(lower_alnum_hyphen(sub)) + "-" +
               hex(sha256(iss))[:12]
```

Example:

```text
sub              = "00u12abc-XYZ_9"
iss              = "https://id.example.com/oauth2/default"
internal_username = "00u12abcXYZ9" + hex(sha256(iss))
k8s_username      = "00u12abc-xyz-9-8f3a1c4d2e6b"
```

### Requirements for Derived Usernames

The derived internal username MUST:

- be deterministic for the same (`iss`, `sub`) pair
- be unique across issuers
- contain only alphanumeric characters
- not require any external lookup to regenerate
- remain stable over time unless `iss` or `sub` changes

The derived Kubernetes-safe username MUST:

- be deterministic for the same (`iss`, `sub`) pair
- be derived from the same canonical identity as the internal username
- contain only characters permitted by the Kubernetes naming scheme in
  use
- normalize uppercase characters to lowercase
- remain stable over time unless `iss` or `sub` changes
- preserve uniqueness across issuers by including a hash derived from
  `iss`

The system SHOULD:

- preserve the original `sub` and `iss` separately for audit and
  debugging
- avoid using display-oriented claims such as `email`,
  `preferred_username`, or `name` as the primary identity key
- define a maximum username length and, if needed, truncate the hash
  portion in a controlled way

### Audit Requirements

For each authorized launch request, the API SHOULD record:

- derived internal username
- derived Kubernetes-safe username, if used
- original `sub`
- original `iss`
- token audience (`aud`)
- token scopes and/or roles used for authorization
- request timestamp
- request ID or trace ID

### Security Considerations

- Clients MUST send bearer tokens only over HTTPS.
- The API MUST validate tokens against trusted issuer metadata or
  trusted signing keys.
- The API SHOULD accept access tokens, not ID tokens, for API
  authorization.
- The API MUST treat the token as untrusted until validation is
  complete.
- The API SHOULD minimize logging of raw tokens and MUST NOT write full
  bearer tokens to standard application logs.
