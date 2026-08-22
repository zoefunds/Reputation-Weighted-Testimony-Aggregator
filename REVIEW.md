# Review Remediation Record

## Review finding

> The validator check agrees only on the accepted/rejected boundary, while score differences it permits can still change the winning testimony, payout shares, reputation updates, and whether a bond is forfeited. Please make validators bind every settlement-driving value—such as by independently verifying exact scores or canonical outcome buckets—and deploy the matching corrected source before resubmitting.

## Resolution

Resolved in commit [`2e48f9e`](https://github.com/zoefunds/Reputation-Weighted-Testimony-Aggregator/commit/2e48f9e) (`Bind settlement scores and document live verification`).

The correction is a targeted contract change: **28 lines added and 20 lines removed** in `contract.py` (48 changed lines). It does not alter the contract's purpose or replace its settlement architecture. It changes the nondeterministic score output into a validator-bound, canonical settlement input.

## What changed

### 1. Canonical settlement buckets

`SETTLEMENT_SCORE_BUCKETS` is now defined as:

```python
(0, 2000, 4000, 6000, 8000, 10000)
```

The buckets deliberately include both economic thresholds used by the contract:

- `4000` bps is the acceptance threshold.
- Scores below `2000` bps forfeit the testimony bond.

This means every score used by settlement is one exact, finite, validator-comparable value.

### 2. Validator output is constrained

The nondeterministic adjudication prompt now requires one `settlement_bps` value per testimony and permits only the canonical bucket values. Intermediate scores are expressly prohibited.

```json
{
  "accepted_narrative": "string",
  "rationale": "string",
  "scores": [{"testimony_id": "string", "settlement_bps": 0}]
}
```

### 3. Validator comparison binds every economic value

`gl.eq_principle.prompt_comparative` now requires that each testimony's `settlement_bps` be the **exact same canonical integer** in both validator outputs. The principle explicitly identifies all affected outcomes: acceptance, winning-testimony selection, reward shares, reputation updates, and bond forfeiture.

The narrative and rationale may remain semantically equivalent rather than byte-identical because they are descriptive fields and do not move funds or change contract state used by settlement.

### 4. Noncanonical output is not transformed into settlement input

The prior clamping path was removed. Parsing now accepts a score only when it is a member of `SETTLEMENT_SCORE_BUCKETS`; otherwise it is ignored and the deterministic fallback for that testimony is zero. This prevents a model-provided, validator-unbound intermediate value from becoming a payout, reputation, or bond input.

## Settlement-value traceability

The only nondeterministic numeric value used in settlement is the parsed canonical bucket. The contract then uses that same value for:

| Settlement effect | Bound input |
|---|---|
| Accepted/rejected verdict | `settlement_bps >= 4000` |
| Winning testimony | highest canonical `settlement_bps` |
| Reward weight | reputation × canonical `settlement_bps` |
| Reputation delta | canonical `settlement_bps` |
| Bond forfeiture | canonical `settlement_bps < 2000` |

Therefore validators cannot agree only on a broad accept/reject boundary while disagreeing on a value that changes settlement.

## Code evidence

- Canonical bucket definition: [`contract.py`](contract.py#L24-L28)
- Constrained nondeterministic response format: [`contract.py`](contract.py#L495-L514)
- Exact per-testimony validator-comparison principle: [`contract.py`](contract.py#L520-L534)
- Rejection of noncanonical values: [`contract.py`](contract.py#L541-L554)
- Downstream settlement consumption of the bound value: [`contract.py`](contract.py#L563-L650)

## Corrected deployment

The source was redeployed before resubmission to GenLayer Studio Network (chain id `61999`):

- Contract: [`0xB801B1BC9797dbE65F35DCD07b2b6df302707fC9`](https://genlayer-explorer.vercel.app/address/0xB801B1BC9797dbE65F35DCD07b2b6df302707fC9)

The test harness at `harness/client.mjs` is configured to use this deployment.

## Live verification performed

Live GEN-funded verification was performed against the corrected deployment. Successful writes were checked for `ACCEPTED`, `MAJORITY_AGREE`, and GenVM `SUCCESS`.

| Area | Verification |
|---|---|
| Event creation | Created real funded events with a 1,000,000,000,000-wei reward. |
| Testimony submission | Submitted detailed testimony records. |
| Finalization | Resolved an event with two canonical `10000` bps accepted testimonies and a fully zeroed reward ledger. |
| Cancellation | Cancelled a funded event before testimony submission and verified refund state. |
| Timeout recovery | Advanced the virtual epoch and successfully used `claim_timeout_refund`. |
| Disputes | Recorded dispute entries against a resolved event. |
| Public views | Exercised `contract_info`, `get_current_epoch`, `get_reputation`, `get_event`, `get_testimony`, `list_event_testimonies`, `list_event_disputes`, and `get_dispute`. |
| Web rendering | Finalized an event with a public GitHub Explore evidence page. |
| Image fetch and vision | Finalized an event using the direct Python topic PNG at `raw.githubusercontent.com/github/explore/main/topics/python/python.png`; validator output described the fetched Python logo. |

## Conclusion

The review finding has been addressed at the source of the issue. Validators now bind every value that can affect settlement, the matching corrected source is deployed, and the corrected lifecycle—including external web and image evidence paths—has been verified live.
