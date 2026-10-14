"""
Fake replacements for LinUDP_Commands, PseudoScopeRecorder and CSV_Graph
so the whole process can be tested without any hardware.

Drives move smoothly towards their target over a short, realistic time.
The rotary torque rises as the rotary drive tightens (position > 800),
with a bit of noise, so the torque waits behave like the real thing.
Use SimCommands(speed=3) to run everything 3x faster.
"""
import csv
import os
import random
import threading
import time
import webbrowser
from pathlib import Path

ROTARY = "192.168.1.1"
LINEAR = "192.168.1.2"


class _SimDrive:
    def __init__(self, pos=0.0):
        self.pos0 = self.target = pos
        self.t0 = 0.0
        self.dur = 0.0

    def position(self):
        if self.dur <= 0:
            return self.target
        f = min(1.0, (time.monotonic() - self.t0) / self.dur)
        return self.pos0 + (self.target - self.pos0) * f

    def move_to(self, target, duration):
        self.pos0 = self.position()
        self.target = target
        self.t0 = time.monotonic()
        self.dur = duration

    def stop(self):
        self.pos0 = self.target = self.position()
        self.dur = 0.0

    def moving_negative(self):
        return (self.dur > 0 and self.target < self.pos0
                and time.monotonic() - self.t0 < self.dur)


class SimCommands:
    """Same function names/signatures as LinUDP_Commands (only those used)."""

    def __init__(self, speed=1.0, connection_ok=True):
        self.speed = speed
        self.connection_ok = connection_ok
        self.drives = {ROTARY: _SimDrive(0.0), LINEAR: _SimDrive(5.0)}
        self._rate = {ROTARY: 800.0, LINEAR: 20.0}   # units per second
        self.loosened = False    # True once the joint was unscrewed

    # ---- helpers
    def _log(self, text):
        print(f"[SIM] {text}")

    def _duration(self, drive, delta, rate=None):
        rate = rate or self._rate[drive]
        return max(0.3, min(abs(delta) / rate, 10.0)) / self.speed

    # ---- connection / state machine
    def establish_connection(self, drives, host):
        self._log(f"establish_connection {drives} from {host}")
        time.sleep(0.5 / self.speed)
        return self.connection_ok

    def close_connection(self):
        self._log("close_connection")

    def acknowledge_errors(self, drive):
        self._log(f"acknowledge_errors {drive}")

    def switch_on(self, drive):
        self._log(f"switch_on {drive}")

    def switch_off(self, drive):
        self._log(f"switch_off {drive}")

    def home_drive(self, drive):
        self._log(f"home_drive {drive}")
        self.drives[drive].move_to(0.0, 1.5 / self.speed)
        time.sleep(1.5 / self.speed)

    def set_ram(self, drive, address, value):
        self._log(f"set_ram {drive} {address} = {value}")

    # ---- motion
    def send_mc_Absolute_VAI(self, drive, pos, vel, acc, dec):
        d = self.drives[drive]
        self._log(f"mc_Absolute {drive} -> {pos}")
        if drive == ROTARY and pos < d.position():
            self.loosened = True
        d.move_to(pos, self._duration(drive, pos - d.position()))

    def send_mc_Relative_VAI(self, drive, delta, vel, acc, dec):
        d = self.drives[drive]
        self._log(f"mc_Relative {drive} {delta:+}")
        if drive == ROTARY and delta < 0:
            self.loosened = True
        d.move_to(d.position() + delta, self._duration(drive, delta))

    def send_mc_stop(self, drive):
        self._log(f"mc_stop {drive}")
        self.drives[drive].stop()

    def send_fc_VAI_GoToPosFromActPos_ResetForceControlSetI(
            self, drive, pos, vel, acc, dec):
        d = self.drives[drive]
        self._log(f"fc_GoToPos {drive} -> {pos}")
        d.move_to(pos, self._duration(drive, pos - d.position()))

    def send_fc_VAI_IncActPosWithHigherForceCtrlLimitAndTargetForce(
            self, drive, distance, vel, acc, limit, target):
        d = self.drives[drive]
        self._log(f"fc_IncActPos {drive} +{distance} (vel {vel})")
        self.loosened = False
        # slow velocity -> long move; fast velocity -> short move
        d.move_to(d.position() + distance,
                  self._duration(drive, distance, rate=max(vel, 1) * 1.8))

    # ---- measurements
    def get_actual_position(self, drive):
        return self.drives[drive].position()

    def get_measured_force(self, drive):
        if drive != ROTARY:
            return 0.0
        d = self.drives[ROTARY]
        pos = d.position()
        noise = random.uniform(-0.0003, 0.0003)
        if self.loosened:
            # unscrewing: negative break-away torque while turning back
            if d.moving_negative() and pos > 800.0:
                return -0.05 * (1.0 + 0.3 * random.random()) + noise
            return noise
        # tightening: torque grows once the screw is seated (pos > 800)
        return max(0.0, 0.0001 * max(0.0, pos - 800.0) + noise)


class SimScope:
    """Stand-in for PseudoScopeRecorder: samples the simulated drives."""

    def __init__(self, sim, period=0.05):
        self.sim = sim
        self.period = period
        self._rows = []
        self._running = False
        self._thread = None

    def start(self):
        self._rows = []
        self._running = True
        self._t0 = time.monotonic()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        while self._running:
            self._rows.append([
                round(time.monotonic() - self._t0, 3),
                self.sim.get_measured_force(ROTARY),
                self.sim.get_actual_position(ROTARY),
                self.sim.get_actual_position(LINEAR),
            ])
            time.sleep(self.period)

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=1)

    def convert(self):
        pass

    def save_csv(self, path):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["time_s", "channel2", "channel3", "channel4"])
            w.writerows(self._rows)


class SimGraph:
    """Stand-in for CSV_Graph. Needs matplotlib for the PNG (optional)."""

    def __init__(self, csv_path):
        self.csv_path = csv_path
        self.png_path = str(Path(csv_path).with_suffix(".png"))

    def save_png(self):
        try:
            from matplotlib.figure import Figure
        except ImportError:
            print("[SIM] matplotlib not installed - PNG skipped")
            self.png_path = None
            return
        t, torque = [], []
        with open(self.csv_path) as f:
            for row in csv.DictReader(f):
                t.append(float(row["time_s"]))
                torque.append(float(row["channel2"]))
        fig = Figure(figsize=(8, 4))
        ax = fig.subplots()
        ax.plot(t, torque)
        ax.set_xlabel("Time [s]")
        ax.set_ylabel("Torque [Nm]")
        ax.set_title("Simulated torque log")
        ax.grid(True)
        fig.savefig(self.png_path)

    def display(self):
        if self.png_path and os.path.exists(self.png_path):
            webbrowser.open(Path(self.png_path).resolve().as_uri())
