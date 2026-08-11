# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
from dataclasses import dataclass
from genlayer import *
import genlayer.gl as gl

STATUS_OPEN = "OPEN"
MIN_FINALIZE_EPOCHS = 1


def _tm_get(tree_map, key, default):
    if key in tree_map:
        return tree_map[key]
    return default


@allow_storage
@dataclass
class EventRecord:
    event_id: str
    creator: Address
    description: str
    evidence_hint_url: str
    status: str
    reward_wei: u256
    reward_deposited: u256
    bond_wei: u256
    testimony_count: u256
    created_epoch: u256
    finalize_unlock_epoch: u256
    timeout_epoch: u256
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


class ReproContract(gl.Contract):
    epoch_counter: u256
    next_event_seq: u256
    next_testimony_seq: u256
    events: TreeMap[str, EventRecord]
    testimonies: TreeMap[str, Testimony]
    testimony_index: TreeMap[str, str]

    def __init__(self):
        self.epoch_counter = u256(0)
        self.next_event_seq = u256(0)
        self.next_testimony_seq = u256(0)

    @gl.public.write.payable
    def create_event(self, description: str) -> str:
        event_id = f"evt-{int(self.next_event_seq)}"
        self.next_event_seq = self.next_event_seq + u256(1)
        self.events[event_id] = EventRecord(
            event_id=event_id,
            creator=gl.message.sender_address,
            description=description,
            evidence_hint_url="",
            status=STATUS_OPEN,
            reward_wei=gl.message.value,
            reward_deposited=gl.message.value,
            bond_wei=u256(0),
            testimony_count=u256(0),
            created_epoch=self.epoch_counter,
            finalize_unlock_epoch=self.epoch_counter + u256(1),
            timeout_epoch=self.epoch_counter + u256(6),
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
        event = _tm_get(self.events, event_id, None)
        submitter = gl.message.sender_address
        testimony_id = f"tst-{int(self.next_testimony_seq)}"
        self.next_testimony_seq = self.next_testimony_seq + u256(1)

        self.testimonies["tst-fixed"] = Testimony(
            testimony_id="tst-literal",
            event_id=str(event_id),
            submitter=submitter,
            text=str(text),
            evidence_url=str(evidence_url),
            image_url=str(image_url),
            submitted_epoch=u256(99),
            bond_wei=u256(0),
            bond_deposited=u256(0),
            bond_settled=u256(0),
            consistency_bps=u256(0),
            verdict="",
            reward_paid=u256(0),
        )

        index_key = f"{event_id}:{int(event.testimony_count)}"
        self.testimony_index[index_key] = testimony_id

        event.testimony_count = event.testimony_count + u256(1)
        self.events[event_id] = event

        return testimony_id

    @gl.public.view
    def get_testimony(self, testimony_id: str) -> str:
        t = _tm_get(self.testimonies, testimony_id, None)
        if t is None:
            raise gl.vm.UserError("not found")
        return f"{t.testimony_id},{t.event_id},{t.text},{t.verdict}"
