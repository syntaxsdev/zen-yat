#!/usr/bin/env python3
"""Sync Zen browser workspaces between profiles, remapping container IDs by name.

Subcommands:
  list       Show Zen profiles on this machine (reads profiles.ini)
  dump       Decompress and pretty-print a profile's zen-sessions.jsonlz4
  pack       Recompress a JSON file back into mozLz40 (debugging)
  transform  Copy source profile's zen-sessions.jsonlz4 into a target profile,
             remapping containerTabId references by container name/l10nID.
             Auto-creates missing containers on the target.

Profile paths (auto-detected when --target is omitted):
  macos    ~/Library/Application Support/zen/Profiles/<xxx>.Default (release)
  windows  %APPDATA%\\zen\\Profiles\\<xxx>.Default (release)
  linux    ~/.zen/<xxx>.Default (release)

Requires: python >= 3.10, pip install lz4
"""
import argparse
import configparser
import json
import os
import platform
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
            sys.exit(f"refuse: Zen appears to be running ({lock} exists). Quit Zen first.")


def load_json(path: Path) -> dict:
    if not path.exists():
        sys.exit(f"missing file: {path}")
    return json.loads(path.read_text())


def default_zen_root() -> Path:
    sys_name = platform.system()
    if sys_name == "Darwin":
        return Path.home() / "Library/Application Support/zen"
    if sys_name == "Windows":
        appdata = os.environ.get("APPDATA")
        if not appdata:
            sys.exit("APPDATA env var not set; pass --zen-root explicitly")
        return Path(appdata) / "zen"
    return Path.home() / ".zen"


def parse_profiles_ini(zen_root: Path) -> tuple[list[dict], str | None]:
    """Return (profiles, active_relative_path).

    profiles: list of {name, path (relative or abs), is_relative, full_path}
    active_relative_path: value from [Install...] Default=... (the profile Zen actually launches)
    """
    ini_path = zen_root / "profiles.ini"
    if not ini_path.exists():
        return [], None
    parser = configparser.ConfigParser()
    parser.read(ini_path)

    profiles: list[dict] = []
    active_rel: str | None = None
    for section in parser.sections():
        if section.startswith("Profile"):
            is_rel = parser[section].get("IsRelative", "1") == "1"
            rel_path = parser[section].get("Path", "")
            full_path = zen_root / rel_path if is_rel else Path(rel_path)
            profiles.append({
                "name": parser[section].get("Name", ""),
                "rel_path": rel_path,
                "is_relative": is_rel,
                "full_path": full_path,
                "default_flag": parser[section].get("Default", "0") == "1",
            })
        elif section.startswith("Install"):
            active_rel = parser[section].get("Default")
    return profiles, active_rel


def profile_size_mb(path: Path) -> float | None:
    try:
        return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) / 1024 / 1024
    except (OSError, PermissionError):
        return None


def pick_profile(zen_root: Path, prompt_label: str = "target") -> Path:
    """Auto-detect or interactively pick a Zen profile from this machine's profiles.ini."""
    profiles, active_rel = parse_profiles_ini(zen_root)
    if not profiles:
        sys.exit(f"no profiles found in {zen_root}/profiles.ini — pass --{prompt_label} explicitly")

    for p in profiles:
        p["is_active"] = active_rel is not None and p["rel_path"] == active_rel

    if len(profiles) == 1:
        return profiles[0]["full_path"]

    default_idx = next((i for i, p in enumerate(profiles) if p["is_active"]), 0)

    print(f"\nfound {len(profiles)} zen profiles in {zen_root}:\n")
    for i, p in enumerate(profiles):
        size = profile_size_mb(p["full_path"])
        size_str = f"{size:>6.1f}MB" if size is not None else "      ?"
        marker = " (active)" if p["is_active"] else ""
        default_marker = "  <- default" if i == default_idx else ""
        print(f"  [{i}] {p['name']:<20} {p['full_path'].name:<40} {size_str}{marker}{default_marker}")

    raw = input(f"\npick {prompt_label} profile [0-{len(profiles) - 1}, enter for default]: ").strip()
    if not raw:
        idx = default_idx
    else:
        try:
            idx = int(raw)
        except ValueError:
            sys.exit(f"not a number: {raw!r}")
        if not (0 <= idx < len(profiles)):
            sys.exit(f"out of range: {idx}")
    return profiles[idx]["full_path"]


def cmd_list(args: argparse.Namespace) -> None:
    zen_root = Path(args.zen_root).expanduser() if args.zen_root else default_zen_root()
    profiles, active_rel = parse_profiles_ini(zen_root)
    if not profiles:
        sys.exit(f"no profiles found in {zen_root}/profiles.ini")
    print(f"zen root: {zen_root}\n")
    for p in profiles:
        full = p["full_path"]
        is_active = active_rel is not None and p["rel_path"] == active_rel
        size = profile_size_mb(full)
        size_str = f"{size:>6.1f}MB" if size is not None else "      ?"
        marker = " (active)" if is_active else ""
        print(f"  {p['name']:<20} {size_str}  {full}{marker}")


def cmd_dump(args: argparse.Namespace) -> None:
    profile = Path(args.profile).expanduser() if args.profile else pick_profile(default_zen_root(), "profile to dump")
    print(json.dumps(read_jsonlz4(profile / SESSION_FILE), indent=2))


def cmd_pack(args: argparse.Namespace) -> None:
    src = Path(args.input).expanduser()
    dst = Path(args.output).expanduser()
    write_jsonlz4(dst, json.loads(src.read_text()))
    print(f"wrote {dst}")


def confirm(prompt: str, default_yes: bool = True) -> bool:
    suffix = " [Y/n] " if default_yes else " [y/N] "
    raw = input(prompt + suffix).strip().lower()
    if not raw:
        return default_yes
    return raw in ("y", "yes")


def cmd_transform(args: argparse.Namespace) -> None:
    src_profile = Path(args.source).expanduser().resolve()
    if args.target:
        tgt_profile = Path(args.target).expanduser().resolve()
    else:
        tgt_profile = pick_profile(default_zen_root(), "target").resolve()

    print(f"\nsource: {src_profile}")
    print(f"target: {tgt_profile}")
    if not args.yes and not confirm("\nproceed?"):
        sys.exit("cancelled")

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

    print("\ntransform complete:")
    print(f"  source: {src_session_path}")
    print(f"  target: {tgt_session_path}")
    print(f"  remapped {remapped} containerTabId reference(s)")
    if created:
        names = [container_key(c) for c in created]
        print(f"  created {len(created)} container(s) on target: {names}")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="sync zen workspaces across profiles with container remapping",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="show zen profiles on this machine")
    p_list.add_argument("--zen-root", help="override zen install root (default: OS-specific)")
    p_list.set_defaults(func=cmd_list)

    p_dump = sub.add_parser("dump", help="pretty-print zen-sessions.jsonlz4")
    p_dump.add_argument("profile", nargs="?", help="profile dir (omit to pick interactively)")
    p_dump.set_defaults(func=cmd_dump)

    p_pack = sub.add_parser("pack", help="recompress JSON back into mozLz40")
    p_pack.add_argument("input", help="path to a JSON file")
    p_pack.add_argument("output", help="output .jsonlz4 path")
    p_pack.set_defaults(func=cmd_pack)

    p_tx = sub.add_parser("transform", help="sync source profile's session into target with container remapping")
    p_tx.add_argument("--source", required=True, help="source zen profile dir (or staging dir with the 2 files)")
    p_tx.add_argument("--target", help="target zen profile dir (omit to pick interactively from local profiles.ini)")
    p_tx.add_argument("-y", "--yes", action="store_true", help="skip confirmation prompt")
    p_tx.set_defaults(func=cmd_transform)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
