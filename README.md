# aiedit

A desktop code editor (Python, PyQt6/PySide6) where AI models work on the whole project,
but never see the parts you closed. The protection is enforced by the editor's code, not by
asking the model nicely in a prompt.

aiedit makes development with AI easier and safe. The AI writes, runs and checks the code; you
explain the task, read and understand the changes, and decide what the AI must never see. Closed
code never leaves your machine, everything that does is logged, and nothing is written without
your approval.

Everything works together in one editor, not as a set of separate tools to install and wire up:
line-level zones, the outbound filter, the sandbox, code navigation and review by a model from
another vendor all know about the same closed code.

> Интерфейс редактора на русском. Everything the editor sends to models is in English.

## Why

AI coding tools usually offer ignore files (`.cursorignore`, `.clineignore`, `.aiignore`).
By their own documentation these are not access control: terminal commands and explicit
mentions can still read ignored files, and they work only on whole files.
Splitting a project into "safe for AI" and "not safe" parts makes the AI work blind.

aiedit keeps the project whole and closes exactly what matters, down to single lines.

## How the protection works

**Zones in code.** Mark lines in the gutter next to line numbers:

| Level | What the model gets |
|---|---|
| `hidden` | only a placeholder: something is here, N lines |
| `interface` | names, signatures and `#` comments, not the bodies or values (Python) |
| `readonly` | full code, but any edit to it is rejected |
| `confirm` | full code, edits need your review |
| `explain` | the model explains this code |

**Marks for yourself.** A separate kind of mark (Ctrl+6, or the gutter menu) is only for the human: a
teal strip in the gutter and a note you see on hover. It is stored next to the file in `*.aimarks.json`,
moves with the lines, survives AI edits and rollback, and is never part of anything sent to a model: it
lives in a list the request code does not read, and the file that holds it is closed to the agent.

Zones can carry a note ("returns the fee in cents") so the model knows how to use closed
code without seeing it.

What the model actually receives for `examples/example.py` with one `hidden` and one
`interface` zone:

```python
def total(items):
    return sum(items)

<<ai-hidden:0:3 lines>>

<<ai-hidden:1:2 lines | def charge(user, amount):>>

def refund(user, amount):
    return -amount
```

**Closed folders and files.** Right-click in the file tree. Closed paths look non-existent
to the model: they are missing from listings, search and reads.

**No shell for the model.** The agent works only through the editor's own tools. Every result
passes the zone and path filters; every proposed edit is checked against the zones by a diff before
you see it. Nothing is written to disk without your approval.

| Tool | What it does | Guard |
|---|---|---|
| `list_files`, `read_file`, `search` | look at the project | closed paths do not exist, zones are redacted |
| `go_to_definition`, `find_references` | navigate code | a language server (pylsp, pyright, typescript-language-server, gopls) runs on a copy without closed code; without one, Python is parsed by the editor |
| `edit_file`, `create_file`, `delete_file`, `rename_file` | propose changes | applied only after review; files with zones can't be deleted or moved |
| `run_command` | run tests or the program | sandbox, your approval (below) |
| `fetch_url` | read a documentation page | https only; sites outside your list need approval |
| `web_search` | search the web (Brave Search API, your key) | the query passes the outbound filter |
| `install_packages` | ask to `pip install` into `.venv` | your approval every time; PyPI names only; prebuilt wheels only, so no package code runs on install |
| `ask_user` | ask you when the task is unclear | |
| `update_notes` | keep a working plan for a long task | |

**Project rules.** Put an `AGENTS.md` (or `AIEDIT.md`, `CLAUDE.md`) in the project root: stack, how
to run tests, code style, what not to touch. The agent and the conductor get it with every task.
It is read like any other file, so zones and secrets in it stay hidden. Rules are text for the
model, not a lock: protection comes from zones and closed paths.

**Long tasks.** When the conversation grows, the editor clears old tool results and long bodies of
old edits, keeps the task and the last step, and adds a summary: which files are already changed
and the agent's own plan. No extra model call is needed for this. After 40 steps the editor asks
whether to continue and shows how many tokens were sent so far.

**Rollback.** Before applying the agent's edits the editor saves how the files looked. Any applied
run can be rolled back from the menu, including created and deleted files and the zones. Rollback
points live in your user profile, not in the project or git.

**Terminal in a sandbox (Linux; macOS experimental).** The agent can run commands (`pytest`, the program itself) to
check its work, but the editor runs them, not the model:

- each command needs your approval (simple commands like `pytest` can be allowed once and for all);
- it runs under bubblewrap on a copy of the project with the agent's proposed edits applied;
- the copy holds only what the model may see: closed files are absent, `interface` functions keep
  their signatures but raise `NotImplementedError`, `hidden` code and secret lines are gone;
- no network, no home directory, an empty environment (no API keys), a time limit;
- Linux: bubblewrap. macOS: the built-in `sandbox-exec` with a deny-by-default profile; it is
  covered by tests of the profile only, not run on a real Mac yet. Windows: run the editor in WSL;
- whatever the command changes stays in the copy and is thrown away;
- the project's `.venv` is mounted read-only for libraries, but copies of the project's own code installed there
  (a plain `pip install .`) are hidden, so closed code cannot be read from `site-packages`.

So even a command that prints every file in base64 cannot leak closed code: it is not there.

**Asks instead of guessing.** When a task is ambiguous (which file, what behavior, what to do with
existing data), the agent can call `ask_user`: the work pauses, the question shows up in the chat,
you answer in the normal input box, and the agent continues with its context intact. At most
3 questions per run; the reviewer never asks.

**Secrets.** Lines that look like keys, tokens or private keys are hidden automatically.
Output of tests and analyzers is scrubbed before it goes back to a model.

**Outbound filter.** The last check before anything leaves the machine. Every request to every
model (programmers, conductor, reviewer, translator) passes through it, including chat text,
history and zone notes. It looks for secrets, words from your own list (client names, internal
hosts) and lines of closed code you pasted into the chat, and asks: mask, send as is, or cancel.
Masked values become `<<masked-N>>` labels and are restored in the model's reply, so edits around
them still apply to the real text.

**Audit.** Every request is logged locally: which files, how many lines were hidden,
how many characters left the machine (the content itself is not logged).

## AI chain

1. **Translator** (gpt-4o-mini, optional): your task ru→en, answer en→ru. Code, paths and
   secret lines are masked and never sent to it.
2. **Conductor**: splits a complex task into steps and routes them to programmers.
3. **Programmers**: write code through the tools above.
4. **Reviewer** (a model from a different vendor): reads the changes, looks for bugs and
   vulnerabilities, sends findings back to the author. Read-only.

The way code reaches you: the author writes, then **the editor itself runs the tests in the sandbox**. If they fail,
the output goes back to the author, who fixes it, and the tests run again until they are green (at most 6 rounds; the same
failure twice in a row stops the loop). Only then the next step: author (Claude) -> reviewer (GLM) -> the author fixes **and checks his own work**
(re-reads the diff, runs the tests in the sandbox) -> reviewer again, until there are no findings (at most
5 rounds; it stops earlier if the same finding comes back or the author changes nothing, and says why). Then
static analysis runs, its findings also go back to the author, and if it changed the code the reviewer looks
once more. Tests run again whenever the reviewer or the analyzers made the author change the code. Only then are the changes shown
to you, together with the plain result of the last test run (green or red), not the model's own claim.

Static analyzers (ruff, mypy, bandit, eslint, tsc) run on a temporary copy after the review; only new issues
are sent back to the author for fixing. After the work, a change journal opens with the largest changes
first.

Providers are plain HTTPS clients (Anthropic, OpenAI and OpenAI-compatible servers), no SDKs.
The tool protocol is text-based, so any model can be used.

## Install and run

```bash
pip install ".[pyside6]"      # or ".[pyqt6]"
aiedit path/to/project_or_file
```

Choose the binding explicitly with `AIEDIT_QT=pyside6` or `AIEDIT_QT=pyqt6`.
For better code navigation install a language server, for example `pip install python-lsp-server`
(Python) or `npm i -g typescript-language-server typescript` (JS/TS). It is optional.
Connect models in the menu (API keys are stored in `~/.config/aiedit/`, never in the project).
Without keys the editor runs on an offline demo model.

## Tests

```bash
QT_QPA_PLATFORM=offscreen python -m unittest discover -s tests
```

The suite runs on both PyQt6 and PySide6 and uses fake providers and local HTTP servers.

## Limits (what this does not protect)

- The outbound filter recognizes only what it can match: secret patterns, your word list and
  closed lines pasted verbatim. A secret retold in your own words goes out.
- Names leak meaning: an `interface` signature like `calc_discount_for_client_x()` tells
  a lot. Zone notes are sent to the model too.
- Everything that is not closed is sent to the provider you chose; what they do with it
  is governed by their terms.
- `interface` works for Python only; other languages fall back to `hidden`.
- Zones live next to the file, in `*.aimarks.json`. If a file is renamed or copied outside the editor, the editor
  recognizes its closed code by fingerprints (hashes of the closed lines) and keeps it hidden until you decide.
  It matches the closed part line by line: if that part was changed in the copy, it is not recognized.
- `interface` sends the `#` comments of the zone to the model, on purpose, so it knows what the closed names mean.
  A comment can reveal as much as the code: keep secrets and client details out of comments in such zones,
  or use `hidden` there. Docstrings are not sent.
- Tests and analyzers the editor runs on its own (post-edit check, static analysis) are ordinary
  local processes; only the agent's commands go through the sandbox. No sandbox on native Windows.
- In the sandbox, code inside closed zones does not run, and lines that look like secrets are
  removed even if the secret is fake. Code that depends on them fails there.
- Web pages are untrusted input. The model is told not to follow instructions from them, but that
  is a request, not a lock. A page cannot reach your files: it only becomes text for the model.

## Status

Early version. Tried end-to-end against real model APIs: Claude Opus 5.5 as the programmer and GLM 5.3 as
the reviewer, and local qwen2.5-coder models. On code quality it still trails Claude Code running the same
model. The test suite runs on offline and fake providers.

## License

MIT, see [LICENSE](LICENSE). Free to use, including commercially; no warranty.

## Authorship

Concept, protection model and requirements by MrSkyAngel. Implemented with AI assistance
under the author's direction; the author reviewed the code and made the design decisions.
