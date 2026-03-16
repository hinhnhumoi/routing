"""
Race Condition Detector - instruments threading.RLock and shared state access.

Severity levels:
  CRITICAL  - Unprotected WRITE to dict/list, or dict iteration while another thread writes
  DANGER    - Unprotected WRITE to any shared var, or read of dict/list without lock
  WARNING   - Unprotected READ of single primitive (bool/int/None) — usually tolerable
  SAFE      - Access while holding lock

Usage:
    from race_detector import RaceDetector
    detector = RaceDetector()
    node.lock = detector.wrap_lock(node.lock, "node.lock")
    detector.watch(node, "neighbours", "is_down", "lsa_db", "lsa_seq", "merged_nodes", "my_partition")
    # ... run your code ...
    detector.dump_html("race_report.html")
"""
import threading
import time
import traceback
import json
import os
from collections import defaultdict

# Attributes that are container types (dict, set, list) — more dangerous to access without lock
_CONTAINER_ATTRS = {'neighbours', 'lsa_db', 'merged_nodes', 'last_heard'}
# Attributes that are primitive types (bool, int, None-able) — less dangerous for reads
_PRIMITIVE_ATTRS = {'is_down', 'lsa_seq', 'my_partition'}

# Stack patterns that indicate dict iteration (for/items/keys/values)
_ITERATION_PATTERNS = {'.items()', '.keys()', '.values()', 'for ', 'in self.node.', 'dict(self.node.'}


def _classify_severity(attr, access_type, has_lock, stack_summary, concurrent_info):
    """
    Classify severity based on:
    - Whether lock is held
    - Read vs write
    - Container vs primitive
    - Whether it's dict iteration
    - Whether another thread is concurrently writing
    """
    if has_lock:
        return 'safe'

    # Check if this is a dict iteration from stack
    is_iteration = False
    for frame in stack_summary:
        line = frame.line or ''
        if any(p in line for p in _ITERATION_PATTERNS):
            is_iteration = True
            break

    is_container = attr.split('[')[0] in _CONTAINER_ATTRS  # handle "lsa_db[X]" style
    is_primitive = attr in _PRIMITIVE_ATTRS

    if access_type == 'write':
        if is_container or is_iteration:
            return 'critical'   # Writing to a container without lock = very dangerous
        return 'danger'         # Writing primitive without lock = still bad

    # access_type == 'read'
    if is_iteration and is_container:
        return 'critical'       # Iterating a dict while others can modify = RuntimeError crash

    if concurrent_info.get('other_thread_writing'):
        return 'danger'         # Reading while another thread is actively writing

    if is_container:
        return 'danger'         # Reading container without lock (stale reads, partial state)

    if is_primitive:
        return 'warning'        # Reading a bool/int without lock — usually tolerable

    return 'warning'


def _build_reason(attr, access_type, has_lock, severity, stack, concurrent_info):
    """Build a human-readable explanation of why this access is flagged."""
    if has_lock:
        return "Lock held - safe"
    base_attr = attr.split('[')[0]
    is_iteration = any(
        any(p in (f.line or '') for p in _ITERATION_PATTERNS)
        for f in stack
    )

    if severity == 'critical':
        if access_type == 'write' and base_attr in _CONTAINER_ATTRS:
            return f"WRITE to container '{base_attr}' without lock - other threads may be iterating"
        if is_iteration:
            return f"Iterating '{base_attr}' without lock - concurrent modify can crash (RuntimeError)"
        return f"Critical unprotected {access_type} on '{attr}'"

    if severity == 'danger':
        if access_type == 'write':
            return f"WRITE to '{attr}' without lock - other threads may read stale value"
        if concurrent_info.get('other_thread_writing'):
            return f"READ '{attr}' while another thread is writing - torn read possible"
        if base_attr in _CONTAINER_ATTRS:
            return f"READ container '{base_attr}' without lock - may see partial state"
        return f"Unprotected {access_type} on '{attr}'"

    # warning
    if base_attr in _PRIMITIVE_ATTRS:
        return f"READ primitive '{attr}' without lock - usually OK if not correlated with other state"
    return f"Unprotected read of '{attr}'"


class AccessEvent:
    __slots__ = ('timestamp', 'thread_name', 'thread_id', 'attr', 'access_type',
                 'has_lock', 'lock_owner', 'stack_summary', 'severity', 'reason')

    def __init__(self, thread_name, thread_id, attr, access_type, has_lock,
                 lock_owner, stack_summary, severity, reason):
        self.timestamp = time.monotonic()
        self.thread_name = thread_name
        self.thread_id = thread_id
        self.attr = attr
        self.access_type = access_type  # 'read' or 'write'
        self.has_lock = has_lock
        self.lock_owner = lock_owner
        self.stack_summary = stack_summary
        self.severity = severity
        self.reason = reason


class LockEvent:
    __slots__ = ('timestamp', 'thread_name', 'thread_id', 'action', 'lock_name', 'stack_summary')

    def __init__(self, thread_name, thread_id, action, lock_name, stack_summary):
        self.timestamp = time.monotonic()
        self.thread_name = thread_name
        self.thread_id = thread_id
        self.action = action  # 'acquire', 'release', 'blocked'
        self.lock_name = lock_name
        self.stack_summary = stack_summary


class InstrumentedRLock:
    """Wraps threading.RLock to track acquire/release events."""

    def __init__(self, original_lock, name, detector):
        self._lock = original_lock
        self._name = name
        self._detector = detector
        self._owner = None
        self._depth = 0

    def acquire(self, blocking=True, timeout=-1):
        t = threading.current_thread()
        stack = self._get_stack()

        # Log blocked if another thread holds it
        if self._owner and self._owner != t.ident:
            self._detector._log_lock_event(
                LockEvent(t.name, t.ident, 'blocked', self._name, stack)
            )

        result = self._lock.acquire(blocking, timeout) if timeout != -1 else self._lock.acquire(blocking)
        if result:
            self._owner = t.ident
            self._depth += 1
            self._detector._current_lock_owner = t.ident
            self._detector._log_lock_event(
                LockEvent(t.name, t.ident, 'acquire', self._name, stack)
            )
        return result

    def release(self):
        self._depth -= 1
        if self._depth == 0:
            self._owner = None
            self._detector._current_lock_owner = None
        t = threading.current_thread()
        stack = self._get_stack()
        self._detector._log_lock_event(
            LockEvent(t.name, t.ident, 'release', self._name, stack)
        )
        self._lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *args):
        self.release()

    def _get_stack(self):
        frames = traceback.extract_stack()
        # Filter out this file's frames
        relevant = [f for f in frames if 'race_detector' not in f.filename]
        return relevant[-3:] if len(relevant) >= 3 else relevant

    # Forward any other RLock attributes
    def __getattr__(self, name):
        return getattr(self._lock, name)


class WatchedObject:
    """Descriptor-based watcher that intercepts __getattr__/__setattr__."""
    pass


class RaceDetector:
    """Main race condition detector. Tracks lock usage and shared state access."""

    def __init__(self, max_events=50000):
        self._lock_events = []
        self._access_events = []
        self._violations = []
        self._max_events = max_events
        self._current_lock_owner = None
        self._instrumented_lock = None
        self._internal_lock = threading.Lock()  # protects detector's own state
        self._start_time = time.monotonic()
        self._watched_attrs = {}  # {(obj_id, attr): original_value}
        self._thread_names = {}
        self._recent_writes = {}  # {thread_id: (attr, timestamp)} - for concurrent write detection

    def wrap_lock(self, original_lock, name="lock"):
        """Wrap an RLock with instrumentation."""
        instrumented = InstrumentedRLock(original_lock, name, self)
        self._instrumented_lock = instrumented
        return instrumented

    def watch(self, obj, *attr_names):
        """Watch attributes on an object for unprotected access.
        Uses a class-level registry so multiple instances of the same class
        each dispatch to their own detector.
        """
        cls = obj.__class__
        watched = set(attr_names)

        # Build a registry mapping object id -> (detector, watched_attrs)
        # so we can handle multiple instances of the same class
        if not hasattr(cls, '_race_detector_registry'):
            cls._race_detector_registry = {}
            cls._race_original_getattr = cls.__getattribute__
            cls._race_original_setattr = cls.__setattr__

            original_getattr = cls._race_original_getattr
            original_setattr = cls._race_original_setattr

            def instrumented_getattr(self_obj, name):
                value = original_getattr(self_obj, name)
                if not name.startswith('_'):
                    registry = cls._race_detector_registry
                    entry = registry.get(id(self_obj))
                    if entry and name in entry[1]:
                        entry[0]._record_access(name, 'read')
                return value

            def instrumented_setattr(self_obj, name, value):
                registry = cls._race_detector_registry
                entry = registry.get(id(self_obj))
                if entry and name in entry[1]:
                    entry[0]._record_access(name, 'write')
                original_setattr(self_obj, name, value)

            cls.__getattribute__ = instrumented_getattr
            cls.__setattr__ = instrumented_setattr

        cls._race_detector_registry[id(obj)] = (self, watched)

    def watch_dict(self, obj, attr_name):
        """Watch a dict attribute for mutations by wrapping it with a TrackedDict."""
        original = getattr(obj, attr_name)
        if isinstance(original, dict):
            tracked = TrackedDict(original, attr_name, self)
            object.__setattr__(obj, attr_name, tracked)

    def _record_access(self, attr, access_type):
        t = threading.current_thread()
        has_lock = (self._instrumented_lock and
                    self._instrumented_lock._owner == t.ident)

        frames = traceback.extract_stack()
        relevant = [f for f in frames if 'race_detector' not in f.filename]
        stack = relevant[-3:] if len(relevant) >= 3 else relevant

        # Build concurrent info: check if another thread is currently writing
        concurrent_info = {}
        with self._internal_lock:
            if not has_lock and self._recent_writes:
                base_attr = attr.split('[')[0]
                for tid, (w_attr, w_time) in self._recent_writes.items():
                    if tid != t.ident and w_attr.split('[')[0] == base_attr:
                        if time.monotonic() - w_time < 0.01:  # within 10ms
                            concurrent_info['other_thread_writing'] = True
                            break

        severity = _classify_severity(attr, access_type, has_lock, stack, concurrent_info)

        # Build human-readable reason
        reason = _build_reason(attr, access_type, has_lock, severity, stack, concurrent_info)

        event = AccessEvent(
            t.name, t.ident, attr, access_type,
            has_lock, self._current_lock_owner, stack, severity, reason
        )

        with self._internal_lock:
            if len(self._access_events) < self._max_events:
                self._access_events.append(event)
            self._thread_names[t.ident] = t.name

            # Track recent writes for concurrent detection
            if access_type == 'write':
                self._recent_writes[t.ident] = (attr, time.monotonic())

            if severity in ('critical', 'danger', 'warning'):
                self._violations.append(event)

    def _log_lock_event(self, event):
        with self._internal_lock:
            if len(self._lock_events) < self._max_events:
                self._lock_events.append(event)
            self._thread_names[event.thread_id] = event.thread_name

    def get_summary(self):
        """Return a text summary of detected races."""
        # Count by severity
        by_severity = defaultdict(int)
        for v in self._violations:
            by_severity[v.severity] += 1

        lines = []
        lines.append(f"=== Race Condition Report ===")
        lines.append(f"Total lock events: {len(self._lock_events)}")
        lines.append(f"Total state accesses: {len(self._access_events)}")
        lines.append(f"")
        lines.append(f"  CRITICAL : {by_severity.get('critical', 0):>5}  (dict iteration + container writes — can CRASH)")
        lines.append(f"  DANGER   : {by_severity.get('danger', 0):>5}  (writes + container reads — wrong results)")
        lines.append(f"  WARNING  : {by_severity.get('warning', 0):>5}  (primitive reads — usually tolerable)")
        lines.append(f"  SAFE     : {len(self._access_events) - len(self._violations):>5}  (lock held)")
        lines.append("")

        # Group violations by severity, then by attr
        for sev in ('critical', 'danger', 'warning'):
            sev_events = [v for v in self._violations if v.severity == sev]
            if not sev_events:
                continue
            icon = {'critical': '!!!', 'danger': '!! ', 'warning': '!  '}[sev]
            lines.append(f"[{icon}] {sev.upper()} ({len(sev_events)}):")

            by_reason = defaultdict(list)
            for e in sev_events:
                by_reason[e.reason].append(e)

            for reason, events in sorted(by_reason.items(), key=lambda x: -len(x[1])):
                lines.append(f"  ({len(events)}x) {reason}")
                # Show first 2 unique locations
                seen = set()
                for e in events[:5]:
                    loc = " -> ".join(
                        f"{os.path.basename(f.filename)}:{f.lineno}"
                        for f in e.stack_summary
                    )
                    if loc not in seen:
                        seen.add(loc)
                        lines.append(f"        at {loc}  [{e.thread_name}]")
                        if len(seen) >= 2:
                            break
            lines.append("")

        return "\n".join(lines)

    def dump_html(self, filepath="race_report.html"):
        """Generate an interactive HTML timeline visualization."""
        # Merge and sort all events by timestamp
        all_events = []

        for e in self._lock_events:
            all_events.append({
                'time': e.timestamp - self._start_time,
                'thread': e.thread_name,
                'tid': e.thread_id,
                'type': 'lock',
                'action': e.action,
                'detail': e.lock_name,
                'severity': 'blocked' if e.action == 'blocked' else 'safe',
                'stack': [f"{os.path.basename(f.filename)}:{f.lineno} {f.name}: {f.line}"
                          for f in e.stack_summary],
                'reason': ''
            })

        for e in self._access_events:
            all_events.append({
                'time': e.timestamp - self._start_time,
                'thread': e.thread_name,
                'tid': e.thread_id,
                'type': 'access',
                'action': e.access_type,
                'detail': e.attr,
                'severity': e.severity,
                'stack': [f"{os.path.basename(f.filename)}:{f.lineno} {f.name}: {f.line}"
                          for f in e.stack_summary],
                'has_lock': e.has_lock,
                'reason': e.reason
            })

        all_events.sort(key=lambda x: x['time'])

        # Collect unique threads
        threads = sorted(set(e['thread'] for e in all_events))

        # Violation summary grouped by severity
        violation_summary = defaultdict(lambda: {
            'critical_r': 0, 'critical_w': 0,
            'danger_r': 0, 'danger_w': 0,
            'warning_r': 0, 'warning_w': 0,
            'reasons': [], 'stacks': []
        })
        for e in self._violations:
            key = e.attr.split('[')[0]  # group "lsa_db[A]" under "lsa_db"
            sev_key = f"{e.severity}_{'w' if e.access_type == 'write' else 'r'}"
            violation_summary[key][sev_key] += 1
            if e.reason not in violation_summary[key]['reasons']:
                violation_summary[key]['reasons'].append(e.reason)
            stack_str = " -> ".join(
                f"{os.path.basename(f.filename)}:{f.lineno}" for f in e.stack_summary
            )
            if stack_str not in violation_summary[key]['stacks']:
                violation_summary[key]['stacks'].append(stack_str)

        # Count by severity for stat cards
        sev_counts = defaultdict(int)
        for e in self._violations:
            sev_counts[e.severity] += 1

        html = self._generate_html(all_events, threads, violation_summary, sev_counts)

        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(html)

        print(f"Race condition report saved to: {os.path.abspath(filepath)}")
        return filepath

    def _generate_html(self, events, threads, violations, sev_counts):
        events_json = json.dumps(events[:8000])  # Limit for browser performance
        threads_json = json.dumps(threads)
        violations_json = json.dumps({
            k: {
                'critical_r': v['critical_r'], 'critical_w': v['critical_w'],
                'danger_r': v['danger_r'], 'danger_w': v['danger_w'],
                'warning_r': v['warning_r'], 'warning_w': v['warning_w'],
                'reasons': v['reasons'][:8], 'stacks': v['stacks'][:5]
            }
            for k, v in violations.items()
        })
        n_critical = sev_counts.get('critical', 0)
        n_danger = sev_counts.get('danger', 0)
        n_warning = sev_counts.get('warning', 0)
        n_safe = len(self._access_events) - len(self._violations)
        total_access = len(self._access_events)

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Race Condition Report</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: 'Segoe UI', system-ui, sans-serif; background: #0d1117; color: #c9d1d9; }}

.header {{
    background: linear-gradient(135deg, #161b22 0%, #0d1117 100%);
    border-bottom: 1px solid #30363d;
    padding: 20px 30px;
}}
.header h1 {{ color: #58a6ff; font-size: 24px; margin-bottom: 8px; }}
.header .subtitle {{ color: #8b949e; font-size: 14px; }}

.stats {{
    display: flex; gap: 16px; padding: 20px 30px;
    flex-wrap: wrap;
}}
.stat-card {{
    background: #161b22; border: 1px solid #30363d; border-radius: 8px;
    padding: 16px 24px; min-width: 180px;
}}
.stat-card.critical {{ border-color: #ff2d20; }}
.stat-card.danger {{ border-color: #f85149; }}
.stat-card.warning {{ border-color: #d29922; }}
.stat-card.safe {{ border-color: #3fb950; }}
.stat-value {{ font-size: 32px; font-weight: 700; }}
.stat-card.critical .stat-value {{ color: #ff2d20; }}
.stat-card.danger .stat-value {{ color: #f85149; }}
.stat-card.warning .stat-value {{ color: #d29922; }}
.stat-card.safe .stat-value {{ color: #3fb950; }}
.stat-label {{ color: #8b949e; font-size: 13px; margin-top: 4px; }}
.stat-sublabel {{ color: #6e7681; font-size: 11px; margin-top: 2px; }}

.tabs {{
    display: flex; gap: 0; padding: 0 30px; border-bottom: 1px solid #30363d;
    background: #161b22;
}}
.tab {{
    padding: 12px 20px; cursor: pointer; color: #8b949e;
    border-bottom: 2px solid transparent; font-size: 14px;
    transition: all 0.2s;
}}
.tab:hover {{ color: #c9d1d9; }}
.tab.active {{ color: #58a6ff; border-bottom-color: #58a6ff; }}

.panel {{ display: none; padding: 20px 30px; }}
.panel.active {{ display: block; }}

/* Timeline */
.timeline-container {{
    overflow-x: auto; overflow-y: auto;
    max-height: 600px; background: #0d1117;
    border: 1px solid #30363d; border-radius: 8px;
}}
.timeline-controls {{
    display: flex; gap: 12px; margin-bottom: 16px; align-items: center;
    flex-wrap: wrap;
}}
.timeline-controls label {{ color: #8b949e; font-size: 13px; }}
.timeline-controls select, .timeline-controls input {{
    background: #161b22; border: 1px solid #30363d; color: #c9d1d9;
    padding: 6px 10px; border-radius: 4px; font-size: 13px;
}}
canvas {{ display: block; }}

/* Violations table */
.violations-table {{
    width: 100%; border-collapse: collapse;
    background: #161b22; border-radius: 8px; overflow: hidden;
}}
.violations-table th {{
    background: #21262d; color: #8b949e; padding: 10px 16px;
    text-align: left; font-size: 12px; text-transform: uppercase;
    letter-spacing: 0.5px;
}}
.violations-table td {{
    padding: 10px 16px; border-top: 1px solid #30363d; font-size: 13px;
}}
.violations-table tr:hover td {{ background: #1c2128; }}
.badge {{
    display: inline-block; padding: 2px 8px; border-radius: 10px;
    font-size: 11px; font-weight: 600; margin: 1px 2px;
}}
.badge.critical {{ background: #ff2d20; color: #fff; }}
.badge.danger {{ background: #f85149; color: #fff; }}
.badge.warning {{ background: #d29922; color: #0d1117; }}
.badge.safe {{ background: #238636; color: #fff; }}
.reason-text {{ color: #8b949e; font-size: 12px; font-style: italic; margin: 2px 0; }}
.stack-trace {{
    font-family: 'Cascadia Code', 'Fira Code', monospace; font-size: 11px;
    color: #8b949e; margin-top: 4px; white-space: pre-wrap;
    max-height: 100px; overflow-y: auto;
}}

/* Event log */
.event-log {{
    max-height: 500px; overflow-y: auto;
    font-family: 'Cascadia Code', 'Fira Code', monospace; font-size: 12px;
    background: #161b22; border: 1px solid #30363d; border-radius: 8px;
    padding: 12px;
}}
.event-row {{
    display: flex; gap: 8px; padding: 3px 0;
    border-bottom: 1px solid #21262d;
}}
.event-row:hover {{ background: #1c2128; }}
.event-time {{ color: #484f58; min-width: 80px; }}
.event-thread {{ min-width: 140px; }}
.event-detail {{ flex: 1; }}
.event-row.critical {{ background: rgba(255,45,32,0.15); }}
.event-row.danger {{ background: rgba(248,81,73,0.1); }}
.event-row.warning {{ background: rgba(210,153,34,0.05); }}
.event-row.blocked {{ background: rgba(188,76,255,0.1); }}

/* Thread colors */
.thread-0 {{ color: #58a6ff; }}
.thread-1 {{ color: #3fb950; }}
.thread-2 {{ color: #d29922; }}
.thread-3 {{ color: #f778ba; }}
.thread-4 {{ color: #bc8cff; }}
.thread-5 {{ color: #79c0ff; }}

.tooltip {{
    position: fixed; background: #21262d; border: 1px solid #30363d;
    border-radius: 6px; padding: 10px; font-size: 12px;
    max-width: 400px; z-index: 1000; display: none;
    box-shadow: 0 4px 12px rgba(0,0,0,0.4);
}}
</style>
</head>
<body>
<div class="header">
    <h1>Race Condition Detector</h1>
    <div class="subtitle">Thread safety analysis for routing node</div>
</div>

<div class="stats">
    <div class="stat-card critical">
        <div class="stat-value">{n_critical}</div>
        <div class="stat-label">CRITICAL</div>
        <div class="stat-sublabel">Dict iteration + container writes (can CRASH)</div>
    </div>
    <div class="stat-card danger">
        <div class="stat-value">{n_danger}</div>
        <div class="stat-label">DANGER</div>
        <div class="stat-sublabel">Writes + container reads (wrong results)</div>
    </div>
    <div class="stat-card warning">
        <div class="stat-value">{n_warning}</div>
        <div class="stat-label">WARNING</div>
        <div class="stat-sublabel">Primitive reads (usually tolerable)</div>
    </div>
    <div class="stat-card safe">
        <div class="stat-value">{n_safe}</div>
        <div class="stat-label">SAFE</div>
        <div class="stat-sublabel">Lock held ({total_access} total accesses)</div>
    </div>
</div>

<div class="tabs">
    <div class="tab active" onclick="switchTab('timeline')">Timeline</div>
    <div class="tab" onclick="switchTab('violations')">Violations</div>
    <div class="tab" onclick="switchTab('log')">Event Log</div>
</div>

<div id="timeline" class="panel active">
    <div class="timeline-controls">
        <label>Filter:
            <select id="filterType">
                <option value="all">All Events</option>
                <option value="critical">CRITICAL Only</option>
                <option value="critical+danger">CRITICAL + DANGER</option>
                <option value="violations">All Violations</option>
                <option value="locks">Lock Events Only</option>
            </select>
        </label>
        <label>Thread:
            <select id="filterThread">
                <option value="all">All Threads</option>
            </select>
        </label>
        <label>Zoom:
            <input type="range" id="zoomLevel" min="1" max="50" value="10" style="width:150px">
        </label>
        <span id="zoomLabel" style="color:#8b949e;font-size:12px">10x</span>
    </div>
    <div class="timeline-container" id="timelineContainer">
        <canvas id="timelineCanvas"></canvas>
    </div>
</div>

<div id="violations" class="panel">
    <div style="margin-bottom:16px;padding:12px;background:#161b22;border:1px solid #30363d;border-radius:8px;font-size:13px;line-height:1.8">
        <strong style="color:#58a6ff">Severity Guide:</strong><br>
        <span style="color:#ff2d20">CRITICAL</span> - Dict iteration without lock, container writes &rarr; <strong>can CRASH</strong> (RuntimeError)<br>
        <span style="color:#f85149">DANGER</span> - Writes to shared vars, container reads &rarr; <strong>wrong results</strong>, stale data<br>
        <span style="color:#d29922">WARNING</span> - Primitive reads (bool/int) &rarr; <strong>usually OK</strong> unless correlated with other state
    </div>
    <table class="violations-table">
        <thead>
            <tr>
                <th>Attribute</th>
                <th>CRITICAL</th>
                <th>DANGER</th>
                <th>WARNING</th>
                <th>Why it's flagged</th>
            </tr>
        </thead>
        <tbody id="violationsBody"></tbody>
    </table>
</div>

<div id="log" class="panel">
    <div class="timeline-controls" style="margin-bottom:12px">
        <label>Show:
            <select id="logFilter" onchange="renderLog()">
                <option value="all">All Events</option>
                <option value="critical">CRITICAL Only</option>
                <option value="critical+danger">CRITICAL + DANGER</option>
                <option value="violations">All Violations</option>
                <option value="locks">Lock Events</option>
            </select>
        </label>
    </div>
    <div class="event-log" id="eventLog"></div>
</div>

<div class="tooltip" id="tooltip"></div>

<script>
const events = {events_json};
const threads = {threads_json};
const violations = {violations_json};
const threadColors = ['#58a6ff','#3fb950','#d29922','#f778ba','#bc8cff','#79c0ff','#f0883e','#a5d6ff'];

// Tab switching
function switchTab(name) {{
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
    document.getElementById(name).classList.add('active');
    event.target.classList.add('active');
    if (name === 'timeline') drawTimeline();
}}

// Populate thread filter
const threadSelect = document.getElementById('filterThread');
threads.forEach(t => {{
    const opt = document.createElement('option');
    opt.value = t; opt.textContent = t;
    threadSelect.appendChild(opt);
}});

// Violations table
const vBody = document.getElementById('violationsBody');
Object.entries(violations).sort((a,b) => {{
    const aScore = (a[1].critical_r + a[1].critical_w) * 100 + (a[1].danger_r + a[1].danger_w) * 10 + a[1].warning_r + a[1].warning_w;
    const bScore = (b[1].critical_r + b[1].critical_w) * 100 + (b[1].danger_r + b[1].danger_w) * 10 + b[1].warning_r + b[1].warning_w;
    return bScore - aScore;
}}).forEach(([attr, data]) => {{
    const critTotal = data.critical_r + data.critical_w;
    const dangTotal = data.danger_r + data.danger_w;
    const warnTotal = data.warning_r + data.warning_w;
    const tr = document.createElement('tr');

    const critCell = critTotal > 0
        ? `<span class="badge critical">${{critTotal}}</span>` + (data.critical_w > 0 ? ` <span style="color:#ff2d20;font-size:10px">${{data.critical_w}}W</span>` : '')
        : '<span style="color:#3fb950">0</span>';
    const dangCell = dangTotal > 0
        ? `<span class="badge danger">${{dangTotal}}</span>` + (data.danger_w > 0 ? ` <span style="color:#f85149;font-size:10px">${{data.danger_w}}W</span>` : '')
        : '<span style="color:#3fb950">0</span>';
    const warnCell = warnTotal > 0
        ? `<span class="badge warning">${{warnTotal}}</span>`
        : '<span style="color:#3fb950">0</span>';

    const reasonsHtml = data.reasons.map(r => `<div class="reason-text">&bull; ${{r}}</div>`).join('');

    tr.innerHTML = `
        <td><strong>${{attr}}</strong></td>
        <td>${{critCell}}</td>
        <td>${{dangCell}}</td>
        <td>${{warnCell}}</td>
        <td>${{reasonsHtml}}<div class="stack-trace" style="margin-top:6px">${{data.stacks.slice(0,3).join('\\n')}}</div></td>
    `;
    vBody.appendChild(tr);
}});

// Timeline drawing
const canvas = document.getElementById('timelineCanvas');
const ctx = canvas.getContext('2d');
const ROW_H = 40;
const LEFT_MARGIN = 160;
const TOP_MARGIN = 30;

function drawTimeline() {{
    const container = document.getElementById('timelineContainer');
    const filter = document.getElementById('filterType').value;
    const threadFilter = document.getElementById('filterThread').value;
    const zoom = parseInt(document.getElementById('zoomLevel').value);

    let filtered = events;
    if (filter === 'critical') filtered = filtered.filter(e => e.severity === 'critical');
    else if (filter === 'critical+danger') filtered = filtered.filter(e => e.severity === 'critical' || e.severity === 'danger');
    else if (filter === 'violations') filtered = filtered.filter(e => e.severity !== 'safe');
    else if (filter === 'locks') filtered = filtered.filter(e => e.type === 'lock');
    if (threadFilter !== 'all') filtered = filtered.filter(e => e.thread === threadFilter);

    const visibleThreads = [...new Set(filtered.map(e => e.thread))];
    if (visibleThreads.length === 0) visibleThreads.push('(no events)');

    const maxTime = filtered.length > 0 ? filtered[filtered.length-1].time : 1;
    const width = Math.max(LEFT_MARGIN + maxTime * zoom * 100 + 100, container.clientWidth);
    const height = TOP_MARGIN + visibleThreads.length * ROW_H + 20;

    canvas.width = width * devicePixelRatio;
    canvas.height = height * devicePixelRatio;
    canvas.style.width = width + 'px';
    canvas.style.height = height + 'px';
    ctx.scale(devicePixelRatio, devicePixelRatio);

    // Background
    ctx.fillStyle = '#0d1117';
    ctx.fillRect(0, 0, width, height);

    // Thread lanes
    visibleThreads.forEach((thread, i) => {{
        const y = TOP_MARGIN + i * ROW_H;
        // Alternating bg
        if (i % 2 === 0) {{
            ctx.fillStyle = '#161b22';
            ctx.fillRect(0, y, width, ROW_H);
        }}
        // Label
        ctx.fillStyle = threadColors[i % threadColors.length];
        ctx.font = '12px "Segoe UI", system-ui';
        ctx.textAlign = 'right';
        ctx.fillText(thread, LEFT_MARGIN - 12, y + ROW_H/2 + 4);
        // Lane line
        ctx.strokeStyle = '#21262d';
        ctx.beginPath();
        ctx.moveTo(LEFT_MARGIN, y + ROW_H);
        ctx.lineTo(width, y + ROW_H);
        ctx.stroke();
    }});

    // Time axis
    ctx.fillStyle = '#484f58';
    ctx.font = '10px monospace';
    ctx.textAlign = 'center';
    const timeStep = Math.max(0.1, 1 / zoom);
    for (let t = 0; t <= maxTime; t += timeStep) {{
        const x = LEFT_MARGIN + t * zoom * 100;
        ctx.fillText(t.toFixed(2) + 's', x, TOP_MARGIN - 8);
        ctx.strokeStyle = '#21262d';
        ctx.beginPath();
        ctx.moveTo(x, TOP_MARGIN);
        ctx.lineTo(x, height);
        ctx.stroke();
    }}

    // Events
    canvas._events = [];  // Store for hover
    filtered.forEach((e, idx) => {{
        const threadIdx = visibleThreads.indexOf(e.thread);
        if (threadIdx < 0) return;
        const x = LEFT_MARGIN + e.time * zoom * 100;
        const y = TOP_MARGIN + threadIdx * ROW_H + ROW_H/2;

        let color, size, shape;
        if (e.type === 'lock') {{
            color = e.action === 'blocked' ? '#bc8cff' : (e.action === 'acquire' ? '#3fb950' : '#484f58');
            size = 5;
            shape = 'diamond';
        }} else {{
            const sevColors = {{ critical: '#ff2d20', danger: '#f85149', warning: '#d29922', safe: '#3fb95040' }};
            color = sevColors[e.severity] || '#484f58';
            size = e.severity === 'safe' ? 2 : (e.severity === 'critical' ? 8 : (e.severity === 'danger' ? 6 : 4));
            shape = e.action === 'write' ? 'square' : 'circle';
        }}

        ctx.fillStyle = color;
        ctx.beginPath();
        if (shape === 'circle') {{
            ctx.arc(x, y, size, 0, Math.PI*2);
        }} else if (shape === 'square') {{
            ctx.rect(x - size, y - size, size*2, size*2);
        }} else {{
            ctx.moveTo(x, y - size);
            ctx.lineTo(x + size, y);
            ctx.lineTo(x, y + size);
            ctx.lineTo(x - size, y);
            ctx.closePath();
        }}
        ctx.fill();

        // Glow for critical/danger violations
        if (e.severity === 'critical') {{
            ctx.shadowColor = '#ff2d20';
            ctx.shadowBlur = 12;
            ctx.fill();
            ctx.shadowBlur = 0;
        }} else if (e.severity === 'danger') {{
            ctx.shadowColor = '#f85149';
            ctx.shadowBlur = 6;
            ctx.fill();
            ctx.shadowBlur = 0;
        }}

        canvas._events.push({{ x, y, size: Math.max(size, 6), event: e }});
    }});
}}

// Tooltip on hover
canvas.addEventListener('mousemove', (evt) => {{
    const rect = canvas.getBoundingClientRect();
    const mx = evt.clientX - rect.left;
    const my = evt.clientY - rect.top;
    const tooltip = document.getElementById('tooltip');

    let found = null;
    for (const item of (canvas._events || [])) {{
        if (Math.abs(mx - item.x) < item.size + 3 && Math.abs(my - item.y) < item.size + 3) {{
            found = item.event;
            break;
        }}
    }}

    if (found) {{
        const sevColors = {{ critical: '#ff2d20', danger: '#f85149', warning: '#d29922', safe: '#3fb950' }};
        const sevLabels = {{ critical: 'CRITICAL', danger: 'DANGER', warning: 'WARNING', safe: 'SAFE' }};
        const col = sevColors[found.severity] || '#8b949e';
        const lockStr = found.has_lock === undefined ? '' : (found.has_lock ? ' &#x1F512;' : ' &#x1F513; NO LOCK');
        tooltip.innerHTML = `
            <div style="color:${{col}}">
                <strong>${{sevLabels[found.severity] || ''}} ${{found.type === 'lock' ? found.action.toUpperCase() : found.action.toUpperCase() + ' ' + found.detail}}</strong>
                ${{lockStr}}
            </div>
            ${{found.reason ? `<div style="margin-top:4px;color:#c9d1d9;font-size:11px">${{found.reason}}</div>` : ''}}
            <div style="margin-top:4px;color:#8b949e">Thread: ${{found.thread}}</div>
            <div style="color:#484f58">t = ${{found.time.toFixed(4)}}s</div>
            <div class="stack-trace" style="margin-top:6px">${{found.stack.join('<br>')}}</div>
        `;
        tooltip.style.display = 'block';
        tooltip.style.left = (evt.clientX + 12) + 'px';
        tooltip.style.top = (evt.clientY + 12) + 'px';
    }} else {{
        tooltip.style.display = 'none';
    }}
}});

canvas.addEventListener('mouseleave', () => {{
    document.getElementById('tooltip').style.display = 'none';
}});

// Event log
function renderLog() {{
    const logDiv = document.getElementById('eventLog');
    const filter = document.getElementById('logFilter').value;
    let filtered = events;
    if (filter === 'critical') filtered = filtered.filter(e => e.severity === 'critical');
    else if (filter === 'critical+danger') filtered = filtered.filter(e => e.severity === 'critical' || e.severity === 'danger');
    else if (filter === 'violations') filtered = filtered.filter(e => e.severity !== 'safe' && e.type === 'access');
    else if (filter === 'locks') filtered = filtered.filter(e => e.type === 'lock');

    const sevIcons = {{ critical: '&#x1F4A5;', danger: '&#x26A0;', warning: '&#x1F514;', safe: '&#x2705;' }};
    const html = filtered.slice(0, 2000).map((e, i) => {{
        const threadIdx = threads.indexOf(e.thread);
        const cls = e.severity === 'critical' ? 'critical' : (e.severity === 'danger' ? 'danger' : (e.severity === 'warning' ? 'warning' : (e.action === 'blocked' ? 'blocked' : '')));
        const icon = e.type === 'access' ? (sevIcons[e.severity] || '') + ' ' : '';
        const detail = e.type === 'lock'
            ? `${{e.action.toUpperCase()}} ${{e.detail}}`
            : `${{icon}}${{e.action}} <strong>${{e.detail}}</strong>`;
        const reason = e.reason ? ` <span style="color:#6e7681;font-size:11px">&mdash; ${{e.reason}}</span>` : '';
        return `<div class="event-row ${{cls}}">
            <span class="event-time">${{e.time.toFixed(4)}}s</span>
            <span class="event-thread thread-${{threadIdx % 6}}">${{e.thread}}</span>
            <span class="event-detail">${{detail}}${{reason}}</span>
        </div>`;
    }}).join('');
    logDiv.innerHTML = html || '<div style="padding:20px;color:#8b949e">No events match filter</div>';
}}

// Controls
document.getElementById('filterType').addEventListener('change', drawTimeline);
document.getElementById('filterThread').addEventListener('change', drawTimeline);
document.getElementById('zoomLevel').addEventListener('input', (e) => {{
    document.getElementById('zoomLabel').textContent = e.target.value + 'x';
    drawTimeline();
}});

// Initial render
drawTimeline();
renderLog();
</script>
</body>
</html>"""


class TrackedDict(dict):
    """A dict subclass that logs mutations."""
    def __init__(self, original, name, detector):
        super().__init__(original)
        self._name = name
        self._detector = detector

    def __setitem__(self, key, value):
        self._detector._record_access(f"{self._name}[{key}]", 'write')
        super().__setitem__(key, value)

    def __delitem__(self, key):
        self._detector._record_access(f"{self._name}[{key}]", 'write')
        super().__delitem__(key)

    def __getitem__(self, key):
        self._detector._record_access(f"{self._name}[{key}]", 'read')
        return super().__getitem__(key)
