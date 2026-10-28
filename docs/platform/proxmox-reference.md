# Proxmox reference deployment

## LXC or VM?

For a trusted personal home server, the compact unprivileged LXC is efficient
and supported by the included bootstrap. It enables Proxmox `nesting` and
`keyctl`, which Docker inside an unprivileged container requires. Proxmox
officially recommends a QEMU VM for Docker when strong isolation,
live migration, or untrusted users matter. Use a VM for a paid/public service;
then follow the existing Debian server guide.

Never disable AppArmor or make the LXC privileged just to make Docker work.
Import-only needs no device passthrough. The optional VPN path adds only
`/dev/net/tun`; it does not enable qBittorrent.

## Compact topology

One LXC runs Omakase and, optionally, Plex. This is the friends/testing default:

```text
Proxmox
└── unprivileged Debian LXC
    ├── Omakase Plus
    ├── Plex (optional profile)
    ├── import root (read-only in Omakase)
    ├── managed library
    └── Gluetun + qBittorrent (optional explicit profile)
```

The LXC bootstrap prepares Docker and directories but intentionally stops
before application startup. Credentials, public exposure, media acquisition,
and an authorized private release cannot be inferred safely.

## Plan and apply

On the Proxmox node:

```bash
cp stack-config.example.env stack-config.env
chmod 0600 stack-config.env
# Edit CTID, bridge, storage, address, and SSH public key.

./bootstrap-omakase-stack.sh plan \
  --config stack-config.env \
  --out omakase-stack-plan.json

# Review every field and preserve the printed hash.
./bootstrap-omakase-stack.sh apply \
  --plan omakase-stack-plan.json \
  --expected-plan-hash <printed-sha256> \
  --receipt omakase-stack-bootstrap-receipt.json
```

Planning is read-only. It refuses an occupied CTID, missing bridge/storage,
unsupported template, unsafe bind mount, absent SSH public key, malformed
static network, implicit public exposure, or a changed release archive. Apply
rechecks those facts, creates a protected unprivileged LXC, installs Docker from
the official repository, creates least-privilege paths, and writes a
secret-free rollback receipt. A partial failure leaves the CT visible for
inspection; it never destroys it automatically.

If a host bind mount is used, reserve a non-symlink path below
`/mnt/bindmounts`. Proxmox does not include bind-mount contents in `vzdump`, and
unprivileged UID mapping must be planned separately. Prefer an LXC-managed
volume for a first install.

## Recommended split topology

At larger scale, use a small app/edge VM or LXC and a separate media VM:

```text
reverse proxy -> Omakase app host -> provider APIs
                         |
                  manifest + scoped Plex API
                         |
                     media host
              Plex + library + transcode
```

This limits a Plex/transcode or FUSE problem from taking out the control plane.
The current bootstrap automates only the compact topology; split storage,
firewall, and backup semantics vary by node/NAS and remain an explicit operator
plan. Do not share SQLite over NFS. Share media through a filesystem designed
for the chosen ownership and locking model, and keep Omakase's application data
local to the app host.

## Firewall and ports

- Omakase: TCP 8766, loopback by default; allow only trusted LAN/reverse proxy.
- Plex: TCP 32400, loopback by default in the example.
- qBittorrent WebUI: TCP 8080 through Gluetun only, loopback by default.
- SSH: restrict at the Proxmox/LXC firewall to administrative sources.

No router forwarding is required. Remote access should use a private overlay
network or an authenticated HTTPS reverse proxy; direct public exposure is not
part of the bootstrap.

Primary reference: the
[Proxmox Container Toolkit manual](https://pve.proxmox.com/pve-docs/pct.1.html),
including its Docker-in-VM recommendation, unprivileged-container model,
`keyctl`/`nesting` behavior, bind-mount backup warning, and security guidance.
