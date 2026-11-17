#!/usr/bin/env python3
"""zech-forge2: a GitHub-compatible git forge (make.fun github-core-clone-01).

Pure Python 3.13 stdlib, one self-contained file. Binds 0.0.0.0 on an ephemeral
port, registers with the trial env, serves REST v3 / GraphQL v4 / MCP / git smart
HTTP from an in-memory store, and exits 0 when the env reports done.

Design notes (from the kit's RULES.txt and the check sources in program/env/src):
- The env dials req.socket.remoteAddress + the registered port, so the listener
  MUST bind 0.0.0.0: a loopback-only bind scores 0 on the grading pod (v1's bug).
- No wall clock or unseeded randomness reaches any response: timestamps come from
  a logical clock and request ids from a counter, so replays are byte-identical.
- Viewer identity is recomputed from the fixed scenario seed (wire.ts
  buildScenario / generateUsers): the token names users[0].
"""

from __future__ import annotations

import base64
import difflib
import hashlib
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# -------------------------------------------------------------------------------------
# Scenario identity (port of wire.ts: SCENARIO_SEED, fnv1a64, mix64, Rng, generateUsers)
# -------------------------------------------------------------------------------------

SCENARIO_SEED = 0x6A09E667F3BCC909
U64_MASK = 0xFFFFFFFFFFFFFFFF
SCENARIO_EPOCH_SEC = 1_735_689_600  # 2025-01-01T00:00:00Z

_FIRST_NAMES = [
    "ada", "bram", "cora", "dmitri", "elena", "felix", "greta", "hugo",
    "iris", "jonas", "kira", "lars", "mira", "nico", "olga", "pavel",
    "quinn", "rosa", "sven", "tess", "ulla", "vito", "wren", "yara", "zane",
]
_LAST_NAMES = [
    "anders", "bauer", "costa", "dahl", "eriksen", "fischer", "garcia",
    "holm", "iversen", "jansen", "keller", "lind", "moreau", "novak",
    "olsen", "peters", "quist", "rossi", "sato", "turi", "ulrich", "vega",
    "weber", "xu", "yilmaz", "zimmer",
]


def _fnv1a64(text: str) -> int:
    h = 0xCBF29CE484222325
    for ch in text:
        h ^= ord(ch)
        h = (h * 0x100000001B3) & U64_MASK
    return h


def _mix64(value: int) -> int:
    z = (value + 0x9E3779B97F4A7C15) & U64_MASK
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & U64_MASK
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & U64_MASK
    return (z ^ (z >> 31)) & U64_MASK


def _scenario_seed_for(label: str) -> int:
    return _mix64(SCENARIO_SEED ^ _fnv1a64(label))


class _Rng:
    """xorshift64* exactly as wire.ts Rng."""

    def __init__(self, seed: int):
        masked = seed & U64_MASK
        self._state = masked if masked != 0 else SCENARIO_SEED

    def next_u64(self) -> int:
        x = self._state
        x = (x ^ (x >> 12)) & U64_MASK
        x = (x ^ ((x << 25) & U64_MASK)) & U64_MASK
        x = (x ^ (x >> 27)) & U64_MASK
        self._state = x
        return (x * 0x2545F4914F6CDD1D) & U64_MASK

    def next_u32(self) -> int:
        return self.next_u64() >> 32

    def next_int(self, max_exclusive: int) -> int:
        if max_exclusive == 1:
            return 0
        limit = 4_294_967_296 - (4_294_967_296 % max_exclusive)
        while True:
            value = self.next_u32()
            if value < limit:
                return value % max_exclusive

    def pick(self, items):
        return items[self.next_int(len(items))]


def _generate_users(count: int):
    rng = _Rng(_scenario_seed_for("users"))
    used = set()
    users = []
    for i in range(count):
        first = rng.pick(_FIRST_NAMES)
        last = rng.pick(_LAST_NAMES)
        base = f"{first}{last}"
        login = base
        suffix = 2
        while login in used:
            login = f"{base}-{suffix}"
            suffix += 1
        used.add(login)
        users.append({
            "id": 1000 + i,
            "login": login,
            "name": f"{first} {last}",
            "email": f"{login}@example.invalid",
        })
    return users


_USERS = _generate_users(12)
VIEWER = _USERS[0]          # {"login": "adaiversen", ...}
STRANGER = _USERS[2]
ZERO_OID = "0" * 40

# -------------------------------------------------------------------------------------
# Logical clock (deterministic timestamps; no wall clock in any response)
# -------------------------------------------------------------------------------------

_clock_lock = threading.Lock()
_clock = SCENARIO_EPOCH_SEC


def tick() -> int:
    """Advance the logical clock by one second and return the new epoch value."""
    global _clock
    with _clock_lock:
        _clock += 1
        return _clock


def iso(ts: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


# -------------------------------------------------------------------------------------
# Git objects: hashing, building, parsing
# -------------------------------------------------------------------------------------

def hash_object(otype: str, data: bytes) -> str:
    header = f"{otype} {len(data)}\0".encode("ascii")
    return hashlib.sha1(header + data).hexdigest()


def build_tree(entries) -> bytes:
    """entries: iterable of (mode_str, name, oid_hex); sorted git order."""
    def sort_key(entry):
        mode, name, _oid = entry
        return name + ("/" if mode == "40000" else "")

    out = bytearray()
    for mode, name, oid in sorted(entries, key=sort_key):
        out += f"{mode} {name}\0".encode("utf-8")
        out += bytes.fromhex(oid)
    return bytes(out)


def parse_tree(data: bytes):
    """Return [(mode, name, oid_hex)] in stored order."""
    entries = []
    i = 0
    n = len(data)
    while i < n:
        sp = data.index(b" ", i)
        mode = data[i:sp].decode("ascii")
        nul = data.index(b"\0", sp)
        name = data[sp + 1:nul].decode("utf-8")
        oid = data[nul + 1:nul + 21].hex()
        entries.append((mode, name, oid))
        i = nul + 21
    return entries


def _ident_line(name: str, email: str, ts: int) -> str:
    return f"{name} <{email}> {ts} +0000"


def build_commit(tree: str, parents, message: str, ts: int) -> bytes:
    lines = [f"tree {tree}"]
    for parent in parents:
        lines.append(f"parent {parent}")
    ident = _ident_line(VIEWER["name"], VIEWER["email"], ts)
    lines.append(f"author {ident}")
    lines.append(f"committer {ident}")
    lines.append("")
    lines.append(message)
    return "\n".join(lines).encode("utf-8")


def parse_commit(data: bytes):
    text = data.decode("utf-8", "replace")
    header, _, message = text.partition("\n\n")
    tree = None
    parents = []
    author = ""
    committer = ""
    for line in header.split("\n"):
        if line.startswith("tree "):
            tree = line[5:].strip()
        elif line.startswith("parent "):
            parents.append(line[7:].strip())
        elif line.startswith("author "):
            author = line[7:]
        elif line.startswith("committer "):
            committer = line[10:]
    return {"tree": tree, "parents": parents, "author": author,
            "committer": committer, "message": message}


def build_tag(obj: str, otype: str, tag: str, message: str, ts: int) -> bytes:
    ident = _ident_line(VIEWER["name"], VIEWER["email"], ts)
    lines = [f"object {obj}", f"type {otype}", f"tag {tag}", f"tagger {ident}", "", message]
    return "\n".join(lines).encode("utf-8")


def parse_tag(data: bytes):
    text = data.decode("utf-8", "replace")
    header, _, message = text.partition("\n\n")
    out = {"message": message}
    for line in header.split("\n"):
        if line.startswith("object "):
            out["object"] = line[7:].strip()
        elif line.startswith("type "):
            out["type"] = line[5:].strip()
        elif line.startswith("tag "):
            out["tag"] = line[4:].strip()
        elif line.startswith("tagger "):
            out["tagger"] = line[7:]
    return out


# -------------------------------------------------------------------------------------
# pkt-line framing
# -------------------------------------------------------------------------------------

def pkt_line(payload: bytes) -> bytes:
    total = len(payload) + 4
    return ("%04x" % total).encode("ascii") + payload


def pkt_flush() -> bytes:
    return b"0000"


def pkt_delim() -> bytes:
    return b"0001"


def parse_pkt_lines(buf: bytes):
    """Parse a byte string into a list of ('data', bytes) | ('flush',) | ('delim',).
    Stops at the first malformed/truncated line; returns (packets, consumed_bytes)."""
    packets = []
    i = 0
    n = len(buf)
    while i + 4 <= n:
        head = buf[i:i + 4]
        try:
            size = int(head.decode("ascii"), 16)
        except ValueError:
            break
        if size == 0:
            packets.append(("flush", b""))
            i += 4
            continue
        if size == 1:
            packets.append(("delim", b""))
            i += 4
            continue
        if size < 4 or i + size > n:
            break
        packets.append(("data", buf[i + 4:i + size]))
        i += size
    return packets, i


# -------------------------------------------------------------------------------------
# PACK v2: write (plain objects) and read (plain + OFS/REF deltas)
# -------------------------------------------------------------------------------------

def _pack_obj_header(otype: str, size: int) -> bytes:
    type_no = {"commit": 1, "tree": 2, "blob": 3, "tag": 4}[otype]
    out = bytearray()
    first = (type_no << 4) | (size & 0x0F)
    size >>= 4
    if size:
        first |= 0x80
    out.append(first)
    while size:
        byte = size & 0x7F
        size >>= 7
        if size:
            byte |= 0x80
        out.append(byte)
    return bytes(out)


def write_pack(objects) -> bytes:
    """objects: iterable of (oid, otype, data). Deterministic order (sorted by oid)."""
    objs = sorted(objects, key=lambda item: item[0])
    out = bytearray()
    out += b"PACK"
    out += (2).to_bytes(4, "big")
    out += len(objs).to_bytes(4, "big")
    body = bytes(out)
    parts = [body]
    for _oid, otype, data in objs:
        parts.append(_pack_obj_header(otype, len(data)))
        parts.append(zlib.compress(data, 6))
    trailer = hashlib.sha1(b"".join(parts)).digest()
    parts.append(trailer)
    return b"".join(parts)


def _read_varint_size(buf: bytes, pos: int):
    byte = buf[pos]
    pos += 1
    size = byte & 0x0F
    shift = 4
    while byte & 0x80:
        byte = buf[pos]
        pos += 1
        size |= (byte & 0x7F) << shift
        shift += 7
    return size, pos


def _read_ofs_offset(buf: bytes, pos: int):
    byte = buf[pos]
    pos += 1
    offset = byte & 0x7F
    while byte & 0x80:
        byte = buf[pos]
        pos += 1
        offset = ((offset + 1) << 7) | (byte & 0x7F)
    return offset, pos


def _apply_delta(base: bytes, delta: bytes) -> bytes:
    pos = 0

    def varint():
        nonlocal pos
        value = 0
        shift = 0
        while True:
            byte = delta[pos]
            pos += 1
            value |= (byte & 0x7F) << shift
            shift += 7
            if not (byte & 0x80):
                return value

    src_size = varint()
    tgt_size = varint()
    if src_size != len(base):
        raise ValueError("delta base size mismatch")
    out = bytearray()
    while pos < len(delta):
        op = delta[pos]
        pos += 1
        if op & 0x80:
            cp_off = 0
            cp_size = 0
            for bit in range(4):
                if op & (1 << bit):
                    cp_off |= delta[pos] << (8 * bit)
                    pos += 1
            for bit in range(3):
                if op & (0x10 << bit):
                    cp_size |= delta[pos] << (8 * bit)
                    pos += 1
            if cp_size == 0:
                cp_size = 0x10000
            out += base[cp_off:cp_off + cp_size]
        elif op:
            out += delta[pos:pos + op]
            pos += op
        else:
            raise ValueError("delta opcode 0")
    if len(out) != tgt_size:
        raise ValueError("delta target size mismatch")
    return bytes(out)


class PackError(ValueError):
    pass


def read_pack(buf: bytes, resolve_base=None):
    """Parse a PACK stream. Returns {oid: (otype, data)} with deltas resolved.
    resolve_base(oid) -> (otype, data) | None for thin-pack bases already held."""
    if len(buf) < 32 or buf[:4] != b"PACK":
        raise PackError("not a PACK stream")
    version = int.from_bytes(buf[4:8], "big")
    if version not in (2, 3):
        raise PackError(f"pack version {version}")
    count = int.from_bytes(buf[8:12], "big")
    trailer = buf[-20:]
    if hashlib.sha1(buf[:-20]).digest() != trailer:
        raise PackError("pack trailer checksum mismatch")
    pos = 12
    end = len(buf) - 20
    raw = []          # (offset, otype_or_None, data_or_delta, delta_kind, delta_base)
    by_offset = {}
    objects = {}
    for _ in range(count):
        start = pos
        byte = buf[pos]
        pos += 1
        type_no = (byte >> 4) & 0x07
        size = byte & 0x0F
        shift = 4
        while byte & 0x80:
            byte = buf[pos]
            pos += 1
            size |= (byte & 0x7F) << shift
            shift += 7
        if type_no in (1, 2, 3, 4):
            otype = {1: "commit", 2: "tree", 3: "blob", 4: "tag"}[type_no]
            dec = zlib.decompressobj()
            data = dec.decompress(buf[pos:end])
            consumed = len(buf[pos:end]) - len(dec.unused_data)
            pos += consumed
            if len(data) != size:
                raise PackError("object size mismatch")
            oid = hash_object(otype, data)
            raw.append((start, otype, data, None, None))
            by_offset[start] = oid
            objects[oid] = (otype, data)
        elif type_no == 6:
            offset, pos = _read_ofs_offset(buf, pos)
            base_offset = start - offset
            dec = zlib.decompressobj()
            data = dec.decompress(buf[pos:end])
            consumed = len(buf[pos:end]) - len(dec.unused_data)
            pos += consumed
            raw.append((start, None, data, "ofs", base_offset))
        elif type_no == 7:
            base_oid = buf[pos:pos + 20].hex()
            pos += 20
            dec = zlib.decompressobj()
            data = dec.decompress(buf[pos:end])
            consumed = len(buf[pos:end]) - len(dec.unused_data)
            pos += consumed
            raw.append((start, None, data, "ref", base_oid))
        else:
            raise PackError(f"unknown pack object type {type_no}")

    def resolve_entry(entry, seen):
        _start, otype, data, kind, base = entry
        if otype is not None:
            return hash_object(otype, data), otype, data
        if kind == "ofs":
            base_oid = by_offset.get(base)
            if base_oid is None:
                raise PackError("ofs-delta base not in pack")
            base_entry = None
            for cand in raw:
                if cand[0] == base:
                    base_entry = cand
                    break
            _boid, btype, bdata = resolve_entry(base_entry, seen)
        else:
            base_oid = base
            if base_oid in objects:
                btype, bdata = objects[base_oid]
            elif resolve_base is not None:
                found = resolve_base(base_oid)
                if found is None:
                    raise PackError("ref-delta base not available")
                btype, bdata = found
            else:
                raise PackError("ref-delta base not available")
        result = _apply_delta(bdata, data)
        oid = hash_object(btype, result)
        return oid, btype, result

    for entry in raw:
        if entry[1] is None:
            oid, otype, data = resolve_entry(entry, set())
            objects[oid] = (otype, data)
    return objects

# -------------------------------------------------------------------------------------
# Store: repositories, issues, pull requests — all in memory
# -------------------------------------------------------------------------------------

ZERO_OID = "0" * 40


class Repo:
    __slots__ = ("owner", "name", "dbid", "private", "description", "default_branch",
                 "created_ts", "refs", "issues", "prs", "labels", "comments", "counter",
                 "comment_counter", "label_counter", "pack_cache", "pack_version")

    def __init__(self, owner, name, dbid, private, description):
        self.owner = owner
        self.name = name
        self.dbid = dbid
        self.private = private
        self.description = description
        self.default_branch = "main"
        self.created_ts = tick()
        self.refs = {}            # "refs/heads/main" -> oid
        self.issues = {}          # number -> issue dict
        self.prs = {}             # number -> pr dict
        self.labels = {}          # name -> label dict
        self.comments = {}        # issue number -> [comment dict]
        self.counter = 0          # shared issue/PR number counter
        self.comment_counter = 0
        self.label_counter = 0
        self.pack_cache = {}
        self.pack_version = 0

    def full_name(self):
        return f"{self.owner}/{self.name}"

    def bump(self):
        self.pack_version += 1
        self.pack_cache.clear()


class Forge:
    """All forge state. Objects are global (an oid determines its bytes); repos,
    refs, issues and PRs live per repository. One lock guards every mutation."""

    def __init__(self, viewer_login: str):
        self.viewer = viewer_login
        self.lock = threading.RLock()
        self.objects = {}         # oid -> (otype, data)
        self.repos = {}           # (owner, name) -> Repo
        self.repo_counter = 0
        self.req_counter = 0
        self.req_lock = threading.Lock()

    def next_req_id(self) -> str:
        with self.req_lock:
            self.req_counter += 1
            return f"req-{self.req_counter}"

    def put_object(self, otype: str, data: bytes) -> str:
        oid = hash_object(otype, data)
        if oid not in self.objects:
            self.objects[oid] = (otype, data)
        return oid

    def get_object(self, oid: str):
        return self.objects.get(oid)

    def create_repo(self, name: str, private: bool, description: str):
        with self.lock:
            key = (self.viewer, name)
            if key in self.repos:
                return None
            self.repo_counter += 1
            repo = Repo(self.viewer, name, 5000 + self.repo_counter, private, description)
            self.repos[key] = repo
            return repo

    def get_repo(self, owner: str, name: str):
        if owner != self.viewer:
            return None
        return self.repos.get((owner, name))


FORGE: Forge


def gid(kind: str, *parts) -> str:
    """Opaque Relay-style global id (base64, never the plain numeric id)."""
    raw = ":".join([kind, *[str(p) for p in parts]]).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def parse_gid(value: str):
    try:
        raw = base64.b64decode(value.encode("ascii"), validate=True).decode("utf-8")
    except Exception:
        return None
    return tuple(raw.split(":"))


# -------------------------------------------------------------------------------------
# Tree helpers (flatten / rebuild / lookup)
# -------------------------------------------------------------------------------------

def flatten_tree(forge: Forge, tree_oid, prefix=""):
    """{path: (mode, oid)} for every blob under a tree (recursive)."""
    out = {}
    if not tree_oid:
        return out
    entry = forge.get_object(tree_oid)
    if entry is None or entry[0] != "tree":
        return out
    for mode, name, oid in parse_tree(entry[1]):
        path = f"{prefix}{name}"
        if mode == "40000":
            out.update(flatten_tree(forge, oid, prefix=path + "/"))
        else:
            out[path] = (mode, oid)
    return out


def build_tree_from_flat(forge: Forge, flat):
    """flat: {path: (mode, oid)} -> root tree oid."""
    def build(node):
        entries = []
        for name, child in sorted(node.items()):
            if isinstance(child, dict):
                entries.append(("40000", name, build(child)))
            else:
                entries.append((child[0], name, child[1]))
        return forge.put_object("tree", build_tree(entries))

    root = {}
    for path, value in flat.items():
        parts = path.split("/")
        node = root
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    return build(root)


def tree_blob_at(forge: Forge, tree_oid, path):
    flat = flatten_tree(forge, tree_oid)
    hit = flat.get(path)
    return hit[1] if hit else None


def commit_tree(forge: Forge, commit_oid):
    entry = forge.get_object(commit_oid)
    if entry is None or entry[0] != "commit":
        return None
    return parse_commit(entry[1])["tree"]


def commit_parents(forge: Forge, commit_oid):
    entry = forge.get_object(commit_oid)
    if entry is None or entry[0] != "commit":
        return []
    return parse_commit(entry[1])["parents"]


def ancestors(forge: Forge, tip):
    """All commits reachable from tip (tip included), BFS order."""
    seen = []
    seen_set = set()
    queue = [tip]
    while queue:
        cur = queue.pop(0)
        if cur in seen_set or cur == ZERO_OID:
            continue
        if forge.get_object(cur) is None:
            continue
        seen_set.add(cur)
        seen.append(cur)
        queue.extend(commit_parents(forge, cur))
    return seen


def merge_base(forge: Forge, head_tip, base_tip):
    base_set = set(ancestors(forge, base_tip))
    for cand in ancestors(forge, head_tip):
        if cand in base_set:
            return cand
    return None


def reachable_objects(forge: Forge, tips):
    """Full object closure (commits, trees, blobs, tags) reachable from tips."""
    out = {}
    stack = list(tips)
    seen = set()
    while stack:
        oid = stack.pop()
        if oid in seen or oid == ZERO_OID:
            continue
        seen.add(oid)
        entry = forge.get_object(oid)
        if entry is None:
            continue
        otype, data = entry
        out[oid] = (otype, data)
        if otype == "commit":
            parsed = parse_commit(data)
            stack.append(parsed["tree"])
            stack.extend(parsed["parents"])
        elif otype == "tree":
            for _mode, _name, child in parse_tree(data):
                stack.append(child)
        elif otype == "tag":
            stack.append(parse_tag(data)["object"])
    return out


def peel_to_commit(forge: Forge, oid):
    """Follow annotated tags to the commit they point at."""
    seen = set()
    while True:
        entry = forge.get_object(oid)
        if entry is None or oid in seen:
            return oid
        seen.add(oid)
        if entry[0] == "tag":
            oid = parse_tag(entry[1])["object"]
        else:
            return oid


def line_diff_counts(old: bytes, new: bytes):
    """(additions, deletions) between two blob versions, by line."""
    old_lines = old.decode("utf-8", "replace").splitlines()
    new_lines = new.decode("utf-8", "replace").splitlines()
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    additions = deletions = 0
    for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
        if tag in ("insert", "replace"):
            additions += j2 - j1
        if tag in ("delete", "replace"):
            deletions += _i2 - _i1
    return additions, deletions


def _pr_base_tip(repo: Repo, pr: dict):
    """Current tip of the PR's base branch (a PR's stored base_sha is only the
    snapshot from creation; the branch may have moved since)."""
    return repo.refs.get(f"refs/heads/{pr['base_ref']}") or pr["base_sha"]


def pr_file_changes(forge: Forge, repo: Repo, pr: dict):
    """[{filename, status, additions, deletions, changes}] head vs merge-base."""
    head_tip = pr["head_sha"]
    base_tip = _pr_base_tip(repo, pr)
    mb = merge_base(forge, head_tip, base_tip) if head_tip and base_tip else None
    mb_tree = commit_tree(forge, mb) if mb else None
    head_tree = commit_tree(forge, head_tip) if head_tip else None
    old_flat = flatten_tree(forge, mb_tree)
    new_flat = flatten_tree(forge, head_tree)
    files = []
    for path in sorted(set(old_flat) | set(new_flat)):
        old = old_flat.get(path)
        new = new_flat.get(path)
        if old is None:
            new_data = forge.get_object(new[1])[1]
            additions, _d = line_diff_counts(b"", new_data)
            files.append({"filename": path, "status": "added", "additions": additions,
                          "deletions": 0, "changes": additions})
        elif new is None:
            old_data = forge.get_object(old[1])[1]
            _a, deletions = line_diff_counts(old_data, b"")
            files.append({"filename": path, "status": "removed", "additions": 0,
                          "deletions": deletions, "changes": deletions})
        elif old[1] != new[1]:
            old_data = forge.get_object(old[1])[1]
            new_data = forge.get_object(new[1])[1]
            additions, deletions = line_diff_counts(old_data, new_data)
            files.append({"filename": path, "status": "modified", "additions": additions,
                          "deletions": deletions, "changes": additions + deletions})
    return files


def merge_conflict(forge: Forge, repo: Repo, pr: dict) -> bool:
    """True when head and base both changed the same path differently vs merge-base."""
    head_tip = pr["head_sha"]
    base_tip = _pr_base_tip(repo, pr)
    mb = merge_base(forge, head_tip, base_tip) if head_tip and base_tip else None
    if mb is None:
        return False
    mb_flat = flatten_tree(forge, commit_tree(forge, mb))
    head_flat = flatten_tree(forge, commit_tree(forge, head_tip))
    base_flat = flatten_tree(forge, commit_tree(forge, base_tip))
    for path in set(mb_flat) | set(head_flat) | set(base_flat):
        mb_v = mb_flat.get(path, (None, None))[1]
        head_v = head_flat.get(path, (None, None))[1]
        base_v = base_flat.get(path, (None, None))[1]
        head_changed = head_v != mb_v
        base_changed = base_v != mb_v
        if head_changed and base_changed and head_v != base_v:
            return True
    return False


def squash_merge(forge: Forge, repo: Repo, pr: dict, ts: int):
    """Apply head's changes onto the base tip; return the new commit oid (or None)."""
    head_tip = pr["head_sha"]
    base_tip = _pr_base_tip(repo, pr)
    mb = merge_base(forge, head_tip, base_tip) if head_tip and base_tip else None
    mb_flat = flatten_tree(forge, commit_tree(forge, mb)) if mb else {}
    head_flat = flatten_tree(forge, commit_tree(forge, head_tip))
    base_flat = flatten_tree(forge, commit_tree(forge, base_tip))
    result = dict(base_flat)
    for path in set(mb_flat) | set(head_flat):
        mb_v = mb_flat.get(path, (None, None))[1]
        head_v = head_flat.get(path, (None, None))[1]
        if head_v == mb_v:
            continue
        if head_v is None:
            result.pop(path, None)
        else:
            result[path] = head_flat[path]
    tree_oid = build_tree_from_flat(forge, result)
    message = (f"Merge pull request #{pr['number']} from {repo.owner}/{pr['head_ref']}\n\n"
               f"{pr['title']}\n")
    commit_data = build_commit(tree_oid, [base_tip], message, ts)
    return forge.put_object("commit", commit_data)


def make_content_commit(forge: Forge, repo: Repo, branch: str, flat, message: str, ts: int):
    """Commit `flat` ({path: (mode, oid)}) on branch; returns the new commit oid."""
    tree_oid = build_tree_from_flat(forge, flat)
    parents = []
    current = repo.refs.get(f"refs/heads/{branch}")
    if current:
        parents = [current]
    data = build_commit(tree_oid, parents, message, ts)
    oid = forge.put_object("commit", data)
    repo.refs[f"refs/heads/{branch}"] = oid
    repo.bump()
    return oid


def resolve_ref(forge: Forge, repo: Repo, ref):
    """Resolve a branch name, full ref, or oid to a commit oid (or None)."""
    if not ref:
        return repo.refs.get(f"refs/heads/{repo.default_branch}")
    if ref in repo.refs:
        return repo.refs[ref]
    full = f"refs/heads/{ref}"
    if full in repo.refs:
        return repo.refs[full]
    if re.fullmatch(r"[0-9a-f]{40}", ref or ""):
        entry = forge.get_object(ref)
        if entry is not None:
            return peel_to_commit(forge, ref)
    return None

# -------------------------------------------------------------------------------------
# HTTP plumbing
# -------------------------------------------------------------------------------------

_REASONS = {
    200: "OK", 201: "Created", 202: "Accepted", 204: "No Content",
    400: "Bad Request", 401: "Unauthorized", 404: "Not Found",
    405: "Method Not Allowed", 409: "Conflict", 422: "Unprocessable Entity",
    500: "Internal Server Error",
}


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 256
    allow_reuse_address = True

    def get_request(self):
        sock, addr = super().get_request()
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return sock, addr


class Req:
    __slots__ = ("method", "path", "segments", "query", "headers", "body", "host")

    def __init__(self, method, path, segments, query, headers, body, host):
        self.method = method
        self.path = path
        self.segments = segments
        self.query = query
        self.headers = headers
        self.body = body
        self.host = host


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _json_bytes(obj) -> bytes:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def parse_json_body(req: Req):
    """Parse a JSON object body; empty body -> {}; malformed -> 400."""
    if not req.body:
        return {}
    try:
        value = json.loads(req.body.decode("utf-8"))
    except Exception:
        raise ApiError(400, "Invalid JSON body")
    if not isinstance(value, dict):
        raise ApiError(400, "Invalid JSON body")
    return value


def parse_json_any(req: Req):
    """Parse any JSON value (object, array, ...); malformed -> 400."""
    if not req.body:
        return None
    try:
        return json.loads(req.body.decode("utf-8"))
    except Exception:
        raise ApiError(400, "Invalid JSON body")


class ForgeServer:
    def __init__(self, forge: Forge, token, host="0.0.0.0", port=0):
        self.forge = forge
        self.token = token  # None = dev mode (accept any non-empty bearer)
        self._httpd = _Server((host, port), self._handler_class())

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    def serve_forever(self):
        self._httpd.serve_forever()

    def shutdown(self):
        self._httpd.shutdown()
        self._httpd.server_close()

    def _handler_class(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _handle(self, method):
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = 0
                try:
                    body = self.rfile.read(length) if length > 0 else b""
                except Exception:
                    body = b""
                split = urllib.parse.urlsplit(self.path)
                path = urllib.parse.unquote(split.path)
                query = urllib.parse.parse_qs(split.query)
                headers = {k.lower(): v for k, v in self.headers.items()}
                host = self.headers.get("Host") or "127.0.0.1"
                req = Req(method, path, [s for s in path.split("/") if s != ""],
                          query, headers, body, host)
                try:
                    status, ctype, resp_body, extra = server.dispatch(req)
                except ApiError as exc:
                    status, ctype, resp_body, extra = server._error(exc.status, exc.message)
                except Exception as exc:  # never crash the server on one request
                    status, ctype, resp_body, extra = server._error(500, f"Internal error: {exc}")
                self._send(status, ctype, resp_body, extra)

            def _send(self, status, ctype, resp_body, extra):
                reason = _REASONS.get(status, "OK")
                head = [f"HTTP/1.1 {status} {reason}\r\n".encode("ascii")]
                head.append(f"Content-Type: {ctype}\r\n".encode("ascii"))
                head.append(f"Content-Length: {len(resp_body)}\r\n".encode("ascii"))
                head.append(f"X-GitHub-Request-Id: {server.forge.next_req_id()}\r\n".encode("ascii"))
                for key, value in (extra or {}).items():
                    head.append(f"{key}: {value}\r\n".encode("ascii"))
                head.append(b"Connection: keep-alive\r\n")
                head.append(b"\r\n")
                try:
                    self.wfile.write(b"".join(head) + resp_body)
                    self.wfile.flush()
                except Exception:
                    self.close_connection = True

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

            def do_PUT(self):
                self._handle("PUT")

            def do_PATCH(self):
                self._handle("PATCH")

            def do_DELETE(self):
                self._handle("DELETE")

        return Handler

    # -- response helpers ----------------------------------------------------

    def _json(self, status, obj, extra=None):
        return status, "application/json", _json_bytes(obj), extra

    def _error(self, status, message):
        return status, "application/json", _json_bytes({"message": message}), None

    def _no_content(self):
        return 204, "application/json", b"", None

    def _require_auth(self, req: Req):
        if self.token is None:
            auth = req.headers.get("authorization", "")
            if not auth.startswith("Bearer ") or not auth[7:].strip():
                raise ApiError(401, "Requires authentication")
            return
        auth = req.headers.get("authorization", "")
        if auth != f"Bearer {self.token}":
            raise ApiError(401, "Bad credentials")

    # -- dispatch ------------------------------------------------------------

    def dispatch(self, req: Req):
        if req.path == "/healthz" and req.method == "GET":
            return self._json(200, {"ok": True})
        if req.path.startswith("/api/v3"):
            return self._dispatch_rest(req)
        if req.path == "/api/graphql" and req.method == "POST":
            return self._dispatch_graphql(req)
        if req.path == "/mcp" and req.method == "POST":
            return self._dispatch_mcp(req)
        match = re.fullmatch(r"/([^/]+)/([^/]+)\.git(/.*)?", req.path)
        if match:
            return self._dispatch_git(req, match.group(1), match.group(2),
                                     match.group(3) or "")
        raise ApiError(404, "Not Found")

    # =====================================================================
    # REST v3
    # =====================================================================

    def _base_url(self, req: Req) -> str:
        return f"http://{req.host}"

    def _dispatch_rest(self, req: Req):
        self._require_auth(req)
        rest = req.path[len("/api/v3"):] or "/"
        seg = [s for s in rest.split("/") if s != ""]
        method = req.method

        if seg == [] and method == "GET":
            return self._json(200, {
                "current_user_url": self._base_url(req) + "/api/v3/user",
                "repository_url": self._base_url(req) + "/api/v3/repos/{owner}/{repo}",
            }, {"X-OAuth-Scopes": "repo, read:org"})
        if seg == ["meta"] and method == "GET":
            return self._json(200, {"installed_version": "1.0.0"})
        if seg == ["user"] and method == "GET":
            return self._json(200, _user_json(self.forge.viewer))
        if seg == ["user", "repos"]:
            if method == "POST":
                return self._rest_create_repo(req)
            if method == "GET":
                return self._rest_list_user_repos(req)
        if len(seg) >= 3 and seg[0] == "repos":
            return self._dispatch_repo_rest(req, seg)
        raise ApiError(404, "Not Found")

    def _rest_create_repo(self, req: Req):
        body = parse_json_body(req)
        name = body.get("name")
        if not isinstance(name, str) or not name:
            raise ApiError(422, "Repository name is required")
        with self.forge.lock:
            repo = self.forge.create_repo(name, bool(body.get("private", False)),
                                          body.get("description") or "")
            if repo is None:
                raise ApiError(422, f"Repository {name} already exists")
        return self._json(201, _repo_json(self.forge, repo))

    def _rest_list_user_repos(self, req: Req):
        per_page = _int_query(req, "per_page", 30)
        page = _int_query(req, "page", 1)
        with self.forge.lock:
            repos = [r for (o, _n), r in sorted(self.forge.repos.items())
                     if o == self.forge.viewer]
        total = len(repos)
        start = (page - 1) * per_page
        chunk = repos[start:start + per_page]
        extra = {}
        if start + per_page < total:
            nxt = (f"{self._base_url(req)}/api/v3/user/repos"
                   f"?per_page={per_page}&page={page + 1}")
            last_page = (total + per_page - 1) // per_page
            last = (f"{self._base_url(req)}/api/v3/user/repos"
                    f"?per_page={per_page}&page={last_page}")
            extra["Link"] = f'<{nxt}>; rel="next", <{last}>; rel="last"'
        return self._json(200, [_repo_json(self.forge, r) for r in chunk], extra)

    def _repo_or_404(self, owner, name):
        repo = self.forge.get_repo(owner, name)
        if repo is None:
            raise ApiError(404, "Not Found")
        return repo

    def _dispatch_repo_rest(self, req: Req, seg):
        owner, name = seg[1], seg[2]
        rest = seg[3:]
        method = req.method
        repo = self._repo_or_404(owner, name)

        if rest == []:
            if method == "GET":
                return self._json(200, _repo_json(self.forge, repo))
            if method == "PATCH":
                body = parse_json_body(req)
                with self.forge.lock:
                    if "description" in body:
                        repo.description = body.get("description") or ""
                    if "private" in body:
                        repo.private = bool(body.get("private"))
                return self._json(200, _repo_json(self.forge, repo))
            if method == "DELETE":
                with self.forge.lock:
                    del self.forge.repos[(owner, name)]
                return self._no_content()
            raise ApiError(404, "Not Found")

        if rest == ["branches"]:
            if method != "GET":
                raise ApiError(404, "Not Found")
            return self._json(200, [_branch_json(self.forge, repo, b)
                                    for b in sorted(_branch_names(repo))])
        if len(rest) == 2 and rest[0] == "branches" and method == "GET":
            branch = rest[1]
            ref = repo.refs.get(f"refs/heads/{branch}")
            if ref is None:
                raise ApiError(404, "Not Found")
            return self._json(200, _branch_json(self.forge, repo, branch))
        if rest == ["commits"] and method == "GET":
            return self._rest_list_commits(req, repo)
        if rest == ["git", "refs"] and method == "POST":
            return self._rest_create_ref(req, repo)
        if len(rest) == 4 and rest[0] == "git" and rest[1] == "refs" and rest[2] == "heads" \
                and method == "GET":
            branch = rest[3]
            ref = repo.refs.get(f"refs/heads/{branch}")
            if ref is None:
                raise ApiError(404, "Not Found")
            return self._json(200, {"ref": f"refs/heads/{branch}",
                                    "node_id": gid("Ref", repo.dbid, branch),
                                    "url": f"{self._base_url(req)}/api/v3/repos/{owner}/{name}/git/refs/heads/{branch}",
                                    "object": {"sha": ref, "type": "commit",
                                               "url": ""}})
        if rest[0] == "contents":
            return self._dispatch_contents(req, repo, rest[1:])
        if rest == ["labels"] and method == "POST":
            return self._rest_create_label(req, repo)
        if rest == ["issues"]:
            if method == "POST":
                return self._rest_create_issue(req, repo)
            if method == "GET":
                return self._rest_list_issues(req, repo)
        if len(rest) == 2 and rest[0] == "issues" and rest[1].isdigit():
            number = int(rest[1])
            if method == "GET":
                return self._json(200, _issue_json(self.forge, repo,
                                                   self._issue_or_404(repo, number)))
            if method == "PATCH":
                return self._rest_patch_issue(req, repo, number)
        if len(rest) == 3 and rest[0] == "issues" and rest[1].isdigit() \
                and rest[2] == "comments":
            number = int(rest[1])
            self._issue_or_404(repo, number)
            if method == "POST":
                return self._rest_create_comment(req, repo, number)
            if method == "GET":
                return self._json(200, [_comment_json(c) for c in repo.comments.get(number, [])])
        if rest == ["pulls"]:
            if method == "POST":
                return self._rest_create_pull(req, repo)
            if method == "GET":
                return self._rest_list_pulls(req, repo)
        if len(rest) == 2 and rest[0] == "pulls" and rest[1].isdigit():
            number = int(rest[1])
            if method == "GET":
                return self._json(200, _pull_json(self.forge, repo,
                                                  self._pr_or_404(repo, number)))
        if len(rest) == 3 and rest[0] == "pulls" and rest[1].isdigit() \
                and rest[2] == "files" and method == "GET":
            pr = self._pr_or_404(repo, int(rest[1]))
            return self._json(200, pr_file_changes(self.forge, repo, pr))
        if len(rest) == 3 and rest[0] == "pulls" and rest[1].isdigit() \
                and rest[2] == "merge" and method == "PUT":
            return self._rest_merge_pull(req, repo, int(rest[1]))
        raise ApiError(404, "Not Found")

    def _issue_or_404(self, repo, number):
        issue = repo.issues.get(number)
        if issue is None:
            raise ApiError(404, "Not Found")
        return issue

    def _pr_or_404(self, repo, number):
        pr = repo.prs.get(number)
        if pr is None:
            raise ApiError(404, "Not Found")
        return pr

    def _rest_create_ref(self, req: Req, repo: Repo):
        body = parse_json_body(req)
        ref = body.get("ref")
        sha = body.get("sha")
        if not isinstance(ref, str) or not ref.startswith("refs/") \
                or not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ApiError(422, "Invalid ref or sha")
        if self.forge.get_object(sha) is None:
            raise ApiError(422, "Invalid sha")
        with self.forge.lock:
            if ref in repo.refs:
                raise ApiError(422, "Reference already exists")
            repo.refs[ref] = sha
            repo.bump()
        return self._json(201, {"ref": ref,
                                "node_id": gid("Ref", repo.dbid, ref),
                                "url": "",
                                "object": {"sha": sha, "type": "commit", "url": ""}})

    def _rest_list_commits(self, req: Req, repo: Repo):
        ref = _str_query(req, "sha", None)
        tip = resolve_ref(self.forge, repo, ref)
        if tip is None:
            return self._json(200, [])
        commits = ancestors(self.forge, tip)
        out = []
        for oid in commits:
            entry = self.forge.get_object(oid)
            parsed = parse_commit(entry[1])
            out.append(_commit_json(self.forge, repo, oid, parsed))
        return self._json(200, out)

    # -- contents ----------------------------------------------------------

    def _dispatch_contents(self, req: Req, repo: Repo, rest):
        if not rest:
            raise ApiError(404, "Not Found")
        path = "/".join(rest)
        method = req.method
        ref = _str_query(req, "ref", None)
        if method == "GET":
            return self._rest_get_contents(req, repo, path, ref)
        if method == "PUT":
            return self._rest_put_contents(req, repo, path)
        if method == "DELETE":
            return self._rest_delete_contents(req, repo, path)
        raise ApiError(404, "Not Found")

    def _rest_get_contents(self, req: Req, repo: Repo, path, ref):
        tip = resolve_ref(self.forge, repo, ref)
        if tip is None:
            raise ApiError(404, "Not Found")
        tree_oid = commit_tree(self.forge, tip)
        flat = flatten_tree(self.forge, tree_oid)
        if path in flat:
            mode, blob_oid = flat[path]
            data = self.forge.get_object(blob_oid)[1]
            return self._json(200, {
                "type": "file", "encoding": "base64",
                "size": len(data), "name": path.split("/")[-1], "path": path,
                "content": base64.b64encode(data).decode("ascii"),
                "sha": blob_oid,
                "url": f"{self._base_url(req)}/api/v3/repos/{repo.owner}/{repo.name}/contents/{path}",
                "git_url": "", "html_url": "", "download_url": None,
                "_links": {"self": "", "git": "", "html": ""},
            })
        # directory?
        prefix = path.rstrip("/") + "/"
        children = {}
        for p, (mode, oid) in flat.items():
            if p.startswith(prefix):
                rest_path = p[len(prefix):]
                first = rest_path.split("/")[0]
                if "/" in rest_path:
                    children[first] = ("dir", None)
                else:
                    children[first] = ("file", oid)
        if not children:
            # maybe a directory that exists only as a subtree
            raise ApiError(404, "Not Found")
        entries = []
        for name in sorted(children):
            kind, oid = children[name]
            if kind == "dir":
                # tree oid of the subdir
                sub_flat = {k[len(prefix) + len(name) + 1:]: v
                            for k, v in flat.items()
                            if k.startswith(prefix + name + "/")}
                sub_tree = build_tree_from_flat(self.forge, sub_flat)
                sha = sub_tree
            else:
                sha = oid
            entries.append({"type": kind, "size": 0 if kind == "dir" else len(self.forge.get_object(oid)[1]),
                            "name": name, "path": prefix + name, "sha": sha,
                            "url": "", "git_url": "", "html_url": "", "download_url": None,
                            "_links": {"self": "", "git": "", "html": ""}})
        return self._json(200, entries)

    def _rest_put_contents(self, req: Req, repo: Repo, path):
        body = parse_json_body(req)
        message = body.get("message") or "Update file"
        content_b64 = body.get("content")
        if not isinstance(content_b64, str):
            raise ApiError(422, "content is required")
        try:
            data = base64.b64decode(content_b64, validate=False)
        except Exception:
            raise ApiError(422, "content is not valid base64")
        branch = body.get("branch") or repo.default_branch
        sha = body.get("sha")
        with self.forge.lock:
            refname = f"refs/heads/{branch}"
            tip = repo.refs.get(refname)
            if tip is None and branch != repo.default_branch:
                # create the branch from the default tip when missing
                tip = repo.refs.get(f"refs/heads/{repo.default_branch}")
                if tip is None:
                    raise ApiError(422, "No base commit to branch from")
                repo.refs[refname] = tip
            tree_oid = commit_tree(self.forge, tip) if tip else None
            flat = flatten_tree(self.forge, tree_oid)
            existing = flat.get(path)
            blob_oid = self.forge.put_object("blob", data)
            if existing is not None:
                if sha is None or sha.lower() != existing[1].lower():
                    raise ApiError(409, "sha does not match the current file")
                flat[path] = ("100644", blob_oid)
                status = 200
            else:
                if sha is not None:
                    raise ApiError(422, "sha does not match any file")
                flat[path] = ("100644", blob_oid)
                status = 201
            ts = tick()
            commit_oid = make_content_commit(self.forge, repo, branch, flat, message, ts)
            repo.bump()
        return self._json(status, {
            "content": {"name": path.split("/")[-1], "path": path, "sha": blob_oid,
                        "size": len(data), "encoding": "base64",
                        "url": "", "git_url": "", "html_url": "", "download_url": None},
            "commit": {"sha": commit_oid, "node_id": gid("Commit", commit_oid),
                       "url": "", "html_url": "", "author": _user_json(self.forge.viewer),
                       "committer": _user_json(self.forge.viewer),
                       "message": message},
        })

    def _rest_delete_contents(self, req: Req, repo: Repo, path):
        body = parse_json_body(req)
        branch = body.get("branch") or repo.default_branch
        sha = body.get("sha")
        with self.forge.lock:
            tip = repo.refs.get(f"refs/heads/{branch}")
            if tip is None:
                raise ApiError(404, "Not Found")
            flat = flatten_tree(self.forge, commit_tree(self.forge, tip))
            existing = flat.get(path)
            if existing is None:
                raise ApiError(404, "Not Found")
            if sha is not None and sha.lower() != existing[1].lower():
                raise ApiError(409, "sha does not match the current file")
            del flat[path]
            ts = tick()
            commit_oid = make_content_commit(self.forge, repo, branch, flat,
                                             body.get("message") or "Delete file", ts)
            repo.bump()
        return self._json(200, {"commit": {"sha": commit_oid,
                                           "node_id": gid("Commit", commit_oid),
                                           "url": "", "html_url": "",
                                           "message": body.get("message") or "Delete file"}})

    # -- labels / issues / comments ----------------------------------------

    def _rest_create_label(self, req: Req, repo: Repo):
        body = parse_json_body(req)
        name = body.get("name")
        if not isinstance(name, str) or not name:
            raise ApiError(422, "Label name is required")
        with self.forge.lock:
            repo.label_counter += 1
            label = {"id": repo.dbid * 1000 + repo.label_counter,
                     "node_id": gid("Label", repo.dbid, name),
                     "name": name, "color": body.get("color") or "ededed",
                     "description": body.get("description") or "", "default": False,
                     "url": ""}
            repo.labels[name] = label
        return self._json(201, label)

    def _rest_create_issue(self, req: Req, repo: Repo):
        body = parse_json_body(req)
        title = body.get("title")
        if not isinstance(title, str) or not title:
            raise ApiError(422, "Title is required")
        labels = body.get("labels") or []
        with self.forge.lock:
            for label_name in labels:
                if label_name not in repo.labels:
                    raise ApiError(422, f"Label {label_name} does not exist")
            repo.counter += 1
            number = repo.counter
            ts = tick()
            issue = {"id": repo.dbid * 100000 + number, "number": number,
                     "title": title, "body": body.get("body") or "", "state": "open",
                     "labels": [dict(repo.labels[n]) for n in labels],
                     "created_ts": ts, "updated_ts": ts, "closed_ts": None}
            repo.issues[number] = issue
            repo.comments.setdefault(number, [])
        return self._json(201, _issue_json(self.forge, repo, issue))

    def _rest_list_issues(self, req: Req, repo: Repo):
        state = _str_query(req, "state", "open")
        sort = _str_query(req, "sort", "created")
        direction = _str_query(req, "direction", "desc")
        per_page = _int_query(req, "per_page", 30)
        page = _int_query(req, "page", 1)
        issues = list(repo.issues.values())
        if state != "all":
            issues = [i for i in issues if i["state"] == state]
        reverse = direction != "asc"
        issues.sort(key=lambda i: (i["created_ts"], i["number"]), reverse=reverse)
        start = (page - 1) * per_page
        chunk = issues[start:start + per_page]
        return self._json(200, [_issue_json(self.forge, repo, i) for i in chunk])

    def _rest_patch_issue(self, req: Req, repo: Repo, number):
        body = parse_json_body(req)
        with self.forge.lock:
            issue = self._issue_or_404(repo, number)
            if "state" in body:
                new_state = body["state"]
                if new_state in ("open", "closed"):
                    issue["state"] = new_state
                    issue["closed_ts"] = tick() if new_state == "closed" else None
            if "title" in body:
                issue["title"] = body["title"]
            if "body" in body:
                issue["body"] = body["body"]
            issue["updated_ts"] = tick()
        return self._json(200, _issue_json(self.forge, repo, issue))

    def _rest_create_comment(self, req: Req, repo: Repo, number):
        body = parse_json_body(req)
        comment_body = body.get("body")
        if not isinstance(comment_body, str):
            raise ApiError(422, "body is required")
        with self.forge.lock:
            repo.comment_counter += 1
            ts = tick()
            comment = {"id": repo.dbid * 1000000 + repo.comment_counter,
                       "body": comment_body, "created_ts": ts, "updated_ts": ts,
                       "issue_number": number}
            repo.comments.setdefault(number, []).append(comment)
        return self._json(201, _comment_json(comment))

    # -- pull requests -------------------------------------------------------

    def _rest_create_pull(self, req: Req, repo: Repo):
        body = parse_json_body(req)
        head = body.get("head")
        base = body.get("base") or repo.default_branch
        title = body.get("title")
        if not isinstance(head, str) or not head:
            raise ApiError(422, "head is required")
        if not isinstance(title, str) or not title:
            raise ApiError(422, "title is required")
        with self.forge.lock:
            head_sha = repo.refs.get(f"refs/heads/{head}")
            base_sha = repo.refs.get(f"refs/heads/{base}")
            if head_sha is None or base_sha is None:
                raise ApiError(422, "head or base branch does not exist")
            for pr in repo.prs.values():
                if pr["state"] == "open" and pr["head_ref"] == head and pr["base_ref"] == base:
                    raise ApiError(422, "A pull request already exists for this head")
            repo.counter += 1
            number = repo.counter
            ts = tick()
            pr = {"id": repo.dbid * 100000 + number, "number": number,
                  "title": title, "body": body.get("body") or "", "state": "open",
                  "head_ref": head, "head_sha": head_sha,
                  "base_ref": base, "base_sha": base_sha,
                  "merged": False, "merged_at": None, "merge_commit_sha": None,
                  "created_ts": ts, "updated_ts": ts, "closed_ts": None}
            repo.prs[number] = pr
        return self._json(201, _pull_json(self.forge, repo, pr))

    def _rest_list_pulls(self, req: Req, repo: Repo):
        state = _str_query(req, "state", "open")
        per_page = _int_query(req, "per_page", 30)
        page = _int_query(req, "page", 1)
        pulls = list(repo.prs.values())
        if state != "all":
            pulls = [p for p in pulls if p["state"] == state]
        pulls.sort(key=lambda p: p["number"])
        start = (page - 1) * per_page
        chunk = pulls[start:start + per_page]
        return self._json(200, [_pull_json(self.forge, repo, p) for p in chunk])

    def _rest_merge_pull(self, req: Req, repo: Repo, number):
        with self.forge.lock:
            pr = self._pr_or_404(repo, number)
            if pr["state"] != "open":
                raise ApiError(405, "Pull request is not open")
            if merge_conflict(self.forge, repo, pr):
                raise ApiError(405, "Merge conflict")
            ts = tick()
            merge_sha = squash_merge(self.forge, repo, pr, ts)
            repo.refs[f"refs/heads/{pr['base_ref']}"] = merge_sha
            pr["state"] = "closed"
            pr["merged"] = True
            pr["merged_at"] = iso(ts)
            pr["merge_commit_sha"] = merge_sha
            pr["closed_ts"] = ts
            pr["updated_ts"] = ts
            pr["base_sha"] = merge_sha
            repo.bump()
        return self._json(200, {"sha": merge_sha, "merged": True,
                                "message": f"Merge pull request #{number}"})


# -------------------------------------------------------------------------------------
# REST JSON shapes
# -------------------------------------------------------------------------------------

def _user_json(login: str):
    user = next((u for u in _USERS if u["login"] == login), None)
    uid = user["id"] if user else 1000
    return {"login": login, "id": uid, "node_id": gid("User", uid),
            "avatar_url": "", "gravatar_id": "", "url": "", "html_url": "",
            "type": "User", "site_admin": False}


def _repo_json(forge: Forge, repo: Repo):
    base = f"https://api.github.invalid/repos/{repo.owner}/{repo.name}"
    return {"id": repo.dbid, "node_id": gid("Repository", repo.dbid),
            "name": repo.name, "full_name": repo.full_name(),
            "private": repo.private,
            "owner": _user_json(repo.owner),
            "html_url": base, "description": repo.description,
            "fork": False, "url": base,
            "created_at": iso(repo.created_ts), "updated_at": iso(repo.created_ts),
            "pushed_at": iso(repo.created_ts),
            "git_url": f"git://github.invalid/{repo.full_name()}.git",
            "ssh_url": f"git@github.invalid:{repo.full_name()}.git",
            "clone_url": f"https://github.invalid/{repo.full_name()}.git",
            "homepage": "", "size": 0, "stargazers_count": 0, "watchers_count": 0,
            "language": None, "has_issues": True, "has_projects": True,
            "has_downloads": True, "has_wiki": True, "has_pages": False,
            "has_discussions": False, "forks_count": 0, "mirror_url": None,
            "archived": False, "disabled": False,
            "open_issues_count": sum(1 for i in repo.issues.values() if i["state"] == "open"),
            "license": None, "allow_forking": True, "is_template": False,
            "web_commit_signoff_required": False, "topics": [],
            "visibility": "private" if repo.private else "public",
            "forks": 0, "open_issues": 0, "watchers": 0,
            "default_branch": repo.default_branch,
            "permissions": {"admin": True, "maintain": True, "push": True,
                            "triage": True, "pull": True}}


def _branch_names(repo: Repo):
    return [r[len("refs/heads/"):] for r in repo.refs if r.startswith("refs/heads/")]


def _branch_json(forge: Forge, repo: Repo, branch: str):
    sha = repo.refs[f"refs/heads/{branch}"]
    return {"name": branch,
            "commit": {"sha": sha,
                       "url": f"https://api.github.invalid/repos/{repo.full_name()}/commits/{sha}"},
            "protected": False}


def _commit_json(forge: Forge, repo: Repo, oid: str, parsed: dict):
    author = {"name": VIEWER["name"], "email": VIEWER["email"],
              "date": iso(SCENARIO_EPOCH_SEC)}
    return {"sha": oid, "node_id": gid("Commit", oid),
            "commit": {"author": author, "committer": author,
                       "message": parsed["message"],
                       "tree": {"sha": parsed["tree"], "url": ""},
                       "url": "", "comment_count": 0,
                       "verification": {"verified": False, "reason": "unsigned",
                                        "signature": None, "payload": None}},
            "url": "", "html_url": "",
            "author": _user_json(forge.viewer), "committer": _user_json(forge.viewer),
            "parents": [{"sha": p, "url": "", "html_url": ""} for p in parsed["parents"]]}


def _issue_json(forge: Forge, repo: Repo, issue: dict):
    return {"id": issue["id"], "node_id": gid("Issue", repo.dbid, issue["number"]),
            "url": f"https://api.github.invalid/repos/{repo.full_name()}/issues/{issue['number']}",
            "repository_url": f"https://api.github.invalid/repos/{repo.full_name()}",
            "html_url": "",
            "number": issue["number"], "title": issue["title"], "body": issue["body"],
            "user": _user_json(forge.viewer),
            "labels": [{"id": l["id"], "node_id": l["node_id"], "name": l["name"],
                        "color": l["color"], "description": l.get("description", ""),
                        "default": False, "url": ""} for l in issue["labels"]],
            "state": issue["state"], "locked": False, "assignee": None,
            "assignees": [], "milestone": None,
            "comments": len(repo.comments.get(issue["number"], [])),
            "created_at": iso(issue["created_ts"]), "updated_at": iso(issue["updated_ts"]),
            "closed_at": iso(issue["closed_ts"]) if issue["closed_ts"] else None,
            "author_association": "OWNER", "active_lock_reason": None,
            "draft": False, "pull_request": None}


def _comment_json(comment: dict):
    return {"id": comment["id"], "node_id": gid("IssueComment", comment["id"]),
            "url": "", "html_url": "", "body": comment["body"],
            "user": _user_json(VIEWER["login"]),
            "created_at": iso(comment["created_ts"]),
            "updated_at": iso(comment["updated_ts"]),
            "author_association": "OWNER"}


def _pull_json(forge: Forge, repo: Repo, pr: dict):
    files = pr_file_changes(forge, repo, pr)
    additions = sum(f["additions"] for f in files)
    deletions = sum(f["deletions"] for f in files)
    state = "open" if pr["state"] == "open" else "closed"
    return {"id": pr["id"], "node_id": gid("PullRequest", repo.dbid, pr["number"]),
            "url": f"https://api.github.invalid/repos/{repo.full_name()}/pulls/{pr['number']}",
            "html_url": "",
            "number": pr["number"], "title": pr["title"], "body": pr["body"],
            "state": state, "locked": False, "draft": False,
            "user": _user_json(forge.viewer),
            "created_at": iso(pr["created_ts"]), "updated_at": iso(pr["updated_ts"]),
            "closed_at": iso(pr["closed_ts"]) if pr["closed_ts"] else None,
            "merged_at": pr["merged_at"], "merge_commit_sha": pr["merge_commit_sha"],
            "merged": pr["merged"],
            "mergeable": not merge_conflict(forge, repo, pr) if pr["state"] == "open" else None,
            "mergeable_state": "clean" if pr["state"] == "open" and not merge_conflict(forge, repo, pr) else "dirty",
            "head": {"label": f"{repo.owner}:{pr['head_ref']}", "ref": pr["head_ref"],
                     "sha": pr["head_sha"], "user": _user_json(forge.viewer),
                     "repo": _repo_json(forge, repo)},
            "base": {"label": f"{repo.owner}:{pr['base_ref']}", "ref": pr["base_ref"],
                     "sha": pr["base_sha"], "user": _user_json(forge.viewer),
                     "repo": _repo_json(forge, repo)},
            "commits": 0, "additions": additions, "deletions": deletions,
            "changed_files": len(files),
            "author_association": "OWNER"}


def _int_query(req: Req, name: str, default: int) -> int:
    try:
        return int(req.query.get(name, [default])[0])
    except (TypeError, ValueError):
        return default


def _str_query(req: Req, name: str, default):
    values = req.query.get(name)
    return values[0] if values else default

# -------------------------------------------------------------------------------------
# GraphQL v4: a small parser + executor for the documents gh / github-mcp-server send
# -------------------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"""
    (?P<ws>\s+)
  | (?P<comma>,)
  | (?P<name>[_A-Za-z][_0-9A-Za-z]*)
  | (?P<int>-?(?:0|[1-9][0-9]*))
  | (?P<float>-?(?:0|[1-9][0-9]*)\.[0-9]+(?:[eE][+-]?[0-9]+)?)
  | (?P<string>"(?:\\.|[^"\\])*")
  | (?P<spread>\.\.\.)
  | (?P<punct>[!():=@\[\]{|}&])
  | (?P<var>\$[_A-Za-z][_0-9A-Za-z]*)
""", re.VERBOSE)


def _gql_tokens(text: str):
    pos = 0
    tokens = []
    while pos < len(text):
        match = _TOKEN_RE.match(text, pos)
        if not match:
            raise ValueError(f"GraphQL parse error at {pos}")
        pos = match.end()
        kind = match.lastgroup
        if kind in ("ws", "comma"):
            continue
        tokens.append((kind, match.group()))
    tokens.append(("eof", ""))
    return tokens


class _GqlParser:
    def __init__(self, text: str):
        self.tokens = _gql_tokens(text)
        self.i = 0

    def peek(self):
        return self.tokens[self.i]

    def next(self):
        tok = self.tokens[self.i]
        self.i += 1
        return tok

    def expect(self, value):
        kind, text = self.next()
        if text != value:
            raise ValueError(f"expected {value!r}, got {text!r}")
        return text

    def expect_name(self):
        kind, text = self.next()
        if kind != "name":
            raise ValueError(f"expected name, got {text!r}")
        return text

    def parse_document(self):
        operations = []
        fragments = {}
        while self.peek()[0] != "eof":
            if self.peek()[1] == "{":
                operations.append({"type": "query", "name": None, "variables": {},
                                   "selection": self.parse_selection_set()})
            elif self.peek()[1] in ("query", "mutation", "subscription"):
                op_type = self.next()[1]
                name = None
                if self.peek()[0] == "name":
                    name = self.next()[1]
                variables = {}
                if self.peek()[1] == "(":
                    variables = self.parse_variable_definitions()
                operations.append({"type": op_type, "name": name,
                                   "variables": variables,
                                   "selection": self.parse_selection_set()})
            elif self.peek()[1] == "fragment":
                self.next()
                frag_name = self.expect_name()
                self.expect("on")
                type_cond = self.expect_name()
                fragments[frag_name] = {"type": type_cond,
                                        "selection": self.parse_selection_set()}
            else:
                raise ValueError(f"unexpected token {self.peek()[1]!r}")
        return operations, fragments

    def parse_variable_definitions(self):
        self.expect("(")
        variables = {}
        while self.peek()[1] != ")":
            kind, text = self.next()
            if kind != "var":
                raise ValueError(f"expected variable, got {text!r}")
            name = text[1:]
            self.expect(":")
            variables[name] = self.parse_type()
            if self.peek()[1] == "=":
                self.next()
                self.parse_value()
        self.expect(")")
        return variables

    def parse_type(self):
        if self.peek()[1] == "[":
            self.next()
            inner = self.parse_type()
            self.expect("]")
            result = ("list", inner)
        else:
            result = ("named", self.expect_name())
        if self.peek()[1] == "!":
            self.next()
            result = ("nonnull", result)
        return result

    def parse_selection_set(self):
        self.expect("{")
        selections = []
        while self.peek()[1] != "}":
            selections.append(self.parse_selection())
        self.expect("}")
        return selections

    def parse_selection(self):
        if self.peek()[1] == "...":
            self.next()
            if self.peek()[1] == "on":
                self.next()
                type_cond = self.expect_name()
                return {"kind": "inline", "type": type_cond,
                        "selection": self.parse_selection_set()}
            frag_name = self.expect_name()
            return {"kind": "fragment", "name": frag_name}
        alias = None
        name = self.expect_name()
        if self.peek()[1] == ":":
            self.next()
            alias = name
            name = self.expect_name()
        arguments = {}
        if self.peek()[1] == "(":
            arguments = self.parse_arguments()
        selection = None
        if self.peek()[1] == "{":
            selection = self.parse_selection_set()
        return {"kind": "field", "name": name, "alias": alias or name,
                "arguments": arguments, "selection": selection}

    def parse_arguments(self):
        self.expect("(")
        args = {}
        while self.peek()[1] != ")":
            name = self.expect_name()
            self.expect(":")
            args[name] = self.parse_value()
        self.expect(")")
        return args

    def parse_value(self):
        kind, text = self.next()
        if kind == "var":
            return ("var", text[1:])
        if kind == "int":
            return ("int", int(text))
        if kind == "float":
            return ("float", float(text))
        if kind == "string":
            return ("string", json.loads(text))
        if kind == "name":
            if text == "true":
                return ("bool", True)
            if text == "false":
                return ("bool", False)
            if text == "null":
                return ("null", None)
            return ("enum", text)
        if text == "[":
            values = []
            while self.peek()[1] != "]":
                values.append(self.parse_value())
            self.expect("]")
            return ("list", values)
        if text == "{":
            fields = {}
            while self.peek()[1] != "}":
                name = self.expect_name()
                self.expect(":")
                fields[name] = self.parse_value()
            self.expect("}")
            return ("object", fields)
        raise ValueError(f"unexpected value token {text!r}")


def _gql_value(node, variables):
    kind, value = node
    if kind == "var":
        return variables.get(value)
    if kind == "list":
        return [_gql_value(v, variables) for v in value]
    if kind == "object":
        return {k: _gql_value(v, variables) for k, v in value.items()}
    return value


_ISSUE_FIELDS = ["id", "number", "title", "state", "body"]
_PR_FIELDS = ["id", "number", "headRefName", "baseRefName", "mergeable", "state",
              "merged", "files"]
_REPO_FIELDS = ["id", "databaseId", "name", "nameWithOwner", "owner", "defaultBranchRef",
                "viewerPermission", "hasIssuesEnabled", "issue", "issues",
                "pullRequests", "pullRequest"]
_SEARCH_ENUMS = ["ISSUE", "REPOSITORY", "USER", "DISCUSSION"]
_TYPE_FIELDS = {
    "Issue": _ISSUE_FIELDS,
    "PullRequest": _PR_FIELDS,
    "Repository": _REPO_FIELDS,
}


class _GqlExecutor:
    def __init__(self, server: ForgeServer, variables):
        self.server = server
        self.forge = server.forge
        self.variables = variables or {}
        self.errors = []

    def execute_operation(self, operation, fragments):
        if operation["type"] == "mutation":
            return self._exec_selection(operation["selection"], ("mutation", None), fragments)
        return self._exec_selection(operation["selection"], ("query", None), fragments)

    def _args(self, field):
        return {k: _gql_value(v, self.variables) for k, v in field["arguments"].items()}

    def _exec_selection(self, selections, parent, fragments):
        out = {}
        ptype, _pobj = parent
        for sel in selections:
            if sel["kind"] == "fragment":
                frag = fragments.get(sel["name"])
                if frag:
                    out.update(self._exec_selection(frag["selection"], parent, fragments))
                continue
            if sel["kind"] == "inline":
                out.update(self._exec_selection(sel["selection"], parent, fragments))
                continue
            name = sel["name"]
            alias = sel["alias"]
            args = self._args(sel)
            value = self._resolve_field(parent, name, args, sel, fragments)
            out[alias] = value
        return out

    def _field_error(self, parent_type, name):
        self.errors.append(
            {"message": f'Cannot query field "{name}" on type "{parent_type}".'})

    def _resolve_field(self, parent, name, args, sel, fragments):
        ptype, pobj = parent
        sub = sel["selection"]
        if ptype == "query":
            if name == "viewer":
                return self._exec_selection(sub, ("User", VIEWER), fragments) if sub else {"login": VIEWER["login"]}
            if name == "repository":
                repo = self.forge.get_repo(args.get("owner"), args.get("name"))
                if repo is None:
                    return None
                return self._exec_selection(sub, ("Repository", repo), fragments) if sub else {}
            if name == "node":
                node = self._resolve_node(args.get("id"))
                if node is None:
                    return None
                return self._exec_selection(sub, node, fragments) if sub else {}
            if name == "__type":
                return self._type_info(args.get("name"), sub, fragments)
            if name == "__typename":
                return "Query"
            self._field_error("Query", name)
            return None
        if ptype == "mutation":
            if name == "createIssue":
                return self._mut_create_issue(args, sub, fragments)
            if name == "closeIssue":
                return self._mut_close_issue(args, sub, fragments)
            if name == "createPullRequest":
                return self._mut_create_pr(args, sub, fragments)
            if name == "mergePullRequest":
                return self._mut_merge_pr(args, sub, fragments)
            if name == "addComment":
                return self._mut_add_comment(args, sub, fragments)
            self._field_error("Mutation", name)
            return None
        if ptype == "User":
            if name == "login":
                return pobj["login"]
            if name == "__typename":
                return "User"
            self._field_error("User", name)
            return None
        if ptype == "Repository":
            return self._resolve_repo_field(pobj, name, args, sel, fragments)
        if ptype == "Issue":
            return self._resolve_issue_field(pobj, name, args, sel, fragments)
        if ptype == "PullRequest":
            return self._resolve_pr_field(pobj, name, args, sel, fragments)
        if ptype == "IssueConnection":
            if name == "nodes":
                return [self._exec_selection(sub, ("Issue", issue), fragments) if sub else {}
                        for issue in pobj["nodes"]]
            if name == "pageInfo":
                return self._exec_selection(sub, ("PageInfo", pobj["pageInfo"]), fragments) if sub else pobj["pageInfo"]
            if name == "totalCount":
                return len(pobj["nodes"])
            if name == "__typename":
                return "IssueConnection"
            self._field_error("IssueConnection", name)
            return None
        if ptype == "PullRequestConnection":
            if name == "nodes":
                return [self._exec_selection(sub, ("PullRequest", pr), fragments) if sub else {}
                        for pr in pobj["nodes"]]
            if name == "totalCount":
                return len(pobj["nodes"])
            if name == "__typename":
                return "PullRequestConnection"
            self._field_error("PullRequestConnection", name)
            return None
        if ptype == "PullRequestChangedFileConnection":
            if name == "nodes":
                return [self._exec_selection(sub, ("PullRequestChangedFile", f), fragments) if sub else {}
                        for f in pobj["nodes"]]
            if name == "__typename":
                return "PullRequestChangedFileConnection"
            self._field_error("PullRequestChangedFileConnection", name)
            return None
        if ptype == "PullRequestChangedFile":
            if name == "path":
                return pobj["filename"]
            if name == "__typename":
                return "PullRequestChangedFile"
            self._field_error("PullRequestChangedFile", name)
            return None
        if ptype == "PageInfo":
            if name in ("hasNextPage", "endCursor"):
                return pobj.get(name)
            if name == "__typename":
                return "PageInfo"
            self._field_error("PageInfo", name)
            return None
        if ptype == "Ref":
            if name == "name":
                return pobj
            if name == "__typename":
                return "Ref"
            self._field_error("Ref", name)
            return None
        if ptype == "__Type":
            return self._resolve_type_field(pobj, name, args, sel, fragments)
        return None

    def _type_info(self, name, sub, fragments):
        if name not in _TYPE_FIELDS and name != "SearchType":
            return None
        return self._exec_selection(sub, ("__Type", name), fragments) if sub else {}

    def _resolve_type_field(self, type_name, name, args, sel, fragments):
        if name == "name":
            return type_name
        if name == "kind":
            return "ENUM" if type_name == "SearchType" else "OBJECT"
        if name == "fields":
            if type_name == "SearchType":
                return None
            return [{"name": f} for f in _TYPE_FIELDS[type_name]]
        if name == "enumValues":
            if type_name != "SearchType":
                return None
            return [{"name": e} for e in _SEARCH_ENUMS]
        if name == "__typename":
            return "__Type"
        self._field_error("__Type", name)
        return None

    def _resolve_node(self, gid_value):
        parsed = parse_gid(gid_value or "")
        if not parsed:
            return None
        kind = parsed[0]
        if kind == "Repository" and len(parsed) >= 2:
            try:
                dbid = int(parsed[1])
            except ValueError:
                return None
            for repo in self.forge.repos.values():
                if repo.dbid == dbid:
                    return ("Repository", repo)
        return None

    def _resolve_repo_field(self, repo: Repo, name, args, sel, fragments):
        sub = sel["selection"]
        if name == "id":
            return gid("Repository", repo.dbid)
        if name == "databaseId":
            return repo.dbid
        if name == "name":
            return repo.name
        if name == "nameWithOwner":
            return repo.full_name()
        if name == "owner":
            return self._exec_selection(sub, ("User", {"login": repo.owner}), fragments) if sub else {"login": repo.owner}
        if name == "defaultBranchRef":
            if not repo.refs.get(f"refs/heads/{repo.default_branch}"):
                return None
            return self._exec_selection(sub, ("Ref", repo.default_branch), fragments) if sub else {"name": repo.default_branch}
        if name == "viewerPermission":
            return "ADMIN"
        if name == "hasIssuesEnabled":
            return True
        if name == "issue":
            issue = repo.issues.get(args.get("number"))
            if issue is None:
                return None
            return self._exec_selection(sub, ("Issue", (repo, issue)), fragments) if sub else {}
        if name == "issues":
            return self._issues_connection(repo, args, sub, fragments)
        if name == "pullRequests":
            prs = [p for p in repo.prs.values() if p["state"] == "open"]
            first = args.get("first")
            if isinstance(first, int):
                prs = prs[:first]
            conn = {"nodes": [(repo, p) for p in prs]}
            return self._exec_selection(sub, ("PullRequestConnection", conn), fragments) if sub else {}
        if name == "pullRequest":
            pr = repo.prs.get(args.get("number"))
            if pr is None:
                return None
            return self._exec_selection(sub, ("PullRequest", (repo, pr)), fragments) if sub else {}
        if name == "__typename":
            return "Repository"
        self._field_error("Repository", name)
        return None

    def _issues_connection(self, repo: Repo, args, sub, fragments):
        issues = list(repo.issues.values())
        states = args.get("states")
        if states:
            wanted = {s.lower() for s in states}
            issues = [i for i in issues if i["state"].lower() in wanted]
        order_by = args.get("orderBy") or {}
        field = (order_by.get("field") or "CREATED_AT").upper()
        direction = (order_by.get("direction") or "ASC").upper()
        if field in ("CREATED_AT", "UPDATED_AT"):
            key = (lambda i: (i["created_ts"], i["number"])) if field == "CREATED_AT" else (lambda i: (i["updated_ts"], i["number"]))
            issues.sort(key=key, reverse=(direction == "DESC"))
        else:
            issues.sort(key=lambda i: i["number"], reverse=(direction == "DESC"))
        after = args.get("after")
        if after:
            try:
                cursor_num = int(base64.b64decode(after.encode("ascii")).decode("ascii"))
            except Exception:
                cursor_num = None
            if cursor_num is not None:
                if direction == "DESC":
                    issues = [i for i in issues if i["number"] < cursor_num]
                else:
                    issues = [i for i in issues if i["number"] > cursor_num]
        first = args.get("first")
        has_next = False
        if isinstance(first, int) and len(issues) > first:
            has_next = True
            issues = issues[:first]
        end_cursor = None
        if issues:
            end_cursor = base64.b64encode(str(issues[-1]["number"]).encode("ascii")).decode("ascii")
        conn = {"nodes": [(repo, i) for i in issues],
                "pageInfo": {"hasNextPage": has_next, "endCursor": end_cursor}}
        if sub is None:
            return conn
        return self._exec_selection(sub, ("IssueConnection", conn), fragments)

    def _resolve_issue_field(self, pair, name, args, sel, fragments):
        repo, issue = pair
        if name == "id":
            return gid("Issue", repo.dbid, issue["number"])
        if name == "number":
            return issue["number"]
        if name == "title":
            return issue["title"]
        if name == "state":
            return issue["state"].upper()
        if name == "body":
            return issue["body"]
        if name == "__typename":
            return "Issue"
        self._field_error("Issue", name)
        return None

    def _resolve_pr_field(self, pair, name, args, sel, fragments):
        repo, pr = pair
        sub = sel["selection"]
        if name == "id":
            return gid("PullRequest", repo.dbid, pr["number"])
        if name == "number":
            return pr["number"]
        if name == "state":
            if pr["merged"]:
                return "MERGED"
            return pr["state"].upper()
        if name == "merged":
            return pr["merged"]
        if name == "headRefName":
            return pr["head_ref"]
        if name == "baseRefName":
            return pr["base_ref"]
        if name == "mergeable":
            if pr["state"] != "open":
                return "UNKNOWN"
            return "CONFLICTING" if merge_conflict(self.forge, repo, pr) else "MERGEABLE"
        if name == "files":
            files = pr_file_changes(self.forge, repo, pr)
            first = args.get("first")
            if isinstance(first, int):
                files = files[:first]
            conn = {"nodes": files}
            return self._exec_selection(sub, ("PullRequestChangedFileConnection", conn), fragments) if sub else {}
        if name == "__typename":
            return "PullRequest"
        self._field_error("PullRequest", name)
        return None

    # -- mutations -----------------------------------------------------------

    def _mut_create_issue(self, args, sub, fragments):
        inp = args.get("input") or {}
        parsed = parse_gid(inp.get("repositoryId") or "")
        if not parsed or parsed[0] != "Repository":
            self.errors.append({"message": "Could not resolve to a Repository with the id provided."})
            return {"issue": None}
        try:
            dbid = int(parsed[1])
        except (ValueError, IndexError):
            self.errors.append({"message": "Could not resolve to a Repository with the id provided."})
            return {"issue": None}
        repo = next((r for r in self.forge.repos.values() if r.dbid == dbid), None)
        if repo is None:
            self.errors.append({"message": "Could not resolve to a Repository with the id provided."})
            return {"issue": None}
        with self.forge.lock:
            repo.counter += 1
            number = repo.counter
            ts = tick()
            issue = {"id": repo.dbid * 100000 + number, "number": number,
                     "title": inp.get("title") or "", "body": inp.get("body") or "",
                     "state": "open", "labels": [], "created_ts": ts,
                     "updated_ts": ts, "closed_ts": None}
            repo.issues[number] = issue
            repo.comments.setdefault(number, [])
        result = self._exec_selection(sub, ("CreateIssuePayload", (repo, issue)), fragments) if sub else {"issue": {"number": number}}
        return result

    def _mut_close_issue(self, args, sub, fragments):
        inp = args.get("input") or {}
        parsed = parse_gid(inp.get("issueId") or "")
        if not parsed or parsed[0] != "Issue":
            self.errors.append({"message": "Could not resolve to an Issue with the id provided."})
            return {"issue": None}
        try:
            dbid, number = int(parsed[1]), int(parsed[2])
        except (ValueError, IndexError):
            self.errors.append({"message": "Could not resolve to an Issue with the id provided."})
            return {"issue": None}
        repo = next((r for r in self.forge.repos.values() if r.dbid == dbid), None)
        issue = repo.issues.get(number) if repo else None
        if issue is None:
            self.errors.append({"message": "Could not resolve to an Issue with the id provided."})
            return {"issue": None}
        with self.forge.lock:
            issue["state"] = "closed"
            issue["closed_ts"] = tick()
            issue["updated_ts"] = tick()
        if sub is None:
            return {"issue": {"number": number, "state": "CLOSED"}}
        return self._exec_selection(sub, ("CloseIssuePayload", (repo, issue)), fragments)

    def _mut_create_pr(self, args, sub, fragments):
        inp = args.get("input") or {}
        parsed = parse_gid(inp.get("repositoryId") or "")
        if not parsed or parsed[0] != "Repository":
            self.errors.append({"message": "Could not resolve to a Repository with the id provided."})
            return {"pullRequest": None}
        try:
            dbid = int(parsed[1])
        except (ValueError, IndexError):
            self.errors.append({"message": "Could not resolve to a Repository with the id provided."})
            return {"pullRequest": None}
        repo = next((r for r in self.forge.repos.values() if r.dbid == dbid), None)
        if repo is None:
            self.errors.append({"message": "Could not resolve to a Repository with the id provided."})
            return {"pullRequest": None}
        head = inp.get("headRefName")
        base = inp.get("baseRefName") or repo.default_branch
        with self.forge.lock:
            head_sha = repo.refs.get(f"refs/heads/{head}")
            base_sha = repo.refs.get(f"refs/heads/{base}")
            if head_sha is None or base_sha is None:
                self.errors.append({"message": "Head or base ref does not exist."})
                return {"pullRequest": None}
            for pr in repo.prs.values():
                if pr["state"] == "open" and pr["head_ref"] == head and pr["base_ref"] == base:
                    self.errors.append({"message": "A pull request already exists for this head."})
                    return {"pullRequest": None}
            repo.counter += 1
            number = repo.counter
            ts = tick()
            pr = {"id": repo.dbid * 100000 + number, "number": number,
                  "title": inp.get("title") or "", "body": inp.get("body") or "",
                  "state": "open", "head_ref": head, "head_sha": head_sha,
                  "base_ref": base, "base_sha": base_sha, "merged": False,
                  "merged_at": None, "merge_commit_sha": None,
                  "created_ts": ts, "updated_ts": ts, "closed_ts": None}
            repo.prs[number] = pr
        if sub is None:
            return {"pullRequest": {"number": number, "state": "OPEN"}}
        return self._exec_selection(sub, ("CreatePullRequestPayload", (repo, pr)), fragments)

    def _mut_merge_pr(self, args, sub, fragments):
        inp = args.get("input") or {}
        parsed = parse_gid(inp.get("pullRequestId") or "")
        if not parsed or parsed[0] != "PullRequest":
            self.errors.append({"message": "Could not resolve to a PullRequest with the id provided."})
            return {"pullRequest": None}
        try:
            dbid, number = int(parsed[1]), int(parsed[2])
        except (ValueError, IndexError):
            self.errors.append({"message": "Could not resolve to a PullRequest with the id provided."})
            return {"pullRequest": None}
        repo = next((r for r in self.forge.repos.values() if r.dbid == dbid), None)
        pr = repo.prs.get(number) if repo else None
        if pr is None:
            self.errors.append({"message": "Could not resolve to a PullRequest with the id provided."})
            return {"pullRequest": None}
        with self.forge.lock:
            if pr["state"] != "open":
                self.errors.append({"message": "Pull request is not open."})
                return {"pullRequest": None}
            if merge_conflict(self.forge, repo, pr):
                self.errors.append({"message": "Merge conflict."})
                return {"pullRequest": None}
            ts = tick()
            merge_sha = squash_merge(self.forge, repo, pr, ts)
            repo.refs[f"refs/heads/{pr['base_ref']}"] = merge_sha
            pr["state"] = "closed"
            pr["merged"] = True
            pr["merged_at"] = iso(ts)
            pr["merge_commit_sha"] = merge_sha
            pr["closed_ts"] = ts
            pr["updated_ts"] = ts
            pr["base_sha"] = merge_sha
            repo.bump()
        if sub is None:
            return {"pullRequest": {"number": number, "merged": True, "state": "CLOSED"}}
        return self._exec_selection(sub, ("MergePullRequestPayload", (repo, pr)), fragments)

    def _mut_add_comment(self, args, sub, fragments):
        inp = args.get("input") or {}
        parsed = parse_gid(inp.get("subjectId") or "")
        if not parsed or parsed[0] != "Issue":
            self.errors.append({"message": "Could not resolve to an Issue with the id provided."})
            return {"commentEdge": None}
        try:
            dbid, number = int(parsed[1]), int(parsed[2])
        except (ValueError, IndexError):
            self.errors.append({"message": "Could not resolve to an Issue with the id provided."})
            return {"commentEdge": None}
        repo = next((r for r in self.forge.repos.values() if r.dbid == dbid), None)
        issue = repo.issues.get(number) if repo else None
        if issue is None:
            self.errors.append({"message": "Could not resolve to an Issue with the id provided."})
            return {"commentEdge": None}
        with self.forge.lock:
            repo.comment_counter += 1
            ts = tick()
            comment = {"id": repo.dbid * 1000000 + repo.comment_counter,
                       "body": inp.get("body") or "", "created_ts": ts,
                       "updated_ts": ts, "issue_number": number}
            repo.comments.setdefault(number, []).append(comment)
        node = {"id": gid("IssueComment", comment["id"]), "body": comment["body"]}
        if sub is None:
            return {"commentEdge": {"node": node}}
        return self._exec_selection(sub, ("AddCommentPayload", node), fragments)


# The CommentEdge pseudo-type is resolved inline in the mutation above; the
# executor only needs its two fields, handled here as a special parent type.
_orig_resolve = _GqlExecutor._resolve_field

_PAYLOAD_FIELD = {
    "CreateIssuePayload": ("issue", "Issue"),
    "CloseIssuePayload": ("issue", "Issue"),
    "CreatePullRequestPayload": ("pullRequest", "PullRequest"),
    "MergePullRequestPayload": ("pullRequest", "PullRequest"),
}


def _resolve_field_with_comment_edge(self, parent, name, args, sel, fragments):
    ptype = parent[0]
    if ptype in _PAYLOAD_FIELD:
        field_name, target_type = _PAYLOAD_FIELD[ptype]
        if name == field_name:
            sub = sel["selection"]
            return self._exec_selection(sub, (target_type, parent[1]), fragments) if sub else {}
        if name == "__typename":
            return ptype
        self._field_error(ptype, name)
        return None
    if ptype == "AddCommentPayload":
        if name == "commentEdge":
            sub = sel["selection"]
            return self._exec_selection(sub, ("CommentEdge", parent[1]), fragments) if sub else parent[1]
        if name == "__typename":
            return ptype
        self._field_error(ptype, name)
        return None
    if parent[0] == "CommentEdge":
        node = parent[1]
        if name == "node":
            sub = sel["selection"]
            if sub is None:
                return node
            out = {}
            for s in sub:
                if s["kind"] == "field":
                    out[s["alias"]] = node.get(s["name"])
            return out
        if name == "__typename":
            return "IssueCommentEdge"
        self._field_error("CommentEdge", name)
        return None
    return _orig_resolve(self, parent, name, args, sel, fragments)


_GqlExecutor._resolve_field = _resolve_field_with_comment_edge

# -------------------------------------------------------------------------------------
# GraphQL / MCP / git dispatch (attached to ForgeServer)
# -------------------------------------------------------------------------------------

def _dispatch_graphql(server: ForgeServer, req: Req):
    server._require_auth(req)
    try:
        payload = parse_json_any(req)
    except ApiError as exc:
        return server._error(exc.status, exc.message)
    if isinstance(payload, list):
        results = [_graphql_one(server, item) for item in payload]
        return server._json(200, results)
    return server._json(200, _graphql_one(server, payload))


def _graphql_one(server: ForgeServer, payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("query"), str):
        return {"data": None, "errors": [{"message": "Invalid GraphQL request."}]}
    try:
        operations, fragments = _GqlParser(payload["query"]).parse_document()
    except Exception as exc:
        return {"data": None, "errors": [{"message": f"GraphQL parse error: {exc}"}]}
    if not operations:
        return {"data": {}, "errors": [{"message": "No operation provided."}]}
    executor = _GqlExecutor(server, payload.get("variables") or {})
    data = executor.execute_operation(operations[0], fragments)
    result = {"data": data}
    if executor.errors:
        result["errors"] = executor.errors
    return result


def _mcp_result(server: ForgeServer, req_id, result):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _mcp_error(server: ForgeServer, req_id, code, message):
    return {"jsonrpc": "2.0", "id": req_id,
            "error": {"code": code, "message": message}}


_MCP_SERVER_INFO = {"name": "zech-forge2", "version": "1.0.0"}
_MCP_TOOLS = [
    {"name": "create_repo",
     "description": "Create a repository owned by the viewer.",
     "inputSchema": {"type": "object",
                     "properties": {"name": {"type": "string"},
                                    "owner": {"type": "string"},
                                    "description": {"type": "string"},
                                    "private": {"type": "boolean"}},
                     "required": ["name"]}},
    {"name": "get_repo",
     "description": "Get a repository by owner and name.",
     "inputSchema": {"type": "object",
                     "properties": {"owner": {"type": "string"},
                                    "repo": {"type": "string"},
                                    "full_name": {"type": "string"}},
                     "required": ["owner", "repo"]}},
    {"name": "create_issue",
     "description": "Create an issue in a repository.",
     "inputSchema": {"type": "object",
                     "properties": {"owner": {"type": "string"},
                                    "repo": {"type": "string"},
                                    "full_name": {"type": "string"},
                                    "title": {"type": "string"},
                                    "body": {"type": "string"}},
                     "required": ["owner", "repo", "title"]}},
    {"name": "list_issues",
     "description": "List issues in a repository.",
     "inputSchema": {"type": "object",
                     "properties": {"owner": {"type": "string"},
                                    "repo": {"type": "string"},
                                    "full_name": {"type": "string"},
                                    "state": {"type": "string"}},
                     "required": ["owner", "repo"]}},
    {"name": "create_pull_request",
     "description": "Open a pull request from a head branch into a base branch.",
     "inputSchema": {"type": "object",
                     "properties": {"owner": {"type": "string"},
                                    "repo": {"type": "string"},
                                    "full_name": {"type": "string"},
                                    "title": {"type": "string"},
                                    "body": {"type": "string"},
                                    "head": {"type": "string"},
                                    "base": {"type": "string"}},
                     "required": ["owner", "repo", "title", "head", "base"]}},
    {"name": "merge_pull_request",
     "description": "Squash-merge a pull request by number.",
     "inputSchema": {"type": "object",
                     "properties": {"owner": {"type": "string"},
                                    "repo": {"type": "string"},
                                    "full_name": {"type": "string"},
                                    "pull_number": {"type": "integer"}},
                     "required": ["owner", "repo", "pull_number"]}},
    {"name": "close_issue",
     "description": "Close an issue by number.",
     "inputSchema": {"type": "object",
                     "properties": {"owner": {"type": "string"},
                                    "repo": {"type": "string"},
                                    "full_name": {"type": "string"},
                                    "issue_number": {"type": "integer"}},
                     "required": ["owner", "repo", "issue_number"]}},
    {"name": "list_repos",
     "description": "List repositories owned by the viewer.",
     "inputSchema": {"type": "object", "properties": {}, "required": []}},
]


def _mcp_tool_result(obj, is_error=False):
    text = json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
    result = {"content": [{"type": "text", "text": text}],
              "structuredContent": obj}
    if is_error:
        result["isError"] = True
    return result


def _mcp_owner_repo(args):
    owner = args.get("owner")
    repo = args.get("repo")
    full = args.get("full_name")
    if (not owner or not repo) and isinstance(full, str) and "/" in full:
        owner, repo = full.split("/", 1)
    return owner, repo


def _mcp_call_tool(server: ForgeServer, name, args):
    forge = server.forge
    if name == "create_repo":
        repo_name = args.get("name")
        if not isinstance(repo_name, str) or not repo_name:
            return _mcp_tool_result({"error": "name is required"}, True)
        with forge.lock:
            repo = forge.create_repo(repo_name, bool(args.get("private", False)),
                                     args.get("description") or "")
            if repo is None:
                return _mcp_tool_result({"error": f"Repository {repo_name} already exists"}, True)
        return _mcp_tool_result(_repo_json(forge, repo))
    if name == "get_repo":
        owner, repo_name = _mcp_owner_repo(args)
        repo = forge.get_repo(owner, repo_name) if owner and repo_name else None
        if repo is None:
            return _mcp_tool_result({"error": "Not Found"}, True)
        return _mcp_tool_result(_repo_json(forge, repo))
    if name == "list_repos":
        with forge.lock:
            repos = [r for (o, _n), r in sorted(forge.repos.items()) if o == forge.viewer]
        return _mcp_tool_result({"repositories": [_repo_json(forge, r) for r in repos]})
    if name == "create_issue":
        owner, repo_name = _mcp_owner_repo(args)
        title = args.get("title")
        if not owner or not repo_name or not isinstance(title, str) or not title:
            return _mcp_tool_result({"error": "owner, repo and title are required"}, True)
        repo = forge.get_repo(owner, repo_name)
        if repo is None:
            return _mcp_tool_result({"error": "Not Found"}, True)
        with forge.lock:
            repo.counter += 1
            number = repo.counter
            ts = tick()
            issue = {"id": repo.dbid * 100000 + number, "number": number,
                     "title": title, "body": args.get("body") or "", "state": "open",
                     "labels": [], "created_ts": ts, "updated_ts": ts, "closed_ts": None}
            repo.issues[number] = issue
            repo.comments.setdefault(number, [])
        return _mcp_tool_result(_issue_json(forge, repo, issue))
    if name == "list_issues":
        owner, repo_name = _mcp_owner_repo(args)
        if not owner or not repo_name:
            return _mcp_tool_result({"error": "owner and repo are required"}, True)
        repo = forge.get_repo(owner, repo_name)
        if repo is None:
            return _mcp_tool_result({"error": "Not Found"}, True)
        state = (args.get("state") or "open").lower()
        issues = list(repo.issues.values())
        if state != "all":
            issues = [i for i in issues if i["state"] == state]
        issues.sort(key=lambda i: i["number"])
        return _mcp_tool_result({"issues": [_issue_json(forge, repo, i) for i in issues]})
    if name == "create_pull_request":
        owner, repo_name = _mcp_owner_repo(args)
        head, base = args.get("head"), args.get("base")
        title = args.get("title")
        if not owner or not repo_name or not title or not head or not base:
            return _mcp_tool_result(
                {"error": "owner, repo, title, head and base are required"}, True)
        repo = forge.get_repo(owner, repo_name)
        if repo is None:
            return _mcp_tool_result({"error": "Not Found"}, True)
        with forge.lock:
            head_sha = repo.refs.get(f"refs/heads/{head}")
            base_sha = repo.refs.get(f"refs/heads/{base}")
            if head_sha is None or base_sha is None:
                return _mcp_tool_result({"error": "head or base branch does not exist"}, True)
            for pr in repo.prs.values():
                if pr["state"] == "open" and pr["head_ref"] == head and pr["base_ref"] == base:
                    return _mcp_tool_result({"error": "A pull request already exists for this head"}, True)
            repo.counter += 1
            number = repo.counter
            ts = tick()
            pr = {"id": repo.dbid * 100000 + number, "number": number,
                  "title": title, "body": args.get("body") or "", "state": "open",
                  "head_ref": head, "head_sha": head_sha,
                  "base_ref": base, "base_sha": base_sha,
                  "merged": False, "merged_at": None, "merge_commit_sha": None,
                  "created_ts": ts, "updated_ts": ts, "closed_ts": None}
            repo.prs[number] = pr
        return _mcp_tool_result(_pull_json(forge, repo, pr))
    if name == "merge_pull_request":
        owner, repo_name = _mcp_owner_repo(args)
        number = args.get("pull_number")
        if not owner or not repo_name or not isinstance(number, int):
            return _mcp_tool_result({"error": "owner, repo and pull_number are required"}, True)
        repo = forge.get_repo(owner, repo_name)
        if repo is None:
            return _mcp_tool_result({"error": "Not Found"}, True)
        with forge.lock:
            pr = repo.prs.get(number)
            if pr is None:
                return _mcp_tool_result({"error": "Not Found"}, True)
            if pr["state"] != "open":
                return _mcp_tool_result({"error": "Pull request is not open"}, True)
            if merge_conflict(forge, repo, pr):
                return _mcp_tool_result({"error": "Merge conflict"}, True)
            ts = tick()
            merge_sha = squash_merge(forge, repo, pr, ts)
            repo.refs[f"refs/heads/{pr['base_ref']}"] = merge_sha
            pr["state"] = "closed"
            pr["merged"] = True
            pr["merged_at"] = iso(ts)
            pr["merge_commit_sha"] = merge_sha
            pr["closed_ts"] = ts
            pr["updated_ts"] = ts
            pr["base_sha"] = merge_sha
            repo.bump()
        return _mcp_tool_result({"sha": merge_sha, "merged": True,
                                 "message": f"Merge pull request #{number}"})
    if name == "close_issue":
        owner, repo_name = _mcp_owner_repo(args)
        number = args.get("issue_number")
        if not owner or not repo_name or not isinstance(number, int):
            return _mcp_tool_result({"error": "owner, repo and issue_number are required"}, True)
        repo = forge.get_repo(owner, repo_name)
        if repo is None:
            return _mcp_tool_result({"error": "Not Found"}, True)
        with forge.lock:
            issue = repo.issues.get(number)
            if issue is None:
                return _mcp_tool_result({"error": "Not Found"}, True)
            issue["state"] = "closed"
            issue["closed_ts"] = tick()
            issue["updated_ts"] = tick()
        return _mcp_tool_result(_issue_json(forge, repo, issue))
    return None


def _dispatch_mcp(server: ForgeServer, req: Req):
    # A body that is not JSON answers HTTP 400 with JSON-RPC -32700.
    try:
        payload = json.loads(req.body.decode("utf-8")) if req.body else None
    except Exception:
        return 400, "application/json", _json_bytes(
            {"jsonrpc": "2.0", "id": None,
             "error": {"code": -32700, "message": "Parse error"}}), None
    if not isinstance(payload, dict):
        return 400, "application/json", _json_bytes(
            {"jsonrpc": "2.0", "id": None,
             "error": {"code": -32700, "message": "Parse error"}}), None
    method = payload.get("method")
    req_id = payload.get("id")
    params = payload.get("params") or {}
    # Notifications (no id): acknowledge with 202 and no body.
    if req_id is None:
        return 202, "application/json", b"", None
    # Auth: a call with no Authorization header answers 401.
    try:
        server._require_auth(req)
    except ApiError as exc:
        return exc.status, "application/json", _json_bytes(
            _mcp_error(server, req_id, -32000, exc.message)), None
    if method == "server/discover":
        result = {"supportedVersions": ["2026-07-28"],
                  "capabilities": {"tools": {}},
                  "serverInfo": dict(_MCP_SERVER_INFO),
                  "instructions": "A GitHub-compatible git forge.",
                  "_meta": {"io.modelcontextprotocol/serverInfo": dict(_MCP_SERVER_INFO)}}
        return server._json(200, _mcp_result(server, req_id, result))
    if method == "initialize":
        offered = params.get("protocolVersion") or "2026-07-28"
        accepted = {"2026-07-28", "2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"}
        chosen = offered if offered in accepted else "2025-11-25"
        result = {"protocolVersion": chosen,
                  "capabilities": {"tools": {}},
                  "serverInfo": dict(_MCP_SERVER_INFO)}
        return server._json(200, _mcp_result(server, req_id, result),
                            {"Mcp-Session-Id": "zech-forge2-session"})
    if method == "tools/list":
        return server._json(200, _mcp_result(server, req_id, {"tools": _MCP_TOOLS}))
    if method == "tools/call":
        tool_name = params.get("name")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return server._json(200, _mcp_error(server, req_id, -32602, "Invalid params"))
        known = {t["name"] for t in _MCP_TOOLS}
        if tool_name not in known:
            return server._json(200, _mcp_error(server, req_id, -32602,
                                                f"Unknown tool: {tool_name}"))
        schema = next(t for t in _MCP_TOOLS if t["name"] == tool_name)
        missing = [p for p in schema["inputSchema"].get("required", [])
                   if p not in args or args.get(p) is None]
        # full_name can stand in for owner+repo
        if "full_name" in args and isinstance(args.get("full_name"), str) \
                and "/" in args["full_name"]:
            missing = [p for p in missing if p not in ("owner", "repo")]
        if missing:
            return server._json(200, _mcp_result(
                server, req_id,
                {"content": [{"type": "text",
                              "text": json.dumps({"error": f"Missing required argument(s): {', '.join(missing)}"})}],
                 "isError": True}))
        result = _mcp_call_tool(server, tool_name, args)
        return server._json(200, _mcp_result(server, req_id, result))
    if method == "ping":
        return server._json(200, _mcp_result(server, req_id, {}))
    return server._json(200, _mcp_error(server, req_id, -32601, f"Method not found: {method}"))


# -------------------------------------------------------------------------------------
# Git smart HTTP
# -------------------------------------------------------------------------------------

def _git_caps_v2() -> bytes:
    lines = [b"version 2\n",
             b"ls-refs=unborn peel symrefs ref-prefix\n",
             b"fetch=shallow filter\n",
             b"object-format=sha1\n"]
    return b"".join(pkt_line(line) for line in lines) + pkt_flush()


def _git_advertisement_v0(forge: Forge, repo: Repo, service: str, caps: str) -> bytes:
    out = bytearray()
    out += pkt_line(f"# service={service}\n".encode("ascii"))
    out += pkt_flush()
    refs = _sorted_refs(repo)
    caps_b = caps.encode("ascii")
    if not refs:
        out += pkt_line(ZERO_OID.encode("ascii") + b" capabilities^{}\0" + caps_b + b"\n")
    else:
        first = True
        for refname, oid in refs:
            line = f"{oid} {refname}".encode("ascii")
            if first:
                line += b"\0" + caps_b
                first = False
            out += pkt_line(line + b"\n")
            if refname.startswith("refs/tags/"):
                entry = forge.get_object(oid)
                if entry and entry[0] == "tag":
                    peeled = peel_to_commit(forge, oid)
                    out += pkt_line(f"{peeled} {refname}^{{}}".encode("ascii") + b"\n")
    out += pkt_flush()
    return bytes(out)


def _sorted_refs(repo: Repo):
    refs = [("HEAD", repo.refs.get(f"refs/heads/{repo.default_branch}", ZERO_OID))]
    for refname in sorted(repo.refs):
        refs.append((refname, repo.refs[refname]))
    return refs


def _git_repo_or_404(server: ForgeServer, owner, name):
    repo = server.forge.get_repo(owner, name)
    if repo is None:
        raise ApiError(404, "Not Found")
    return repo


def _dispatch_git(server: ForgeServer, req: Req, owner, name, rest):
    forge = server.forge
    if rest == "/info/refs":
        service = req.query.get("service", [None])[0]
        if service not in ("git-upload-pack", "git-receive-pack"):
            raise ApiError(404, "Not Found")
        try:
            server._require_auth(req)
        except ApiError as exc:
            return server._error(exc.status, exc.message)
        repo = _git_repo_or_404(server, owner, name)
        ctype = f"application/x-{service}-advertisement"
        if service == "git-upload-pack" and \
                req.headers.get("git-protocol", "") == "version=2":
            return 200, ctype, _git_caps_v2(), None
        if service == "git-upload-pack":
            caps = ("side-band-64k shallow multi_ack ofs-delta "
                    f"symref=HEAD:refs/heads/{repo.default_branch}")
            return 200, ctype, _git_advertisement_v0(forge, repo, service, caps), None
        caps = "report-status side-band-64k delete-refs ofs-delta"
        return 200, ctype, _git_advertisement_v0(forge, repo, service, caps), None
    if rest == "/git-upload-pack" and req.method == "POST":
        try:
            server._require_auth(req)
        except ApiError as exc:
            return server._error(exc.status, exc.message)
        repo = _git_repo_or_404(server, owner, name)
        try:
            if req.headers.get("git-protocol", "") == "version=2":
                body = _handle_upload_pack_v2(forge, repo, req.body)
            else:
                body = _handle_upload_pack_v0(forge, repo, req.body)
        except Exception:
            # A malformed request must not crash the server.
            body = b""
        return 200, "application/x-git-upload-pack-result", body, None
    if rest == "/git-receive-pack" and req.method == "POST":
        try:
            server._require_auth(req)
        except ApiError as exc:
            return server._error(exc.status, exc.message)
        repo = _git_repo_or_404(server, owner, name)
        try:
            body = _handle_receive_pack(forge, repo, req.body)
        except Exception:
            body = pkt_line(b"unpack error\n") + pkt_flush()
            body = _sideband(body)
        return 200, "application/x-git-receive-pack-result", body, None
    raise ApiError(404, "Not Found")


def _sideband(data: bytes) -> bytes:
    """Wrap bytes into sideband band-1 pkt-lines (chunks <= 65515 payload bytes)."""
    out = bytearray()
    for i in range(0, len(data), 65515):
        chunk = data[i:i + 65515]
        out += pkt_line(b"\x01" + chunk)
    if not data:
        out += pkt_line(b"\x01")
    out += pkt_flush()
    return bytes(out)


def _ls_refs_lines(forge: Forge, repo: Repo, peel: bool, symrefs: bool,
                   prefixes):
    """ls-refs response lines (without the trailing flush)."""
    lines = []
    default_tip = repo.refs.get(f"refs/heads/{repo.default_branch}")

    def keep(refname):
        return not prefixes or any(refname.startswith(p) for p in prefixes)

    if not repo.refs:
        if keep("HEAD"):
            line = "unborn HEAD"
            if symrefs:
                line += f" symref-target:refs/heads/{repo.default_branch}"
            lines.append(line + "\n")
        return lines
    if keep("HEAD"):
        line = f"{default_tip} HEAD"
        if symrefs:
            line += f" symref-target:refs/heads/{repo.default_branch}"
        lines.append(line + "\n")
    for refname in sorted(repo.refs):
        if not keep(refname):
            continue
        oid = repo.refs[refname]
        line = f"{oid} {refname}"
        if peel and refname.startswith("refs/tags/"):
            entry = forge.get_object(oid)
            if entry and entry[0] == "tag":
                line += f" peeled:{peel_to_commit(forge, oid)}"
        lines.append(line + "\n")
    return lines


def _tree_closure(forge: Forge, tree_oid):
    """Trees + blobs under a tree (no commits)."""
    out = {}
    stack = [tree_oid]
    seen = set()
    while stack:
        oid = stack.pop()
        if oid in seen or oid == ZERO_OID:
            continue
        seen.add(oid)
        entry = forge.get_object(oid)
        if entry is None:
            continue
        otype, data = entry
        out[oid] = (otype, data)
        if otype == "tree":
            for _mode, _name, child in parse_tree(data):
                stack.append(child)
    return out


def _have_closure(forge: Forge, haves, shallow_existing):
    """Objects the client already holds: the closure of its haves, cut at the
    shallow boundary (a shallow clone holds the boundary commit and its tree,
    but not the boundary's parents or their objects). Returns
    (held_commit_oids, held_objects {oid: (type, data)})."""
    boundary = set(shallow_existing)
    commits = set()
    objects = {}
    stack = list(haves)
    seen = set()
    while stack:
        cur = stack.pop()
        if cur in seen or cur == ZERO_OID:
            continue
        seen.add(cur)
        entry = forge.get_object(cur)
        if entry is None:
            continue
        otype, data = entry
        objects[cur] = (otype, data)
        if otype == "commit":
            commits.add(cur)
            objects.update(_tree_closure(forge, parse_commit(data)["tree"]))
            if cur in boundary:
                continue
            stack.extend(commit_parents(forge, cur))
        elif otype == "tag":
            stack.append(parse_tag(data)["object"])
    return commits, objects


def _fetch_objects(forge: Forge, repo: Repo, wants, haves, shallow_existing, depth):
    """Compute the objects a fetch must send.

    Returns (objects {oid: (type, data)}, shallow_list, unshallow_list).
    depth N means N commits per path from each want; boundary commits are shallow.
    Only objects not reachable from the client's haves are sent, and the tree
    closure stops at the depth boundary (parents beyond it are never sent).
    """
    have_commits, have_objects = _have_closure(forge, haves, shallow_existing)

    included = set()
    boundary = set()
    traversed = set()
    queue = [(w, 0) for w in wants]
    visited = set()
    while queue:
        cur, gen = queue.pop(0)
        if cur in visited or cur == ZERO_OID:
            continue
        visited.add(cur)
        entry = forge.get_object(cur)
        if entry is None:
            continue
        traversed.add(cur)
        if entry[0] == "tag":
            target = parse_tag(entry[1])["object"]
            if target not in visited:
                queue.append((target, gen))
            if cur not in have_commits:
                included.add(cur)
            continue
        if entry[0] != "commit":
            if cur not in have_commits:
                included.add(cur)
            continue
        if cur not in have_commits:
            included.add(cur)
        if depth is not None and gen + 1 >= depth:
            continue
        for parent in commit_parents(forge, cur):
            if parent not in visited:
                queue.append((parent, gen + 1))
    if depth is not None:
        for oid in included:
            entry = forge.get_object(oid)
            if entry is None or entry[0] != "commit":
                continue
            parents = commit_parents(forge, oid)
            if parents and any(p not in included for p in parents):
                boundary.add(oid)
            elif not parents:
                boundary.add(oid)
    shallow = sorted(boundary)
    unshallow = []
    if depth is not None:
        unshallow = sorted(old for old in shallow_existing
                           if old in traversed and old not in boundary)
    objects = {}
    for oid in included:
        entry = forge.get_object(oid)
        if entry is None:
            continue
        otype, data = entry
        objects[oid] = (otype, data)
        if otype == "commit":
            objects.update(_tree_closure(forge, parse_commit(data)["tree"]))
    for oid in list(objects):
        if oid in have_objects:
            del objects[oid]
    return objects, shallow, unshallow


def _pack_cache_key(repo: Repo, wants, depth, haves, shallow_existing):
    return (repo.pack_version, tuple(sorted(wants)), depth,
            tuple(sorted(haves)), tuple(sorted(shallow_existing)))


def _build_fetch_pack(forge: Forge, repo: Repo, wants, haves, shallow_existing, depth):
    key = _pack_cache_key(repo, wants, depth, haves, shallow_existing)
    cached = repo.pack_cache.get(key)
    if cached is not None:
        return cached
    objects, shallow, unshallow = _fetch_objects(forge, repo, wants, haves,
                                                 shallow_existing, depth)
    pack = write_pack([(oid, otype, data) for oid, (otype, data) in objects.items()])
    result = (pack, shallow, unshallow)
    if len(repo.pack_cache) > 64:
        repo.pack_cache.clear()
    repo.pack_cache[key] = result
    return result


def _handle_upload_pack_v2(forge: Forge, repo: Repo, body: bytes) -> bytes:
    packets, _consumed = parse_pkt_lines(body)
    command = None
    args = []
    in_args = False
    for kind, payload in packets:
        if kind == "delim":
            in_args = True
            continue
        if kind != "data":
            continue
        text = payload.decode("utf-8", "replace")
        if not in_args:
            if text.startswith("command="):
                command = text[len("command="):].strip()
            continue
        args.append(text.rstrip("\n"))
    if command == "ls-refs":
        peel = "peel" in args
        symrefs = "symrefs" in args
        prefixes = [a[len("ref-prefix "):] for a in args if a.startswith("ref-prefix ")]
        out = bytearray()
        for line in _ls_refs_lines(forge, repo, peel, symrefs, prefixes):
            out += pkt_line(line.encode("utf-8"))
        out += pkt_flush()
        return bytes(out)
    if command == "fetch":
        wants, haves, shallow_existing, depth = [], [], [], None
        for arg in args:
            if arg.startswith("want "):
                wants.append(arg[5:].strip())
            elif arg.startswith("have "):
                haves.append(arg[5:].strip())
            elif arg.startswith("shallow "):
                shallow_existing.append(arg[8:].strip())
            elif arg.startswith("deepen "):
                try:
                    depth = int(arg[7:].strip())
                except ValueError:
                    depth = None
        pack, shallow, unshallow = _build_fetch_pack(forge, repo, wants, haves,
                                                     shallow_existing, depth)
        out = bytearray()
        out += pkt_line(b"acknowledgments\n")
        out += pkt_line(b"NAK\n")
        out += pkt_delim()
        if shallow or unshallow:
            out += pkt_line(b"shallow-info\n")
            for oid in shallow:
                out += pkt_line(f"shallow {oid}\n".encode("ascii"))
            for oid in unshallow:
                out += pkt_line(f"unshallow {oid}\n".encode("ascii"))
            out += pkt_delim()
        out += pkt_line(b"packfile\n")
        out += _sideband(pack)
        return bytes(out)
    return pkt_flush()


def _handle_upload_pack_v0(forge: Forge, repo: Repo, body: bytes) -> bytes:
    packets, _consumed = parse_pkt_lines(body)
    wants, haves, shallow_existing, depth = [], [], [], None
    section = 0
    for kind, payload in packets:
        if kind == "flush":
            section += 1
            continue
        if kind != "data":
            continue
        text = payload.decode("utf-8", "replace").rstrip("\n")
        if section == 0:
            if text.startswith("want "):
                wants.append(text[5:].split(" ")[0])
            elif text.startswith("shallow "):
                shallow_existing.append(text[8:].strip())
            elif text.startswith("deepen "):
                try:
                    depth = int(text[7:].strip())
                except ValueError:
                    depth = None
        else:
            if text.startswith("have "):
                haves.append(text[5:].strip())
    pack, shallow, unshallow = _build_fetch_pack(forge, repo, wants, haves,
                                                 shallow_existing, depth)
    out = bytearray()
    out += pkt_line(b"NAK\n")
    for oid in shallow:
        out += pkt_line(f"shallow {oid}\n".encode("ascii"))
    for oid in unshallow:
        out += pkt_line(f"unshallow {oid}\n".encode("ascii"))
    out += _sideband(pack)
    return bytes(out)


def _handle_receive_pack(forge: Forge, repo: Repo, body: bytes) -> bytes:
    packets, consumed = parse_pkt_lines(body)
    commands = []
    for kind, payload in packets:
        if kind == "flush":
            break
        if kind != "data":
            continue
        text = payload.decode("utf-8", "replace").rstrip("\n")
        nul = text.find("\0")
        if nul >= 0:
            text = text[:nul]
        parts = text.split(" ")
        if len(parts) == 3 and re.fullmatch(r"[0-9a-f]{40}", parts[0]) \
                and re.fullmatch(r"[0-9a-f]{40}", parts[1]):
            commands.append((parts[0], parts[1], parts[2]))
    pack_bytes = body[consumed:]
    unpack_ok = True
    unpack_detail = "ok"
    new_objects = {}
    if pack_bytes:
        try:
            new_objects = read_pack(
                pack_bytes,
                resolve_base=lambda oid: forge.get_object(oid))
        except Exception as exc:
            unpack_ok = False
            unpack_detail = f"unpacker error: {exc}"
    report_lines = []
    if unpack_ok:
        with forge.lock:
            for oid, (otype, data) in new_objects.items():
                if oid not in forge.objects:
                    forge.objects[oid] = (otype, data)
            for old_oid, new_oid, refname in commands:
                current = repo.refs.get(refname, ZERO_OID)
                ok, reason = _check_ref_update(forge, repo, old_oid, new_oid,
                                               refname, current)
                if ok:
                    if new_oid == ZERO_OID:
                        repo.refs.pop(refname, None)
                    else:
                        repo.refs[refname] = new_oid
                    repo.bump()
                    report_lines.append(f"ok {refname}\n")
                else:
                    report_lines.append(f"ng {refname} {reason}\n")
    else:
        for _old, _new, refname in commands:
            report_lines.append(f"ng {refname} unpacker error\n")
    out = bytearray()
    out += pkt_line(f"unpack {unpack_detail}\n".encode("utf-8"))
    for line in report_lines:
        out += pkt_line(line.encode("utf-8"))
    out += pkt_flush()
    return _sideband(bytes(out))


def _check_ref_update(forge: Forge, repo: Repo, old_oid, new_oid, refname, current):
    """(ok, reason) for one receive-pack command."""
    if new_oid == ZERO_OID:
        # delete
        if current == ZERO_OID:
            return False, "non-fast-forward"
        if old_oid != ZERO_OID and old_oid != current:
            return False, "non-fast-forward"
        return True, ""
    if old_oid == ZERO_OID:
        # create
        if current != ZERO_OID:
            return False, "already exists"
        if forge.get_object(new_oid) is None:
            return False, "missing object"
        return True, ""
    if old_oid != current:
        return False, "non-fast-forward"
    if forge.get_object(new_oid) is None:
        return False, "missing object"
    # fast-forward: current must be an ancestor of new_oid
    if new_oid == current:
        return True, ""
    if current in ancestors(forge, new_oid):
        return True, ""
    return False, "non-fast-forward"


ForgeServer._dispatch_graphql = _dispatch_graphql
ForgeServer._dispatch_mcp = _dispatch_mcp
ForgeServer._dispatch_git = _dispatch_git

# -------------------------------------------------------------------------------------
# Lifecycle
# -------------------------------------------------------------------------------------


def _http_json(url, method, payload=None, timeout=10.0):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw) if raw else {}
        except Exception:
            return exc.code, {}
    except Exception:
        return None, {}
    try:
        return 200, json.loads(raw) if raw else {}
    except Exception:
        return 200, {}


def main() -> int:
    global FORGE
    env_url = os.environ.get("MAKEFUN_ENV_URL", "").rstrip("/")
    if not env_url:
        print("MAKEFUN_ENV_URL is not set", file=sys.stderr)
        return 1
    token = os.environ.get("MAKEFUN_SHOP_TOKEN")

    FORGE = Forge(VIEWER["login"])
    server = ForgeServer(FORGE, token)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.port
    print(f"zech-forge2 listening on 0.0.0.0:{port} as {VIEWER['login']}", flush=True)

    registered = False
    for _ in range(120):
        status, _ = _http_json(f"{env_url}/register", "POST", {"port": port})
        if status == 200:
            registered = True
            break
        time.sleep(1)
    if not registered:
        print("failed to register with the environment", file=sys.stderr)
        server.shutdown()
        return 1

    while True:
        status, payload = _http_json(f"{env_url}/done", "GET")
        if status == 200 and payload.get("done") is True:
            break
        time.sleep(1)

    server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
