# Proxmox Cloudflare Sync

This is a fork of [AndrewPaglusch/Proxmox-To-Cloudflare-Sync](https://github.com/AndrewPaglusch/Proxmox-To-Cloudflare-Sync). It adds native support for Proxmox LXC containers alongside the original QEMU VM support, plus a set of reliability and safety fixes described below.

This application queries your Proxmox node(s) for all VMs and LXC containers, determines each one's IP address, and keeps the matching Cloudflare A record up to date. If an entity's real IP address cannot be discovered, the tool can optionally predict one, using the entity's VMID as the last octet of an address inside `PREDICT_NETWORK`. This keeps DNS records current even when a VM or LXC's IP address can't be looked up directly.

## How IP addresses are found

- **QEMU VMs**: via the QEMU guest agent (`network-get-interfaces`). Requires the guest agent to be installed and running in the VM.
- **LXC containers**: by parsing the container's network configuration (`netX` lines) for a statically assigned IP. DHCP-configured interfaces (`ip=dhcp`) are skipped, not treated as a literal address.
- **Fallback (both)**: if no real IP address is found and `PREDICT_IP_ADDRESSES=true`, a predicted address is used instead: `PREDICT_NETWORK`'s network address plus the entity's VMID.

Any IP address found this way, static or agent-reported, is only used if it falls inside one of the `VALID_NETWORKS` ranges. Addresses outside those ranges are ignored, and the entity falls through to prediction (or is skipped if prediction is off or its VMID is blacklisted).

## Prerequisites

- Docker and Docker Compose, on a host that can reach both the Proxmox API and the Cloudflare API
- A Proxmox cluster and an API token with `PVEAuditor` permissions
- A Cloudflare account, zone, and an API token with DNS edit permission on that zone

## Configuration

Copy `docker-compose.yml.EXAMPLE` to `docker-compose.yml` and `.env.EXAMPLE` to `.env`, then edit `.env`.

### Required

| Variable | Description |
|---|---|
| `VALID_NETWORKS` | Comma-separated CIDR ranges an entity's discovered IP address must fall within to be used, e.g. `192.168.2.0/24,192.168.3.0/24`. |
| `PREDICT_NETWORK` | CIDR range used when generating a predicted IP address. |
| `PROXMOX_URL` | Your Proxmox API URL, e.g. `https://proxmox-server:8006`. |
| `PROXMOX_NODES_LIST` | Comma-separated Proxmox node names, e.g. `pve01,pve02`. |
| `PROXMOX_TOKEN_NAME` | Proxmox API token name, e.g. `api-user@pam!main`. |
| `PROXMOX_TOKEN` | Proxmox API token secret. |
| `CLOUDFLARE_TOKEN` | Cloudflare API token. |
| `CLOUDFLARE_ZONE` | The Cloudflare-managed domain, e.g. `mydomain.net`. |

### Optional

| Variable | Default | Description |
|---|---|---|
| `PREDICT_IP_ADDRESSES` | `false` | Turn on IP address prediction, based on VMID, when a real address can't be discovered. |
| `PREDICT_IP_ADDRESSES_VMID_BLACKLIST` | (empty) | Comma-separated VMIDs to exclude from prediction, e.g. if they live in a different subnet than `PREDICT_NETWORK`. |
| `CLOUDFLARE_DNS_SUBDOMAIN` | (empty) | Subdomain for the A records, e.g. `nyc`. If unset, records are created directly under `CLOUDFLARE_ZONE`. |
| `DEBUG` | `false` | Set to `true` for verbose logging. |
| `REQUEST_TIMEOUT` | `30` | Per-request timeout, in seconds, for both the Proxmox and Cloudflare APIs. |
| `PROXMOX_VERIFY_SSL` | `false` | Verify the Proxmox API's TLS certificate. Proxmox ships with a self-signed certificate by default, so this defaults to off; set it to `true` once you've replaced it with a trusted certificate. |
| `PRUNE_STALE_RECORDS` | `false` | See **Stale record pruning** below. |
| `INTERVAL` | `1h` | Time between sync attempts, e.g. `10m`, `1h`. Read by the container's entrypoint script, not by `run.py` itself. |

## Stale record pruning

By default this tool only creates and updates A records; it never deletes one. When a VM or LXC is removed from Proxmox, its DNS record is left behind and starts pointing at a decommissioned or possibly reused address. Every sync cycle logs how many such records it finds.

Set `PRUNE_STALE_RECORDS=true` to have those records deleted automatically. This is scoped to your managed suffix: with `CLOUDFLARE_DNS_SUBDOMAIN` set, only records ending in `.<subdomain>.<zone>` are candidates for deletion. **Without a subdomain set, every A record in the zone that doesn't match a currently discovered VM or LXC name is a deletion candidate** - including ones you created by hand for unrelated purposes. If you enable pruning and don't want that exposure, set `CLOUDFLARE_DNS_SUBDOMAIN` so pruning only ever touches records under that subdomain.

A record is only ever considered stale if its VM/LXC is missing from Proxmox's full roster (the node listing APIs), not merely because it failed to report an IP on this particular cycle - a guest agent hiccup or a transient LXC config-read failure won't get a live entity's record deleted. If any configured node can't be listed at all this cycle, the roster is treated as untrustworthy and pruning is skipped entirely for that cycle, logged clearly, regardless of `PRUNE_STALE_RECORDS`.

## Deployment

1. Clone the repository to a host that can reach your Proxmox and Cloudflare APIs:
   ```bash
   git clone https://github.com/vassilsh/Proxmox-To-Cloudflare-Sync.git
   cd Proxmox-To-Cloudflare-Sync/
   ```
2. Copy `docker-compose.yml.EXAMPLE` to `docker-compose.yml` and `.env.EXAMPLE` to `.env`. Fill in `.env` with your values.
3. Start the container:
   ```bash
   docker-compose up -d
   ```

The container runs as a non-root user and reports health via `docker ps` / `docker inspect`, based on whether a sync cycle has completed successfully within the last two `INTERVAL` periods.

## Notes on reliability

- A single unreachable Proxmox node, or a single malformed VM/LXC entry, no longer aborts the whole sync cycle - it's logged and the rest of the fleet is still processed.
- Proxmox and Cloudflare API calls retry with backoff on network errors, timeouts, 5xx responses, and Cloudflare rate limiting (429).
- On a hard failure (bad config, or Proxmox totally unreachable), the container process exits with a non-zero status instead of silently exiting 0, so failures are visible to anything monitoring the container.
