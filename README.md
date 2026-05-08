# zen-yat

sync zen browser workspaces across machines.

mozilla sync handles bookmarks/passwords/history fine in zen, but workspaces (and pinned/essential tabs) don't propagate cross-device. this script does that part — it transforms zen's workspace file from one profile and writes it into another, fixing up multi-account container ids by name so containerized tabs survive the trip.

## requires

- python 3.10+
- `pip install lz4`

## profile paths

- macos: `~/Library/Application Support/zen/Profiles/<xxx>.Default (release)`
- windows: `%APPDATA%\zen\Profiles\<xxx>.Default (release)`
- linux: `~/.zen/<xxx>.Default (release)`

## use it

1. quit zen on both machines.
2. copy the source profile's `zen-sessions.jsonlz4` and `containers.json` to the target machine — syncthing, scp, usb stick, whatever. put them in a folder like `~/zen-yat-staging/<machine>/`.
3. run the transform on the target. you can omit `--target` and the script will list local profiles from `profiles.ini` and let you pick (defaults to the active one):

```bash
python zen_sync.py transform --source ~/zen-yat-staging/windows
```

   or pass `--target` explicitly:

```bash
python zen_sync.py transform \
  --source ~/zen-yat-staging/windows \
  --target "$HOME/Library/Application Support/zen/Profiles/abc123.Default (release)"
```

4. open zen. workspaces show up.

the script:
- refuses to run if zen is open on the target (checks for `.parentlock`)
- backs up the target's existing `zen-sessions.jsonlz4` and `containers.json` with a timestamped suffix before overwriting
- remaps `containerTabId` from source ids to target ids by matching container name / l10nID
- auto-creates containers on the target if the source has ones the target doesn't

## list local profiles

```bash
python zen_sync.py list
```

shows every profile in `profiles.ini` with its size and which one is active.

## debug

```bash
# pretty-print what's actually in zen-sessions.jsonlz4
python zen_sync.py dump "$HOME/Library/Application Support/zen/Profiles/abc123.Default (release)"

# pack a hand-edited json back into mozLz40
python zen_sync.py pack edited.json zen-sessions.jsonlz4
```

## limits

- one-way sync per run. last writer wins. if you edit workspaces on both machines without running the transform between, the side you ran *from* overwrites the other.
- doesn't sync live tabs/scroll state — just workspaces, pinned, essentials, folders.
- no schema versioning in the file; if zen changes the format, the script may need a tweak.
