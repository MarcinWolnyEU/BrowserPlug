# Custom scripts for `page_eval`

A custom script lets `page_eval` run JavaScript that a program on the server's machine
generates. The agent passes `script_name` (and optionally `script_args`) instead of `code`,
the server runs the script, and its stdout runs in the tab as `code` would (`tab_id`,
`world` and `frame_id` apply as usual).

## Turning it on

Set `BROWSERPLUG_EXT_SCRIPTS` to a directory, in the environment of the server process, not
only the MCP client's. The server reads it once at start, so a running server needs a
`shutdown` and a fresh start ([server.md](server.md#getting-a-variable-to-the-server)).
Without it, `page_eval` does not advertise `script_name` or `script_args`, and a client that
sends them gets an error naming the variable.

The variable names one directory. Any directory works, such as `custom-scripts/` at the
checkout root: it ships empty and git ignores its contents, so a `git pull` never touches
your scripts. Git does not back them up either, and deleting or re-cloning the checkout
deletes them. To try the samples, point the variable at `docs/custom-scripts-samples`; to use
one next to your own scripts, copy it in.

## Which files run

A script is a file directly inside the directory. Its name may contain only letters, digits,
`.`, `_` and `-`, and never `..`. The extension decides how it starts:

| File | Runs as | Needs |
|---|---|---|
| `*.py` | the server's own Python, in isolated mode | nothing: no execute bit, no Python on `PATH` |
| `*.exe`, `*.com` on Windows | the program itself | nothing |
| anything else, on Linux and macOS | the file itself | the execute bit, usually with a shebang line |
| anything else, on Windows | never | |
| `*.bat`, `*.cmd` | never, on any OS | |

Prefer `.py`: it behaves the same everywhere. Isolated mode stops a `json.py` next to your
script from shadowing the standard library and ignores `PYTHON*` variables, and output is
UTF-8 whatever the console code page. A script can import the server's packages, but they
change with the server's dependencies, so stick to the standard library. Batch files are
refused because `cmd.exe` parses its command line a second time, so an argument such as
`"&echo x` would run a second command.

## Arguments, output and limits

- `script_args` is a JSON array of strings, or an empty string for none. The items become the
  command-line arguments. No shell is involved, so `&`, `|` and quotes arrive as plain text.
  An argument containing NUL, CR or LF is refused.
- Stdout, stripped, must not be empty. It is read as UTF-8, and invalid bytes become the
  replacement character.
- The agent sees stderr only when the script fails: a non-zero exit code fails the call and
  returns the code and stderr.
- The script gets no stdin and 30 seconds, then it is killed. A slow script does not hold up
  other clients.

## Writing a script

Print one self-contained JavaScript expression. The samples print a function expression that
is called at once and returns a value; `page_eval` returns the last expression's value and
waits for a returned Promise. Put values into it as JSON literals, never as raw strings:

```python
js = TEMPLATE.replace('"@@TITLE@@"', json.dumps(args.title))
```

`json.dumps` output is valid JavaScript, so a quote or a backslash in the title stays data.
Parse numbers with argparse (`type=int`), give fixed sets of values `choices=`, and write the
result with `sys.stdout.buffer.write(js.encode("utf-8"))`.

Scripts run with the server's privileges and produce code that runs in your logged-in
browser. Put only scripts you trust in the directory.

## The samples

`docs/custom-scripts-samples/` holds five. Each takes its arguments through `script_args`.

| Script | Arguments | What the JavaScript does |
|---|---|---|
| `youtube-play-pause.py` | `[]`, or one of `["play"]`, `["pause"]`, `["toggle"]` | clicks the player's play button when `video.paused` says the state needs to change; the default is toggle. It does not check the video afterwards, so if the browser blocked autoplay it can report `success:true` while the video stays paused |
| `gcal-daily-events-js.py` | `["day"]`, `["day","month"]` or `["day","month","year"]` | reads one day's events from Google Calendar's month view; month and year default to today's. Each event comes back as two or three entries, and `eventCount` counts entries, not events |
| `gcal-cal-event-js.py` | `["title","day","time"]` | finds an event on that day of the current month, by exact title or else by event text containing the title and `time` (such as `11:30am`), opens it and reads its details |
| `gcal-monthly-events-js.py` | none | reads the current month's events, grouped by day. Each event comes back as two or three entries, so `eventCount` and `summary.totalEvents` overstate |
| `dom-summarize.py` | none, `["onscreen"]` or `["onscreen","withpos"]` | lists the page's visible interactive elements (inputs, buttons, links, options and anything with an `on*` attribute) as an indented tree and tags each with a `dsid` attribute that stays on the element; `onscreen` keeps only elements inside the viewport (a tab that has no layout, such as a background tab you never showed, returns nothing), and `withpos` adds `pos="x,y,width,height"` and needs `onscreen` |

The Google Calendar scripts depend on class names in Calendar's month view (`.qLWd9c` and a
few others), which Google can change without notice. When one returns nothing, check those
first. They recognize times only in 12-hour form (`9am`, `11:30am`).

They find a day by its position in the month grid, so the month on screen must be the month
the script reads: the one you ask for in `gcal-daily-events-js.py`, the current month in the
other two. A wrong month is not detected: the script returns another day's events with
`success:true`. The grid starts on your first day of the week (Monday on a European
profile). The scripts read the first column header, in the page's language and then in
English, to learn which day that is, and assume Sunday when they cannot tell.
