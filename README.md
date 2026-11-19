# MM Desk and Chain Operator

Two independent command-line entry points sharing asset metadata and transaction code: `mm-desk` for all 23 desk API methods plus chain/desk transfers, and `chain-operator` for chain balances and account-to-account transfers. Chain Operator does not require desk login.

- [Manual](MANUAL.md): requirements, authentication, parameters, data conventions, and recovery behavior. Contains no command examples.
- [Examples](EXAMPLES.md): installation and launch commands, an example for every API and auxiliary command, and startup-option examples.

The default desk is `wss://desk.testnet.curated.finance`. The default chain RPC is `https://rpc.testnet.curated.finance`. After login, `check-balance` (alias `chain-balances`) without an address shows both desk and linked chain balances. An explicit address queries that chain owner in any connection state and preserves the session. Master-seed login derives the owner locally; desk-seed login checks a matching sibling `.master.seed`. Pasted-seed and raw login use the endpoint/public-key association in [accounts.json](accounts.json) when no owner is derived. The existing testnet account is configured. Alternatively, set `--owner` when logging in. Before login, interactive queries prompt for a missing address.

Maintain all asset names and decimals in [assets.json](assets.json). Known assets are displayed as `NAME（ADDRESS）` in balances, results, and errors. Restart after editing the registry.

Python 3.9 or later is required. Tested dependencies are pinned in `requirements.txt`. Zig and the system OpenSSL command are not required. Keep the application directory together when copying or installing it.

Use `--help` for startup options and `help COMMAND` inside the CLI for command options. `manual` and startup `--manual` open [mm-desk(1)](mm-desk.1) through the system man viewer and its configured pager. With less, Space advances, `/` searches, and `q` returns. A system `man` installation is required for these manual commands. The extended Markdown reference and separate examples remain in `MANUAL.md` and `EXAMPLES.md`.

Order placement, modification, and cancellation print `TTL (RTT)` in milliseconds to the terminal after receiving the desk response. Timings and public request/order IDs are appended as JSONL to `$XDG_STATE_HOME/mm-desk/latency.jsonl`, defaulting to `~/.local/state/mm-desk/latency.jsonl`. Use `--latency-log FILE` to choose another file. A timeout reports elapsed waiting time with an unavailable RTT. Timing excludes log writes and output formatting; see the manual for fields and command coverage.

The latest code verification passed 84 local tests. UAT login and queries were verified on November 18, 2026. Test orders were rejected, ultimately for insufficient desk balance, and no open order remained. Successful live modification/cancellation, withdrawals, and algorithm execution have not been verified. See `uat-verification.json` for historical results.

On November 19, 2026, a combined read-only query verified automatic owner resolution, an empty desk balance list, and 13 chain assets with names and full addresses.

`chain-operator` uses conventional subcommands: `balance`, `transfer`, `address`, and `receipt`. Its `--manual` opens `chain-operator(1)`. A launcher is also installed at `/Users/mattye/Documents/workspace/Curated/scripts/chain-operator`.

Inside mm-desk, `transfer to-desk` constructs a spot-contract deposit_pool transaction; `transfer from-desk` requests a withdrawal. Both default to preview and require `--yes` to submit. `--amount` uses normal token units and `--raw-amount` uses smallest units. The older `withdraw --amount` retains raw-unit semantics.

The launchers use the adjacent `.venv/bin/python` when present. `MM_DESK_PYTHON` and `CHAIN_OPERATOR_PYTHON` override it. Install the shared pinned requirements into a virtual environment. There is one `assets.json` for both programs; `desk_networks.json` holds environment-specific pool addresses.

Transfer verification used local tests and testnet simulations only. Current account 28 has trade/read scopes, so actual desk withdrawals require withdraw scope from the desk operator.

[markets.json](markets.json) contains all 12 spot books listed by the project testnet reference: pair IDs, base/quote asset references, price scales, quantity steps, minimum notionals, fees, channels, and snapshot URLs. It records the source and retrieval date; asset names and precision resolve through assets.json.
