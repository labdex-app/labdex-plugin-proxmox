# Proxmox VE

[![Release](https://github.com/labdex-app/labdex-plugin-proxmox/actions/workflows/release.yml/badge.svg)](https://github.com/labdex-app/labdex-plugin-proxmox/actions/workflows/release.yml) [![Buy Me a Coffee](https://img.shields.io/badge/buy%20me%20a%20coffee-bitsandbricks-7c3aed?logo=buymeacoffee&logoColor=white&labelColor=181b20)](https://buymeacoffee.com/bitsandbricks)

A [labdex](https://github.com/labdex-app/labdex) plugin. Imports VMs and containers as hosts (a plan you review), links the ones you already have, and reports drift in names, addresses, MACs, node and state. Each kind of change is switched on separately and is always a plan a person approves; with all of them off it never changes Proxmox. “Create guests” creates the VMs and containers you planned in the inventory; “Start and stop guests” and “Change cores and memory” bring linked guests in line with the inventory; “Take snapshots” takes them on request and before each change.

**Version** 1.2 · **Works with** labdex >=1.26,<2 · **Plugin id** `proxmox`

> [!WARNING]
> Built with AI, for home labs only: labdex and its plugins were written largely with an AI
> coding assistant (Claude), directed and reviewed by a human. They haven't had an independent
> security audit and may contain security problems. Run them on a trusted home network.

## Install

It ships with labdex: **Plugins › Marketplace**, then *Install* (nothing is downloaded). Updates arrive through the official Marketplace (signed with the labdex project key).

## Settings

| Setting | Notes |
| --- | --- |
| URL |  |
| API token id | user@realm!name, e.g. labdex@pve!inventory, with the PVEAuditor role (read-only) |
| API token secret |  |
| Check the TLS certificate |  |
| Category for imported guests |  |
| Import templates too |  |
| Ask the QEMU guest agent for addresses |  |
| Guests labdex doesn't know go into Discovery | With their addresses, MAC and name, on every “Check drift”: adopt them there |
| Create guests | Lets labdex create the VMs and containers you plan in the inventory (always a plan you approve). The token then needs VM.Allocate, VM.Clone, VM.Config.*, VM.PowerMgmt, Datastore.AllocateSpace and SDN.Use, e.g. the PVEVMAdmin role plus Datastore.AllocateSpace on the storage |
| Start and stop guests | Lets “Apply guest settings” start a linked guest that is active in the inventory and shut down one that is offline there (always a plan a person approves). The token then needs VM.PowerMgmt |
| Change cores and memory | Lets “Apply guest settings” set the cores and memory of a linked guest to its host's Proxmox settings (always a plan a person approves). The token then needs VM.Config.CPU and VM.Config.Memory |
| Take snapshots | Lets labdex take a snapshot of a linked guest (always a plan a person approves). The token then needs VM.Snapshot |
| Snapshot before a change | With “Take snapshots” on: a labdex-… snapshot before labdex changes a guest's cores or memory or shuts it down, to roll back to |
| Default node | For planned hosts whose “Runs on” isn't a Proxmox node |
| Storage for new guests |  |
| LXC template |  |
| VM template (id to clone) | A cloud-init template; labdex sets the network, addresses and SSH keys through cloud-init |
| Cores |  |
| Memory (MB) |  |
| Container disk (GB) | VMs keep their template's disk unless the host asks for more |
| Bridge | VLAN-aware bridge; the tag is the network's VLAN |
| Bridges per network | Only where a network isn't on the default bridge with its VLAN as the tag |
| DNS server for new guests |  |
| Search domain for new guests |  |
| SSH public keys for new guests |  |
| Start new guests |  |
| Wait for a Proxmox task (seconds) |  |

## Actions

- **Check connection** (run)
- **Import guests** (plan): One new host per guest labdex doesn't know; guests that match a host by MAC, address or name are linked to it
- **Create planned guests** (plan): A VM or container for every planned host enabled here that has no guest yet; the inventory's addresses, MACs and VLANs go into Proxmox
- **Create this guest** (plan)
- **Apply guest settings** (plan, always approved by a person): Linked guests whose power state, cores or memory differ from the inventory, for what is switched on
- **Apply this guest's settings** (plan, always approved by a person)
- **Take a snapshot** (plan, always approved by a person)
- **Check drift** (run)

## Releasing

Bump `version` in `plugin.yaml`, add a `## <version>` section to `CHANGELOG.md`, commit, then
publish a GitHub release tagged `v<version>` (web UI, or `gh release create v<version> --target main --notes ""`).
The release workflow signs the plugin with the `LABDEX_SIGNING_KEY` secret, attaches the zip and
`CHANGELOG.md` to the release and puts that version's CHANGELOG section into its description.
Files listed in `.releaseignore` stay out of the zip.
