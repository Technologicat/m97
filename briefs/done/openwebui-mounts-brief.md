# Brief: read-only mounts for Open WebUI's plugins

## What shipped (2026-11-24)

**Done**, in m97 0.3.0 (unreleased at the time of writing). The rest of this brief is the plan as written; the
README's `[openwebui]` settings describe what shipped.

- **Built as planned:** `[openwebui] mounts` in m97, `services.read_only_mounts`, with the tests listed below;
  `settings.toml`, the README and the changelog document it.
- **Changed or added while building:**
  - **A spec without both halves is skipped too**, with its own warning, as a missing host path is.
  - **dotclaude's `open-webui-serve` reads m97's list**, the recommendation taken: `[openwebui] mounts` from
    `~/.config/m97/settings.toml`, parsed with `python3`'s `tomllib`, the same rules as m97. With no such file
    it mounts nothing; with a `python3` older than 3.11 it warns and mounts nothing.
- **Set up on the local AI server:** the lorebook folder and its symlink; the ledger copied from the work
  machine, the glossary being identical on both (same sha1); the three mounts; Open WebUI restarted (by
  Juha); the filter global, the tool attached to `flash-next` and readable by all users, as the model is.
- **The live test**, and what it found, is in grimoire's `briefs/ports-brief.md`, under Open WebUI. One case
  is still to test there: the filter's `request` hook on a later hop of a tool loop.

Open WebUI's plugins run inside its container, where the host's files are out of reach unless mounted. The first
plugin that needs them is [grimoire](https://github.com/Technologicat/grimoire)'s lorebook filter and tool, which
read a folder of Markdown definitions and the inflected forms of their terms, both on the host. This brief adds
a general way to mount host folders read-only into the Open WebUI container, and sets up grimoire's three
mounts on the local AI server.

Both launchers start the same container, so both need it: m97, and dotclaude's `open-webui-serve`. Either one
starting the container without the mounts would leave the plugins reading nothing.

## m97

**A `mounts` list in `[openwebui]`**: `"host:container"` strings, each mounted read-only, the host path with `~`
expanded. The shipped default is `[]`; a machine's own `~/.config/m97/settings.toml` lists its mounts.

- **A host path that does not exist is skipped, with a warning in the log.** podman refuses to start a container
  with a missing mount source, and Open WebUI serves the household: a stale entry must not keep it down.
- **Split at the last colon**, since the path inside the container has none.
- **Appended to `openwebui_run_args`** after the data volume.
- **Tests:** an existing folder mounted with `:ro`, a missing one skipped, `~` expanded (with `HOME` pointed at a
  temporary folder).
- **`settings.toml`** documents the key, with grimoire's mounts as the example.

A sketch, written for this brief and not run (the session that wrote it had no m97 venv):

```python
def read_only_mounts(specs):
    """`-v` arguments mounting each of `specs` read-only: `"host:container"` strings, `~` expanded in the host path.

    A host path that does not exist is skipped with a warning, since podman would refuse to start the container.
    """
    args = []
    for spec in specs:
        host, _, inside = spec.rpartition(":")  # the path inside the container has no colon
        host = pathlib.Path(host).expanduser()
        if not host.exists():
            logger.warning(f"mount `{spec}`: {host} does not exist; not mounted")
            continue
        args += ["-v", f"{host}:{inside}:ro"]
    return args
```

## dotclaude's `open-webui-serve`

**The same mounts, from one list on the machine.** To decide: whether `open-webui-serve` reads them from m97's
`~/.config/m97/settings.toml` (`[openwebui] mounts`), so the machine has one list, or keeps its own. Reading
m97's file couples the script to m97's settings, which it already shadows (same container, same image); keeping
two lists invites them to drift. The first is the recommendation. Either way, a missing host path is skipped, as
in m97.

## Setting up the local AI server

1. **The lorebook folder**: `~/.config/raven/librarian/lorebook/`, holding a symlink `glossary.md` →
   `~/Documents/koodit/substrate-independent/glossary.md`, as on the dev machines. A folder rather than the
   checkout itself, since grimoire reads every `.md` in the folder it is given.
2. **The inflected forms**: either copy `expansions-01308c24.jsonl` (with its `-traces.jsonl`) from the work
   machine's `~/.config/raven/librarian/lorebook_expansion/`, made 2026-11-24 with `flash-next` on gufo, English
   forms and Finnish stems, or run `grimoire-expand` there (`pipx install git+https://github.com/Technologicat/grimoire`,
   then `grimoire-expand --lorebook ~/.config/raven/librarian/lorebook --url http://127.0.0.1:8080 --language
   English=forms --language Finnish=stems`, a few minutes). The same model gives the same ledger name either way.
3. **The three mounts**, in `~/.config/m97/settings.toml`:
   ```toml
   [openwebui]
   mounts = ["~/.config/raven/librarian/lorebook:/lorebook",
             "~/Documents/koodit/substrate-independent:/home/jje/Documents/koodit/substrate-independent",
             "~/.config/raven/librarian/lorebook_expansion:/lorebook_expansion"]
   ```
   The second exists so the lorebook's symlink resolves inside the container: a link to a host path points at
   nothing there unless its target is mounted at the same path.
4. **Restart Open WebUI** so the container picks up the mounts. It serves the household and the other machines,
   so ask before restarting it. m97 restarts itself with `systemctl --user restart m97` after the code change;
   the container is a separate step.
5. **Install grimoire's plugins** in Open WebUI, as its
   [README](https://github.com/Technologicat/grimoire/blob/main/open-webui/README.md) says: the filter from
   `open-webui/grimoire_filter.py` (global), the tool from `open-webui/grimoire_tool.py` (enabled for
   `flash-next`), valves `lorebook_dir` = `/lorebook` and `ledger_dir` = `/lorebook_expansion`, and
   `stem_languages` = `Finnish`. Needs the admin account.

## The live test

What grimoire's brief lists as needing one: that the inserted tool round before the user's message reaches gufo
intact and gufo accepts it, that the filter's `request` hook leaves its own round alone on a later hop of a tool
loop, and that the tool reads its folder from its valve. A message mentioning a glossary term, say "AI
ethnographies", should bring its definition; asking the model to look up another term exercises the tool. Report
what is found back into grimoire's `briefs/ports-brief.md`, under Open WebUI.
