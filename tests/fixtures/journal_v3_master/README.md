# journal_v3_master

`operations.sqlite` is a schema-v3 operations journal written by master@90071ad (before the modular merge). It was
made through the real API on simulated engines: 3 jobs in one batch run, enhancement done, and a confirmed generation
wave whose fake image engine never finishes. So the snapshot is the state of a Studio that crashed mid-generation.

`expected_recovery.json` is what master's `recover_after_restart` did to a copy of that file. The generator script was
a one-off and is not kept here. Regenerating requires master code from before journal v4.

Used by `tests/regression/test_journal_upgrade_from_master.py`.

`instance-backup-master.tar.gz` is an instance backup created by the same master code from a fresh instance with
generated tokens: one integration token and one MCP token, stored as digests only. Master backups have no
`auth.sqlite`, and the branch must still verify them.
