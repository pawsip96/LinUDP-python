"""
process.py

Your main.py logic, reorganised into one class so a GUI can drive it.
The drive commands module is passed in, so the same code runs against
the real LinUDP_Commands or against the simulator (sim_backend.py).

Differences to main.py:
  * every wait loop checks the Stop request
  * time.sleep(2) after the rotary stop is replaced by a Yes/No question
  * progress and status are reported through callbacks
  * the disassembly torque (most negative measured torque during the
    disassembly step) is tracked and reported
  * on Stop / error: all movement is stopped, then the linear drive is
    sent to 0 and we WAIT until it is really there before switching off
"""
import os
import time
from datetime import datetime

ROTARY_DRIVE = "192.168.1.1"
LINEAR_DRIVE = "192.168.1.2"
HOST_IP = "192.168.1.3"
TARGET_FORCE = 0.063
MESSAGE_DELAY = 0.1
REPORTS_DIR = "reports"


class ProcessAborted(Exception):
    """Raised to leave the process early (Stop button, dose answered 'No')."""


class AssemblyProcess:
    STEPS = [
        "Connecting",
        "Starting drives",
        "Homing linear drive",
        "Resetting rotary position",
        "Moving linear drive down",
        "Tightening (fast)",
        "Tightening (to target force)",
        "Waiting for dose confirmation",
        "Disassembly",
        "Saving report",
        "Closing connection",
    ]

    def __init__(self, cmds, scope, graph_cls, status, progress, ask_yes_no,
                 stop_event, torque_cb=None, stop_wait=1.0,
                 linear_home_timeout=120.0):
        self.cmds = cmds
        self.scope = scope
        self.graph_cls = graph_cls
        self.status = status            # status(text)
        self.progress = progress        # progress(fraction 0..1)
        self.ask_yes_no = ask_yes_no    # ask_yes_no(question) -> bool
        self.stop_event = stop_event    # threading.Event set by Stop button
        self.torque_cb = torque_cb or (lambda value: None)
        self.stop_wait = stop_wait
        self.linear_home_timeout = linear_home_timeout
        self.graph = None
        self.disassembly_torque = None  # most negative torque [Nm]
        self._last_torque_report = 0.0
        self._scope_running = False

    # ------------------------------------------------------------ helpers
    def _step(self, name):
        idx = self.STEPS.index(name)
        self.progress(idx / len(self.STEPS))
        self.status(f"Step {idx + 1}/{len(self.STEPS)}: {name}")

    def _check_stop(self):
        if self.stop_event.is_set():
            raise ProcessAborted("Stopped by user.")

    def _sleep(self, seconds, track=False):
        """time.sleep that reacts to Stop (and can sample the torque)."""
        end = time.monotonic() + seconds
        interval = 0.01 if track else 0.05
        while True:
            self._check_stop()
            if track:
                self._track_torque()
            remaining = end - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(interval, remaining))

    @staticmethod
    def _build_report_path():
        os.makedirs(REPORTS_DIR, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return os.path.join(REPORTS_DIR, f"torque_log_{stamp}.csv")

    # ------------------------------------------- disassembly torque tracking
    def _track_torque(self):
        """Read the torque once; keep the most negative value seen."""
        torque = self.cmds.get_measured_force(ROTARY_DRIVE)
        if self.disassembly_torque is None or torque < self.disassembly_torque:
            self.disassembly_torque = torque
        self._report_torque()

    def _report_torque(self, force=False):
        now = time.monotonic()
        if force or now - self._last_torque_report > 0.1:   # don't flood GUI
            self._last_torque_report = now
            self.torque_cb(self.disassembly_torque)

    # ------------------------------------------------- waits (from main.py)
    def _reset_rotary(self):
        c = self.cmds
        actual = c.get_actual_position(ROTARY_DRIVE) * 10
        c.send_fc_VAI_GoToPosFromActPos_ResetForceControlSetI(
            ROTARY_DRIVE, 0, 360, 100, 100)
        while actual > 1 or actual < -1:
            self._check_stop()
            actual = c.get_actual_position(ROTARY_DRIVE) * 10
            time.sleep(0.5)

    def _await_position(self, drive, demand, tolerance, track=False):
        """
        Wait until the drive is within +-tolerance of demand. Also returns
        if the drive crossed `demand` between two polls (e.g. while passing
        -30 on its way to -40), so a fast move can't be missed.
        With track=True it polls every 10 ms and samples the torque.
        """
        interval = 0.01 if track else 0.5
        actual = self.cmds.get_actual_position(drive)
        previous = actual
        while not demand - tolerance <= actual <= demand + tolerance:
            if (previous - demand) * (actual - demand) < 0:
                return
            self._check_stop()
            if track:
                self._track_torque()
            previous = actual
            time.sleep(interval)
            actual = self.cmds.get_actual_position(drive)

    def _wait_torque(self, target, label, timeout_s=120.0):
        start = time.time()
        last_ui = 0.0
        while True:
            self._check_stop()
            torque = self.cmds.get_measured_force(ROTARY_DRIVE)
            now = time.time()
            if now - last_ui > 0.2:               # don't flood the GUI
                self.status(f"{label}: {torque:.4f} / {target:.4f} Nm")
                last_ui = now
            if now - start >= timeout_s:
                raise TimeoutError(
                    f"Timeout waiting for torque: "
                    f"{torque:.6f} Nm < {target:.6f} Nm")
            if torque >= target:
                return
            time.sleep(0.01)

    # ------------------------------------------------------------ shutdown
    def _emergency_stop(self):
        """Stop all current movement immediately."""
        for drive in (ROTARY_DRIVE, LINEAR_DRIVE):
            try:
                self.cmds.send_mc_stop(drive)
            except Exception as e:
                print(f"Emergency stop failed on {drive}: {e}")

    def _wait_linear_at_zero(self):
        """Wait (NOT interruptible by Stop) until linear drive is at 0."""
        start = time.time()
        while time.time() - start < self.linear_home_timeout:
            pos = self.cmds.get_actual_position(LINEAR_DRIVE)
            if abs(pos) <= 0.5:
                return True
            time.sleep(0.1)
        print("WARNING: linear drive did not reach 0 before timeout")
        return False

    def _shutdown(self):
        c = self.cmds
        drives = [LINEAR_DRIVE, ROTARY_DRIVE]

        try:
            if self._scope_running:
                self.scope.stop()
                self._scope_running = False
        except Exception as e:
            print(f"Error stopping scope: {e}")

        # 1. linear drive to 0 - and really wait until it is there
        try:
            self.status("Linear drive returning to 0...")
            c.send_mc_Absolute_VAI(LINEAR_DRIVE, 0, 0.01, 1, 1)
            self._wait_linear_at_zero()
        except Exception as e:
            print(f"Error moving linear drive to 0: {e}")

        # 2. stop + switch off + close
        for d in drives:
            try:
                c.send_mc_stop(d)
                time.sleep(self.stop_wait)
            except Exception as e:
                print(f"Error stopping {d}: {e}")
        for d in drives:
            try:
                c.switch_off(d)
            except Exception as e:
                print(f"Error switching off {d}: {e}")
        try:
            c.close_connection()
        except Exception as e:
            print(f"Error closing connection: {e}")

    # ----------------------------------------------------------------- run
    def run(self):
        c = self.cmds
        self.disassembly_torque = None
        self.torque_cb(None)

        self._step("Connecting")
        if not c.establish_connection([ROTARY_DRIVE, LINEAR_DRIVE], HOST_IP):
            self.status("Failed to activate connection.")
            self.progress(0)
            return False

        report_path = self._build_report_path()
        ok = False
        final_msg = ""

        try:
            # --- startup
            self._step("Starting drives")
            for d in (ROTARY_DRIVE, LINEAR_DRIVE):
                c.acknowledge_errors(d)
                self._sleep(MESSAGE_DELAY)
            for d in (ROTARY_DRIVE, LINEAR_DRIVE):
                c.switch_on(d)
                self._sleep(MESSAGE_DELAY)

            self._step("Homing linear drive")
            c.home_drive(LINEAR_DRIVE)
            self._sleep(MESSAGE_DELAY)

            # --- assembly
            self._step("Resetting rotary position")
            self._reset_rotary()
            self._sleep(MESSAGE_DELAY)

            self._step("Moving linear drive down")
            c.set_ram(LINEAR_DRIVE, '13A6', 600)
            self._sleep(MESSAGE_DELAY)
            c.send_mc_Absolute_VAI(LINEAR_DRIVE, -40, 0.01, 1, 1)
            self._await_position(LINEAR_DRIVE, -30, 0.5)

            self.scope.start()
            self._scope_running = True
            self._sleep(MESSAGE_DELAY)

            self._step("Tightening (fast)")
            c.send_fc_VAI_IncActPosWithHigherForceCtrlLimitAndTargetForce(
                ROTARY_DRIVE, 1600, 180, 100, 0.084, 0.085)
            self._wait_torque(0.03, "Tightening (fast)")

            self._step("Tightening (to target force)")
            c.send_fc_VAI_IncActPosWithHigherForceCtrlLimitAndTargetForce(
                ROTARY_DRIVE, 3 * 360, 2, 100, 0.084, 0.085)
            self._wait_torque(TARGET_FORCE, "Tightening (to target)")
            c.send_mc_stop(ROTARY_DRIVE)

            # --- replaces time.sleep(2): ask the operator
            self._step("Waiting for dose confirmation")
            if not self.ask_yes_no("Was the dose applied?"):
                raise ProcessAborted("Dose not applied - process aborted.")

            # --- disassembly (torque is tracked here, 10 ms sampling)
            self._step("Disassembly")
            self.disassembly_torque = None
            c.send_mc_Relative_VAI(ROTARY_DRIVE, -5, 10 / 10000, 100, 100)
            self._sleep(5, track=True)

            c.send_mc_Absolute_VAI(ROTARY_DRIVE, -3, 360 / 10000, 100, 100)
            self._await_position(ROTARY_DRIVE, -3, 1, track=True)
            self._report_torque(force=True)
            print(f"Disassembly torque: {self.disassembly_torque} Nm")

            self.scope.stop()
            self._scope_running = False

            c.set_ram(LINEAR_DRIVE, '13A6', 1000)
            self._sleep(MESSAGE_DELAY)
            c.send_mc_Absolute_VAI(LINEAR_DRIVE, 0, 0.01, 100, 100)
            self._await_position(LINEAR_DRIVE, 0, 0.5)

            c.send_mc_Absolute_VAI(ROTARY_DRIVE, 0, 360 / 10000, 100, 100)
            self._await_position(ROTARY_DRIVE, 0, 1)

            self._step("Saving report")
            self.scope.convert()
            self.scope.save_csv(report_path)

            ok = True
            final_msg = "Finished successfully."

        except ProcessAborted as e:
            self._emergency_stop()          # stop all movement right away
            final_msg = str(e) or "Process aborted."
        except Exception as e:
            self._emergency_stop()
            final_msg = f"Error: {e}"
        finally:
            self._step("Closing connection")
            self._shutdown()                # linear -> 0, then switch off

        # graph (only if a report was actually written)
        if os.path.exists(report_path):
            try:
                self.graph = self.graph_cls(report_path)
                self.graph.save_png()
            except Exception as e:
                print(f"Graph error: {e}")

        self.progress(1.0 if ok else 0.0)
        self.status(final_msg)
        return ok