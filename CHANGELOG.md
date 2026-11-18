# Proxmox VE

## Unreleased
- Guest changes, each switched on separately and always a plan a person approves (whatever the
  approval policy says): *Apply guest settings* starts linked guests that are active in the
  inventory and shuts down ones that are offline there (*Start and stop guests*), and sets cores
  and memory from the host's Proxmox settings (*Change cores and memory*). *Take a snapshot*
  snapshots one guest (*Take snapshots*); with *Snapshot before a change* on, labdex takes one
  before it resizes or shuts down a guest. A switch turned off after planning still wins at
  apply time. Needs labdex 1.26 or later.

## 1.2
- First release as its own repository.
