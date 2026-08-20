"""
Reusable LinUDP connection + drive functions, built on the LinMot LinUDP.dll
"Axis Communication Interface" (ACI) .NET object, loaded via pythonnet.

Add new drive functions to this file as you need them -- keep main.py
focused on orchestration (which drives, in what order, with what values).
"""

import clr
import sys

# Add the folder containing LinUDP.dll to the path, then load the assembly.
sys.path.append(r"C:\Program Files (x86)\LinMot LinUDP Driver and Testtool")
clr.AddReference("LinUDP")

try:
    from LinUDP import ACI  # type: ignore  # the Axis Communication Interface class from the DLL
except ImportError as exc:
    raise ImportError(
        "Could not import LinUDP.ACI. Make sure clr.AddReference('LinUDP') "
        "succeeded and LinUDP.dll is either registered system-wide or its "
        "folder was added to sys.path above."
    ) from exc

# Module-level connection handle, shared by every function below.
# (Equivalent to the VBA "Public Shared ACI As LinUDP.ACI" pattern.)
aci = None


def establish_connection(ip_list, host_ip="", host_port="", target_port=""):
    """
    Register a list of drive IP addresses and establish the UDP connection.

    ip_list:     list of drive IP strings, e.g. ["192.168.1.10", "192.168.1.11"]
    host_ip:     ""  DLL uses first available network adapter
    host_port:   ""  DLL uses default host port 41136
    target_port: ""  DLL uses default drive port 49360
    """
    global aci

    aci = ACI()  # equivalent of "ACI1 = New LinUDP.ACI"

    aci.ClearTargetAddressList()
    for ip in ip_list:
        aci.SetTargetAddressList(ip, target_port)

    result = aci.ActivateConnection(host_ip, host_port)
    print(f"ActivateConnection  {result}")
    return result


def close_connection():
    """Close the UDP connection to all registered drives."""
    global aci
    if aci is not None:
        result = aci.CloseConnection()
        print(f"CloseConnection  {result}")
        return result
    return False


def get_actual_position(target_ip):
    """Read and print the actual slider position (mm) for the given drive."""
    pos = aci.getActualPos(target_ip)
    print(f"Actual position {target_ip}: {pos:.3f} mm")
    return pos


def home_drive(target_ip):
    """
    Perform a homing procedure on the specified drive.

    Blocks until homing completes or the DLL's internal 60s timeout is hit
    (per the manual: "If a timeout of 60s is reached, the function will
    abort and a timeout error message will be created in the DLL error.").
    """
    print(f"Homing {target_ip} ...")
    result = aci.Homing(target_ip)

    if result:
        print(f"Homing {target_ip} success")
    else:
        dll_error = aci.getDLLError()
        print(f"Homing {target_ip}  FAILED (DLL error: {dll_error!r})")

    return result


def switch_on(target_ip):
    """Function checks if motor on switch on, if it's not, it will swich it on"""
    if not aci.isSwitchOnActive(target_ip):
        if aci.SwitchOn(target_ip):
            print(f"SwitchOn {target_ip} success")
            return True
        else:
            print(f"SwitchOn {target_ip} failed")
            return False
    else:
        print(f"SwitchOn {target_ip}: already active")


def acknowledge_errors(target_ip):
    if aci.setErrorAcknowledgeBit(target_ip, True):
        print(f"Error Acknowledge {target_ip} successful")
    else:
        print(f"Error Acknowledge {target_ip} failed")
