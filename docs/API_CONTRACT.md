# API Contract

This document describes the HTTP API that the SvelteKit frontend expects from its backend.
Any compatible backend must implement all endpoints below with the request/response shapes
and authentication behavior documented here.

> **Live reference:** The FastAPI backend at `https://api.archive.ire.org` serves interactive
> OpenAPI documentation at `/docs` and a machine-readable schema at `/openapi.json`.

This is a compatibility boundary between the backend and
[`ireapps/ire-archive-frontend`](https://github.com/ireapps/ire-archive-frontend). Data publication must not
change endpoint shapes, authentication, stable resource links, pagination, metadata types, search modes, CORS,
or cookie behavior without a coordinated frontend change. See [ARCHITECTURE.md](ARCHITECTURE.md).

---

## Base URL

The frontend reads its base URL from the `VITE_API_BASE_URL` environment variable:

```
VITE_API_BASE_URL=https://api.archive.ire.org   # production
VITE_API_BASE_URL=http://localhost:8000          # local development
```

All endpoint paths below are relative to this base URL.

---

## Authentication Model

The backend uses **server-side sessions stored in Redis**. Sessions are identified by an
`HttpOnly` cookie. The frontend sends `credentials: "include"` on every request so the
browser automatically includes this cookie.

### Session cookie behavior

| Property | Value |
|---|---|
| Name | `ire_session` (configurable via `SESSION_COOKIE_NAME` env var) |
| HttpOnly | Yes |
| SameSite | `Lax` in local development; `None` in production |
| Secure | Yes in production, No in development |
| Domain | Host-only in local development; `.archive.ire.org` in production |
| Lifetime | Configured via `SESSION_TTL_SECONDS` (default 3600 seconds / 1 hour) |

### Auth error codes

| HTTP Status | Meaning | Frontend behavior |
|---|---|---|
| `401 Unauthorized` | No session or session expired | Redirect to `/login?expired=true&returnTo=<current_url>` |
| `403 Forbidden` | Session valid but user lacks active membership | Show "membership required" error |

---

## CORS Requirements

The backend must include the frontend's origin in its CORS `allow_origins` list.
Credentials (`cookies`) require that CORS is configured with explicit origins — wildcards
(`*`) will not work.

Required CORS behavior:
- `allow_origins`: list containing the frontend's deployed origin(s)
- `allow_credentials`: `true`
- `allow_methods`: `["GET", "POST", "OPTIONS"]`
- request headers required by the frontend must be allowed

The reference backend supports runtime-configurable origins via the
`ADDITIONAL_ALLOWED_ORIGINS` environment variable (see `app/config.py`).

---

## Endpoints

### Public Endpoints (no authentication required)

---

#### `GET /`

Root health check. Used by deployment tooling to verify the API is running.

**Response `200 OK`:**
```json
{ "message": "IRE Resources Semantic Search API" }
```

---

#### `GET /stats`

Returns collection statistics shown on the homepage (e.g., total resource count).

**Response `200 OK`:**
```typescript
interface StatsResponse {
  collection_name: string;   // Qdrant collection name
  total_documents: number;   // Unique documents indexed
  total_points: number;      // Total vector points (may be > documents due to chunking)
  vector_size: number;       // Embedding dimensions (384 for all-MiniLM-L6-v2)
  status: string;            // Qdrant collection status (e.g., "green")
}
```

**Example:**
```json
{
  "collection_name": "nonprofit_knowledge",
  "total_documents": 33412,
  "total_points": 33412,
  "vector_size": 384,
  "status": "green"
}
```

---

#### `GET /auth/status`

Auth service health check. Not used by the UI directly but useful for diagnostics.

**Response `200 OK`:**
```typescript
interface AuthStatusResponse {
  configured: boolean;     // Whether MemberSuite credentials are present
  frontend_url: string;    // Configured frontend origin
  callback_url: string;    // OAuth callback URL registered with MemberSuite
}
```

---

#### `GET /auth/login`

Initiates the OAuth/SSO login flow. Returns the URL the browser should redirect to.

**Query parameters:**

| Parameter | Type | Required | Description |
|---|---|---|---|
| `returnTo` | `string` | No | URL to redirect back to after successful login (default: `/`) |

**Response `200 OK`:**
```typescript
interface LoginResponse {
  redirect_url: string;    // Full URL to the identity provider login page
}
```

**Frontend usage:**
```typescript
const loginUrl = new URL(`${API_BASE_URL}/auth/login`);
loginUrl.searchParams.set("returnTo", returnTo);
const response = await fetch(loginUrl.toString(), { credentials: "include" });
if (!response.ok) {
  const errorData = await response.json().catch(() => ({}));
  throw new Error(errorData.detail || "Failed to initiate login");
}
const { redirect_url } = await response.json();
window.location.href = redirect_url;
```

---

#### `GET /auth/callback`

OAuth callback handler. The identity provider redirects the user's browser here after
authentication. This endpoint creates the session and redirects the browser to the
original `returnTo` URL.

This endpoint is called directly by the identity provider — the frontend never calls it
programmatically.

**Query parameters:** `code`, `state` (identity-provider specific)

**Response:** `302 Redirect` to the `returnTo` URL on success, or an error page on failure.

---

### Authenticated Endpoints (session cookie required)

All endpoints in this section require a valid session cookie. Without one, the backend
returns `401`. If the user's session is valid but they lack an active membership, the
backend returns `403`.

---

#### `GET /auth/me`

Returns information about the currently authenticated user. Called by the root layout on
every page load and on tab focus (with a 5-minute debounce) to validate/restore session state.

**Request:** No body. Session identified by cookie.

**Response `200 OK`:**
```typescript
interface UserResponse {
  user_id: string;           // Opaque user identifier from identity provider
  email: string;
  first_name: string;
  last_name: string;
  full_name: string;
  is_active_member: boolean; // Whether the user has an active paid membership
  session_expires_in: number;// Seconds until session expires
}
```

**Error responses:**
- `401` — No session or expired session
- `403` — Session valid but membership inactive

---

#### `POST /auth/logout`

Ends the current session server-side and clears the session cookie.

**Request:** No body. Session identified by cookie.

**Response `200 OK`:**
```typescript
interface LogoutResponse {
  success: boolean;
  message: string;
}
```

**Frontend note:** The frontend surfaces logout failures to the user rather than silently
ignoring them, because a failed logout on a shared computer is a security concern.

---

#### `POST /search`

Performs semantic, keyword, or hybrid search over the resource collection. Requires an
active membership session.

**Request body (`Content-Type: application/json`):**
```typescript
interface SearchQuery {
  query?: string;                            // Search text (required if categories is empty)
  limit?: number;                            // Results per page (default: 20, min: 1, max: 100)
  offset?: number;                           // Pagination offset (default: 0, max: 10000)
  categories?: string[];                     // Filter by category names (see valid values below)
  sort_by?: "relevance" | "newest" | "oldest";  // Sort order (default: "relevance")
  search_mode?: "hybrid" | "keyword";        // Search algorithm (default: "hybrid")
}
```

**Valid `categories` values** (must match exactly, case-sensitive):
- `"audio"`
- `"contest entry"`
- `"dataset"`
- `"journal"`
- `"tipsheet"`
- `"webinar"`

**Validation rules:**
- Either `query` or `categories` must be non-empty.
- `query` maximum length: 1000 characters (configurable via `MAX_QUERY_LENGTH`).
- Invalid `categories` values cause a `422 Unprocessable Entity` response.

**Response `200 OK`:**
```typescript
interface SearchResponse {
  query: string;             // The query that was searched (echoed back)
  results: SearchResult[];   // Page of results
  count: number;             // Number of results in this response
  total: number;             // Total matching documents across all pages
  limit: number;             // Results per page (as requested)
  offset: number;            // Current offset (as requested)
  has_more: boolean;         // Whether there are more results beyond this page
}

interface SearchResult {
  vector_id: string;         // Opaque stable ID; use for /resource/{vector_id} lookups
  title: string;
  text: string;              // Excerpt/summary text
  score: number;             // Relevance score (higher = more relevant)
  metadata?: ResourceMetadata;
}
```

**Error responses:**
- `401` — No or expired session
- `403` — Inactive membership
- `422` — Validation error (invalid categories, query too long, etc.)
- `429` — Rate limit exceeded

---

#### `GET /resource/{vector_id}`

Fetches the full detail for a single resource. Treat the `vector_id` returned in search
results as an opaque stable identifier.

**Path parameters:**

| Parameter | Type | Description |
|---|---|---|
| `vector_id` | `string` | Opaque stable identifier for the resource |

**Response `200 OK`:**
```typescript
interface ResourceDetail {
  vector_id: string;
  title: string;
  text: string;              // Full text content
  doc_type: string;          // Document type (corresponds to category)
  metadata: ResourceMetadata;
}
```

**Error responses:**
- `401` — No or expired session
- `403` — Inactive membership
- `404` — Resource not found
- `429` — Rate limit exceeded

---

#### `GET /resource/{vector_id}/similar`

Returns a list of semantically similar resources for the "You might also like" panel.

**Path parameters:**

| Parameter | Type | Description |
|---|---|---|
| `vector_id` | `string` | Opaque stable identifier of the source resource |

**Response `200 OK`:**
```typescript
interface SimilarResourcesResponse {
  vector_id: string;                    // Source resource ID (echoed back)
  similar_resources: SimilarResource[]; // Up to 10 similar results
  count: number;
}

interface SimilarResource {
  vector_id: string;       // Use for /resource/{vector_id} lookups
  resource_id: string;     // Original source system resource ID
  title: string;
  score: number;           // Similarity score
  metadata: ResourceMetadata;
}
```

**Error responses:**
- `401` — No or expired session
- `403` — Inactive membership
- `404` — Source resource not found
- `429` — Rate limit exceeded

---

## Shared Types

### Identifier stability

`vector_id` is part of public resource URLs. Clients must not derive or parse it. The current index uses generated
hashes; the planned publication system will derive IDs from permanent exported record IDs while preserving
existing IDs for confidently matched records. See
[backend issue #11](https://github.com/ireapps/ire-archive-backend/issues/11).

### `ResourceMetadata`

Metadata is returned on every `SearchResult`, `ResourceDetail`, and `SimilarResource`.
All fields are optional since metadata completeness varies by resource.

```typescript
interface ResourceMetadata {
  vector_id?: string;
  resource_id?: string;
  authors?: string;
  affiliations?: string;
  category?: string;              // One of the valid category values
  subject?: string;
  description?: string;
  tags?: string[];
  keywords?: string[];
  conference?: string;
  year?: string;
  published?: string;
  resource_year?: number | null;
  date_created?: string;
  date_updated?: string;
  contest_name?: string;
  contest_entry_status?: string;
  downloads?: Download[];         // Downloadable files attached to this resource
  speakers?: string[];
  [key: string]: any;             // Additional fields may be present
}

interface Download {
  id: string;
  url: string;       // Direct URL to downloadable file
  name: string;      // Filename for display
}
```

---

## Standardized Error Response

All API errors return a consistent JSON body:

```typescript
interface ErrorResponse {
  error: string;          // Machine-readable error code (e.g., "NOT_FOUND", "VALIDATION_ERROR")
  message: string;        // Human-readable description
  status_code: number;    // HTTP status code (mirrors the HTTP response status)
  request_id?: string;    // Opaque request identifier for support/debugging
}
```

---

## Rate Limiting

Protected endpoints include rate-limit response headers when the limit is approached:

| Header | Description |
|---|---|
| `X-RateLimit-Limit` | Maximum requests allowed in the window |
| `X-RateLimit-Remaining` | Requests remaining in the current window |
| `X-RateLimit-Reset` | Unix timestamp when the window resets |

When the limit is exceeded, the backend returns `429 Too Many Requests`.

Default limits (all configurable via environment variables):

| Endpoint | Default limit |
|---|---|
| `POST /search` | 60/minute, 10/second |
| `GET /resource/{id}` | 120/minute, 20/second |
| `GET /stats` | 30/minute |

---

## Dev / Test Mode

The frontend supports a `VITE_AUTH_BYPASS=true` mode that bypasses all backend calls and
returns mock data instead. This is used in CI E2E tests to avoid requiring a live backend.
See `site/src/lib/api.ts` for the mock data definitions.
