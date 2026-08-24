"""Helper functions for the Local Audio Source plugin."""

from __future__ import annotations

import asyncio
from typing import Any

from music_assistant_models.config_entries import ConfigValueOption

from .pa_simple import enumerate_pa_sources


async def get_available_input_devices(include_monitors: bool = False) -> list[ConfigValueOption]:
    """
    Scan for available PulseAudio/PipeWire capture sources via `pactl`.

    :param include_monitors: also list sink monitor sources. Off by default.
    """
    loop = asyncio.get_running_loop()
    sources = await loop.run_in_executor(None, _enumerate_pa_sources_safe)

    options: list[ConfigValueOption] = []
    for src in sources:
        if src["is_monitor"] and not include_monitors:
            continue
        label = (
            f"{src['description']} — "
            f"{src['bit_depth']}bit/{src['sample_rate']}Hz/{src['channels']}ch"
        )
        options.append(ConfigValueOption(src["name"], title=label))

    if not options:
        options = [ConfigValueOption("")]
    return options


def resolve_input_device(configured: str, sources: list[dict[str, Any]]) -> str:
    """
    Resolve the configured source name against currently live sources.

    WirePlumber renumbers the trailing `.N` suffix of a Bluetooth capture source (e.g.
    ``bluez_input.<MAC>.2``) on reconnect/renegotiation, so an exact match against the
    configured name can go stale even though "the same" device is still live. Falls back,
    in order: an exact name match, a same-prefix match (unambiguous only), the first live
    non-monitor source, or the configured value unchanged so existing error paths fire.

    :param configured: The source name from the provider config.
    :param sources: Live sources, as returned by enumerate_pa_sources().
    :return: The source name to actually open.
    """
    names: set[str] = {src["name"] for src in sources}
    if configured in names:
        return configured

    if "." in configured:
        prefix = configured.rsplit(".", 1)[0] + "."
        prefix_matches = [name for name in names if name.startswith(prefix)]
        if len(prefix_matches) == 1:
            return prefix_matches[0]

    non_monitor: list[str] = [src["name"] for src in sources if not src["is_monitor"]]
    if non_monitor:
        return non_monitor[0]

    return configured


def _enumerate_pa_sources_safe() -> list[dict[str, Any]]:
    """Enumerate live PA/PipeWire sources, swallowing errors into an empty result."""
    try:
        return enumerate_pa_sources()
    except FileNotFoundError, RuntimeError:
        return []
