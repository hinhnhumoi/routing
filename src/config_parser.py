"""
Handles CLI argument validation and config file parsing.
"""
import sys
import os


def parse_args(argv):
    """Parse and validate command-line arguments.
    Returns: (node_id, port, config_file, routing_delay, update_interval)
    """
    if len(argv) < 6:
        print("Error: Insufficient arguments provided. Usage: ./Routing.sh <Node-ID> <Port-NO> <Node-Config-File> <RoutingDelay> <UpdateInterval>")
        sys.exit(1)

    node_id = argv[1]
    if len(node_id) != 1 or not node_id.isalpha() or not node_id.isupper():
        print("Error: Invalid Node-ID.")
        sys.exit(1)

    try:
        port = int(argv[2])
    except ValueError:
        print("Error: Invalid Port number. Must be an integer.")
        sys.exit(1)

    config_file = argv[3]
    if not os.path.exists(config_file):
        print(f"Error: Configuration file {config_file} not found.")
        sys.exit(1)

    try:
        routing_delay = float(argv[4])
    except ValueError:
        print("Error: Invalid RoutingDelay. Must be numeric.")
        sys.exit(1)

    try:
        update_interval = float(argv[5])
    except ValueError:
        print("Error: Invalid UpdateInterval. Must be numeric.")
        sys.exit(1)

    return node_id, port, config_file, routing_delay, update_interval


def parse_config(config_file):
    """Parse the node configuration file.
    Returns: (neighbours dict, original_config lines)
    """
    with open(config_file, 'r') as f:
        lines = [line.strip() for line in f.readlines() if line.strip()]

    original_config = lines[:]

    # First line: number of neighbours
    try:
        n = int(lines[0])
    except ValueError:
        print("Error: Invalid configuration file format. (First line must be an integer.)")
        sys.exit(1)

    neighbours = {}
    for i in range(1, n + 1):
        if i >= len(lines):
            print("Error: Invalid configuration file format.")
            sys.exit(1)

        tokens = lines[i].split()
        if len(tokens) != 3:
            if len(tokens) > 3:
                print("Error: Invalid configuration file format.")
            else:
                print("Error: Invalid configuration file format. (Each neighbour entry must have exactly three tokens; cost must be numeric.)")
            sys.exit(1)

        neighbour_id = tokens[0]
        try:
            cost = float(tokens[1])
        except ValueError:
            print("Error: Invalid configuration file format. (Each neighbour entry must have exactly three tokens; cost must be numeric.)")
            sys.exit(1)

        try:
            neighbour_port = int(tokens[2])
        except ValueError:
            print("Error: Invalid configuration file format. (Each neighbour entry must have exactly three tokens; cost must be numeric.)")
            sys.exit(1)

        neighbours[neighbour_id] = {
            'cost': cost,
            'port': neighbour_port
        }

    return neighbours, original_config
