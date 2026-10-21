# AILT9019-gp38 — Campus Recycling Helper

A brand-aware packaging sorting assistant: user enters a brand or product name, the app identifies its packaging components, matches them against a fixed set of published campus recycling rules, and returns the category with a visible source. If a rule does not match, it refuses rather than guessing.

## Status

Planning stage. The full framework — module layout, interface contracts, priority tiers, task tracking, and test policy — lives in [plan.md](./plan.md).

## For the team

`plan.md` is the single source of truth for this project. Read section 0 (priority tiers) first; every change must keep the progress table in section 8 up to date in the same commit.

## Stack

Python 3.10+ standard library only, plus a single-file HTML frontend. No build step, no third-party packages.

```bash
python server.py     # then open http://localhost:8000
```
