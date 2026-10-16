"""
gui_app.py

  python gui_app.py          -> PRODUCTION: real drives only, no simulation,
                                no graph button.
  python gui_app.py --dev    -> DEV MODE: adds a "Simulation mode" checkbox
                                (fake drives from sim_backend.py) and a
                                "Show graph" button.

In production mode sim_backend.py is never imported, so you can leave
that file out of the production install entirely.
"""
import argparse
import os
import queue
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import ttk, messagebox

# The graph is created on the process thread and only saved to a PNG, so use
# Matplotlib's non-GUI backend. This MUST happen before CSV_Graph (pyplot)
# is imported. It avoids "Starting a Matplotlib GUI outside of the main thread".
try:
    import matplotlib
    matplotlib.use("Agg")
except ImportError:
    pass

from process import AssemblyProcess, ROTARY_DRIVE, NEEDLE_TYPES


class ColorButton(tk.Label):
    """
    Coloured button built from a Label. Needed because on macOS the normal
    tk/ttk buttons ignore background colours. Supports .config(state=...).
    """
    DISABLED_BG = "#b5b5b5"

    def __init__(self, parent, text, command, bg, state="normal", width=12):
        super().__init__(parent, text=text, fg="white", width=width, pady=6,
                         font=("Helvetica", 12, "bold"))
        self._color = bg
        self._command = command
        self._state = state
        self.bind("<Button-1>", self._on_click)
        self._apply()

    def _apply(self):
        enabled = self._state == "normal"
        tk.Label.configure(self, bg=self._color if enabled else self.DISABLED_BG,
                           cursor="hand2" if enabled else "arrow")

    def _on_click(self, _event):
        if self._state == "normal":
            self._command()

    def configure(self, **kw):
        if "bg" in kw:
            self._color = kw.pop("bg")
            self._apply()
        if "state" in kw:
            self._state = kw.pop("state")
            self._apply()
        if kw:
            tk.Label.configure(self, **kw)

    config = configure


class App:
    NEEDLE_ON = "#1f6feb"     # selected needle button (blue)
    NEEDLE_OFF = "#8a8f98"    # not selected (grey)
    PAD = 16                  # outer margin of the window content

    def __init__(self, root, dev_mode=False):
        self.root = root
        self.dev_mode = dev_mode
        root.title("Assembly Process" + ("  [DEV MODE]" if dev_mode else ""))
        root.geometry("860x380")
        root.iconbitmap("ico/ypsn.ico")
        root.resizable(False, False)

        self.q = queue.Queue()          # worker thread -> GUI thread
        self.stop_event = threading.Event()
        self.running = False
        self.graph = None
        self.png_path = None
        self.assembly_torque = None
        self.disassembly_torque = None
        self.transfer_kind = None
        self.transfer_value = None
        self.pending_transfer = None
        self._excel_lock = threading.Lock()   # serialise Excel writes
        self._error_windows = set()           # currently open error popups
        self._pending_asks = []               # dose questions waiting for errors
        self.sim_var = tk.BooleanVar(value=True)
        self.graph_btn = None
        self.needle = "BD needle"
        self.needle_btns = {}

        # Torque values live in read-only entries (selectable / copyable).
        self.assembly_value_var = tk.StringVar(value="")
        self.removal_value_var = tk.StringVar(value="")
        self.excel_var = tk.StringVar(value="")
        self.status_var = tk.StringVar(value="Ready")

        P = self.PAD

        # ================= BOTTOM (packed first so it stays at the edge)
        ttk.Label(root, textvariable=self.status_var, relief="sunken",
                  anchor="w", padding=(6, 3)).pack(side="bottom", fill="x")

        bottom = ttk.Frame(root)
        bottom.pack(side="bottom", fill="x", padx=P, pady=(4, 10))
        # left bottom corner: Transfer to Excel
        self.transfer_btn = ColorButton(
            bottom, text="Transfer to Excel", width=16,
            command=self.transfer_to_excel, bg="#1769aa", state="disabled")
        self.transfer_btn.pack(side="left")
        # right bottom corner: Show graph (dev only)
        if dev_mode:
            self.graph_btn = ttk.Button(bottom, text="Show graph", width=12,
                                        command=self.show_graph,
                                        state="disabled")
            self.graph_btn.pack(side="right")

        ttk.Label(root, textvariable=self.excel_var, anchor="w",
                  wraplength=800).pack(side="bottom", fill="x", padx=P)

        # ================= TOP BAR
        top = ttk.Frame(root)
        top.pack(side="top", fill="x", padx=P, pady=(10, 0))
        if dev_mode:
            ttk.Checkbutton(top, text="Simulation mode (no hardware)",
                            variable=self.sim_var).pack(side="left")
        # right top corner: BD / CF needle buttons
        needle_row = ttk.Frame(top)
        needle_row.pack(side="right")
        for i, name in enumerate(NEEDLE_TYPES):
            btn = ColorButton(needle_row, text=name, bg=self.NEEDLE_OFF,
                              command=lambda n=name: self.select_needle(n))
            btn.grid(row=0, column=i, padx=(4, 0))
            self.needle_btns[name] = btn
        self.select_needle(self.needle)

        # ================= START / STOP
        buttons = ttk.Frame(root)
        buttons.pack(side="top", pady=(14, 6))
        self.start_btn = ColorButton(buttons, text="Start",
                                     command=self.start, bg="#2e9e47")  # green
        self.start_btn.grid(row=0, column=0, padx=5)
        self.stop_btn = ColorButton(buttons, text="Stop", command=self.stop,
                                    bg="#d63031", state="disabled")     # red
        self.stop_btn.grid(row=0, column=1, padx=5)

        self.bar = ttk.Progressbar(root, maximum=100)
        self.bar.pack(side="top", fill="x", padx=P, pady=10)

        # ================= TORQUE VALUES (left, fixed grid, copyable)
        torque = ttk.Frame(root)
        torque.pack(side="top", anchor="w", padx=P, pady=(6, 0))
        torque.columnconfigure(0, minsize=210)   # label column (fixed)
        torque.columnconfigure(1, minsize=150)   # value column (fixed)
        torque.columnconfigure(2, minsize=40)    # unit column  (fixed)
        label_font = ("Helvetica", 16, "bold")
        self._torque_row(torque, 0, "Assembly torque:",
                         self.assembly_value_var, label_font)
        self._torque_row(torque, 1, "Removal torque:",
                         self.removal_value_var, label_font)

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(50, self._poll)

    @staticmethod
    def _torque_row(parent, row, text, var, font):
        """One aligned row: fixed label | read-only (copyable) value | unit."""
        ttk.Label(parent, text=text, font=font, anchor="w",
                  width=18).grid(row=row, column=0, sticky="w", pady=6)
        entry = tk.Entry(parent, textvariable=var, state="readonly",
                         readonlybackground="white", fg="black", font=font,
                         width=9, justify="center", relief="solid", bd=1)
        entry.grid(row=row, column=1, sticky="w", padx=(0, 6), pady=6)
        ttk.Label(parent, text="Nm", font=font).grid(
            row=row, column=2, sticky="w", pady=6)
        return entry

    # ------------------------------------------------------------ buttons
    def start(self):
        self.running = True
        self.stop_event.clear()
        self.graph = None
        self.png_path = None
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        if self.graph_btn:
            self.graph_btn.config(state="disabled")
        self.bar["value"] = 0
        self.assembly_torque = None
        self.disassembly_torque = None
        self.assembly_value_var.set("")
        self.removal_value_var.set("")
        self.excel_var.set("")
        self.transfer_kind = None
        self.transfer_value = None
        self.pending_transfer = None
        self.transfer_btn.config(state="disabled")
        # simulation can only ever be requested in dev mode
        simulate = self.dev_mode and self.sim_var.get()
        threading.Thread(target=self._worker, args=(simulate, self.needle),
                         daemon=True).start()

    def select_needle(self, name):
        """Choose BD / CF needle (locked while a process is running)."""
        if self.running:
            return
        self.needle = name
        for n, btn in self.needle_btns.items():
            btn.configure(bg=self.NEEDLE_ON if n == name else self.NEEDLE_OFF)
        cfg = NEEDLE_TYPES[name]

    def stop(self):
        self.stop_event.set()
        self.stop_btn.config(state="disabled")
        # Release the worker if it is waiting for the assembly transfer button.
        if self.pending_transfer is not None:
            self.pending_transfer["transferred"] = False
            self.pending_transfer["event"].set()
            self.pending_transfer = None
        self.transfer_kind = None
        self.transfer_value = None
        self.transfer_btn.config(state="disabled")
        self.status_var.set("Stopping...")

    @staticmethod
    def _write_to_selected_excel_cell(value):
        """Write a numeric torque value to Excel's current single-cell selection."""
        try:
            import xlwings as xw
        except ImportError as exc:
            raise RuntimeError(
                "Excel transfer requires xlwings. Install it with "
                "'python -m pip install xlwings', keep Excel open, then retry."
            ) from exc

        try:
            if xw.apps.count == 0:
                raise RuntimeError("No running Excel instance was found.")
            try:
                app = xw.apps.active
            except Exception:
                app = xw.apps[0]
            selection = app.selection
        except Exception as exc:
            raise RuntimeError(
                "Could not access Excel. Open the workbook in desktop Excel, "
                "select one destination cell, and try again."
            ) from exc

        if not isinstance(selection, xw.Range):
            raise RuntimeError(
                "Excel does not currently have a worksheet cell selected. "
                "Select one cell and click Transfer to Excel again."
            )
        try:
            if selection.count != 1:
                raise RuntimeError(
                    "Select exactly one Excel cell before transferring the torque."
                )

            selection.value = round(float(value), 4)

            # Move the selection one cell down
            app.selection.offset(1, 0).select()
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(
                "Excel could not write to the selected cell. Check that the "
                "workbook and worksheet are editable, then retry."
            ) from exc

    def transfer_to_excel(self):
        """Hand the torque to a background thread and let the process continue."""
        if self.transfer_kind not in ("assembly", "disassembly"):
            return
        if self.transfer_value is None:
            return

        kind = self.transfer_kind
        value = self.transfer_value
        box = self.pending_transfer if kind == "assembly" else None

        # Clear state first so the GUI never waits on Excel.
        self.pending_transfer = None
        self.transfer_kind = None
        self.transfer_value = None
        self.transfer_btn.config(state="disabled")
        self.excel_var.set(f"Sending {kind} torque to Excel...")

        # Release the process right away, whether or not the write succeeds.
        if box is not None:
            box["transferred"] = True
            box["event"].set()

        threading.Thread(target=self._excel_worker, args=(kind, value),
                         daemon=True).start()

    def _excel_worker(self, kind, value):
        """Runs off the GUI thread; reports back through the queue."""
        with self._excel_lock:
            com_ready = False
            try:
                try:                              # needed for COM on Windows
                    import pythoncom
                    pythoncom.CoInitialize()
                    com_ready = True
                except ImportError:
                    pass                          # macOS / no pywin32
                self._write_to_selected_excel_cell(value)
                self.q.put(("excel_result", kind, value, None))
            except Exception as exc:
                self.q.put(("excel_result", kind, value, str(exc)))
            finally:
                if com_ready:
                    pythoncom.CoUninitialize()

    def _show_error_popup(self, title, message):
        """Non-modal error window with an OK button. It does not grab input,
        so the main window and the running process stay fully usable."""
        win = tk.Toplevel(self.root)
        win.title(title)
        win.transient(self.root)
        win.resizable(False, False)
        ttk.Label(win, text=message, wraplength=380, justify="left",
                  padding=(16, 16, 16, 8)).pack()
        ttk.Button(win, text="OK", width=10,
                   command=win.destroy).pack(pady=(0, 14))
        self._error_windows.add(win)
        win.bind("<Destroy>",
                 lambda e, w=win: self._error_windows.discard(w)
                 if e.widget is w else None)
        win.bind("<Return>", lambda _e: win.destroy())
        win.bind("<Escape>", lambda _e: win.destroy())
        win.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - win.winfo_width()) // 2
        y = self.root.winfo_rooty() + 40
        win.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        win.lift()

    def show_graph(self):
        """Dev mode: open the saved PNG in the system image viewer."""
        if self.png_path and os.path.exists(self.png_path):
            webbrowser.open(Path(self.png_path).resolve().as_uri())
        elif self.graph:
            self.graph.display()

    def on_close(self):
        if self.running:
            messagebox.showwarning("Running", "Press Stop and wait for the "
                                   "process to finish before closing.")
            return
        self.root.destroy()

    # ------------------------------------------------------ worker thread
    def _ask_yes_no(self, question):
        """Called from the worker thread; the popup is shown by the GUI thread."""
        box = {"event": threading.Event(), "answer": False}
        self.q.put(("ask", question, box))
        box["event"].wait()
        return box["answer"]

    def _await_assembly_transfer(self, torque):
        """Pause the worker until the assembly torque is transferred or stopped."""
        box = {"event": threading.Event(), "transferred": False}
        self.q.put(("transfer_ready", "assembly", torque, box))
        while not box["event"].wait(0.1):
            if self.stop_event.is_set():
                return False
        return box["transferred"]

    def _worker(self, simulate, needle):
        try:
            if simulate:
                import sim_backend as sb          # dev mode only
                cmds = sb.SimCommands(speed=1.0)  # raise speed to run faster
                scope = sb.SimScope(cmds)
                graph_cls = sb.SimGraph
                waits = dict(stop_wait=0.2)
            else:
                import LinUDP_Commands as cmds
                from LinUDP_PseudoScope import PseudoScopeRecorder
                from CSV_Graph import CSV_Graph
                scope = PseudoScopeRecorder(
                    ROTARY_DRIVE,
                    channels={2: 1.0, 3: 1.0, 4: 1.0},
                    samples_per_burst=200,
                )
                graph_cls = CSV_Graph
                waits = {}

            proc = AssemblyProcess(
                cmds, scope, graph_cls,
                status=lambda t: self.q.put(("status", t)),
                progress=lambda f: self.q.put(("progress", f)),
                torque_cb=lambda v: self.q.put(("torque", v)),
                error_cb=lambda m: self.q.put(("error", m)),
                wait_for_assembly_transfer=self._await_assembly_transfer,
                disassembly_torque_ready_cb=lambda v: self.q.put(
                    ("transfer_ready", "disassembly", v, None)),
                ask_yes_no=self._ask_yes_no,
                stop_event=self.stop_event,
                needle=needle,
                **waits,
            )
            proc.run()
            self.q.put(("done", (proc.graph, proc.png_path)))
        except Exception as e:
            self.q.put(("status", "Stopped due to an error."))
            self.q.put(("error", f"{type(e).__name__}: {e}"))
            self.q.put(("done", (None, None)))

    # --------------------------------------------------------- GUI thread
    def _poll(self):
        try:
            while True:
                kind, *data = self.q.get_nowait()
                if kind == "status":
                    self.status_var.set(data[0])
                elif kind == "progress":
                    self.bar["value"] = data[0] * 100
                elif kind == "torque":
                    v = data[0]
                    self.disassembly_torque = v
                    self.removal_value_var.set("" if v is None else f"{v:.4f}")
                elif kind == "transfer_ready":
                    torque_kind, value, box = data
                    if torque_kind == "assembly":
                        self.assembly_torque = value
                        self.assembly_value_var.set(f"{value:.4f}")
                        # A Stop request may arrive just before this queue item.
                        if self.stop_event.is_set():
                            if box is not None:
                                box["transferred"] = False
                                box["event"].set()
                            continue
                        self.pending_transfer = box
                    else:
                        self.disassembly_torque = value
                    self.transfer_kind = torque_kind
                    self.transfer_value = value
                    self.transfer_btn.config(state="normal")
                    self.status_var.set(
                        f"{torque_kind.capitalize()} torque ready — transfer to Excel.")
                elif kind == "excel_result":
                    torque_kind, value, err = data
                    if err is None:
                        self.excel_var.set(
                            f"{torque_kind.capitalize()} torque {value:.4f} Nm "
                            f"transferred to Excel.")
                    else:
                        self.excel_var.set("")
                        self._show_error_popup(
                            "Excel transfer failed",
                            f"The {torque_kind} torque ({value:.4f} Nm) "
                            f"could not be transferred.\n\n{err}")
                        # Re-arm for a manual retry, unless a newer torque
                        # is already waiting.
                        if self.transfer_kind is None and not self.stop_event.is_set():
                            self.transfer_kind = torque_kind
                            self.transfer_value = value
                            self.transfer_btn.config(state="normal")
                elif kind == "error":
                    self._show_error_popup("Error", data[0])
                elif kind == "ask":
                    # Shown after the loop, and only once no error popup is open.
                    self._pending_asks.append((data[0], data[1]))
                elif kind == "done":
                    self.running = False
                    self.graph, self.png_path = data[0]
                    self.start_btn.config(state="normal")
                    self.stop_btn.config(state="disabled")
                    if self.graph_btn and (self.graph or self.png_path):   # dev only
                        self.graph_btn.config(state="normal")
        except queue.Empty:
            pass

        # Error popups always come first: the dose question waits until the
        # user has confirmed (closed) every open error window.
        if self._pending_asks and not self._error_windows:
            question, box = self._pending_asks.pop(0)
            box["answer"] = messagebox.askyesno(
                "Dose check", question, parent=self.root)
            box["event"].set()

        self.root.after(50, self._poll)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dev", action="store_true",
                        help="dev mode: enables simulation checkbox and graph button")
    args = parser.parse_args()

    root = tk.Tk()
    App(root, dev_mode=args.dev)
    root.mainloop()