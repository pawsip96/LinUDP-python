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
    for drive in drives:
        LinUDP_Commands.switch_off(drive)
    LinUDP_Commands.close_connection()


def start_assembly_process():
    LinUDP_Commands.send_mc_Absolute_VAI(LINEAR_DRIVE, -20, 0.01, 1, 1)
    time.sleep(3)
    LinUDP_Commands.get_actual_position(LINEAR_DRIVE)


def main():
    if not LinUDP_Commands.establish_connection([ROTARY_DRIVE, LINEAR_DRIVE], HOST_IP):
        print("Failed to activate connection.")
        return

    try:
        startup([ROTARY_DRIVE, LINEAR_DRIVE])
        LinUDP_Commands.home_drive(LINEAR_DRIVE)
        time.sleep(message_delay)

        scope = PseudoScopeRecorder(
            ROTARY_DRIVE,
            channels={2: 0.1, 3: 1.0, 4: 0.1},  # log all three at once, each with its own scale
            samples_per_burst=200,
        )
        scope.start()

        LinUDP_Commands.send_fc_VAI_GoToPosFromActPos_ResetForceControlSetI(
            ROTARY_DRIVE,
            0,
            10,
            1,
            0.64,
        )

        LinUDP_Commands.motion_finished(ROTARY_DRIVE)

        time.sleep(10)
        scope.stop()
        scope.save_csv("torque_log.csv")

    finally:
        close_connection([LINEAR_DRIVE, ROTARY_DRIVE])


if __name__ == "__main__":
    main()
