"""
main.py

Entry point: wires together the connection + drive functions from
LinUDP_Commands.py.
"""
import os
import time
from datetime import datetime

import LinUDP_Commands
from CSV_Graph import CSV_Graph
from LinUDP_PseudoScope import PseudoScopeRecorder

ROTARY_DRIVE = "192.168.1.1"
LINEAR_DRIVE = "192.168.1.2"
HOST_IP = "192.168.1.3"

TARGET_FORCE = 0.063

message_delay = 0.1

scope = PseudoScopeRecorder(
    ROTARY_DRIVE,
    channels={2: 1.0, 3: 1.0, 4: 1.0},  # log all three at once, each with its own scale
    samples_per_burst=200,
)

REPORTS_DIR = "reports"


def startup(drives):
    """
    Acknowledge any pending errors on all drives, then switch all of them
    on. Errors are cleared first since a drive in an error state cannot
    be switched on (see manual chapter 2.1.3 state machine).
    """
    for drive in drives:
        LinUDP_Commands.acknowledge_errors(drive)
        time.sleep(message_delay)

    for drive in drives:
        LinUDP_Commands.switch_on(drive)
        time.sleep(message_delay)


def close_connection(drives):
    LinUDP_Commands.send_mc_Absolute_VAI(LINEAR_DRIVE, 0, 0.01, 1, 1)
    time.sleep(5)
    for drive in drives:
        LinUDP_Commands.send_mc_stop(drive)
        time.sleep(1)
    for drive in drives:
        LinUDP_Commands.switch_off(drive)
    LinUDP_Commands.close_connection()


def reset_rotary_position():
    print("Resetting rotary position...")
    actual_position = LinUDP_Commands.get_actual_position(ROTARY_DRIVE) * 10
    print("Actual position " + f"{actual_position} ")
    LinUDP_Commands.send_fc_VAI_GoToPosFromActPos_ResetForceControlSetI(ROTARY_DRIVE, 0, 360, 100, 100)

    while actual_position > 1 or actual_position < -1:
        actual_position = LinUDP_Commands.get_actual_position(ROTARY_DRIVE) * 10
        time.sleep(0.5)

def await_linear_position(demand_position):
    print("Awaiting linear position " + f"{demand_position} ")
    actual_position = LinUDP_Commands.get_actual_position(LINEAR_DRIVE)
    tolerance = 0.5

    while not demand_position - tolerance <= actual_position <= demand_position + tolerance:
        actual_position = LinUDP_Commands.get_actual_position(LINEAR_DRIVE)
        time.sleep(0.5)


def await_rotary_position(demand_position):
    print("Awaiting rotary position " + f"{demand_position} ")
    actual_position = LinUDP_Commands.get_actual_position(ROTARY_DRIVE)
    tolerance = 1

    while not demand_position - tolerance <= actual_position <= demand_position + tolerance:
        actual_position = LinUDP_Commands.get_actual_position(ROTARY_DRIVE)
        time.sleep(0.5)


def start_assembly_process():
    reset_rotary_position()
    time.sleep(message_delay)
    LinUDP_Commands.set_ram(LINEAR_DRIVE, '13A6', 600)
    time.sleep(message_delay)
    LinUDP_Commands.send_mc_Absolute_VAI(LINEAR_DRIVE, -40, 0.01, 1, 1)
    await_linear_position(-30)

    scope.start()

    time.sleep(message_delay)

    LinUDP_Commands.send_fc_VAI_IncActPosWithHigherForceCtrlLimitAndTargetForce(
        ROTARY_DRIVE,
        1600,
        180,
        100,
        0.084,
        0.085
    )
    wait_torque(0.03)

    LinUDP_Commands.send_fc_VAI_IncActPosWithHigherForceCtrlLimitAndTargetForce(
        ROTARY_DRIVE,
        3*360,
        2,
        100,
        0.084,
        0.085
    )

def start_disassembly_process():
    LinUDP_Commands.send_mc_Relative_VAI(ROTARY_DRIVE, -5, 10 / 10000, 100, 100)
    time.sleep(8)

    LinUDP_Commands.send_mc_Absolute_VAI(ROTARY_DRIVE, -3, 60 / 10000, 1, 100)
    await_rotary_position(-3)

    scope.stop()

    LinUDP_Commands.set_ram(LINEAR_DRIVE, '13A6', 1000)
    time.sleep(message_delay)
    LinUDP_Commands.send_mc_Absolute_VAI(LINEAR_DRIVE, 0, 0.01, 100, 100)
    await_linear_position(0)

    LinUDP_Commands.send_mc_Absolute_VAI(ROTARY_DRIVE, 0, 360 / 10000, 100, 100)
    await_rotary_position(0)


def wait_torque_stable(target_torque: float,
                       hold_time_s: float,
                       cycle_time_s: float = 0.01,
                       timeout_s: float = 120.0) -> None:
    """
    Wait until measured torque reaches target_torque and remains
    at or above the target continuously for hold_time_s seconds.

    Args:
        get_torque: Function that returns measured torque in Nm.
        target_torque: Required torque in Nm.
        hold_time_s: Required continuous time above target in seconds.
        cycle_time_s: Time between torque measurements in seconds.
        timeout_s: Maximum time to wait in seconds.

    Raises:
        TimeoutError: If target torque is not maintained before timeout.
    """
    start_time = time.time()
    stable_since = None

    while True:
        time.sleep(cycle_time_s)

        torque = LinUDP_Commands.get_measured_force(ROTARY_DRIVE)
        print("Measured_force " + f"{ + torque} Nm")
        now = time.time()

        if now - start_time >= timeout_s:
            raise TimeoutError(
                f"Timeout waiting for torque: "
                f"{torque:.6f} Nm < {target_torque:.6f} Nm"
            )

        if torque >= target_torque:
            if stable_since is None:
                stable_since = now

            if now - stable_since >= hold_time_s:
                return
        else:
            stable_since = None

def wait_torque(target_torque: float, timeout_s: float = 120.0) -> None:

    start_time = time.time()

    while True:
        torque = LinUDP_Commands.get_measured_force(ROTARY_DRIVE)
        print("Measured_force " + f"{+ torque} Nm")
        now = time.time()

        if now - start_time >= timeout_s:
            raise TimeoutError(
                f"Timeout waiting for torque: "
                f"{torque:.6f} Nm < {target_torque:.6f} Nm"
            )

        if torque >= target_torque:
            return


def build_report_path():
    """
    Build a unique path in the reports folder for this run's CSV log,
    using the current date and time so previous logs are never overwritten.
    """
    os.makedirs(REPORTS_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"torque_log_{timestamp}.csv"
    return os.path.join(REPORTS_DIR, filename)


def main():
    if not LinUDP_Commands.establish_connection([ROTARY_DRIVE, LINEAR_DRIVE], HOST_IP):
        print("Failed to activate connection.")
        return

    report_path = build_report_path()

    try:
        startup([ROTARY_DRIVE, LINEAR_DRIVE])
        LinUDP_Commands.home_drive(LINEAR_DRIVE)
        time.sleep(message_delay)

        start_assembly_process()

        wait_torque(TARGET_FORCE)
        LinUDP_Commands.send_mc_stop(ROTARY_DRIVE)

        time.sleep(2)

        start_disassembly_process()

        scope.convert()
        scope.save_csv(report_path)


    finally:
        close_connection([LINEAR_DRIVE, ROTARY_DRIVE])
        graph = CSV_Graph(report_path)
        graph.save_png()
        graph.display()


if __name__ == "__main__":
    main()
