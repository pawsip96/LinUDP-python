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
    print(f"ActivateConnection")
    return result


def close_connection():
    """Close the UDP connection to all registered drives."""
    global aci
    if aci is not None:
        result = aci.CloseConnection()
        print(f"CloseConnection")
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


def switch_on(target_ip, settle_s=0.5):
    """
    Force the drive into switched-on state: reset bit 0 (SwitchOn) to 0,
    then set it to 1, regardless of its current state.

    Manual: "boolean setSwitchOnBit(TargetIP As String, State as Boolean)"
    -- direct set/reset of Control Word bit 0, unlike SwitchOn() which
    toggles the bit and requires knowing the current state first.
    """
    aci.setSwitchOnBit(target_ip, False)
    time.sleep(settle_s)
    aci.setSwitchOnBit(target_ip, True)
    time.sleep(settle_s)

    if aci.isSwitchOnActive(target_ip):
        print(f"switch_on {target_ip} -> success")
        time.sleep(settle_s)
        return True
    else:
        print(f"switch_on {target_ip} -> FAILED (DLL error: {aci.getDLLError()!r})")
        return False


def switch_off(target_ip):
    """
    Force the drive into switched-off state: reset bit 0 (SwitchOn) to 0.
    """
    aci.setSwitchOnBit(target_ip, False)
    time.sleep(0.5)
    if not aci.isSwitchOnActive(target_ip):
        print(f"switch_off {target_ip} -> success")
        return True
    else:
        print(f"switch_off {target_ip} -> FAILED (DLL error: {aci.getDLLError()!r})")
        return False


def acknowledge_errors(target_ip):
    set_acknowledge_errors_bit(target_ip, False)
    time.sleep(0.5)
    set_acknowledge_errors_bit(target_ip, True)
    time.sleep(0.5)
    set_acknowledge_errors_bit(target_ip, False)
    print(f"Error Acknowledge {target_ip} successful")


def set_acknowledge_errors_bit(target_ip, status):
    if not aci.SetErrorAcknowledgeBit(target_ip, status):
        print(f"Error Acknowledge {target_ip} failed")


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


def set_ram(target_ip, upid, value, timeout_s=5.0, poll_interval_s=0.1):
    """
    Write a parameter to drive RAM (runtime only, lost on reboot).
    Manual: "long setRAM_ByUPID(TargetIP As String, UPID As Uint32, Value As long)"

    Note: in practice this has been observed to return the written value
    itself on success, not strictly 0 as the manual's Table 4 describes --
    treat any of {0, value} as success, and only the documented negative
    codes as real errors.
    """
    upid_i = _upid_to_int(upid)
    value_i = int(value)

    deadline = time.time() + timeout_s
    code = aci.SetRAM_ByUPID(target_ip, upid_i, value_i)
    while code == 2 and time.time() < deadline:
        time.sleep(poll_interval_s)
        code = aci.SetRAM_ByUPID(target_ip, upid_i, value_i)

    known_errors = {192, 193, 194, 195, 208, 9090}
    if code in known_errors:
        status = _PARAM_RETURN_CODES.get(code, f"unknown code {code}")
        print(f"setRAM {target_ip} UPID {upid_i:04X}h = {value_i} -> "
              f"FAILED (code {code}: {status})")
    else:
        print(f"setRAM {target_ip} UPID {upid_i:04X}h = {value_i} -> OK (code {code})")

    return code



# ── Motion commands (LMmt_*, manual chapter 2.3.3) ──────────────────────────

def send_mc_Absolute_VAI(target_ip, target_pos, max_v, acc, dcc):
    """
    Absolute point-to-point move.
    Manual: "boolean LMmt_MoveAbs(TargetIP, Pos1, MaxVel1, Acc1, Dec1)"
    """
    result = aci.LMmt_MoveAbs(target_ip, float(target_pos), float(max_v), float(acc), float(dcc))
    print(f"LMmt_MoveAbs {target_ip} -> pos={target_pos} v={max_v} acc={acc} dcc={dcc} -> {result}")
    return result


def send_mc_Relative_VAI(target_ip, target_pos, max_v, acc, dcc):
    """
    Relative point-to-point move.
    Manual: "boolean LMmt_MoveRel(TargetIP, Pos1, MaxVel1, Acc1, Dec1)"
    """
    result = aci.LMmt_MoveRel(target_ip, float(target_pos), float(max_v), float(acc), float(dcc))
    print(f"LMmt_MoveRel {target_ip} -> pos={target_pos} v={max_v} acc={acc} dcc={dcc} -> {result}")
    return result


def motion_finished(target_ip, timeout=10.0, poll_interval_s=0.1):
    """
    Block until the drive reports motion has finished (isMotionActive == False)
    or timeout (s) elapses.
    """
    deadline = time.time() + timeout
    while aci.isMotionActive(target_ip) and time.time() < deadline:
        time.sleep(poll_interval_s)

    if aci.isMotionActive(target_ip):
        print(f"motion_finished {target_ip} -> TIMEOUT after {timeout}s")
        return False

    print(f"motion_finished {target_ip} -> done")
    return True


# ── Force control commands (LMfc_*, manual chapter 2.3.3,
#    "Force Control" order no. 0150-2503) ───────────────────────────────────

def send_fc_VAI_GoToPosFromActPos_ResetForceControlSetI(
        target_ip, #target drive ip
        target_pos, #target position in °
        max_v, #max velocity in °/s
        acc,
        dcc
):
    """
    'VAI Go To Pos From Act Pos And Reset Force Control Set I'
    Manual: "boolean LMfc_GoToPosRstForceCtrlSetI(TargetIP, Position, Velocity,
             Acceleration, Deceleration)"

    Resets force control to normal position control, setting the I-term to
    the current value of the last force value, while moving to target_pos.
    No force parameters -- the drive reuses the last active force value.
    """
    result = aci.LMfc_GoToPosRstForceCtrlSetI(
        target_ip, float(target_pos/10), float(max_v/10000), float(acc), float(dcc))
    print(f"LMfc_GoToPosRstForceCtrlSetI {target_ip} -> pos={target_pos} "
          f"v={max_v} acc={acc} dcc={dcc} -> {result}")
    return result


def send_fc_VAI_IncActPosWithHigherForceCtrlLimitAndTargetForce(
        target_ip, target_pos, max_v, acc, force_limit, target_force):
    """
    'VAI Inc Act Pos With Higher Force Ctrl Limit and Target Force'
    Manual: "boolean LMfc_IncrementActPosWithHigherForceCtrlLimitAndTargetForce(
             TargetIP, PositionIncrement, MaxVelocity, Acceleration,
             ForceLimit, TargetForce)"

    No deceleration parameter exists for this command.
    """
    result = aci.LMfc_IncrementActPosWithHigherForceCtrlLimitAndTargetForce(
        target_ip, float(target_pos), float(max_v), float(acc),
        float(force_limit), float(target_force))
    print(f"LMfc_IncrementActPosWithHigherForceCtrlLimitAndTargetForce {target_ip} -> "
          f"pos={target_pos} v={max_v} acc={acc} force_limit={force_limit} "
          f"target_force={target_force} -> {result}")
    return result


def send_fc_ChangeTargetForce(target_ip, force):
    """
    Manual: "boolean LMfc_ChangeTargetForce(TargetIP, TargetForce)"
    """
    result = aci.LMfc_ChangeTargetForce(target_ip, float(force))
    print(f"LMfc_ChangeTargetForce {target_ip} -> force={force} -> {result}")
    return result


