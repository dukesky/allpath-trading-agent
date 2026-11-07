"""Offline, read-only research exports for the paper (TDSC R2-R4).

Nothing in this package writes to the database it reads: every entry point
opens the DB through `research.db.open_readonly`. Outputs are CSV/markdown
files in a directory the caller names."""
