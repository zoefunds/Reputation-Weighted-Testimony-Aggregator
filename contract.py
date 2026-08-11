# v0.2.16
# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }

import json
from dataclasses import dataclass
from genlayer import *
import genlayer.gl as gl

# --------------------------------------------------------------------------
# Tunable constants
# --------------------------------------------------------------------------

BPS_DENOMINATOR = u256(10000)          # basis-points scale used everywhere
DEFAULT_REPUTATION_BPS = u256(5000)    # new submitters start "neutral"
MIN_REPUTATION_BPS = u256(500)         # reputation floor (never fully zero,
                                        # so a bad run can still recover)
MAX_REPUTATION_BPS = u256(10000)
REPUTATION_STEP_UP = u256(400)         # gained per correctly-accepted testimony
REPUTATION_STEP_DOWN = u256(600)       # lost per rejected/low-consistency testimony
ACCEPTANCE_THRESHOLD_BPS = u256(4000)  # consistency score needed to be "accepted"
BOND_FORFEIT_THRESHOLD_BPS = u256(2000)  # below this, bond is forfeited to pool
MIN_TESTIMONIES_TO_FINALIZE = 2
MIN_FINALIZE_EPOCHS = 1                # creator must allow >= 1 epoch for submissions
DEFAULT_TIMEOUT_GRACE_EPOCHS = 10      # epochs past unlock before anyone can force-refund

STATUS_OPEN = "OPEN"
STATUS_RESOLVED = "RESOLVED"
STATUS_CANCELLED = "CANCELLED"
STATUS_REFUNDED_TIMEOUT = "REFUNDED_TIMEOUT"

VERDICT_ACCEPTED = "ACCEPTED"
VERDICT_REJECTED = "REJECTED"


# --------------------------------------------------------------------------
# GEN transfer plumbing (single emission choke point, per escrow best
# practice: every payout in this contract funnels through _send_gen, so
# every place money can leave the contract is grep-able in one place).
# --------------------------------------------------------------------------

@gl.evm.contract_interface
class _Recipient:
    class View:
        pass

    class Write:
        pass


def _send_gen(to_address: Address, amount: u256) -> None:
    if to_address == Address("0x0000000000000000000000000000000000000000"):
        raise gl.vm.UserError("Missing recipient address")
    if amount <= u256(0):
        raise gl.vm.UserError("Transfer amount must be positive")
    _Recipient(to_address).emit_transfer(value=amount)


def _clamp_bps(value: int) -> u256:
    if value < 0:
        return u256(0)
    if value > int(MAX_REPUTATION_BPS):
        return MAX_REPUTATION_BPS
    return u256(value)


def _tm_get(tree_map, key, default):
    # TreeMap storage is not guaranteed to support dict-style .get(key,
    # default) the way a plain Python dict does, so every read that needs
    # a fallback goes through this explicit membership check instead.
    if key in tree_map:
        return tree_map[key]
    return default


def _list_ids(index_map, event_id: str, count: u256) -> list:
    # Rebuilds an event's id list from the compound-key index
    # ("{event_id}:{n}" -> id) instead of a stored DynArray, since GenVM's
    # DynArray cannot be constructed by user code (see storage field
    # comment on testimony_index/dispute_index).
    out = []
    for i in range(int(count)):
        key = f"{event_id}:{i}"
        if key in index_map:
            out.append(index_map[key])
    return out


# --------------------------------------------------------------------------
# Storage records
# --------------------------------------------------------------------------

@allow_storage
@dataclass
class EventRecord:
    event_id: str
    creator: Address
    description: str
    evidence_hint_url: str          # optional public source the creator points at
    status: str

    reward_wei: u256                # agreed terms
    reward_deposited: u256          # actual custody ledger (zeroed on payout)
    bond_wei: u256                  # required anti-spam bond per testimony

    testimony_count: u256
    created_epoch: u256
    finalize_unlock_epoch: u256     # earliest epoch finalize_event() is allowed
    timeout_epoch: u256             # epoch at/after which anyone may force-refund

    accepted_testimony_id: str
    accepted_narrative: str
    finalize_rationale: str

    dispute_count: u256


@allow_storage
@dataclass
class Testimony:
    testimony_id: str
    event_id: str
    submitter: Address
    text: str
    evidence_url: str
    image_url: str
    submitted_epoch: u256
    bond_wei: u256
    bond_deposited: u256
    bond_settled: u256
    consistency_bps: u256
    verdict: str
    reward_paid: u256


@allow_storage
@dataclass
class Dispute:
    dispute_id: str
    event_id: str
    disputant: Address
    reason: str
    filed_epoch: u256


# --------------------------------------------------------------------------
# Contract
# --------------------------------------------------------------------------

class ReputationTestimonyAggregator(gl.Contract):
    # -- global bookkeeping -------------------------------------------------
    owner: Address
    epoch_counter: u256
    next_event_seq: u256
    next_testimony_seq: u256
    next_dispute_seq: u256

    # -- per-event / per-testimony storage ----------------------------------
    events: TreeMap[str, EventRecord]
    testimonies: TreeMap[str, Testimony]
    disputes: TreeMap[str, Dispute]

    # GenVM's DynArray cannot be constructed by user code (only auto
    # zero-inits as a top-level storage field), so per-event id lists are
    # NOT stored as TreeMap[str, DynArray[str]]. Instead each event's Nth
    # testimony/dispute id is written under the compound key
    # "{event_id}:{n}" (n = event.testimony_count / event.dispute_count
    # at the moment of insertion), and readers rebuild the list by
    # looping 0..count using that key -- no array construction needed.
    testimony_index: TreeMap[str, str]
    dispute_index: TreeMap[str, str]

    # -- reputation state (persists across ALL events, this is the point) ---
    reputation_score: TreeMap[Address, u256]     # bps, 0..10000
    reputation_submissions: TreeMap[Address, u256]
    reputation_accepted: TreeMap[Address, u256]

    # -- heartbeat anti-spam: last epoch each address bumped the clock ------
    last_heartbeat_epoch: TreeMap[Address, u256]

    def __init__(self):
        self.owner = gl.message.sender_address
        self.epoch_counter = u256(0)
        self.next_event_seq = u256(0)
        self.next_testimony_seq = u256(0)
        self.next_dispute_seq = u256(0)

    # ------------------------------------------------------------------
    # Virtual epoch clock
    # ------------------------------------------------------------------

    @gl.public.write
    def heartbeat(self) -> u256:
        """Permissionlessly advance the contract's virtual clock by one
        epoch. Anyone may call this; it costs the caller only gas. Events
        measure their submission window and timeout window in epochs
        rather than wall-clock time because GenVM contract code does not
        expose a trusted timestamp primitive. Repeated calls in the SAME
        epoch by the same caller are cheap no-ops that still return the
        current epoch, so callers cannot "double count" their own bumps,
        but distinct callers each advance the shared counter."""
        caller = gl.message.sender_address
        last = _tm_get(self.last_heartbeat_epoch, caller, u256(0))
        if last < self.epoch_counter:
            self.last_heartbeat_epoch[caller] = self.epoch_counter
            return self.epoch_counter
        self.epoch_counter = self.epoch_counter + u256(1)
        self.last_heartbeat_epoch[caller] = self.epoch_counter
        return self.epoch_counter

    @gl.public.view
    def get_current_epoch(self) -> int:
        return int(self.epoch_counter)

    # ------------------------------------------------------------------
    # Reputation helpers
    # ------------------------------------------------------------------

    def _get_reputation(self, addr: Address) -> u256:
        return _tm_get(self.reputation_score, addr, DEFAULT_REPUTATION_BPS)

    @gl.public.view
    def get_reputation(self, address: str) -> str:
        # Returns a JSON-encoded string rather than a plain dict: GenVM
        # schema generation for @gl.public.view methods only supports a
        # small set of return types (primitives, storage generics), so
        # every "structured" view in this contract serializes to JSON
        # text instead of returning dict/list directly.
        addr = Address(address)
        return json.dumps({
            "address": address,
            "reputation_bps": int(self._get_reputation(addr)),
            "submissions": int(_tm_get(self.reputation_submissions, addr, u256(0))),
            "accepted": int(_tm_get(self.reputation_accepted, addr, u256(0))),
        })

    def _apply_reputation_delta(self, addr: Address, accepted: bool, consistency_bps: u256) -> None:
        current = int(self._get_reputation(addr))
        if accepted:
            # scale the step by how strong the match was, so a borderline
            # accept moves reputation less than a near-perfect one.
            strength = int(consistency_bps) / int(BPS_DENOMINATOR)
            delta = int(int(REPUTATION_STEP_UP) * strength)
            new_score = current + delta
        else:
            weakness = 1.0 - (int(consistency_bps) / int(BPS_DENOMINATOR))
            delta = int(int(REPUTATION_STEP_DOWN) * weakness)
            new_score = current - delta
        new_score = max(int(MIN_REPUTATION_BPS), min(int(MAX_REPUTATION_BPS), new_score))
        self.reputation_score[addr] = u256(new_score)

    # ------------------------------------------------------------------
    # Event lifecycle: create / submit / finalize / cancel / timeout
    # ------------------------------------------------------------------

    @gl.public.write.payable
    def create_event(
        self,
        description: str,
        evidence_hint_url: str,
        bond_wei: int,
        finalize_epochs: int,
        timeout_grace_epochs: int,
    ) -> str:
        """Open a new testimony-collection round, funding the reward pool
        with whatever GEN is attached to this call. `bond_wei` is the
        exact anti-spam bond every submitter must lock when they testify
        (0 disables the bond requirement). `finalize_epochs` is how many
        virtual epochs must pass (via `heartbeat()`) before the creator
        may call `finalize_event`; `timeout_grace_epochs` is how many
        FURTHER epochs past that before anyone may force-refund the whole
        event if the creator never finalizes."""
        if not description or len(description) < 8:
            raise gl.vm.UserError("Description must be a meaningful, non-empty string")
        if gl.message.value <= u256(0):
            raise gl.vm.UserError("Event must be funded with a positive GEN reward")
        if bond_wei < 0:
            raise gl.vm.UserError("bond_wei cannot be negative")
        if finalize_epochs < MIN_FINALIZE_EPOCHS:
            raise gl.vm.UserError(f"finalize_epochs must be >= {MIN_FINALIZE_EPOCHS}")
        if timeout_grace_epochs < 1:
            raise gl.vm.UserError("timeout_grace_epochs must be >= 1")

        event_id = f"evt-{int(self.next_event_seq)}"
        self.next_event_seq = self.next_event_seq + u256(1)

        created_epoch = self.epoch_counter
        finalize_unlock = created_epoch + u256(finalize_epochs)
        timeout_epoch = finalize_unlock + u256(timeout_grace_epochs)

        self.events[event_id] = EventRecord(
            event_id=event_id,
            creator=gl.message.sender_address,
            # str(...) re-wraps every calldata string parameter before it
            # is stored in a freshly-constructed @allow_storage dataclass.
            # Empirically confirmed (via a minimal on-chain repro) that
            # passing a @gl.public.write parameter's string value straight
            # through into a new dataclass's str field can corrupt the
            # storage layout of that instance's OTHER fields (observed as
            # "AttributeError: 'int' object has no attribute 'encode'" on
            # an unrelated field) -- str()-copying it first avoids
            # whatever calldata-string-proxy quirk causes that.
            description=str(description),
            evidence_hint_url=str(evidence_hint_url),
            status=STATUS_OPEN,
            reward_wei=gl.message.value,
            reward_deposited=gl.message.value,
            bond_wei=u256(bond_wei),
            testimony_count=u256(0),
            created_epoch=created_epoch,
            finalize_unlock_epoch=finalize_unlock,
            timeout_epoch=timeout_epoch,
            accepted_testimony_id="",
            accepted_narrative="",
            finalize_rationale="",
            dispute_count=u256(0),
        )
        return event_id

    @gl.public.write.payable
    def submit_testimony(
        self,
        event_id: str,
        text: str,
        evidence_url: str,
        image_url: str,
    ) -> str:
        """Submit a free-text testimony about the event. If the event was
        created with a non-zero bond, the caller must attach EXACTLY
        `bond_wei` GEN -- not "at least", an exact match, per escrow best
        practice (never trust "close enough" on money). `evidence_url` and
        `image_url` are both optional; when present they are fetched and
        interpreted (text render / vision) during `finalize_event` as
        independent corroborating signal for this testimony."""
        event = _tm_get(self.events, event_id, None)
        if event is None:
            raise gl.vm.UserError("Unknown event_id")
        if event.status != STATUS_OPEN:
            raise gl.vm.UserError("Event is not open for testimony")
        if not text or len(text) < 20:
            raise gl.vm.UserError("Testimony text must be a meaningful, non-empty string (>=20 chars)")
        if len(text) > 4000:
            raise gl.vm.UserError("Testimony text too long (max 4000 chars)")

        if event.bond_wei > u256(0):
            if gl.message.value != event.bond_wei:
                raise gl.vm.UserError("Must attach exactly bond_wei GEN")
        else:
            if gl.message.value != u256(0):
                raise gl.vm.UserError("This event does not require a bond; do not attach GEN")

        submitter = gl.message.sender_address
        testimony_id = f"tst-{int(self.next_testimony_seq)}"
        self.next_testimony_seq = self.next_testimony_seq + u256(1)

        self.testimonies[testimony_id] = Testimony(
            testimony_id=testimony_id,
            event_id=str(event_id),
            submitter=submitter,
            text=str(text),
            evidence_url=str(evidence_url),
            image_url=str(image_url),
            submitted_epoch=self.epoch_counter,
            bond_wei=event.bond_wei,
            bond_deposited=gl.message.value,
            bond_settled=u256(0),
            consistency_bps=u256(0),
            verdict="",
            reward_paid=u256(0),
        )

        index_key = f"{event_id}:{int(event.testimony_count)}"
        self.testimony_index[index_key] = testimony_id

        event.testimony_count = event.testimony_count + u256(1)
        self.events[event_id] = event

        subs = _tm_get(self.reputation_submissions, submitter, u256(0))
        self.reputation_submissions[submitter] = subs + u256(1)
        if submitter not in self.reputation_score:
            self.reputation_score[submitter] = DEFAULT_REPUTATION_BPS

        return testimony_id

    # ------------------------------------------------------------------
    # Finalization: the reputation-weighted, evidence-corroborated,
    # non-deterministic consensus step.
    # ------------------------------------------------------------------

    @gl.public.write
    def finalize_event(self, event_id: str) -> str:
        """Run GenLayer consensus over every testimony submitted to this
        event and settle escrow. Requires `finalize_unlock_epoch` to have
        passed (advance it with `heartbeat()`) and at least
        MIN_TESTIMONIES_TO_FINALIZE submissions. Any address may trigger
        finalization once it is unlocked -- this is intentional: nothing
        about the outcome depends on WHO calls it, only on the testimonies
        and evidence already committed on-chain, so permissioning this to
        "creator only" would just be an availability foot-gun."""
        event = _tm_get(self.events, event_id, None)
        if event is None:
            raise gl.vm.UserError("Unknown event_id")
        if event.status != STATUS_OPEN:
            raise gl.vm.UserError("Event already settled")
        if self.epoch_counter < event.finalize_unlock_epoch:
            raise gl.vm.UserError("Too early to finalize; call heartbeat() to advance the clock")

        testimony_ids = _list_ids(self.testimony_index, event_id, event.testimony_count)
        if len(testimony_ids) < MIN_TESTIMONIES_TO_FINALIZE:
            raise gl.vm.UserError(
                f"Need at least {MIN_TESTIMONIES_TO_FINALIZE} testimonies to finalize; "
                f"if the deadline has passed with too few, use claim_timeout_refund instead"
            )

        # Build a plain-data snapshot (str/int/bool only) so it can safely
        # cross into the nondet closure and be JSON-serialized for the
        # prompt. Reputation is read HERE, outside the nondet block, so
        # every validator consensus-checks against the exact same
        # reputation snapshot rather than each re-reading possibly-stale
        # storage independently.
        snapshot = []
        for tid in testimony_ids:
            t = self.testimonies[tid]
            snapshot.append({
                "testimony_id": t.testimony_id,
                "submitter": str(t.submitter),
                "text": t.text,
                "evidence_url": t.evidence_url,
                "image_url": t.image_url,
                "submitter_reputation_bps": int(self._get_reputation(t.submitter)),
            })

        event_description = event.description
        evidence_hint_url = event.evidence_hint_url

        def _resolve_testimony() -> str:
            # Each validator independently fetches whatever web/image
            # evidence is referenced, then asks the model to cluster and
            # judge. Fetch failures are swallowed into a placeholder
            # string rather than raised, so a flaky URL degrades the
            # signal instead of blocking the whole nondet call.
            enriched = []
            for item in snapshot:
                corroboration_text = ""
                if item["evidence_url"]:
                    try:
                        rendered = gl.nondet.web.render(item["evidence_url"], mode="text")
                        corroboration_text = (rendered or "")[:3000]
                    except Exception:
                        corroboration_text = "(evidence URL could not be retrieved)"

                image_note = ""
                if item["image_url"]:
                    try:
                        resp = gl.nondet.web.get(item["image_url"])
                        image_bytes = resp.body
                        image_note = gl.nondet.exec_prompt(
                            "Describe factually what is visible in this image in 2-3 "
                            "sentences. Focus on details relevant to verifying a "
                            "real-world claim (damage, location, people, objects, "
                            "timestamps/signage if visible). Do not speculate beyond "
                            "what is visibly present.",
                            images=[image_bytes],
                        )
                    except Exception:
                        image_note = "(image URL could not be retrieved or interpreted)"

                enriched.append({
                    "testimony_id": item["testimony_id"],
                    "submitter": item["submitter"],
                    "submitter_reputation_bps": item["submitter_reputation_bps"],
                    "text": item["text"],
                    "web_corroboration": corroboration_text,
                    "image_interpretation": image_note,
                })

            prompt = (
                "You are adjudicating conflicting eyewitness testimony about a "
                "single real-world event, as a neutral fact-finder for an "
                "on-chain reputation-weighted testimony aggregator.\n\n"
                f"EVENT DESCRIPTION:\n{event_description}\n\n"
                f"OPTIONAL BACKGROUND SOURCE (creator-supplied hint, may be "
                f"empty or unrelated, treat with caution): {evidence_hint_url}\n\n"
                "TESTIMONIES (JSON array, each with the submitter's prior "
                "reputation in basis points 0-10000, where 10000 = perfect "
                "track record and 5000 = neutral/new submitter):\n"
                f"{json.dumps(enriched)}\n\n"
                "TASK:\n"
                "1. Cluster the testimonies into competing narratives about "
                "what actually happened.\n"
                "2. Identify the single most credible narrative. Weigh BOTH "
                "the semantic/factual consistency across testimonies AND each "
                "submitter's reputation -- a testimony from a high-reputation "
                "submitter that is well corroborated by web/image evidence "
                "should be able to outweigh a larger but low-reputation, "
                "poorly-corroborated cluster. Do not simply pick the largest "
                "cluster by raw count.\n"
                "3. For EVERY testimony_id, output a consistency_bps integer "
                "0-10000 measuring how well that specific testimony matches "
                "the narrative you selected as most credible (10000 = fully "
                "consistent, 0 = directly contradicts it).\n"
                "4. Write a neutral 2-4 sentence accepted_narrative summarizing "
                "what most likely happened, and a short rationale explaining "
                "why this narrative was chosen over the alternatives "
                "(mention reputation/evidence factors used).\n"
                "5. If web_corroboration or image_interpretation for an item "
                "says it could not be retrieved, judge that testimony on its "
                "text and reputation alone -- do not penalize it for a fetch "
                "failure.\n\n"
                "Respond ONLY as strict JSON with this exact shape:\n"
                "{\n"
                '  "accepted_narrative": "string",\n'
                '  "rationale": "string",\n'
                '  "scores": [{"testimony_id": "string", "consistency_bps": 0}]\n'
                "}"
            )

            result = gl.nondet.exec_prompt(prompt, response_format="json")
            return json.dumps(result)

        raw_result = gl.eq_principle.prompt_comparative(
            _resolve_testimony,
            principle=(
                "The JSON must contain the same set of testimony_id keys in "
                "'scores' as the input. For each testimony_id, whether "
                "consistency_bps is >= 4000 or < 4000 must match between "
                "outputs (i.e. the accept/reject boundary must agree), but the "
                "exact numeric value may differ by up to 1000. "
                "'accepted_narrative' and 'rationale' may be phrased "
                "differently as long as they describe the same underlying "
                "account of the event."
            ),
        )

        parsed = json.loads(raw_result)
        accepted_narrative = str(parsed.get("accepted_narrative", ""))
        rationale = str(parsed.get("rationale", ""))
        scores = parsed.get("scores", [])

        score_by_id = {}
        for row in scores:
            try:
                tid = str(row["testimony_id"])
                bps = _clamp_bps(int(row["consistency_bps"]))
                score_by_id[tid] = bps
            except (KeyError, TypeError, ValueError):
                continue

        # -------- settle reputations, mark verdicts, compute weights ------
        accepted_ids = []
        weights = {}
        total_weight = 0
        best_id = ""
        best_bps = -1

        for tid in testimony_ids:
            t = self.testimonies[tid]
            bps = score_by_id.get(tid, u256(0))
            accepted = bps >= ACCEPTANCE_THRESHOLD_BPS
            t.consistency_bps = bps
            t.verdict = VERDICT_ACCEPTED if accepted else VERDICT_REJECTED
            self.testimonies[tid] = t

            self._apply_reputation_delta(t.submitter, accepted, bps)
            if accepted:
                acc = _tm_get(self.reputation_accepted, t.submitter, u256(0))
                self.reputation_accepted[t.submitter] = acc + u256(1)

                rep = int(self._get_reputation(t.submitter))
                weight = rep * int(bps)
                weights[tid] = weight
                total_weight += weight
                accepted_ids.append(tid)

            if int(bps) > best_bps:
                best_bps = int(bps)
                best_id = tid

        # -------- escrow settlement: zero ledger BEFORE any transfer ------
        reward_pool = event.reward_deposited
        event.reward_deposited = u256(0)
        event.status = STATUS_RESOLVED
        event.accepted_testimony_id = best_id
        event.accepted_narrative = accepted_narrative
        event.finalize_rationale = rationale
        self.events[event_id] = event

        if total_weight > 0 and reward_pool > u256(0):
            paid_so_far = u256(0)
            accepted_ids_sorted = accepted_ids
            for i, tid in enumerate(accepted_ids_sorted):
                t = self.testimonies[tid]
                if i == len(accepted_ids_sorted) - 1:
                    # last recipient gets the remainder, avoids dust loss
                    # from integer-division rounding.
                    share = reward_pool - paid_so_far
                else:
                    share = (reward_pool * u256(weights[tid])) // u256(total_weight)
                paid_so_far = paid_so_far + share
                t.reward_paid = share
                self.testimonies[tid] = t
                if share > u256(0):
                    _send_gen(t.submitter, share)
        elif reward_pool > u256(0):
            # nobody was accepted -- return the untouched reward to the
            # creator rather than let it sit unclaimable.
            _send_gen(event.creator, reward_pool)

        # -------- bonds: refund accepted, partially forfeit low scorers ---
        forfeited_total = u256(0)
        for tid in testimony_ids:
            t = self.testimonies[tid]
            if t.bond_settled != u256(0) or t.bond_deposited <= u256(0):
                continue
            bond = t.bond_deposited
            t.bond_deposited = u256(0)
            t.bond_settled = u256(1)
            self.testimonies[tid] = t

            if t.consistency_bps < BOND_FORFEIT_THRESHOLD_BPS:
                # sharply inconsistent testimony forfeits its bond into a
                # pool that is donated back to the ACCEPTED submitters,
                # split evenly, rewarding accuracy without being punitive
                # enough to make honest-but-imperfect recall unbondable.
                forfeited_total = forfeited_total + bond
            else:
                _send_gen(t.submitter, bond)

        if forfeited_total > u256(0) and len(accepted_ids) > 0:
            per_head = forfeited_total // u256(len(accepted_ids))
            remainder = forfeited_total - (per_head * u256(len(accepted_ids)))
            for i, tid in enumerate(accepted_ids):
                t = self.testimonies[tid]
                amt = per_head + (remainder if i == 0 else u256(0))
                if amt > u256(0):
                    _send_gen(t.submitter, amt)
        elif forfeited_total > u256(0):
            # nobody accepted to donate to -- return forfeits to creator
            # rather than strand them.
            _send_gen(event.creator, forfeited_total)

        return json.dumps({
            "event_id": event_id,
            "accepted_testimony_id": best_id,
            "accepted_narrative": accepted_narrative,
            "rationale": rationale,
            "accepted_count": len(accepted_ids),
            "total_testimonies": len(testimony_ids),
        })

    # ------------------------------------------------------------------
    # Escrow recovery paths
    # ------------------------------------------------------------------

    @gl.public.write
    def cancel_event(self, event_id: str) -> None:
        """Creator-only early exit. Only allowed before any testimony has
        been submitted (once testimony/bonds exist, participants have
        already committed funds and reputation expectations, so the event
        must run to finalize() or timeout() instead)."""
        event = _tm_get(self.events, event_id, None)
        if event is None:
            raise gl.vm.UserError("Unknown event_id")
        if gl.message.sender_address != event.creator:
            raise gl.vm.UserError("Only the creator may cancel")
        if event.status != STATUS_OPEN:
            raise gl.vm.UserError("Event is not open")
        if event.testimony_count > u256(0):
            raise gl.vm.UserError("Cannot cancel after testimony has been submitted; use timeout path instead")

        refund = event.reward_deposited
        if refund <= u256(0):
            raise gl.vm.UserError("Nothing to refund")

        event.reward_deposited = u256(0)
        event.status = STATUS_CANCELLED
        self.events[event_id] = event

        _send_gen(event.creator, refund)

    @gl.public.write
    def claim_timeout_refund(self, event_id: str) -> None:
        """Permissionless recovery path: if the event's `timeout_epoch` has
        passed (advance the clock with `heartbeat()`) and it was never
        finalized, ANY address may trigger a full unwind -- the reward
        pool returns to the creator and every submitter gets their bond
        back. This guarantees no GEN can be locked forever just because
        the creator disappears or refuses to finalize."""
        event = _tm_get(self.events, event_id, None)
        if event is None:
            raise gl.vm.UserError("Unknown event_id")
        if event.status != STATUS_OPEN:
            raise gl.vm.UserError("Event already settled")
        if self.epoch_counter < event.timeout_epoch:
            raise gl.vm.UserError("Timeout window has not been reached yet; call heartbeat() to advance the clock")

        refund = event.reward_deposited
        event.reward_deposited = u256(0)
        event.status = STATUS_REFUNDED_TIMEOUT
        self.events[event_id] = event

        if refund > u256(0):
            _send_gen(event.creator, refund)

        testimony_ids = _list_ids(self.testimony_index, event_id, event.testimony_count)
        for tid in testimony_ids:
            t = self.testimonies[tid]
            if t.bond_settled != u256(0) or t.bond_deposited <= u256(0):
                continue
            bond = t.bond_deposited
            t.bond_deposited = u256(0)
            t.bond_settled = u256(1)
            self.testimonies[tid] = t
            _send_gen(t.submitter, bond)

    # ------------------------------------------------------------------
    # Lightweight dispute registry (extension point, moves no funds)
    # ------------------------------------------------------------------

    @gl.public.write
    def flag_dispute(self, event_id: str, reason: str) -> str:
        """Record an on-chain objection to a RESOLVED event's outcome.
        This intentionally does not move funds or reopen consensus -- a
        second-order contract (or a future version of this one) can watch
        `dispute_count` / `list_disputes` and decide whether to trigger a
        re-finalization, arbitration, or slashing flow. Keeping this
        primitive's own scope to "record the objection" avoids bolting an
        under-specified slashing mechanism onto the core aggregator."""
        event = _tm_get(self.events, event_id, None)
        if event is None:
            raise gl.vm.UserError("Unknown event_id")
        if event.status != STATUS_RESOLVED:
            raise gl.vm.UserError("Can only dispute a resolved event")
        if not reason or len(reason) < 10:
            raise gl.vm.UserError("Reason must be a meaningful, non-empty string (>=10 chars)")

        dispute_id = f"dsp-{int(self.next_dispute_seq)}"
        self.next_dispute_seq = self.next_dispute_seq + u256(1)

        self.disputes[dispute_id] = Dispute(
            dispute_id=dispute_id,
            event_id=str(event_id),
            disputant=gl.message.sender_address,
            reason=str(reason),
            filed_epoch=self.epoch_counter,
        )
        index_key = f"{event_id}:{int(event.dispute_count)}"
        self.dispute_index[index_key] = dispute_id

        event.dispute_count = event.dispute_count + u256(1)
        self.events[event_id] = event
        return dispute_id

    # ------------------------------------------------------------------
    # Views
    # ------------------------------------------------------------------

    @gl.public.view
    def get_event(self, event_id: str) -> str:
        event = _tm_get(self.events, event_id, None)
        if event is None:
            raise gl.vm.UserError("Unknown event_id")
        return json.dumps({
            "event_id": event.event_id,
            "creator": str(event.creator),
            "description": event.description,
            "evidence_hint_url": event.evidence_hint_url,
            "status": event.status,
            "reward_wei": int(event.reward_wei),
            "reward_deposited": int(event.reward_deposited),
            "bond_wei": int(event.bond_wei),
            "testimony_count": int(event.testimony_count),
            "created_epoch": int(event.created_epoch),
            "finalize_unlock_epoch": int(event.finalize_unlock_epoch),
            "timeout_epoch": int(event.timeout_epoch),
            "accepted_testimony_id": event.accepted_testimony_id,
            "accepted_narrative": event.accepted_narrative,
            "finalize_rationale": event.finalize_rationale,
            "dispute_count": int(event.dispute_count),
        })

    @gl.public.view
    def get_testimony(self, testimony_id: str) -> str:
        t = _tm_get(self.testimonies, testimony_id, None)
        if t is None:
            raise gl.vm.UserError("Unknown testimony_id")
        return json.dumps({
            "testimony_id": t.testimony_id,
            "event_id": t.event_id,
            "submitter": str(t.submitter),
            "text": t.text,
            "evidence_url": t.evidence_url,
            "image_url": t.image_url,
            "submitted_epoch": int(t.submitted_epoch),
            "bond_wei": int(t.bond_wei),
            "bond_deposited": int(t.bond_deposited),
            "bond_settled": bool(int(t.bond_settled)),
            "consistency_bps": int(t.consistency_bps),
            "verdict": t.verdict,
            "reward_paid": int(t.reward_paid),
        })

    @gl.public.view
    def list_event_testimonies(self, event_id: str) -> str:
        event = _tm_get(self.events, event_id, None)
        if event is None:
            raise gl.vm.UserError("Unknown event_id")
        return json.dumps(_list_ids(self.testimony_index, event_id, event.testimony_count))

    @gl.public.view
    def list_event_disputes(self, event_id: str) -> str:
        event = _tm_get(self.events, event_id, None)
        if event is None:
            raise gl.vm.UserError("Unknown event_id")
        return json.dumps(_list_ids(self.dispute_index, event_id, event.dispute_count))

    @gl.public.view
    def get_dispute(self, dispute_id: str) -> str:
        d = _tm_get(self.disputes, dispute_id, None)
        if d is None:
            raise gl.vm.UserError("Unknown dispute_id")
        return json.dumps({
            "dispute_id": d.dispute_id,
            "event_id": d.event_id,
            "disputant": str(d.disputant),
            "reason": d.reason,
            "filed_epoch": int(d.filed_epoch),
        })

    @gl.public.view
    def contract_info(self) -> str:
        return json.dumps({
            "owner": str(self.owner),
            "current_epoch": int(self.epoch_counter),
            "total_events": int(self.next_event_seq),
            "total_testimonies": int(self.next_testimony_seq),
            "total_disputes": int(self.next_dispute_seq),
        })