# ubuntu-torrent-operator

A Juju charm that deploys and manages the BitTorrent seeding infrastructure behind `torrent.ubuntu.com`.

## Components

The charm orchestrates three sub-components:

- **Aquatic** (`src/aquatic.py`) — a high-performance HTTP BitTorrent tracker written in Rust. It runs with an access-list allow mode so only approved torrents can announce.

- **Transmission** (`src/transmission.py`) — the BitTorrent client daemon that seeds Ubuntu ISO files. It watches a directory for new `.torrent` files and auto-adds them, announcing to the Aquatic tracker.

- **Synchronizer** (`src/synchronizer.py`) — a systemd timer that periodically runs `src/torrent-sync.py`, which rsyncs `.torrent` metafiles and ISO images from `cdimage.ubuntu.com`, feeds them to Transmission, and updates the Aquatic access list.

## Data Flow

```
                  cdimage.ubuntu.com
                           ↓
             torrent-sync.py (every 5 min)
          runs `rsync` and manipulates files
            ↙              ↓           ↘
   .torrent files      ISO images     access-list
           ↓               ↓            |
     watch dir        download dir      |
              ↘        ↙                |
               auto-add                 |
                  ↓                     ↓
             Transmission → Aquatic tracker
                      announce
                          
```

## Code Layout

| Path | Purpose |
|---|---|
| `src/charm.py` | Charm entry point — wires lifecycle hooks to the three sub-components |
| `src/aquatic.py` | Aquatic tracker lifecycle |
| `src/transmission.py` | Transmission daemon lifecycle |
| `src/synchronizer.py` | Torrent-sync systemd timer lifecycle |
| `src/torrent-sync.py` | Script that downloads torrents and ISOs |
| `src/constants.py` | Shared paths and user constants |

## Github Actions and Charmhub

The charm is automatically built and published by the CI.

### Refreshing the `CHARMCRAFT_TOKEN`

The `CHARMCRAFT_TOKEN` is what allows the CI to push the charm to charmhub. If it expires, you can refresh it with the following:
```
charmcraft login --export=secrets.auth --charm=ubuntu-torrent-operator  --permission=package-manage --permission=package-view --ttl=$((3600*24*365))
cat secrets.auth
```
