#!/usr/bin/env python3
"""
Race Condition Checker - runs a simulated multi-node scenario with
instrumentation to detect and visualize race conditions.

Usage:
    cd routing/tools
    python run_race_check.py

This will:
1. Start multiple nodes with instrumented locks
2. Send commands (CHANGE, FAIL, RECOVER, QUERY, MERGE, SPLIT, etc.)
3. Let them exchange UPDATEs for a few seconds
4. Generate an HTML report: race_report.html
"""
import sys
import os
import threading
import time
import socket
import io

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from race_detector import RaceDetector
from node import Node
from config_parser import parse_config


def create_temp_config(neighbours):
    """Create a temporary config file content and return path."""
    import tempfile
    lines = [str(len(neighbours))]
    for nb_id, cost, port in neighbours:
        lines.append(f"{nb_id} {cost} {port}")
    fd, path = tempfile.mkstemp(suffix='.txt', prefix='config_')
    with os.fdopen(fd, 'w') as f:
        f.write('\n'.join(lines) + '\n')
    return path


def inject_stdin_lines(node, lines, delay_between=0.3):
    """Simulate STDIN input by calling _process_message on the listening thread."""
    time.sleep(1.5)  # Wait for node to be ready
    for line in lines:
        time.sleep(delay_between)
        try:
            node.listening_thread._process_message(line.strip())
        except Exception as e:
            print(f"  [inject] Error processing '{line.strip()}': {e}")


def run_scenario():
    print("=" * 60)
    print("  Race Condition Detector")
    print("  Simulating multi-command scenario...")
    print("=" * 60)
    print()

    # Create configs for a small network: A--B--C (triangle)
    #   A: neighbours B(5.0:6001), C(10.0:6002)
    #   B: neighbours A(5.0:6000), C(3.0:6002)
    #   C: neighbours A(10.0:6000), B(3.0:6001)
    config_a = create_temp_config([('B', 5.0, 6001), ('C', 10.0, 6002)])
    config_b = create_temp_config([('A', 5.0, 6000), ('C', 3.0, 6002)])
    config_c = create_temp_config([('A', 10.0, 6000), ('B', 3.0, 6001)])

    configs = [config_a, config_b, config_c]

    try:
        # Parse configs
        neighbours_a, orig_a = parse_config(config_a)
        neighbours_b, orig_b = parse_config(config_b)
        neighbours_c, orig_c = parse_config(config_c)

        # Create nodes with short intervals for faster testing
        routing_delay = 1.0
        update_interval = 1.0

        node_a = Node('A', 6000, config_a, routing_delay, update_interval, neighbours_a, orig_a)
        node_b = Node('B', 6001, config_b, routing_delay, update_interval, neighbours_b, orig_b)
        node_c = Node('C', 6002, config_c, routing_delay, update_interval, neighbours_c, orig_c)

        # Install race detectors
        detectors = {}
        for name, node in [('A', node_a), ('B', node_b), ('C', node_c)]:
            d = RaceDetector()
            node.lock = d.wrap_lock(node.lock, f"Node{name}.lock")
            d.watch(node, 'neighbours', 'is_down', 'lsa_db', 'lsa_seq',
                    'merged_nodes', 'my_partition')
            detectors[name] = d

        print("[*] Starting nodes A, B, C...")
        print()

        # Suppress stdout noise during test
        real_stdout = sys.stdout
        sys.stdout = io.StringIO()

        # Start nodes in background threads (node.start() blocks on stdin)
        def start_node(node):
            try:
                node.listening_thread = __import__('threads.listening_thread', fromlist=['ListeningThread']).ListeningThread(node)
                node.sending_thread = __import__('threads.sending_thread', fromlist=['SendingThread']).SendingThread(node)
                node.routing_thread = __import__('threads.routing_thread', fromlist=['RoutingThread']).RoutingThread(node)

                node.listening_thread.daemon = True
                node.sending_thread.daemon = True
                node.routing_thread.daemon = True

                node.timeout_thread = threading.Thread(target=node._check_timeouts, daemon=True)

                # Start socket listener only (skip stdin)
                socket_thread = threading.Thread(
                    target=node.listening_thread._listen_socket, daemon=True
                )
                socket_thread.start()
                node.sending_thread.start()
                node.routing_thread.start()
                node.timeout_thread.start()
            except Exception:
                pass

        for node in [node_a, node_b, node_c]:
            t = threading.Thread(target=start_node, args=(node,), daemon=True)
            t.start()

        # Give threads time to start
        time.sleep(2)

        # Scenario: fire various commands to trigger race conditions
        commands_a = [
            'CHANGE B 2.0',         # Change edge cost
            'QUERY C',              # Query path
            'QUERY PATH B C',       # Query path between others
            'FAIL B',               # Fail a neighbour
            'RECOVER B',            # Recover
            'CHANGE C 1.5',         # Another change
            'CYCLE DETECT',         # Cycle detection
        ]

        commands_b = [
            'CHANGE A 2.0',
            'CHANGE C 1.0',
            'QUERY A',
            'FAIL A',
            'RECOVER A',
        ]

        commands_c = [
            'CHANGE A 4.0',
            'QUERY B',
            'CHANGE B 2.5',
        ]

        # Inject commands in parallel to maximize race condition exposure
        threads = []
        for node, cmds in [(node_a, commands_a), (node_b, commands_b), (node_c, commands_c)]:
            t = threading.Thread(
                target=inject_stdin_lines,
                args=(node, cmds, 0.2),
                daemon=True
            )
            t.start()
            threads.append(t)

        # Wait for commands and propagation
        for t in threads:
            t.join(timeout=10)

        # Let updates propagate
        time.sleep(3)

        # Restore stdout
        sys.stdout = real_stdout

        # Generate reports
        print("[*] Generating reports...")
        print()

        report_dir = os.path.dirname(os.path.abspath(__file__))

        for name, detector in detectors.items():
            report_path = os.path.join(report_dir, f"race_report_{name}.html")
            detector.dump_html(report_path)

            summary = detector.get_summary()
            print(f"--- Node {name} ---")
            print(summary)
            print()

        # Also generate a combined report
        print("=" * 60)
        total_violations = sum(len(d._violations) for d in detectors.values())
        total_accesses = sum(len(d._access_events) for d in detectors.values())
        print(f"  TOTAL: {total_violations} violations / {total_accesses} state accesses")
        print(f"  Reports saved to: {report_dir}/race_report_[A|B|C].html")
        print("=" * 60)

        if total_violations > 0:
            print()
            print("  Open the HTML reports in a browser to see the interactive timeline!")
            print("  - Red squares = UNPROTECTED WRITES (critical)")
            print("  - Yellow circles = UNPROTECTED READS (warning)")
            print("  - Green diamonds = Lock acquire")
            print("  - Grey diamonds = Lock release")
            print("  - Purple diamonds = Lock contention (blocked)")

    finally:
        # Cleanup temp configs
        for path in configs:
            try:
                os.unlink(path)
            except OSError:
                pass


if __name__ == '__main__':
    run_scenario()
