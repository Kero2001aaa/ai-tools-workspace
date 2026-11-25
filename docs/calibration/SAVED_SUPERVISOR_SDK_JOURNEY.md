# Offline saved quote to supervisor SDK qualification

A combined regression now runs the existing fixed project-source local quote
journey, opens a second reviewed database backup, prepares bounded saved context,
and supplies it to the existing supervisor through the real ChatOpenAI SDK and
production FreeRouteTransport. Only the lowest HTTP transport is MockTransport.
A synthetic key and synthetic planner/specialist/final-review responses are used.
A Python socket.connect guard is present during inference and is not tripped.
This journey does not exercise a blocked connection. No actual
provider request, model-quality claim or new execution interface is introduced.

The success path verifies exact saved context reaches the coding specialist,
three attempted model hops, actual UTF-8 content accounting, zero-price policy,
privacy deny, no fallback, no tools and the 512-token output cap on every mocked wire
request. Separate 429 responses at planner, specialist and final review stop at
exactly that call without retries and do not expose the mock provider body.
All cases preserve the completed plan snapshot and durable task record, with
its original one attempt. The code_task.execute_code_report attribute is patched
to fail if called; that patch is not a claim about every imported dispatch alias.
Unchanged durable snapshots supply the no-replay state evidence. Actual project
quote provenance rests on the existing fixed-source journey, not the substring
assertion alone. Exact context matching is asserted at the specialist hop only;
budget-byte equality is asserted on the success path only.

This is transport and state integration evidence only. It does not establish
semantic correctness, inference availability, live quota, safe disclosure to a
provider, independent final-review source verification, or complete Stage 5
acceptance. The context helper never grants disclosure authority, and the test
uses only the existing reviewed project-source acceptance scope. Synthetic
analysis remains explicitly labeled; no new production model call is wired up.
