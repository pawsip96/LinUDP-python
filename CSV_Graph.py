"""
CSV_Graph.py

Simple viewer for PseudoScope CSV logs produced by
LinUDP_PseudoScope.PseudoScopeRecorder.save_csv().

Only 'elapsed_s' and 'channel2' (torque, in Nm) are used. 'channel3'
and 'channel4' are ignored, as are 'actual_pos', 'demand_pos', and
'current'.
"""

import csv
import matplotlib.pyplot as plt


class CSV_Graph:
    """
    Loads a PseudoScope CSV log and displays channel2 (torque) vs.
    elapsed_s in a new plot window.

    Args:
        path (str): Path to the CSV file (e.g. "torque_log.csv").
    """

    def __init__(self, path):
        self.path = path
        self.elapsed_s = []
        self.torque = []
        self._load()

    def _load(self):
        with open(self.path, newline='') as f:
            reader = csv.DictReader(f)
            if 'elapsed_s' not in reader.fieldnames or 'channel2' not in reader.fieldnames:
                raise ValueError(
                    f"{self.path} is missing required columns 'elapsed_s' "
                    f"and/or 'channel2' (found: {reader.fieldnames})"
                )
            for row in reader:
                self.elapsed_s.append(float(row['elapsed_s']))
                self.torque.append(float(row['channel2']))

        if not self.elapsed_s:
            raise ValueError(f"No data rows found in {self.path}")

    def display(self):
        """
        Open a new window plotting torque (channel2) vs. elapsed time.

        Uses a step plot (zero-order hold, 'steps-post') rather than a
        straight-line connection between samples. Each PseudoScope
        sample is a discrete measurement that holds until the next
        sample arrives -- a plain line plot would draw a linear ramp
        between two samples, implying intermediate torque values that
        were never actually measured. Markers show exactly where real
        samples were taken.
        """
        fig, ax = plt.subplots()
        ax.step(self.elapsed_s, self.torque, where='post', linewidth=1)
        ax.set_xlabel("Elapsed time (s)")
        ax.set_ylabel("Torque (Nm)")
        ax.set_title(f"Torque vs. Time \u2014 {self.path}")
        ax.grid(True)
        fig.tight_layout()
        plt.show()


if __name__ == "__main__":
    graph = CSV_Graph("torque_log.csv")
    graph.display()