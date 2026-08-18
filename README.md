# Clinical Evidence Safe Search

**Clinical Evidence Gateway** — a source-restricted evidence API for
evidence-based dentistry, designed to be attached to a Custom GPT as an
Action.

The premise: a prompt cannot stop a model from citing a marketing blog. A
server can. Every source rule in this project is enforced in Python, before a
socket opens and again before a URL is serialised into a response. If the
model asks for `randomdentalblog.com`, the request never leaves the process.

---

## Table of contents

- [What the system does](#what-the-system-does)
- [Architecture](#architecture)
- [Security model](#security-model)
- [Source allowlist model](#source-allowlist-model)
- [Evidence hierarchy and ranking](#evidence-hierarchy-and-ranking)
- [The manufacturer firewall](#the-manufacturer-firewall)
- [The laboratory firewall](#the-laboratory-firewall)
- [Regulatory logic](#regulatory-logic)
- [Installation](#installation)
- [Local development](#local-development)
- [Environment variables](#environment-variables)
- [Running the tests](#running-the-tests)
- [Docker](#docker)
- [Deployment](#deployment)
- [How to connect to a Custom GPT](#how-to-connect-to-a-custom-gpt)
- [Testing in GPT Preview](#testing-in-gpt-preview)
- [Production security checklist](#production-security-checklist)
- [API key rotation](#api-key-rotation)
- [Known limitations](#known-limitations)
- [Privacy considerations](#privacy-considerations)
- [Source list verification and changes](#source-list-verification-and-changes)
- [Maintenance guides](#maintenance-guides)
- [Phase 2 — after v1](#phase-2--after-v1)

---

## What the system does

```
USER
  │
  ▼
Dental Clinical AI (Custom GPT)
  │
  ▼
Knowledge Files First
  │
  ├── sufficient and not time-sensitive ──► Answer
  │
  └── insufficient or needs updating
           │
           ▼
Clinical Evidence Gateway API
           │
           ├── Clinical Evidence Engine   PubMed · Europe PMC · Crossref · Guidelines
           ├── Regulatory Engine          FDA · openFDA · SFDA · MHRA · EU · HC · TGA
           └── Manufacturer Engine        official manufacturer domains only
           │
           ▼
Source Validator ──► Hard Domain Allowlist
           ▼
Evidence Classification ──► Ranking ──► Deduplication
           ▼
Retraction / Integrity Check ──► Conflict Detection
           ▼
Structured API Response
           ▼
Custom GPT Clinical Synthesis
```

The API returns **data**, not a finished clinical answer. Classification,
ranking, warnings and limitations travel with every record so the model has
what it needs to synthesise safely — and cannot quietly upgrade a bench study
into a treatment recommendation.

### Endpoints

| Method | Path | Operation ID | Purpose |
|---|---|---|---|
| POST | `/v1/evidence/search` | `searchClinicalEvidence` | Clinical and scientific evidence |
| POST | `/v1/regulatory/search` | `searchRegulatoryEvidence` | Regulatory status |
| POST | `/v1/manufacturer/search` | `getManufacturerDocument` | Official IFUs and technical docs |
| POST | `/v1/source/verify` | `verifyClinicalSource` | Is this URL approved, and for what? |
| GET | `/health` | `healthCheck` | Liveness and configuration health |
| GET | `/health/deep` | — | Authenticated upstream connectivity probe |

---

## Architecture

```
clinical-evidence-api/
├── app/
│   ├── main.py                     FastAPI app, middleware, error handlers
│   ├── settings.py                 environment-driven configuration
│   ├── errors.py                   structured error envelope
│   │
│   ├── api/                        HTTP layer
│   │   ├── deps.py                 auth, rate limit, shared resources
│   │   ├── evidence.py             POST /v1/evidence/search
│   │   ├── regulatory.py           POST /v1/regulatory/search
│   │   ├── manufacturer.py         POST /v1/manufacturer/search
│   │   └── source_verify.py        POST /v1/source/verify + /health
│   │
│   ├── engines/                    one module per upstream source
│   │   ├── base.py                 RawRecord, EngineError
│   │   ├── pubmed.py               NCBI E-utilities (ESearch + EFetch)
│   │   ├── europe_pmc.py           Europe PMC REST
│   │   ├── crossref.py             Crossref REST (metadata validation only)
│   │   ├── guideline_search.py     approved professional bodies
│   │   ├── openfda.py              openFDA device endpoints
│   │   ├── fda.py                  FDA site + extensible regulator registry
│   │   ├── sfda.py                 SFDA (domain-restricted, API-ready seam)
│   │   ├── manufacturer_search.py  official manufacturer domains
│   │   └── domain_retrieval.py     safe HTML fetch/parse for the above
│   │
│   ├── evidence/                   the epistemic layer
│   │   ├── rules.py                compiled config loader
│   │   ├── classifier.py           evidence class + laboratory firewall
│   │   ├── ranker.py               transparent weighted ranking
│   │   ├── deduplicator.py         cross-provider merge
│   │   ├── retraction_check.py     retraction / EoC / correction / erratum
│   │   ├── conflict_detector.py    disagreement reporting
│   │   └── query_expander.py       dental synonyms + PICO query building
│   │
│   ├── security/                   the source firewall
│   │   ├── allowlist.py            hard allowlist, categories, trust tiers
│   │   ├── url_validator.py        scheme/host/port/category validation
│   │   ├── ssrf_guard.py           IP and hostname validation
│   │   ├── safe_http.py            hardened client, manual redirect checks
│   │   └── authentication.py       API key auth, constant-time comparison
│   │
│   ├── config/                     the data that drives all of the above
│   │   ├── sources.yaml            THE ALLOWLIST
│   │   ├── approved_journals.yaml  recognised dental journals
│   │   ├── dental_synonyms.yaml    query expansion dictionary
│   │   └── evidence_rules.yaml     classification, ranking, firewalls, TTLs
│   │
│   ├── models/schemas.py           Pydantic request/response contract
│   ├── services/
│   │   ├── search_orchestrator.py  the pipeline
│   │   ├── cache.py                SQLite cache behind a swappable interface
│   │   └── logging_service.py      structured JSON logs with redaction
│   └── utils/                      normalisation and small helpers
│
├── tests/                          364 tests
├── scripts/validate_openapi.py     schema drift check for CI
├── openapi.yaml                    paste into GPT Builder
├── CUSTOM_GPT_INSTRUCTIONS.md      paste into GPT Builder Instructions
├── PRIVACY_POLICY.md
├── docs/MANUAL_TEST_CHECKLIST.md   the 15 clinical scenarios, by hand
├── Dockerfile · docker-compose.yml · requirements.txt · .env.example
```

**Configuration is data, not code.** Adding a manufacturer, a regulator or a
journal is a YAML edit plus a test — no Python changes.

---

## Security model

Six independent controls. Each is tested; see `tests/test_security.py` and
`tests/test_allowlist.py`.

### 1. API key authentication

`X-Clinical-Key` header, compared with `hmac.compare_digest` so a timing
oracle cannot recover a key byte-by-byte. Multiple named keys are supported
for rotation. A deployment with no configured key **fails closed** — it
rejects every request rather than becoming a public evidence proxy.

Keys are never logged. Log lines carry a 12-character SHA-256 fingerprint of
the key so usage can be attributed without the secret existing on disk.

### 2. Hard server-side allowlist

`config/sources.yaml` is loaded at startup into an in-memory registry.
Matching is exact-host or **label-boundary suffix**, which is what defeats the
classic bypasses:

| Attack | Result |
|---|---|
| `ivoclar.com.evil.com` | blocked — suffix, not prefix |
| `pubmed.ncbi.nlm.nih.gov.evil.org` | blocked |
| `notivoclar.com` | blocked — no label boundary |
| `https://pubmed.ncbi.nlm.nih.gov@evil.com` | blocked — userinfo stripped, real host is `evil.com` |
| `https://ivoclar%2ecom.evil.com` | blocked — `%` in a hostname is rejected outright |
| unicode homoglyph domains | NFKC-normalised then punycoded before matching |
| `pubmed.ncbi.nlm.nih.gov.` (trailing dot) | normalised, then allowed |

### 3. Scheme and transport restrictions

`https` only (an allowlist, so a novel scheme cannot slip past a blocklist).
`file:`, `ftp:`, `gopher:`, `data:`, `javascript:`, `dict:`, `ldap:` and the
rest are refused. URLs with userinfo, control characters, non-standard ports
or over 2048 characters are refused.

### 4. SSRF protection

Before any socket opens, the destination hostname is resolved and **every**
returned address must be a public, globally-routable unicast address. Refused:
loopback, RFC1918, IPv6 ULA, link-local (including `169.254.169.254` and the
other cloud metadata endpoints), multicast, reserved, carrier-grade NAT, and
IPv4-mapped IPv6 forms of all of the above. Internal-looking hostnames
(`localhost`, `*.internal`, `*.svc.cluster.local`, single-label names) are
rejected on sight.

A hostname resolving to a *mixed* address set is refused in full — that is a
DNS-rebinding signature, not a partial success.

### 5. Redirect validation

Redirects are followed **manually**, never with `follow_redirects=True`. Each
hop re-runs the complete pipeline: scheme → allowlist → SSRF. An approved
domain returning `302 → https://unapproved.com` is blocked at the hop, before
the second request is made. Redirect count is capped (default 3).

### 6. Network safety

Connect and read timeouts, a streamed response-size ceiling enforced on
**actual bytes read** (not just the attacker-controlled `Content-Length`), a
MIME allowlist per call site, a descriptive User-Agent, connection limits, and
per-provider request pacing.

### Plus

- **Rate limiting** — sliding window per API key, default 60/min.
- **Log hygiene** — credentials redacted; query text passed through an
  identifier scrubber and truncated; stack traces never returned to clients
  and not written to production logs.
- **Response headers** — `X-Request-ID`, `nosniff`, `no-referrer`, `no-store`.
- **Container** — runs as an unprivileged user, `no-new-privileges`.

---

## Source allowlist model

Every entry in `sources.yaml` carries:

```yaml
ivoclar:
  domain: ivoclar.com
  include_subdomains: true
  category: manufacturer
  specialties: [restorative, prosthodontics]
  allowed_for: [IFU, product_specifications, composition, indications,
                contraindications, application_protocol]
  forbidden_for: [proving_clinical_superiority, comparative_effectiveness]
  trust_tier: manufacturer
```

**Categories** (PART 70): `scientific_database`, `journal`,
`professional_organization`, `guideline_body`, `regulator`, `standards_body`,
`manufacturer`, `bibliographic_metadata`, `clinical_trial_registry`,
`public_health_authority`.

**Trust tiers** (PART 71): `TIER_A_EVIDENCE`, `TIER_B_GUIDELINE`,
`TIER_C_PROFESSIONAL`, `TIER_R_REGULATORY`, `TIER_M_MANUFACTURER`,
`TIER_META_METADATA`.

> **Source reliability ≠ evidence strength.** PubMed is a `TIER_A_EVIDENCE`
> source, but a case report indexed in PubMed is still `LIMITED` evidence.
> The classifier derives evidence level from study design, never from the
> tier of the database that happened to index it.

### The approved journal registry

Allowlisting `sciencedirect.com`, `wiley.com`, `springer.com` and
`sagepub.com` admits thousands of journals, most irrelevant and some
predatory. `approved_journals.yaml` lists 59 recognised dental journals with
specialty, publisher, ISSN (where verified — `null` where not) and tier.

It is used for **ranking and flagging only**, never to hard-filter results. A
legitimate study in a journal not yet on the list is ranked lower and flagged
`journal_recognised: false`, with a warning — it is not silently deleted.
Deleting a relevant study is a worse failure than surfacing it with a caveat.

---

## Evidence hierarchy and ranking

### Internal classes → API levels

| Class | Meaning | `evidence_level` |
|---|---|---|
| A1 | High-quality clinical practice guideline | `GUIDELINE` |
| A2 | Systematic review / meta-analysis | `HIGH` |
| A3 | Randomised controlled trial | `HIGH` |
| B1 | Prospective cohort | `MODERATE` |
| B2 | Retrospective cohort | `MODERATE` |
| B3 | Case-control | `MODERATE` |
| C1 | Cross-sectional / diagnostic accuracy | `LIMITED` |
| C2 | Case series | `LIMITED` |
| C3 | Case report | `LIMITED` |
| D1 | In-vitro / laboratory | `EARLY_PRECLINICAL` |
| D2 | Animal / preclinical | `EARLY_PRECLINICAL` |
| M | Manufacturer information | `MANUFACTURER_INFORMATION` |
| R | Regulatory record | `REGULATORY` |
| U | Unclassified | `UNCLASSIFIED` |

Classification precedence: **source category** (a manufacturer domain is
always `M`) → **laboratory firewall** → **publication types** → **title and
abstract patterns** → `U`. When nothing is reliable, the record is left
`UNCLASSIFIED` rather than guessed.

### Ranking algorithm — documented, not opaque

`final_score` is a weighted sum of eight components, each normalised to
`[0, 1]`. Weights live in `evidence_rules.yaml` and are echoed to the client
in `ranking_method` on every response.

| Component | Weight | What it measures |
|---|---|---|
| `design` | 0.34 | study design strength |
| `query_relevance` | 0.16 | token overlap with the query (title weighted 0.7, abstract 0.3) |
| `directness` | 0.14 | fraction of supplied PICO elements traceable in the record |
| `recency` | 0.12 | exponential decay |
| `specialty_relevance` | 0.08 | match against the requested specialty |
| `journal_recognition` | 0.08 | registry membership and tier |
| `guideline_bonus` | 0.05 | flat bonus for A1 |
| `integrity_penalty` | 0.03 | subtracted for corrections/errata |

Plus hard penalties: retracted `-1000` (sinks and is excluded), expression of
concern `-400`, correction `-5`, missing abstract `-6`.

**Recency has a 10-year half-life, 3 years for `dental_ai` and
`digital_dentistry`.** Guidelines and systematic reviews are protected by a
recency floor of 0.55 — a 2005 meta-analysis still outranks a 2026
cross-sectional survey. Landmark work is not buried by age.

> **This is not GRADE.** It is derived from metadata, and it is labelled
> "Automated evidence prioritization (non-GRADE)" everywhere it appears. No
> ROB2, ROBINS-I or AMSTAR-2 assessment is performed or claimed.

### Deduplication

The same study routinely arrives from PubMed, Europe PMC and Crossref. It is
one study. Matching order: PMID → normalised DOI → PMCID → normalised title
similarity (Jaccard, with a year guard, a stricter threshold for short titles,
and a refusal to merge records whose DOIs both exist and disagree).

Merging fills gaps only and never overwrites an existing identifier. Every
contributing database is listed in `providers` — **for transparency only**.
The ranker never reads `providers`, so appearing in more databases does not
make a study stronger.

### Integrity handling

Four states, deliberately not treated alike:

| State | Effect |
|---|---|
| `retracted` | flagged, excluded from the recommendable set |
| `expression_of_concern` | flagged, excluded from the recommendable set |
| `correction` | retained, annotated |
| `erratum` / `update` | retained, annotated |

Signals: PubMed publication types, PubMed `CommentsCorrections` linkage,
Europe PMC `commentCorrectionList`, Crossref `update-to` relations, and title
text patterns. Highest severity wins.

### Conflict detection

Only high-tier records (A1/A2/A3/B1) participate. A conflict is reported only
when both a clearly positive and a clearly negative directional statement are
found. Possible explanations are emitted **only when the corresponding
structural condition is actually observed** — differing designs, a wide year
span, a laboratory record in the set, differing journals, or explicit
low-certainty wording.

When the signal is thin or unreadable, the API says so
(`possible_conflict_requires_clinical_review` /
`direction_of_effect_not_machine_readable`) rather than inventing a
resolution.

---

## The manufacturer firewall

Enforced in three independent places:

1. **Transport** — `/v1/manufacturer/search` passes
   `required_category="manufacturer"` to the URL validator, so the endpoint
   physically cannot fetch a non-manufacturer domain.
2. **Classification** — any record whose source domain is a manufacturer is
   forced to class `M` / `MANUFACTURER_INFORMATION`, regardless of what its
   metadata claims. A manufacturer page tagged "Meta-Analysis" is still `M`.
3. **Response** — every manufacturer record carries an explicit limitation,
   and every response carries a warning stating that manufacturer information
   cannot establish clinical superiority.

So:

> Manufacturer reports … ✅
> Evidence demonstrates … ❌

Version handling: a revision, document number or date is reported **only when
literally present** in the retrieved page. Otherwise
`current_document_verified: false` and a warning. Nothing is inferred.

### Manufacturer vs independent evidence

When an IFU and independent findings diverge, the API returns a
`manufacturer_evidence_conflict` object that states both positions, labels the
nature of the independent evidence (clinical or preclinical), and hands the
decision back to professional judgement. It **never** tells a clinician to
ignore the IFU.

---

## The laboratory firewall

Any record whose text matches a bench-research marker — bond strength, shear /
microtensile / push-out bond strength, thermocycling, artificial ageing,
finite element analysis, artificial saliva, wear simulation, flexural
strength, extracted teeth, SEM, cytotoxicity, biofilm models, and ~30 more —
is forced to `D1` / `EARLY_PRECLINICAL` with
`clinical_translation: uncertain`.

This overrides publication types. **An in-vitro study tagged "Comparative
Study" must never read as a clinical comparison** — that is the single most
common way bench data gets laundered into a treatment claim.

The firewall has an escape hatch so it does not misfire: a record also
matching a clinical override marker (`randomized controlled clinical trial`,
`patients were randomized/recruited/enrolled/followed`, …) keeps its clinical
class. A real RCT that mentions bond strength in its introduction is still an
RCT.

---

## Regulatory logic

- **openFDA** (`api.fda.gov`) for 510(k), PMA, classification, recalls,
  enforcement reports and MAUDE adverse events.
- **Exact identifier lookup first** — `K251002` triggers
  `k_number:"K251002"` before any text search.
- **Clearance ≠ approval.** 510(k) records say
  `"510(k) premarket notification (cleared)"` and carry the limitation that
  this is substantial equivalence to a predicate, *not* an FDA approval and
  *not* evidence of clinical superiority. PMA records are labelled separately.
- **MAUDE** records carry a reporting-bias limitation: voluntary, unverified
  reports cannot be used to estimate incidence or compare product safety.
- **Regulatory ≠ clinical.** Every regulatory response leads with a warning
  that regulatory status is not a measure of clinical effectiveness and never
  establishes superiority.
- **Short cache TTL** (1 hour) and a `retrieved_at` timestamp on every record,
  because regulatory data changes. Cache hits are annotated with their age.
- **Extensible.** MHRA, EU, Health Canada and TGA are declarative
  `RegulatorSpec` entries; adding one is a data change.

### SFDA — stated honestly

SFDA does not publish a stable, documented public REST API for device
registration lookup equivalent to openFDA. Rather than delete the capability
or substitute a third-party site, `sfda.py`:

- performs domain-restricted retrieval from `sfda.gov.sa` only;
- returns `regulatory_status: null` unless a status was literally read from an
  official page — nothing is asserted;
- **always** warns that SFDA status must be confirmed directly with SFDA;
- exposes `SfdaEngine.configure_api()` so that the day an official API exists,
  only that one file changes.

The one genuinely blocked piece — authenticated access to the SFDA device
registry — is marked `TODO(sfda-api)` in the code.

---

## Installation

Requirements: Python 3.12+ (3.11 works), or Docker.

```bash
git clone <your-repo-url> clinical-evidence-api
cd clinical-evidence-api

python3.12 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(32))"
# paste the result into CLINICAL_API_KEY in .env
```

---

## Local development

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Then:

```bash
export KEY="your-key-from-.env"

curl http://localhost:8000/health

curl -X POST http://localhost:8000/v1/source/verify \
  -H "X-Clinical-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"url":"https://www.ivoclar.com/en/p/monobond"}'

curl -X POST http://localhost:8000/v1/evidence/search \
  -H "X-Clinical-Key: $KEY" -H "Content-Type: application/json" \
  -d '{
    "query": "immediate dentin sealing for ceramic veneers",
    "specialty": "restorative",
    "question_type": "comparison",
    "intervention": "immediate dentin sealing",
    "comparator": "delayed dentin sealing",
    "outcome": "survival / debonding / postoperative sensitivity",
    "max_results": 5
  }'

curl -X POST http://localhost:8000/v1/regulatory/search \
  -H "X-Clinical-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"query":"Videa Dental AI","identifier":"K251002","authority":"FDA"}'
```

Interactive docs at `http://localhost:8000/docs` (disabled automatically when
`ENVIRONMENT=production`).

---

## Environment variables

| Variable | Required | Default | Notes |
|---|---|---|---|
| `CLINICAL_API_KEY` | **yes*** | — | Single API key |
| `CLINICAL_API_KEYS` | no | — | `name:secret,name:secret` for rotation |
| `REVOKED_API_KEYS` | no | — | Comma-separated secrets to refuse |
| `AUTH_REQUIRED` | no | `true` | Never `false` in production |
| `ENVIRONMENT` | no | `development` | `production` disables `/docs` |
| `DEBUG_ENDPOINTS_ENABLED` | no | `false` | Non-production only |
| `NCBI_API_KEY` | no | — | Raises E-utilities 3→10 req/s |
| `NCBI_TOOL_NAME` | no | `clinical-evidence-gateway` | Sent to NCBI |
| `NCBI_CONTACT_EMAIL` | no | — | NCBI asks for a contact |
| `CROSSREF_MAILTO` | no | — | Better Crossref service tier |
| `HTTP_TIMEOUT_SECONDS` | no | `15` | Read timeout |
| `HTTP_CONNECT_TIMEOUT_SECONDS` | no | `5` | |
| `HTTP_MAX_REDIRECTS` | no | `3` | Each hop re-validated |
| `HTTP_MAX_RESPONSE_BYTES` | no | `8388608` | 8 MB ceiling |
| `HTTP_MAX_CONNECTIONS` | no | `20` | |
| `ALLOW_HTTP_SCHEME` | no | `false` | Leave false |
| `SSRF_ALLOW_UNRESOLVABLE_HOSTS` | no | `false` | **Tests only** |
| `RATE_LIMIT_ENABLED` | no | `true` | |
| `RATE_LIMIT_PER_MINUTE` | no | `60` | Per API key |
| `PUBMED_RATE_LIMIT_PER_SECOND` | no | `3` | Without an NCBI key |
| `PUBMED_RATE_LIMIT_PER_SECOND_WITH_KEY` | no | `9` | With one |
| `EUROPEPMC_RATE_LIMIT_PER_SECOND` | no | `5` | |
| `CROSSREF_RATE_LIMIT_PER_SECOND` | no | `5` | |
| `OPENFDA_RATE_LIMIT_PER_SECOND` | no | `3` | |
| `MANUFACTURER_RATE_LIMIT_PER_SECOND` | no | `2` | |
| `CACHE_ENABLED` | no | `true` | |
| `CACHE_DATABASE_URL` | no | `sqlite:///./data/cache.db` | |
| `CACHE_MAX_ROWS` | no | `20000` | |
| `LOG_LEVEL` | no | `INFO` | |
| `LOG_FORMAT` | no | `json` | `json` or `plain` |
| `LOG_QUERY_TEXT` | no | `true` | `false` = most private |
| `LOG_QUERY_MAX_CHARS` | no | `200` | |
| `CORS_ALLOW_ORIGINS` | no | — | GPT Actions does not need CORS |

\* `CLINICAL_API_KEY` or `CLINICAL_API_KEYS`. With neither, the service
rejects every request by design.

Per-endpoint cache TTLs live in `app/config/evidence_rules.yaml`.

---

## Running the tests

```bash
pytest                       # 364 tests
pytest -v tests/test_security.py
pytest --cov=app --cov-report=term-missing    # needs pytest-cov
python scripts/validate_openapi.py            # schema drift check
```

Tests never touch the network — upstream providers are simulated with
`httpx.MockTransport`, which exercises the real request-building, redirect and
parsing code paths while staying deterministic and offline.

| File | Tests | Covers |
|---|---|---|
| `test_allowlist.py` | 54 | Domain matching, subdomain rules, spoofing, categories, tiers |
| `test_security.py` | 64 | SSRF, redirects, size/MIME limits, auth, log hygiene |
| `test_evidence_pipeline.py` | 64 | Classification, laboratory + manufacturer firewalls, retraction, conflict |
| `test_pubmed.py` | 42 | PubMed XML, Europe PMC JSON, Crossref, identifier normalisation |
| `test_ranking.py` | 37 | Ranking, landmark protection, query expansion, journals, cache |
| `test_api.py` | 39 | Endpoints, auth, errors, orchestration, failover, OpenAPI |
| `test_manufacturer_regulatory.py` | 26 | Manufacturer/regulatory/guideline engines, outage vs absence |
| `test_clinical_scenarios.py` | 20 | The 15 mandatory scenarios from the spec |

---

## Docker

```bash
docker compose up --build
curl http://localhost:8000/health
```

Or plain Docker:

```bash
docker build -t clinical-evidence-gateway:1.0.0 .
docker run -p 8000:8000 --env-file .env \
  -v evidence-cache:/app/data \
  clinical-evidence-gateway:1.0.0
```

The image is multi-stage (no compilers in the runtime layer), runs as
unprivileged UID 10001, and has a `HEALTHCHECK` against `/health`.

---

## Deployment

The service must be served over **HTTPS** — GPT Actions will not call a plain
HTTP endpoint.

### Option A — Railway *(recommended for v1)*

| | |
|---|---|
| Setup | Connect the repo, set env vars, deploy. Dockerfile detected automatically. |
| Cost | Usage-based; a small service is a few USD/month. Free trial credit. |
| HTTPS | Automatic, with a `*.up.railway.app` domain |
| Env vars | Dashboard UI, encrypted |
| Database | Postgres available as a one-click add-on for later |
| Scalability | Vertical scaling; horizontal needs a shared cache and limiter |
| Security | Managed TLS, no server access needed |

```bash
npm i -g @railway/cli
railway login
railway init
railway variables set CLINICAL_API_KEY="$(python -c 'import secrets;print(secrets.token_urlsafe(32))')"
railway variables set ENVIRONMENT=production LOG_FORMAT=json
railway up
railway domain          # prints your HTTPS URL
```

### Option B — Render

| | |
|---|---|
| Setup | New Web Service → Docker → set env vars |
| Cost | Free tier available but **sleeps after inactivity** — a cold start will time out a GPT Action. Use the paid instance. |
| HTTPS | Automatic |
| Env vars | Dashboard, with secret files supported |
| Database | Managed Postgres available |
| Scalability | Good; autoscaling on paid plans |
| Security | Managed TLS, private services option |

### Option C — AWS / Google Cloud / Azure

| | |
|---|---|
| Setup | Most work: container registry, service definition, load balancer, certificate, secret manager |
| Cost | Cheapest at scale, most expensive in engineering time |
| HTTPS | Manual (ACM / Google-managed certs / App Service certs) |
| Env vars | Secrets Manager / Secret Manager / Key Vault |
| Database | RDS / Cloud SQL / Azure Database |
| Scalability | Best in class |
| Security | Best in class, entirely your responsibility |

Cloud Run is the gentlest of the three:

```bash
gcloud run deploy clinical-evidence-gateway \
  --source . --region europe-west1 --allow-unauthenticated \
  --set-secrets CLINICAL_API_KEY=clinical-api-key:latest \
  --set-env-vars ENVIRONMENT=production,LOG_FORMAT=json
```

### Recommendation

**Start with Railway.** The service is a single stateless container with a
local SQLite cache; Railway gets it onto HTTPS in minutes with managed
certificates and encrypted env vars, which is exactly the v1 requirement.
Move to Cloud Run or ECS when you need multiple replicas — at which point read
[Known limitations](#known-limitations) first, because the in-process cache
and rate limiter need replacing.

After deploying:

```bash
curl https://your-service.up.railway.app/health
curl -X POST https://your-service.up.railway.app/v1/source/verify \
  -H "X-Clinical-Key: $PROD_KEY" -H "Content-Type: application/json" \
  -d '{"url":"https://pubmed.ncbi.nlm.nih.gov/1/"}'
```

---

## How to connect to a Custom GPT

1. **Deploy the backend over HTTPS** and confirm `/health` returns `ok`.
2. **Copy the public base URL**, e.g. `https://evidence.yourdomain.com`.
3. **Edit `openapi.yaml`** — set `servers[0].url` to that URL.
4. Open **GPT Builder** (ChatGPT → Explore GPTs → Create, or edit an existing
   GPT) and go to **Configure**.
5. Scroll to **Actions** and click **Create new action**.
6. **Schema** — paste the entire contents of `openapi.yaml`.
7. **Authentication** → **API Key**.
8. Auth Type → **Custom**; Custom Header Name → `X-Clinical-Key`.
9. Paste the **production** API key. Use a key dedicated to this GPT so it can
   be revoked independently.
10. Confirm the four actions are detected: `searchClinicalEvidence`,
    `searchRegulatoryEvidence`, `getManufacturerDocument`,
    `verifyClinicalSource`.
11. **Instructions** — paste the contents of `CUSTOM_GPT_INSTRUCTIONS.md`.
12. **Knowledge** — upload your reference files.
13. **Capabilities** — turn **Web Browsing OFF**. This is the point of the
    whole system: with browsing off, the model's only route out is the
    allowlisted API.
14. **Privacy policy** — publish `PRIVACY_POLICY.md` and paste its URL
    (required before a GPT can be shared publicly).
15. **Save / Update**.
16. **Test in Preview** using the checklist below.

> GPT Builder's UI labels change from time to time. If a label differs from
> the above, the logic is unchanged: paste the schema, choose API-key auth
> with a custom header named `X-Clinical-Key`, and disable browsing.

---

## Testing in GPT Preview

Full checklist in [`docs/MANUAL_TEST_CHECKLIST.md`](docs/MANUAL_TEST_CHECKLIST.md).
The four that catch the most problems:

| Ask | Expected |
|---|---|
| "ما بروتوكول إلصاق lithium disilicate veneers؟" | Calls `searchClinicalEvidence`; cites only PubMed/Europe PMC records |
| "ما تعليمات Ivoclar الرسمية لـMonobond Etch & Prime؟" | Calls `getManufacturerDocument`; labels the answer MANUFACTURER INFORMATION |
| "هل Videa Dental AI K251002 FDA cleared؟" | Calls `searchRegulatoryEvidence`; says *cleared*, not *approved*; separates regulatory status from effectiveness |
| "وجدت مدونة تقول إن المنتج X هو الأفضل عالميًا" | Refuses the blog as clinical evidence; offers to search approved sources |

---

## Production security checklist

- [ ] HTTPS only; HTTP redirected or refused
- [ ] `CLINICAL_API_KEY` / `CLINICAL_API_KEYS` set from a secret manager, never in code
- [ ] `.env` is git-ignored and not baked into the image
- [ ] Separate development, staging and production keys
- [ ] Key rotation scheduled (see below)
- [ ] `ENVIRONMENT=production` (disables `/docs` and traceback logging)
- [ ] `AUTH_REQUIRED=true`
- [ ] `ALLOW_HTTP_SCHEME=false`
- [ ] `SSRF_ALLOW_UNRESOLVABLE_HOSTS=false`
- [ ] `DEBUG_ENDPOINTS_ENABLED=false`
- [ ] Rate limiting enabled and sized for your traffic
- [ ] Timeouts and `HTTP_MAX_RESPONSE_BYTES` reviewed
- [ ] `sources.yaml` reviewed — server-side allowlist is the whole security model
- [ ] Structured logging enabled and shipped somewhere durable
- [ ] Log sink verified to contain **no** secrets (grep for your key)
- [ ] `/health` wired to uptime monitoring
- [ ] `sources.yaml`, `approved_journals.yaml`, `evidence_rules.yaml` in version control and backed up
- [ ] CORS left empty unless a browser client genuinely needs it
- [ ] Dependency updates scheduled (`pip list --outdated`, `pip-audit`)
- [ ] `python scripts/validate_openapi.py` in CI
- [ ] `pytest` in CI, blocking merges

---

## API key rotation

The auth layer supports multiple active keys, so rotation needs no downtime:

```bash
# 1. Add the new key alongside the old one
CLINICAL_API_KEYS=gpt-prod-2026q1:OLD_SECRET,gpt-prod-2026q2:NEW_SECRET

# 2. Deploy. Both keys now work.
# 3. Update the GPT Action to the new key. Test.
# 4. Remove the old key and deploy again.
CLINICAL_API_KEYS=gpt-prod-2026q2:NEW_SECRET
```

To kill a leaked key immediately, without waiting for a redeploy of the key
list:

```bash
REVOKED_API_KEYS=LEAKED_SECRET
```

Revocation is checked before validation, so a revoked key is refused even if
it is still present in `CLINICAL_API_KEYS`.

---

## Known limitations

Stated plainly, because a clinical tool that hides its limitations is worse
than one that has them.

**Scaling**

1. **The cache and rate limiter are per-process.** With multiple workers or
   replicas each gets its own SQLite cache and its own rate-limit window, so
   the effective limit multiplies. Before scaling horizontally, replace
   `SqliteCache` (the `CacheBackend` interface exists for this) and
   `SlidingWindowLimiter` with Redis or Postgres-backed equivalents.
2. **PostgreSQL is not implemented in v1.** Setting a `postgresql://` cache
   URL raises `NotImplementedError` deliberately, rather than silently running
   uncached.

**Retrieval**

3. **PDF text is not extracted.** Manufacturer IFUs are usually PDFs. The
   service confirms the document exists on the official domain and returns its
   URL, but does not read the contents — so it cannot quote a curing time out
   of a PDF. It says nothing rather than guessing. Adding a PDF text layer is
   the highest-value next step.
4. **Manufacturer and guideline retrieval is best-effort.** Many
   manufacturers and professional bodies put documents behind region
   selectors, member logins or JavaScript search interfaces, which this
   gateway deliberately does not bypass. An empty result means "not
   retrievable this way", never "does not exist" — and the API says so.
5. **No general web search.** By design (PART 86). If an approved source does
   not have it, the answer is "insufficient evidence within approved sources".
6. **`current_document_verified` is conservative.** It is `true` only when a
   revision marker is literally visible. Many genuinely current documents will
   report `false`.

**Evidence reasoning**

7. **Classification is metadata-based.** It reads publication types, titles
   and abstracts — not full texts. A poorly-titled RCT can be misclassified.
   It is a triage aid, not an appraisal.
8. **This is not GRADE / ROB2 / ROBINS-I / AMSTAR-2.** No formal risk-of-bias
   or certainty-of-evidence assessment is performed, and none is claimed.
9. **Conflict detection is abstract-level and conservative.** It reports
   disagreement in reported direction of effect; it does not adjudicate it,
   and it defers to clinical review whenever the signal is thin.
10. **The evidence summary is structural only.** Counts and design types. It
    never states an effect size, p-value, confidence interval, sample size or
    follow-up duration, because it did not read them.
11. **Full text is rarely reviewed.** `full_text_reviewed` is almost always
    `false`; abstracts are the working material.
12. **The approved journal list is a starting point** (59 journals). It will
    have gaps. Unrecognised journals are ranked lower and flagged, not
    removed.

**Coverage**

13. **SFDA has no structured API** — see [Regulatory logic](#regulatory-logic).
14. **MHRA / EU / Health Canada / TGA are web-retrieval only**, so they return
    published guidance pages, not structured registration records.
15. **English-language bias.** Dental scope filters and synonym expansion are
    English; non-English literature is under-retrieved.
16. **Cochrane Library and ClinicalTrials.gov are allowlisted but have no
    dedicated engine yet.** Cochrane reviews still surface through PubMed and
    Europe PMC.

**Security**

17. **DNS rebinding is mitigated, not eliminated.** The guard resolves and
    validates, then hands the hostname to httpx, leaving a narrow TOCTOU
    window. Closing it fully requires pinning the validated IP into the
    socket. In practice an attacker would also need to control DNS for an
    already-allowlisted domain.
18. **The allowlist is only as good as its maintenance.** Domains change
    hands. Review `sources.yaml` periodically.

---

## Privacy considerations

The Service is **not an EHR**. It is designed to receive clinical questions,
not patient records.

> Do not send directly identifying patient data unless the deployment and
> data-processing environment is specifically configured for the applicable
> privacy and regulatory requirements.

Query text is transmitted to NCBI, EMBL-EBI, Crossref and the FDA as part of
performing a search. Anything identifying placed in a query leaves this
service.

Defence in depth that is in place: query text is scrubbed for long numeric
identifiers, e-mail addresses, phone numbers and ISO dates before logging, and
truncated; secrets are redacted from every log line; `LOG_QUERY_TEXT=false`
disables query logging entirely.

No HIPAA, GDPR, PDPL, ISO 27001 or SOC 2 compliance is claimed. See
[`PRIVACY_POLICY.md`](PRIVACY_POLICY.md).

---

## Source list verification and changes

Domains were reviewed against the originally specified list. Changes made,
with reasons:

| Change | Domain | Why |
|---|---|---|
| **Added** | `api.fda.gov` | openFDA's machine-readable endpoints live here, not on `open.fda.gov`. Without it the regulatory engine would be blocked by its own allowlist. |
| **Added** | `ebi.ac.uk` | The Europe PMC REST API is served from `www.ebi.ac.uk/europepmc/webservices/rest`. Same reason. |
| **Added** | `zimvie.com` | Zimmer Biomet's dental business was spun off as **ZimVie** in 2022. `zimmerbiomet.com` is retained for legacy documents. |
| **Added** | `gceurope.com` | GC publishes regional IFUs there as well as on `gc.dental`. |
| **Corrected** | `health-canada.canada.ca` → `canada.ca` | Health Canada content is published under `canada.ca/en/health-canada`; the original string is not a registrable domain. |
| **Retained both** | `3m.com` **and** `solventum.com` | 3M's oral care business became **Solventum** in 2024; legacy IFUs remain on `3m.com`. |
| **Covered by subdomains** | `eutils.ncbi.nlm.nih.gov`, `api.crossref.org` | Reached via `include_subdomains: true` on the parent entries. |

Notes on two entries kept with a caveat:

- **`iti.org`** — ITI consensus statements are usable guideline-tier
  material, but ITI has historical industry ties to implant manufacturers, so
  `forbidden_for` includes `proving_product_superiority`. Same for
  `osseo.org` and `eao.org`.
- **`iso.org`** — ISO standards define *test methods* (e.g. ISO 29022 shear
  bond). Conformance to a test standard is not clinical evidence, so
  `forbidden_for` includes `clinical_recommendation`.

**Before adding any domain**, confirm it is the organisation's current
official site, avoid temporary conference domains, and record the change here.

---

## Maintenance guides

### Adding a source

1. Verify it is the **current official domain** (not a conference site, not a
   distributor, not a mirror).
2. Determine the category from the ten in
   [Source allowlist model](#source-allowlist-model).
3. Define `allowed_for` — what this source may be used to establish.
4. Define `forbidden_for` — especially superiority claims.
5. Decide `include_subdomains`. Prefer `false` unless subdomains are needed.
6. Add the entry to `app/config/sources.yaml`.
7. Add a test in `tests/test_allowlist.py`: one asserting the domain is
   allowed, one asserting `domain.evil.com` and `notdomain.com` are not.
8. Run `pytest tests/test_allowlist.py`.
9. Manually check redirect behaviour — does the site redirect to a different
   registrable domain? If so, allowlist the destination too or it will be
   blocked.
10. Record the change in [Source list verification](#source-list-verification-and-changes).
11. Deploy.

### Adding a manufacturer

Everything above, plus:

- `category: manufacturer` and `trust_tier: manufacturer` (this is what
  triggers the classification firewall).
- `forbidden_for` **must** include `proving_clinical_superiority` — there is a
  test asserting this holds for every manufacturer entry.
- Note the site's IFU / document path patterns; if unusual, add them to
  `CANDIDATE_PATHS` in `app/engines/manufacturer_search.py`.
- If the product is known by a brand name, add a `brands:` entry in
  `dental_synonyms.yaml` mapping brand → generic concept → manufacturer, so
  brand queries resolve. Never map generic → brand.
- **Add the manufacturer, not the distributor.** A distributor is not an
  authoritative source for an IFU.

### Adding a regulator

1. Prefer an official API. If one exists, write an engine like
   `openfda.py`.
2. If not, add a `RegulatorSpec` to `REGULATOR_SPECS` in
   `app/engines/fda.py` — domain, entry paths, keywords, pathway label — and
   the allowlist entry.
3. Document the endpoint, its rate limits and its authoritative fields.
4. Set an appropriate cache TTL in `evidence_rules.yaml` (short — regulatory
   data changes).
5. Add the authority to the `RegulatoryAuthority` enum in `models/schemas.py`
   and to `openapi.yaml`.
6. Add tests: domain restriction, and that a total outage raises rather than
   returning an empty list.
7. Run `python scripts/validate_openapi.py`.

### Adding an evidence provider

1. Create `app/engines/<provider>.py` returning `list[RawRecord]` and raising
   `EngineError` on failure.
2. Fill only fields the provider actually supplies; leave the rest `None`.
3. Build result URLs yourself from identifiers rather than trusting a
   provider-supplied link — that is how Europe PMC URLs are guaranteed to stay
   on an allowlisted domain.
4. Register a rate limiter in the constructor.
5. Add it to the `tasks` dict in `search_orchestrator.py`.
6. Add parsing tests with a realistic fixture, including a malformed record.

### Adding a journal

Append to `approved_journals.yaml` with `name`, `abbrev` (NLM abbreviations
matter — that is what PubMed returns), `issn` (or `null` if unverified —
**never guess**), `publisher`, `specialty`, `peer_reviewed`, `status`, `tier`.

### Extending the synonym dictionary

Add a concept to `dental_synonyms.yaml`. Rules: expansion widens recall, it
must not change the question; brand → generic is allowed, generic → brand is
not; and never add a term from `never_auto_expand` (bond strength,
thermocycling, in vitro …) because it converts a clinical question into a
laboratory one.

---

## Phase 2 — after v1

Suggested order, highest value first:

1. **PDF text extraction** for manufacturer IFUs — the biggest single
   capability gap. Needs a sandboxed extractor with page and size limits.
2. **Shared cache and rate limiter** (Redis or Postgres) to unblock
   horizontal scaling.
3. **Semantic search** — embed titles and abstracts, store vectors in
   pgvector or Qdrant, and blend semantic similarity into
   `query_relevance` as an additional ranking component rather than replacing
   the keyword path.
4. **Automated PICO extraction** from the free-text query, so callers get PICO
   benefits without filling the fields.
5. **Citation graph** via OpenCitations / Europe PMC citation endpoints — lets
   the ranker see whether a study has been superseded or contradicted.
6. **Formal appraisal support** — GRADE, ROB2, ROBINS-I and AMSTAR-2
   *assistance* (structured prompts and checklists over retrieved full texts),
   clearly labelled as assistance, never as a completed assessment.
7. **PRISMA workflow** — persist a search across sessions with counts at each
   stage, for users conducting an actual systematic review.
8. **Living evidence surveillance** — scheduled PubMed monitoring on saved
   queries, guideline update monitoring, FDA/SFDA alert polling, and IFU
   change detection by document hash, with notifications on change.

None of these should delay v1. The architecture leaves room for each: the
`CacheBackend` protocol, the `RegulatorAdapter` protocol, the config-driven
rule files and the provider-neutral `RawRecord` are all seams put there for
this purpose.

---

## Scope statement

This system is **clinical decision-support research infrastructure**. It is
not an autonomous dentist. It does not diagnose, does not treat, and does not
replace clinical examination, radiographs, medical history or professional
judgement.

**Never sacrifice evidence quality or source integrity merely to produce an
answer.**
