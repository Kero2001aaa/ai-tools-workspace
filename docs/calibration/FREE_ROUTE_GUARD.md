# Free route calibration,24 November2026

This patch adds a fail-closed transport to the shared chat-model factory, not merely a diagnostic flag. Both Workspace loader and Adult chat adapter use that factory. Unverified model/endpoint configuration now fails before constructing a usable client. Every sync/async SDK request is rechecked at HTTP transport after runtime overrides. Only exact OpenRouter chat endpoint, openrouter/free, zero prompt/completion price, data_collection deny, no fallback, nonstreaming and bounded output are allowed. Routing lists, tools and plugins are denied pending separate verification. Redirects and automatic retries are disabled. This is intentionally narrower than the full project vision.

No provider success is claimed. Existing key authenticated; its quota reported50remaining before a single generic probe. That probe returned429 despite available quota metadata. No retry or privacy relaxation followed. Dashboard remains disabled. An existing malformed.env was preserved privately and repaired; key equality verified without printing values. No secrets are in this patch.

Baseline exact PR2 SHA1f808f31:815tests passed locally10.319s, total12.166s. User Codespace at same SHA:815tests passed15.010s. Patched isolated suite834tests passed10.302s, total11.099s. Published exact code also passed834tests in15.286s in the preserved Codespace. Test duration does not measure autonomous engineering throughput or establish a calendar deadline.

Existing Codespace:2cores,7941MB RAM,27GBavailable disk,Python3.12.11. Monthly reported usage26.97two-core wallhours; included120corehours implies about33.03two-core hours remaining, subject to billing lag/current usage. Storage3.16GB-month consumed, not current disk size. $0overage cap preserved. Not enough for24/7continuous execution all month.

Sources inspected:
- https://github.com/settings/billing
- https://github.com/settings/billing/usage?group=2&period=3&query=product%3Acodespaces
- https://github.com/settings/billing/budgets
- https://docs.github.com/en/billing/concepts/product-billing/github-codespaces
- https://openrouter.ai/openrouter/free
- https://openrouter.ai/docs/api_reference/limits
- https://openrouter.ai/docs/guides/routing/provider-selection
- https://openrouter.ai/docs/faq

Limits: no live model inference accepted, no image generation/training path validated, no offline model installed, no always-on daemon. User local hardware pending. Existing image adapters are separate and retain their own cost-class checks; this patch does not certify them. Direct use of an unrelated HTTP library elsewhere is outside this shared chat boundary. Diagnostic response body/key details are never returned; diagnostic redirects are denied too. Additional actual sync/async SDK429tests verify sanitized error text and exactly one network attempt. Unknown modalities, message parts and request extensions fail closed.
