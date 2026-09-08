"""
main.py

Entry point: wires together the connection + drive functions from
LinUDP_Commands.py.
"""
import time

import LinUDP_Commands
from LinUDP_PseudoScope import PseudoScopeRecorder

ROTARY_DRIVE = "192.168.1.1"
LINEAR_DRIVE = "192.168.1.2"
HOST_IP = "192.168.1.3"

message_delay = 0.1


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


def start_assembly_process():
    LinUDP_Commands.send_fc_VAI_GoToPosFromActPos_ResetForceControlSetI(ROTARY_DRIVE, 0, 60, 100, 100)
    time.sleep(message_delay)
    LinUDP_Commands.set_ram(LINEAR_DRIVE, '13A6', 300)
    time.sleep(message_delay)
    LinUDP_Commands.send_mc_Absolute_VAI(LINEAR_DRIVE, -40, 0.01, 1, 1)
    time.sleep(3)
    LinUDP_Commands.get_actual_position(LINEAR_DRIVE)
    time.sleep(message_delay)
    LinUDP_Commands.send_fc_VAI_IncActPosWithHigherForceCtrlLimitAndTargetForce(ROTARY_DRIVE, 6*360, 60, 100, 0.054, 0.055)


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


def main():
    if not LinUDP_Commands.establish_connection([ROTARY_DRIVE, LINEAR_DRIVE], HOST_IP):
        print("Failed to activate connection.")
        return

    try:
        startup([ROTARY_DRIVE, LINEAR_DRIVE])
        LinUDP_Commands.home_drive(LINEAR_DRIVE)
        time.sleep(message_delay)

        # scope = PseudoScopeRecorder(
        #     LINEAR_DRIVE,
        #     channels={2: 0.1, 3: 1.0, 4: 0.1},  # log all three at once, each with its own scale
        #     samples_per_burst=200,
        # )
        # scope.start()


        # start_assembly_process()
        # wait_torque_stable(0.054, 2)
        # LinUDP_Commands.send_fc_ChangeTargetForce(ROTARY_DRIVE, 0)

        # LinUDP_Commands.reboot_drive(LINEAR_DRIVE)

        # scope.stop()
        # scope.save_csv("torque_log.csv")

        LinUDP_Commands.send_mc_Absolute_VAI(LINEAR_DRIVE, -20, 0.01, 1, 1)
        time.sleep(5)


    finally:
        close_connection([LINEAR_DRIVE, ROTARY_DRIVE])


if __name__ == "__main__":
    main()
