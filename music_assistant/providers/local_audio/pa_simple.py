"""Minimal ctypes wrapper around libpulse-simple for direct PA sink PCM streaming."""

from __future__ import annotations

import ctypes
import os
import threading
from typing import Any, Self

from music_assistant.helpers.pulse_capture import get_default_pulse_server
from music_assistant.helpers.pulseaudio import (
    PA_SAMPLE_S16LE,
    PA_SAMPLE_S24LE,
    PA_SAMPLE_S32LE,
    PA_STREAM_PLAYBACK,
    PCM_FORMAT_TO_BIT_DEPTH,
    run_pactl_json,
)
from music_assistant.helpers.pulseaudio import (
    PASampleSpec as _PASampleSpec,
)
from music_assistant.helpers.pulseaudio import (
    get_simple_lib as _get_lib,
)


def _pa_sample_format(bit_depth: int) -> int:
    """Return PA sample format constant for given bit depth."""
    if bit_depth == 32:
        return PA_SAMPLE_S32LE
    if bit_depth == 24:
        # MA delivers in 32-bit containers; _apply_software_volume repacks to
        # packed 3-byte before writing, so PA sees s24le here.
        return PA_SAMPLE_S24LE
    return PA_SAMPLE_S16LE


class PASimpleStream:
    """
    Synchronous PCM playback stream to a named PulseAudio sink.

    All libpulse calls are serialized behind a threading.Lock so that
    concurrent executor threads cannot simultaneously write/free the
    same pa_simple connection, which causes assertion failures in libpulse.
    """

    def __init__(
        self,
        sink_name: str,
        app_name: str,
        rate: int,
        channels: int,
        bit_depth: int = 16,
    ) -> None:
        """Open a synchronous PCM playback stream to the named PulseAudio sink."""
        lib = _get_lib()
        spec = _PASampleSpec(
            format=_pa_sample_format(bit_depth),
            rate=rate,
            channels=channels,
        )
        error = ctypes.c_int(0)
        self._lib = lib
        self._lock = threading.Lock()
        pulse_server = get_default_pulse_server()
        self._conn: int | None = lib.pa_simple_new(
            pulse_server.encode() if pulse_server else None,
            app_name.encode(),
            PA_STREAM_PLAYBACK,
            sink_name.encode(),
            b"playback",
            ctypes.byref(spec),
            None,
            None,
            ctypes.byref(error),
        )
        if not self._conn:
            raise OSError(
                f"pa_simple_new failed for sink '{sink_name}' "
                f"(pa_error={error.value}, server={pulse_server!r})"
            )

    def write(self, data: bytes) -> None:
        """Write a PCM chunk. Blocks until PA has buffered it."""
        with self._lock:
            if not self._conn:
                return
            error = ctypes.c_int(0)
            ret = self._lib.pa_simple_write(self._conn, data, len(data), ctypes.byref(error))
            if ret < 0:
                raise OSError(f"pa_simple_write failed (pa_error={error.value})")

    def drain(self) -> None:
        """Block until all buffered audio has played out."""
        with self._lock:
            if not self._conn:
                return
            error = ctypes.c_int(0)
            self._lib.pa_simple_drain(self._conn, ctypes.byref(error))

    def close(self) -> None:
        """
        Free the PA stream.

        Acquires the lock before zeroing _conn and calling pa_simple_free,
        ensuring no concurrent write() or drain() can touch the pointer
        between the None assignment and the free call.
        """
        with self._lock:
            conn, self._conn = self._conn, None
            if conn:
                self._lib.pa_simple_free(conn)

    def __enter__(self) -> Self:
        """Enter context manager."""
        return self

    def __exit__(self, *_: object) -> None:
        """Exit context manager and close the stream."""
        self.close()


def enumerate_alsa_devices() -> list[dict[str, Any]]:
    """
    Enumerate stereo-capable ALSA output devices via PortAudio.

    Returns device dicts in the same shape as ``enumerate_pa_sinks()`` so
    both backends share the same bridge registration path.
    """
    import sounddevice as _sd  # noqa: PLC0415

    # Find the ALSA host API index
    alsa_hostapi_index: int | None = None
    for i, api in enumerate(_sd.query_hostapis()):
        if "alsa" in api.get("name", "").lower():
            alsa_hostapi_index = i
            break

    devices: list[dict[str, Any]] = []
    for idx, dev in enumerate(_sd.query_devices()):
        if dev.get("max_output_channels", 0) < 2:
            continue
        if alsa_hostapi_index is not None and dev.get("hostapi") != alsa_hostapi_index:
            continue
        try:
            test = _sd.RawOutputStream(
                device=idx,
                samplerate=int(dev.get("default_samplerate", 48000)),
                channels=2,
                dtype="int16",
            )
            test.close()
        except _sd.PortAudioError:
            continue
        name: str = dev.get("name", f"alsa-device-{idx}")

        # Skip virtual ALSA PCM plugins — only keep real hardware nodes.
        # PortAudio enumerates both hw: entries and virtual plugins
        # (sysdefault, front, surround*, dmix, lavrate, upmix, …).
        # Hardware entries always contain "(hw:" in their name.
        if "(hw:" not in name:
            continue

        sample_rate = int(dev.get("default_samplerate", 48000))

        # Build a clean display name: strip the " (hw:C,D)" suffix so the
        # MA player name reads e.g. "Intel Audio: ALC889A Analog" not
        # "Intel Audio: ALC889A Analog (hw:1,0)".
        import re as _re  # noqa: PLC0415

        description = _re.sub(r"\s*\(hw:\d+,\d+\)$", "", name).strip()

        devices.append(
            {
                "name": name,  # stable key — includes (hw:C,D) for uniqueness
                "description": description,  # human-readable MA player label
                "pa_sink_name": None,
                "max_output_channels": dev.get("max_output_channels", 2),
                "sample_rate": sample_rate,
                "bit_depth": 16,
                "is_remap": False,
                "master_device": None,
                "index": idx,
                "hostapi": dev.get("hostapi", 0),
            }
        )
    return devices


def enumerate_pa_sinks() -> list[dict[str, Any]]:
    """
    Enumerate PulseAudio output sinks via pactl.

    :raises FileNotFoundError: if pactl is not installed.
    :raises RuntimeError: if pactl returns unexpected output.
    :returns: List of sink dicts, one per sink, containing name, description,
        sample rate, bit depth, channel map, and remap-sink metadata.
    """
    sinks = []
    for sink in run_pactl_json("sinks"):
        name: str = sink.get("name", "")
        desc: str = sink.get("description", name)
        spec_str: str = sink.get("sample_specification", "")
        driver: str = sink.get("driver", "")
        properties: dict[str, str] = sink.get("properties", {})
        # device.master_device is set by module-remap-sink itself on every
        # sink it creates, and only on those sinks — so it's a reliable way
        # to detect a remap-sink child regardless of what the host reports
        # in the top-level "driver" field. Real PulseAudio reports the
        # literal module name there ("module-remap-sink.c" /
        # "module-alsa-card.c"), but PipeWire's PulseAudio-compatibility
        # layer instead reports a generic "PipeWire" driver string for
        # every sink it manages, so "driver == module-remap-sink.c" alone
        # would silently misdetect every remap sink (and every ALSA-card
        # master sink) as something else on a PipeWire-only system.
        master_device: str | None = properties.get("device.master_device")
        is_remap = master_device is not None or driver == "module-remap-sink.c"
        alsa_card_name: str | None = properties.get("alsa.card_name")
        # pactl --format=json represents channel_map as a comma-separated
        # string (e.g. "front-left,front-right,rear-left,rear-right,...").
        channel_map_str: str = sink.get("channel_map", "")
        channel_map: list[str] = [c for c in channel_map_str.split(",") if c]
        try:
            parts = spec_str.split()
            fmt = parts[0]  # e.g. 's32le'
            channels = int(parts[1].replace("ch", ""))
            sample_rate = int(parts[2].replace("Hz", ""))
            bit_depth = PCM_FORMAT_TO_BIT_DEPTH.get(fmt.lower(), 16)
        except (IndexError, ValueError):  # fmt: skip
            continue
        if channels < 2:
            continue
        sinks.append(
            {
                "name": name,  # stable PA sink name — used for UUID/player-id generation
                "description": desc,  # human-readable label — used as MA player display name
                "pa_sink_name": name,
                "max_output_channels": channels,
                "sample_rate": sample_rate,
                "bit_depth": bit_depth,
                "is_remap": is_remap,
                "master_device": master_device,
                "driver": driver,
                "channel_map": channel_map,
                "alsa_card_name": alsa_card_name,
            }
        )
    return sinks


def suspend_resume_sink(sink_name: str) -> None:
    """
    Suspend then resume a PA sink to reset its underlying ALSA driver state.

    Works around the snd_ctxfi mmap bug (kernel 6.12.x, commit 391e69143d0a)
    where the X-Fi card's DMA transfer stalls after driver init, causing
    pa_simple_write to timeout. A suspend/resume cycle re-initialises the
    ALSA PCM device and clears the stall without requiring a PA or system
    restart.

    Called on ALSA-card master sinks after remap-sink topology creation at
    provider load/reload time. No-op if pactl is not available.

    :param sink_name: PA sink name to suspend and resume.
    """
    import shutil  # noqa: PLC0415
    import subprocess  # noqa: PLC0415
    import time  # noqa: PLC0415

    if not (pactl_bin := shutil.which("pactl")):
        return

    env = {**os.environ}
    if pulse_server := get_default_pulse_server():
        env["PULSE_SERVER"] = pulse_server

    try:
        subprocess.run(  # noqa: S603
            [pactl_bin, "suspend-sink", sink_name, "1"],
            check=True,
            capture_output=True,
            timeout=3,
            env=env,
        )
        time.sleep(0.5)
        subprocess.run(  # noqa: S603
            [pactl_bin, "suspend-sink", sink_name, "0"],
            check=True,
            capture_output=True,
            timeout=3,
            env=env,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):  # fmt: skip
        pass
