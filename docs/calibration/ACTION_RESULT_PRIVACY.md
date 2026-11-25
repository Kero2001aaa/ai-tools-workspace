# Local dashboard execution-result privacy fences

Changing or clearing the control token now hides existing action/agent results
as well as monitoring and saved reports. Each action family also uses the token,
monitoring epoch and its latest request number before rendering a success or
error. A late response cannot repopulate a locked page, a page whose token was
cleared and reentered with the same value, or overwrite a newer request result.
An obsolete action response does not trigger a monitoring reload with a new token.

This is display privacy, not action cancellation, request deduplication or server
authorization. Already submitted actions may complete. The page says to inspect
current state before retrying an uncertain action; no automatic retry is added.
Action errors shown by these two paths are generic rather than raw exceptions.
Existing API routes, request payloads, confirmation steps and allowlists stay the
same. The correction does not make the dashboard safe for public hosting.

Tests execute the actual JS for both action POST and agent POST using deferred
responses and errors, token changes, same-token reentry and superseded requests.
No actual model/provider or task dispatch is performed by those tests.

A successful action response is retained if its separate follow-up monitoring
refresh fails. The monitoring card shows that failure separately, with no new
POST or retry. Existing latest-request/token fences also guard that message.
