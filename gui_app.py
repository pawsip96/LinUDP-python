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
import queue
import threading
import tkinter as tk
from tkinter import ttk, messagebox

from process import AssemblyProcess, ROTARY_DRIVE


class App:
    def __init__(self, root, dev_mode=False):
        self.root = root
        self.dev_mode = dev_mode
        root.title("Assembly Process" + ("  [DEV MODE]" if dev_mode else ""))
        root.geometry("480x300")
        root.resizable(False, False)

        self.q = queue.Queue()          # worker thread -> GUI thread
        self.stop_event = threading.Event()
        self.running = False
        self.graph = None

        # ---- dev-only widgets
        self.sim_var = tk.BooleanVar(value=True)
        self.graph_btn = None
        if dev_mode:
            ttk.Checkbutton(root, text="Simulation mode (no hardware)",
                            variable=self.sim_var).pack(pady=(12, 4))
        else:
            ttk.Label(root, text="").pack(pady=(12, 4))   # keep layout equal

        buttons = ttk.Frame(root)
        buttons.pack(pady=8)
        self.start_btn = ttk.Button(buttons, text="Start", width=12,
                                    command=self.start)
        self.start_btn.grid(row=0, column=0, padx=5)
        self.stop_btn = ttk.Button(buttons, text="Stop", width=12,
                                   command=self.stop, state="disabled")
        self.stop_btn.grid(row=0, column=1, padx=5)
        if dev_mode:
            self.graph_btn = ttk.Button(buttons, text="Show graph", width=12,
                                        command=self.show_graph,
                                        state="disabled")
            self.graph_btn.grid(row=0, column=2, padx=5)

        self.bar = ttk.Progressbar(root, maximum=100, length=420)
        self.bar.pack(pady=10)

        # disassembly torque (most negative torque seen during disassembly)
        self.torque_var = tk.StringVar(value="Disassembly torque:  ---")
        ttk.Label(root, textvariable=self.torque_var,
                  font=("Helvetica", 16, "bold")).pack(pady=8)

        # status bar at the bottom
        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(root, textvariable=self.status_var, relief="sunken",
                  anchor="w", padding=(6, 3)).pack(side="bottom", fill="x")

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(50, self._poll)

    # ------------------------------------------------------------ buttons
    def start(self):
        self.running = True
        self.stop_event.clear()
        self.graph = None
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        if self.graph_btn:
            self.graph_btn.config(state="disabled")
        self.bar["value"] = 0
        self.torque_var.set("Disassembly torque:  ---")
        # simulation can only ever be requested in dev mode
        simulate = self.dev_mode and self.sim_var.get()
        threading.Thread(target=self._worker, args=(simulate,),
                         daemon=True).start()

    def stop(self):
        self.stop_event.set()
        self.stop_btn.config(state="disabled")
        self.status_var.set("Stopping...")

    def show_graph(self):
        if self.graph:
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

    def _worker(self, simulate):
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
                ask_yes_no=self._ask_yes_no,
                stop_event=self.stop_event,
                **waits,
            )
            proc.run()
            self.q.put(("done", proc.graph))
        except Exception as e:
            self.q.put(("status", f"Error: {e}"))
            self.q.put(("done", None))

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
                    self.torque_var.set("Disassembly torque:  ---" if v is None
                                        else f"Disassembly torque:  {v:.4f} Nm")
                elif kind == "ask":
                    question, box = data
                    box["answer"] = messagebox.askyesno(
                        "Dose check", question, parent=self.root)
                    box["event"].set()
                elif kind == "done":
                    self.running = False
                    self.graph = data[0]
                    self.start_btn.config(state="normal")
                    self.stop_btn.config(state="disabled")
                    if self.graph_btn and self.graph:   # dev mode only
                        self.graph_btn.config(state="normal")
        except queue.Empty:
            pass
        self.root.after(50, self._poll)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dev", action="store_true",
                        help="dev mode: enables simulation checkbox and graph button")
    args = parser.parse_args()

    root = tk.Tk()
    App(root, dev_mode=args.dev)
    root.mainloop()