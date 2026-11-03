# FastIdentity

First-party, consent-first identity resolution for B2C sites, built with FastHTML for the [FastSME](https://fastsme.com) stack.
It turns high-intent sessions into known, consented contacts you can message, and stays inside UK GDPR, EU GDPR and PECR.

It does **not** silently turn anonymous visitors into email addresses. No open-source project can do that for UK/EU traffic,
and doing it with bought data is the compliance risk this tool is meant to avoid. What it does:

* collects first-party behaviour, but only after CMP consent
* links sessions deterministically when people identify themselves
* links sessions probabilistically only with `identity_resolution` consent, behind review and ambiguity guards
* sends abandonment messages only to people with a valid marketing basis

Target deployment: `identity.fastsme.com` (port **5023**).

## Quick start

```bash
uv venv .venv && uv pip install -p .venv/bin/python -r requirements.txt
cp .env.example .env          # defaults work locally
.venv/bin/python seed.py --demo   # bootstrap + synthetic visitors through the real pipeline
.venv/bin/python app.py           # http://localhost:5023  (admin@fastsme.com / admin)
.venv/bin/python -m workers.run   # matching + flows + retention loop (or --once)
.venv/bin/python -m pytest -q
```

To run with Docker and Postgres, use `docker compose up --build`. It starts the app, the worker and Postgres 16.

## Architecture

```
browser ──fi.js──▶ /collect ─┐                       ┌─▶ match_candidate ─▶ review UI
no-JS   ──────────▶ /collect/px│  consent gate          │
backend ──SDK─────▶ /v1/*    ─┴▶ deterministic resolve ─▶ identity graph ──▶ flows ─▶ Klaviyo / Customer.io /
                                 consent ledger           (profile, identity,         Braze / webhook / log
                                 hashed signals           signal, event, merge_log)
```

| Piece | Path |
|---|---|
| Ingest pipeline, CORS, pixel, privacy API | `collect.py` |
| Browser collector (CookieYes / OneTrust / Google Consent Mode aware) | `static/fi.js` |
| Deterministic resolution, merge, unmerge | `domain/identity.py` |
| Hashed device and network signals | `domain/signals.py` |
| Probabilistic matching (built-in Fellegi-Sunter, optional Splink) | `domain/matching.py` |
| Consent ledger, legal-basis gates, suppression | `domain/consent.py` |
| Abandonment flows, frequency caps, ESP sync | `domain/activation.py`, `destinations/` |
| Erasure, DSAR export, retention | `domain/privacy.py` |
| Rule segments | `domain/segments.py` |
| Admin UI | `ui/` |
| Python server SDK | `sdk/fastidentity.py` |
| Worker | `workers/run.py` |

Storage is SQLite by default. Set `FASTIDENTITY_DATABASE_URL` to use Postgres with its own schema; this is the same dual-mode pattern as FastCDP and FastERP.

## Resolution rules (the compliance core)

1. **No consent, no data.** Browser events without CMP `analytics` consent are dropped and only counted.
   The script sets no cookie until consent is given. Server sources (`kind=server`) are not gated.
2. **Cookie consent ≠ email consent.** CMP categories map only to `analytics`, `personalisation` and `identity_resolution`.
   `marketing_email` and `marketing_sms` come only from an explicit opt-in sent with `identify` (the wording and page are stored as evidence), from staff, or from the API.
3. **Deterministic links** (the same email, phone, user id or anonymous id) merge automatically. The person identified themselves on that browser.
4. **Probabilistic links** need `identity_resolution` consent on *both* profiles. Raw signals are never stored, only HMACs with `IDENTITY_HASH_SECRET`.
   * Anonymous → known auto-merge needs a score of at least `AUTO_MERGE_THRESHOLD` **and** a single plausible known match.
     Common phone and laptop models collide, so an anonymous profile that matches two people goes to review.
   * Known ↔ known pairs, such as a shared family laptop, never auto-merge.
5. **Every merge can be undone.** Rows keep `origin_profile_id`, and `merge_log` snapshots the survivor.
   When two profiles merge, a withdrawal on either side always wins.
6. **Messaging gate.** A message needs:
   * a `marketing_email` grant under `consent` or `soft_opt_in` (PECR reg. 22)
   * no suppression
   * room under the frequency cap
   * a known profile

   Anonymous intent is never messaged.
7. **Erasure** deletes events, identifiers, signals, consent and messages for the profile and everything merged into it.
   It leaves a PII-free tombstone and keeps a *hashed* suppression entry, so a returning opt-in still cannot be emailed.

## Spec coverage

| Phase | Status |
|---|---|
| 0: First-party collection, domain models (`INTEREST_KEYS` such as treatment/hospital → profile interests) | ✅ |
| 1: Identify, deterministic merge, profile and timeline UI, consent on every profile | ✅ |
| 2: Probabilistic candidates, review queue, auto-merge, match-quality dashboard, Splink adapter | ✅ built-in engine; Splink adapter written but untested at volume |
| 3: Browse/cart/booking abandonment flows, ESP sync (Klaviyo, Customer.io, Braze, webhook), suppression, frequency caps | ✅ dry-run by default; live ESP calls untested without keys |
| 4: CMP integration, retention, audit log, erasure/export API | ✅ first pass |

## Not done yet

* Multi-tenant admin. The schema has `tenant_id`, but the UI is single-tenant.
* Rate limiting and bot filtering on `/collect`.
* Visual flow editor. Flows are trigger → wait → exit → destination rows, and the ESP owns templates.
* Segment → ESP list sync. Profile sync is per-profile today.
* Splink validation on real volumes.
* Role-based access for staff.

## Endpoints

| Method | Path | Auth |
|---|---|---|
| POST | `/collect` (single or `{"batch":[…]}`) | write key in body |
| GET | `/collect/px?k=&aid=&e=&c=analytics,marketing&p=` | write key |
| POST | `/v1/identify` `/v1/track` `/v1/page` `/v1/consent` | `Bearer <write key>` |
| POST | `/v1/erasure` `/v1/export` (`{email|phone|user_id|profile_id}`) | `Bearer $ADMIN_API_TOKEN` |
| GET | `/health` | none |

## Configuration

See `.env.example`. The production must-haves are:

* `SECRET_KEY`
* `IDENTITY_HASH_SECRET`
* `ADMIN_PASSWORD`
* `ADMIN_API_TOKEN`
* `PUBLIC_BASE_URL`
* `ALLOWED_ORIGINS`
* `FASTIDENTITY_DATABASE_URL`

ESP keys are optional.
