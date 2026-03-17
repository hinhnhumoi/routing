#!/usr/bin/env python3
"""
Race Condition Checker - runs a simulated multi-node scenario with
instrumentation to detect and visualize race conditions.

Usage:
    cd routing
    python tools/run_race_check.py

This will:
1. Start 5 nodes (A-B-C-D-E) with instrumented locks
2. Send commands (CHANGE, FAIL, RECOVER, QUERY, MERGE, SPLIT, RESET, etc.)
3. Let them exchange UPDATEs for a few seconds
4. Generate an HTML report: race_report_[A-E].html
"""
import sys
import os
import threading
import time
import socket
import io
import tempfile

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from race_detector import RaceDetector
from node import Node
from config_parser import parse_config


def create_temp_config(neighbours):
    """Create a temporary config file content and return path."""
    lines = [str(len(neighbours))]
    for nb_id, cost, port in neighbours:
        lines.append(f"{nb_id} {cost} {port}")
    fd, path = tempfile.mkstemp(suffix='.txt', prefix='config_')
    with os.fdopen(fd, 'w') as f:
        f.write('\n'.join(lines) + '\n')
    return path


def create_batch_file(commands):
    """Create a temporary batch command file."""
    fd, path = tempfile.mkstemp(suffix='.txt', prefix='batch_')
    with os.fdopen(fd, 'w') as f:
        f.write('\n'.join(commands) + '\n')
    return path


def inject_stdin_lines(node, lines, delay_between=0.3):
    """Simulate STDIN input by calling _process_message on the listening thread."""
    time.sleep(1.5)  # Wait for node to be ready
    for line in lines:
        time.sleep(delay_between)
        try:
            node.listening_thread._process_message(line.strip())
        except SystemExit:
            pass  # Some error commands cause sys.exit, catch it
        except Exception as e:
            print(f"  [inject] Error processing '{line.strip()}': {e}")


def start_node_threads(node):
    """Start node threads without blocking on stdin."""
    try:
        from threads.listening_thread import ListeningThread
        from threads.sending_thread import SendingThread
        from threads.routing_thread import RoutingThread

        node.listening_thread = ListeningThread(node)
        node.sending_thread = SendingThread(node)
        node.routing_thread = RoutingThread(node)

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
    except Exception as e:
        print(f"  [start_node] Error: {e}")


def run_scenario():
    print("=" * 70)
    print("  Race Condition Detector — Comprehensive 5-Node Scenario")
    print("=" * 70)
    print()

    # ─────────────────────────────────────────────
    # Network topology (pentagon + cross links):
    #
    #       A(6000)
    #      / \
    #  1.0/   \4.0
    #    /     \
    #   B(6001)─C(6002)
    #   |  2.0  |
    #3.0|      |2.5
    #   |      |
    #   D(6003)─E(6004)
    #      1.5
    #
    # ─────────────────────────────────────────────
    config_a = create_temp_config([('B', 1.0, 6001), ('C', 4.0, 6002)])
    config_b = create_temp_config([('A', 1.0, 6000), ('C', 2.0, 6002), ('D', 3.0, 6003)])
    config_c = create_temp_config([('A', 4.0, 6000), ('B', 2.0, 6001), ('E', 2.5, 6004)])
    config_d = create_temp_config([('B', 3.0, 6001), ('E', 1.5, 6003)])
    config_e = create_temp_config([('C', 2.5, 6002), ('D', 1.5, 6003)])

    configs = [config_a, config_b, config_c, config_d, config_e]

    # Create batch file for testing BATCH UPDATE
    batch_file = create_batch_file([
        'CHANGE B 0.5',
        'CHANGE C 3.0',
    ])

    try:
        # Parse configs
        neighbours_a, orig_a = parse_config(config_a)
        neighbours_b, orig_b = parse_config(config_b)
        neighbours_c, orig_c = parse_config(config_c)
        neighbours_d, orig_d = parse_config(config_d)
        neighbours_e, orig_e = parse_config(config_e)

        # Create nodes with short intervals
        routing_delay = 1.0
        update_interval = 1.0

        node_a = Node('A', 6000, config_a, routing_delay, update_interval, neighbours_a, orig_a)
        node_b = Node('B', 6001, config_b, routing_delay, update_interval, neighbours_b, orig_b)
        node_c = Node('C', 6002, config_c, routing_delay, update_interval, neighbours_c, orig_c)
        node_d = Node('D', 6003, config_d, routing_delay, update_interval, neighbours_d, orig_d)
        node_e = Node('E', 6004, config_e, routing_delay, update_interval, neighbours_e, orig_e)

        nodes = {'A': node_a, 'B': node_b, 'C': node_c, 'D': node_d, 'E': node_e}

        # Install race detectors
        detectors = {}
        for name, node in nodes.items():
            d = RaceDetector()
            node.lock = d.wrap_lock(node.lock, f"Node{name}.lock")
            d.watch(node, 'neighbours', 'is_down', 'lsa_db', 'lsa_seq',
                    'merged_nodes', 'my_partition')
            detectors[name] = d

        print("[*] Starting 5 nodes (A, B, C, D, E)...")

        # Suppress stdout noise during test
        real_stdout = sys.stdout
        sys.stdout = io.StringIO()

        # Start all nodes
        for node in nodes.values():
            t = threading.Thread(target=start_node_threads, args=(node,), daemon=True)
            t.start()

        # Wait for threads to start and initial routing to compute
        time.sleep(3)

        # ── Phase 1: QUERY and basic commands ──
        commands_a = [
            'QUERY B',              # Simple query
            'QUERY E',              # Multi-hop query
            'QUERY PATH B E',       # Query path from another source
            'CHANGE B 0.5',         # Reduce cost to B
            'QUERY B',              # Verify changed cost
            'CYCLE DETECT',         # Should detect cycle (pentagon)
        ]

        # ── Phase 2: FAIL/RECOVER ──
        commands_b = [
            'QUERY A',
            'CHANGE C 1.0',         # Reduce B-C cost
            'FAIL D',               # Mark D as failed from B's perspective
            'QUERY D',              # D should be unreachable
            'RECOVER D',            # Recover D
            'QUERY D',              # D reachable again
        ]

        # ── Phase 3: Concurrent topology changes ──
        commands_c = [
            'CHANGE A 2.0',         # Change C-A cost
            'CHANGE E 5.0',         # Change C-E cost
            'QUERY PATH A E',       # Query across changed costs
            'QUERY B',
        ]

        # ── Phase 4: More concurrent stress ──
        commands_d = [
            'QUERY A',
            'CHANGE B 1.0',         # Change D-B cost
            'QUERY PATH A C',
            'CYCLE DETECT',
        ]

        # ── Phase 5: RESET + BATCH UPDATE ──
        commands_e = [
            'QUERY A',
            'CHANGE C 1.0',         # Change E-C cost
            'CHANGE D 3.0',         # Change E-D cost
            'QUERY PATH A D',
        ]

        sys.stdout = real_stdout
        print("[*] Phase 1: Injecting commands (QUERY, CHANGE, CYCLE DETECT)...")
        sys.stdout = io.StringIO()

        # Inject commands in parallel (maximizes race condition exposure)
        threads = []
        for node, cmds in [(node_a, commands_a), (node_b, commands_b),
                           (node_c, commands_c), (node_d, commands_d),
                           (node_e, commands_e)]:
            t = threading.Thread(
                target=inject_stdin_lines,
                args=(node, cmds, 0.15),
                daemon=True
            )
            t.start()
            threads.append(t)

        for t in threads:
            t.join(timeout=15)

        # Let updates propagate
        time.sleep(2)

        # ── Phase 6: FAIL self + RECOVER self ──
        sys.stdout = real_stdout
        print("[*] Phase 2: FAIL/RECOVER self node...")
        sys.stdout = io.StringIO()

        inject_stdin_lines(node_a, [
            'FAIL A',               # Node A goes down
        ], delay_between=0.2)
        time.sleep(1)

        inject_stdin_lines(node_a, [
            'RECOVER A',            # Node A comes back up
        ], delay_between=0.2)
        time.sleep(1)

        # ── Phase 7: RESET ──
        sys.stdout = real_stdout
        print("[*] Phase 3: RESET node A...")
        sys.stdout = io.StringIO()

        inject_stdin_lines(node_a, [
            'RESET',                # Reset to original config
            'QUERY B',              # Verify original costs
        ], delay_between=0.3)
        time.sleep(2)

        # ── Phase 8: BATCH UPDATE ──
        sys.stdout = real_stdout
        print("[*] Phase 4: BATCH UPDATE on node A...")
        sys.stdout = io.StringIO()

        inject_stdin_lines(node_a, [
            f'BATCH UPDATE {batch_file}',
            'QUERY B',              # Should reflect batch changes
        ], delay_between=0.3)
        time.sleep(2)

        # ── Phase 9: MERGE ──
        sys.stdout = real_stdout
        print("[*] Phase 5: MERGE D into B...")
        sys.stdout = io.StringIO()

        inject_stdin_lines(node_b, [
            'MERGE B D',            # Merge D into B
            'QUERY E',              # Path should be updated
            'CYCLE DETECT',
        ], delay_between=0.3)
        time.sleep(2)

        # ── Phase 10: SPLIT ──
        sys.stdout = real_stdout
        print("[*] Phase 6: SPLIT...")
        sys.stdout = io.StringIO()

        inject_stdin_lines(node_c, [
            'SPLIT',                # Partition the graph
            'QUERY E',              # Only nodes in same partition
        ], delay_between=0.3)
        time.sleep(2)

        # ── Phase 11: Final concurrent stress test ──
        sys.stdout = real_stdout
        print("[*] Phase 7: Final concurrent stress test...")
        sys.stdout = io.StringIO()

        stress_threads = []
        for node_obj in nodes.values():
            cmds = [
                'QUERY A',
                'CYCLE DETECT',
            ]
            t = threading.Thread(
                target=inject_stdin_lines,
                args=(node_obj, cmds, 0.05),
                daemon=True
            )
            t.start()
            stress_threads.append(t)

        for t in stress_threads:
            t.join(timeout=10)

        time.sleep(2)

        # Restore stdout
        sys.stdout = real_stdout

        # Generate reports
        print()
        print("[*] Generating race condition reports...")
        print()

        report_dir = os.path.dirname(os.path.abspath(__file__))

        for name, detector in sorted(detectors.items()):
            report_path = os.path.join(report_dir, f"race_report_{name}.html")
            detector.dump_html(report_path)

            summary = detector.get_summary()
            print(f"--- Node {name} ---")
            print(summary)
            print()

        # Combined summary
        print("=" * 70)
        total_violations = sum(len(d._violations) for d in detectors.values())
        total_accesses = sum(len(d._access_events) for d in detectors.values())
        total_critical = sum(
            sum(1 for v in d._violations if v.severity == 'critical')
            for d in detectors.values()
        )
        total_danger = sum(
            sum(1 for v in d._violations if v.severity == 'danger')
            for d in detectors.values()
        )
        total_warning = sum(
            sum(1 for v in d._violations if v.severity == 'warning')
            for d in detectors.values()
        )

        print(f"  TOTAL across all 5 nodes:")
        print(f"    State accesses : {total_accesses}")
        print(f"    CRITICAL       : {total_critical}")
        print(f"    DANGER         : {total_danger}")
        print(f"    WARNING        : {total_warning}")
        print(f"    Violations     : {total_violations}")
        print(f"  Reports: {report_dir}/race_report_[A-E].html")
        print("=" * 70)

        if total_critical > 0:
            print()
            print("  !! CRITICAL race conditions found — these can CRASH the program!")
            print("  Open the HTML reports in a browser for details.")
        elif total_danger > 0:
            print()
            print("  !! DANGER-level issues found — may cause incorrect results.")
            print("  Open the HTML reports for details.")
        elif total_warning > 0:
            print()
            print("  Warnings found (primitive reads without lock) — usually tolerable.")
        else:
            print()
            print("  All clear — no race conditions detected!")

    finally:
        # Cleanup temp files
        for path in configs:
            try:
                os.unlink(path)
            except OSError:
                pass
        try:
            os.unlink(batch_file)
        except OSError:
            pass


if __name__ == '__main__':
    run_scenario()
