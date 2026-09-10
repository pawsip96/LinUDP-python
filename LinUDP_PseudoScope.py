"""
LinUDP_PseudoScope.py

Continuous multi-channel logging via the LinMot LinUDP.dll PseudoScope
feature (manual chapter 2.3.2). Depends on LinUDP_Commands.aci being
established (i.e. establish_connection() already called) before use.
"""

import threading
import time
import csv

import LinUDP_Commands


class PseudoScopeRecorder:
    """
    Continuously captures actual/demand position, current, and any of
    monitoring channels 2, 3, 4, re-arming the DLL's fixed-length burst
    capture automatically so the whole process is covered.

    channels: dict mapping channel number (2, 3, or 4) -> scale factor,
    e.g. {2: 0.001, 4: 1.0} to log channel 2 scaled by 0.001 and channel 4
    raw. Only channels you include here are read and saved -- pass as many
    of {2, 3, 4} as you need. Channel 1 is reserved for the drive
    timestamp (must be configured to UPID 82h in LinMot-Talk) and is not
    available as a data channel.
    """

    _VALID_CHANNELS = (2, 3, 4)

    def __init__(self, target_ip, channels, samples_per_burst=200):
        invalid = set(channels) - set(self._VALID_CHANNELS)
        if invalid:
            raise ValueError(f"Invalid channel(s) {invalid}; only {self._VALID_CHANNELS} allowed")
        if not channels:
            raise ValueError("channels must contain at least one of {2, 3, 4}")

        self.target_ip = target_ip
        self.channels = dict(channels)  # {channel_number: scale}
        self.samples_per_burst = samples_per_burst

        self._entries = []
        self._last_drive_ts = None
        self._elapsed_ms = 0.0

        self._stop_event = threading.Event()
        self._thread = None
        self._lock = threading.Lock()

    def _set_sample_count(self, n):
        try:
            LinUDP_Commands.aci.PseudoScopeSamples = n
        except (AttributeError, TypeError):
            LinUDP_Commands.aci.PseudoScopeSamples(n)

    def _channel_raw(self, entry, channel):
        return {
            2: entry.MonitoringChannel2,
            3: entry.MonitoringChannel3,
            4: entry.MonitoringChannel4,
        }[channel]

    def _append_samples(self, samples):
        for entry in samples:
            ts_ms = float(entry.TimeStamp)
            if self._last_drive_ts is None:
                delta_ms = 0.0
            else:
                delta_ms = ts_ms - self._last_drive_ts
                if delta_ms < 0:
                    delta_ms += 3600 * 1000  # hourly sub-hour rollover

            self._elapsed_ms += delta_ms
            self._last_drive_ts = ts_ms

            row = {
                'elapsed_s': self._elapsed_ms / 1000.0,
                'actual_pos': entry.ActualPos,
                'demand_pos': entry.DemandPos,
                'current': entry.Current,
            }
            for channel, scale in self.channels.items():
                row[f'channel{channel}'] = self._channel_raw(entry, channel) * scale

            self._entries.append(row)

    def _run_one_burst(self, poll_interval_s=0.02, timeout_s=30.0):
        with self._lock:
            self._set_sample_count(self.samples_per_burst)
            started = LinUDP_Commands.aci.enablePseudoScopeTrace(self.target_ip)

        if not started:
            print(f"EnablePseudoScopeTrace {self.target_ip} -> FAILED to start "
                  f"(DLL error: {LinUDP_Commands.aci.getDLLError()!r})")
            return

        deadline = time.time() + timeout_s
        while LinUDP_Commands.aci.isPseudoScopeSampling() and time.time() < deadline:
            time.sleep(poll_interval_s)

        if LinUDP_Commands.aci.isPseudoScopeSampling():
            print(f"PseudoScope burst on {self.target_ip} -> TIMEOUT after {timeout_s}s")
            return

        with self._lock:
            samples = LinUDP_Commands.aci.getPseudoScopeSamples(self.target_ip)
        self._append_samples(samples)

    def _worker(self):
        while not self._stop_event.is_set():
            self._run_one_burst()

    def start(self):
        self._entries = []
        self._last_drive_ts = None
        self._elapsed_ms = 0.0
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()
        print(f"PseudoScopeRecorder started for {self.target_ip} "
              f"(channels: {sorted(self.channels)})")

    def stop(self, join_timeout_s=5.0):
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=join_timeout_s)
        print(f"PseudoScopeRecorder stopped for {self.target_ip}: "
              f"{len(self._entries)} samples captured")

    def get_data(self):
        return list(self._entries)

    def save_csv(self, path):
        fieldnames = ['elapsed_s', 'actual_pos', 'demand_pos', 'current']
        fieldnames += [f'channel{c}' for c in sorted(self.channels)]

        with open(path, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(self._entries)
        print(f"PseudoScope data written to {path} ({len(self._entries)} rows)")


    def convert(self, channel=2):
        """
        Re-interpret already-captured samples for `channel` as signed
        Int16 and apply the force scale (UPID 1EA1h, 0.00057295779513082
        Nm/count) -- in place, on self._entries.

        PseudoScope monitoring channels are raw 16-bit samples. This
        recovers the raw count using the scale the channel was captured
        with, reinterprets it as a signed Int16 (same convention as
        get_measured_force), then applies FORCE_SCALE directly -- so the
        result is correct Nm regardless of what scale was passed in
        `channels` at construction time (e.g. even if channel 2 was
        captured raw with scale=1.0).

        Call this once after stop() and before save_csv() / get_data().
        Safe to call more than once (idempotent).
        """
        if channel not in self.channels:
            raise ValueError(f"Channel {channel} was not configured for this recorder")

        capture_scale = self.channels[channel]
        if capture_scale == 0:
            raise ValueError(f"Channel {channel} has a zero scale; cannot recover raw counts")

        key = f'channel{channel}'
        with self._lock:
            for row in self._entries:
                raw = round(row[key] / capture_scale) & 0xFFFF
                signed = raw - 0x10000 if raw & 0x8000 else raw
                row[key] = signed * LinUDP_Commands.FORCE_SCALE

        print(f"PseudoScope channel {channel}: {len(self._entries)} samples "
              f"reinterpreted as signed Int16 and converted to Nm "
              f"(scale={LinUDP_Commands.FORCE_SCALE})")