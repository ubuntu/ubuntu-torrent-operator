from constants import ACCESS_LIST, MAIN_USER, AQUATIC_HOME
import os
import tomlkit
import logging
from pathlib import Path
from textwrap import dedent
from subprocess import check_call, check_output

logger = logging.getLogger(__name__)


class Aquatic:
    def __init__(self, shipped_binary_location):
        # provided by caller
        self._shipped_binary = shipped_binary_location

        self._user = MAIN_USER
        # For use by caller
        self.aquatic_port = 3000

        self._systemd_unit_path = Path("/etc/systemd/system/aquatic.service")
        self._systemd_unit_path.parent.mkdir(parents=True, exist_ok=True)
        self._bin_path = AQUATIC_HOME / "aquatic"
        self._bin_path.parent.mkdir(exist_ok=True, parents=True)
        self._access_list = ACCESS_LIST
        self._config_path = AQUATIC_HOME / "aquatic.toml"
        self._config_path.parent.mkdir(exist_ok=True, parents=True)

    def install(self):
        pass

    def configure(self):
        check_call(["systemctl", "stop", "aquatic.service"])
        self._deploy_binary()
        self._write_systemd_service()
        self._write_configuration()
        check_call(["systemctl", "restart", "aquatic.service"])

    def start(self):
        check_call(["systemctl", "enable", "--now", "aquatic"])

    def stop(self):
        check_call(["systemctl", "disable", "--now", "aquatic"])

    def _deploy_binary(self):
        if not self._shipped_binary.exists():
            logger.error(f"Shipped binary not found at {self._shipped_binary}")
            return
        self._shipped_binary.copy(self._bin_path)
        self._bin_path.chmod(0o755)

    def _write_systemd_service(self):
        logger.info("writing aquatic systemd service file")
        content = dedent(f"""\
            [Unit]
            Description=The Aquatic torrent tracker (https://github.com/greatest-ape/aquatic)
            After=network.target

            [Service]
            Type=simple
            User={self._user}
            Group={self._user}
            ExecStart={self._bin_path} -c {self._config_path}
            Restart=always
            RestartSec=5
            LimitNOFILE=65536
            LimitMEMLOCK=65536000
            AmbientCapabilities=CAP_IPC_LOCK

            [Install]
            WantedBy=multi-user.target
        """)

        self._systemd_unit_path.write_text(content)
        check_call(["systemctl", "daemon-reload"])

    def _write_configuration(self):
        logger.info(f"configuring aquatic in {self._config_path}")
        config = tomlkit.parse(check_output([self._bin_path, "-p"]))
        config["socket_workers"] = os.cpu_count()
        config["log_level"] = "debug"
        config["network"]["runs_behind_reverse_proxy"] = True
        config["metrics"]["run_prometheus_endpoint"] = True
        config["access_list"]["mode"] = "allow"
        config["access_list"]["path"] = str(self._access_list)
        config["privileges"]["drop_privileges"] = (
            False  # This doesn't work for some reason
        )

        self._config_path.write_text(tomlkit.dumps(config))

        check_call(["chown", "-R", f"{self._user}:{self._user}", AQUATIC_HOME])
