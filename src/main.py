"""
COMP3221 Assignment 1: Routing Algorithms
Main entry point - parses CLI arguments and starts the node.
"""
import sys
from node import Node
from config_parser import parse_args, parse_config


def main():
    node_id, port, config_file, routing_delay, update_interval = parse_args(sys.argv)
    neighbours, original_config = parse_config(config_file)
    node = Node(node_id, port, config_file, routing_delay, update_interval, neighbours, original_config)
    node.start()


if __name__ == "__main__":
    main()
