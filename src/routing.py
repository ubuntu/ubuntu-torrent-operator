import logging

from charms.haproxy.v1.haproxy_route_tcp import HaproxyRouteTcpRequirer
from charms.haproxy.v2.haproxy_route import HaproxyRouteRequirer
from ops.charm import CharmBase

from constants import (
    AQUATIC_PORT,
    HAPROXY_ROUTE_RELATION,
    HAPROXY_ROUTE_TCP_RELATION,
    HOSTNAME,
    TRANSMISSION_PEER_PORT,
)

logger = logging.getLogger(__name__)


class Routing:
    def __init__(self, charm: CharmBase):
        self._charm = charm
        self.tracker = HaproxyRouteRequirer(
            charm,
            relation_name=HAPROXY_ROUTE_RELATION,
            service="aquatic",
            ports=[AQUATIC_PORT],
            protocol="http",
            hostname=HOSTNAME,
        )
        self.torrent = HaproxyRouteTcpRequirer(
            charm,
            relation_name=HAPROXY_ROUTE_TCP_RELATION,
            port=TRANSMISSION_PEER_PORT,
            backend_port=TRANSMISSION_PEER_PORT,
            enforce_tls=False,
            tls_terminate=False,
        )

    def configure(self):
        self.tracker.provide_haproxy_route_requirements(
            service="aquatic",
            ports=[AQUATIC_PORT],
            protocol="http",
            hostname=HOSTNAME,
        )
        self.torrent.provide_haproxy_route_tcp_requirements(
            port=TRANSMISSION_PEER_PORT,
            backend_port=TRANSMISSION_PEER_PORT,
            enforce_tls=False,
            tls_terminate=False,
        )
