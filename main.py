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

message_delay = 0.1


def main():
    if not LinUDP_Commands.establish_connection([ROTARY_DRIVE, LINEAR_DRIVE], HOST_IP):
        print("Failed to activate connection.")
        return

    try:
        LinUDP_Commands.switch_on(LINEAR_DRIVE)
        time.sleep(message_delay)
        LinUDP_Commands.home_drive(LINEAR_DRIVE)
        time.sleep(message_delay)
        LinUDP_Commands.send_mc_Absolute_VAI(LINEAR_DRIVE, -20, 0.01, 1, 1)
        time.sleep(3)
        LinUDP_Commands.motion_finished(LINEAR_DRIVE)
        time.sleep(message_delay)
        LinUDP_Commands.get_actual_position(LINEAR_DRIVE)


    finally:
        LinUDP_Commands.switch_off(LINEAR_DRIVE)
        LinUDP_Commands.close_connection()


if __name__ == "__main__":
    main()
