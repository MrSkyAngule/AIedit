# aiedit

A desktop code editor (Python, PyQt6/PySide6) where AI models work on the whole project,
but never see the parts you closed. The protection is enforced by the editor's code, not by
asking the model nicely in a prompt.

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
| `interface` | function signatures only, not the body (Python) |
| `readonly` | full code, but any edit to it is rejected |
| `confirm` | full code, edits need your review |
| `explain` | the model explains this code |

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

**No shell for the model.** The agent works only through the editor's own tools:
`list_files`, `read_file`, `search`, `edit_file`, `create_file`. Every result passes the
zone and path filters; every proposed edit is checked against the zones by a diff before
you see it. Nothing is written to disk without your approval.

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
   vulnerabilities, sends findings back to the author. Read-only, at most 2 rounds.

Static analyzers (ruff, mypy, bandit, eslint, tsc) run on a temporary copy; only new issues
are sent back for fixing. After the work, a change journal opens with the largest changes
first.

Providers are plain HTTPS clients (Anthropic, OpenAI and OpenAI-compatible servers), no SDKs.
The tool protocol is text-based, so any model can be used.

## Install and run

```bash
pip install ".[pyside6]"      # or ".[pyqt6]"
aiedit path/to/project_or_file
```

Choose the binding explicitly with `AIEDIT_QT=pyside6` or `AIEDIT_QT=pyqt6`.
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
- Tests and analyzers you run are ordinary local processes, not sandboxed.

## Status

Early version. All tests pass on offline and fake providers; it has not yet been
verified end-to-end against real model APIs.

## License

MIT, see [LICENSE](LICENSE). Free to use, including commercially; no warranty.

## Authorship

Concept, protection model and requirements by MrSkyAngel. Implemented with AI assistance
under the author's direction; the author reviewed the code and made the design decisions.
