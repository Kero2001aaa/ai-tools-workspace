# Stage 10 — Scheduled automation

The workspace now runs its existing quality and security workflow automatically every Wednesday at 04:23 UTC.

## What runs

The scheduled GitHub Actions workflow `.github/workflows/quality-security.yml`:

1. Checks out the repository and prepares Python 3.12.
2. Installs the project's base Python dependencies.
3. Runs the complete unit-test suite.
4. Exports the approved MCP tool names, descriptions, and input schemas.
5. Scans the exported MCP inventory with Cisco MCP Scanner's YARA and schema analyzers.
6. Audits both base and optional browser dependency manifests against OSV vulnerability data.

The same workflow also runs on pushes and pull requests to `main`, and can be started manually with **Actions → Workspace quality and security → Run workflow**.

## Cost and permissions

- No LLM inference is performed.
- No model, Exa, Firecrawl, Cisco, or other API key is required by this workflow.
- The workflow requests read-only repository contents permission.
- Scanner and dependency-audit tools are invoked with `uvx`; they are not installed into the agent's runtime environment.
- GitHub-hosted runner usage is subject to the repository's GitHub Actions plan and current usage policy. Public repositories generally have free standard-runner usage; verify current GitHub policy rather than assuming unlimited capacity.

## Scheduling limitations

GitHub Actions scheduled workflows use UTC and may start later than the scheduled minute during periods of high load. Scheduled workflows run from the default branch. GitHub may automatically disable scheduled workflows in public repositories after 60 days without repository activity. Use the manual **Run workflow** action if needed.

## What this stage does not do

This is repository maintenance automation, not an autonomous agent scheduler. It does not run the LLM, browse websites, modify application data, or deploy services. Those capabilities should be added as separate, narrowly scoped workflows with explicit permissions, bounded execution, logs, and approval gates for consequential actions.

## References

- [GitHub Actions: Events that trigger workflows](https://docs.github.com/en/actions/using-workflows/events-that-trigger-workflows#schedule)
- [GitHub Actions: Usage limits and billing](https://docs.github.com/en/billing/managing-billing-for-github-actions/about-billing-for-github-actions)
- [Cisco AI Defense MCP Scanner](https://github.com/cisco-ai-defense/mcp-scanner)
- [OSV vulnerability database](https://osv.dev/)
