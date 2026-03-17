"""
Routing Calculations Thread - computes and outputs the routing table.
Runs after initial delay and whenever notified of topology changes.
"""
import threading
import time


class RoutingThread(threading.Thread):
    def __init__(self, node):
        super().__init__()
        self.node = node
        self.recalculate_event = threading.Event()
        self.last_table = None

    def run(self):
        # Wait for initial routing delay
        time.sleep(self.node.routing_delay)
        self._compute_and_print()

        # Wait for recalculation triggers
        while True:
            self.recalculate_event.wait()
            self.recalculate_event.clear()
            self._compute_and_print()

    def trigger_recalculation(self):
        """Signal the routing thread to recalculate."""
        self.recalculate_event.set()

    def _compute_and_print(self):
        """Compute routing table and print only if changed."""
        with self.node.lock:
            if self.node.is_down:
                return
            table = self.node.graph.compute_routing_table()
        if table != self.last_table:
            self.last_table = table
            self._print_routing_table(table)

    def _print_routing_table(self, table):
        """Output the routing table in the required format."""
        lines = [f"I am Node {self.node.node_id}"]
        for dest, path, cost in table:
            cost_str = f"{cost}" if cost != int(cost) else f"{cost:.1f}"
            lines.append(f"Least cost path from {self.node.node_id} to {dest}: {path}, link cost: {cost_str}")
        output = "\n".join(lines)
        with self.node.print_lock:
            print(output, flush=True)
