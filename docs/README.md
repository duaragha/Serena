# Serena Documentation

## Start here

- [Repository and runtime map](repository-map.md): source, private data, runtime state, credentials, caches, and machine ownership
- [Architecture status](serena-gideon-architecture-status.md): implemented systems, acceptance evidence, and remaining physical gates
- [Architecture status, visual edition](serena-gideon-architecture-status.html): the same system review in a browser-friendly format

## Operations

- [Backup and bootstrap](backup-and-bootstrap.md): machine doctor, service installation, private snapshots, restore, and clean reconstruction
- [Windows setup](windows-setup.md): Windows runtime and container setup

## System specifications

- [Brain daemon](spec-brain-daemon.md): resident session, streaming protocol, lifecycle, and acceptance contract
- [Fleet runtime](fleet-runtime.md): renderer-owned terminals, work-unit contracts, capacity recovery, and delivery obligations
- [Knowledge maintenance](knowledge-maintenance-spec.md): knowledge lifecycle and curation contract

## Voice acceptance

Voice and phone documentation stays beside the implementation because its commands and file paths are package-specific:

- [Call runtime](../voice/call/README.md)
- [Wake word](../voice/call/WAKEWORD.md)
- [Wake model training handoff](../voice/call/WAKEWORD_TRAINING.md)
- [iPhone one-call acceptance](../voice/call/IPHONE_CALL_ACCEPTANCE.md)
- [Desk loop](../voice/desk/README.md)
