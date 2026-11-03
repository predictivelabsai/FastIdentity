# FastIdentity

**First-party, consent-first identity resolution for B2C websites.**

FastIdentity turns high-intent sessions on your site into known, consented contacts you can message. Examples of high intent are viewing a treatment, starting a booking or abandoning a cart. It is built to stay inside UK GDPR, EU GDPR and PECR. It is part of the [FastSME](https://fastsme.com) stack (FastHTML + HTMX, SQLite or Postgres) and is deployed at `identity.fastsme.com`.

```
 visitor browses ──▶ consent banner ──▶ fi.js collects (only after consent)
                                         │
                       fills a form ───▶ identify(email, explicit opt-in)
                                         │
              returns on same device ──▶ probabilistic match (only with consent) ──▶ review / auto-merge
                                         │
                 abandons booking ─────▶ abandonment flow ──▶ consent + suppression + frequency checks ──▶ ESP
```

---

## Contents

- [What it is (and is not)](#what-it-is-and-is-not)
- [Features](#features)
- [Quick start](#quick-start)
- [Core concepts](#core-concepts)
  - [Consent purposes](#consent-purposes)
  - [Resolution rules](#resolution-rules)
  - [Probabilistic matching](#probabilistic-matching)
  - [Activation](#activation)
- [Installing on a website](#installing-on-a-website)
- [Server-side SDK](#server-side-sdk)
- [HTTP API](#http-api)
- [Admin UI](#admin-ui)
- [Configuration](#configuration)
- [Running in production](#running-in-production)
- [Project layout](#project-layout)
- [Testing](#testing)
- [Roadmap](#roadmap)

---

## What it is (and is not)

**It is** a small, owned customer-identity layer:

- a first-party event collector
- an identity graph with deterministic and probabilistic matching
- a consent ledger
- abandonment flows that hand messages to your email provider (ESP)

**It is not** a way to put a name to anonymous UK/EU visitors. US "visitor identification" vendors (Retention, Opensend and similar) do this with purchased identity graphs. That approach is very hard to justify under PECR and UK/EU GDPR. FastIdentity only resolves people who:

- identified themselves (form, login, booking), or
- consented to cross-session linking and match an already-known profile.

For a UK consumer business (for example, a medical marketplace), the best return usually comes from three things together:

- strong first-party consent collection
- a clean identity graph
- well-timed abandonment messages to people who have shown intent

That is the job this tool does.

## Features

| Area | What you get |
|---|---|
| **Collection** | `fi.js` browser collector (~3 KB gzipped, unminified) that understands CookieYes, OneTrust and Google Consent Mode v2 consent. It auto-captures page views, form starts, time on page, UTM parameters and click events (via `data-fi-event`). There is also a no-JS pixel and a Segment-style server API. |
| **Identity graph** | Profiles, identifiers, hashed device/network signals and an append-only event log. Profiles merge deterministically on email, phone, user id, CRM id or anonymous id. Every merge can be undone and is audited. |
| **Probabilistic matching** | Built-in Fellegi-Sunter scorer over HMAC-hashed signals, with blocking, a review queue, guarded auto-merge and a match-quality dashboard. Optional [Splink](https://github.com/moj-analytical-services/splink) engine. |
| **Domain interests** | Event properties such as `treatment`, `hospital` or `category` become per-profile interest counts. Segments and messages can use them. |
| **Activation** | Browse, cart and booking abandonment flows. Each send passes the consent gate, the suppression list and the frequency cap. Destinations: Klaviyo, Customer.io, Braze, signed webhook and a dry-run log. See [docs/esp_integration.md](docs/esp_integration.md). |
| **Segments** | Rule builder over status, traits, interests, event counts and consent. |
| **Compliance** | Purpose-based consent ledger with legal basis and evidence, hashed suppression list, right-to-erasure and subject-access export (UI and API), retention jobs, audit log and a UK/EU data-region flag. |
| **Admin UI** | FastHTML pages for the dashboard, profiles and timelines, events, merge candidates, match quality, segments, flows, destinations, consent, privacy requests, audit log and sources. |

## Quick start

Requires Python 3.12+.

```bash
git clone https://github.com/predictivelabsai/FastIdentity.git
cd FastIdentity
uv venv .venv && uv pip install -p .venv/bin/python -r requirements.txt
#   (or: python -m venv .venv && .venv/bin/pip install -r requirements.txt)
cp .env.example .env                    # defaults are fine locally

.venv/bin/python seed.py --demo         # bootstrap + synthetic visitors run through the real pipeline
.venv/bin/python app.py                 # http://localhost:5023
.venv/bin/python -m workers.run         # in a second terminal: matching + flows + retention
```

Sign in with `admin@fastsme.com` / `admin` (set by `ADMIN_EMAIL` / `ADMIN_PASSWORD`).

`seed.py --demo` creates about 25 synthetic people (all `@example.com`). Some:

- browse treatments, start bookings, opt in or decline email, and complete bookings
- return on the same device with cleared cookies (auto-merge, or review when ambiguous)
- refuse consent altogether (dropped)

All abandonment messages go to the **dry-run log** destination, so nothing is sent anywhere.

With Docker (app + worker + Postgres 16):

```bash
cp .env.example .env
docker compose up --build
```

## Core concepts

### Consent purposes

Every profile has an append-only **consent ledger**. The current state is the latest record for each purpose. Each record stores the legal basis, the source and evidence such as the opt-in wording and the page URL.

| Purpose | Meaning | Set by |
|---|---|---|
| `analytics` | Behavioural events may be collected at all | Cookie banner (CMP) |
| `personalisation` | Events may tailor the site experience | CMP "functional" / "preferences" |
| `identity_resolution` | This browser may be linked to other sessions probabilistically | CMP "marketing" / "advertisement" |
| `marketing_email` | Marketing email may be sent | **Explicit opt-in only**: form checkbox via `identify`, staff, or the API |
| `marketing_sms` | Marketing SMS may be sent | Explicit opt-in only |

Legal bases are `consent`, `soft_opt_in` (PECR reg. 22(3), existing customers), `legitimate_interest` and `contract`. Email and SMS marketing require `consent` or `soft_opt_in`.

CMP categories map to purposes as follows. The mapping is in `domain/consent.py:CMP_CATEGORY_MAP` and is easy to extend.

| CMP | analytics | personalisation | identity_resolution |
|---|---|---|---|
| CookieYes | `analytics` | `functional` | `advertisement` |
| OneTrust | `C0002` | `C0003` | `C0004` |
| Google Consent Mode | `analytics_storage` | `personalization_storage` | `ad_storage` |

### Resolution rules

These rules are the compliance core. Each one is covered by tests.

1. **No consent, no data.** A browser event without `analytics` consent is dropped; only a daily counter goes up. `fi.js` sets no cookie before consent and deletes its id when consent is withdrawn. Sources of kind `server` (your own backend) are not gated.
2. **Cookie consent is not email consent.** Accepting marketing cookies only grants `identity_resolution`. It never grants `marketing_email`.
3. **Deterministic links merge automatically.** When an `identify` call carries an identifier that already belongs to another profile, the two profiles merge (method `deterministic`). Identifiers are the email (lower-cased), the phone (normalised to E.164, so UK `07…` becomes `+447…`), `user_id`, `crm_id` and `anonymous_id`. The survivor is the known profile, or else the older one.
4. **Probabilistic links need consent on both sides.** See [below](#probabilistic-matching).
5. **Every merge can be undone.** Identities, signals and events keep an immutable `origin_profile_id`, and `merge_log` stores a snapshot of the survivor. **Unmerge** moves everything that originated on the loser back to it.
6. **Withdrawals win.** When two profiles merge, a withdrawal on either side is applied to the survivor. A merge can never re-enable marketing.
7. **Anonymous intent is never messaged.** Flows only target `known` profiles that pass the messaging gate.
8. **Erasure is thorough but remembers "do not contact".** It deletes events, identifiers, signals, consent records and messages for the profile and everything merged into it. It keeps a PII-free tombstone row and a hashed suppression entry, so a person who erased their data and later opts in again is still not emailed. Lifting a suppression is a deliberate manual step; there is no UI for it yet.

### Probabilistic matching

Signals are derived from the request context, and only when `identity_resolution` is granted. Each one is HMAC-SHA256'd with `IDENTITY_HASH_SECRET`; raw values are never stored.

| Signal | Source |
|---|---|
| `device` | user agent + screen + timezone + language + platform |
| `net` | IPv4 /24 or IPv6 /48 prefix (private and loopback addresses are ignored) |
| `ua_family` | e.g. Safari/Mac OS X |
| `tz` | time zone |
| `lang` | primary language |

Matching steps:

1. **Blocking.** Only pairs that share a `device` or `net` hash are compared.
2. **Scoring.** Each comparison has `(m, u)` probabilities, and the Bayes factors are combined with a prior of 0.001. Comparisons with missing data count as neutral.

   | Comparison | m | u |
   |---|---|---|
   | device | 0.90 | 0.0005 |
   | net | 0.70 | 0.01 |
   | ua_family | 0.95 | 0.30 |
   | tz | 0.98 | 0.40 |
   | lang | 0.97 | 0.60 |
   | interests (Jaccard ≥ 0.3) | 0.60 | 0.10 |

   So the same device on the same network scores about 0.999, and the same device on a different network about 0.87.
3. **Policy.**
   - A score of at least `REVIEW_THRESHOLD` (0.70) creates a **merge candidate**.
   - Anonymous → known merges automatically when the score is at least `AUTO_MERGE_THRESHOLD` (0.97) **and** the anonymous profile plausibly matches only one known person. Popular phone and laptop models collide, so ambiguous matches go to review.
   - Known ↔ known pairs (for example a shared family laptop) **never** auto-merge.
   - Anonymous ↔ anonymous pairs are ignored.
4. **Quality.** The *Match quality* page shows:
   - the known rate
   - reviewer precision (accepted ÷ reviewed)
   - an auto-merge precision proxy (1 − undone ÷ auto-merged)
   - the score distribution

   Use it to tune the thresholds.

Set `MATCH_ENGINE=splink` and run `pip install -r requirements-splink.txt` to score with Splink (DuckDB backend, EM-trained). Blocking and policy stay the same. Below about 50 profiles, matching falls back to the built-in scorer.

### Activation

A **flow** is defined by:

- a trigger event
- an exit event
- a delay
- a lookback window
- a destination

The worker finds known profiles whose latest trigger is older than the delay and who have no exit event since. Each one passes three checks before the destination is called:

- **consent gate:** `marketing_email` granted under `consent` or `soft_opt_in`
- **suppression list**
- **frequency cap:** default 2 messages per 7 days per profile, overridable per flow

Every decision is written to `message` as `sent`, `dry_run`, `failed` or `skipped` with a reason. There is one row per (flow, trigger event), so runs are idempotent.

Seeded templates:

| Flow | Trigger → exit | Delay / lookback |
|---|---|---|
| Browse abandonment | `product_viewed` → `booking_started` | 2 h / 48 h |
| Cart abandonment | `cart_started` → `order_completed` | 1 h / 72 h |
| Booking abandonment | `booking_started` → `booking_completed` | 45 min / 72 h |

FastIdentity decides **who** to message and **when**. Your ESP owns templates and delivery. Wiring up Klaviyo, Customer.io, Braze or your own webhook is covered in **[docs/esp_integration.md](docs/esp_integration.md)**.

## Installing on a website

**1. Create a source.** In **Sources & pixel**, set `allowed_origins` to the site's origins. Copy the write key.

**2. Serve it first-party (recommended).** CNAME a subdomain of the client's site (e.g. `t.mymedicalgateway.com`) to the FastIdentity host and use that as `host`. Requests then stay same-site.

**3. Load the collector after the CMP:**

```html
<script>
  window.FastIdentityConfig = { key: "wk_…", host: "https://t.mymedicalgateway.com", cmp: "auto" };
</script>
<script async src="https://t.mymedicalgateway.com/static/fi.js"></script>
```

`cmp: "auto"` detects CookieYes, OneTrust and Google Consent Mode, including late-loading CMPs. If you use another CMP, set `cmp: "manual"` and call:

```js
fastidentity.consent({ analytics: true, marketing: false, functional: true });
```

**4. Send intent and identity:**

```js
// Intent (property keys in INTEREST_KEYS become profile interests)
fastidentity.track("product_viewed",  { treatment: "hip replacement", hospital: "London Bridge" });
fastidentity.track("booking_started", { treatment: "hip replacement", hospital: "London Bridge" });

// Identity + explicit opt-in from a form (wording is stored as evidence)
fastidentity.identify(null, { email: form.email.value, first_name: form.first_name.value }, {
  consent: { marketing_email: { granted: form.optin.checked, wording: "Email me about my enquiry and offers" } }
});
```

**Declarative tracking (no JS):**

```html
<a href="/book" data-fi-event="cta_clicked" data-fi-prop-treatment="cataract surgery">Book now</a>
<form data-fi-form="enquiry">…</form>   <!-- emits form_started on first focus -->
```

**Auto-captured events:**

- `page_view`
- `form_started`
- `page_engaged` (seconds on page, sent when the tab is hidden)
- UTM campaign parameters

Property keys that look like PII (`email`, `phone`, `name`, `address`, `dob`, `password`…) are stripped from event properties on the server. Identity belongs in `identify` traits.

**Without JavaScript.** Use the pixel, e.g. from a server-rendered page or an AMP template:

```
GET /collect/px?k=<write key>&aid=<anonymous id>&e=page_view&c=analytics,marketing&p=/path
```

## Server-side SDK

Use a source of kind **server**. Server sources are not consent-gated, so send only data you have a basis for.

```python
from sdk.fastidentity import FastIdentity

fi = FastIdentity("wk_…", host="https://identity.fastsme.com")

fi.identify(user_id="u_123", anonymous_id=request.cookies.get("_fi_aid"),
            traits={"email": "ada@example.com", "first_name": "Ada"},
            consent={"marketing_email": {"granted": True, "legal_basis": "soft_opt_in",
                                         "wording": "Existing patient – similar services"}})
fi.track("booking_completed", user_id="u_123", properties={"treatment": "cataract surgery", "value": 2450})
fi.consent({"marketing_email": False}, email="ada@example.com")       # unsubscribe
fi.erase(admin_token=ADMIN_API_TOKEN, email="ada@example.com")       # right to erasure
```

Pass the browser's `_fi_aid` cookie as `anonymous_id`. That links server-side events to the browser session deterministically.

## HTTP API

| Method | Path | Auth | Body |
|---|---|---|---|
| `POST` | `/collect` | write key in body (`writeKey`) | one message or `{"batch": [...]}` (max 100). Responds `{"ok", "accepted"}` and never returns profile ids. Plain-text JSON avoids a CORS preflight. |
| `GET` | `/collect/px` | `k=` write key | Query params as above. Returns a 1×1 GIF. |
| `POST` | `/v1/identify` · `/v1/track` · `/v1/page` · `/v1/consent` | `Authorization: Bearer <write key>` | Segment-style: `userId`, `anonymousId`, `traits`, `event`, `properties`, `context`, `consent`, `messageId`, `timestamp` |
| `POST` | `/v1/export` | `Authorization: Bearer $ADMIN_API_TOKEN` | `{"email"\|"phone"\|"user_id"\|"profile_id": …}`. Returns the subject-access bundle. |
| `POST` | `/v1/erasure` | `Authorization: Bearer $ADMIN_API_TOKEN` | same lookup, optional `"keep_suppression": false` |
| `GET` | `/health` | none | `{"ok": true}` |

Message shape (all fields optional except an identifier):

```json
{
  "type": "track",
  "event": "booking_started",
  "anonymousId": "8f0c…",
  "userId": "u_123",
  "messageId": "uuid (idempotency key)",
  "timestamp": "2026-11-03T10:00:00Z",
  "properties": { "treatment": "MRI scan" },
  "traits": { "email": "…" },
  "consent": { "marketing_email": { "granted": true, "wording": "…" } },
  "context": { "consent": { "analytics": true, "marketing": true }, "page": { "path": "/mri" },
               "userAgent": "…", "screen": { "width": 1512, "height": 982 }, "timezone": "Europe/London", "locale": "en-GB" }
}
```

## Admin UI

| Page | Purpose |
|---|---|
| Dashboard | Profiles, known rate, email-contactable count, events, consent drops, pending merges, messages, recent activity |
| Profiles | Search and filter. The detail page shows identifiers, hashed-signal summary, consent (record or withdraw), interests, traits, timeline, messages and merge history with **Unmerge**, plus **Export**, **Erase** and **Sync to destination** |
| Events | Event stream with per-event-name filter |
| Merge candidates | Review queue with per-comparison evidence; **Merge** or **Reject**; **Run matching now** |
| Match quality | Precision, auto-merge and undo rates, score buckets |
| Segments | Rule builder and member lists |
| Flows | Create, enable or disable, **Run all flows now**, message log with skip reasons |
| Destinations | Configure ESPs and webhooks, readiness check |
| Consent | State per purpose, consent drops by day, suppression list, latest records, retention settings |
| Privacy requests | Find by email, phone or user id, then export or erase. Apply retention now. |
| Audit log | Every merge, unmerge, consent change, erasure, export, sync and admin action (no raw PII) |
| Sources & pixel | Write keys, allowed origins, copy-paste snippets |

## Configuration

All settings come from environment variables (`.env` is loaded automatically). See `.env.example`.

| Variable | Default | Notes |
|---|---|---|
| `SECRET_KEY` | `dev-only-change-me` | **Required in prod.** Session signing. |
| `IDENTITY_HASH_SECRET` | `dev-only-hash-secret` | **Required in prod.** HMAC key for signals and suppression hashes. Keep it stable: rotating it unlinks historical signals and suppression entries. |
| `ADMIN_EMAIL` / `ADMIN_PASSWORD` | `admin@fastsme.com` / `admin` | Bootstrap admin, created on first start. |
| `ADMIN_API_TOKEN` | *(empty: API disabled)* | Bearer token for `/v1/erasure` and `/v1/export`. |
| `PUBLIC_BASE_URL` | `http://localhost:5023` | Used in snippets and docs pages. |
| `ALLOWED_ORIGINS` | `*` | Fallback CORS allow-list when a source has none. |
| `DB_SOURCE` | `sqlite` | Backend switch: `sqlite` or `psql`. |
| `DATABASE_URL` (or `FASTIDENTITY_DATABASE_URL`, `DB_URL`) | *(empty)* | Postgres DSN, required when `DB_SOURCE=psql`. |
| `DB_SCHEMA` (or `FASTIDENTITY_DB_SCHEMA`) | `fast_identity` | Postgres schema; created on start if missing. |
| `DATABASE_PATH` | `data/fastidentity.db` | SQLite file (`DB_SOURCE=sqlite`). |
| `DEFAULT_TENANT` | `demo` | Single-tenant v1. |
| `FASTIDENTITY_PORT` | `5023` | |
| `DATA_REGION` | `uk` | Displayed. Enforce it by where you deploy the app and database. |
| `REQUIRE_ANALYTICS_CONSENT` | `true` | Drop browser events without analytics consent. |
| `EVENT_RETENTION_DAYS` | `395` | Events older than this are deleted. |
| `ANON_PROFILE_RETENTION_DAYS` | `90` | Anonymous profiles inactive this long are deleted. |
| `MATCH_ENGINE` | `builtin` | `builtin` or `splink`. |
| `AUTO_MERGE_THRESHOLD` / `REVIEW_THRESHOLD` | `0.97` / `0.70` | |
| `INTEREST_KEYS` | `treatment,hospital,category,product` | Event property keys folded into profile interests. |
| `DEFAULT_FREQUENCY_CAP` / `DEFAULT_FREQUENCY_WINDOW_DAYS` | `2` / `7` | |
| `FLOW_WORKER_INTERVAL_SECONDS` | `60` | Worker tick. Retention runs every 60 ticks. |
| `KLAVIYO_API_KEY`, `CUSTOMERIO_SITE_ID`, `CUSTOMERIO_API_KEY`, `CUSTOMERIO_REGION`, `BRAZE_API_KEY`, `BRAZE_REST_ENDPOINT`, `WEBHOOK_DEFAULT_URL`, `WEBHOOK_SIGNING_SECRET` | *(empty)* | Optional. See [docs/esp_integration.md](docs/esp_integration.md). |

## Running in production

- **Processes.** The web app (`python app.py`) and the worker (`python -m workers.run`). The Docker image runs both via `scripts/start.sh`. Set `RUN_WORKER=0` when the worker runs as its own service, as `docker-compose.yml` does.
- **Coolify.** The service is registered in FastDevOps (`config/services.yaml`, key `fastidentity`) at `https://identity.fastsme.com`, with the A record `identity` → `191.218.164.166`.
- **CI/CD.** A push to `main` triggers a GitHub webhook (push events only), which calls Coolify's manual webhook endpoint (`https://coolify.fastsme.com/webhooks/source/github/events/manual`). Coolify then builds the Dockerfile, waits for `/health` to pass, and swaps traffic over. For manual operations, run `python scripts/coolify.py status|env --sync --yes|deploy --yes` with FastDevOps checked out beside this repo (see `.env.coolify.sample`).
- **Database.** Use Postgres (`DB_SOURCE=psql` + `DATABASE_URL`). The FastSME shared server hosts it in schema `fast_identity`. Tables and indexes are created on start and the schema is idempotent. SQLite is fine for demos and single-site pilots.
- **Hosting region.** Deploy the app and database in the UK or EU to match `DATA_REGION`. Check your ESP's region too; see the ESP guide.
- **Domain.** `identity.fastsme.com` for the admin UI and API. Use a per-client first-party CNAME for collection.
- **Secrets.** Generate `SECRET_KEY`, `IDENTITY_HASH_SECRET` and `ADMIN_API_TOKEN` with `python -c "import secrets; print(secrets.token_urlsafe(48))"`. Change `ADMIN_PASSWORD`. Restrict `ALLOWED_ORIGINS` or per-source origins.
- **Your own paperwork.** Before going live, make sure you have:
  - an ROPA entry
  - a DPIA (profiling and health-related interests make this likely to be required)
  - a privacy notice that describes cross-session linking
  - a CMP configured so the marketing category explains device linking

## Project layout

```
app.py                  FastHTML routes (admin UI + public ingest)
collect.py              ingest pipeline, CORS, pixel, privacy API
settings.py  db.py      config; dual-mode SQLite/Postgres data layer + schema
seed.py                 bootstrap + synthetic demo traffic
domain/
  identity.py           deterministic resolution, merge / unmerge
  signals.py            hashed device / network signals
  matching.py           blocking, scoring, review, quality metrics, Splink adapter
  consent.py            purposes, CMP mapping, ledger, suppression, messaging gate
  activation.py         abandonment flows, frequency caps, ESP profile sync
  privacy.py            erasure, export, retention
  segments.py  audit.py
destinations/           log, webhook, Klaviyo, Customer.io, Braze adapters
ui/                     layout, auth, graph pages, ops/compliance pages
static/fi.js            browser collector
sdk/fastidentity.py     Python server SDK
workers/run.py          matching + flows + retention loop
tests/                  pytest suite (identity, consent, matching, activation, privacy/HTTP)
docs/esp_integration.md how to connect an ESP
```

## Testing

```bash
.venv/bin/python -m pytest -q
```

Tests force `DB_SOURCE=sqlite` with a temporary file and never touch the database in `.env`. They cover:

- deterministic merge and unmerge
- phone normalisation and idempotency
- PII stripping and the consent gates
- CMP mapping, withdrawal → suppression, and the legal-basis check
- auto-merge, review and the ambiguity guard
- flows, frequency caps, erasure, export, CORS, the pixel and the admin pages

## Roadmap

- [ ] Live ESP integrations, plus inbound unsubscribe and bounce webhooks ([plan](docs/esp_integration.md))
- [ ] Per-destination property allow-list (data minimisation for health-related interests)
- [ ] Retries with backoff for failed sends
- [ ] Rate limiting and bot filtering on `/collect`
- [ ] Multi-tenant admin and staff roles
- [ ] Segment → ESP list sync
- [ ] Visual flow editor (multi-step, branches)
- [ ] Splink validation on production volumes

## License

MIT. See [LICENSE](LICENSE).
