import traceback
import time
import os
import multiprocessing as mp
import logging
from datetime import datetime
import threading
from readerwriterlock import rwlock
import LinMot_EtherCAT_Comm_0v86 as ECComm
import LinMot_Data_Handling_Custom_0v12 as DataH
from LinMot_Recorder import LinMot_Recorder
from LinMot_Plot import plot_and_save
from LinMot_Plot_Loosen import plot_disassembly_torque


# ── Drive indices ──────────────────────────────────────────────────────────────
ROTARY_DRIVE = 1
LINEAR_DRIVE = 2

REPORTS_DIR = 'reports'


class LinMot_Ypsomed:
    """
    Implements the Ypsomed syringe cap tighten / loosen sequence.

    One tighten cycle always followed by one loosen cycle.
    Rotary motor is returned to 0 deg at the end.
    A CSV + PNG report is saved to reports/<timestamp>/.
    Loosen sequence torque is sampled and saved as a separate PNG.
    """

    def __init__(self):
        # ── EtherCAT configuration ─────────────────────────────────────────────
        self.ctx           = mp.get_context("spawn")
        self.adapter_id    = r'\Device\NPF_{6A5FA721-CA99-4AF3-A954-F8E439A20495}'
        self.noDev         = 2
        self.cycle_time    = 0.005
        self.no_Monitoring = 4
        self.no_Parameter  = 0
        self.Activate_LMDrive_Data = False
        self.mp_logging    = 40

        # ── Shared state ───────────────────────────────────────────────────────
        self.lock           = self.ctx.Lock()
        self.lm_drive_lock  = rwlock.RWLockFairD()
        self.lm_drive_data_dict = {}
        self.data_length    = 0

        self.unit_scale     = [None] * (self.noDev + 1)
        self.force_scale    = [None] * (self.noDev + 1)
        self.drive_Nr       = [0]    * (self.noDev + 1)
        self.drive_in_error = False

        self.forceControl_Channel = 'mon_ch2'

        # Required by DataH internals
        self.oszi_file_nr         = 0
        self.timestamp_start_oszi = 0
        self.filename             = 'Ypsomed'

        # ── Linear motion parameters ───────────────────────────────────────────
        self.ADD_RESET = False

        # ── Linear motion parameters ───────────────────────────────────────────
        self.LIN_V_FAST      =  0.5    # m/s
        self.LIN_A_FAST      =  1.0    # m/s^2
        self.LIN_V_SLOW      =  0.02   # m/s
        self.LIN_A_SLOW      =  1.0    # m/s^2
        self.LIN_V_RETURN    =  0.05    # m/s
        self.LIN_A_RETURN    =  1.0    # m/s^2
        self.LIN_CURRENT_LOW =  300    # raw  0.6 A  UPID 13A6h
        self.LIN_CURRENT_MEDIUM =  1000    # raw  0.6 A  UPID 13A6h
        self.LIN_CURRENT_MAX = 7500    # raw  7.5 A
        self.LIN_CURRENT_OFF =    0    # raw  0 A

        # ── Rotary: quick tighten ──────────────────────────────────────────────
        self.ROT_V_TIGHTEN_FAST    =  100.0   # deg/s
        self.ROT_A_TIGHTEN_FAST    = 500.0    # deg/s^2
        self.ROT_TARGET_FORCE_FAST =   0.06   # Nm
        self.ROT_FORCE_LIMIT_FAST  =   0.059  # Nm
        self.ROT_FORCE_RANGE_FAST  =   0.006  # Nm  stable when torque >= target - range
        self.ROT_FORCE_HOLD_FAST   =   0.5    # s

        # ── Rotary: slow tighten ───────────────────────────────────────────────
        self.ROT_V_TIGHTEN_SLOW    =  30       # deg/s
        self.ROT_A_TIGHTEN_SLOW    = 300      # deg/s^2
        self.ROT_TARGET_FORCE_SLOW =   0.063  # Nm
        self.ROT_FORCE_LIMIT_SLOW  =   0.062   # Nm
        self.ROT_FORCE_RANGE_SLOW  =   0.003  # Nm
        self.ROT_FORCE_HOLD_SLOW   =   2.0    # s

        # ── Rotary: position moves ─────────────────────────────────────────────
        self.ROT_V_BACK        =  50.0    # deg/s
        self.ROT_A_BACK        = 500.0    # deg/s^2
        self.ROT_D_BACK        = 100.0    # deg/s^2
        self.ROT_TARGET_POS_FC = 6 * 360  # deg  large target so FC engages first

        # ── Force wait timeouts ────────────────────────────────────────────────
        self.FC_TIMEOUT_FAST_S = 60.0    # s  quick tighten
        self.FC_TIMEOUT_SLOW_S = 360.0   # s  slow tighten

        # ── Loosen ────────────────────────────────────────────────────────────
        self.ROT_V_DIS = 240.0    # deg/s
        self.ROT_ACC_DIS = 9000.0   # deg/s^2

        # ── Loosen torque recording ────────────────────────────────────────────
        # List of (elapsed_seconds, torque_Nm) filled during _loosen_sequence.
        self.loosen_torque_samples: list[tuple[float, float]] = []

        # ── Helper instances ───────────────────────────────────────────────────
        self.sendData    = DataH.LinMot_SendData(self)
        self.ProCommData = DataH.LinMot_ProCommData(self)
        self.DC          = DataH.LinMot_DriveCondition(self)
        self.HK          = DataH.LinMot_Housekeeping(self)
        self.MC          = DataH.LinMot_MotionCommand(self)
        self.cfg         = DataH.LinMot_Cfg(self)
        self.DD          = DataH.LinMot_Information(self)
        self.Oszi        = DataH.LinMot_Oszilloscope(self)
        self.FC          = DataH.LinMot_ForceControl(self)

        # ── Data recorder ─────────────────────────────────────────────────────
        self.recorder = LinMot_Recorder(self)

    # ══════════════════════════════════════════════════════════════════════════
    # Public entry point
    # ══════════════════════════════════════════════════════════════════════════

    def start(self):
        """Set up EtherCAT, run one tighten + one loosen cycle, shut down."""
        print('\n=== Ypsomed Syringe Tighten / Loosen Sequence ===')
        print('==================================================\n')

        self.ethercat_comm = ECComm.EtherCATCommunication(
            self.ctx, self.adapter_id, self.noDev,
            self.cycle_time, self.lock,
            self.no_Monitoring, self.no_Parameter,
            self.Activate_LMDrive_Data, self.mp_logging
        )

        try:
            print('--- Connecting to drives ---')
            self.ethercat_comm.start()
            self._await_connection()
            print('EtherCAT communication established.\n')
            time.sleep(0.1)

            self._initialize_drives()
            self._setup_motors()
            self._main_loop()

        except KeyboardInterrupt:
            logging.info('Keyboard interrupt -- stopping.')
            self.ethercat_comm.stop_event.set()

        except Exception as e:
            logging.error(f'Unhandled exception: {e}')
            traceback.print_exc()
            self.ethercat_comm.stop_event.set()

        finally:
            print('\n--- Emergency stop: switching off all motors ---')
            try:
                if self.lm_drive_data_dict:
                    self.recorder.stop()
                    self.ethercat_comm.start()
                    self._await_connection()
                    time.sleep(0.1)
                    self._initialize_drives()
                    self.HK.switch_off_motor(drive=[LINEAR_DRIVE, ROTARY_DRIVE])
                    time.sleep(self.cycle_time * 20)
                    print('  Motors switched off.')
            except Exception as stop_err:
                logging.warning(f'Could not switch off motors cleanly: {stop_err}')

            print('--- Stopping EtherCAT communication ---')

            while not self.ethercat_comm.error_queue.empty():
                print(f'[EtherCAT] Error: {self.ethercat_comm.error_queue.get()}')

            while not self.ethercat_comm.info_queue.empty():
                print(f'[EtherCAT] Info:  {self.ethercat_comm.info_queue.get()}')

            stop_thread = threading.Thread(target=self.ethercat_comm.stop)
            stop_thread.start()

            stop_thread.join(timeout=5.0)  # wait up to 5 seconds

            if stop_thread.is_alive():
                print('[EtherCAT] ERROR: stop() timed out after 5 seconds')
            else:
                print('Done.\n')

    # ══════════════════════════════════════════════════════════════════════════
    # Initialization
    # ══════════════════════════════════════════════════════════════════════════

    def _await_connection(self, timeout_s: float = 30.0):
        if not (self.ethercat_comm.comm_proc and self.ethercat_comm.comm_proc.is_alive()):
            raise RuntimeError('EtherCAT master process did not start. Check adapter_id.')
        deadline = time.time() + timeout_s
        print('  Waiting for EtherCAT master...')
        while time.time() < deadline:
            if not self.ethercat_comm.stop_event.wait(timeout=1.0):
                return
        while not self.ethercat_comm.error_queue.empty():
            print(f'[EtherCAT] Error: {self.ethercat_comm.error_queue.get()}')
        raise RuntimeError('EtherCAT communication could not be established within timeout.')

    def _initialize_drives(self):
        if self.Activate_LMDrive_Data:
            raise RuntimeError('Activate_LMDrive_Data must be False for this script.')

        for i in range(self.noDev):
            self.lm_drive_data_dict[i + 1] = ECComm.LMDrive_Data(
                num_mon_channels=self.no_Monitoring,
                num_par_channels=self.no_Parameter)
        self.data_length = self.ethercat_comm.InputLength

        print('--- Drive configuration ---')
        for i in range(self.noDev):
            drive          = i + 1
            article_nr     = self.cfg.read_cfg(drive=drive, header='Read_Value_RAM', UPID='000C')
            self.drive_Nr[drive] = self.DD.drive_dict[article_nr][0]
            motor_topology = self.cfg.read_cfg(drive=drive, header='Read_Value_RAM', UPID='1195')

            if motor_topology == 1:
                self.DD.config_drive(drive=drive, motor_type='LinMot_Px',
                                     is_rotary_motor=False,
                                     drive_type=self.DD.drive_dict[article_nr])
            elif motor_topology == 2:
                self.DD.config_drive(drive=drive, motor_type='LinMot_Px',
                                     is_rotary_motor=True,
                                     drive_type=self.DD.drive_dict[article_nr])
            else:
                raise TypeError(f'Motor type undefined on Drive {drive} '
                                f'(UPID 1195h = {motor_topology}).')

            cfg = self.lm_drive_data_dict[drive].config
            print(f'  Drive {drive}: rotary={cfg["is_rotary_motor"]}  '
                  f'unit_scale={cfg["unit_scale"]:.4f}  name={cfg["drive_name"]}')
        print()

    def _setup_motors(self):
        print('--- Checking for drive errors ---')
        self._acknowledge_errors()

        print('--- Switching on both motors ---')
        self.HK.switch_on_motor(drive=[LINEAR_DRIVE, ROTARY_DRIVE])
        self._print_status('after switch on')
        print()

        print('--- Homing both motors ---')
        self.HK.home_motor(drive=LINEAR_DRIVE)
        self.HK.home_motor(drive=ROTARY_DRIVE)
        self._print_status('after homing')
        print()

    # ══════════════════════════════════════════════════════════════════════════
    # Main loop
    # ══════════════════════════════════════════════════════════════════════════

    def _main_loop(self):
        """One tighten, one loosen, return to 0 deg, save report."""

        response = input('> Start tighten operation? [yes/no]: ').strip().lower()
        while response not in ('yes', 'y', 'no', 'n'):
            response = input('  Please type yes or no: ').strip().lower()
        if response in ('no', 'n'):
            print('Aborted by user.')
            return

        self.recorder.start()

        try:
            self._tighten_sequence()
        except RuntimeError as e:
            logging.error(f'Tighten sequence drive error: {e}')
            raise
        except Exception as e:
            logging.error(f'Tighten sequence failed: {e}')
            traceback.print_exc()
            raise

        self.recorder.stop()

        response = input('> Start loosen operation? [yes/no]: ').strip().lower()
        while response not in ('yes', 'y', 'no', 'n'):
            response = input('  Please type yes or no: ').strip().lower()
        if response in ('no', 'n'):
            print('Aborted by user.')
            return
        try:
            self._loosen_sequence()
        except Exception as e:
            logging.error(f'Loosen sequence failed: {e}')
            traceback.print_exc()
            raise

        self._save_report()

        print('--- Switching off both motors ---')
        self.HK.switch_off_motor(drive=[LINEAR_DRIVE, ROTARY_DRIVE])
        self._print_status('final')
        print('\nSequence complete.')

    # ══════════════════════════════════════════════════════════════════════════
    # Tighten sequence
    # ══════════════════════════════════════════════════════════════════════════

    def _tighten_sequence(self):
        """
        Execute one full tighten cycle:

            Lin: fast to -20 mm
            Lin: set current limit to 0.6 A
            Lin: slow to -40 mm  (simultaneously with rotary tighten quick)
            Rot: tighten quickly  to 0.06 Nm, hold 0.5 s
            Rot: reset target force / turn back -180°
            Rot: tighten slowly   to 0.075 Nm, hold 2 s
            Rot: reset target force / hold position (0° increment)
            Lin: return to 0 mm  (restore max current)
        """
        print('\n=== TIGHTEN SEQUENCE ===')


        # ── Step 4: Lin set motor current to 0.6 A ───────────────────────────
        print('  [Lin] Setting motor current to 0.6 A...')
        self.write_cfg_with_retry(drive=LINEAR_DRIVE, header='Write_Value_RAM',
                           UPID='13A6', value=self.LIN_CURRENT_LOW)
        time.sleep(2)
        # Disable position lag so linear motor can be pushed by rotary
        self.write_cfg_with_retry(drive=LINEAR_DRIVE, header='Write_Value_RAM',
                           UPID='1587', value=0)
        time.sleep(2)

        # Reset Rotary position
        self.FC.motion_force_control(
            drive=ROTARY_DRIVE,
            header='VAI Go To Pos From Act Pos And Reset Force Control Set I',
            target_pos=0,
            max_v=360,
            acc=self.ROT_A_TIGHTEN_FAST,
            dcc=self.ROT_A_TIGHTEN_FAST,
            target_force=self.ROT_TARGET_FORCE_SLOW,
            force_limit=self.ROT_FORCE_LIMIT_SLOW,
            execute_mc=True
        )
        self.MC.motion_finished(drive=ROTARY_DRIVE, timeout=10)


        # ── Step 5: Lin slow to -40 mm  +  Step 6: Rot tighten quick ─────────
        # Both commands are fired without waiting so they run simultaneously.
        print('  [Lin] Moving slowly to -40 mm (overshoot) + [Rot] Tighten quick...')
        self.MC.send_motion_command(
            drive=LINEAR_DRIVE, header='Absolute_VAI',
            target_pos=-40,
            max_v=self.LIN_V_SLOW * 1000,
            acc=self.LIN_A_SLOW * 1000,
            dcc=self.LIN_A_SLOW * 1000,
            execute_mc=True
        )
        self.MC.motion_finished(drive=LINEAR_DRIVE, timeout=10)


        #If reset is enabled execute those steps
        if self.ADD_RESET:
            count_nibble_fc1 = self.FC.motion_force_control(
                drive=ROTARY_DRIVE,
                header='VAI Inc Act Pos With Higher Force Ctrl Limit and Target Force',
                target_pos=self.ROT_TARGET_POS_FC,
                max_v=self.ROT_V_TIGHTEN_FAST,
                acc=self.ROT_A_TIGHTEN_FAST,
                target_force=self.ROT_TARGET_FORCE_FAST,
                force_limit=self.ROT_FORCE_LIMIT_FAST,
                execute_mc=True
            )

            # ── Step 7: Wait for force to reach target and hold 0.5 s ────────────
            print(f'  [Rot] Waiting for torque {self.ROT_TARGET_FORCE_FAST} Nm '
                  f'(+/-{self.ROT_FORCE_RANGE_FAST} Nm) for {self.ROT_FORCE_HOLD_FAST} s...')
            self._wait_force_stable(
                count_nibble=count_nibble_fc1,
                target_force=self.ROT_TARGET_FORCE_FAST,
                force_range=self.ROT_FORCE_RANGE_FAST,
                hold_time_s=self.ROT_FORCE_HOLD_FAST,
                timeout_s=self.FC_TIMEOUT_FAST_S,
                step_label='quick tighten'
            )

            # ── Step 8: Rot set target force to 0 (visual reset) ─────────────────
            print('  [Rot] Resetting target force to 0...')
            count_nibble_reset = self.FC.force_control(
                drive=ROTARY_DRIVE, header='Change_Target_Force', force=0, execute_mc=True
            )
            self.MC.command_received_by_drive(drive=ROTARY_DRIVE,
                                              count_nibble=count_nibble_reset)

            # ── Step 9: Rot turn back -180° ───────────────────────────────────────
            print('  [Rot] Turning back -180°...')
            self.FC.motion_force_control(
                drive=ROTARY_DRIVE,
                header='VAI Increment Act Pos And Reset Force Control Set I',
                target_pos=-180,
                max_v=self.ROT_V_BACK,
                acc=self.ROT_A_BACK,
                dcc=self.ROT_D_BACK,
                execute_mc=True
            )
            self.MC.in_target_pos(drive=ROTARY_DRIVE)
            self._print_status('after back -180°')

        # ── Step 10: Rot tighten slowly ───────────────────────────────────────
        print(f'  [Rot] Tightening slowly to {self.ROT_TARGET_FORCE_SLOW} Nm...')
        count_nibble_fc2 = self.FC.motion_force_control(
            drive=ROTARY_DRIVE,
            header='VAI Inc Act Pos With Higher Force Ctrl Limit and Target Force',
            target_pos=self.ROT_TARGET_POS_FC,
            max_v=self.ROT_V_TIGHTEN_SLOW,
            acc=self.ROT_A_TIGHTEN_SLOW,
            target_force=self.ROT_TARGET_FORCE_SLOW,
            force_limit=self.ROT_FORCE_LIMIT_SLOW,
            execute_mc=True
        )

        # ── Step 11: Wait for force to reach target and hold 2 s ─────────────
        print(f'  [Rot] Waiting for torque {self.ROT_TARGET_FORCE_SLOW} Nm '
              f'(+/-{self.ROT_FORCE_RANGE_SLOW} Nm) for {self.ROT_FORCE_HOLD_SLOW} s...')
        self._wait_force_stable(
            count_nibble=count_nibble_fc2,
            target_force=self.ROT_TARGET_FORCE_SLOW,
            force_range=self.ROT_FORCE_RANGE_SLOW,
            hold_time_s=self.ROT_FORCE_HOLD_SLOW,
            timeout_s=self.FC_TIMEOUT_SLOW_S,
            step_label='slow tighten'
        )

        # ── Step 12: Rot set target force to 0 (visual reset) ────────────────
        print('  [Rot] Resetting target force to 0...')
        count_nibble_reset2 = self.FC.force_control(
            drive=ROTARY_DRIVE, header='Change_Target_Force', force=0, execute_mc=True
        )
        self.MC.command_received_by_drive(drive=ROTARY_DRIVE,
                                          count_nibble=count_nibble_reset2)

        # ── Step 13: Rot turn by 0° (switch to position control, hold) ───────
        print('  [Rot] Holding position (0° increment, position control)...')
        self.FC.motion_force_control(
            drive=ROTARY_DRIVE,
            header='VAI Increment Act Pos And Reset Force Control Set I',
            target_pos=0,
            max_v=self.ROT_V_BACK,
            acc=self.ROT_A_BACK,
            dcc=self.ROT_D_BACK,
            execute_mc=True
        )
        self.MC.in_target_pos(drive=ROTARY_DRIVE, timeout=10)

        # ── Step 14: Lin return to 0 mm ─────────────────
        print('  [Lin] Returning to 0 mm, restoring max current...')
        # Reset linear demand position to actual (no jump)
        self.MC.send_motion_command(
            drive=LINEAR_DRIVE, header='Absolute_VAI',
            target_pos=0,
            max_v=self.LIN_V_RETURN * 1000,
            acc=self.LIN_A_RETURN * 1000,
            dcc=self.LIN_A_RETURN * 1000,
            execute_mc=True
        )

        print("Here should be at 0mm Lin")

        self.MC.in_target_pos(drive=LINEAR_DRIVE, timeout=10)
    # ══════════════════════════════════════════════════════════════════════════
    # Loosen sequence
    # ══════════════════════════════════════════════════════════════════════════

    def _loosen_sequence(self):
        """
        Execute the loosen cycle:

            User: take off cover of syringe
            Lin:  move to -40 mm
            User: guide linear motor, gripper on thread
            Rot:  reset to actual position
            Lin:  set motor current to 0 A
            Rot:  turn back to 0°  (untie thread) — torque sampled every cycle
            Lin:  reset to actual position
            Lin:  move quickly to 0 mm
            Lin:  set motor current to 7.5 A (max)
        """
        print('\n=== LOOSEN SEQUENCE ===')
        self.loosen_torque_samples.clear()

        # ── Step 16: Lin move to -40 mm ───────────────────────────────────────
        print('  [Lin] Moving to -40 mm...')
        self.MC.send_motion_command(
            drive=LINEAR_DRIVE, header='Absolute_VAI',
            target_pos=-40,
            max_v=self.LIN_V_RETURN * 1000,  # v=0.1 m/s as in flowchart
            acc=self.LIN_A_RETURN * 1000,
            dcc=self.LIN_A_RETURN * 1000,
            execute_mc=True
        )

        time.sleep(3)
        # self.MC.motion_finished(drive=LINEAR_DRIVE, timeout=10)
        self._print_status('Lin at -40 mm (disassembly)')

        # # Ensure full rotary current
        self.write_cfg_with_retry(drive=ROTARY_DRIVE, header='Write_Value_RAM',
                           UPID='13A6', value=self.LIN_CURRENT_MAX)

        # ── Step 18: Lin set motor current to 0 A ────────────────────────────
        # print('  [Lin] Setting motor current to 0 A (free-wheel)...')
        # self.write_cfg_with_retry(drive=LINEAR_DRIVE, header='Write_Value_RAM',
        #                    UPID='13A6', value=self.LIN_CURRENT_OFF)

        # ── Step 19: Rot turn back to -360° — sample torque every cycle ─────────
        print('  [Rot] Turning back to -360° to untie thread (torque sampled)...')
        self.MC.send_motion_command(
            drive=ROTARY_DRIVE, header='Absolute_VAI',
            target_pos=-360,
            max_v=self.ROT_V_DIS,
            acc=self.ROT_ACC_DIS,
            dcc=self.ROT_ACC_DIS,
            execute_mc=True
        )

        # ── Torque-sampling loop while rotation runs ──────────────────────────
        t_start = time.time()
        last_print_t = -1.0
        LOOSEN_TIMEOUT_S = 2.0

        while True:
            time.sleep(self.cycle_time)
            self.ProCommData.process_input_data()
            self.recorder.sample()

            # Check for drive errors
            with self.lm_drive_lock.gen_rlock():
                for drv in range(1, self.noDev + 1):
                    if self.lm_drive_data_dict[drv].status['error']:
                        code = self.lm_drive_data_dict[drv].status['error_code']
                        raise RuntimeError(
                            f'Drive {drv} error during loosen rotation. '
                            f'Code: 0x{code:02X}')

            elapsed = time.time() - t_start
            torque = self.FC.get_measured_force(drive=ROTARY_DRIVE)
            self.loosen_torque_samples.append((elapsed, torque))

            # Periodic console feedback
            if time.time() - last_print_t >= 1.0:
                print(f'    [loosen] t={elapsed:6.2f}s  torque={torque:+.5f} Nm')
                last_print_t = time.time()

            # Exit once rotary reaches target
            with self.lm_drive_lock.gen_rlock():
                in_target = self.lm_drive_data_dict[ROTARY_DRIVE].status.get(
                    'in_target_pos', False)
            if in_target:
                break

            # Timeout guard
            if elapsed >= LOOSEN_TIMEOUT_S:
                break

        time.sleep(3)
        self._print_status('after untie rotation')

        # self.MC.motion_finished(drive=ROTARY_DRIVE, timeout=20)
        self.write_cfg_with_retry(drive=LINEAR_DRIVE, header='Write_Value_RAM',
                           UPID='1587', value=1)  # re-enable position lag

        time.sleep(1)
        self.MC.send_motion_command(
            drive=LINEAR_DRIVE, header='Absolute_VAI',
            target_pos=0,
            max_v=self.LIN_V_RETURN * 1000,
            acc=self.LIN_A_RETURN * 1000,
            dcc=self.LIN_A_RETURN * 1000,
            execute_mc=True
        )
        # self.MC.motion_finished(drive=LINEAR_DRIVE, timeout=10)

        time.sleep(5)

        self.HK.home_motor(drive=LINEAR_DRIVE)

        # Step 23 is implicit — max current already restored in step 22
        self._print_status('loosen done — Lin at 0 mm')
        print()

        # ── Step 23: Reset actual rotary position ───────────────────────────────────────
        self.FC.motion_force_control(
            drive=ROTARY_DRIVE,
            header='VAI Go To Pos From Act Pos And Reset Force Control Set I',
            target_pos=0,
            max_v=360,
            acc=self.ROT_A_TIGHTEN_FAST,
            dcc=self.ROT_A_TIGHTEN_FAST,
            target_force=self.ROT_TARGET_FORCE_SLOW,
            force_limit=self.ROT_FORCE_LIMIT_SLOW,
            execute_mc=True
        )
        # self.MC.motion_finished(drive=ROTARY_DRIVE, timeout=20)
        time.sleep(5)


    # ══════════════════════════════════════════════════════════════════════════
    # Report: CSV + PNG (sequence) + PNG (loosen torque)
    # ══════════════════════════════════════════════════════════════════════════

    def _save_report(self):
        """
        Write CSV and generate PNG graphs.
        Output folder: reports/<YYYYMMDD_HHMMSS>/

            sequence.csv          -- full recorder data
            sequence.png          -- position / force / velocity overview
            loosen_torque.png     -- loosen-phase torque with peak annotation
        """
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        folder    = os.path.join(REPORTS_DIR, timestamp)
        os.makedirs(folder, exist_ok=True)

        # ── CSV ───────────────────────────────────────────────────────────────
        csv_path = os.path.join(folder, 'sequence.csv')
        self.recorder._write_csv(csv_path)
        print(f'[Report] CSV         --> {csv_path}')

        # ── Sequence overview PNG ─────────────────────────────────────────────
        try:
            png_path = plot_and_save(csv_path, folder, label='sequence')
            print(f'[Report] PNG         --> {png_path}')
        except Exception as e:
            logging.warning(f'[Report] Could not generate sequence plot: {e}')

        # ── Loosen torque PNG ─────────────────────────────────────────────────
        if self.loosen_torque_samples:
            try:
                lt_path = plot_disassembly_torque(
                    self.loosen_torque_samples, folder, label='loosen_torque')
                print(f'[Report] PNG (loosen) --> {lt_path}')
            except Exception as e:
                logging.warning(f'[Report] Could not generate loosen-torque plot: {e}')
        else:
            logging.warning('[Report] No loosen torque samples recorded — skipping plot.')

        print()

    # ══════════════════════════════════════════════════════════════════════════
    # Force-stable wait
    # ══════════════════════════════════════════════════════════════════════════

    def _wait_force_stable(self, count_nibble: int, target_force: float,
                           force_range: float, hold_time_s: float,
                           timeout_s: float = 120.0,
                           step_label: str = '') -> None:
        """
        Block until torque >= (target_force - force_range) continuously
        for hold_time_s seconds. Samples recorder every cycle.
        """
        threshold      = target_force - force_range
        t_start        = time.time()
        t_stable_since = None
        last_print_t   = -1.0
        print_interval = 0.1

        print(f'    [{step_label}] Waiting: torque >= {threshold:.4f} Nm '
              f'for {hold_time_s:.1f} s  (timeout {timeout_s:.0f} s)')

        while True:
            time.sleep(self.cycle_time)
            self.ProCommData.process_input_data()
            self.recorder.sample()

            with self.lm_drive_lock.gen_rlock():
                for drv in range(1, self.noDev + 1):
                    if self.lm_drive_data_dict[drv].status['error']:
                        code = self.lm_drive_data_dict[drv].status['error_code']
                        self.drive_in_error = True
                        if code == 81:
                            raise RuntimeError(
                                f'Drive {drv} error 81: Software Key Missing '
                                f'(Force Control license?).')
                        raise RuntimeError(
                            f'Drive {drv} error during "{step_label}". '
                            f'Code: 0x{code:02X}')

            now     = time.time()
            elapsed = now - t_start
            torque  = self.FC.get_measured_force(drive=ROTARY_DRIVE)

            if elapsed > timeout_s:
                logging.warning(
                    f'[{step_label}] Timeout after {timeout_s:.0f} s. '
                    f'Last torque={torque:.5f} Nm  threshold={threshold:.4f} Nm. '
                    f'Continuing anyway.')
                return

            in_end = self.DC.if_masked_status_word(
                drive=ROTARY_DRIVE, bit_mask=0x0600,
                condition=0x0400, count_nibble=count_nibble)
            if in_end:
                logging.warning(
                    f'[{step_label}] Motor reached end position without resistance.')
                return

            special_active = self.DC.if_masked_status_word(
                drive=ROTARY_DRIVE, bit_mask=0x0600,
                condition=0x0600, count_nibble=count_nibble)

            if special_active:
                if torque >= threshold:
                    if t_stable_since is None:
                        t_stable_since = now
                    if (now - t_stable_since) >= hold_time_s:
                        logging.info(
                            f'[{step_label}] Force stable for '
                            f'{now - t_stable_since:.2f} s. Done.')
                        return
                else:
                    t_stable_since = None

            if now - last_print_t >= print_interval:
                held = 0.0 if t_stable_since is None else (now - t_stable_since)
                pct  = min(100.0, 100.0 * held / hold_time_s)
                print(f'    [{step_label}] '
                      f't={elapsed:6.2f}s  '
                      f'torque={torque:+.5f} Nm  '
                      f'threshold>={threshold:.4f} Nm  '
                      f'held={held:.2f}/{hold_time_s:.1f} s '
                      f'({pct:.0f}%)')
                last_print_t = now

    # ══════════════════════════════════════════════════════════════════════════
    # Motion wait (samples recorder every cycle)
    # ══════════════════════════════════════════════════════════════════════════

    def _wait_motion(self, drive: int):
        """Wait for motion to finish, sampling the recorder on every cycle."""
        while True:
            time.sleep(self.cycle_time)
            self.ProCommData.process_input_data()
            self.recorder.sample()
            with self.lm_drive_lock.gen_rlock():
                in_target = self.lm_drive_data_dict[drive].status.get('in_target_pos', False)
                error     = self.lm_drive_data_dict[drive].status['error']
            if error:
                code = self.lm_drive_data_dict[drive].status['error_code']
                raise RuntimeError(f'Drive {drive} error while waiting for motion. '
                                   f'Code: 0x{code:02X}')
            if in_target:
                return

    # ══════════════════════════════════════════════════════════════════════════
    # Status / error helpers
    # ══════════════════════════════════════════════════════════════════════════

    def _print_status(self, label: str = ''):
        self.ProCommData.process_input_data()
        with self.lm_drive_lock.gen_rlock():
            lin = self.lm_drive_data_dict[LINEAR_DRIVE]
            rot = self.lm_drive_data_dict[ROTARY_DRIVE]
            lin_pos   = lin.status['actual_position']
            lin_homed = lin.status['homed']
            lin_err   = lin.status['error']
            rot_pos   = rot.status['actual_position']
            rot_homed = rot.status['homed']
            rot_err   = rot.status['error']
        tag = f'[{label}] ' if label else ''
        print(f'  {tag}'
              f'Lin(D{LINEAR_DRIVE}): pos={lin_pos:7.3f} mm   '
              f'homed={lin_homed}  err={lin_err} | '
              f'Rot(D{ROTARY_DRIVE}): pos={rot_pos:9.3f} deg  '
              f'homed={rot_homed}  err={rot_err}')

    def _acknowledge_errors(self):
        self.ProCommData.process_input_data()
        for i in range(self.noDev):
            drive = i + 1
            with self.lm_drive_lock.gen_rlock():
                has_error = self.lm_drive_data_dict[drive].status['error']
            if has_error:
                print(f'  Drive {drive} has an error -- acknowledging...')
                self.HK.error_acknowledge(drive=drive)
                time.sleep(self.cycle_time * 10)
                self.ProCommData.process_input_data()
                with self.lm_drive_lock.gen_rlock():
                    still = self.lm_drive_data_dict[drive].status['error']
                    code  = self.lm_drive_data_dict[drive].status['error_code']
                if still:
                    logging.warning(f'  Drive {drive} still in error. Code: 0x{code:02X}')
                else:
                    print(f'  Drive {drive} error cleared.')
        print()




    def write_cfg_with_retry(self, drive, header, UPID, value, retries=3):
        self._print_status('before write cfg')
        for attempt in range(1, retries + 1):
            try:
                self.cfg.write_cfg(
                    drive=drive,
                    header=header,
                    UPID=UPID,
                    value=value
                )
                return  # success

            except TimeoutError as e:
                logging.warning(
                    f"write_cfg failed attempt {attempt}/{retries}: {e}"
                )

                if attempt < retries:
                    time.sleep(5)  # wait before retry
                else:
                    raise  # give original TimeoutError

# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    mp.set_start_method('spawn', force=True)
    logging.basicConfig(format='%(levelname)s: %(message)s', level=logging.INFO)
    app = LinMot_Ypsomed()
    app.start()

if __name__ == '__main__':
    main()