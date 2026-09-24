# Private process-owner channel — authority slice only

Local explicit General Python `serve` launches may use inherited duplex FD 3.
`main.ts` enables this only on POSIX, without a shell, when the resolved runtime
contains `tui_gateway/owner_bootstrap.py` and the exact supported module/profile
argv shape matches. Unsupported/old backends remain without owner authority.
No renderer/preload/public HTTP/WebSocket API exposes this channel.

Electron generates a fresh 32-byte OS-random capability per child and sends it
only on FD 3. Python makes FD 3 non-inheritable and generates a random transport
generation. Subsequent frames authenticate generation, strictly increasing
sequence, and operation with HMAC-SHA256 and constant-time tag comparison.
Transport EOF permanently revokes that generation. Reconnecting ordinary chat
cannot adopt or recreate owner authority. No caller-selected owner identifiers.
Responses contain neither capability nor authentication tag.

Native-only operations: `status`, `quiesce`, `cancel`. No exit, signal, restart,
configuration mutation, or process selection operations exist. Status reports
`authority: launch-channel`, `process_fence: true`. Quiesce returns the process
admission receipt (`scope`, `status`, `admission`, `generation`, `nonce`,
`inventory`, `inventory_complete`, optional `reason`), not a session receipt.
The generation must match the handshake and the nonce must remain stable until
cancel. Electron validates exact response shapes, integer nonnegative inventory,
complete idle/busy inventory and status consistency before resolving a request.
Unknown receipts retain closed admission but never establish idle. Cancel returns
`admission: open` and clears the tracked nonce. Null, arrays, malformed frames,
error responses and mismatched proofs permanently revoke the channel and remove
the main-only map entry; child exit and channel closure do the same.
Existing session quiescence cannot authorize replacing a shared backend.

## Remaining before activation

- Complete/verify process admission coverage for all producers before activation;
  `serve` has unfenced producers and must remain unknown. Unknown/busy never permits exit.
- Independently verify Python exact-lease cancel/disconnect recovery and queued-race/all-session tests.
- Connect native transition controller to the main-only process owner map; there
  is deliberately no renderer bridge or runtime selection mutation in this slice.
- Exercise complete Electron boot and lifecycle identity bookkeeping with the
  bootstrap module argv. The real ownership reaper regression covers failed-stop
  record retention and successful retry with an exact bootstrap command matcher.
  Cross-language fixtures exercise real FD 3/bootstrap/dispatch and malformed
  Python peer responses, not a live app.
- Independently review before committing or activation. Initial old-app bootstrap
  still requires a later user-owned manual quit/reopen; none was performed.

Tests: Python socketpair exercises foreign keys, stale generation, replay,
unsupported operations, EOF, and no secret receipts; real public RPC dispatch
rejects owner method names despite desktop env. Vitest spawns Python with real
FD 3, exercises native HMAC dispatch and bootstrap, verifies clean stdout/stderr
and closure. Tests do not run models or read live profile credentials.
