# Privacy Policy — Clinical Evidence Safe Search

**Effective date:** [Effective Date]
**Service operator:** [Company Name]
**Contact:** [Contact Email]

> Replace every `[bracketed]` placeholder before publishing. This is a
> starting draft for a simple API service, not legal advice. Have it
> reviewed by a qualified adviser for your jurisdiction before relying on it.

---

## 1. What this service is

Clinical Evidence Safe Search ("the Service") is a research API that searches
approved scientific, regulatory and manufacturer sources and returns
structured bibliographic and regulatory records.

The Service is **not** an electronic health record, not a medical device, not
a diagnostic tool, and not a substitute for professional clinical judgement.
It is designed to receive *clinical questions*, not patient records.

## 2. What data the Service receives

When a client calls the Service, it receives:

- **The search request** — the query text and any structured fields
  (specialty, question type, PICO elements, date range, product or
  manufacturer names, regulatory identifiers, result limits).
- **An API key** in the `X-Clinical-Key` header, used to authenticate the
  calling application.
- **Standard network metadata** created by any HTTP request — source IP
  address, timestamp, user agent, request path and response status.

The Service does **not** request, require or knowingly collect names, dates of
birth, national identity numbers, medical record numbers, contact details,
images, or any other directly identifying patient information.

## 3. How search queries are processed

1. The query is normalised and expanded with dental terminology.
2. It is sent to the upstream providers listed in section 6.
3. Results are validated against a server-side domain allowlist, classified,
   deduplicated, ranked and returned.

Query text is transmitted to those upstream providers as part of performing
the search. **Do not place identifying patient information in a query**: it
would leave this Service and reach third parties.

## 4. Logging and retention

The Service writes structured operational logs containing: a random request
identifier, timestamp, endpoint, HTTP status, duration, which providers were
searched, result counts, cache hit/miss, error types, and a non-reversible
12-character fingerprint of the API key used.

Before anything is written to a log:

- API keys, bearer tokens, authorization headers and credential-shaped
  query-string values are replaced with `***`;
- query text is passed through an identifier scrubber that redacts long
  numeric identifiers, e-mail addresses, telephone numbers and ISO-format
  dates, and is truncated (default 200 characters).

Query logging can be disabled entirely by setting `LOG_QUERY_TEXT=false`.

**API keys, upstream credentials and request bodies are never logged.**

**Retention.** Operational logs are retained for **[retention period, e.g. 30
days]** and then deleted. Cached upstream responses are retained only for the
configured time-to-live (12 hours for literature searches, 1 hour for
regulatory searches, 24 hours for manufacturer documents, 7 days for Crossref
metadata) and contain no credentials.

## 5. Legal basis and your choices

[State the legal basis applicable to your deployment, e.g. legitimate
interests in providing a requested research service, or contract performance.]

Because the Service is not designed to hold personal data, it provides no
per-user profile, no account, and no user-level data export. If you believe
personal data reached the Service through a query, contact
[Contact Email] and we will locate and delete the affected log entries.

## 6. Third-party services

Search requests are forwarded to the following third parties, each with its
own privacy practices:

| Provider | Purpose | Operator |
|---|---|---|
| NCBI E-utilities (PubMed) | literature search | US National Library of Medicine |
| Europe PMC REST API | literature search, full-text links | EMBL-EBI |
| Crossref REST API | DOI and metadata validation | Crossref |
| openFDA (`api.fda.gov`) | device regulatory records | US Food and Drug Administration |
| Official regulator websites | regulatory publications | FDA, SFDA, MHRA, EC, Health Canada, TGA |
| Official manufacturer websites | IFUs and technical documents | the respective manufacturers |

The Service will not send requests to any domain outside its configured
allowlist. It does not use general web search engines, advertising networks or
analytics services.

## 7. Data sharing and sale

We do **not** sell, rent or trade any data received by the Service. Data is
shared only with the upstream providers in section 6, strictly as necessary to
perform the requested search, and with [hosting provider] as our
infrastructure processor.

## 8. Security

Measures in place include: HTTPS-only outbound requests, API-key
authentication with constant-time comparison, a server-side domain allowlist,
SSRF protection, redirect validation, request timeouts, response-size limits,
per-key rate limiting, secret redaction in logs, and a container that runs as
an unprivileged user.

No system is perfectly secure. Report a suspected vulnerability to
[Security Contact Email].

## 9. Compliance status — stated plainly

This Service has **not** been assessed or certified against HIPAA, GDPR,
Saudi PDPL, ISO 27001, SOC 2 or any other framework, and no such compliance is
claimed. If your use requires any of these, you are responsible for the
assessment, the contractual arrangements (such as a Business Associate
Agreement or Data Processing Agreement) and the deployment configuration.

Do not send directly identifying patient data unless the deployment and
data-processing environment is specifically configured for the applicable
privacy and regulatory requirements.

## 10. Children

The Service is intended for dental and medical professionals. It is not
directed at children and we do not knowingly collect data from them.

## 11. International transfers

Upstream providers are located in the United States and the European Union.
Using the Service results in query text being transferred to those
jurisdictions. [Add the transfer mechanism relied on, if applicable.]

## 12. Changes to this policy

We may update this policy. The effective date at the top will change, and
material changes will be announced at [announcement channel]. Continued use
after an update constitutes acceptance.

## 13. Contact

[Company Name]
[Postal Address]
[Contact Email]
