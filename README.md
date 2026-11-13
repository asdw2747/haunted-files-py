# Haunted Files

A file browser that is also an abandoned room.

Folders are doors. Files are objects on the floor. Things that used to be
here linger as ghosts. Everything is read-only.


## Run

Requirements: Python 3.10+ and PyQt6.

```sh
./run.sh                 # open your home folder
./run.sh ~/Desktop       # open a specific room
# or
python3 haunted_files.py ~/Desktop
```

## Controls

| Input | Action |
|---|---|
| click a door | enter that folder |
| click a file | inspect it |
| `Esc` / `Backspace` | go up one room |
| `Home` | return to the outermost room |
| `Ctrl+O` | choose a different outermost room |
| `G` | show / hide ghosts |
| `F5` | rescan the room |
| `Ctrl+Q` | quit |

Hover anything for details. The breadcrumb at the top is clickable.

## Ghosts

The first time a room is visited, every file in it is recorded in a local
index. If a recorded file is later missing, it reappears as a faded,
dashed outline with its last-seen date. Ghosts fade over roughly five
months.

The index lives at:

```
~/.local/share/haunted-files/index.json
```

Delete that file to forget everything.

## What it does not do

- It never creates, renames, moves, or deletes anything in the folders
  you browse.
- It does not open or execute files. Clicking only inspects.
- It does not follow symlinked folders, so it cannot leave the room
  through a link.
- Hidden names (leading dot) are skipped.
- Rooms show at most 54 things, 40 ghosts, and 6 doors at once; the
  remainder is reported as "unlit" or "shut". Enter a subfolder or use
  a narrower root to see more.

## Layout

```
haunted_files.py   the whole app
run.sh             launcher
```

The room is drawn with `QPainter`; there are no image assets. Dust,
flicker, grain, and the aging of objects are generated at runtime. A
file's colour is derived from its modification age: pale when fresh,
amber after a few weeks, grey-green after a year.
