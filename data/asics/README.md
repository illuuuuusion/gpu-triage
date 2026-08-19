# ASIC channel/lane profiles

This directory is a provenance gate, not a collection of guessed memory
layouts. Production profiles live in `profiles/*.json` and must validate
against the structural contract in `schema-v1.json` plus the stricter semantic
checks in `app/asic_inference.py`.

A profile is usable only when it provides all of the following:

- exact PCI vendor/device IDs and an explicit revision allowlist;
- a semantic mapping version and mapping confidence;
- a precise definition of the address space, unit, helper schema, pattern
  version and experiments consumed by its rules;
- cited primary sources or documented experiments for both channel and lane
  rules;
- cited known-fault fixtures whose expected channel/lane results are executed
  by the loader before the profile can match a report.

The current native helper exposes **allocation-relative byte offsets**, never
physical VRAM addresses. A profile for virtual or physical addresses therefore
cannot be applied to those records. A matching profile produces a bounded
hypothesis with its version, sources and confidence. It never produces a
physical package name.

`profiles/` is intentionally empty. No public mapping has yet met the evidence
and known-fault-validation requirements for the supported hardware. Therefore
the production result is, by design:

```text
Channel/Lane: UNKNOWN
```

Synthetic profiles belong only in tests and must not be copied into the
production catalog.
