`scenarios.yaml` defines the scenario grid, the risk and cost profiles and the C
benchmark (SPEC §1.1, §4, §5). It is read on every daily run:

- **Add a scenario**: append it under `scenarios:` (or widen `grid:`). The next run
  creates its A and B ledgers, plus a C ledger if its (capital, cost profile) pair
  is new. No code change.
- **Change a parameter**: the affected scenarios get a new `config_hash`, so their
  old rows are deactivated and new rows (fresh ledgers) start. Scenarios are never
  edited in place.
- **Remove a scenario**: its row is deactivated; its history stays.

On the server the file is baked into the image, so a change ships as a new image
(see `deploy/README.md`). `TRADELAB_SCENARIOS` can point to another file.

The `primary` scenario is pre-registered (SPEC §10): don't change it after the start.

`zero_commission` is a fee-free **reference, not a real broker**: it exists to
measure fee drag and must be labeled that way wherever it's reported. Values
marked `TO VERIFY` are placeholders; each cost profile names its source and the
date it was checked.
