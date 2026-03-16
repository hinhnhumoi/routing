"""
Network topology visualizer (debug tool, not part of submission).
Usage: python tools/visualizer.py configs/Aconfig.txt configs/Bconfig.txt configs/Cconfig.txt
Requires: pip install networkx matplotlib
"""
import sys
import networkx as nx
import matplotlib.pyplot as plt


def parse_config(filename):
    """Parse a node config file, return (node_id, neighbours)."""
    # Extract node ID from filename (e.g., Aconfig.txt -> A)
    node_id = filename.split('/')[-1].split('\\')[-1][0]
    with open(filename, 'r') as f:
        lines = [l.strip() for l in f if l.strip()]
    n = int(lines[0])
    neighbours = {}
    for i in range(1, n + 1):
        tokens = lines[i].split()
        neighbours[tokens[0]] = float(tokens[1])
    return node_id, neighbours


def main():
    if len(sys.argv) < 2:
        print("Usage: python visualizer.py <config1> <config2> ...")
        sys.exit(1)

    G = nx.Graph()

    for config_file in sys.argv[1:]:
        node_id, neighbours = parse_config(config_file)
        G.add_node(node_id)
        for nb, cost in neighbours.items():
            G.add_edge(node_id, nb, weight=cost)

    # Draw
    pos = nx.spring_layout(G, seed=42)
    plt.figure(figsize=(8, 6))
    plt.title("Network Topology", fontsize=16)

    # Nodes
    nx.draw_networkx_nodes(G, pos, node_color='lightblue', node_size=700, edgecolors='black')
    nx.draw_networkx_labels(G, pos, font_size=14, font_weight='bold')

    # Edges with weights
    nx.draw_networkx_edges(G, pos, width=2, alpha=0.7)
    edge_labels = nx.get_edge_attributes(G, 'weight')
    nx.draw_networkx_edge_labels(G, pos, edge_labels=edge_labels, font_size=11)

    plt.axis('off')
    plt.tight_layout()
    plt.savefig('topology.png', dpi=150)
    print("Saved to topology.png")
    plt.show()


if __name__ == "__main__":
    main()
