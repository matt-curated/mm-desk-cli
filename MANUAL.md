# MM Desk CLI Manual

CLI version 1.0.0 | Documentation updated November 20, 2026

MM Desk CLI supports all 23 methods in the supplied `mm_pool_desk` API reference: authentication, ordinary and batch orders, history, balances, deposit records, withdrawals, and TWAP/VWAP schedules. The additional `chain-balances` command queries on-chain balances through `leaf_getLeafByOwner`.

This manual contains reference information only. All command examples, installation commands, and walkthroughs are in the separate [Examples document](EXAMPLES.md).

## Requirements and configuration

The application requires Python 3.9 or later, `websockets` 15.0.1, and `cryptography` 47.0.0, and `coincurve` 21.0.0. Dependency versions are pinned in `requirements.txt`. The `mm-desk` launcher runs `mm_desk.py`; Zig and the system `openssl` executable are not required.

Keep the application directory together. The launcher needs the adjacent Python modules, and the built-in `manual` command opens the adjacent `mm-desk.1` using the system `man(1)` viewer. A system man viewer is required for `manual` and `--manual`; normal CLI operation does not require it. Installation and launch procedures are in [Examples](EXAMPLES.md#installation-and-launch).

| Setting | Default or location |
|---|---|
| Installed application | `/Users/mattye/Documents/workspace/Curated/scripts/mm-desk-cli` |
| Desk WebSocket URL | `wss://desk.testnet.curated.finance` |
| Chain HTTP JSON-RPC URL | `https://rpc.testnet.curated.finance` |
| Chain owner address | Explicit query address, or the owner linked to the current login |
| Asset registry | Adjacent `assets.json`; loaded at startup |
| Public account associations | Adjacent `accounts.json`; read on each login |
| Request timeout | 15 seconds |
| WebSocket ping interval | 15 seconds |
| Chain query limit | 1000 leaves per request |

`--url` takes precedence over `MM_DESK_URL`, which takes precedence over the built-in endpoint. `MM_DESK_PYTHON` selects the launcher's Python executable. Otherwise the launcher uses the adjacent `.venv/bin/python`, if present, then `python3`. `--no-proxy` disables automatic WebSocket proxy use; HTTP chain queries retain urllib's normal proxy behavior. TLS certificate verification remains enabled.

## Terminal help

Startup `--help` provides usage and options. Inside the CLI, `help` lists commands and `help COMMAND` shows the selected command's options. Command examples remain in `EXAMPLES.md`.

`manual` and startup `--manual` open the native `mm-desk(1)` man page using the system `man` command. They inherit the terminal and normal system pager settings, including `MANPAGER` and `PAGER` where supported by the viewer. With `less`, Space advances a page, `/` searches, `n` repeats the search, and `q` returns. WebSocket reads and keepalives continue while the manual is open. No custom navigation or pager is implemented.

Redirected manual output uses normal man behavior, so it can be piped or saved as formatted text. If man is missing, the CLI reports the missing dependency and points to command help. This Markdown document remains a separate extended reference for reading in a Markdown viewer.

## Authentication and sessions

### Seed inputs

A seed file contains 32 bytes as 64 hexadecimal characters, with an optional `0x` prefix and surrounding whitespace. A desk seed is an Ed25519 private-key seed. A chain private-key file is a separate credential and must not be substituted for it.

A master seed from `mm-keys.py` is supported. The client derives the desk seed using HKDF-SHA256 with salt `bbm-mm-keys/v1` and info `desk/ed25519`, matching `mm-keys.py`.

`--seed-file` and `--master-file` are mutually exclusive. Interactive `login` and `pubkey` prompt for a desk seed with echo disabled if neither file is specified. Non-interactive use requires a file option.

### Challenge-response protocol

Automatic login performs these steps on one WebSocket connection:

1. Obtain the hexadecimal challenge through `mm_challenge`.
2. Decode the challenge to bytes and prefix it with the UTF-8 bytes of `mm_pool_desk/login:`.
3. Sign the resulting message with the desk's Ed25519 private key.
4. Send the hexadecimal public key as `key_id` and the hexadecimal signature as `signature` to `mm_login`.

The seed is processed locally; authentication sends only the public key and signature. Success returns `mm_id` and `scopes` and authenticates the current connection. Manual signing must use the challenge obtained on that same connection. The supplied reference does not specify challenge lifetime or reuse rules.

`login` automates the complete exchange. `challenge` and `login-raw` expose the underlying steps. `pubkey` derives the public key locally without logging in.

### Chain owner linked to a login

The login API returns only `mm_id` and `scopes`, so the CLI resolves the chain owner locally. With master-seed login, it derives the chain key using the `chain/secp256k1/<n>` HKDF label, then derives the Cura account address using the same secp256k1, Keccak-256, and SHA3-256 steps as `mm-keys.py`.

With a desk-seed filename ending in `.desk.seed`, the CLI checks for the matching `.master.seed` file in the same directory. It uses that master only after verifying that its derived desk seed matches the login seed. A mismatch stops login with an error. It does not infer ownership from a neighboring chain-key filename.

The optional `--owner` argument on `login` or automatic startup login explicitly associates a chain owner with that login and takes precedence over derivation and public account associations. This is useful for independently generated keys or a desk key registered to a different chain account. The supplied protocol does not expose the server's registration mapping; the local association should match the account registered with the desk operator. `status` shows the owner and its source. The owner is cleared on reauthentication, authentication failure, disconnection, and reconnection.

A desk seed alone cannot reveal its chain owner. When no owner is supplied or derived from a matching master, the client reads `accounts.json`, which maps WebSocket endpoints to public API keys and their chain owner addresses. The lookup uses the actual login public key, not `mm_id` or the last address queried. It applies to pasted-seed login, seed-file login without a matching master, `login-raw`, and raw `rpc mm_login`. An omitted URL path and the root path are equivalent; scheme, host, other paths, and query parameters remain significant.

The file contains public data only. The supplied installation includes the existing testnet key association, verified against its matching master. To add another account, maintain its endpoint, public API key, and owner in this file. Changes apply on the next login. Duplicate or invalid entries stop login with a configuration error. A missing file or unmatched key leaves the owner unresolved; a combined query then shows the desk result and instructions for configuring the chain owner.

Owner selection follows this order: explicit login `--owner`, owner derived from a matching master, then `accounts.json`. All login forms clear the previous session association before authenticating. The configured owner becomes active only after successful authentication, so a failed login or another key cannot inherit it.

### Connection and terminal state

| Prompt | Meaning |
|---|---|
| `desk[offline]>` | No usable WebSocket connection |
| `desk[login]>` | Connected without authentication |
| `desk[ready]>` | Connected with an authenticated session |

`status` reports current connection and session state. A prompt already displayed when the connection drops may remain visible until the next input cycle. Reconnection clears login state and requires a new login. Each API key should use one WebSocket connection.

Tab completes command names and supported options. Arrow keys browse in-process history, which is not persisted to disk. `quit`, `exit`, Ctrl-D, and Ctrl-C exit the application. Exit and disconnect do not themselves send cancellation requests.

## Market reference

`markets.json` is a source-dated snapshot of the 12 spot books in the project testnet reference. Entries are keyed by desk book name and include `pair_id`, base/quote asset addresses, `price_scale`, raw and normal quantity steps, minimum quote notional, maker/taker fees, stream channels, and order-book snapshot URLs. `base_asset` and `quote_asset` reference the single `assets.json` registry for names and decimals.

Raw price equals quote price per base token multiplied by `price_scale`. Raw quantity must be a multiple of `quantity_step_raw`. Large raw amounts are JSON strings. Metadata includes supported candle intervals, maximum snapshot depth, source URL, source verification date, and retrieval date. This file is a reference snapshot, not a live market-status endpoint.

## Order request latency

Every sent `place`, `batch`, `modify`, `cancel`, `cancel-all`, `algo-place`, and `algo-cancel` request prints `TTL (RTT)` in milliseconds to the terminal's standard error stream when the request completes. Full method names and raw `rpc` calls use the same timing path. Each batch has one RTT for the entire request.

RTT is measured with a monotonic clock immediately before WebSocket send until the matching response is received. It includes sending, network transit, desk processing, and the return trip. Local argument validation, JSON serialization, log writing, and output formatting are excluded. This measures the desk response, not subsequent execution or settlement. Accepted responses, business rejections, and RPC errors all have measured RTTs. Timeouts and connection failures without a matching response print an unavailable RTT and the elapsed waiting time; they do not trigger retries. Local validation and login/scope failures before sending produce no RTT record.

Each completed request also appends one JSON object per line to `$XDG_STATE_HOME/mm-desk/latency.jsonl`. If XDG_STATE_HOME is unset or not absolute, the default is `~/.local/state/mm-desk/latency.jsonl`. Startup `--latency-log FILE` overrides the path. The interactive startup banner shows the selected path. Directories and the log are created on first use, with new files readable and writable only by their owner. Existing records are preserved across restarts.

Records include a UTC timestamp, run ID, endpoint without URL credentials or query parameters, desk account ID, JSON-RPC request ID, method, request references, returned order/request IDs, outcome, `rtt_ms`, and `elapsed_ms`. A missing response has `rtt_ms: null`. RPC errors include their numeric error code. Logs exclude login payloads, seeds, signatures, JWTs, and full request/response bodies. A log-write failure prints a warning and preserves the actual request result.

## Input conventions

Prices, quantities, remaining quantities, withdrawal amounts, and algorithm limit prices are decimal strings of raw on-chain integers. Fractional notation and JSON floating-point amounts are not accepted. The client does not infer market price scales, quantity scales, tick sizes, or minimum notionals; the service enforces those rules.

Positive amount fields must exceed zero; `remaining` may be zero. Ordinary CLI ID options convert unsigned decimal text without leading zeros to JSON integers and preserve other nonempty IDs as strings. Raw `rpc` JSON preserves explicit ID types.

`ts`, `start`, `end`, and history-filter `from`/`to` use Unix milliseconds. Withdrawal `to` is a Cura account address. `interval_ms` and `timeout_ms` are millisecond durations. For known methods, missing `ts` fields are filled with the current time, including every order in a batch. Explicit timestamps are preserved. The protocol requires timestamps within five seconds of the desk clock.

History ranges include `from` and exclude `to`. When both boundaries are supplied, `to` must be greater than `from`. Omitted optional fields are not sent, except automatic timestamps. `--post-only` sends true, `--no-post-only` sends false, and omitting both leaves the server default in effect.

`rpc` accepts a JSON object or an `@`-prefixed JSON file path. Known methods are validated and receive automatic timestamps. Unknown methods receive the provided object without inferred fields.

`batch` accepts an order array or an object containing an `orders` array, provided inline or through an `@`-prefixed file path. A batch contains 1 to 50 orders, each using the ordinary order schema. Relative files resolve against the process working directory, not automatically against the application directory.

## Balances and token units

### Desk balances

`balances` queries the logged-in desk account through `mm_balances`. It displays server fields including `available`, `in_flight`, `settled`, `reserved`, `pending_out`, `pending_in`, `frozen`, and `deposits_in_progress`.

### Chain balances

`check-balance` and `chain-balances` are aliases. With a positional address or `--owner`, they query any valid chain owner, whether disconnected, connected without authentication, or logged in. An explicit query address does not replace the owner linked to the login. The positional address and `--owner` cannot be combined.

After login, omitting the address queries both the current desk account through `mm_balances` and its locally linked chain owner through `leaf_getLeafByOwner`. Results are shown in separate desk and chain sections. When the desk returns an empty balance list, its section remains visible and displays `Empty (no desk balances).` JSON mode preserves the empty `balances` array. A failed query still displays an error. With `--json`, the result contains `mm_id`, `desk`, and `chain`. The two queries run independently; a failed source does not suppress the successful source. An incomplete combined query reports per-source errors and exits with code 1 in command mode.

Before login, an omitted address triggers an interactive address prompt. Non-interactive execution before login requires an explicit address. A balance query does not disconnect, reconnect, or replace the session. Offline mode is optional.

The query retains leaves whose `info.deleted_txhash` is null and whose second index value is hexadecimal 3. It interprets the second 32-byte word of `leaf.data` as an unsigned amount and sums amounts by operator address. Aggregation and decimal formatting use exact integer arithmetic.

Asset names and decimal precision come from `assets.json`. Unknown operators retain their address and raw amount without guessed precision. JSON results include `raw`, `decimals`, and `balance`. Malformed balance leaves are excluded and reported.

Only one page is fetched. A full page, continuation marker, or malformed leaves triggers a potentially incomplete-result indication. No pagination protocol was supplied, so the client does not infer continuation parameters.

Chain owner balances and desk account balances use different APIs and identities. A chain balance does not establish that funds were credited to the logged-in desk's `available` balance. The desk remains authoritative for its own balance checks. Use `transfer to-desk` to construct a chain deposit into the desk pool.

### Shared asset registry and display

`assets.json` is the single maintenance file for asset addresses, names, and decimal precision. Each JSON key is a 20-byte asset address. Its metadata contains a unique `name` and an optional `decimals` integer from 0 to 255. A missing or null precision keeps amounts raw. Names and addresses are matched without regard to case. Invalid entries and duplicate addresses or names cause a startup error. Restart the CLI after editing the file.

Known asset addresses are displayed as `NAME（ADDRESS）` throughout result objects, balance tables, rejection details, and error messages, with the full address retained. Unknown addresses are left unchanged. The same registry supplies token-name resolution for withdrawal options. The display formatting does not alter request parameters, raw response objects, or integer amounts. Displayed JSON also uses the annotated address strings.

## Chain and desk transfers

The standalone `chain-operator` program queries any chain address and transfers between chain accounts without a desk connection. It provides standard subcommands, per-command `--help`, and a native `chain-operator(1)` page. Both tools use the same `assets.json` and chain implementation.

Within mm-desk, `transfer to-desk` constructs and simulates a `deposit_pool` transaction. It requires a chain key or master file matching the chain owner linked to the authenticated session. Network parameters are selected by desk endpoint from `desk_networks.json`. The deposit uses the spot contract and creates an asset locked in its custody for the configured pool owner; it is not an ordinary transfer to that owner's address.

`transfer from-desk` calls `mm_withdraw`. The destination defaults to the linked chain owner and must match it if supplied. Submission requires the desk key's `withdraw` scope; no chain private key is needed.

Both directions accept exactly one of `--amount` in normal token units or `--raw-amount` in smallest units. Conversion uses exact integer arithmetic and the shared asset precision. This differs from the existing low-level `withdraw --amount`, which remains a raw integer API option.

The default is preview only. `--dry-run` explicitly selects it. Chain deposits are validated with `transaction_debugTransaction`; desk withdrawals only show a local request preview because the API has no simulation method. `--yes` submits once after validation. It cannot be combined with `--dry-run`.

After submitting, the default behavior waits for chain finality and then the matching deposit record, or for the matching withdrawal request's finalized payout. `--no-wait` returns after submission. `--wait-timeout` defaults to 120 seconds per waiting stage; `--timeout` controls individual HTTP calls. A timeout does not undo a submitted transfer. The transaction hash or withdrawal request ID remains available for follow-up, and the client never automatically resubmits.

The current testnet configuration comes from the project's testnet reference. Its `deposit_pool` construction was checked against the published command-leaf layout, the pool's chain asset layout, and successful testnet operator/unlocker simulation. The private-key signature implementation uses native libsecp256k1 through coincurve.

## Orders and outcomes

Ordinary orders require book, side, price, quantity, and `gtc` or `ioc` time-in-force. Post-only is optional. Modifications require at least one of price, quantity, or remaining. `quantity` is the new total; `remaining` is the new remaining quantity. The latter two are mutually exclusive.

`cancel-all` with a book limits cancellation to that book; without a book it requests cancellation across all books for the current account.

Order states are `pending`, `open`, `partially_filled`, `filled`, `closed`, and `suspended`. Live-order queries return pending, open, partially filled, and suspended orders. Order details include `fills`, execution amounts, fees, settlement states, and transaction hashes. Request states are `pending`, `accepted`, and `rejected`.

A business rejection is a normal JSON-RPC result with rejected status and details, distinct from a JSON-RPC error. Acceptance does not establish execution or final settlement. Batch outcomes are returned per item; one rejection does not imply rollback of the other items.

### Algorithmic orders

TWAP/VWAP schedules specify total quantity, a worst acceptable slice price, an end time, a positive interval, and optionally a start time. End must be later than start, or later than the request timestamp when start is omitted.

The supplied protocol describes one IOC slice per interval at the limit price. Slices are ordinary orders linked by `parent_id`. Schedule queries expose state, filled quantity, in-slice quantity, reserved quantity, end reason, and slices. States are `running`, `paused`, `done`, and `cancelled`.

### Withdrawals, deadman, and market data

Withdrawals require `withdraw` scope and the owner's Cura account address. Ordinary CLI token options accept an operator address or a configured token symbol, which is converted locally. Raw `rpc` JSON does not convert symbols.

`deposits` and `withdrawals` query records. `deadman` explicitly sets a server timeout; zero disables it. The client does not automatically arm, disarm, or renew deadman, and does not assume WebSocket ping renews it.

`gateway-token` returns a market-data JWT, channels, and expiration in Unix seconds. The separate Centrifugo subscription protocol is not implemented by this client.

## API command reference

Each short command also accepts its complete `mm_*` method name with the same options. Scope `none` needs no login; `any` means any authenticated key. Other scopes must appear in the session's `scopes` list.

| Command | API method | Scope | Purpose |
|---|---|---|---|
| `challenge` | `mm_challenge` | `none` | Obtain a challenge for the current connection. |
| `login-raw` | `mm_login` | `none` | Authenticate with a public key and a signature over the current challenge. |
| `deadman` | `mm_setDeadman` | `trade` | Set the server deadman timeout; zero disables it. |
| `gateway-token` | `mm_gatewayToken` | `any` | Obtain a market-data gateway credential. |
| `place` | `mm_placeOrder` | `trade` | Submit an ordinary order. |
| `batch` | `mm_placeOrders` | `trade` | Submit between 1 and 50 ordinary orders. |
| `modify` | `mm_modifyOrder` | `trade` | Modify price, total quantity, or remaining quantity. |
| `cancel` | `mm_cancelOrder` | `trade` | Request cancellation of one order. |
| `cancel-all` | `mm_cancelAll` | `trade` | Request cancellation of all orders, optionally restricted to one book. |
| `open-orders` | `mm_openOrders` | `any` | List live orders, optionally restricted to one book. |
| `order` | `mm_getOrder` | `any` | Retrieve one order and its fills. |
| `orders` | `mm_listOrders` | `any` | List order history with filters and pagination. |
| `request` | `mm_getRequest` | `any` | Retrieve one pool request. |
| `requests` | `mm_listRequests` | `any` | List pool request history with filters and pagination. |
| `balances` | `mm_balances` | `any` | Retrieve balances for the authenticated desk account. |
| `deposits` | `mm_deposits` | `any` | Retrieve desk deposit records. |
| `withdraw` | `mm_withdraw` | `withdraw` | Request a withdrawal to the owner's Cura account address. |
| `withdrawals` | `mm_withdrawals` | `any` | Retrieve withdrawal records and payout status. |
| `algo-place` | `mm_placeAlgo` | `trade` | Create a TWAP or VWAP schedule. |
| `algo-cancel` | `mm_cancelAlgo` | `trade` | Cancel an algorithmic order. |
| `algo-pause` | `mm_pauseAlgo` | `trade` | Pause an algorithmic order. |
| `algo-resume` | `mm_resumeAlgo` | `trade` | Resume an algorithmic order. |
| `algo` | `mm_getAlgo` | `any` | Retrieve an algorithmic order and its slices. |

### challenge

API method: `mm_challenge`. Required scope: `none`. Obtain a challenge for the current connection.

No parameters.

### login-raw

API method: `mm_login`. Required scope: `none`. Authenticate with a public key and a signature over the current challenge.

| Option | Required | Type | Meaning |
|---|---|---|---|
| `--key-id` | Yes | 32 bytes as 64 hexadecimal characters | Public API key. |
| `--signature` | Yes | 64 bytes as 128 hexadecimal characters | Ed25519 signature for the challenge on this connection. |

### deadman

API method: `mm_setDeadman`. Required scope: `trade`. Set the server deadman timeout; zero disables it.

| Option | Required | Type | Meaning |
|---|---|---|---|
| `--timeout-ms` | Yes | Nonnegative integer | Timeout in milliseconds; zero disables deadman. |

### gateway-token

API method: `mm_gatewayToken`. Required scope: `any`. Obtain a market-data gateway credential.

No parameters.

### place

API method: `mm_placeOrder`. Required scope: `trade`. Submit an ordinary order.

| Option | Required | Type | Meaning |
|---|---|---|---|
| `--book` | Yes | String | Order book identifier. |
| `--side` | Yes | buy / sell | Order direction. |
| `--price` | Yes | Positive raw integer, encoded as a decimal string | Raw integer price; server market rules apply. |
| `--quantity` | Yes | Positive raw integer, encoded as a decimal string | Order quantity. |
| `--tif` | Yes | gtc / ioc | Time-in-force. |
| `--post-only` | No | Boolean | Positive or negative boolean flag; omit for the server default. |
| `--ts` | No | Nonnegative integer | Unix milliseconds; automatically filled if omitted. |

### batch

API method: `mm_placeOrders`. Required scope: `trade`. Submit between 1 and 50 ordinary orders.

| Option | Required | Type | Meaning |
|---|---|---|---|
| `--orders` | Yes | Order array or file | Each item follows the ordinary order schema; timestamps are filled per item. |

### modify

API method: `mm_modifyOrder`. Required scope: `trade`. Modify price, total quantity, or remaining quantity.

| Option | Required | Type | Meaning |
|---|---|---|---|
| `--order-id` | Yes | Integer or nonempty string ID | Order ID returned by the desk. |
| `--price` | No | Positive raw integer, encoded as a decimal string | Raw integer price; server market rules apply. |
| `--quantity` | No | Positive raw integer, encoded as a decimal string | New total quantity. |
| `--remaining` | No | Nonnegative raw integer, encoded as a decimal string | New remaining quantity; cannot be combined with quantity. |
| `--ts` | No | Nonnegative integer | Unix milliseconds; automatically filled if omitted. |

### cancel

API method: `mm_cancelOrder`. Required scope: `trade`. Request cancellation of one order.

| Option | Required | Type | Meaning |
|---|---|---|---|
| `--order-id` | Yes | Integer or nonempty string ID | Order ID returned by the desk. |
| `--ts` | No | Nonnegative integer | Unix milliseconds; automatically filled if omitted. |

### cancel-all

API method: `mm_cancelAll`. Required scope: `trade`. Request cancellation of all orders, optionally restricted to one book.

| Option | Required | Type | Meaning |
|---|---|---|---|
| `--book` | No | String | Order book identifier. |
| `--ts` | No | Nonnegative integer | Unix milliseconds; automatically filled if omitted. |

### open-orders

API method: `mm_openOrders`. Required scope: `any`. List live orders, optionally restricted to one book.

| Option | Required | Type | Meaning |
|---|---|---|---|
| `--book` | No | String | Order book identifier. |

### order

API method: `mm_getOrder`. Required scope: `any`. Retrieve one order and its fills.

| Option | Required | Type | Meaning |
|---|---|---|---|
| `--order-id` | Yes | Integer or nonempty string ID | Order ID returned by the desk. |

### orders

API method: `mm_listOrders`. Required scope: `any`. List order history with filters and pagination.

| Option | Required | Type | Meaning |
|---|---|---|---|
| `--book` | No | String | Order book identifier. |
| `--from` | No | Nonnegative integer | Inclusive lower time boundary in Unix milliseconds. |
| `--to` | No | Nonnegative integer | Exclusive upper time boundary in Unix milliseconds. |
| `--state` | No | String | Server-side state filter. |
| `--limit` | No | Nonnegative integer | Maximum number of records requested. |
| `--offset` | No | Nonnegative integer | Pagination offset. |

### request

API method: `mm_getRequest`. Required scope: `any`. Retrieve one pool request.

| Option | Required | Type | Meaning |
|---|---|---|---|
| `--pool-request-id` | Yes | Integer or nonempty string ID | Pool request ID returned by the desk. |

### requests

API method: `mm_listRequests`. Required scope: `any`. List pool request history with filters and pagination.

| Option | Required | Type | Meaning |
|---|---|---|---|
| `--book` | No | String | Order book identifier. |
| `--from` | No | Nonnegative integer | Inclusive lower time boundary in Unix milliseconds. |
| `--to` | No | Nonnegative integer | Exclusive upper time boundary in Unix milliseconds. |
| `--state` | No | String | Server-side state filter. |
| `--limit` | No | Nonnegative integer | Maximum number of records requested. |
| `--offset` | No | Nonnegative integer | Pagination offset. |

### balances

API method: `mm_balances`. Required scope: `any`. Retrieve balances for the authenticated desk account.

No parameters.

### deposits

API method: `mm_deposits`. Required scope: `any`. Retrieve desk deposit records.

No parameters.

### withdraw

API method: `mm_withdraw`. Required scope: `withdraw`. Request a withdrawal to the owner's Cura account address.

| Option | Required | Type | Meaning |
|---|---|---|---|
| `--token` | Yes | Operator address or configured token symbol | Symbol conversion applies to ordinary CLI options only. |
| `--amount` | Yes | Positive raw integer, encoded as a decimal string | Withdrawal amount in raw token units. |
| `--to` | Yes | String | Owner's Cura account address. |
| `--ts` | No | Nonnegative integer | Unix milliseconds; automatically filled if omitted. |

### withdrawals

API method: `mm_withdrawals`. Required scope: `any`. Retrieve withdrawal records and payout status.

No parameters.

### algo-place

API method: `mm_placeAlgo`. Required scope: `trade`. Create a TWAP or VWAP schedule.

| Option | Required | Type | Meaning |
|---|---|---|---|
| `--book` | Yes | String | Order book identifier. |
| `--side` | Yes | buy / sell | Order direction. |
| `--quantity` | Yes | Positive raw integer, encoded as a decimal string | Total schedule quantity. |
| `--limit-price` | Yes | Positive raw integer, encoded as a decimal string | Worst price at which a slice may trade. |
| `--start` | No | Nonnegative integer | Start time in Unix milliseconds. |
| `--end` | Yes | Nonnegative integer | End time in Unix milliseconds; must be later than start or request timestamp. |
| `--interval-ms` | Yes | Nonnegative integer | Slice interval in milliseconds; must be greater than zero. |
| `--kind` | Yes | twap / vwap | Algorithm type. |
| `--ts` | No | Nonnegative integer | Unix milliseconds; automatically filled if omitted. |

### algo-cancel

API method: `mm_cancelAlgo`. Required scope: `trade`. Cancel an algorithmic order.

| Option | Required | Type | Meaning |
|---|---|---|---|
| `--parent-id` | Yes | Integer or nonempty string ID | Algorithmic order ID. |
| `--ts` | No | Nonnegative integer | Unix milliseconds; automatically filled if omitted. |

### algo-pause

API method: `mm_pauseAlgo`. Required scope: `trade`. Pause an algorithmic order.

| Option | Required | Type | Meaning |
|---|---|---|---|
| `--parent-id` | Yes | Integer or nonempty string ID | Algorithmic order ID. |
| `--ts` | No | Nonnegative integer | Unix milliseconds; automatically filled if omitted. |

### algo-resume

API method: `mm_resumeAlgo`. Required scope: `trade`. Resume an algorithmic order.

| Option | Required | Type | Meaning |
|---|---|---|---|
| `--parent-id` | Yes | Integer or nonempty string ID | Algorithmic order ID. |
| `--ts` | No | Nonnegative integer | Unix milliseconds; automatically filled if omitted. |

### algo

API method: `mm_getAlgo`. Required scope: `any`. Retrieve an algorithmic order and its slices.

| Option | Required | Type | Meaning |
|---|---|---|---|
| `--parent-id` | Yes | Integer or nonempty string ID | Algorithmic order ID. |

## Auxiliary command reference

| Command | Arguments | Behavior |
|---|---|---|
| `connect` | Optional WebSocket URL | Connect to that URL or reconnect to the previous URL; clear login state |
| `disconnect` | None | Close the connection and clear authentication |
| `login` | Optional, mutually exclusive `--seed-file` / `--master-file`; optional `--owner` | Automatic login and local chain-owner association; hidden desk-seed input if neither file is supplied |
| `pubkey` | Optional, mutually exclusive `--seed-file` / `--master-file` | Derive the public API key locally |
| `status` | None | Show connection, session, linked chain owner and source, and ignored-message count |
| `now` | None | Print current Unix milliseconds |
| `help` | Optional command name | Show all commands or a selected command's options |
| `?` | Same as help | Alias for help |
| `methods` | Same as help | Alias for help |
| `manual` | None | Open the native mm-desk(1) page with the system man viewer |
| `transfer` | `to-desk` or `from-desk`, with direction-specific options | Preview or submit a chain/desk transfer; use direction `--help` for the full reference |
| `check-balance` | Address or options listed below | Query an explicit chain address; without an address after login, query both balance sources |
| `chain-balances` | Same as check-balance | Alias for check-balance |
| `rpc` | Method and optional JSON object or file | Call a method; omitted parameters default to an empty object |
| `quit` | None | Exit and close the connection |
| `exit` | None | Alias for quit |

### Chain balance options

| Option | Required | Default | Description |
|---|---|---|---|
| `ADDRESS` | No after login | Current login when omitted | Explicit 20-byte, `0x`-prefixed chain address; omission after login queries both sources, before login prompts interactively |
| `--owner` | Alternative to ADDRESS | None | Specify the address as an option; mutually exclusive with the positional address |
| `--rpc-url` | No | Configured chain RPC | HTTP or HTTPS JSON-RPC endpoint |
| `--limit` | No | 1000 | Positive maximum leaf count for one request |
| `--json` | No | Disabled | Structured output instead of a table |

## Startup options

These options belong to the application launcher. Copyable invocations are in [Examples](EXAMPLES.md#startup-option-examples).

| Option | Value | Behavior |
|---|---|---|
| `--help`, `-h` | None | Show startup help and exit |
| `--version` | None | Show the version and exit |
| `--manual` | None | Open the native man page without connecting |
| `--url` | WebSocket URL | Override the configured endpoint |
| `--seed-file` | Path | Automatically log in using a desk seed |
| `--master-file` | Path | Derive a desk seed and chain owner and automatically log in |
| `--owner` | Chain address | Override the chain owner for automatic login; requires a seed or master file option |
| `--timeout` | Positive finite seconds | Request timeout; default 15 |
| `--latency-log` | Path | Append order request timing records to this JSONL file; defaults to the XDG state directory |
| `--no-proxy` | None | Disable automatic WebSocket proxy use |
| `--offline` | None | Skip startup desk connection; local commands and chain queries remain available |
| `--demo` | None | Start the local simulator and authenticate with its public fixture |
| `--command`, `-c` | Interactive command string | Execute and exit; repeat for sequential commands in one session |

Seed and master options are mutually exclusive. Offline mode cannot be combined with automatic login. Demo mode cannot be combined with an explicit URL, `MM_DESK_URL`, key-input options, `--owner`, or offline mode.

Command sequences stop on an error or business rejection. Output may include connection and login messages; it is not a dedicated JSON-only stream.

| Exit code | Meaning |
|---:|---|
| 0 | Successful command-mode completion |
| 1 | Parameter, connection, or RPC failure |
| 2 | Invalid startup arguments or business rejection in command mode |
| 130 | User interrupt |

## Errors and recovery

| Condition | Meaning and response |
|---|---|
| `unauthorized`, -32001 | Check key registration and authenticate again |
| `stale_timestamp`, -32002 | Correct clock skew and avoid old timestamps |
| `rate_limited`, -32003 | Reduce request frequency |
| `not_implemented`, -32004 | The running desk version lacks the method |
| `not_ready`, -32005 | The service is not ready |
| `internal`, -32010 | Server failure; retain public request information for diagnosis |
| `not_found`, -32011 | ID not found or unavailable to this account |
| `invalid_terms` | Inspect details for quantity step, minimum notional, or other business rules |
| `insufficient_balance` | The desk has insufficient available balance |
| Missing scope | The key lacks the required trade or withdraw permission |
| Execution status unknown | The request may have been sent before failure; inspect order/request state before deciding whether to submit again |

Trading requests are not automatically retried, and reconnection does not replay history. Messages without a matching pending request ID are skipped. Business rejection details are displayed even for valid JSON-RPC results.

The application does not persist seeds, signatures, JWTs, or command history to its own logs. The latency log stores only the order request metadata described above. Requested results are printed, including gateway JWTs.

## Simulation and verification

The simulator uses a public test seed and loopback WebSocket. It verifies protocol interactions and signatures but does not perform real matching, settlement, algorithm execution, or timed deadman cancellation. Automated tests do not use user credentials or connect to UAT.

The latest code verification passed 84 local tests, covering all 23 desk methods, chain parsing, arbitrary-address and combined balance queries, owner derivation, pasted-seed and raw-login account lookup, endpoint/key isolation, partial failures, shared asset metadata, and display-only address annotation. On November 18, 2026, UAT login succeeded for `mm_id` 28 with trade/read scopes. Desk queries returned no balances or orders. Adjusted test orders were rejected for insufficient balance, and no open order remained. Successful live modification/cancellation, withdrawal, and algorithm execution were not verified. Historical results are in `uat-verification.json`.

On November 19, 2026, a read-only combined query authenticated as desk account 28, resolved the chain owner from the matching local master, and returned 13 chain assets with asset-name annotations. The desk returned an empty balance list and the session remained connected. The matching master establishes the local key relationship; the two balance sources do not establish deposit credit into the desk.

## Files and protocol sources

| File | Purpose |
|---|---|
| `mm-desk` | Interactive desk launcher |
| `chain-operator` | Standalone chain CLI launcher |
| `chain_operator.py` | Chain CLI subcommands and argument parsing |
| `chain_protocol.py` | Leaf/transaction encoding and native recoverable signatures |
| `chain_ops.py` | Chain RPC, pagination, exact amounts, simulation and submission |
| `desk_transfers.py` | Desk deposit and withdrawal orchestration |
| `desk_networks.json` | Endpoint-specific desk pool and contract parameters |
| `markets.json` | Source-dated trading pair parameters keyed by book |
| `chain-operator.1` | Native chain CLI manual |
| `mm_desk.py` | CLI, schemas, authentication, and request dispatch |
| `chain_balances.py` | Chain query and exact amount formatting |
| `assets.json` | Single address-to-asset-name and decimal-precision registry |
| `asset_registry.py` | Registry loading and display-only asset annotation |
| `account_keys.py` | Local chain-owner derivation and public account lookup |
| `accounts.json` | Public API key to chain owner associations scoped to each desk endpoint |
| `demo_server.py` | Local protocol simulator |
| `requirements.txt` | Tested dependencies |
| `mm-desk.1` | Native man(1) reference used by terminal manual commands |
| `MANUAL.md` | Extended reference for Markdown viewers |
| `EXAMPLES.md` | Command examples and walkthroughs |
| `examples/orders.json` | Batch input fixture |
| `tests/` | Local automated tests |
| `uat-verification.json` | Historical UAT verification |

The desk protocol follows the supplied Reference - mm_pool_desk API, marked with commit `8e97592`, and the Python authentication demo. Chain queries follow the supplied curl/Python logic and token mapping. Current service rules and key scopes determine whether an operation is accepted.

The transfer extension was verified with 77 local tests and testnet read-only calls: a 13-asset paginated balance query, a chain transfer simulation, a deposit_pool simulation, and a desk withdrawal preview. No transaction was broadcast during this verification. Account 28 returned trade/read scopes; real desk withdrawal requires the operator to grant withdraw scope.
