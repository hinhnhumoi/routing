"""
Graph representation and shortest-path computation.
Maintains the network topology and computes least-cost paths using Dijkstra's algorithm.
"""
import heapq


class Graph:
    def __init__(self, node_id, neighbours):
        self.node_id = node_id
        # adjacency: {node: {neighbour: cost}}
        self.adjacency = {}
        # port_map: {node_id: port}
        self.port_map = {}

        # Initialize with local neighbours
        self.adjacency[node_id] = {}
        for nb_id, info in neighbours.items():
            self.adjacency[node_id][nb_id] = info['cost']
            self.port_map[nb_id] = info['port']
            # Add reverse edge
            if nb_id not in self.adjacency:
                self.adjacency[nb_id] = {}
            self.adjacency[nb_id][node_id] = info['cost']

        self.failed_nodes = set()

    def update_edge(self, u, v, cost):
        """Update or add an edge between u and v."""
        if u not in self.adjacency:
            self.adjacency[u] = {}
        if v not in self.adjacency:
            self.adjacency[v] = {}
        self.adjacency[u][v] = cost
        self.adjacency[v][u] = cost

    def remove_node(self, node_id):
        """Mark a node as failed."""
        self.failed_nodes.add(node_id)

    def recover_node(self, node_id):
        """Mark a node as recovered."""
        self.failed_nodes.discard(node_id)

    def get_active_adjacency(self):
        """Return adjacency excluding failed nodes."""
        active = {}
        for node, neighbours in self.adjacency.items():
            if node in self.failed_nodes:
                continue
            active[node] = {}
            for nb, cost in neighbours.items():
                if nb not in self.failed_nodes:
                    active[node][nb] = cost
        return active

    def dijkstra(self, source):
        """Compute shortest paths from source using Dijkstra's algorithm.
        Returns: (dist, prev) where dist[node] = min cost, prev[node] = previous node in path.
        """
        active = self.get_active_adjacency()
        dist = {source: 0.0}
        prev = {source: None}
        visited = set()
        pq = [(0.0, source)]

        while pq:
            d, u = heapq.heappop(pq)
            if u in visited:
                continue
            visited.add(u)

            if u not in active:
                continue
            for v, w in active[u].items():
                if v in visited:
                    continue
                new_dist = d + w
                if v not in dist or new_dist < dist[v]:
                    dist[v] = new_dist
                    prev[v] = u
                    heapq.heappush(pq, (new_dist, v))

        return dist, prev

    def get_path(self, prev, destination):
        """Reconstruct path from prev dict."""
        path = []
        current = destination
        while current is not None:
            path.append(current)
            current = prev.get(current)
        path.reverse()
        return path

    def compute_routing_table(self, source=None):
        """Compute full routing table from source.
        Returns list of (destination, path_string, cost) sorted alphabetically.
        """
        if source is None:
            source = self.node_id
        dist, prev = self.dijkstra(source)
        table = []
        for dest in sorted(dist.keys()):
            if dest == source:
                continue
            path = self.get_path(prev, dest)
            path_str = ''.join(path)
            table.append((dest, path_str, dist[dest]))
        return table

    def detect_cycle(self):
        """Detect if the current graph has a cycle using DFS."""
        active = self.get_active_adjacency()
        visited = set()

        def dfs(node, parent):
            visited.add(node)
            for nb in active.get(node, {}):
                if nb not in visited:
                    if dfs(nb, node):
                        return True
                elif nb != parent:
                    return True
            return False

        for node in active:
            if node not in visited:
                if dfs(node, None):
                    return True
        return False

    def get_all_nodes(self):
        """Return all known nodes."""
        return set(self.adjacency.keys())

    def show_topology(self):
        """Show graph visualization - image if matplotlib available, else ASCII."""
        active = self.get_active_adjacency()
        nodes = sorted(active.keys())

        # Collect edges
        edges = []
        seen = set()
        for u in nodes:
            for v in sorted(active[u].keys()):
                edge = tuple(sorted([u, v]))
                if edge not in seen:
                    seen.add(edge)
                    edges.append((u, v, active[u][v]))

        try:
            self._show_image(nodes, edges, active)
        except ImportError:
            self._show_ascii(nodes, edges, seen, active)

    def _show_image(self, nodes, edges, active):
        """Draw graph with matplotlib + networkx and open it."""
        import matplotlib
        matplotlib.use('Agg')  # Non-GUI backend, safe from any thread
        import networkx as nx
        import matplotlib.pyplot as plt
        import os
        import tempfile

        G = nx.Graph()
        for n in nodes:
            G.add_node(n)
        for u, v, cost in edges:
            G.add_edge(u, v, weight=cost)

        plt.figure(figsize=(8, 6))
        plt.title(f"Topology from [{self.node_id}]'s view", fontsize=14, fontweight='bold')

        pos = nx.spring_layout(G, seed=42)

        # Color nodes: current = green, failed = red, others = lightblue
        colors = []
        for n in G.nodes():
            if n == self.node_id:
                colors.append('#4CAF50')
            elif n in self.failed_nodes:
                colors.append('#F44336')
            else:
                colors.append('#64B5F6')

        nx.draw_networkx_nodes(G, pos, node_color=colors, node_size=800, edgecolors='black', linewidths=2)
        nx.draw_networkx_labels(G, pos, font_size=16, font_weight='bold')
        nx.draw_networkx_edges(G, pos, width=2, alpha=0.7)

        edge_labels = {(u, v): w for u, v, w in edges}
        nx.draw_networkx_edge_labels(G, pos, edge_labels=edge_labels, font_size=12)

        # Legend
        legend_text = f"Nodes: {len(nodes)}  Edges: {len(edges)}  Failed: {len(self.failed_nodes)}"
        plt.figtext(0.5, 0.02, legend_text, ha='center', fontsize=10, style='italic')

        plt.axis('off')
        plt.tight_layout()

        # Save and open
        img_path = os.path.join(tempfile.gettempdir(), f"topology_{self.node_id}.png")
        plt.savefig(img_path, dpi=150)
        plt.close()

        # Open image on Windows
        os.startfile(img_path)
        print(f"Topology image opened: {img_path}")

    def _show_ascii(self, nodes, edges, seen, active):
        """Fallback ASCII display."""
        adj_display = {n: [] for n in nodes}
        for u, v, cost in edges:
            adj_display[u].append((v, cost))
            adj_display[v].append((u, cost))

        lines = []
        lines.append("")
        lines.append(f"  ╔══════════════════════════════════════╗")
        lines.append(f"  ║  Topology from [{self.node_id}]'s view           ║")
        lines.append(f"  ╠══════════════════════════════════════╣")

        for i, node in enumerate(nodes):
            marker = " *" if node == self.node_id else "  "
            failed = " (DOWN)" if node in self.failed_nodes else ""
            neighbours = adj_display[node]
            nb_str = ", ".join(f"{nb}({c})" for nb, c in sorted(neighbours))
            lines.append(f"  ║{marker} [{node}]{failed} ── {nb_str:<28}║")
            if i < len(nodes) - 1:
                next_node = nodes[i + 1]
                edge_key = tuple(sorted([node, next_node]))
                if edge_key in seen:
                    cost = active[node].get(next_node, "")
                    lines.append(f"  ║     │  {cost}                            ║"[:42] + "║")
                else:
                    lines.append(f"  ║     :                                ║")

        lines.append(f"  ╠══════════════════════════════════════╣")
        lines.append(f"  ║  Nodes: {len(nodes)}  Edges: {len(edges)}  Failed: {len(self.failed_nodes):<8}║")
        lines.append(f"  ╚══════════════════════════════════════╝")
        lines.append("")
        print("\n".join(lines), flush=True)
