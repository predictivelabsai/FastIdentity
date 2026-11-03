# ESP integration guide

This guide explains how to connect FastIdentity's activation layer to an email/SMS service provider (ESP): Klaviyo, Customer.io, Braze or your own system via a signed webhook. It covers:

- what FastIdentity sends
- how to build the matching journey in each ESP
- how to report unsubscribes and bounces back
- what to check before go-live

> **Status.** The adapters live in `destinations/__init__.py` and are unit-level only. They build the requests below, but none has been run against a live ESP account yet. Everything defaults to the **dry-run log** destination, so no messages leave the system until you configure one. Vendor APIs change; check each request shape against the vendor's current API reference when you wire it up.

---

## 1. Division of labour

```
FastIdentity                                         ESP
────────────                                         ───
who + when                                           what + how
  • identity graph (who is this person?)               • templates, copy, design
  • intent (what did they look at / abandon?)          • sending, deliverability, DKIM/SPF
  • consent ledger + suppression (may we?)             • its own subscription state
  • frequency cap (too much?)                          • opens / clicks / bounces
  • decision log (message table)                       • A/B tests, send-time optimisation
          │                                                     ▲
          └──── "FastIdentity Booking abandonment" event ───────┘
          ◀──── unsubscribe / bounce / complaint (to build) ────┘
```

FastIdentity never renders or sends email itself. Each flow emits **one custom event per qualifying person**. In the ESP you build a journey (Klaviyo *Flow*, Customer.io *Campaign*, Braze *Canvas*) triggered by that event, and the ESP sends the message.

Why split it this way:

- Your marketing team keeps the tools they know.
- FastIdentity's consent and frequency rules apply **before** any data reaches the ESP.
- Changing ESP means changing an adapter, not your identity data.

## 2. What FastIdentity sends

### 2.1 Flow trigger event (`send_event`)

When a flow fires, the worker calls `destination.send_event(name, profile, properties)` (see `domain/activation.py:run_flow`).

- **Event name:** `FastIdentity <flow name>`, e.g. `FastIdentity Booking abandonment`. Create one ESP journey per event name.
- **Profile object** (`public_profile`):

  ```json
  {
    "id": "p_3f1c…",
    "email": "ada@example.com",
    "phone": "+447700900123",
    "first_name": "Ada",
    "interests": { "treatment": { "hip replacement": 3 }, "hospital": { "London Bridge": 2 } }
  }
  ```

- **Properties:** the trigger event's own properties, plus flow metadata:

  ```json
  {
    "treatment": "hip replacement",
    "hospital": "London Bridge",
    "flow": "Booking abandonment",
    "template": "tmpl_booking_v2",
    "trigger_event": "booking_started",
    "trigger_ts": "2026-11-03T09:12:44+00:00"
  }
  ```

  `template` is a free-text field on the flow. Use it to pass a template or journey id, or a variant name, when one ESP journey branches on it.

### 2.2 Profile sync (`sync_profile`)

Click **Sync to <destination>** on a profile page, or call `domain.activation.sync_profile(tenant, profile_id, destination_id)`. This upserts the contact with scalar traits only (strings, numbers, booleans). It goes through the same `can_message(profile, "email")` gate, so you cannot sync people without email consent.

### 2.3 Who gets an event

An event is sent only when **all** of these hold. Anything else is logged in `message` as `skipped` with a reason.

| Check | Skip reason |
|---|---|
| Profile is known (has an email/phone/user id) and not merged or erased | *(not logged; anonymous intent is ignored)* |
| No exit event since the trigger (e.g. no `booking_completed`) | *(not logged; the person converted)* |
| Has an address for the channel | `no_address` |
| `marketing_email` granted | `no_consent` |
| Legal basis is `consent` or `soft_opt_in` | `invalid_legal_basis` |
| Address is not on the suppression list | `suppressed` |
| Under the frequency cap (default 2 per 7 days, all flows combined) | `frequency_cap` |

Result statuses: `dry_run` (log destination), `sent` (ESP returned 2xx), `failed` (non-2xx, network error or missing config; the detail holds the response).

## 3. Before you connect anything: design decisions

### 3.1 Health data and data minimisation (read this for medical sites)

Treatment and hospital interests can reveal health information. That puts them in the **special category data** of UK/EU GDPR Art. 9; "viewed IVF consultation three times" is health data about a person. Sending it to an ESP means:

- extra processing
- a transfer to the ESP (often outside the UK/EU)
- the data appearing in email copy, inbox previews, and possibly on a shared screen

Recommendations:

1. **Send the minimum.** Today the adapter forwards **all** trigger-event properties. Before go-live, add a per-destination property allow-list (§8, gap 1). A generic nudge ("You left a booking unfinished") often converts nearly as well as a specific one.
2. If you do personalise by treatment, make sure the opt-in wording covers it (e.g. "Email me about the treatments I've looked at"). Record that wording through `identify(..., consent={"marketing_email": {"wording": ...}})`; it is stored as evidence. Cover this processing in your DPIA.
3. Never put treatment names in subject lines or preview text.
4. Don't sync `interests` as ESP profile properties unless the ESP is in scope of your DPIA. `public_profile` currently includes `interests`.

### 3.2 Hosting region and contracts

| ESP | Region options | Notes |
|---|---|---|
| Klaviyo | US-hosted | UK/EU personal data is transferred to the US. Rely on the vendor DPA plus the UK–US data bridge / EU–US DPF or SCCs. Confirm the current position with the vendor and record it in your ROPA. |
| Customer.io | **EU data centre** available | Create the workspace in the EU region and set `CUSTOMERIO_REGION=eu` (the default), so the adapter uses `track-eu.customer.io`. |
| Braze | **EU clusters** (e.g. `fra-02`) | Set `BRAZE_REST_ENDPOINT` to your cluster's REST endpoint (default `https://rest.fra-02.braze.eu`). |
| Webhook | Wherever you host it | Your own system, your responsibility. |

You need a signed DPA with whichever ESP you use. This is not a technical step, but it blocks go-live.

### 3.3 Identity keys and merges

The adapters use the FastIdentity **profile id** as the ESP's external id. Klaviyo uses it as `external_id` (alongside email), Customer.io as the customer id, and Braze as `external_id`.

**Caveat:** when two FastIdentity profiles merge, events from then on use the **survivor's** id. If the ESP already holds the loser's id as a separate contact, the ESP will now have two contacts for one person. Choose one approach:

| Option | When to use |
|---|---|
| **A. Key the ESP on email** (recommended) | Klaviyo and Customer.io can identify by email. Drop the profile id from the identifier, or send it only as a property. Merges become invisible to the ESP. |
| **B. Forward merges** | Add a hook in `domain/identity.py:merge` that calls the ESP's merge or alias API. Braze has `/users/merge`, Customer.io has a merge-customers endpoint, and Klaviyo merges by email automatically. More work, fully accurate. |
| **C. Accept duplicates** | Fine for a pilot with low volumes. |

Unmerge cannot be mirrored in most ESPs. That is another reason to prefer A.

### 3.4 Two consent states

The ESP keeps its own subscription state. Klaviyo marketing flows send only to subscribed profiles, Braze has `email_subscribe`, and Customer.io has `unsubscribed`. If you trigger an event for someone the ESP thinks is not subscribed, a marketing journey will normally not send. That is the correct, fail-safe outcome, but it will look like "nothing happened".

You therefore need to:

1. **Push opt-ins to the ESP** when `marketing_email` is granted. The calls for each ESP are in §4.
2. **Pull opt-outs back** from the ESP into FastIdentity. Unsubscribe links are hosted by the ESP. See §5.

FastIdentity stays the system of record for **evidence** (who agreed, when, to what wording). The ESP subscription flag is a cache of it.

### 3.5 Frequency capping in two places

FastIdentity caps per profile across all its flows. ESPs have their own caps (Klaviyo *Smart Sending*, Braze *frequency capping*, Customer.io *message limits*) that also count newsletters and campaigns FastIdentity doesn't see. Keep both on. If messages seem to vanish, check the ESP's cap before suspecting FastIdentity.

## 4. Per-ESP setup

The steps are the same for every ESP:

1. Get credentials and put them in `.env`, or in the destination's JSON config.
2. In **Destinations**, add a destination of the right kind and confirm it shows **ready**.
3. In **Flows**, create a flow that uses it. The UI can't edit existing flows yet; to repoint a seeded flow, update `flow.destination_id` in the database.
4. Run **Run all flows now** against a test profile (your own email, opted in), then check the message log and the ESP's activity feed.
5. Build the journey in the ESP, triggered by the event name.

Credentials are resolved per key as follows: a value in the destination's `config_json` wins; otherwise the environment variable is used. Prefer environment variables (or a secrets manager), because `config_json` is stored in the database in plain text.

### 4.1 Klaviyo

**Credentials.** Create a *private API key* with the narrowest scopes possible. You need write access to events and profiles, and to subscriptions if you implement §3.4.

```
KLAVIYO_API_KEY=pk_…
```

Optional per-destination config: `{"revision": "2024-10-15"}`. This pins the API revision header; update it deliberately, never automatically.

**What the adapter calls:**

- `send_event` → `POST https://a.klaviyo.com/api/events/` with headers `Authorization: Klaviyo-API-Key …` and `revision: …`.

  ```json
  {"data": {"type": "event", "attributes": {
    "properties": { "treatment": "hip replacement", "flow": "Booking abandonment", "...": "..." },
    "metric":  {"data": {"type": "metric",  "attributes": {"name": "FastIdentity Booking abandonment"}}},
    "profile": {"data": {"type": "profile", "attributes": {"external_id": "p_…", "email": "ada@example.com", "phone_number": "+447…"}}}
  }}}
  ```

  The metric is created automatically on the first event.
- `sync_profile` → `POST /api/profile-import/` (create or update by email/external id) with traits as `properties`.

**Build the journey.** In Klaviyo, go to Flows → Create flow → trigger *Metric* → `FastIdentity Booking abandonment`. Then:

- Set the flow's delay to **0**. FastIdentity already waited `delay_minutes`.
- Template variables: `{{ person.first_name|default:"there" }}`, `{{ event.hospital }}`, `{{ event.template }}`.
- Optional flow filter: `{{ event.template }} equals tmpl_booking_v2` to run variants.

**Subscription push (to implement).** When `marketing_email` is granted, subscribe the profile with Klaviyo's bulk subscribe job: `POST /api/profile-subscription-bulk-create-jobs/`. Send `subscriptions.email.marketing.consent = "SUBSCRIBED"` and `consented_at` set to the FastIdentity consent timestamp. A natural place is a hook after `record_consent` in `domain/consent.py`, or a dedicated `subscribe(profile)` method on the destination class. Klaviyo rejects backdated "subscribed" status without evidence for some list settings; check the list's opt-in type (single or double).

**Opt-outs back.** See §5. Use Klaviyo webhooks (if your plan has them) or poll for unsubscribed profiles.

### 4.2 Customer.io

**Credentials.** In Workspace settings → API credentials, get the **Track API** site id and API key. Create the workspace in the **EU region**.

```
CUSTOMERIO_SITE_ID=…
CUSTOMERIO_API_KEY=…
CUSTOMERIO_REGION=eu        # eu → track-eu.customer.io, us → track.customer.io
```

**What the adapter calls** (HTTP Basic auth `site_id:api_key`):

- `sync_profile` → `PUT /api/v1/customers/{profile_id}` with `{"email": …, …traits}`
- `send_event` → `POST /api/v1/customers/{profile_id}/events` with `{"name": "FastIdentity Booking abandonment", "data": {…properties}}`

Customer.io creates the person on first identify. Sync the profile once (or on opt-in) **before** the first event, or the event lands on a person with no email. Simplest fix: call `sync_profile` inside `send_event` for this adapter. If you choose to identify people by email (§3.3 option A), the workspace must allow email as an identifier.

**Build the journey.** Create a Campaign → trigger *Event* → event name `FastIdentity Booking abandonment` → email action.

- Liquid variables: `{{customer.first_name}}`, `{{event.hospital}}`, `{{event.template}}`.
- Set the campaign's message type to marketing (not transactional), so Customer.io respects `unsubscribed`.

**Subscription state.** Customer.io uses the `unsubscribed` attribute. On opt-in, send `{"unsubscribed": false}` in `sync_profile`. On opt-out, send `true`. Customer.io's hosted unsubscribe page sets it automatically; mirror that back into FastIdentity (§5) with a *Reporting webhook* for unsubscribed, bounced and spam-complaint events.

### 4.3 Braze

**Credentials.** Create a REST API key with the `users.track` permission only (add `users.merge` if you implement §3.3 option B). Find your instance's REST endpoint, e.g. `https://rest.fra-02.braze.eu`.

```
BRAZE_API_KEY=…
BRAZE_REST_ENDPOINT=https://rest.fra-02.braze.eu
```

**What the adapter calls:** `POST {endpoint}/users/track` with `Authorization: Bearer …`.

- `send_event` → `{"events": [{"external_id": "p_…", "name": "FastIdentity Booking abandonment", "time": "…", "properties": {…}}]}`
- `sync_profile` → `{"attributes": [{"external_id": "p_…", "email": "…", …traits}]}`

**Build the journey.** Create a Canvas with an *action-based* entry on the custom event `FastIdentity Booking abandonment`.

- Liquid variables: `{{${first_name}}}` (when synced as the standard attribute) and `{{event_properties.${hospital}}}`.

**Subscription state.** Set `email_subscribe` to `"opted_in"` on opt-in and `"unsubscribed"` on opt-out, as part of the `attributes` payload. Target the Canvas at "opted-in users only". Use Braze Currents, or a webhook from a Canvas, to report unsubscribes back (§5).

### 4.4 Generic webhook

Use this for an in-house mailer, Zapier/Make, n8n, FastCRM, Listmonk, Postmark/SES behind your own code, or anything else.

```
WEBHOOK_DEFAULT_URL=https://hooks.example.com/fastidentity
WEBHOOK_SIGNING_SECRET=…     # strongly recommended
```

Per-destination override: `{"url": "...", "secret": "..."}`.

**Payloads** (`POST`, `content-type: application/json`):

```json
{"type": "event", "event": "FastIdentity Booking abandonment",
 "profile": {"id": "p_…", "email": "…", "phone": "…", "first_name": "…", "interests": {…}},
 "properties": {"treatment": "…", "flow": "…", "template": "…", "trigger_event": "…", "trigger_ts": "…"}}

{"type": "profile", "profile": {…}, "traits": {…}}
```

**Signature.** When a secret is set, the header `x-fastidentity-signature: sha256=<hex HMAC-SHA256 of the raw body>` is added. Verify it against the raw bytes, before parsing:

```python
import hashlib, hmac

def verify(raw_body: bytes, header: str, secret: str) -> bool:
    expected = "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header or "")
```

Return any 2xx to acknowledge. Anything else is recorded as `failed`. The request times out after 10 s. There are no retries yet (§8, gap 2). The receiver should be idempotent on `(profile.id, properties.trigger_ts, event)`.

## 5. Closing the loop: unsubscribes, bounces, complaints (to build)

Unsubscribe links are hosted by the ESP, so FastIdentity only learns about opt-outs if the ESP tells it. Until this is built, an ESP-side unsubscribe is respected by the ESP but **not** by FastIdentity. FastIdentity would keep sending trigger events; the ESP drops them, so no email goes out, but the logs and dashboards would be wrong.

**What already exists:** `POST /v1/consent` on a server source records a withdrawal and adds a hashed suppression entry automatically.

```bash
curl -X POST https://identity.fastsme.com/v1/consent \
  -H "authorization: Bearer $SERVER_WRITE_KEY" -H "content-type: application/json" \
  -d '{"traits": {"email": "ada@example.com"}, "consent": {"marketing_email": {"granted": false, "wording": "ESP unsubscribe"}}}'
```

**What to build** (suggested design):

1. Add `POST /hooks/{kind}` in `app.py`, public and listed in `PUBLIC`/`skip`. Each `kind` has a verifier for that vendor's webhook signature:
   - Klaviyo and Customer.io sign with HMAC
   - Braze uses Currents or a Canvas webhook with a shared-secret header

   Reject unsigned requests.
2. Normalise each vendor event to one of:
   - `unsubscribe` → `record_consent(..., "marketing_email", False, source=f"esp:{kind}")`
   - `hard_bounce` / `spam_complaint` → `suppress(tenant, "email", address, reason=…)`, without changing consent
   - `resubscribe` (only from an ESP-hosted preference centre with its own evidence) → `record_consent(..., True, source=f"esp:{kind}", evidence={…})`
3. Look the person up by email (`domain.privacy.lookup`). If nobody matches, still add the suppression; it is hashed, so this costs nothing and protects you.
4. Audit every call (`domain.audit.audit`).
5. As a safety net, run a nightly **reconciliation** job that pulls the ESP's unsubscribed and suppressed list and applies anything missing.

## 6. Adding a new destination

Each adapter is a small class. Example, for a hypothetical "Acme Mail":

```python
# destinations/__init__.py
class AcmeDestination(Destination):
    kind = "acme"
    required = ("api_key",)

    def __init__(self, row):
        super().__init__(row)
        self.cfg.setdefault("api_key", settings.ACME_API_KEY)   # add to settings.py + .env.example

    def send_event(self, name, profile, properties):
        body = {"email": profile["email"], "event": name, "data": properties}
        return self._guard(lambda: _result(httpx.post(
            "https://api.acme.example/v1/events", json=body,
            headers={"Authorization": f"Bearer {self.cfg['api_key']}"}, timeout=TIMEOUT)))

    def sync_profile(self, profile, traits):
        ...

REGISTRY["acme"] = AcmeDestination
KINDS.append("acme")
```

Rules for adapters:

- Wrap network calls in `self._guard(...)`. It turns missing config and `httpx` errors into a `("failed", detail)` result instead of crashing the worker.
- Return `("sent", detail)` only on a 2xx response; `_result(resp)` does that for you.
- Never log or return the API key in `detail`; it is stored in `message.reason`.
- Keep the adapter stateless. FastIdentity owns consent, caps and idempotency.

**Testing without a network.** Monkeypatch `httpx.post` and assert on the request:

```python
import httpx
import destinations

def test_acme_payload(monkeypatch):
    seen = {}
    def fake_post(url, **kw):
        seen.update(url=url, **kw)
        return httpx.Response(202, request=httpx.Request("POST", url))
    monkeypatch.setattr(httpx, "post", fake_post)
    d = destinations.build({"kind": "acme", "config_json": '{"api_key": "k"}'})
    status, _ = d.send_event("FastIdentity Booking abandonment", {"id": "p_1", "email": "a@example.com"}, {"x": 1})
    assert status == "sent" and seen["json"]["event"].startswith("FastIdentity")
```

**End-to-end smoke test.** Point a webhook destination at a local receiver (`python -m http.server` won't accept POST; use a five-line Starlette or Flask app, or a request-bin service you trust with test data only). Run `seed.py --demo`, create a *Booking abandonment* flow (`booking_started` → `booking_completed`) that targets it, disable the seeded one, and click **Run all flows now**.

## 7. Go-live checklist

- [ ] DPA signed with the ESP. The transfer mechanism is recorded in the ROPA. The DPIA covers profiling, cross-session linking and any health-related personalisation.
- [ ] ESP workspace is in the UK/EU region where available (§3.2).
- [ ] Opt-in wording on every form is final and passed in `identify(..., consent=...)`. Soft opt-in is only used for genuine existing customers, with an opt-out offered at collection and in every message.
- [ ] Identity key strategy chosen (§3.3).
- [ ] Opt-ins are pushed to the ESP's subscription state (§3.4).
- [ ] Unsubscribe, bounce and complaint webhooks flow back into FastIdentity (§5), tested by unsubscribing a test address.
- [ ] Property allow-list in place; no treatment names in subject or preview text (§3.1).
- [ ] ESP journeys: delay 0, marketing message type, subscribed-only audience, ESP frequency cap on.
- [ ] Credentials in environment variables or a secrets manager, with least-privilege scopes, and a rotation owner named.
- [ ] Ran one flow end to end with internal addresses: the FastIdentity message log shows `sent`, the ESP shows the event and the journey entry, and the email arrives.
- [ ] Monitoring: alert on `failed` messages (e.g. a daily count from the `message` table) and on the worker process being down.

## 8. Known gaps in the current adapters

| # | Gap | Impact | Suggested fix |
|---|---|---|---|
| 1 | All trigger properties and `interests` are forwarded | Over-sharing, possibly health data | `config_json.allowed_properties: [...]`; filter in `run_flow` and `public_profile` |
| 2 | No retries. A `failed` send is recorded against `(flow, trigger_event)` and never retried. | Lost messages during ESP outages | Store failures as `pending_retry` with an attempt count and backoff, and let the worker re-send them |
| 3 | No ESP subscription push (§3.4) or inbound opt-out webhooks (§5) | ESP and FastIdentity consent can drift | Implement as described |
| 4 | ESP external id is the profile id, which changes on merge (§3.3) | Duplicate ESP contacts | Key on email, or forward merges |
| 5 | Customer.io event before identify | Event on a person without an email | Identify inside `send_event` |
| 6 | Only the email channel is wired in flows (`channel` is always `email` in the UI) | No SMS flows | Add a channel select; adapters already receive `phone` |
| 7 | Klaviyo `revision` pinned to `2024-10-15` | Breaks when the revision is retired | Review the revision on each upgrade |
| 8 | Sends happen inline in the worker loop | One slow ESP delays the other flows | Fine at SME volume; move to a queue if needed |
