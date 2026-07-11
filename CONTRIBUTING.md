# Contributing

## Setup

```bash
git clone <your fork>
cd Luclas_Open
pip install -r requirements.txt
cp .env.example .env   # set LUC_LLM_BASE_URL and LUC_LLM_MODEL
./luclas.sh
```

## Making a change

1. Fork the repo, create a branch off `master`.
2. Make your change.
3. Before opening a PR, run what CI runs:
   ```bash
   python -m compileall -q luclas
   ruff check --select E9,F63,F7,F82 luclas
   ```
   There's no test suite yet — these two checks catch syntax errors, broken
   imports, and undefined names. If your change touches behavior that's easy
   to break silently (memory, task decomposition, adapters), test it manually
   and describe how in the PR.
4. Open a PR against `master`. CI (`.github/workflows/ci.yml`) runs the same
   checks automatically.

## Scope

- Keep PRs focused — one change per PR is easier to review than a bundle.
- If you're adding a messaging adapter or a new tool, follow the existing
  pattern in `luclas/adapters/` or `luclas/tools/` rather than introducing a
  new structure.
- If a change affects `data/core.md`'s self-update behavior, memory schema,
  or anything else agents build history around, call that out explicitly in
  the PR description — it's not always obvious from the diff alone.

## Reporting bugs / proposing features

Open a GitHub issue. Include what you ran, what you expected, and what
happened instead — for agent behavior bugs, the actual `core.md` state and
relevant memory entries (redact anything sensitive) help a lot more than a
description alone.
