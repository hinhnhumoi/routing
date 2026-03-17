"""
Handles all dynamic commands: CHANGE, FAIL, RECOVER, QUERY, QUERY PATH,
RESET, BATCH UPDATE, MERGE, SPLIT, CYCLE DETECT.
"""
import sys
import os

from config_parser import parse_config
from graph import Graph


class CommandHandler:
    def __init__(self, node):
        self.node = node

    def _print(self, *args, **kwargs):
        """Thread-safe print to prevent interleaved output."""
        with self.node.print_lock:
            print(*args, **kwargs, flush=True)

    def handle_lsa(self, message, from_port=None):
        """Process an LSA packet: LSA <origin> <seq> <nb1>:<cost>,<nb2>:<cost>,..."""
        parts = message.split()
        if len(parts) < 3:
            return

        origin = parts[1]
        try:
            seq = int(parts[2])
        except ValueError:
            return

        # Parse neighbour data
        neighbours = {}
        if len(parts) >= 4 and parts[3]:
            for entry in parts[3].split(','):
                tokens = entry.split(':')
                if len(tokens) == 2:
                    try:
                        neighbours[tokens[0]] = float(tokens[1])
                    except ValueError:
                        continue

        # After SPLIT, ignore LSAs from nodes outside my partition
        if self.node.my_partition is not None and origin not in self.node.my_partition:
            return
        # Ignore LSAs from merged (absorbed) nodes
        if origin in self.node.merged_nodes:
            return

        # Record heartbeat if origin is a direct neighbour
        if origin in self.node.neighbours:
            self.node.record_heartbeat(origin)

        # Check if this LSA is newer than what we have
        if origin in self.node.lsa_db:
            if seq <= self.node.lsa_db[origin]['seq']:
                return  # Already have this or newer, ignore

        # Store new LSA
        self.node.lsa_db[origin] = {'seq': seq, 'neighbours': neighbours}

        # Rebuild graph from all LSAs
        self.node.rebuild_graph_from_lsa()

        # Trigger routing recalculation
        if hasattr(self.node, 'routing_thread'):
            self.node.routing_thread.trigger_recalculation()

        # Forward this LSA to all neighbours except sender (flooding)
        if hasattr(self.node, 'sending_thread'):
            self.node.sending_thread.flood_lsa(origin, self.node.lsa_db[origin], exclude_port=from_port)

    def handle_update(self, message):
        """Process an UPDATE packet received from another node."""
        parts = message.split()
        if len(parts) < 3 or parts[0] != "UPDATE":
            self._print("Error: Invalid update packet format.")
            sys.exit(1)

        source = parts[1]
        neighbour_data = parts[2]

        # Parse neighbour entries: N1:Cost1:Port1,N2:Cost2:Port2,...
        entries = neighbour_data.split(',')
        for entry in entries:
            tokens = entry.split(':')
            if len(tokens) != 3:
                self._print("Error: Invalid update packet format.")
                sys.exit(1)
            nb_id = tokens[0]
            try:
                cost = float(tokens[1])
                port = int(tokens[2])
            except ValueError:
                self._print("Error: Invalid update packet format.")
                sys.exit(1)

            # Update graph with learned edges
            self.node.graph.update_edge(source, nb_id, cost)
            self.node.graph.port_map[nb_id] = port

        # Trigger routing recalculation
        if hasattr(self.node, 'routing_thread'):
            self.node.routing_thread.trigger_recalculation()

    def handle_command(self, command):
        """Dispatch a dynamic command."""
        tokens = command.split()
        if not tokens:
            return
        # Uppercase the command keyword(s), keep arguments as-is
        tokens[0] = tokens[0].upper()
        if len(tokens) > 1 and tokens[0] in ("QUERY", "BATCH", "CYCLE"):
            tokens[1] = tokens[1].upper()

        cmd = tokens[0]

        if cmd == "CHANGE":
            self._handle_change(tokens)
        elif cmd == "FAIL":
            self._handle_fail(tokens)
        elif cmd == "RECOVER":
            self._handle_recover(tokens)
        elif cmd == "QUERY":
            if len(tokens) >= 2 and tokens[1] == "PATH":
                self._handle_query_path(tokens)
            else:
                self._handle_query(tokens)
        elif cmd == "RESET":
            self._handle_reset(tokens)
        elif cmd == "BATCH":
            if len(tokens) >= 2 and tokens[1] == "UPDATE":
                self._handle_batch_update(tokens)
            else:
                self._print("Error: Invalid command format.")
                sys.exit(1)
        elif cmd == "CYCLE":
            if len(tokens) >= 2 and tokens[1] == "DETECT":
                self._handle_cycle_detect(tokens)
            else:
                self._print("Error: Invalid command format.")
                sys.exit(1)
        elif cmd == "MERGE":
            self._handle_merge(tokens)
        elif cmd == "SPLIT":
            self._handle_split(tokens)
        elif cmd == "SHOW":
            self.node.graph.show_topology()
            return
        else:
            self._print("Error: Invalid command format.")
            sys.exit(1)

    def _is_valid_node_id(self, node_id):
        """Check if node_id is a single uppercase letter."""
        return len(node_id) == 1 and node_id.isalpha() and node_id.isupper()

    def _handle_change(self, tokens):
        """CHANGE <NeighbourID> <NewCost>"""
        if len(tokens) != 3:
            if len(tokens) > 3:
                self._print("Error: Invalid command format. Expected exactly two tokens after CHANGE.")
            else:
                self._print("Error: Invalid command format. Expected numeric cost value.")
            sys.exit(1)

        neighbour_id = tokens[1]
        try:
            new_cost = float(tokens[2])
        except ValueError:
            self._print("Error: Invalid command format. Expected numeric cost value.")
            sys.exit(1)

        # Update local neighbour cost
        if neighbour_id in self.node.neighbours:
            self.node.neighbours[neighbour_id]['cost'] = new_cost
        self.node.graph.update_edge(self.node.node_id, neighbour_id, new_cost)
        self.node._update_own_lsa()

        # Immediately broadcast updated configuration
        if hasattr(self.node, 'sending_thread'):
            self.node.sending_thread.immediate_broadcast()

        # Trigger recalculation
        if hasattr(self.node, 'routing_thread'):
            self.node.routing_thread.trigger_recalculation()

    def _handle_fail(self, tokens):
        """FAIL <Node-ID>"""
        if len(tokens) != 2:
            self._print("Error: Invalid command format. Expected: FAIL <Node-ID>.")
            sys.exit(1)

        node_id = tokens[1]
        if not self._is_valid_node_id(node_id):
            self._print("Error: Invalid command format. Expected a valid Node-ID.")
            sys.exit(1)

        if node_id == self.node.node_id:
            self.node.is_down = True
            self._print(f"Node {node_id} is now DOWN.")
        else:
            self.node.graph.remove_node(node_id)
            if hasattr(self.node, 'routing_thread'):
                self.node.routing_thread.trigger_recalculation()

    def _handle_recover(self, tokens):
        """RECOVER <Node-ID>"""
        if len(tokens) != 2:
            self._print("Error: Invalid command format. Expected: RECOVER <Node-ID>.")
            sys.exit(1)

        node_id = tokens[1]
        if not self._is_valid_node_id(node_id):
            self._print("Error: Invalid command format. Expected a valid Node-ID.")
            sys.exit(1)

        if node_id == self.node.node_id:
            self.node.is_down = False
            self._print(f"Node {node_id} is now UP.")
        else:
            self.node.graph.recover_node(node_id)
            if hasattr(self.node, 'routing_thread'):
                self.node.routing_thread.trigger_recalculation()

    def _handle_query(self, tokens):
        """QUERY <Destination>"""
        if len(tokens) != 2:
            self._print("Error: Invalid command format. Expected a valid Destination.")
            sys.exit(1)

        dest = tokens[1]
        if not self._is_valid_node_id(dest):
            self._print("Error: Invalid command format. Expected a valid Destination.")
            sys.exit(1)

        table = self.node.graph.compute_routing_table(self.node.node_id)
        for d, path, cost in table:
            if d == dest:
                cost_str = f"{cost}" if cost != int(cost) else f"{cost:.1f}"
                self._print(f"Least cost path from {self.node.node_id} to {dest}: {path}, link cost: {cost_str}")
                return
        self._print(f"Least cost path from {self.node.node_id} to {dest}: not reachable")

    def _handle_query_path(self, tokens):
        """QUERY PATH <Source> <Destination>"""
        if len(tokens) != 4:
            self._print("Error: Invalid command format. Expected two valid identifiers for Source and Destination.")
            sys.exit(1)

        source = tokens[2]
        dest = tokens[3]
        if not self._is_valid_node_id(source) or not self._is_valid_node_id(dest):
            self._print("Error: Invalid command format. Expected two valid identifiers for Source and Destination.")
            sys.exit(1)

        table = self.node.graph.compute_routing_table(source)
        for d, path, cost in table:
            if d == dest:
                cost_str = f"{cost}" if cost != int(cost) else f"{cost:.1f}"
                self._print(f"Least cost path from {source} to {dest}: {path}, link cost: {cost_str}")
                return
        self._print(f"Least cost path from {source} to {dest}: not reachable")

    def _handle_reset(self, tokens):
        """RESET"""
        if len(tokens) != 1:
            self._print("Error: Invalid command format. Expected exactly: RESET.")
            sys.exit(1)

        # Reload original config
        neighbours, _ = parse_config(self.node.config_file)
        self.node.neighbours = neighbours
        self.node.graph = Graph(self.node.node_id, neighbours)
        self.node.is_down = False
        self.node.my_partition = None
        self.node.merged_nodes = set()
        self.node.lsa_db = {}
        self.node._update_own_lsa()

        # Immediately broadcast new update packet
        if hasattr(self.node, 'sending_thread'):
            self.node.sending_thread.immediate_broadcast()

        self._print(f"Node {self.node.node_id} has been reset.")

        # Trigger recalculation
        if hasattr(self.node, 'routing_thread'):
            self.node.routing_thread.trigger_recalculation()

    def _handle_batch_update(self, tokens):
        """BATCH UPDATE <Filename>"""
        if len(tokens) != 3:
            self._print("Error: Invalid command format. Expected: BATCH UPDATE <Filename>.")
            sys.exit(1)

        filename = tokens[2]
        if not os.path.exists(filename):
            self._print(f"Error: File {filename} not found.")
            sys.exit(1)

        with open(filename, 'r') as f:
            commands = [line.strip() for line in f.readlines() if line.strip()]

        for cmd in commands:
            self.handle_command(cmd)

        self._print("Batch update complete.")

        # Trigger recalculation
        if hasattr(self.node, 'routing_thread'):
            self.node.routing_thread.trigger_recalculation()

    def _handle_merge(self, tokens):
        """MERGE <Node-ID1> <Node-ID2> (Bonus)"""
        if len(tokens) != 3:
            self._print("Error: Invalid command format. Expected two valid identifiers for MERGE.")
            sys.exit(1)

        node1 = tokens[1]
        node2 = tokens[2]
        if not self._is_valid_node_id(node1) or not self._is_valid_node_id(node2):
            self._print("Error: Invalid command format. Expected two valid identifiers for MERGE.")
            sys.exit(1)

        graph = self.node.graph
        # Transfer all edges from node2 to node1
        if node2 in graph.adjacency:
            for nb, cost in list(graph.adjacency[node2].items()):
                if nb == node1:
                    continue
                # Use lower cost if edge already exists
                if nb in graph.adjacency.get(node1, {}):
                    existing_cost = graph.adjacency[node1][nb]
                    cost = min(existing_cost, cost)
                graph.update_edge(node1, nb, cost)
                # Remove edge from nb to node2
                if nb in graph.adjacency and node2 in graph.adjacency[nb]:
                    del graph.adjacency[nb][node2]
            del graph.adjacency[node2]

        # Remove node2 from node1's adjacency if present
        if node1 in graph.adjacency and node2 in graph.adjacency[node1]:
            del graph.adjacency[node1][node2]

        # Update local neighbours if needed
        # If this node is node1, absorb node2's neighbours into our own
        if self.node.node_id == node1:
            # Add node2's former neighbours (from graph) as our direct neighbours
            if node1 in graph.adjacency:
                for nb, cost in graph.adjacency[node1].items():
                    if nb == node2:
                        continue
                    if nb not in self.node.neighbours:
                        # Determine port from port_map or existing neighbour info
                        port = graph.port_map.get(nb, 6000)
                        self.node.neighbours[nb] = {'cost': cost, 'port': port}
                    else:
                        # Keep lower cost
                        if cost < self.node.neighbours[nb]['cost']:
                            self.node.neighbours[nb]['cost'] = cost
        if node2 in self.node.neighbours:
            del self.node.neighbours[node2]

        # Mark node2 as merged so future LSAs referencing it are ignored
        self.node.merged_nodes.add(node2)

        # Update LSA database: transfer node2's LSA neighbours to node1, remove node2
        if node2 in self.node.lsa_db:
            del self.node.lsa_db[node2]
        # Update all LSAs: replace references to node2 with node1
        for origin in list(self.node.lsa_db.keys()):
            lsa = self.node.lsa_db[origin]
            if node2 in lsa['neighbours']:
                cost2 = lsa['neighbours'].pop(node2)
                if origin != node1:
                    # Add edge to node1 with lower cost if exists
                    if node1 in lsa['neighbours']:
                        lsa['neighbours'][node1] = min(lsa['neighbours'][node1], cost2)
                    else:
                        lsa['neighbours'][node1] = cost2
        self.node._update_own_lsa()

        self._print("Graph merged successfully.")
        if hasattr(self.node, 'routing_thread'):
            self.node.routing_thread.trigger_recalculation()

    def _handle_split(self, tokens):
        """SPLIT (Bonus)"""
        if len(tokens) != 1:
            self._print("Error: Invalid command format. Expected exactly: SPLIT.")
            sys.exit(1)

        graph = self.node.graph
        all_nodes = sorted(graph.get_all_nodes() - graph.failed_nodes)
        k = len(all_nodes) // 2
        v1 = set(all_nodes[:k])
        v2 = set(all_nodes[k:])

        # Remove all edges between V1 and V2
        for node in v1:
            if node in graph.adjacency:
                for nb in list(graph.adjacency[node].keys()):
                    if nb in v2:
                        del graph.adjacency[node][nb]
                        if node in graph.adjacency.get(nb, {}):
                            del graph.adjacency[nb][node]

        # Update local neighbours
        my_partition = v1 if self.node.node_id in v1 else v2
        for nb_id in list(self.node.neighbours.keys()):
            if nb_id not in my_partition:
                del self.node.neighbours[nb_id]

        # Update LSA database: remove cross-partition edges and LSAs from other partition
        for origin in list(self.node.lsa_db.keys()):
            if origin not in my_partition:
                del self.node.lsa_db[origin]
            else:
                lsa = self.node.lsa_db[origin]
                lsa['neighbours'] = {
                    nb: cost for nb, cost in lsa['neighbours'].items()
                    if nb in my_partition
                }
        self.node.my_partition = my_partition
        self.node._update_own_lsa()

        self._print("Graph partitioned successfully.")
        if hasattr(self.node, 'routing_thread'):
            self.node.routing_thread.trigger_recalculation()

    def _handle_cycle_detect(self, tokens):
        """CYCLE DETECT (Bonus)"""
        if len(tokens) != 2:
            self._print("Error: Invalid command format. Expected exactly: CYCLE DETECT.")
            sys.exit(1)

        if self.node.graph.detect_cycle():
            self._print("Cycle detected.")
        else:
            self._print("No cycle found.")
