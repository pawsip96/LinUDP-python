"""
Reusable LinUDP connection + drive functions, built on the LinMot LinUDP.dll
"Axis Communication Interface" (ACI) .NET object, loaded via pythonnet.

Add new drive functions to this file as you need them -- keep main.py
focused on orchestration (which drives, in what order, with what values).
"""

import clr
import sys
import time

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

    aci = ACI()  # equivalent of "aci = New LinUDP.ACI"

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
import time



# ── RAM parameter access (manual chapter 2.3.3, Table 4) ───────────────────

# Return codes for parameter write operations (Table 4: "Return values
# during parameter access"). Reads (get*) return the raw data value
# directly, not a status code.
_PARAM_RETURN_CODES = {
    0: "OK, command executed",
    2: "Command is running",
    192: "UPID wrong",
    193: "Parameter data type wrong",
    194: "Range error",
    195: "Address usage wrong",
    208: "Odd Address",
    9090: "Timeout during parameter channel access",
}


def _upid_to_int(upid):
    """Accepts a UPID as a hex string ('13A6') or an int, returns int."""
    return int(upid, 16) if isinstance(upid, str) else int(upid)


def get_ram(target_ip, upid):
    """
    Read a parameter's current value from drive RAM.
    Manual: "long getRAM_ByUPID(TargetIP As String, UPID As Uint32)"
    """
    upid_i = _upid_to_int(upid)
    value = aci.getRAM_ByUPID(target_ip, upid_i)
    print(f"getRAM {target_ip} UPID {upid_i:04X}h -> {value}")
    return value


def set_ram(target_ip, upid, value, timeout_s=5.0, poll_interval_s=0.05):
    """
    Write a parameter to drive RAM (runtime only, lost on reboot).
    Manual: "long setRAM_ByUPID(TargetIP As String, UPID As Uint32, Value As long)"

    Polls while the drive reports code 2 ("command is running"), up to
    timeout_s. Returns the final status code (0 = success, see Table 4).
    """
    upid_i = _upid_to_int(upid)

    deadline = time.time() + timeout_s
    code = aci.setRAM_ByUPID(target_ip, upid_i, int(value))
    while code == 2 and time.time() < deadline:
        time.sleep(poll_interval_s)
        code = aci.setRAM_ByUPID(target_ip, upid_i, int(value))

    if code == 0:
        print(f"setRAM {target_ip} UPID {upid_i:04X}h = {value} -> OK")
    else:
        status = _PARAM_RETURN_CODES.get(code, f"unknown code {code}")
        print(f"setRAM {target_ip} UPID {upid_i:04X}h = {value} -> "
              f"FAILED (code {code}: {status})")

    return code

