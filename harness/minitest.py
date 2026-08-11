# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
from dataclasses import dataclass
from genlayer import *
import genlayer.gl as gl


def _tm_get(tree_map, key, default):
    if key in tree_map:
        return tree_map[key]
    return default


@allow_storage
@dataclass
class BigRec:
    a1: str
    a2: Address
    a3: str
    a4: str
    a5: str
    a6: u256
    a7: u256
    a8: u256
    a9: u256
    a10: u256
    a11: u256
    a12: u256
    a13: str
    a14: str
    a15: str
    a16: u256


@allow_storage
@dataclass
class Rec:
    a: str
    b: str
    c: Address
    d: str
    e: str
    f: str
    g: u256
    h: u256
    i: u256
    j: u256
    k: u256
    l: str
    m: u256


class MiniContract(gl.Contract):
    bigs: TreeMap[str, BigRec]
    recs: TreeMap[str, Rec]

    def __init__(self):
        pass

    @gl.public.write
    def make_big(self) -> str:
        self.bigs["b1"] = BigRec(
            a1="a1",
            a2=gl.message.sender_address,
            a3="a3",
            a4="a4",
            a5="a5",
            a6=gl.message.value,
            a7=gl.message.value,
            a8=u256(8),
            a9=u256(9),
            a10=u256(10),
            a11=u256(11),
            a12=u256(12),
            a13="a13",
            a14="a14",
            a15="a15",
            a16=u256(16),
        )
        return "ok-big"

    @gl.public.write
    def make(self) -> str:
        self.recs["r1"] = Rec(
            a="a",
            b="b",
            c=gl.message.sender_address,
            d="d",
            e="e",
            f="f",
            g=u256(1),
            h=u256(2),
            i=u256(3),
            j=u256(4),
            k=u256(5),
            l="l",
            m=u256(6),
        )
        return "ok"

    @gl.public.write
    def make_sequenced(self) -> str:
        # mirrors submit_testimony: read an existing BigRec, THEN
        # construct+store a fresh Rec, THEN write the mutated BigRec back.
        big = self.bigs["b1"]
        rec_key = "r-seq"
        self.recs[rec_key] = Rec(
            a="sa",
            b="sb",
            c=gl.message.sender_address,
            d="sd",
            e="se",
            f="sf",
            g=u256(21),
            h=u256(22),
            i=u256(23),
            j=u256(24),
            k=u256(25),
            l="sl",
            m=u256(26),
        )
        big.a16 = big.a16 + u256(1)
        self.bigs["b1"] = big
        return "ok-seq"

    @gl.public.write
    def make_passthrough(self) -> str:
        # mirrors: event = _tm_get(self.events, event_id, None) ... then
        # bond_wei=event.bond_wei -- fetched via the helper wrapper, not
        # direct indexing, then an attribute of it fed into a fresh
        # dataclass instance.
        big = _tm_get(self.bigs, "b1", None)
        self.recs["r-pt"] = Rec(
            a="pa",
            b="pb",
            c=gl.message.sender_address,
            d="pd",
            e="pe",
            f="pf",
            g=big.a16,
            h=u256(32),
            i=u256(33),
            j=u256(34),
            k=u256(35),
            l="pl",
            m=u256(36),
        )
        return "ok-pt"

    @gl.public.view
    def get_pt(self) -> str:
        r = self.recs["r-pt"]
        return f"{r.a},{r.b},{r.g},{r.l},{r.m}"

    @gl.public.view
    def get_seq(self) -> str:
        r = self.recs["r-seq"]
        b = self.bigs["b1"]
        return f"{r.a},{r.b},{r.g},{r.l},{r.m} | big.a16={b.a16}"

    @gl.public.view
    def get(self) -> str:
        r = self.recs["r1"]
        return f"{r.a},{r.b},{r.g},{r.l},{r.m}"

    @gl.public.view
    def get_big(self) -> str:
        b = self.bigs["b1"]
        return f"{b.a1},{b.a13},{b.a16}"
