# Research source-evidence projection, local candidate

Stage5.1candidate, not connected-service qualification or live research success.
Reuses existing Exa/Firecrawl normalization and public URL validation. Caps output
at10sources,12000characters per search excerpt,300title characters and2048URL
characters. Search input capped2000characters, integer result count1-10.
Malformed source shapes fail closed. Evidence SHA-256covers exactly the returned
excerpt, not the original page or authenticity. Truncation is explicit. Scrape
uses the existing30000character text cap, adds excerpt digest and validates the
reported source URL. No provider configuration, fallback or network call added.

The fixed existing provider POST routes now use streamed JSON reading capped at
512KiB. Accept-Encodingidentity is requested; any compressed response is refused
before reading, so decompression cannot allocate beyond this cap. Content type
must be application/json; redirects/non200are refused without reading error
bodies or retrying/falling back.35second overall async deadline, existing HTTP
connect/read bounds. Duplicate keys, nonfinite numbers, malformed UTF-8/JSON and
non-object roots fail closed with stable sanitized codes. No raw HTTP exception
containing a URL or key is returned. Deterministic mock streams test these rules;
no real provider request has been made. Existing free-tier availability/billing
is not certified by this change, and no DNS-rebinding protection is claimed by
lexical URL checks. External source text remains untrusted data, not instructions.
The digest proves only identity of returned excerpt bytes, not source authenticity
or an independently fetched page. Stage5.1is not accepted based on this slice.

Source handling corrections: malformed surrogate text is refused with stable
research_response_invalid before digest encoding. Firecrawl metadata.publishedTime
remains supported. Scrape records requested_url and reported_url separately;
url remains the requested target, with source_url_matches_request explicit. A
provider report cannot silently relabel a fetch as another source. Whole-response
rejection on any unsafe search result is intentional for now: sources are not
silently omitted, and no best-effort/partial-success claim is made.
