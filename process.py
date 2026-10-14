"""
process.py

main.py logic, reorganised into one class so a GUI can drive it.
The drive commands module is passed in, so the same code runs against
the real LinUDP_Commands or against the simulator (sim_backend.py).

Differences to main.py:
  * every wait loop checks the Stop request
  * time.sleep(2) after the rotary stop is replaced by a Yes/No question
  * progress and status are reported through callbacks
  * the needle type (BD / CF) sets the target torque and the torque at
    which the fast tightening phase ends (see NEEDLE_TYPES)
  * the disassembly torque is NOT tracked live: after the CSV report is
    saved, the CSV is parsed, the minimum (most negative) torque value is
    taken and reported as an absolute value
  * on Stop / error: all movement is stopped, then the linear drive is
    sent to 0 and we WAIT until it is really there before switching off
"""
import csv
import glob
import os
import time
import traceback
from datetime import datetime

ROTARY_DRIVE = "192.168.1.1"
LINEAR_DRIVE = "192.168.1.2"
HOST_IP = "192.168.1.3"

# Needle types. Selected in the GUI (or passed to AssemblyProcess).
#   target_force : final tightening target torque [Nm]
#   fast_torque  : torque at which the fast tightening phase ends [Nm]
NEEDLE_TYPES = {
    "BD needle": {"target_force": 0.063, "fast_torque": 0.03},
    "CF needle": {"target_force": 0.02, "fast_torque": 0.01},
}
MESSAGE_DELAY = 0.1
REPORTS_DIR = "reports"

# Which CSV column holds the torque?
#   None  -> auto-detect: first column whose header contains "torque" or "force"
#   "abc" -> column with this header name (case-insensitive)
#   3     -> column index (0 = first column), also works for files without header
TORQUE_COLUMN = "channel2"


class ProcessAborted(Exception):
    """Raised to leave the process early (Stop button, dose answered 'No')."""


def _to_float(text):
    return float(text.strip().replace(",", "."))      # also accepts "0,063"


def _is_number(text):
    try:
        _to_float(text)
        return True
    except ValueError:
        return False


def read_min_torque(csv_path, column=TORQUE_COLUMN):
    """
    Parse the scope CSV and return the minimum (most negative) value of the
    torque column.
    """
    with open(csv_path, newline="") as f:
        sample = f.read(4096)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        rows = [r for r in csv.reader(f, dialect) if r]

    if not rows:
        raise ValueError("the CSV file is empty")

    has_header = not all(_is_number(c) for c in rows[0])
    header = [c.strip() for c in rows[0]] if has_header else None
    data = rows[1:] if has_header else rows

    # ---- choose the torque column
    if isinstance(column, int):
        idx = column
    elif isinstance(column, str):
        lowered = [h.lower() for h in header] if header else []
        if column.strip().lower() not in lowered:
            raise ValueError(f"column '{column}' not found. Columns: {header}")
        idx = lowered.index(column.strip().lower())
    else:
        if header is None:
            raise ValueError("the CSV has no header row - set TORQUE_COLUMN "
                             "in process.py to the column index")
        matches = [i for i, h in enumerate(header)
                   if "torque" in h.lower() or "force" in h.lower()]
        if not matches:
            raise ValueError("no torque column found - set TORQUE_COLUMN in "
                             f"process.py. Columns in the CSV: {header}")
        idx = matches[0]

    values = []
    for row in data:
        try:
            values.append(_to_float(row[idx]))
        except (ValueError, IndexError):
            continue                                  # skip junk rows
    if not values:
        raise ValueError(f"no numeric values found in column {idx}")
    return min(values)


class AssemblyProcess:
    STEPS = [
        "Connecting",
        "Starting drives",
        "Homing linear drive",
        "Resetting rotary position",
        "Moving linear drive down",
        "Tightening (fast)",
        "Tightening (to target force)",
        "Waiting for assembly torque transfer",
        "Waiting for dose confirmation",
        "Disassembly",
        "Saving report",
        "Closing connection",
    ]

    def __init__(self, cmds, scope, graph_cls, status, progress, ask_yes_no,
                 stop_event, torque_cb=None, error_cb=None, stop_wait=1.0,
                 linear_home_timeout=120.0, needle="BD needle",
                 wait_for_assembly_transfer=None,
                 disassembly_torque_ready_cb=None):
        self.cmds = cmds
        self.scope = scope
        self.graph_cls = graph_cls
        self.status = status            # status(text)
        self.progress = progress        # progress(fraction 0..1)
        self.ask_yes_no = ask_yes_no    # ask_yes_no(question) -> bool
        self.stop_event = stop_event    # threading.Event set by Stop button
        self.torque_cb = torque_cb or (lambda value: None)   # abs. disassembly torque [Nm]
        self.error_cb = error_cb or (lambda message: None)   # error_cb(text)
        # The GUI pauses after assembly until the operator transfers the value.
        self.wait_for_assembly_transfer = wait_for_assembly_transfer
        self.disassembly_torque_ready_cb = (
            disassembly_torque_ready_cb or (lambda value: None))
        self._warnings = []             # problems found during shutdown/report
        self.stop_wait = stop_wait
        self.linear_home_timeout = linear_home_timeout
        self.needle = needle
        self.target_force = NEEDLE_TYPES[needle]["target_force"]
        self.fast_torque = NEEDLE_TYPES[needle]["fast_torque"]
        self.graph = None
        self.png_path = None            # saved graph image, if found
        self.assembly_torque = None     # maximum sampled positive torque during tightening [Nm]
        self.disassembly_torque = None  # raw minimum from the CSV (negative)
        self._track_assembly_torque = False
        self._scope_running = False

    # ------------------------------------------------------------ helpers
    def _step(self, name):
        idx = self.STEPS.index(name)
        self.progress(idx / len(self.STEPS))
        self.status(f"Step {idx + 1}/{len(self.STEPS)}: {name}")

    def _warn(self, text):
        print(text)
        self._warnings.append(text)

    def _check_stop(self):
        if self.stop_event.is_set():
            raise ProcessAborted("Stopped by user.")

    def _sleep(self, seconds):
        """time.sleep that reacts to the Stop button."""
        end = time.monotonic() + seconds
        while True:
            self._check_stop()
            remaining = end - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.05, remaining))

    @staticmethod
    def _build_report_path():
        os.makedirs(REPORTS_DIR, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return os.path.join(REPORTS_DIR, f"torque_log_{stamp}.csv")

    @staticmethod
    def _png_files():
        found = set()
        for folder in (REPORTS_DIR, "."):
            found.update(glob.glob(os.path.join(folder, "*.png")))
        return found

    # ------------------------------------------------- disassembly torque
    def _evaluate_disassembly_torque(self, csv_path):
        """Parse the saved CSV, take the minimum torque, report |minimum|."""
        try:
            minimum = read_min_torque(csv_path)
        except Exception as e:
            traceback.print_exc()
            self._warn(f"Could not read the disassembly torque from "
                       f"{csv_path}:\n{e}")
            return
        self.disassembly_torque = minimum
        reported_torque = abs(minimum)
        self.torque_cb(reported_torque)
        print(f"Disassembly torque (min of CSV): {minimum} Nm "
              f"-> reported as {reported_torque} Nm")
        if minimum < 0:
            # Enable Excel transfer only when the CSV contains a valid negative
            # break-away torque, as expected during disassembly.
            self.disassembly_torque_ready_cb(reported_torque)
        else:
            self._warn("The minimum torque in the CSV is not negative "
                       f"({minimum} Nm), so no disassembly torque was "
                       "recorded. Check the torque column / the scope data.")

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

    def _await_position(self, drive, demand, tolerance):
        """
        Wait until the drive is within +-tolerance of demand. Also returns
        if the drive crossed `demand` between two polls (e.g. while passing
        -30 on its way to -40), so a fast move can't be missed.
        """
        actual = self.cmds.get_actual_position(drive)
        previous = actual
        while not demand - tolerance <= actual <= demand + tolerance:
            if (previous - demand) * (actual - demand) < 0:
                return
            self._check_stop()
            previous = actual
            time.sleep(0.5)
            actual = self.cmds.get_actual_position(drive)

    def _record_assembly_peak(self, torque):
        """Keep the maximum non-negative torque sampled during tightening."""
        if torque >= 0.0 and (
                self.assembly_torque is None or torque > self.assembly_torque):
            self.assembly_torque = torque

    def _wait_torque(self, target, label, timeout_s=120.0):
        start = time.time()
        last_ui = 0.0
        while True:
            self._check_stop()
            torque = self.cmds.get_measured_force(ROTARY_DRIVE)
            if self._track_assembly_torque:
                self._record_assembly_peak(torque)
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
        self._warn("The linear drive did not reach position 0 before the timeout.")
        return False

    def _shutdown(self):
        c = self.cmds
        drives = [LINEAR_DRIVE, ROTARY_DRIVE]

        try:
            if self._scope_running:
                self.scope.stop()
                self._scope_running = False
        except Exception as e:
            self._warn(f"Error stopping scope: {e}")

        # 1. linear drive to 0 - and really wait until it is there
        try:
            self.status("Linear drive returning to 0...")
            c.send_mc_Absolute_VAI(LINEAR_DRIVE, 0, 0.01, 1, 1)
            self._wait_linear_at_zero()
        except Exception as e:
            self._warn(f"Error moving linear drive to 0: {e}")

        # 2. stop + switch off + close
        for d in drives:
            try:
                c.send_mc_stop(d)
                time.sleep(self.stop_wait)
            except Exception as e:
                self._warn(f"Error stopping {d}: {e}")
        for d in drives:
            try:
                c.switch_off(d)
            except Exception as e:
                self._warn(f"Error switching off {d}: {e}")
        try:
            c.close_connection()
        except Exception as e:
            self._warn(f"Error closing connection: {e}")

    # ----------------------------------------------------------------- run
    def run(self):
        c = self.cmds
        self.assembly_torque = None
        self.disassembly_torque = None
        self._track_assembly_torque = False
        self.png_path = None
        self.torque_cb(None)

        self._step("Connecting")
        if not c.establish_connection([ROTARY_DRIVE, LINEAR_DRIVE], HOST_IP):
            self.status("Failed to activate connection.")
            self.progress(0)
            self.error_cb("Failed to activate the connection to the drives.\n\n"
                          "Check the cables, the drive power and the IP "
                          f"addresses ({ROTARY_DRIVE}, {LINEAR_DRIVE}, "
                          f"host {HOST_IP}).")
            return False

        self._warnings = []
        report_path = self._build_report_path()
        ok = False
        final_msg = ""
        error_text = None

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
            self._track_assembly_torque = True
            c.send_fc_VAI_IncActPosWithHigherForceCtrlLimitAndTargetForce(
                ROTARY_DRIVE, 1600, 180, 100, 0.084, 0.085)
            self._wait_torque(self.fast_torque, "Tightening (fast)")

            self._step("Tightening (to target force)")
            c.send_fc_VAI_IncActPosWithHigherForceCtrlLimitAndTargetForce(
                ROTARY_DRIVE, 3 * 360, 2, 100, 0.084, 0.085)
            self._wait_torque(self.target_force, "Tightening (to target)")
            c.send_mc_stop(ROTARY_DRIVE)
            # Include one final reading after stopping the rotary drive.
            self._record_assembly_peak(c.get_measured_force(ROTARY_DRIVE))
            self._track_assembly_torque = False
            if self.assembly_torque is None:
                raise RuntimeError("No assembly torque measurement was recorded.")

            self._sleep(MESSAGE_DELAY)
            c.send_mc_Absolute_VAI(LINEAR_DRIVE, 0, 0.01, 100, 100)
            self._await_position(LINEAR_DRIVE, 0, 1)

            # The operator must transfer the measured assembly torque before
            # the dose-confirmation prompt is shown.
            if self.wait_for_assembly_transfer is not None:
                self._step("Waiting for assembly torque transfer")
                transferred = self.wait_for_assembly_transfer(self.assembly_torque)
                if not transferred:
                    self._check_stop()
                    raise ProcessAborted(
                        "Assembly torque was not transferred - process aborted.")

            # --- replaces time.sleep(2): ask the operator
            self._step("Waiting for dose confirmation")
            if not self.ask_yes_no("Was the dose applied?"):
                raise ProcessAborted("Dose not applied - process aborted.")

            c.send_mc_Absolute_VAI(LINEAR_DRIVE, -40, 0.01, 1, 1)
            self._await_position(LINEAR_DRIVE, -30, 1)
            self._sleep(1)

            # --- disassembly
            self._step("Disassembly")
            c.send_mc_Relative_VAI(ROTARY_DRIVE, -5, 10 / 10000, 100, 100)
            self._sleep(8)

            c.send_mc_Absolute_VAI(ROTARY_DRIVE, -3, 60 / 10000, 0.1, 100)
            self._await_position(ROTARY_DRIVE, -3, 1)

            self.scope.stop()
            self._scope_running = False

            c.set_ram(LINEAR_DRIVE, '13A6', 1000)
            self._sleep(MESSAGE_DELAY)
            c.send_mc_Absolute_VAI(LINEAR_DRIVE, 0, 0.01, 100, 100)
            self._await_position(LINEAR_DRIVE, 0, 0.5)

            c.send_mc_Absolute_VAI(ROTARY_DRIVE, 0, 360 / 10000, 100, 100)
            self._await_position(ROTARY_DRIVE, 0, 1)

            # --- report: save CSV, then take the minimum torque from it
            self._step("Saving report")
            self.scope.convert()
            self.scope.save_csv(report_path)
            self._evaluate_disassembly_torque(report_path)

            ok = True
            final_msg = "Finished successfully."

        except ProcessAborted as e:
            self._emergency_stop()          # stop all movement right away
            final_msg = str(e) or "Process aborted."
        except Exception as e:
            traceback.print_exc()
            self._emergency_stop()
            final_msg = "Stopped due to an error."
            error_text = f"{type(e).__name__}: {e}"
        finally:
            self._step("Closing connection")
            self._shutdown()                # linear -> 0, then switch off

        # graph (only if a report was actually written)
        if os.path.exists(report_path):
            try:
                before = self._png_files()
                self.graph = self.graph_cls(report_path)
                self.graph.save_png()
                # where did the PNG end up?
                self.png_path = getattr(self.graph, "png_path", None)
                if not self.png_path:
                    new = self._png_files() - before
                    if new:
                        self.png_path = max(new, key=os.path.getmtime)
            except Exception as e:
                print(f"Graph error: {e}")

        self.progress(1.0 if ok else 0.0)
        self.status(final_msg)

        # show errors in a popup (after the safe shutdown has finished)
        problems = []
        if error_text:
            problems.append(error_text)
        problems.extend(self._warnings)
        if problems:
            self.error_cb("\n\n".join(problems))
        return ok
