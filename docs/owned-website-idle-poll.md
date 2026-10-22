# Default-off owned Website idle poll

`POST /v1/sessions/poll` accepts only `session_id` in JSON and the same widget and
session-token headers as visitor history. Owner, integration, agent and binding
are revalidated by Control Plane. The existing direct HTTPS/trusted TCP proxy
policy applies before credentials or body parsing. Browser-supplied poll, writer,
source, attempt and supplier identities are rejected. Bodies are capped at 65,536
bytes and ten seconds; the poll's complete admission/wait/write budget is at most
twenty seconds. Current credential authorization and durable poll admission stay
in Conversation, including one inflight poll and 30/minute burst-two admission.

The Gateway reserves a private poll exactly once, discovers zero or one ordered
candidate, then requests one original delivery acquisition. Discovery never
authorizes socket output. Full public JSON, including escaping and envelope, is
sized before begin; the positive begin's frame is checked again. A DELIVERY has
only a stable opaque delivery ID, actor and text. EMPTY and recovery/oversize
metadata contain no text or private source/intent/attempt IDs and fit 4,096 bytes.
One poll writes at most one delivery. No HTTP/model/provider retry is introduced.

Each private begin, HTTP write and completion task is owned, bounded, shielded
from parent cancellation and actually joined. Repeated cancellation does not
detach a write future. Only successful original `prepare` plus `write_eof`
records WRITE_COMPLETED; this is transport acceptance, never read evidence.
Disconnect, write timeout and lost begin ACK record UNKNOWN, without resending.
A lost begin ACK can submit UNKNOWN with the same original identity after that
begin task has joined: a committed acquisition may accept that evidence, while
an uncommitted/different writer rejects it. No replayed acquisition writes text.
If the original request is cancelled before writing, no write is scheduled and
original-task join can still record UNKNOWN. Completed original tasks are the
only source of TASK_JOINED; a replacement process never invents predecessor join.

The response finishes or its socket closes within the public budget. Bounded
private completion/cleanup can then finish independently. Failed completion is
not retried or promoted to acceptance. Poll cleanup/expiry never ends or retires
the DeliveryAttempt. Permanent process/node loss still requires the common
trusted writer retirement proof source; an unreachable process remains UNKNOWN.
Random per-process supplier/incarnation UUIDs identify original requests and are
not host attestation. No daemon, proof source or new infrastructure is installed.

`COMMON_WEBSITE_POLL_ENABLED=false` by default, independently of the empty common
Website cohort. Enabling it requires a configured common cohort and supports a
smaller `COMMON_WEBSITE_POLL_WAIT_SECONDS`, never more than 20. Production enablement
still requires CS43 durable poll admission, common lifecycle/provisioning,
positive retirement of every legacy writer, deployed peer/TLS/privacy evidence,
the shared trusted retirement source, browser deduplication and end-to-end QA.
This source change does not satisfy or bypass those gates or the paid QA gate.
