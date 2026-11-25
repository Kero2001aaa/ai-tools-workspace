# Explicitly approved local code excerpts

Internal read-only `read_code_excerpt` takes a trusted local repository root and
1-20 exact caller-approved relative file paths. Extensions are a coarse text
filter, not a security grant or proof that content contains no secrets. The caller
must review the approved files. No globbing, discovery, shell/git execution,
network, auto-approval, tool registration or model dispatch is added.

Relative components reject traversal, hidden names, backslashes and controls.
Descriptor-relative directory/file opens with O_NOFOLLOW prevent child symlink
swaps. The root and its ancestors are trusted caller configuration, not an
untrusted user input boundary. This is not a sandbox. Hardlinks, allowed files
containing secrets and hostile same-stat concurrent writes remain outside these
guarantees; caller approval is required. Unsupported OS facilities fail closed.

Nonregular files/FIFOs, more than64KiB, malformed UTF-8 and unsupported control
bytes are refused. Reads stop after64KiB+1; at most200lines are returned. File
stat before/after catches ordinary concurrent changes. Hashes identify the exact
read full bytes and returned excerpt separately, not immutable disk identity,
authorship or truth. Local storage is assumed responsive: the byte cap does not
provide a kernel filesystem I/O deadline. No hard wall-time guarantee is made.

Result carries only approved relative path, line range, full-file byte/line count,
full-file/excerpt digest, truncation and untrusted-data label, never absolute root.
All I/O/decode failures use stable errors. Nothing is stored, executed or added
to plan results. Real temporary file, FIFO, symlink, concurrent-change and actual
repository code-source tests exercise the read boundary. This does not accept
Stage5.1or a code-understanding journey without review and downstream integration.

## Local code quote report, separate candidate

`build_code_quote_report` accepts a question and1-5exact file/line selections,
then reads each through the approved-file boundary. Caller cannot supply quote
text: returned quotes come from those actual reads. Repeated selections from the
same file must have identical full-file hashes or the whole report fails. Different
files are not an atomic snapshot. Output identifies relative path, exact line
range, full/excerpt hashes and quote, with source-data-only labels. It never labels
local code as Exa/Firecrawl or fabricates web URLs. Empty quotes are refused.

This is a quote-only report, not generated code understanding or a correctness
review. No claims, automatic selection, source execution, persistence, plan-result
attachment or model call is added. There is no visual rendering, so downstream UI
must escape all strings and treat supplied question/quotes as untrusted. Maximum
input read is5x64KiB+1across selections, with up to5x200returned lines. Existing
root trust, source secrecy and kernel-I/O limitations apply without expansion.
Line numbering follows Python str.splitlines, including NEL(U+0085), U+2028 and
U+2029 as breaks as well as CR/LF. It may differ from a newline-only editor count.
