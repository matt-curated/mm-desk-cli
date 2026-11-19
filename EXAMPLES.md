# MM Desk CLI Examples

CLI version 1.0.0 | Updated November 20, 2026

This document contains the command examples separated from the [Manual](MANUAL.md). It covers all 23 desk API commands, all 16 auxiliary commands, and startup options.

## How to use these examples

Blocks labeled **Operating-system terminal** run in your shell. Blocks labeled **Inside the CLI** run at the desk prompt. Trading commands submit when you press Enter. The prices and quantities below demonstrate syntax; replace them with your intended raw integer values. IDs 123, 456, and 789 are illustrative and must be replaced with actual returned IDs.

Uppercase placeholders must be replaced before execution. In particular, OWNER_ADDRESS, FIRST_OWNER_ADDRESS, and SECOND_OWNER_ADDRESS must be replaced with the addresses you want to query; FUTURE_UNIX_MS must be a future Unix millisecond timestamp, YOUR_CURA_ACCOUNT_ADDRESS must be your account address, and the login-raw placeholders must contain valid hexadecimal credentials/signatures. Do not copy a signature from an earlier connection.

Examples using relative files assume the application directory is your working directory. The first command below selects the installed directory on this machine. If installed elsewhere, change that path.

## Installation and launch

**Operating-system terminal:**

```bash
cd /Users/mattye/Documents/workspace/Curated/scripts/mm-desk-cli
```

Dependencies were already verified on this machine. For a fresh Python environment:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
./mm-desk
```

Start and automatically log in with the existing desk seed:

```bash
./mm-desk --seed-file /Users/mattye/Documents/workspace/Curated/keys/my-mm.desk.seed
```

The matching `my-mm.master.seed` beside this desk seed is used to derive its chain owner after verifying the desk seed. Alternatively, derive both the desk seed and chain owner directly from the master seed:

```bash
./mm-desk --master-file /Users/mattye/Documents/workspace/Curated/keys/my-mm.master.seed
```

For a local simulator using a public fixture rather than an account key:

```bash
./mm-desk --demo
```

If MM_DESK_URL is already set, unset it before using demo mode. To query chain balances without connecting to the desk:

```bash
./mm-desk --offline -c "check-balance OWNER_ADDRESS"
```

## Desk API command examples

Order placement, modification, and cancellation automatically print their complete request/response RTT in the CLI and append a timing log. No extra per-order flag is needed. A terminal timing line looks like this; the number below is illustrative:

```text
TTL (RTT): 23.456 ms | place | request=3 | accepted
```

To select a log file at startup, run in the operating-system terminal:

```bash
./mm-desk --latency-log ./latency.jsonl --seed-file /Users/mattye/Documents/workspace/Curated/keys/my-mm.desk.seed
```

With the default path, follow new records from another terminal:

```bash
tail -f "${XDG_STATE_HOME:-$HOME/.local/state}/mm-desk/latency.jsonl"
```

The file is created after the first timed request. Print selected fields from an existing log with jq:

```bash
jq '{timestamp, method, request_id, outcome, rtt_ms, elapsed_ms, results}' "${XDG_STATE_HOME:-$HOME/.local/state}/mm-desk/latency.jsonl"
```

For a timeout without a response, rtt_ms is null and elapsed_ms records the waiting time. Rejected requests and RPC errors are also logged. Batch orders have one timing record per API request.

Each heading names the short command and identifies its corresponding API method. Full mm_* method names accept the same options.

### challenge

API: `mm_challenge`. Scope: `none`.

**Inside the CLI:**

```text
challenge
```

Obtain the challenge on the connection you will use for login. The normal login command performs this step automatically.

### login-raw

API: `mm_login`. Scope: `none`.

**Inside the CLI:**

```text
login-raw --key-id YOUR_64_HEX_PUBLIC_KEY --signature YOUR_128_HEX_SIGNATURE
```

Replace the placeholders with your public key and a signature generated from this connection's challenge. Sign the UTF-8 prefix mm_pool_desk/login: followed by the decoded challenge bytes. Use login for automatic signing. Successful raw login also resolves a configured chain owner from accounts.json using this endpoint and public API key.

### deadman

API: `mm_setDeadman`. Scope: `trade`.

**Inside the CLI:**

```text
deadman --timeout-ms 30000
```

Set the timeout to 30,000 milliseconds. To disable it instead, use the alternative below.

```text
deadman --timeout-ms 0
```

### gateway-token

API: `mm_gatewayToken`. Scope: `any`.

**Inside the CLI:**

```text
gateway-token
```

Return the gateway token, authorized channels, and expiration.

### place

API: `mm_placeOrder`. Scope: `trade`.

**Inside the CLI:**

```text
place --book btc_usdt --side buy --price 6000000 --quantity 10000 --tif gtc --post-only
```

Submit a post-only GTC buy order using raw integer price and quantity. Replace both values with the intended order terms.

### batch

API: `mm_placeOrders`. Scope: `trade`.

**Inside the CLI:**

```text
batch --orders @examples/orders.json
```

Read the batch fixture relative to the current working directory. Edit its orders before submitting, or provide an absolute path to your own file.

The batch file can contain the following JSON array:

```json
[
  {"book":"btc_usdt","side":"buy","price":"6000000","quantity":"10000","tif":"gtc","post_only":true},
  {"book":"btc_usdt","side":"sell","price":"6200000","quantity":"10000","tif":"gtc","post_only":true}
]
```

### modify

API: `mm_modifyOrder`. Scope: `trade`.

**Inside the CLI:**

```text
modify --order-id 123 --price 6100000 --remaining 20000
```

Replace 123 with an actual order ID. This changes the price and remaining quantity. Use --quantity for a new total instead; quantity and remaining cannot be supplied together.

Alternative: change total quantity instead of remaining quantity.

```text
modify --order-id 123 --quantity 20000
```

### cancel

API: `mm_cancelOrder`. Scope: `trade`.

**Inside the CLI:**

```text
cancel --order-id 123
```

Replace 123 with the order ID to cancel.

### cancel-all

API: `mm_cancelAll`. Scope: `trade`.

**Inside the CLI:**

```text
cancel-all --book btc_usdt
```

Request cancellation of all current-account orders in btc_usdt. Omitting the book option would apply to all books.

### open-orders

API: `mm_openOrders`. Scope: `any`.

**Inside the CLI:**

```text
open-orders --book btc_usdt
```

List active orders in one book. Omit the book option to list all books.

### order

API: `mm_getOrder`. Scope: `any`.

**Inside the CLI:**

```text
order --order-id 123
```

Retrieve the actual order ID and its fill details.

### orders

API: `mm_listOrders`. Scope: `any`.

**Inside the CLI:**

```text
orders --book btc_usdt --state closed --limit 20 --offset 0
```

List up to 20 closed orders for the selected book, beginning at offset zero.

Optional time filtering uses an inclusive start and exclusive end. These timestamps describe a historical interval; replace them with your required interval.

```text
orders --book btc_usdt --from 1794931200000 --to 1795017600000 --limit 20
```

### request

API: `mm_getRequest`. Scope: `any`.

**Inside the CLI:**

```text
request --pool-request-id 456
```

Replace 456 with a pool_request_id returned by the desk.

### requests

API: `mm_listRequests`. Scope: `any`.

**Inside the CLI:**

```text
requests --book btc_usdt --state rejected --limit 20 --offset 0
```

List rejected requests for the selected book; omit the state option to include all states.

### balances

API: `mm_balances`. Scope: `any`.

**Inside the CLI:**

```text
balances
```

Query balances belonging to the authenticated desk account. Use chain-balances for the independent chain owner query.

### deposits

API: `mm_deposits`. Scope: `any`.

**Inside the CLI:**

```text
deposits
```

Query recorded desk deposits. This does not construct an on-chain deposit transaction.

### withdraw

API: `mm_withdraw`. Scope: `withdraw`.

**Inside the CLI:**

```text
withdraw --token USDT --amount 1000000 --to YOUR_CURA_ACCOUNT_ADDRESS
```

Replace the address placeholder with your own Cura account address. With the configured six USDT decimals, the raw amount here is 1 USDT. The key must have withdraw scope.

### withdrawals

API: `mm_withdrawals`. Scope: `any`.

**Inside the CLI:**

```text
withdrawals
```

Query withdrawal records, including request and payout state.

### algo-place

API: `mm_placeAlgo`. Scope: `trade`.

**Inside the CLI:**

```text
algo-place --book btc_usdt --side buy --quantity 100000 --limit-price 6000000 --end FUTURE_UNIX_MS --interval-ms 10000 --kind twap
```

Run now, add the desired duration in milliseconds, and replace FUTURE_UNIX_MS with that integer. For a 60-second schedule, add 60000. This example uses a 10-second slice interval; change the kind value to vwap for VWAP.

### algo-cancel

API: `mm_cancelAlgo`. Scope: `trade`.

**Inside the CLI:**

```text
algo-cancel --parent-id 789
```

Replace 789 with the parent_id returned by the algorithm submission.

### algo-pause

API: `mm_pauseAlgo`. Scope: `trade`.

**Inside the CLI:**

```text
algo-pause --parent-id 789
```

Pause the actual parent_id.

### algo-resume

API: `mm_resumeAlgo`. Scope: `trade`.

**Inside the CLI:**

```text
algo-resume --parent-id 789
```

Resume the actual paused parent_id.

### algo

API: `mm_getAlgo`. Scope: `any`.

**Inside the CLI:**

```text
algo --parent-id 789
```

Retrieve schedule state, quantities, and slices for the actual parent_id.

## Auxiliary command examples

### connect

**Inside the CLI:**

```text
connect wss://desk.testnet.curated.finance
```

Connect to the specified desk. With no URL, connect reuses the previous address. Log in again after reconnecting.

### disconnect

**Inside the CLI:**

```text
disconnect
```

Close the WebSocket and clear local authentication. No cancellation request is sent by this command.

### login

**Inside the CLI:**

```text
login --seed-file /Users/mattye/Documents/workspace/Curated/keys/my-mm.desk.seed
```

Automatically request a challenge, sign it, and log in. Calling login without options prompts for a desk seed with input hidden. For the configured testnet account, the public-key association in accounts.json supplies the chain owner:

```text
login
check-balance
```

Enter the desk seed at the hidden prompt. The following check-balance queries both sources without asking for an address. A matching sibling master file enables automatic chain-owner derivation when logging in with a seed file.

Alternative master-seed input:

```text
login --master-file /Users/mattye/Documents/workspace/Curated/keys/my-mm.master.seed
```

For a separately registered chain owner, explicitly associate it with this login:

```text
login --seed-file /Users/mattye/Documents/workspace/Curated/keys/my-mm.desk.seed --owner OWNER_ADDRESS
```

Replace OWNER_ADDRESS with the Cura account registered for this desk login. This overrides local master derivation for the session.

### pubkey

**Inside the CLI:**

```text
pubkey --seed-file /Users/mattye/Documents/workspace/Curated/keys/my-mm.desk.seed
```

Calculate the public API key locally without authenticating a session.

### status

**Inside the CLI:**

```text
status
```

Display current connection, account, scopes, linked chain owner and its source, and ignored-message count.

### now

**Inside the CLI:**

```text
now
```

Print Unix milliseconds for timestamp and schedule calculations.

### help

**Inside the CLI:**

```text
help place
```

Display the place command's options. Plain help lists all commands.

### ?

**Inside the CLI:**

```text
? chain-balances
```

The question-mark command is an alias for help.

### methods

**Inside the CLI:**

```text
methods
```

List all commands; equivalent to help without arguments.

### manual

**Inside the CLI:**

```text
manual
```

Open the native mm-desk(1) page through the system man viewer. With less, press Space to advance, type / followed by a term to search, and press q to return to the CLI. To see just one command's options, use ordinary command help:

```text
help login
help check-balance
place --help
```

The CLI uses the system pager configuration; it has no custom manual topics or navigation. Command examples remain in this separate EXAMPLES.md file.

### check-balance

**Inside the CLI:**

```text
check-balance OWNER_ADDRESS
```

Replace OWNER_ADDRESS with the address you want to query. This works at desk[offline]>, desk[login]>, and desk[ready]> without changing the connection or login. Before login, entering just check-balance in an interactive terminal prompts with Owner address:. After login, omit the address to query both the desk account and its linked chain owner:

```text
check-balance
check-balance --json
```

The first form shows separate desk and chain sections. If the desk has no balance records, its section shows:

```text
Desk balances | mm_balances | Account: 28
Empty (no desk balances).
```

Chain balances follow in their own section. The second returns an object containing mm_id, desk, and chain. If either source fails, the other result remains visible. An explicit query address never changes the owner linked to the login.

### chain-balances

**Inside the CLI:**

```text
chain-balances --owner OWNER_ADDRESS --limit 1000 --json
```

This is an alias for check-balance. Replace OWNER_ADDRESS with your target address. The positional form chain-balances OWNER_ADDRESS is also supported. Omit --json for a table. No desk login is required, and an existing connected or authenticated session is preserved.

### rpc

**Inside the CLI:**

```text
rpc mm_getOrder {"order_id":123}
```

Replace 123 with the required ID. JSON inside the CLI does not need an additional pair of shell quotes.

Additional raw JSON and complete-method-name forms:

```text
rpc mm_balances {}
rpc mm_getOrder {"order_id":"YOUR_STRING_ID"}
rpc mm_placeOrder {"book":"btc_usdt","side":"buy","price":"6000000","quantity":"10000","tif":"gtc","post_only":true}
mm_getOrder --order-id 123
```

For file input, create filters.json with a JSON parameter object, then call:

```text
rpc mm_listOrders @filters.json
```

### quit

**Inside the CLI:**

```text
quit
```

Exit and close the connection. The client does not automatically send cancellations.

### exit

**Inside the CLI:**

```text
exit
```

Alias for quit.

## Query balances while connected

If the desk is already connected, skip the connect line. Both addresses below are independent inputs; replace them with any two valid target addresses. The first query runs before login, and the second runs after login without changing that session.

**Inside the CLI:**

```text
connect wss://desk.testnet.curated.finance
check-balance FIRST_OWNER_ADDRESS
login --seed-file /Users/mattye/Documents/workspace/Curated/keys/my-mm.desk.seed
chain-balances SECOND_OWNER_ADDRESS --json
check-balance
status
```

The final check-balance uses the current login and displays both balance sources. You can continue submitting desk commands after either balance query. There is no need to disconnect or restart with --offline.

## Startup option examples

Run these in the **operating-system terminal**, from the application directory. Each row is an independent invocation.

| Option | Example | Purpose |
|---|---|---|
| `--help` / `-h` | `./mm-desk --help` | Show startup options |
| `--version` | `./mm-desk --version` | Show version without connecting |
| `--manual` | `./mm-desk --manual` | Open the native man page without connecting |
| `--url` | `./mm-desk --url wss://desk.testnet.curated.finance` | Select the desk endpoint |
| `--seed-file` | `./mm-desk --seed-file /Users/mattye/Documents/workspace/Curated/keys/my-mm.desk.seed` | Automatic desk-seed login |
| `--master-file` | `./mm-desk --master-file /Users/mattye/Documents/workspace/Curated/keys/my-mm.master.seed` | Automatic master-seed login |
| `--owner` | `./mm-desk --seed-file /Users/mattye/Documents/workspace/Curated/keys/my-mm.desk.seed --owner OWNER_ADDRESS -c check-balance` | Explicit owner for automatic login and combined query |
| `--timeout` | `./mm-desk --timeout 30` | Set the request timeout to 30 seconds |
| `--no-proxy` | `./mm-desk --no-proxy` | Disable automatic WebSocket proxy use |
| `--offline` | `./mm-desk --offline -c "check-balance OWNER_ADDRESS"` | Query chain balances without a desk connection |
| `--demo` | `./mm-desk --demo` | Run the local simulator |
| `--command` / `-c` | `./mm-desk --demo -c balances -c open-orders` | Execute commands in one session and exit |

## Session walkthrough

After starting and authenticating, the following commands inspect both balance sources and submit one order. Choose the order terms before submitting.

**Inside the CLI:**

```text
status
check-balance
open-orders --book btc_usdt
place --book btc_usdt --side buy --price 6000000 --quantity 10000 --tif gtc --post-only
```

Only if the order is accepted, replace 123 and 456 below with its order_id and pool_request_id. These are separate subsequent commands, not part of an automatically executed script.

```text
order --order-id 123
request --pool-request-id 456
modify --order-id 123 --price 6100000
cancel --order-id 123
order --order-id 123
```

For a non-interactive query sequence in the operating-system terminal:

```bash
./mm-desk --seed-file /Users/mattye/Documents/workspace/Curated/keys/my-mm.desk.seed -c balances -c "open-orders --book btc_usdt"
```

## Reading the documents and running local checks

**Operating-system terminal:**

```bash
./mm-desk --manual
man ./mm-desk.1
less EXAMPLES.md
.venv/bin/python -B -m unittest discover -s tests -v
```

The automated tests use local fixtures, not the account seed. For the historical UAT verification and current implementation limits, see the Manual and uat-verification.json.

## Maintaining asset names

Edit the adjacent assets.json file, then restart the CLI. Each address has one name and its decimal precision. A single entry has this shape; keep the other existing entries when editing:

```json
{
  "0xaca71f1748aa4c3291bbccd905b71224cb1b602f": {
    "name": "USDT",
    "decimals": 6
  }
}
```

A rejection referring to that asset is displayed as:

```json
{
  "code": "insufficient_balance",
  "detail": "needs 6000000 of USDT（0xaca71f1748aa4c3291bbccd905b71224cb1b602f）, available 0",
  "status": "rejected"
}
```

The same label appears in balance results and other output. Outgoing API requests continue to use the plain asset address.

## Maintaining public account associations

The adjacent accounts.json file stores public API keys and chain owner addresses per WebSocket endpoint. The existing testnet account is already configured. For a new account, add an entry with its real public API key and the owner registered with the desk operator:

```json
{
  "wss://desk.testnet.curated.finance": {
    "YOUR_64_HEX_PUBLIC_API_KEY": "YOUR_CURA_ACCOUNT_ADDRESS"
  }
}
```

The placeholders must be replaced. Preserve existing accounts when editing. Do not put any private key or seed in this file. Obtain the public API key with pubkey, then log in again after changing the mapping:

```text
pubkey --seed-file /path/to/account.desk.seed
login --seed-file /path/to/account.desk.seed
status
check-balance
```

The status result shows chain_owner and owner_source. The public mapping works with hidden-seed input and login-raw as well as file-based login. An explicit login --owner or matching master takes precedence over this mapping.

## Standalone Chain Operator

Run these in the operating-system terminal. They do not require desk login.

```bash
cd /Users/mattye/Documents/workspace/Curated/scripts
./chain-operator --help
./chain-operator balance 0xae20eb58791f0346adbcd974bb324534540b8c18
./chain-operator balance OWNER_ADDRESS --limit 100 --json
./chain-operator address --key-file ../keys/my-mm.chain.key
```

Replace OWNER_ADDRESS with the account to query. The following command simulates a transfer of 1 USDT to the given receiver. Replace RECEIVER_ADDRESS before running:

```bash
./chain-operator transfer --key-file ../keys/my-mm.chain.key --token USDT --amount 1 --to RECEIVER_ADDRESS --dry-run
```

To submit that transfer after checking the recipient and amount:

```bash
./chain-operator transfer --key-file ../keys/my-mm.chain.key --token USDT --amount 1 --to RECEIVER_ADDRESS --yes
```

Use `--raw-amount 1000000` instead of `--amount 1` for the same USDT amount. The mutually exclusive `--master-file ../keys/my-mm.master.seed` can replace `--key-file`. Both keys remain local.

Query a transaction whose hash was returned, including after a timeout:

```bash
./chain-operator receipt TRANSACTION_HASH --owner 0xae20eb58791f0346adbcd974bb324534540b8c18 --wait
./chain-operator --manual
```

Replace TRANSACTION_HASH with the actual hash. Receipt lookup never resubmits a transfer.

## transfer: chain account to desk

Inside a logged-in mm-desk session, preview a 1 USDT deposit:

```text
transfer to-desk --key-file /Users/mattye/Documents/workspace/Curated/keys/my-mm.chain.key --token USDT --amount 1 --dry-run
```

To submit a 1 USDT deposit after checking the preview:

```text
transfer to-desk --key-file /Users/mattye/Documents/workspace/Curated/keys/my-mm.chain.key --token USDT --amount 1 --yes
```

The chain key must match the current login's linked chain owner. The command submits deposit_pool, waits for finality, then looks for that exact transaction in deposits. To return after submission instead, add --no-wait. A credit timeout does not undo the deposit; inspect deposits and balances before considering another transfer.

## transfer: desk to chain account

Preview a 1 USDT withdrawal to the current login's chain owner:

```text
transfer from-desk --token USDT --amount 1 --dry-run
```

The preview includes has_scope. The API does not simulate withdrawal execution. To submit with a key that has withdraw scope:

```text
transfer from-desk --token USDT --amount 1 --yes
```

An explicit destination must match the owner linked to this login:

```text
transfer from-desk --token USDT --raw-amount 1000000 --to 0xae20eb58791f0346adbcd974bb324534540b8c18 --yes --no-wait
```

The high-level transfer command accepts normal token units through --amount. The low-level withdraw command continues to accept raw integer units through --amount.

Inspect status or help without resubmitting:

```text
deposits
withdrawals
check-balance
transfer to-desk --help
transfer from-desk --help
```

## Reading trading pair metadata

From the application directory, list all books or inspect a single book using jq:

```bash
jq '.markets | keys' markets.json
jq '.markets.btc_usdt' markets.json
```

The JSON is also directly readable from Python:

```python
import json
from pathlib import Path

root = Path('/Users/mattye/Documents/workspace/Curated/scripts/mm-desk-cli')
markets = json.loads((root / 'markets.json').read_text())['markets']
assets = json.loads((root / 'assets.json').read_text())
market = markets['btc_usdt']
print(market['pair_id'], market['price_scale'], market['quantity_step_raw'])
print(assets[market['base_asset']]['name'], assets[market['quote_asset']]['name'])
```
