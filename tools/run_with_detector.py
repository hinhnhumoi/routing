#!/usr/bin/env python3
"""
Run a real node with race detection enabled.

Usage (same args as main.py):
    python tools/run_with_detector.py A 6000 config_A.txt 1.0 1.0

Ctrl+C to stop and generate race_report_<NodeID>.html in tools/.
"""
import sys
import os
import signal
import atexit

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from race_detector import RaceDetector
from node import Node
from config_parser import parse_args, parse_config


def main():
    node_id, port, config_file, routing_delay, update_interval = parse_args(sys.argv)
    neighbours, original_config = parse_config(config_file)
    node = Node(node_id, port, config_file, routing_delay, update_interval, neighbours, original_config)

    detector = RaceDetector()
    node.lock = detector.wrap_lock(node.lock, f"Node{node_id}.lock")
    detector.watch(node, 'neighbours', 'is_down', 'lsa_db', 'lsa_seq',
                   'merged_nodes', 'my_partition')

    report_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               f"race_report_{node_id}.html")

    def save_report():
        detector.dump_html(report_path)
        print(f"\n{detector.get_summary()}")

    atexit.register(save_report)

    print(f"[RaceDetector] Monitoring node {node_id} - Ctrl+C to stop and generate report")
    node.start()


if __name__ == "__main__":
    main()
