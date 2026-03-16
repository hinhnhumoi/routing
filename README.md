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


Cách dùng Race Detector
Cách 1: Chạy luôn script có sẵn (nhanh nhất)

cd c:\Users\ext_daothanh\learning\routing\tools
python run_race_check.py
Script sẽ tự động:

Tạo 3 node A, B, C kết nối thành tam giác
Bắn các lệnh CHANGE, FAIL, RECOVER, QUERY đồng thời từ nhiều thread
Đợi vài giây cho UPDATE lan truyền
Xuất ra 3 file HTML report
Sau khi chạy xong, mở file report:


start race_report_A.html
Cách 2: Tích hợp vào code của bạn
Nếu muốn test một scenario cụ thể, thêm vào đầu main.py:


from race_detector import RaceDetector

# Sau khi tạo node:
detector = RaceDetector()
node.lock = detector.wrap_lock(node.lock, "node.lock")
detector.watch(node, 'neighbours', 'is_down', 'lsa_db', 'lsa_seq', 
               'merged_nodes', 'my_partition')

# ... node chạy bình thường ...

# Khi muốn xuất report (ví dụ bắt Ctrl+C):
import signal
def on_exit(sig, frame):
    detector.dump_html("race_report.html")
    sys.exit(0)
signal.signal(signal.SIGINT, on_exit)
Cách đọc HTML report
Mở report trong browser, có 3 tab:

Tab Timeline - đồ thị theo thời gian:


Thread-1 (socket)   ●●■●●●■●●      ← mỗi dot là 1 event
Thread-2 (sending)  ◆  ●◆  ●◆      ← hover để xem chi tiết
Thread-3 (routing)  ◆●◆  ◆●◆

◆ xanh = Lock acquire          ◆ xám = Lock release
● vàng = READ không lock        ■ đỏ  = WRITE không lock  
◆ tím  = Thread bị blocked chờ lock
Tab Violations - bảng tổng hợp attribute nào bị access không lock nhiều nhất

Tab Event Log - danh sách chi tiết từng event, có filter


routing (dự án hiện tại) - 31 race conditions, 7 CRITICAL
Vấn đề nghiêm trọng hơn nhiều - hầu hết command handlers chạy hoàn toàn KHÔNG có lock:

Mức độ	Vấn đề	Vị trí
CRITICAL	_handle_reset() thay đổi 6 fields không lock	command_handler.py
CRITICAL	_handle_merge() sửa lsa_db, neighbours, merged_nodes không lock	command_handler.py
CRITICAL	_handle_split() sửa lsa_db, my_partition không lock	command_handler.py
CRITICAL	get_active_adjacency() iterate dict trong khi thread khác modify	graph.py
CRITICAL	dijkstra() đọc adjacency không lock	graph.py
HIGH	_handle_change() sửa neighbours không lock	command_handler.py
HIGH	_handle_fail/recover() sửa is_down không lock	command_handler.py
HIGH	_update_own_lsa() sửa lsa_seq, lsa_db không lock	node.py
MEDIUM	Print output interleave ở mọi thread	Tất cả threads
Đặc điểm: Lock gần như không được dùng trong command handlers. Đây là race condition ở mức dictionary iteration concurrent modification - có thể crash chương trình.