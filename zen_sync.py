#!/usr/bin/env python3
"""Sync Zen browser workspaces between profiles, remapping container IDs by name.

Subcommands:
  dump       Decompress and pretty-print zen-sessions.jsonlz4 for inspection
  pack       Recompress a JSON file into zen-sessions.jsonlz4 (debugging)
  transform  Copy source profile's zen-sessions.jsonlz4 into target profile,
             remapping containerTabId references by container name/l10nID.
             Auto-creates missing containers on target.

Profile paths:
  macOS    ~/Library/Application Support/zen/Profiles/<xxx>.Default (release)
  Windows  %APPDATA%\\zen\\Profiles\\<xxx>.Default (release)

Requires: python >= 3.10, pip install lz4
"""
import argparse
import json
import os
import shutil
import struct
import sys
import time
from pathlib import Path

import lz4.block

MOZ_MAGIC = b"mozLz40\x00"
LOCK_NAMES = (".parentlock", "parent.lock", "lock")
SESSION_FILE = "zen-sessions.jsonlz4"
CONTAINERS_FILE = "containers.json"
COLLECTIONS_WITH_CONTAINER = ("spaces", "tabs", "folders", "groups")


def moz_decompress(data: bytes) -> bytes:
    if not data.startswith(MOZ_MAGIC):
        raise ValueError(f"not a mozLz40 file (got header {data[:8]!r})")
    decompressed_size = struct.unpack("<I", data[8:12])[0]
    return lz4.block.decompress(data[12:], uncompressed_size=decompressed_size)


def moz_compress(data: bytes) -> bytes:
    compressed = lz4.block.compress(data, store_size=False)
    return MOZ_MAGIC + struct.pack("<I", len(data)) + compressed


def read_jsonlz4(path: Path) -> dict:
    return json.loads(moz_decompress(path.read_bytes()))


def write_jsonlz4(path: Path, obj: dict) -> None:
    raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    path.write_bytes(moz_compress(raw))


def container_key(identity: dict) -> str:
    """Cross-machine identity key. l10nID is stable for built-ins; name for user-created."""
    return identity.get("l10nID") or identity.get("name") or f"id-{identity['userContextId']}"


def assert_zen_not_running(profile: Path) -> None:
    for lock_name in LOCK_NAMES:
        lock = profile / lock_name
        if lock.exists() or lock.is_symlink():
            sys.exit(f"refuse: Zen appears to be running ({lock} exists). Quit Zen on the target first.")


def load_json(path: Path) -> dict:
    if not path.exists():
        sys.exit(f"missing file: {path}")
    return json.loads(path.read_text())


def cmd_dump(args: argparse.Namespace) -> None:
    profile = Path(args.profile).expanduser()
    print(json.dumps(read_jsonlz4(profile / SESSION_FILE), indent=2))


def cmd_pack(args: argparse.Namespace) -> None:
    src = Path(args.input).expanduser()
    dst = Path(args.output).expanduser()
    write_jsonlz4(dst, json.loads(src.read_text()))
    print(f"wrote {dst}")


def cmd_transform(args: argparse.Namespace) -> None:
    src_profile = Path(args.source).expanduser().resolve()
    tgt_profile = Path(args.target).expanduser().resolve()
    assert_zen_not_running(tgt_profile)

    src_session_path = src_profile / SESSION_FILE
    tgt_session_path = tgt_profile / SESSION_FILE
    tgt_containers_path = tgt_profile / CONTAINERS_FILE

    if not src_session_path.exists():
        sys.exit(f"missing source session: {src_session_path}")

    src_containers = load_json(src_profile / CONTAINERS_FILE)
    tgt_containers = load_json(tgt_containers_path)

    src_id_to_key = {i["userContextId"]: container_key(i) for i in src_containers["identities"]}
    src_key_to_identity = {container_key(i): i for i in src_containers["identities"]}
    tgt_key_to_id = {container_key(i): i["userContextId"] for i in tgt_containers["identities"]}

    next_id = max((i["userContextId"] for i in tgt_containers["identities"]), default=0) + 1
    created: list[dict] = []

    def remap(cid: int | None) -> int | None:
        if not cid:
            return cid
        key = src_id_to_key.get(cid)
        if key is None:
            print(f"  warn: source container_id {cid} not in containers.json — defaulting to 0")
            return 0
        if key in tgt_key_to_id:
            return tgt_key_to_id[key]
        nonlocal next_id
        new_identity = dict(src_key_to_identity[key])
        new_identity["userContextId"] = next_id
        tgt_containers["identities"].append(new_identity)
        tgt_key_to_id[key] = next_id
        created.append(new_identity)
        next_id += 1
        return tgt_key_to_id[key]

    session = read_jsonlz4(src_session_path)
    remapped = 0
    for collection in COLLECTIONS_WITH_CONTAINER:
        for item in session.get(collection, []):
            old = item.get("containerTabId")
            if old:
                new = remap(old)
                if new != old:
                    item["containerTabId"] = new
                    remapped += 1

    ts = time.strftime("%Y%m%d-%H%M%S")
    if tgt_session_path.exists():
        backup = tgt_session_path.with_name(f"{tgt_session_path.name}.bak-{ts}")
        shutil.copy2(tgt_session_path, backup)
        print(f"backed up session: {backup.name}")
    if created:
        cbackup = tgt_containers_path.with_name(f"{tgt_containers_path.name}.bak-{ts}")
        shutil.copy2(tgt_containers_path, cbackup)
        print(f"backed up containers: {cbackup.name}")

    tmp_session = tgt_session_path.with_name(tgt_session_path.name + ".tmp")
    write_jsonlz4(tmp_session, session)
    os.replace(tmp_session, tgt_session_path)

    if created:
        tgt_containers["lastFileUpdateTimestamp"] = int(time.time() * 1000)
        tmp_containers = tgt_containers_path.with_name(tgt_containers_path.name + ".tmp")
        tmp_containers.write_text(json.dumps(tgt_containers, indent=2))
        os.replace(tmp_containers, tgt_containers_path)

    print(f"\ntransform complete:")
    print(f"  source: {src_session_path}")
    print(f"  target: {tgt_session_path}")
    print(f"  remapped {remapped} containerTabId reference(s)")
    if created:
        names = [container_key(c) for c in created]
        print(f"  created {len(created)} container(s) on target: {names}")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Sync Zen workspaces across profiles with container remapping.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_dump = sub.add_parser("dump", help="Pretty-print zen-sessions.jsonlz4")
    p_dump.add_argument("profile", help="Path to Zen profile directory")
    p_dump.set_defaults(func=cmd_dump)

    p_pack = sub.add_parser("pack", help="Recompress JSON back into mozLz40")
    p_pack.add_argument("input", help="Path to a JSON file")
    p_pack.add_argument("output", help="Output .jsonlz4 path")
    p_pack.set_defaults(func=cmd_pack)

    p_tx = sub.add_parser("transform", help="Sync source profile's session into target with container remapping")
    p_tx.add_argument("--source", required=True, help="Source Zen profile directory")
    p_tx.add_argument("--target", required=True, help="Target Zen profile directory")
    p_tx.set_defaults(func=cmd_transform)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
