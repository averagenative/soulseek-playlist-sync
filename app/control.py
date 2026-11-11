"""Turning the slskd stack on: nothing, a webhook, or the Docker socket."""
import time

import httpx

from .config import Config


class ControlError(Exception):
    pass


def available(cfg: Config) -> bool:
    return cfg.slskd_control in ("webhook", "docker")


def turn_on(cfg: Config) -> str:
    if cfg.slskd_control == "webhook":
        if not cfg.slskd_control_url:
            raise ControlError("SLSKD_CONTROL_URL is not set")
        try:
            r = httpx.post(cfg.slskd_control_url, timeout=120)
        except httpx.HTTPError as e:
            raise ControlError(f"webhook failed: {e}") from e
        if r.status_code >= 400:
            raise ControlError(f"webhook returned HTTP {r.status_code}: {r.text[:200]}")
        return "Turned on"
    if cfg.slskd_control == "docker":
        if not cfg.slskd_containers:
            raise ControlError("SLSKD_CONTAINERS is not set")
        transport = httpx.HTTPTransport(uds="/var/run/docker.sock")
        with httpx.Client(transport=transport, base_url="http://docker", timeout=60) as d:
            for name in cfg.slskd_containers:      # in order, e.g. gluetun before slskd
                r = d.post(f"/containers/{name}/start")
                if r.status_code not in (204, 304):
                    raise ControlError(f"starting {name}: HTTP {r.status_code} {r.text[:200]}")
                time.sleep(2)
        return "Started " + ", ".join(cfg.slskd_containers)
    raise ControlError("No way to turn slskd on is configured (SLSKD_CONTROL)")
