"""
main.py

Entry point: wires together the connection + drive functions from
linudp_functions.py.
"""
import time

import LinUDP_Commands

ROTARY_DRIVE = "192.168.1.1"
LINEAR_DRIVE = "192.168.1.2"
HOST_IP = "192.168.1.3"


def main():
    if not LinUDP_Commands.establish_connection([ROTARY_DRIVE, LINEAR_DRIVE], HOST_IP):
        print("Failed to activate connection.")
        return

    try:
        LinUDP_Commands.home_drive(LINEAR_DRIVE)
        LinUDP_Commands.get_actual_position(LINEAR_DRIVE)
        LinUDP_Commands.send_mc_Absolute_VAI(LINEAR_DRIVE, -10, 0.1, 1, 1)
        time.sleep(4)
        LinUDP_Commands.motion_finished(LINEAR_DRIVE)
        LinUDP_Commands.get_actual_position(LINEAR_DRIVE)

    finally:
        LinUDP_Commands.close_connection()


if __name__ == "__main__":
    main()
