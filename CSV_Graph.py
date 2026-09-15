"""
CSV_Graph.py

Simple viewer for PseudoScope CSV logs produced by
LinUDP_PseudoScope.PseudoScopeRecorder.save_csv().

Uses 'elapsed_s', 'channel2' (torque, in Nm), 'actual_pos', and
'demand_pos' (both in degrees). 'channel3' and 'channel4' are
ignored, as is 'current'.
"""

import csv
import os
import matplotlib.pyplot as plt


class CSV_Graph:
    """
    Loads a PseudoScope CSV log and displays channel2 (torque) vs.
    elapsed_s on the left y-axis, with actual_pos and demand_pos
    (in degrees) on a second y-axis on the right, in a new plot
    window.

    Args:
        path (str): Path to the CSV file (e.g. "torque_log.csv").
    """

    def __init__(self, path):
        self.path = path
        self.elapsed_s = []
        self.torque = []
        self.actual_position = []
        self.demand_position = []
        self.fig = None
        self._load()

    def _load(self):
        with open(self.path, newline='') as f:
            reader = csv.DictReader(f)
            required = {'elapsed_s', 'channel2', 'actual_pos', 'demand_pos'}
            missing = required - set(reader.fieldnames or [])
            if missing:
                raise ValueError(
                    f"{self.path} is missing required columns {sorted(missing)} "
                    f"(found: {reader.fieldnames})"
                )
            for row in reader:
                self.elapsed_s.append(float(row['elapsed_s']))
                self.torque.append(float(row['channel2']))
                self.actual_position.append(float(row['actual_pos']))
                self.demand_position.append(float(row['demand_pos']))

        if not self.elapsed_s:
            raise ValueError(f"No data rows found in {self.path}")

    def _build_figure(self):
        """
        Build (but do not show) the torque/position figure, caching it
        on self.fig so display() and save_png() can share the same
        figure instead of plotting the data twice.

        All three traces use a step plot (zero-order hold,
        'steps-post') rather than a straight-line connection between
        samples. Each PseudoScope sample is a discrete measurement
        that holds until the next sample arrives -- a plain line plot
        would draw a linear ramp between two samples, implying
        intermediate values that were never actually measured.
        """
        if self.fig is not None:
            return self.fig

        fig, ax_torque = plt.subplots()

        # Left axis: torque
        line_torque, = ax_torque.step(
            self.elapsed_s, self.torque, where='post',
            linewidth=1, color='tab:blue', label='Torque (Nm)'
        )
        ax_torque.set_xlabel("Elapsed time (s)")
        ax_torque.set_ylabel("Torque (Nm)", color='tab:blue')
        ax_torque.tick_params(axis='y', labelcolor='tab:blue')
        ax_torque.grid(True)

        # Right axis: position, sharing the same x-axis
        ax_pos = ax_torque.twinx()
        line_actual, = ax_pos.step(
            self.elapsed_s, self.actual_position, where='post',
            linewidth=1, color='tab:red', label='Actual position (deg)'
        )
        line_demand, = ax_pos.step(
            self.elapsed_s, self.demand_position, where='post',
            linewidth=1, color='tab:green', linestyle='--',
            label='Demand position (deg)'
        )
        ax_pos.set_ylabel("Position (deg)", color='black')
        ax_pos.tick_params(axis='y', labelcolor='black')

        # Combine legends from both axes into one
        lines = [line_torque, line_actual, line_demand]
        ax_torque.legend(lines, [l.get_label() for l in lines], loc='best')

        ax_torque.set_title(f"Torque & Position vs. Time \u2014 {self.path}")
        fig.tight_layout()

        self.fig = fig
        return fig

    def display(self):
        """
        Open a new window plotting torque (channel2) vs. elapsed time
        on the left y-axis, and actual/demand position (degrees) vs.
        elapsed time on a second y-axis on the right.
        """
        self._build_figure()
        plt.show()

    def save_png(self, path=None, dpi=300):
        """
        Save the torque/position plot as a high-resolution PNG image.

        Args:
            path (str, optional): Output PNG path. Defaults to the
                source CSV path with its extension replaced by
                '.png' (e.g. "reports/torque_log_20260914_103448.png").
            dpi (int, optional): Resolution in dots per inch. Defaults
                to 300, which is print-quality resolution.

        Returns:
            str: The path the PNG was saved to.
        """
        fig = self._build_figure()
        if path is None:
            path = os.path.splitext(self.path)[0] + ".png"
        fig.savefig(path, dpi=dpi, bbox_inches='tight')
        return path


if __name__ == "__main__":
    graph = CSV_Graph("reports/torque_log_20260914_103448.csv")
    graph.save_png()
    graph.display()
