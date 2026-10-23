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

## Testing

Integration tests live in `tests/integration/` and are run against a real
Juju model (they deploy the charm, `haproxy`, `self-signed-certificates` and a
bare `ubuntu` unit, and relate them). They check, in order, that:

1. the charm deploys and all of its components come online;
2. the HAProxy relations work (the `tracker` HTTP route and the `bittorrent`
   TCP route);
3. a local aria2 client downloads the test torrent the charm generates,
   end to end through HAProxy;
4. a torrent served by a bare `ubuntu` unit acting as a fake
   `cdimage.ubuntu.com` is synced by the charm and downloaded by a regular
   torrent client too.

Run them with:

```
uv run --all-extras pytest tests/integration -vv --log-level=INFO -o log_cli=1
```

Pass `--charm-path=/path/to/ubuntu-torrent-operator_amd64.charm` to test a
pre-built charm instead of packing one from source.

The tests rely on a few config options that are primarily useful outside of
production:

| Option | Purpose |
|---|---|
| `tracker_url` | Announce URL used by the seeding client and embedded in the generated test torrent (defaults to `https://<hostname>/announce`). |
| `announce_ip` | Address the seeder advertises to the tracker as its own, so peers reach it through a proxy (e.g. the HAProxy `bittorrent` relation). |
| `allow_http` | Also serve the tracker over plain HTTP on the HAProxy route, in addition to HTTPS. |
| `rsync_base` | Base rsync URL the synchronizer pulls torrents and images from (defaults to `rsync://cdimage.ubuntu.com`). |

## Github Actions and Charmhub

The charm is automatically built and published by the CI.

### Refreshing the `CHARMCRAFT_TOKEN`

The `CHARMCRAFT_TOKEN` is what allows the CI to push the charm to charmhub. If it expires, you can refresh it with the following:
```
charmcraft login --export=secrets.auth --charm=ubuntu-torrent-operator  --permission=package-manage --permission=package-view --ttl=$((3600*24*365))
cat secrets.auth
```
