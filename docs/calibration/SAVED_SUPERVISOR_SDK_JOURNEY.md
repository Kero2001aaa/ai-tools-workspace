# Offline saved quote to supervisor SDK qualification

A combined regression now runs the existing fixed project-source local quote
journey, opens a second reviewed database backup, prepares bounded saved context,
and supplies it to the existing supervisor through the real ChatOpenAI SDK and
production FreeRouteTransport. Only the lowest HTTP transport is MockTransport.
A synthetic key and synthetic planner/specialist/final-review responses are used.
Python socket connection is explicitly refused during inference. No actual
provider request, model-quality claim or new execution interface is introduced.

The success path verifies exact saved context reaches the coding specialist,
three attempted model hops, actual UTF-8 content accounting, zero-price policy,
privacy deny, no fallback, no tools and the512output cap on every mocked wire
request. Separate429 responses at planner, specialist and final review stop at
exactly that call without retries and do not expose the mock provider body.
All cases preserve the completed plan snapshot and durable task record, with
its original one attempt. The source-reading worker is not dispatched again.

This is transport and state integration evidence only. It does not establish
semantic correctness, inference availability, live quota, safe disclosure to a
provider, independent final-review source verification, or complete Stage5
acceptance. The context helper never grants disclosure authority, and the test
uses only the existing reviewed project-source acceptance scope. Synthetic
analysis remains explicitly labeled; no new production model call is wired up.
