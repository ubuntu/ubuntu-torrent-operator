import logging
from pathlib import Path

from ops.charm import CharmBase
from ops.main import main
from ops.model import ActiveStatus, MaintenanceStatus

from aquatic import Aquatic
from transmission import Transmission

logger = logging.getLogger(__name__)


class TorrentCharm(CharmBase):
    def __init__(self, *args):
        super().__init__(*args)

        self._aquatic = Aquatic(self._shipped_aquatic_binary)
        self._transmission = Transmission()

        self.framework.observe(self.on.install, self._on_install)
        self.framework.observe(self.on.config_changed, self._on_config_changed)
        self.framework.observe(self.on.start, self._on_start)
        self.framework.observe(self.on.stop, self._on_stop)
        self.framework.observe(self.on.upgrade_charm, self._on_upgrade_charm)

    @property
    def _shipped_aquatic_binary(self) -> Path:
        return self.framework.charm_dir / "bin" / "aquatic_http"

    def _on_install(self, event):
        self.unit.status = MaintenanceStatus("Installing aquatic")
        self._aquatic.install()
        self._transmission.install()
        self.unit.status = ActiveStatus("Ready")

    def _on_config_changed(self, event):
        self._aquatic.configure()
        self._transmission.configure()
        self.unit.status = ActiveStatus()

    def _on_start(self, event):
        self._aquatic.start()
        self._transmission.start()
        self.unit.status = ActiveStatus()

    def _on_stop(self, event):
        self._aquatic.stop()
        self._transmission.stop()
        self.unit.status = MaintenanceStatus("Stopped")

    def _on_upgrade_charm(self, event):
        self._on_config_changed(event)


if __name__ == "__main__":
    main(TorrentCharm)
