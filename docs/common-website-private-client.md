# Common Website private client

`WebsiteSessionClient` implements the closed Conversation credential/session,
admission, common delivery and history wire profile from Conversation PRs37/40/41.
It is source groundwork: no new public routes, session issuance, deployment flags
or production enrollment are wired by this change.

The configured origin must be HTTPS with the exact intended Conversation hostname.
System trust or an explicit CA verifies the certificate and hostname. Redirects and
environment proxies are disabled. No production plaintext or verify=false option
exists. MockTransport injection is for offline protocol tests; those tests do not
claim a live cluster certificate or encrypted service hop has been installed.

Every request is bounded to65536 UTF-8 JSON bytes. Responses are read incrementally
with decoded byte limits:65536 normally,4096 for recovery/receipts,262144 for history.
Limits include whitespace, escaping and metadata. Duplicate/unknown fields, wrong
original delivery identity and send authority on recover_only are rejected.

Capability values travel only in private JSON, never a URL. Error messages contain
no response body or provider exception. The client performs no automatic retry.
A lost begin response remains uncertain original authority; a future handler must
resolve that exact original attempt and must not blindly start another writer.

Remaining gates: durable creation/IP/integration quotas and poll admission, trusted
public HTTPS/client-IP boundary, public handlers and browser storage/dedup/reload,
positive original socket-write/task-join evidence, trusted production retirement
proof source, current shared grant enrollment, and exact integrated acceptance.
