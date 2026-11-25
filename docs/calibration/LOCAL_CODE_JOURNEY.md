# Run the local quote-only acceptance journey

From the repository root:

```
python -m scripts.local_code_journey --output-dir /new/private/acceptance-directory
```

This is an explicit offline project acceptance command, not a general file grant.
It reads ONLY this repository's existing agent/mcp_server.py utility definitions,
selects the text_stats definition, and retains that short quote in a NEW private
local plan DB. It checkpoints, copies DB through reviewed backup, retrieves saved
report with no active code context, then completes the remaining status step. It
checks same first step and one attempt, writes report.json with plan/step/task IDs
and source line/hash citations, and prints ASCII-escaped JSON. Output directory
must not exist. No overwrite, cleanup/deletion, source modification, model,
provider, network, GitHub or account action occurs. Environment is restored.
The source excerpt and report remain in local plaintext DB/private backup under
existing project acceptance retention, not published or shared automatically.

Output directory mode0700, report and DB mode0600. Parent directories are trusted
operator configuration. There is no filesystem sandbox, cross-file atomicity,
source authenticity or hard kernel-I/O guarantee. The fixed source must remain
reviewed safe project code. This command does not authorize wider files or a new
retention policy. Failed runs keep their partial NEW directory for inspection;
choose another new directory after fixing the cause, never overwrite an unknown
outcome. Quote-only consistency is not model understanding. stage_accepted=false
is explicit. All display strings remain untrusted; ASCII JSON is terminal-safe
only until another consumer decodes it, so renderers must escape independently.

The command makes the durable source-evidence subset repeatable for the project.
Model-based research/specialist review, actual connected-provider qualification,
publication and complete Stage5.1/5.3acceptance remain separate outcomes.
