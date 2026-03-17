# COMP3221 Assignment 1: Distributed Routing Algorithm

A multi-threaded, distributed link-state routing simulator in Python. Each node runs as an independent process, exchanges topology updates via UDP, and computes least-cost paths using Dijkstra's algorithm.

## Project Structure

```
routing/
├── Routing.sh                  # Entry point shell script
├── configs/                    # Sample node config files
│   ├── Aconfig.txt
│   ├── Bconfig.txt
│   └── Cconfig.txt
└── src/
    ├── main.py                 # CLI argument parsing & node startup
    ├── node.py                 # Core Node class, thread coordination
    ├── graph.py                # Graph topology & Dijkstra's algorithm
    ├── config_parser.py        # CLI & config file parsing
    ├── command_handler.py      # Dynamic command processing
    └── threads/
        ├── listening_thread.py # STDIN + UDP socket listener
        ├── sending_thread.py   # Periodic UPDATE broadcaster
        └── routing_thread.py   # Routing table computation
```

## Usage

```bash
./Routing.sh <Node-ID> <Port-NO> <Node-Config-File> <RoutingDelay> <UpdateInterval>
```

**Parameters:**
| Parameter | Description | Example |
|-----------|-------------|---------|
| `Node-ID` | Single uppercase letter (A-Z) | `A` |
| `Port-NO` | UDP port for this node | `6000` |
| `Node-Config-File` | Path to neighbour config | `configs/Aconfig.txt` |
| `RoutingDelay` | Seconds before first routing table computation | `5` |
| `UpdateInterval` | Seconds between UPDATE broadcasts | `2` |

**Example - start a 3-node network:**

```bash
# Terminal 1
./Routing.sh A 6000 configs/Aconfig.txt 5 2

# Terminal 2
./Routing.sh B 6001 configs/Bconfig.txt 5 2

# Terminal 3
./Routing.sh C 6002 configs/Cconfig.txt 5 2
```

## Config File Format

```
<number_of_neighbours>
<NeighbourID> <Cost> <Port>
...
```

Example (`Aconfig.txt`):
```
2
B 1.5 6001
C 3.0 6002
```

## Dynamic Commands

Type these into STDIN while a node is running:

| Command | Description |
|---------|-------------|
| `CHANGE <NeighbourID> <NewCost>` | Update link cost to a neighbour |
| `FAIL <Node-ID>` | Mark a node as down |
| `RECOVER <Node-ID>` | Recover a failed node |
| `QUERY <Destination>` | Show least-cost path to destination |
| `QUERY PATH <Source> <Destination>` | Show least-cost path between any two nodes |
| `RESET` | Reload original config |
| `BATCH UPDATE <Filename>` | Execute commands from a file |
| `MERGE <Node1> <Node2>` | Merge Node2 into Node1 (bonus) |
| `SPLIT` | Partition graph into two halves (bonus) |
| `CYCLE DETECT` | Check if topology contains a cycle (bonus) |
| `SHOW` | Visualize current topology (requires matplotlib + networkx) |

## Architecture

The system uses 4 daemon threads per node, coordinated by a single mutex (`threading.Lock`):

- **Listening Thread** - Reads STDIN commands and receives UDP packets. Holds the lock during all message processing to ensure atomic state updates.
- **Sending Thread** - Every `UpdateInterval` seconds, acquires the lock to snapshot the current state, then broadcasts UPDATE packets via UDP outside the lock.
- **Routing Thread** - Event-driven. Acquires the lock to compute the routing table (Dijkstra), then prints the result outside the lock.
- **Timeout Thread** - Checks for silent neighbours (no UPDATE received within `3 × UpdateInterval`). Marks them as failed and triggers routing recalculation.

### Thread Safety

All reads and writes to shared state (`neighbours`, `graph`, `is_down`, `last_heard`) are protected by `node.lock`:

- `is_down` is always checked inside the lock context to prevent stale reads
- Sending thread uses a single lock acquisition to atomically read `is_down` + copy neighbours + build packet
- I/O operations (`print`, `sendto`) are performed outside the lock to avoid blocking other threads
- No nested lock acquisitions exist, so deadlock is impossible

## Algorithms

- **Topology Exchange**: Link-state protocol - each node broadcasts its direct neighbours; nodes build a full topology map from received updates.
- **Shortest Path**: Dijkstra's algorithm on the active (non-failed) subgraph.
- **Failure Detection**: If no UPDATE is received from a neighbour within `3 × UpdateInterval`, the node is marked as failed.


## Race Condition Detector

The project includes a built-in race detector tool that instruments lock usage and shared state accesses, then generates interactive HTML reports.

### Quick Start

```bash
cd tools
python run_race_check.py
```

The script automatically:
1. Creates a 3-node triangle network (A, B, C)
2. Fires concurrent commands (CHANGE, FAIL, RECOVER, QUERY, MERGE, SPLIT, CYCLE DETECT) from multiple threads
3. Waits for UPDATE propagation
4. Generates HTML reports per node

Open a report:
```bash
start race_report_A.html
```

### Manual Integration

To test a specific scenario, add to your code:

```python
from race_detector import RaceDetector

detector = RaceDetector()
node.lock = detector.wrap_lock(node.lock, "node.lock")
detector.watch(node, 'neighbours', 'is_down', 'lsa_db', 'lsa_seq',
               'merged_nodes', 'my_partition')

# Export report on exit:
import signal
def on_exit(sig, frame):
    detector.dump_html("race_report.html")
    sys.exit(0)
signal.signal(signal.SIGINT, on_exit)
```

### Reading the HTML Report

The report has 3 tabs:

- **Timeline** - Visual timeline of thread events (hover for details)
  - Green = Lock acquire, Grey = Lock release
  - Yellow = READ without lock, Red = WRITE without lock
  - Purple = Thread blocked waiting for lock
- **Violations** - Summary table of unprotected accesses by attribute
- **Event Log** - Chronological list of all events with filters