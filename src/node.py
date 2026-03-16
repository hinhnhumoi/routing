"""
Core Node class - manages state, threads, and coordination.
"""
import threading
import time
import sys

from threads.listening_thread import ListeningThread
from threads.sending_thread import SendingThread
from threads.routing_thread import RoutingThread
from graph import Graph


class Node:
    def __init__(self, node_id, port, config_file, routing_delay, update_interval, neighbours, original_config):
        self.node_id = node_id
        self.port = port
        self.config_file = config_file
        self.routing_delay = routing_delay
        self.update_interval = update_interval
        self.neighbours = neighbours          # {neighbour_id: {'cost': float, 'port': int}}
        self.original_config = original_config
        self.is_down = False
        self.lock = threading.RLock()         # Protects shared state (reentrant for LSA flooding)
        self.print_lock = threading.Lock()    # Prevents interleaved output across threads

        # Graph holds the full topology learned over time
        self.graph = Graph(node_id, neighbours)

        # LSA database: {origin_id: {'seq': int, 'neighbours': {nb_id: cost}}}
        # Initialize with own LSA
        self.lsa_seq = 0
        self.lsa_db = {}
        self._update_own_lsa()

        # After SPLIT, only accept LSAs from nodes in my partition (None = no split active)
        self.my_partition = None
        # Nodes that have been merged (absorbed) — ignore in LSA processing
        self.merged_nodes = set()

        # Track last UPDATE received from each neighbour for timeout detection
        # Initialize with current time so we don't immediately mark them as failed
        now = time.time()
        self.last_heard = {nb_id: now for nb_id in neighbours}
        self.timeout_multiplier = 3  # Consider node DOWN after 3x UpdateInterval silence

    def start(self):
        """Start all threads."""
        self.listening_thread = ListeningThread(self)
        self.sending_thread = SendingThread(self)
        self.routing_thread = RoutingThread(self)

        self.listening_thread.daemon = True
        self.sending_thread.daemon = True
        self.routing_thread.daemon = True

        # Start timeout checker
        self.timeout_thread = threading.Thread(target=self._check_timeouts, daemon=True)

        self.listening_thread.start()
        self.sending_thread.start()
        self.routing_thread.start()
        self.timeout_thread.start()

        # Keep main thread alive
        try:
            while True:
                self.listening_thread.join(timeout=1)
        except KeyboardInterrupt:
            sys.exit(0)

    def _update_own_lsa(self):
        """Update this node's own LSA entry in the database."""
        self.lsa_seq += 1
        self.lsa_db[self.node_id] = {
            'seq': self.lsa_seq,
            'neighbours': {nb_id: info['cost'] for nb_id, info in self.neighbours.items()}
        }

    def rebuild_graph_from_lsa(self):
        """Rebuild the full graph adjacency from LSA database."""
        new_adj = {}
        for origin, lsa in self.lsa_db.items():
            if origin in self.merged_nodes:
                continue
            if origin not in new_adj:
                new_adj[origin] = {}
            for nb_id, cost in lsa['neighbours'].items():
                if nb_id in self.merged_nodes:
                    continue
                # After SPLIT, skip edges to nodes outside my partition
                if self.my_partition is not None and nb_id not in self.my_partition:
                    continue
                new_adj[origin][nb_id] = cost
                if nb_id not in new_adj:
                    new_adj[nb_id] = {}
                new_adj[nb_id][origin] = cost
        self.graph.adjacency = new_adj

    def record_heartbeat(self, node_id):
        """Record that we received an UPDATE from node_id."""
        self.last_heard[node_id] = time.time()
        # If node was previously marked as failed by timeout, recover it
        if node_id in self.graph.failed_nodes:
            self.graph.recover_node(node_id)
            self.routing_thread.trigger_recalculation()

    def _check_timeouts(self):
        """Periodically check if neighbours have gone silent."""
        timeout = self.update_interval * self.timeout_multiplier
        while True:
            time.sleep(self.update_interval)
            with self.lock:
                if self.is_down:
                    continue
                now = time.time()
                changed = False
                for nb_id in self.neighbours:
                    if nb_id in self.last_heard:
                        if now - self.last_heard[nb_id] > timeout:
                            if nb_id not in self.graph.failed_nodes:
                                self.graph.remove_node(nb_id)
                                changed = True
                if changed and hasattr(self, 'routing_thread'):
                    self.routing_thread.trigger_recalculation()
