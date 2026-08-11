# Reputation-Weighted Testimony Aggregator

A reusable [GenLayer](https://genlayer.com) Intelligent Contract primitive for decentralized fact-finding. It collects free-text "testimony" about a single real-world event from multiple, independent submitters; optionally corroborates each testimony against a public web source and/or an uploaded photo; and asks GenLayer validators to converge on **one** accepted narrative using an LLM equivalence check that is weighted by each submitter's on-chain reputation, not a flat majority vote.

Live on GenLayer Studio Network (chain id `61999`): [`0x695fC1D13c57ccB5E2c0D7b6C2111a823917430E`](https://genlayer-explorer.vercel.app/address/0x695fC1D13c57ccB5E2c0D7b6C2111a823917430E)

## Why this is a primitive, not a demo

Ordinary "AI decides X" contracts either:

1. Take a **strict-equality vote** across validators, which usually goes `UNDETERMINED` the moment the model's wording differs run to run, or
2. Take a **naive majority vote on raw text**, which rewards coordinated / sybil submitters equally with a lone accurate witness.

This contract instead:

- Uses `gl.eq_principle.prompt_comparative` with a deliberately loose principle, so validators only have to agree on *substance* (which testimony ids were accepted, the gist of the narrative) rather than exact wording or scores. That is what keeps consensus out of `UNDETERMINED` territory while still requiring real agreement on the outcome that pays out.
- Feeds each submitter's historical accuracy (`reputation_score`, a 0–10000 bps figure carried in contract storage **across events**) into the *same* nondet prompt that does the semantic clustering — reputation is load-bearing inside consensus, not a cosmetic score computed after the fact.
- Pulls in outside evidence: an optional public source URL (`gl.nondet.web.render`) and an optional uploaded photo (fetched then interpreted via `gl.nondet.exec_prompt(images=...)`, i.e. real vision), folding both into the same consensus prompt as independent corroborating signal.

## Use cases this primitive is meant to seed

- Peer-to-peer insurance claims ("what actually happened to the car")
- Dispute witness aggregation for marketplaces / freelance platforms
- Community fact-finding after a local incident, funded by whoever wants the truth established (a journalist, an insurer, a DAO)

## Contract design

### State

- `events: TreeMap[str, EventRecord]` — one row per testimony-collection round: description, an optional evidence-hint URL, escrow ledger (`reward_wei` / `reward_deposited`), the per-testimony bond amount, status, timing, and the resolved outcome once finalized.
- `testimonies: TreeMap[str, Testimony]` — one row per submission: text, optional evidence/image URLs, bond ledger, and (post-finalize) its consistency score, verdict, and reward paid.
- `disputes: TreeMap[str, Dispute]` — post-resolution objections (see "Disputes" below).
- `testimony_index` / `dispute_index: TreeMap[str, str]` — per-event id lists, stored as compound keys (`"{event_id}:{n}"` → id) rather than a `TreeMap[str, DynArray[...]]`. GenVM's `DynArray` cannot be constructed by user code (only auto-zero-inits as a top-level storage field), so this compound-key index is the reusable pattern for "a growable list of ids per parent record" under that constraint.
- `reputation_score` / `reputation_submissions` / `reputation_accepted: TreeMap[Address, u256]` — reputation persists globally, across every event this contract has ever run.
- `epoch_counter` + `last_heartbeat_epoch` — see "Virtual epoch clock" below.

### Escrow

Every function that accepts GEN is `@gl.public.write.payable` and reads only `gl.message.value` — never a caller-supplied amount parameter. Two separate ledgers are kept per event/testimony: the *terms* (`reward_wei`, `bond_wei`) versus the *actual custody* (`reward_deposited`, `bond_deposited`). Every payout path re-derives the amount from the deposited ledger field, **zeroes that field, persists state, and only then calls the single GEN-emission helper `_send_gen`**. This zero-then-transfer ordering removes any reentrancy/double-spend window; a second call against an already-settled event/testimony finds the ledger at zero and fails cleanly with `gl.vm.UserError` instead of re-paying.

Four exit paths for locked GEN:

1. **Success** — `finalize_event()` pays the accepted testimony's submitter(s) their share of the reward pool (weighted by `reputation × consistency`), refunds every submitter's anti-spam bond, and donates any bonds forfeited by sharply-inconsistent testimony to the accepted submitters.
2. **Cancel** — `cancel_event()` lets the creator pull the reward back, but only before any testimony has been submitted (once bonds/expectations exist, the event must run to finalize or timeout instead).
3. **Timeout** — `claim_timeout_refund()` is fully **permissionless**: if the creator never finalizes within the configured window, *anyone* can trigger a full unwind (reward back to the creator, every bond back to its submitter). This guarantees GEN can never be locked forever by a creator who disappears.
4. **Dispute** — `flag_dispute()` records an on-chain objection to a resolved event's outcome. It does **not** move funds (that would need a second consensus round and a slashing design out of scope for this primitive) — it's a documented extension point for a follow-up contract to watch and act on.

### Virtual epoch clock

GenVM contract code has no trusted wall-clock primitive available to Python. Rather than depend on an unconfirmed API, the contract runs its own free-running, permissionless counter: any address may call `heartbeat()`, which advances `epoch_counter` by one. Events measure their submission window and timeout window in epochs rather than seconds. This keeps the timeout/recovery exit fully on-chain and dependency-free, while guaranteeing the counter advances as long as *anyone* — including the party wanting their refund — calls `heartbeat()`.

### Consensus strictness

`finalize_event` gathers all of an event's testimonies, then inside a single nondet closure: fetches each testimony's optional web evidence (`gl.nondet.web.render`, text mode) and optional image evidence (`gl.nondet.web.get` for bytes, then `gl.nondet.exec_prompt(images=[...])` for a factual vision description) — a fetch failure degrades to "could not be retrieved" rather than raising, so a flaky URL never blocks consensus. It then asks the model to cluster testimonies into competing narratives, weigh them by *both* semantic/factual consistency and submitter reputation, and return a strict-JSON verdict (accepted narrative, rationale, per-testimony consistency scores 0–10000 bps).

That result is wrapped in `gl.eq_principle.prompt_comparative` with a principle that only requires the accept/reject boundary (consistency ≥ 4000 bps) to match between validators, and free-text fields to describe the same underlying account — not match byte-for-byte. This is what has kept every finalize call in testing at a clean `SUCCESS` / `FINALIZED`, never `UNDETERMINED`.

## Methods

| Method | Type | Notes |
|---|---|---|
| `create_event(description, evidence_hint_url, bond_wei, finalize_epochs, timeout_grace_epochs)` | payable write | Opens a round, funds the reward pool with `gl.message.value`. |
| `submit_testimony(event_id, text, evidence_url, image_url)` | payable write | `evidence_url`/`image_url` optional. Must attach exactly `bond_wei` GEN (or 0 if the event has no bond). |
| `heartbeat()` | write | Permissionlessly advances the virtual clock. |
| `finalize_event(event_id)` | write | Runs the reputation-weighted consensus, pays out escrow, updates reputations. Callable by anyone once unlocked. |
| `cancel_event(event_id)` | write | Creator-only, pre-testimony only. |
| `claim_timeout_refund(event_id)` | write | Permissionless, post-timeout only. |
| `flag_dispute(event_id, reason)` | write | Post-resolution only; records an objection, moves no funds. |
| `get_event`, `get_testimony`, `get_dispute`, `get_reputation`, `list_event_testimonies`, `list_event_disputes`, `get_current_epoch`, `contract_info` | views | All structured views return JSON-encoded strings (GenVM public-method schema generation does not support plain `dict` return types). |

**No admin-only methods.** Every write is either fully permissionless or gated per-event to that event's own creator (`cancel_event`). The `owner` field is recorded at deploy time but is never checked anywhere in the contract logic — there is nothing to configure or restrict from Studio.

## Known GenVM gotchas this contract works around

These were found empirically, on-chain, via a bisected minimal reproduction — not documented anywhere at the time of writing:

1. **`DynArray` cannot be constructed by user code**, even as a `TreeMap` value (`DynArray[str]()` raises `TypeError: this class can't be instantiated by user`). It only auto-zero-inits when declared as a top-level storage field. Worked around with the compound-key index pattern described above.
2. **A raw calldata string parameter passed directly into a freshly-constructed `@allow_storage` dataclass's `str` field can corrupt that instance's storage layout**, surfacing as `AttributeError: 'int' object has no attribute 'encode'` on an unrelated field of the *same* instance. Reproduced in isolation with a minimal contract; fixed by re-wrapping every `@gl.public.write` string parameter with `str(...)` before using it in a dataclass constructor call. Plain literals, values read back off already-stored dataclasses, and values derived from `self.<storage-field>` were all unaffected — only fresh calldata parameters passed straight into a *new* dataclass instance triggered it.
3. **`bool` was avoided as a storage field type** (`bond_settled` is a `u256` 0/1 flag instead) after early debugging suspected it as a contributing factor; not conclusively proven necessary once cause #2 was found and fixed, but kept as a defensive simplification.
4. Public method return types are restricted — `dict`/`list` are not supported by GenVM's schema generator; every structured getter here returns a JSON-encoded `str` instead.
5. `TreeMap` reads use an explicit `if key in tree_map: tree_map[key] else default` helper rather than assuming a Python-dict-style `.get(key, default)` is supported.
6. The correct EVM-interface decorator for emitting a native GEN transfer is `gl.evm.contract_interface` (not `gl.contract.interface`).

## Testing

Tested end-to-end against the live deployment on GenLayer Studio Network using `genlayer-js` (for value-carrying payable calls) and the `genlayer` CLI (for non-payable calls), across real accounts and real GEN.

**Positive paths:** event creation with real GEN escrow (×4, varying reward/bond/timing parameters); testimony submission from three independent accounts against one event, including one submission corroborated by a real fetched web page and one corroborated by a real fetched-and-vision-interpreted photo; `heartbeat`-driven epoch advancement; full `finalize_event` consensus — the model correctly favored a disinterested, confident witness over one that self-reported uncertainty and one with a direct financial interest in the outcome, paid the full reward to the accepted testimony's submitter, and moved reputations up/down proportionally to consistency; `cancel_event` on a pre-testimony event with a confirmed on-chain refund; `claim_timeout_refund` triggered by a completely unrelated third-party account on a stalled event, with confirmed refunds to both the creator and all bonded submitters; `flag_dispute` on a resolved event.

**Negative/validation paths, all correctly rejected with a clean `UserError` and no state corruption:** wrong bond amount attached, testimony text below the minimum length, unknown event id, finalizing before the unlock epoch, finalizing an already-resolved event twice, cancelling an already-cancelled event, cancelling as a non-creator, claiming a timeout refund twice, disputing a non-resolved event, finalizing with fewer than the minimum testimony count, and submitting testimony to an already-resolved event.

Every write across this test pass reached `ACCEPTED`/`FINALIZED` network consensus with no `UNDETERMINED` results.
