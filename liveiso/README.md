# Experimental BDF initramfs guard

This directory is a prototype, not a released safe-boot mechanism.  It writes
the non-existent sentinel `gpu-triage-quarantine` to one target's
`driver_override` during mkinitcpio's early-hook phase.  It must be proven to
run before udev coldplug on a VM and on known-good AMD and NVIDIA hardware
before the same-vendor safety gate may be removed.

The normal `go.sh`/`start.sh` path never installs or invokes this hook.

Prototype kernel argument:

```text
gpu_triage.target=0000:03:00.0
```

Markers are written below `/run/gpu-triage-guard/` for later inspection.  A
missing/invalid target fails closed by leaving an error marker; it never falls
back to a global or guessed device.
